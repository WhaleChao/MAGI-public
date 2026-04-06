# -*- coding: utf-8 -*-
"""
Legal Crawler Wrapper (爬蟲包裝器)
Iron Dome Audit: ✅ SAFE — Calls existing trusted crawler
"""
import argparse
import fcntl
import os
import json
import random
import re
import subprocess
import logging
import threading
import time
import uuid
import sys
from datetime import datetime
from typing import Optional

from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import config_candidates


def _tools_api_default() -> str:
    try:
        from api.routing.service_registry import get_service_url
        return get_service_url("tools_api")
    except Exception:
        return "http://127.0.0.1:5003"

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 32, exc_info=True)
# Define path to original crawler
CRAWLER_PATH = os.path.expanduser("~/.openclaw/skills/law-office/legal_crawler.py")
VENV_PYTHON = f"{_MAGI_ROOT}/venv/bin/python3"
STATE_DIR = os.path.expanduser("~/.magi")
STATE_PATH = os.path.join(STATE_DIR, "crawler_guard_state.json")
MAX_ATTEMPTS = int(os.environ.get("MAGI_CRAWLER_RETRY_ATTEMPTS", "3"))
BASE_COOLDOWN_MIN = int(os.environ.get("MAGI_CRAWLER_BASE_COOLDOWN_MIN", "20"))
MAX_COOLDOWN_MIN = int(os.environ.get("MAGI_CRAWLER_MAX_COOLDOWN_MIN", "360"))
BG_JOB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bg_jobs")

logger = logging.getLogger("LegalCrawlerWrapper")

CLOSED_CASE_KEYWORDS = [
    "已結案",
    "結案",
    "終結",
    "已終結",
    "歸檔",
]

BLOCK_PATTERNS = [
    r"\b429\b",
    r"\b403\b",
    r"too many requests",
    r"access denied",
    r"blocked",
    r"forbidden",
    r"cloudflare",
    r"captcha",
    r"verify you are human",
    r"ip (?:ban|blocked|limit)",
]
TRANSIENT_PATTERNS = [
    r"timed? out",
    r"connection reset",
    r"connection aborted",
    r"temporary failure",
    r"service unavailable",
    r"remote disconnected",
]


def _load_state() -> dict:
    try:
        if not os.path.exists(STATE_PATH):
            return {"cooldown_until": 0, "blocked_count": 0, "last_error": "", "last_run": ""}
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 84, exc_info=True)
    return {"cooldown_until": 0, "blocked_count": 0, "last_error": "", "last_run": ""}


def _save_state(state: dict):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 94, exc_info=True)


def _detect_pattern(text: str, patterns) -> bool:
    body = (text or "").lower()
    for p in patterns:
        if re.search(p, body, re.IGNORECASE):
            return True
    return False


def _cooldown_remaining(state: dict) -> int:
    remain = int(float(state.get("cooldown_until", 0)) - time.time())
    return max(0, remain)


def _set_cooldown(state: dict, reason: str):
    blocked_count = int(state.get("blocked_count", 0)) + 1
    state["blocked_count"] = blocked_count
    cooldown_min = min(MAX_COOLDOWN_MIN, BASE_COOLDOWN_MIN * (2 ** max(0, blocked_count - 1)))
    state["cooldown_until"] = int(time.time()) + cooldown_min * 60
    state["last_error"] = reason[:400]
    state["last_run"] = datetime.now().isoformat()
    _save_state(state)


def _clear_cooldown(state: dict):
    state["cooldown_until"] = 0
    state["blocked_count"] = 0
    state["last_error"] = ""
    state["last_run"] = datetime.now().isoformat()
    _save_state(state)


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _crawler_job_paths(job_id: str) -> tuple[str, str]:
    return (
        os.path.join(BG_JOB_DIR, f"crawler_{job_id}.json"),
        os.path.join(BG_JOB_DIR, f"crawler_{job_id}.log"),
    )


def _read_crawler_job(job_id: str) -> dict:
    status_path, _ = _crawler_job_paths(job_id)
    if not os.path.exists(status_path):
        return {}
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_crawler_job(job_id: str, patch: dict) -> dict:
    os.makedirs(BG_JOB_DIR, exist_ok=True)
    status_path, _ = _crawler_job_paths(job_id)
    cur = _read_crawler_job(job_id)
    cur.update(patch or {})
    cur["job_id"] = job_id
    cur["updated_at"] = datetime.now().isoformat()
    tmp = status_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, status_path)
    return cur


def _latest_crawler_job_id() -> str:
    if not os.path.isdir(BG_JOB_DIR):
        return ""
    files = [
        os.path.join(BG_JOB_DIR, x)
        for x in os.listdir(BG_JOB_DIR)
        if x.startswith("crawler_") and x.endswith(".json")
    ]
    if not files:
        return ""
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return os.path.basename(files[0])[len("crawler_") : -len(".json")]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _run_once(timeout_sec: int = 1200):
    return subprocess.run(
        [VENV_PYTHON, CRAWLER_PATH],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )

def _run_osc_index_cases() -> str:
    """
    Best-effort: refresh local DB case index before downstream crawlers.
    This avoids false "no active cases" skips when Synology is slow.
    """
    import requests
    tools_api = os.environ.get("MAGI_TOOLS_API", _tools_api_default()).rstrip("/")
    payload = {
        "max_cases": int(os.environ.get("MAGI_CASE_INDEX_MAX_CASES", "220") or "220"),
        "max_files_per_case": int(os.environ.get("MAGI_CASE_INDEX_MAX_FILES_PER_CASE", "200") or "200"),
        "dry_run": False,
    }
    try:
        r = requests.post(
            f"{tools_api}/skills/run",
            json={
                "skill": "osc-orchestrator",
                "task": "index_cases " + json.dumps(payload, ensure_ascii=False),
                "timeout_sec": int(os.environ.get("MAGI_CASE_INDEX_TIMEOUT_SEC", "900") or "900"),
                "auto_repair": False,
                "rollback_on_fail": False,
                "auto_install_deps": False,
            },
            timeout=930,
        )
        if r.status_code == 200:
            data = r.json() or {}
            out = (data.get("output") or "").strip()
            try:
                obj = json.loads(out) if out else {}
            except Exception:
                obj = {}
            msg = (obj.get("message") or "").strip()
            if msg:
                return "✅ 案件索引: " + msg
            scanned = obj.get("scanned")
            updated = obj.get("updated")
            inserted = obj.get("inserted")
            if scanned is not None:
                return f"✅ 案件索引: scanned={scanned} inserted={inserted} updated={updated}"
            return "✅ 案件索引完成"
        return f"⚠️ 案件索引 HTTP {r.status_code}"
    except Exception as e:
        return f"⚠️ 案件索引失敗: {str(e)[:120]}"


def run_crawler_sync():
    """
    Executes crawler with anti-block cooldown and transient retry.
    """
    if not os.path.exists(CRAWLER_PATH):
        return f"❌ Crawler script not found at {CRAWLER_PATH}"

    state = _load_state()
    remain = _cooldown_remaining(state)
    if remain > 0:
        mins = max(1, remain // 60)
        until = datetime.fromtimestamp(time.time() + remain).strftime("%Y-%m-%d %H:%M:%S")
        return (
            "🛑 偵測到先前反爬/IP 風險，爬蟲進入冷卻中。\n"
            f"剩餘約 {mins} 分鐘（until {until}）。\n"
            "建議等待冷卻後再執行。"
        )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            logger.info(f"🕷️ Starting Legal Crawler (attempt {attempt}/{MAX_ATTEMPTS})...")
            result = _run_once(timeout_sec=1200)
            output = result.stdout or ""
            error = result.stderr or ""
            merged = f"{output}\n{error}"

            if result.returncode == 0:
                _clear_cooldown(state)
                logger.info("✅ Crawler finished successfully.")
                # Ensure local DB has up-to-date active case index before follow-up tasks.
                try:
                    idx_report = _run_osc_index_cases()
                except Exception as e:
                    idx_report = f"⚠️ 案件索引失敗（不影響爬蟲成功）：{e}"
                # Nightly add-on: refresh legal_insights for active cases (best-effort, non-fatal).
                try:
                    refresh = refresh_active_case_insights()
                except Exception as e:
                    refresh = f"⚠️ 實務見解更新失敗（不影響爬蟲成功）：{e}"
                # Nightly add-on 2: judgment-collector daily crawl (best-effort, non-fatal).
                jc_report = ""
                try:
                    jc_result = _run_judgment_collector_daily()
                    jc_report = jc_result
                except Exception as e:
                    jc_report = f"⚠️ 判決收集失敗（不影響爬蟲成功）：{e}"
                # Nightly add-on 3: transcript sync (best-effort, non-fatal).
                ts_report = ""
                try:
                    ts_result = _run_transcript_sync()
                    ts_report = ts_result
                except Exception as e:
                    ts_report = f"⚠️ 筆錄同步失敗（不影響爬蟲成功）：{e}"
                # Nightly add-on 4: file review email check (best-effort, non-fatal).
                fr_report = ""
                try:
                    fr_result = _run_file_review_check()
                    fr_report = fr_result
                except Exception as e:
                    fr_report = f"⚠️ 閱卷檢查失敗（不影響爬蟲成功）：{e}"
                # Nightly add-on 5: pdf-namer batch scan (best-effort, non-fatal).
                pn_report = ""
                try:
                    pn_result = _run_pdf_namer_nightly()
                    pn_report = pn_result
                except Exception as e:
                    pn_report = f"⚠️ PDF命名掃描失敗（不影響爬蟲成功）：{e}"
                return (
                    f"✅ 爬蟲執行完成:\n{output[-800:]}\n\n"
                    f"---\n[案件索引]\n{idx_report}\n\n"
                    f"---\n[實務見解更新]\n{refresh}\n\n"
                    f"---\n[判決收集]\n{jc_report}\n\n"
                    f"---\n[筆錄同步]\n{ts_report}\n\n"
                    f"---\n[閱卷檢查]\n{fr_report}\n\n"
                    f"---\n[PDF命名]\n{pn_report}"
                )

            if _detect_pattern(merged, BLOCK_PATTERNS):
                _set_cooldown(state, f"blocked pattern detected (rc={result.returncode})")
                remain2 = _cooldown_remaining(state)
                mins2 = max(1, remain2 // 60)
                return (
                    "🚫 爬蟲被目標站點阻擋（反爬/封鎖徵兆）。\n"
                    f"已自動進入冷卻 {mins2} 分鐘，避免 IP 風險擴大。\n"
                    f"錯誤摘要: {(error or output)[-300:]}"
                )

            if attempt < MAX_ATTEMPTS and _detect_pattern(merged, TRANSIENT_PATTERNS):
                sleep_sec = min(90, 6 * attempt + random.randint(0, 6))
                logger.warning(f"Transient crawler failure, retry after {sleep_sec}s...")
                time.sleep(sleep_sec)
                continue

            state["last_error"] = (error or output)[-400:]
            state["last_run"] = datetime.now().isoformat()
            _save_state(state)
            # Even if crawler fails (non-block), still try to refresh active case insights.
            try:
                refresh = refresh_active_case_insights()
            except Exception as e:
                refresh = f"⚠️ 實務見解更新失敗：{e}"
            return f"❌ 爬蟲執行失敗 (Code {result.returncode}):\n{error[-800:] or output[-800:]}\n\n---\n[實務見解更新]\n{refresh}"

        except subprocess.TimeoutExpired:
            if attempt < MAX_ATTEMPTS:
                sleep_sec = min(90, 8 * attempt + random.randint(0, 8))
                logger.warning(f"Crawler timeout, retry after {sleep_sec}s...")
                time.sleep(sleep_sec)
                continue
            state["last_error"] = "crawler timeout"
            state["last_run"] = datetime.now().isoformat()
            _save_state(state)
            return "❌ 爬蟲執行逾時 (超過 20 分鐘)"
        except Exception as e:
            state["last_error"] = str(e)[:400]
            state["last_run"] = datetime.now().isoformat()
            _save_state(state)
            return f"❌ 執行錯誤: {e}"


_BG_LOCK_PATH = os.path.join(BG_JOB_DIR, ".crawler_bg.lock")


def _spawn_crawler_background() -> str:
    singleton = _truthy(os.environ.get("MAGI_CRAWLER_BG_SINGLETON", "1"))
    if singleton:
        # Use file lock to prevent race condition between check and spawn
        os.makedirs(BG_JOB_DIR, exist_ok=True)
        try:
            lock_fd = open(_BG_LOCK_PATH, "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "🕷️ 爬蟲背景任務正在啟動中，請稍後再試"
        try:
            latest = _latest_crawler_job_id()
            if latest:
                st = _read_crawler_job(latest)
                pid = int(st.get("pid") or 0)
                if st.get("running") and pid > 1 and _pid_alive(pid):
                    return f"🕷️ 爬蟲背景任務已執行中（job_id={latest}）"
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    os.makedirs(BG_JOB_DIR, exist_ok=True)
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    status_path, log_path = _crawler_job_paths(job_id)
    _write_crawler_job(
        job_id,
        {
            "status": "queued",
            "running": False,
            "queued_at": datetime.now().isoformat(),
            "status_path": status_path,
            "log_path": log_path,
        },
    )
    py = VENV_PYTHON if os.path.exists(VENV_PYTHON) else "python3"
    cmd = [py, os.path.abspath(__file__), "--task", "run_sync", "--job-id", job_id]
    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        threading.Thread(target=proc.wait, daemon=True).start()
        _write_crawler_job(
            job_id,
            {
                "status": "running",
                "running": True,
                "pid": int(proc.pid),
                "started_at": datetime.now().isoformat(),
            },
        )
        return f"🕷️ 爬蟲已於背景啟動（job_id={job_id}）"
    except Exception as e:
        _write_crawler_job(
            job_id,
            {
                "status": "failed",
                "running": False,
                "finished_at": datetime.now().isoformat(),
                "success": False,
                "error": f"spawn_failed: {e}",
            },
        )
        return f"❌ 背景啟動失敗: {e}"


def get_crawler_status(job_id: str = "") -> dict:
    jid = (job_id or "").strip()
    if not jid or jid == "latest":
        jid = _latest_crawler_job_id()
    if not jid:
        return {"success": False, "error": "no_background_job"}
    st = _read_crawler_job(jid)
    if not st:
        return {"success": False, "error": "job_not_found", "job_id": jid}
    pid = int(st.get("pid") or 0)
    if st.get("running") and pid > 1 and (not _pid_alive(pid)):
        status_name = str(st.get("status") or "")
        if status_name not in {"done", "failed"}:
            st = _write_crawler_job(jid, {"status": "stopped", "running": False, "finished_at": datetime.now().isoformat()})
        else:
            st = _write_crawler_job(jid, {"running": False})
    st["success"] = True
    return st


def run_crawler(background: Optional[bool] = None):
    if background is None:
        background = _truthy(os.environ.get("MAGI_CRAWLER_RUN_BACKGROUND", "1"))
    if background:
        return _spawn_crawler_background()
    return run_crawler_sync()

def _run_judgment_collector_daily() -> str:
    """
    Call judgment-collector daily_crawl via Tools API (best-effort).
    Follows the same pattern as refresh_active_case_insights.
    """
    import requests

    tools_api = os.environ.get("MAGI_TOOLS_API", _tools_api_default()).rstrip("/")
    try:
        r = requests.post(
            f"{tools_api}/skills/run",
            json={
                "skill": "judgment-collector",
                "task": "daily_crawl",
                "timeout_sec": int(os.environ.get("MAGI_JUDGMENT_CRAWL_TIMEOUT_SEC", "1200")),
                "auto_repair": False,
                "rollback_on_fail": True,
                "auto_install_deps": False,
            },
            timeout=1230,
        )
        if r.status_code == 200:
            data = r.json() or {}
            out = data.get("output") or ""
            try:
                obj = json.loads(out) if out else {}
            except Exception:
                obj = {}
            reasons = obj.get("reasons_processed", 0)
            return f"✅ 每日判決收集完成：{reasons} 案由處理"
        else:
            return f"judgment-collector HTTP {r.status_code}"
    except Exception as e:
        return f"judgment-collector 失敗: {str(e)[:120]}"


def _run_transcript_sync() -> str:
    """
    Call transcript-downloader sync via Tools API (best-effort).
    Downloads new transcripts + renames existing ones.
    """
    import requests

    tools_api = os.environ.get("MAGI_TOOLS_API", _tools_api_default()).rstrip("/")
    try:
        r = requests.post(
            f"{tools_api}/skills/run",
            json={
                "skill": "transcript-downloader",
                "task": "sync",
                "timeout_sec": int(os.environ.get("MAGI_TRANSCRIPT_SYNC_TIMEOUT_SEC", "900")),
                "auto_repair": False,
                "rollback_on_fail": True,
                "auto_install_deps": False,
            },
            timeout=930,
        )
        if r.status_code == 200:
            data = r.json() or {}
            out = data.get("output") or ""
            return f"✅ 筆錄同步完成: {out[:200]}"
        else:
            return f"transcript-downloader HTTP {r.status_code}"
    except Exception as e:
        return f"transcript-downloader 失敗: {str(e)[:120]}"


def _run_file_review_check() -> str:
    """
    Call file-review-orchestrator check_emails + download via Tools API (best-effort).
    """
    import requests

    tools_api = os.environ.get("MAGI_TOOLS_API", _tools_api_default()).rstrip("/")
    results = []
    for task_name in ("check_emails", "download"):
        try:
            r = requests.post(
                f"{tools_api}/skills/run",
                json={
                    "skill": "file-review-orchestrator",
                    "task": task_name,
                    "timeout_sec": int(os.environ.get("MAGI_FILE_REVIEW_TIMEOUT_SEC", "600")),
                    "auto_repair": False,
                    "rollback_on_fail": True,
                    "auto_install_deps": False,
                },
                timeout=630,
            )
            if r.status_code == 200:
                results.append(f"✅ {task_name}")
            else:
                results.append(f"⚠️ {task_name} HTTP {r.status_code}")
        except Exception as e:
            results.append(f"⚠️ {task_name} 失敗: {str(e)[:80]}")
    return " | ".join(results)


def _load_magi_config_profiles() -> list[dict]:
    """
    Best-effort read MAGI canonical config.json (mariadb_profiles).
    Keep it local-only; no network I/O here.
    """
    for p in (str(path) for path in config_candidates("config.json")):
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f) or {}
                profiles = cfg.get("mariadb_profiles") or []
                if isinstance(profiles, list):
                    return profiles
        except Exception:
            continue
    return []


def _get_db_config_local_first() -> dict:
    """
    Prefer local DB profiles when MAGI_PREFER_LOCAL_DB=1.
    Falls back to 127.0.0.1:3307 (Casper local).
    """
    prefer_local = str(os.environ.get("MAGI_PREFER_LOCAL_DB", "0")).strip().lower() in {"1", "true", "yes", "on"}
    profiles = _load_magi_config_profiles()
    want = ["Home_Local_Test", "Studio_Local"] if prefer_local else ["Studio_Local", "Home_Local_Test"]
    for name in want:
        for p in profiles:
            if (p.get("profile_name") or "").strip() != name:
                continue
            c = (p.get("config") or {})
            if c.get("host") and (c.get("user") or os.environ.get("OSC_DB_USER")) and c.get("database"):
                return {
                    "host": c.get("host"),
                    "port": int(c.get("port") or 3306),
                    "user": c.get("user") or os.environ.get("OSC_DB_USER", "python_user"),
                    "password": c.get("password") or os.environ.get("OSC_DB_PASSWORD", ""),
                    "database": c.get("database"),
                    "connection_timeout": int(c.get("connection_timeout") or 3),
                }
    return {"host": "127.0.0.1", "port": int(os.environ.get("MAGI_LOCAL_DB_PORT", "3306")), "user": os.environ.get("OSC_DB_USER", "casper_service"), "password": os.environ.get("OSC_DB_PASSWORD", ""), "database": "law_firm_data", "connection_timeout": 3}


def _scan_active_cases_from_db(max_cases: int = 10) -> list[dict]:
    """
    DB-first active case scan.
    This avoids Synology Drive stalls (os.scandir/os.listdir can hang under CloudStorage).
    """
    try:
        import mysql.connector  # type: ignore

        cfg = _get_db_config_local_first()
        conn = mysql.connector.connect(
            host=cfg["host"],
            port=int(cfg["port"]),
            user=cfg.get("user") or os.environ.get("OSC_DB_USER", "python_user"),
            password=cfg.get("password") or os.environ.get("OSC_DB_PASSWORD", ""),
            database=cfg["database"],
            connection_timeout=int(cfg.get("connection_timeout") or 3),
            charset="utf8mb4",
            collation="utf8mb4_unicode_ci",
        )
    except Exception:
        return []

    try:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SHOW TABLES LIKE 'cases'")
            if not cur.fetchone():
                return []
            limit = max(20, int(max_cases) * 8)
            cur.execute(
                """
                SELECT case_number, folder_path, case_type, case_reason, status, updated_at, created_date
                FROM cases
                WHERE (status IS NULL OR status='' OR status='進行中')
                  AND (folder_path IS NOT NULL AND folder_path <> '')
                ORDER BY COALESCE(updated_at, created_date) DESC, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall() or []
        finally:
            try:
                cur.close()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 644, exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 649, exc_info=True)

    out: list[dict] = []
    for r in rows:
        p = (r.get("folder_path") or "").strip()
        if not p:
            continue
        name = os.path.basename(p.rstrip(os.sep))
        if not name or any(k in name for k in CLOSED_CASE_KEYWORDS):
            continue
        out.append(
            {
                "name": name,
                "path": p,
                "db_case_type": (r.get("case_type") or "").strip(),
                "db_case_reason": (r.get("case_reason") or "").strip(),
            }
        )
        if len(out) >= int(max_cases):
            break
    return out


def _parse_reason_from_folder(name: str) -> str:
    n = (name or "").strip()
    parts = [p.strip() for p in n.split("-") if p.strip()]
    if not parts:
        return ""
    reason = parts[-1]
    # Minimal normalization
    reason = reason.replace("詐騙", "詐欺").replace("侵佔", "侵占")
    return reason


def _detect_case_domain(path: str) -> str:
    p = (path or "").replace("\\", "/")
    if "/行政/" in p:
        return "admin"
    return "general"


def refresh_active_case_insights() -> str:
    """
    Best-effort nightly refresh:
    - Pick N most recently modified active cases under Synology Drive
    - For each unique case_reason, run insight-flow-judicial-ingest with court filter and commit to DB
    """
    # DB-first to avoid CloudStorage stalls.
    cases = _scan_active_cases_from_db(max_cases=int(os.environ.get("MAGI_ACTIVE_CASES_REFRESH_MAX_CASES", "8")))
    if not cases:
        return "⚠️ 未找到可用的進行中案件（本機 DB / 索引為空），跳過。"

    # Pick unique reasons (keep order)
    reasons = []
    seen = set()
    for c in cases:
        ct0 = (c.get("db_case_type") or "").strip()
        if any(k in ct0 for k in ["顧問", "法律顧問", "常年顧問"]):
            continue
        r = (c.get("db_case_reason") or "").strip()
        if not r:
            r = _parse_reason_from_folder(c.get("name") or "")
        if any(k in (r or "") for k in ["顧問", "常年顧問"]):
            continue
        if not r:
            continue
        k = r.lower()
        if k in seen:
            continue
        seen.add(k)
        # Prefer DB case_type if present; otherwise infer from path.
        domain = (c.get("db_case_type") or "").strip()
        if not domain:
            domain = _detect_case_domain(c.get("path") or "")
        reasons.append({"reason": r, "domain": domain})
        if len(reasons) >= int(os.environ.get("MAGI_ACTIVE_CASES_REFRESH_MAX_REASONS", "3")):
            break

    if not reasons:
        return "⚠️ 近期案件資料夾無法解析案由，跳過。"

    # Call Tools API to run existing ingest skill (so Iron Dome & skill versioning apply).
    import requests

    tools_api = os.environ.get("MAGI_TOOLS_API", _tools_api_default()).rstrip("/")
    profile_name = os.environ.get("MAGI_INSIGHT_DB_PROFILE", "").strip()
    max_results = int(os.environ.get("MAGI_ACTIVE_CASES_REFRESH_MAX_RESULTS", "2"))

    lines = []
    for item in reasons:
        reason = item["reason"]
        domain = item["domain"]
        courts = ["最高法院"] if domain != "admin" else ["最高行政法院(含改制前行政法院)"]
        payload = {
            "keywords": reason,
            "max_results": max_results,
            "max_chars": 40000,
            "commit": True,
            "profile_name": profile_name,
            "courts": courts,
        }
        try:
            r = requests.post(
                f"{tools_api}/skills/run",
                json={
                    "skill": "insight-flow-judicial-ingest",
                    "task": "ingest " + json.dumps(payload, ensure_ascii=False),
                    "timeout_sec": int(os.environ.get("MAGI_ACTIVE_CASES_REFRESH_TIMEOUT_SEC", "900")),
                    "auto_repair": True,
                    "rollback_on_fail": True,
                    "auto_install_deps": False,
                },
                timeout=930,
            )
            if r.status_code == 200:
                data = r.json() or {}
                # Tools API wraps stdout JSON in "output"
                out = data.get("output") or ""
                try:
                    obj = json.loads(out) if out else {}
                except Exception:
                    obj = {}
                added = obj.get("added")
                processed = obj.get("processed")
                lines.append(f"- {reason}（{courts[0]}）：processed={processed} added={added}")
            else:
                lines.append(f"- {reason}：HTTP {r.status_code}")
        except Exception as e:
            lines.append(f"- {reason}：失敗 {str(e)[:120]}")

    return "\n".join(lines) if lines else "（無更新）"

def _run_pdf_namer_nightly():
    """
    Nightly pdf-namer routine:
    1. Sync doc_rules + pending learns
    2. Process scan folder (analyze + match + file + LINE report)
    """
    import sys
    pdf_namer_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pdf-namer")
    sys.path.insert(0, pdf_namer_dir)
    
    reports = []
    
    # Step 1: DB sync
    try:
        from training_loader import sync_db_to_training, sync_pending_learns
        sync_result = sync_db_to_training()
        reports.append(f"DB同步: {sync_result.get('db_entries', 0)} 筆 doc_rules")
        
        pending = sync_pending_learns()
        if pending > 0:
            reports.append(f"待處理學習紀錄同步: {pending} 筆")
    except Exception as e:
        reports.append(f"DB同步失敗: {e}")
    
    # Step 2: Smart filing — scan inbox → analyze → match case → file (with LINE report)
    try:
        from smart_filer import process_scan_folder
        result = process_scan_folder(dry_run=False, notify=True)
        
        filed = len(result.get("filed", []))
        failed = len(result.get("failed", []))
        unnamed = len(result.get("unnamed", []))
        
        reports.append(f"歸檔: ✅{filed} ⚠️{failed} ❌{unnamed}")
        
        if result.get("message"):
            reports.append(result["message"])
    except Exception as e:
        reports.append(f"歸檔失敗: {e}")
    
    logger.info(f"[pdf-namer nightly] {'; '.join(reports)}")
    return "\n".join(reports)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Legal crawler wrapper")
    ap.add_argument("--task", default="run", help="run | run_sync | status")
    ap.add_argument("--job-id", default="", help="Background job id")
    args = ap.parse_args()

    task = (args.task or "run").strip()
    if task == "run":
        print(run_crawler())
    elif task == "run_sync":
        job_id = (args.job_id or "").strip()
        if job_id:
            _write_crawler_job(
                job_id,
                {
                    "status": "running",
                    "running": True,
                    "started_at": datetime.now().isoformat(),
                },
            )
        out = run_crawler_sync()
        if job_id:
            ok = str(out or "").strip().startswith("✅")
            _write_crawler_job(
                job_id,
                {
                    "status": "done" if ok else "failed",
                    "running": False,
                    "success": bool(ok),
                    "finished_at": datetime.now().isoformat(),
                    "result": str(out or "")[-4000:],
                },
            )
        print(out)
    elif task == "status":
        print(json.dumps(get_crawler_status(args.job_id or "latest"), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"success": False, "error": f"unknown task: {task}"}, ensure_ascii=False))
