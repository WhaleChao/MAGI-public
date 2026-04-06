#!/usr/bin/env python3
"""
file-review-orchestrator -- 閱卷系統協調器
=============================================
包裝 file_review_automation.FileReviewManager，
提供 CASPER skill API 與 LINE/DC 指令介面。

Usage (CLI):
    python action.py --task 'apply {"court_code":"TPD","year":"114","case_type":"訴","case_number":"123"}'
    python action.py --task 'download'
    python action.py --task 'check_emails'
    python action.py --task 'help'
"""
import argparse
import glob
import json
import logging
import os
import re
import shutil
import threading
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional
import subprocess
import uuid

# Ensure .env is loaded (critical when run as subprocess)
_magi_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_env_path = os.path.join(_magi_root, ".env")
if os.path.isfile(_env_path):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path, override=False)
    except ImportError:
        # Manual fallback: parse KEY=VALUE lines
        with open(_env_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    _k = _k.strip()
                    _v = _v.strip()
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v

# Long output → export as TXT to /static/exports and share URL/path
try:
    if _magi_root not in sys.path:
        sys.path.insert(0, _magi_root)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from ops.export_text import export_txt  # type: ignore
except Exception:
    export_txt = None  # type: ignore

from api.runtime_paths import (
    get_config_path,
    get_json_dir,
    get_module_path,
    get_orch_dir,
    get_skill_python,
)
from api.case_path_mapper import translate_case_path_to_local
from api.product_runtime import apply_product_runtime_env, product_profile_report

ORCH_DIR = str(get_orch_dir())
FILE_REVIEW_RUNTIME = apply_product_runtime_env("file_review", env=os.environ)

# ---------------------------------------------------------------------------
# Prefer project venv (avoids PEP 668 / Homebrew "externally-managed" pip issues)
# ---------------------------------------------------------------------------
_VENV_PY = str(get_skill_python())
try:
    _target_prefix = os.path.realpath(str(Path(_VENV_PY).expanduser().parent.parent))
    _current_prefix = os.path.realpath(sys.prefix)
    if os.path.exists(_VENV_PY) and _current_prefix != _target_prefix:
        os.execv(_VENV_PY, [_VENV_PY, __file__, *sys.argv[1:]])
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 79, exc_info=True)

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------
CODE_DIR = ORCH_DIR
CONFIG_PATH = str(get_config_path("config.json"))
DEFAULT_DOWNLOAD_FOLDER = os.path.expanduser("~/Desktop/MAGI_v2/閱卷下載")
JSON_DIR = str(get_json_dir())
BG_JOB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bg_jobs")
RECENT_ACTIVITY_STATE_FILE = ".recent_activity_notified.json"

# Safety-first defaults: never auto-route uncertain cases.
os.environ.setdefault("MAGI_ALLOW_RISKY_CASE_SCAN", "0")
os.environ.setdefault("MAGI_ALLOW_FILENAME_HEURISTIC_ARCHIVE", "1")
os.environ.setdefault("MAGI_REQUIRE_CASE_SIGNAL_FOR_AUTO", "1")
os.environ.setdefault("MAGI_ALLOW_LOOSE_CASE_FOLDER_FALLBACK", "0")
os.environ.setdefault("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "1")
os.environ.setdefault("MAGI_ENABLE_PRECLICK_SMART_SKIP", "1")

logger = logging.getLogger("file-review-orchestrator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", stream=sys.stderr)

def _cleanup_old_downloads(download_folder: str, max_days: int = 15):
    """Clean up downloaded YYYYMMDD date-folders older than max_days.

    Applies to: 閱卷下載/, 筆錄下載/, 法扶資料/ 下的 YYYYMMDD 暫存資料夾。
    """
    if not download_folder or not os.path.exists(download_folder):
        return

    # [Safety Guard] Ensure we only delete inside a MAGI folder, protecting case folders
    abs_folder = os.path.abspath(download_folder)
    safe_markers = ("MAGI", "閱卷下載", "筆錄下載", "法扶資料")
    if not any(m in abs_folder for m in safe_markers):
        logger.warning("Safety abort: download_folder %s does not contain safe markers. Cleanup aborted to protect case folders.", abs_folder)
        return

    import time, shutil
    try:
        now = time.time()
        for item in os.listdir(download_folder):
            item_path = os.path.join(download_folder, item)
            # Only clean up YYYYMMDD folders
            if not os.path.isdir(item_path) or not item.isdigit() or len(item) != 8:
                continue
            try:
                # use modification time of the folder
                mtime = os.path.getmtime(item_path)
                if (now - mtime) > (max_days * 86400):
                    shutil.rmtree(item_path, ignore_errors=True)
                    logger.info("Cleaned up old download folder: %s", item_path)
            except Exception as e:
                logger.warning("Failed to check/cleanup %s: %s", item_path, e)
    except Exception as e:
        logger.warning("Cleanup old downloads failed: %s", e)


def _cleanup_all_download_folders(base_dir: str, max_days: int = 15):
    """對 閱卷下載/、筆錄下載/、法扶資料/ 都執行舊資料夾清理。"""
    if not base_dir:
        return
    for sub in ("閱卷下載", "筆錄下載", "法扶資料"):
        folder = os.path.join(base_dir, sub)
        if os.path.isdir(folder):
            _cleanup_old_downloads(folder, max_days=max_days)


def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    """
    Best-effort：將閱卷流程的關鍵事件寫入向量記憶，供對話追溯。
    """
    try:
        if CODE_DIR not in sys.path:
            sys.path.insert(0, CODE_DIR)
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(
            event,
            ok=ok,
            payload=payload or {},
            tags=tags or {},
            source="file_review_orchestrator",
        )
    except Exception:
        return


def _token_backups(token_path: str) -> list[str]:
    base = (token_path or "").strip()
    if not base:
        return []
    pats = [f"{base}.bak_*", f"{base}.invalid_*"]
    out: list[str] = []
    for p in pats:
        out.extend(glob.glob(p))
    out = [p for p in out if os.path.exists(p)]
    out.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out


def _restore_latest_token_backup(token_path: str) -> dict:
    target = (token_path or "").strip()
    if not target:
        return {"success": False, "error": "missing token_path"}
    cand = _token_backups(target)
    if not cand:
        return {"success": False, "error": "no backup token found"}
    src = cand[0]
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            keep = f"{target}.pre_restore_{ts}"
            shutil.copy2(target, keep)
        shutil.copy2(src, target)
        return {"success": True, "restored_from": src}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}

class _SimpleCase:
    def __init__(self, row: dict):
        self._row = row or {}
        self.folder_path = self._row.get("folder_path")


class _SimpleMariaDB:
    """
    輕量 DB wrapper（避免 legalbridge_core import 牽扯 linebot 等完整依賴）。
    只提供 file_review_automation 需要的方法：execute/fetch_all/find_case/translate_path_to_local。
    """

    def __init__(self, db_config: dict, path_hints: Optional[dict] = None):
        self._db_config = dict(db_config or {})
        self._path_hints = path_hints or {}

    def get_connection(self):
        import pymysql
        cfg = dict(self._db_config)
        # 兼容 config key 名稱：connection_timeout -> connect_timeout
        if "connection_timeout" in cfg and "connect_timeout" not in cfg:
            cfg["connect_timeout"] = cfg.pop("connection_timeout")
        cfg.setdefault("autocommit", True)
        cfg.setdefault("cursorclass", pymysql.cursors.DictCursor)
        return pymysql.connect(**cfg)

    def execute(self, query: str, params: tuple = None, fetch: str = None):
        conn = None
        try:
            conn = self.get_connection()
            cur = conn.cursor()
            cur.execute(query, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            conn.commit()
            return cur.lastrowid
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 241, exc_info=True)

    def fetch_all(self, query: str, params: tuple = None, as_dict: bool = True):
        conn = None
        try:
            conn = self.get_connection()
            if as_dict:
                cur = conn.cursor()
            else:
                import pymysql
                cur = conn.cursor(pymysql.cursors.Cursor)
            cur.execute(query, params)
            return cur.fetchall()
        except Exception:
            return []
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 261, exc_info=True)

    def find_case(self, case_number: str):
        if not case_number:
            return None
        row = self.execute(
            "SELECT * FROM cases WHERE case_number=%s LIMIT 1",
            (case_number,),
            fetch="one",
        )
        if not row:
            return None
        return _SimpleCase(row)

    def translate_path_to_local(self, path: str) -> str:
        """
        盡量把 DB 內 Windows 路徑換成本機實際路徑（macOS SynologyDrive）。
        若無法判斷就原樣回傳，讓後續降級掃描接手。
        """
        return translate_case_path_to_local(path)


def _sanitize_db_config(cfg: dict) -> dict:
    safe = dict(cfg or {})
    if "password" in safe:
        safe["password"] = "***"
    return safe


def _pick_db_profiles(cfg: dict, prefer: str = "") -> list:
    profiles = cfg.get("mariadb_profiles", []) or []
    if not isinstance(profiles, list):
        return []

    # 若 Keeper/主 DB 未開機，優先使用本機測試 DB（避免每次都先打 VPN/3306，造成大量「連線失敗」噪音）
    prefer_local = os.environ.get("MAGI_PREFER_LOCAL_DB", "").strip().lower() in {"1", "true", "yes", "on"}
    env_prefer = (prefer or os.environ.get("MAGI_DB_PREFER_PROFILE", "")).strip()
    if prefer_local and not env_prefer:
        # 常見本機 profile name（兼容 config.json 變體）
        for cand in ["Home_Local_Test", "Home_Local", "Local_Test", "Local"]:
            if any((p.get("profile_name") or "") == cand for p in profiles):
                env_prefer = cand
                break
        # 仍找不到就用 heuristic：127.0.0.1:3307 的那顆
        if not env_prefer:
            for p in profiles:
                dbc = p.get("config") or {}
                host = str(dbc.get("host") or "")
                port = str(dbc.get("port") or "")
                if host in {"127.0.0.1", "localhost"} and port == "3307":
                    env_prefer = (p.get("profile_name") or "").strip()
                    if env_prefer:
                        break

    prefer = env_prefer
    if prefer:
        head = [p for p in profiles if (p.get("profile_name") or "") == prefer]
        tail = [p for p in profiles if (p.get("profile_name") or "") != prefer]
        profiles = head + tail

    # Runtime override for CASPER service account (keeps manual OSC profile untouched).
    # Priority: OSC_DB_* > MAGI_REMOTE_DB_*
    o_host = (os.environ.get("OSC_DB_HOST") or os.environ.get("MAGI_REMOTE_DB_HOST") or "").strip()
    o_port = (os.environ.get("OSC_DB_PORT") or os.environ.get("MAGI_REMOTE_DB_PORT") or "").strip()
    o_user = (os.environ.get("OSC_DB_USER") or os.environ.get("MAGI_REMOTE_DB_USER") or "").strip()
    o_pass = (os.environ.get("OSC_DB_PASSWORD") or os.environ.get("MAGI_REMOTE_DB_PASSWORD") or "").strip()
    o_name = (os.environ.get("OSC_DB_NAME") or os.environ.get("MAGI_REMOTE_DB_NAME") or "").strip()
    if any([o_host, o_port, o_user, o_pass, o_name]):
        patched = []
        for p in profiles:
            item = dict(p or {})
            dbc = dict(item.get("config") or {})
            if o_host:
                dbc["host"] = o_host
            if o_port:
                try:
                    dbc["port"] = int(o_port)
                except Exception:
                    dbc["port"] = o_port
            if o_user:
                dbc["user"] = o_user
            if o_pass:
                dbc["password"] = o_pass
            if o_name:
                dbc["database"] = o_name
            item["config"] = dbc
            patched.append(item)
        profiles = patched
    return profiles


def cmd_db_smoke(prefer_profile: str = "") -> dict:
    """
    DB 冒煙測試：依序嘗試連線 mariadb_profiles，回報第一個可用的 profile 與表清單。
    不做任何寫入、不建立資料。
    """
    _ensure_runtime_deps()
    cfg = _load_config()
    profiles = _pick_db_profiles(cfg, prefer=prefer_profile)
    attempts = []

    for p in profiles:
        name = (p.get("profile_name") or "未命名").strip()
        dbc = p.get("config") or {}
        try:
            db = _SimpleMariaDB(dbc, path_hints=cfg.get("paths") or {})
            row = db.execute("SELECT 1 AS ok", fetch="one")
            tables = db.execute("SHOW TABLES", fetch="all") or []
            attempts.append({
                "profile_name": name,
                "ok": True,
                "host": dbc.get("host"),
                "port": dbc.get("port"),
                "database": dbc.get("database"),
                "tables": [list(t.values())[0] if isinstance(t, dict) and t else str(t) for t in tables][:50],
            })
            return {"success": True, "active_profile": name, "select_1": row, "attempts": attempts}
        except Exception as e:
            attempts.append({
                "profile_name": name,
                "ok": False,
                "host": dbc.get("host"),
                "port": dbc.get("port"),
                "database": dbc.get("database"),
                "error": str(e)[:200],
                "config": _sanitize_db_config(dbc),
            })

    return {"success": False, "error": "no reachable mariadb profile", "attempts": attempts}


def _ok(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _download_job_paths(job_id: str) -> tuple[str, str]:
    return (
        os.path.join(BG_JOB_DIR, f"download_{job_id}.json"),
        os.path.join(BG_JOB_DIR, f"download_{job_id}.log"),
    )


def _read_download_job(job_id: str) -> dict:
    status_path, _ = _download_job_paths(job_id)
    if not os.path.exists(status_path):
        return {}
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_download_job(job_id: str, patch: dict) -> dict:
    os.makedirs(BG_JOB_DIR, exist_ok=True)
    status_path, _ = _download_job_paths(job_id)
    cur = _read_download_job(job_id)
    cur.update(patch or {})
    cur["job_id"] = job_id
    cur["updated_at"] = datetime.now().isoformat()
    tmp = status_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, status_path)
    return cur


def _latest_download_job_id() -> str:
    if not os.path.isdir(BG_JOB_DIR):
        return ""
    files = [
        os.path.join(BG_JOB_DIR, x)
        for x in os.listdir(BG_JOB_DIR)
        if x.startswith("download_") and x.endswith(".json")
    ]
    if not files:
        return ""
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return os.path.basename(files[0])[len("download_") : -len(".json")]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load config: %s", e)
        return {}


def _get_credentials(cfg: dict) -> dict:
    jc = cfg.get("judicial", {})
    return {
        "username": (
            os.environ.get("MAGI_JUDICIAL_EEFILE_USERNAME")
            or os.environ.get("MAGI_JUDICIAL_RECORD_USERNAME")
            or jc.get("eefile_username", jc.get("record_username", ""))
        ),
        "password": (
            os.environ.get("MAGI_JUDICIAL_EEFILE_PASSWORD")
            or os.environ.get("MAGI_JUDICIAL_RECORD_PASSWORD")
            or jc.get("eefile_password", jc.get("record_password", ""))
        ),
        "download_folder": os.environ.get("MAGI_EEFILE_DOWNLOAD_FOLDER", "").strip()
                          or jc.get("eefile_download_folder", DEFAULT_DOWNLOAD_FOLDER),
        "headless": jc.get("headless", True),
    }


def _ensure_imports():
    """Lazy import file_review_automation, preferring MAGI's maintained copy."""
    import importlib.util

    candidates = [str(get_module_path("file_review_automation.py"))]
    for idx, path in enumerate(candidates):
        if not os.path.exists(path):
            continue
        mod_name = f"magi_file_review_automation_{idx}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("file_review_automation.py not found in MAGI")


def _ensure_portal_probe_imports():
    """
    Lazy import the portal-probe implementation.
    MAGI 版已包含 probe_downloadable_from_portal，優先使用。
    """
    import importlib.util

    candidates = [str(get_module_path("file_review_automation.py"))]
    last_mod = None
    for idx, path in enumerate(candidates):
        if not os.path.exists(path):
            continue
        mod_name = f"portal_probe_file_review_automation_{idx}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if not spec or not spec.loader:
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        last_mod = mod
        if hasattr(getattr(mod, "FileReviewManager", object), "probe_downloadable_from_portal"):
            return mod
    if last_mod is not None:
        return last_mod
    raise ImportError("file_review_automation.py not found for portal probe")

def _pip_install(pkgs):
    pkgs = [p for p in (pkgs or []) if (p or "").strip()]
    if not pkgs:
        return True
    try:
        cmd = [sys.executable, "-m", "pip", "install", *pkgs]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            # PEP 668 (externally-managed) fallback
            if "externally-managed" in err.lower() or "pep 668" in err.lower() or "--break-system-packages" in err:
                r2 = subprocess.run(cmd + ["--break-system-packages"], capture_output=True, text=True, timeout=900)
                if r2.returncode == 0:
                    return True
                err = (r2.stderr or r2.stdout or err).strip()
            logger.warning("pip install failed: %s", err[-400:])
            return False
        return True
    except Exception as e:
        logger.warning("pip install exception: %s", e)
        return False

def _ensure_runtime_deps():
    """
    Best-effort dependency bootstrap for:
    - Gmail API (google-api-python-client + auth libs)
    - DB (pymysql)
    """
    need = []
    try:
        import googleapiclient  # noqa: F401
    except Exception:
        need += ["google-api-python-client", "google-auth", "google-auth-oauthlib", "google-auth-httplib2"]
    try:
        import pymysql  # noqa: F401
    except Exception:
        need += ["pymysql"]
    try:
        import holidays  # noqa: F401
    except Exception:
        need += ["holidays"]
    if need:
        logger.info("Installing missing deps (best-effort): %s", ", ".join(sorted(set(need))))
        _pip_install(sorted(set(need)))

def _json_path(name: str) -> str:
    """Resolve credential/token file under JSON_DIR if present."""
    try:
        p = os.path.join(JSON_DIR, name)
        if JSON_DIR and os.path.exists(p):
            return p
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 577, exc_info=True)
    return name


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------
def _load_telegram_targets() -> tuple[str, list[str]]:
    token = (os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN") or "").strip()
    notify_ids = [
        x.strip()
        for x in (os.environ.get("MAGI_NOTIFY_TELEGRAM_IDS") or "").split(",")
        if x.strip()
    ]
    if token and notify_ids:
        return token, notify_ids
    try:
        _magi_cfg_path = str(get_config_path("config.json"))
        if os.path.exists(_magi_cfg_path):
            _magi_cfg = json.loads(open(_magi_cfg_path, "r", encoding="utf-8").read() or "{}")
            _magi_tg = _magi_cfg.get("telegram") or {}
            _magi_notify = _magi_tg.get("notifyTo") or []
            if isinstance(_magi_notify, list):
                notify_ids.extend([str(x).strip() for x in _magi_notify if str(x).strip()])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 602, exc_info=True)
    try:
        oc_path = os.path.join(os.path.expanduser("~"), ".openclaw", "openclaw.json")
        if os.path.exists(oc_path):
            cfg = json.loads(open(oc_path, "r", encoding="utf-8").read() or "{}")
            tg = (cfg.get("channels") or {}).get("telegram") or {}
            if not token:
                token = str(tg.get("botToken") or "").strip()
            notify_to = tg.get("notifyTo") or []
            if isinstance(notify_to, list):
                notify_ids.extend([str(x).strip() for x in notify_to if str(x).strip()])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 614, exc_info=True)
    dedup: list[str] = []
    seen: set[str] = set()
    for x in notify_ids:
        if x and x not in seen:
            seen.add(x)
            dedup.append(x)
    return token, dedup


def _notify_tg(text: str) -> bool:
    token, notify_ids = _load_telegram_targets()
    if not token or not notify_ids:
        return False
    msg_to_send = str(text or "")
    try:
        from api.tw_output_guard import normalize_output_text
        msg_to_send = normalize_output_text(msg_to_send, platform="TELEGRAM")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 633, exc_info=True)
    payload = json.dumps({"text": msg_to_send}, ensure_ascii=False).encode("utf-8")
    ok_any = False
    from urllib import request as _urlreq
    for chat_id in notify_ids:
        try:
            req = _urlreq.Request(
                f"https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with _urlreq.urlopen(req, timeout=10):
                pass
            ok_any = True
        except Exception:
            continue
    return ok_any


def _notify(text: str, flag: bool = True, topic_key: str = "filereview"):
    if not flag:
        return
    msg = str(text or "")
    try:
        from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore

        st = send_telegram_push_with_status(
            msg,
            severity="info",
            source="file_review_orchestrator",
            topic_key=topic_key,
            queue_on_fail=True,
        ) or {}
        if bool(st.get("telegram")) or bool(st.get("queued")):
            return
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 670, exc_info=True)
    _notify_tg(msg)


def _notify_file(file_path: str, caption: str = "", flag: bool = True):
    """Send a file (image/PDF/etc.) to admin via TG and DC."""
    if not flag:
        return
    if not file_path or not os.path.isfile(file_path):
        logger.warning("_notify_file: file not found: %s", file_path)
        return
    # Use LAFNotifier which now sends to both TG and DC
    try:
        import sys
        if CODE_DIR not in sys.path:
            sys.path.insert(0, CODE_DIR)
        from line_notifier import LAFNotifier
        LAFNotifier().notify_admin_with_files(
            caption or os.path.basename(file_path), [file_path],
            topic_key="filereview", source="file_review_orchestrator",
        )
        logger.info("File sent via LAFNotifier (TG+DC): %s", os.path.basename(file_path))
        return
    except Exception as e:
        logger.warning("LAFNotifier send failed: %s", e)
    # Fallback: TG-only via red_phone
    try:
        from skills.ops.red_phone import send_file_admin  # type: ignore
        result = send_file_admin(file_path, caption=caption, topic_key="filereview")
        if result.get("ok"):
            logger.info("File sent via red_phone (TG-only): %s", os.path.basename(file_path))
            return
        logger.warning("red_phone send_file_admin returned: %s", result)
    except Exception as e2:
        logger.warning("red_phone fallback also failed: %s", e2)


# ---------------------------------------------------------------------------
# DB Helper
# ---------------------------------------------------------------------------
def _get_db_manager(cfg: dict):
    try:
        # 在 Keeper/主 DB 未開機時，強制用本機 DB（避免 legalbridge_core 嘗試 VPN/3306 造成噪音與延遲）
        prefer_local = os.environ.get("MAGI_PREFER_LOCAL_DB", "").strip().lower() in {"1", "true", "yes", "on"}
        if prefer_local:
            raise RuntimeError("prefer_local_db")
        if CODE_DIR not in sys.path:
            sys.path.insert(0, CODE_DIR)
        from legalbridge_core import ConfigManager, DatabaseManager
        cfg_mgr = ConfigManager(config_path=CONFIG_PATH)
        return DatabaseManager(cfg_mgr)
    except Exception as e:
        # legalbridge_core 可能因 linebot 等依賴缺漏而無法 import；此處回退到輕量 DB。
        if str(e) == "prefer_local_db":
            logger.info("DB manager: prefer local DB (MAGI_PREFER_LOCAL_DB=1), fallback to simple db.")
        else:
            logger.warning("DB manager not available (fallback to simple db): %s", e)
        _ensure_runtime_deps()
        profiles = _pick_db_profiles(cfg)
        for p in profiles:
            dbc = p.get("config") or {}
            try:
                db = _SimpleMariaDB(dbc, path_hints=cfg.get("paths") or {})
                db.execute("SELECT 1", fetch="one")
                return db
            except Exception:
                continue
        return None


# ---------------------------------------------------------------------------
# Court Code Mapping (short aliases)
# ---------------------------------------------------------------------------
COURT_ALIASES = {
    "台北": "TPD", "臺北": "TPD", "北院": "TPD",
    "新北": "PCD", "板橋": "PCD",
    "士林": "SLD",
    "桃園": "TYD",
    "新竹": "SCD",
    "苗栗": "MLD",
    "台中": "TCD", "臺中": "TCD",
    "彰化": "CHD",
    "南投": "NTD",
    "雲林": "ULD",
    "嘉義": "CYD",
    "台南": "TND", "臺南": "TND",
    "高雄": "KSD",
    "屏東": "PTD",
    "花蓮": "HLD",
    "台東": "TTD", "臺東": "TTD",
    "宜蘭": "ILD",
    "基隆": "KLD",
    "澎湖": "PHD",
    "金門": "KMD",
    "連江": "LCD",
    # 高等法院
    "高院": "TPH", "台灣高等法院": "TPH",
    "高雄高分院": "KSH",
    "台中高分院": "TCH",
    "台南高分院": "TNH",
    "花蓮高分院": "HLH",
}


def _resolve_court_code(text: str) -> str:
    """Resolve court name alias to code."""
    text = text.strip()
    if text.upper() in ("TPD", "PCD", "SLD", "TYD", "SCD", "MLD", "TCD",
                         "CHD", "NTD", "ULD", "CYD", "TND", "KSD", "PTD",
                         "HLD", "TTD", "ILD", "KLD", "PHD", "KMD", "LCD",
                         "TPH", "KSH", "TCH", "TNH", "HLH"):
        return text.upper()
    return COURT_ALIASES.get(text, text)


# ---------------------------------------------------------------------------
# Core Commands
# ---------------------------------------------------------------------------
def cmd_apply(court_code: str, year: str, case_type: str,
              case_number: str, client_name: str = "",
              auto_submit: bool = True, notify: bool = True,
              folder_path: str = "") -> dict:
    """Apply for file review (閱卷聲請)."""
    if not all([court_code, year, case_type, case_number]):
        return {"success": False, "error": "missing required fields: court_code, year, case_type, case_number"}

    court_code = _resolve_court_code(court_code)
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        return {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_EEFILE_USERNAME/PASSWORD in .env"}

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        # ── 當事人自動補齊：未提供 client_name 時從 DB 查詢 ──
        if not client_name and db:
            court_case_no = f"{year}年度{case_type}字第{case_number}號"
            try:
                row = db.execute(
                    "SELECT client_name FROM cases "
                    "WHERE court_case_number LIKE %s LIMIT 1",
                    (f"%{year}%{case_type}%{case_number}%",),
                    fetch="one",
                )
                if row and row.get("client_name"):
                    client_name = row["client_name"].strip()
                    logger.info("自動從 DB 補齊當事人：%s（%s）", client_name, court_case_no)
            except Exception as db_e:
                logger.debug("DB 查詢當事人失敗（不影響聲請）：%s", db_e)
        if not client_name:
            logger.warning("⚠️ 未提供當事人姓名，閱卷系統可能拒絕聲請。建議格式：閱卷聲請 <法院> <案號> <當事人>")

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            # SSO login
            logger.info("Logging into SSO for file review...")
            if not mgr.login():
                msg = "❌ 閱卷登入失敗，可能驗證碼連錯或系統維護，已中斷自動聲請。"
                logger.error(msg)
                _notify(msg, notify)
                return {"success": False, "error": "sso_login_failed"}

            mgr.navigate_to_file_review()

            # Apply
            case_info = {
                "court_code": court_code,
                "year": str(year),
                "case_type": case_type,
                "case_number": str(case_number),
                "client_name": client_name,
            }
            if folder_path:
                case_info["folder_path"] = folder_path
            logger.info("Applying for review: %s", case_info)
            result = mgr.apply_for_review(case_info, auto_submit=auto_submit)

            label = f"{court_code} {year}年{case_type}字第{case_number}號"

            # Parse evidence from result (format: "Applied|{json}")
            evidence = {}
            result_key = result
            if isinstance(result, str) and "|" in result:
                result_key, _, evidence_str = result.partition("|")
                try:
                    evidence = json.loads(evidence_str)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 867, exc_info=True)

            if result_key == "Applied":
                app_no = evidence.get("application_number", "")
                msg = f"📋 閱卷聲請已送出 — {label}"
                if app_no:
                    msg += f"\n收件編號：{app_no}"
                if evidence.get("list_row_count"):
                    msg += f"\n列表確認：共 {evidence['list_row_count']} 筆"
            elif result_key == "Ready":
                msg = f"✅ 閱卷已聲請完成（表單已填寫，待確認送出） — {label}"
            else:
                msg = f"⚠️ 閱卷聲請結果: {result_key} — {label}"

            _notify(msg, notify)

            # Send evidence screenshot if available
            screenshot = evidence.get("screenshot", "")
            if screenshot and os.path.isfile(screenshot):
                _notify_file(screenshot, caption=f"聲請截圖 — {label}", flag=notify)
            list_screenshot = evidence.get("list_screenshot", "")
            if list_screenshot and os.path.isfile(list_screenshot):
                _notify_file(list_screenshot, caption=f"列表確認 — {label}", flag=notify)

            return {"success": True, "result": result_key, "case": label,
                    "message": msg, "evidence": evidence}

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Apply failed: %s", error_msg)
        _notify("❌ 閱卷聲請失敗: " + error_msg, notify)
        return {"success": False, "error": error_msg, "traceback": traceback.format_exc()[-500:]}


def cmd_upload_attachment(court_code: str, year: str, case_type: str,
                         case_number: str, client_name: str = "",
                         file_path: str = "", file_remark: str = "委任狀",
                         notify: bool = True) -> dict:
    """Upload attachment to an existing file review application."""
    if not all([court_code, year, case_type, case_number]):
        return {"success": False, "error": "missing required fields"}

    court_code = _resolve_court_code(court_code)

    # Auto-find the attachment file if not specified
    if not file_path:
        return {"success": False, "error": "file_path is required"}

    if not os.path.exists(file_path):
        return {"success": False, "error": f"file not found: {file_path}"}

    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        return {"success": False, "error": "missing credentials"}

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Logging into SSO for attachment upload...")
            if not mgr.login():
                msg = "❌ 閱卷登入失敗"
                _notify(msg, notify)
                return {"success": False, "error": "sso_login_failed"}

            mgr.navigate_to_file_review()

            case_info = {
                "court_code": court_code,
                "year": str(year),
                "case_type": case_type,
                "case_number": str(case_number),
                "client_name": client_name,
            }
            logger.info("Uploading attachment to: %s", case_info)
            result = mgr.upload_to_existing_application(
                case_info, file_path, file_remark=file_remark
            )

            label = f"{court_code} {year}年{case_type}字第{case_number}號"
            if result == "Uploaded":
                msg = f"✅ 附件已上傳 — {label} ({file_remark})"
            elif result == "NotFound":
                msg = f"⚠️ 找不到案件 — {label}"
            else:
                msg = f"❌ 附件上傳失敗 — {label} (結果: {result})"

            _notify(msg, notify)
            return {"success": result == "Uploaded", "result": result, "case": label, "message": msg}

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Upload attachment failed: %s", error_msg)
        _notify("❌ 附件上傳失敗: " + error_msg, notify)
        return {"success": False, "error": error_msg, "traceback": traceback.format_exc()[-500:]}


# ---------------------------------------------------------------------------
# 繳費憑證上傳
# ---------------------------------------------------------------------------
def cmd_upload_payment_proof(court_code: str, year: str, case_type: str,
                             case_number: str, client_name: str = "",
                             file_path: str = "", notify: bool = True) -> dict:
    """Upload payment proof screenshot to an existing file review application."""
    if not all([court_code, year, case_type, case_number]):
        return {"success": False, "error": "missing required fields"}

    court_code = _resolve_court_code(court_code)

    if not file_path or not os.path.exists(file_path):
        return {"success": False, "error": f"file not found: {file_path}"}

    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        return {"success": False, "error": "missing credentials"}

    # 去重檢查
    case_num_padded = str(case_number).zfill(6)
    raw_case_id = f"{year}.{case_type}.{case_num_padded}"
    registry_path = os.path.join(creds.get("download_folder", "./閱卷下載"), "payment_proof_registry.json")
    proof_registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as _rf:
                proof_registry = json.load(_rf)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1011, exc_info=True)

    # DB-backed dedup (primary), JSON fallback
    _proof_already_done = raw_case_id in proof_registry
    if not _proof_already_done:
        try:
            from skills.ops.dedup_db import is_done as _dd_is_done
            _proof_already_done = _dd_is_done("payment_proof", raw_case_id)
        except Exception:
            pass
    if _proof_already_done:
        msg = f"ℹ️ {raw_case_id} 繳費憑證已上傳過 ({proof_registry.get(raw_case_id, {}).get('uploaded_at', '?')})，跳過"
        logger.info(msg)
        _notify(msg, notify)
        return {"success": True, "result": "Skipped", "message": msg}

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Logging into SSO for payment proof upload...")
            if not mgr.login():
                msg = "❌ 閱卷登入失敗"
                _notify(msg, notify)
                return {"success": False, "error": "sso_login_failed"}

            mgr.navigate_to_file_review()

            case_info = {
                "court_code": court_code,
                "year": str(year),
                "case_type": case_type,
                "case_number": str(case_number),
                "client_name": client_name,
            }
            # 複製並改名為含「繳費憑證」的檔名讓 OLA 自動辨識
            import shutil as _shutil
            import tempfile as _tempfile
            renamed = os.path.join(
                _tempfile.gettempdir(),
                f"繳費憑證_{year}{case_type}{case_num_padded}.png",
            )
            _shutil.copy2(file_path, renamed)
            logger.info("Uploading payment proof to: %s (as %s)", case_info, os.path.basename(renamed))
            result = mgr.upload_payment_proof(case_info, renamed)

            label = f"{court_code} {year}年{case_type}字第{case_number}號"
            if result == "Uploaded":
                msg = f"✅ 繳費憑證已上傳 — {label}"
                # 記錄到 registry
                from datetime import datetime as _dt
                proof_registry[raw_case_id] = {
                    "uploaded_at": _dt.now().isoformat(),
                    "court_code": court_code,
                    "file": os.path.basename(file_path),
                }
                try:
                    os.makedirs(os.path.dirname(registry_path), exist_ok=True)
                    with open(registry_path, "w", encoding="utf-8") as _wf:
                        json.dump(proof_registry, _wf, ensure_ascii=False, indent=2)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1073, exc_info=True)
                # DB dedup sync
                try:
                    from skills.ops.dedup_db import mark_done as _dd_mark
                    _dd_mark("payment_proof", raw_case_id, metadata={
                        "court_code": court_code, "file": os.path.basename(file_path),
                        "source": "cmd_upload_payment_proof",
                    })
                except Exception:
                    pass
            elif result == "NotFound":
                msg = f"⚠️ 找不到案件 — {label}"
            else:
                msg = f"❌ 繳費憑證上傳失敗 — {label} (結果: {result})"

            _notify(msg, notify)
            return {"success": result == "Uploaded", "result": result,
                    "case": label, "message": msg}

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Upload payment proof failed: %s", error_msg)
        _notify("❌ 繳費憑證上傳失敗: " + error_msg, notify)
        return {"success": False, "error": error_msg,
                "traceback": traceback.format_exc()[-500:]}


def cmd_upload_payment_proofs_batch(screenshot_dir: str = "",
                                    notify: bool = True) -> dict:
    """
    批次掃描目錄中的繳費截圖，自動判讀案號並逐一上傳繳費憑證。

    流程:
    1. 掃描 screenshot_dir（預設桌面）中今天的「截圖」PNG 檔
    2. 用 vision 解析每張截圖取得案號和法院
    3. 登入 OLA 一次，逐一上傳
    """
    if not screenshot_dir:
        screenshot_dir = os.path.expanduser("~/Desktop")

    # 找到今天的截圖檔案
    import glob as _glob
    from datetime import date as _date

    today_str = _date.today().strftime("%Y-%m-%d")
    # macOS 截圖格式: "截圖 2026-03-10 清晨5.18.23.png"
    candidates = sorted(_glob.glob(os.path.join(screenshot_dir, f"截圖 {today_str}*.png")))
    if not candidates:
        # 嘗試更寬鬆的匹配
        candidates = sorted(_glob.glob(os.path.join(screenshot_dir, "截圖*.png")))
        # 只取今天修改的
        candidates = [
            f for f in candidates
            if _date.fromtimestamp(os.path.getmtime(f)) == _date.today()
        ]

    if not candidates:
        msg = "⚠️ 桌面上找不到今天的繳費截圖"
        _notify(msg, notify)
        return {"success": False, "error": "no screenshots found", "message": msg}

    logger.info("Found %d screenshot candidates: %s",
                len(candidates), [os.path.basename(f) for f in candidates])

    # 解析每張截圖
    try:
        mod = _ensure_imports()
    except Exception as e:
        return {"success": False, "error": f"import failed: {e}"}

    parsed_list = []
    for img_path in candidates:
        logger.info("Parsing screenshot: %s", os.path.basename(img_path))
        info = mod.FileReviewManager.parse_payment_screenshot(img_path)
        if info and info.get("court_code") and info.get("year"):
            info["file_path"] = img_path
            parsed_list.append(info)
            logger.info("  → %s (%s)", info.get("raw_case_id"), info.get("court_name"))
        else:
            logger.warning("  → 無法解析: %s (result=%s)", os.path.basename(img_path), info)

    if not parsed_list:
        msg = f"⚠️ 掃到 {len(candidates)} 張截圖但都無法解析出案號"
        _notify(msg, notify)
        return {"success": False, "error": "no parseable screenshots",
                "candidates": len(candidates), "message": msg}

    # ── 去重: 載入已上傳記錄 ──
    cfg = _load_config()
    creds = _get_credentials(cfg)
    registry_path = os.path.join(creds.get("download_folder", "./閱卷下載"), "payment_proof_registry.json")
    proof_registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as _rf:
                proof_registry = json.load(_rf)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1164, exc_info=True)

    # 過濾已上傳的案件 (DB primary, JSON fallback)
    new_list = []
    for p in parsed_list:
        key = p.get("raw_case_id", "")
        _already = key in proof_registry
        if not _already and key:
            try:
                from skills.ops.dedup_db import is_done as _dd_is_done
                _already = _dd_is_done("payment_proof", key)
            except Exception:
                pass
        if _already:
            logger.info("  ⏭ 跳過已上傳: %s (上傳於 %s)", key, proof_registry.get(key, {}).get("uploaded_at", "?"))
        else:
            new_list.append(p)

    if not new_list and parsed_list:
        msg = f"ℹ️ {len(parsed_list)} 筆繳費憑證皆已上傳過，無需重複操作"
        _notify(msg, notify)
        return {"success": True, "skipped": len(parsed_list), "message": msg}

    parsed_list = new_list

    # 通知解析結果
    summary_lines = [f"📋 解析到 {len(parsed_list)} 筆繳費憑證:"]
    for p in parsed_list:
        summary_lines.append(
            f"  • {p['raw_case_id']} ({p['court_name']}) ${p.get('amount', '?')}"
        )
    _notify("\n".join(summary_lines), notify)

    # 登入 OLA 並逐一上傳 (cfg/creds 已在去重段載入)
    if not creds["username"] or not creds["password"]:
        return {"success": False, "error": "missing credentials"}

    db = _get_db_manager(cfg)
    mgr = mod.FileReviewManager(
        username=creds["username"],
        password=creds["password"],
        download_folder=creds["download_folder"],
        db_manager=db,
        headless=True,
        log_callback=lambda msg: logger.info(msg),
    )

    results = []
    try:
        logger.info("Logging into SSO for batch payment proof upload...")
        if not mgr.login():
            msg = "❌ 閱卷登入失敗"
            _notify(msg, notify)
            return {"success": False, "error": "sso_login_failed"}

        mgr.navigate_to_file_review()

        for p in parsed_list:
            case_info = {
                "court_code": p["court_code"],
                "year": p["year"],
                "case_type": p["case_type"],
                "case_number": p["case_number"],
            }
            label = f"{p['court_code']} {p['year']}年{p['case_type']}字第{p['case_number']}號"
            # 複製並改名為「繳費憑證_案號.png」讓 OLA 自動辨識
            import shutil as _shutil
            import tempfile as _tempfile
            renamed = os.path.join(
                _tempfile.gettempdir(),
                f"繳費憑證_{p['raw_case_id'].replace('.', '')}.png",
            )
            _shutil.copy2(p["file_path"], renamed)
            logger.info("Uploading payment proof: %s → %s", label, os.path.basename(renamed))

            try:
                result = mgr.upload_payment_proof(case_info, renamed)
            except Exception as ex:
                logger.error("Upload error for %s: %s", label, ex)
                result = "Error"

            results.append({
                "case": label,
                "raw_case_id": p["raw_case_id"],
                "court_name": p["court_name"],
                "result": result,
                "file": os.path.basename(p["file_path"]),
            })

            if result == "Uploaded":
                _notify(f"✅ 繳費憑證已上傳 — {label}", notify)
                # 記錄到 registry 避免重複上傳
                from datetime import datetime as _dt
                proof_registry[p["raw_case_id"]] = {
                    "uploaded_at": _dt.now().isoformat(),
                    "court_code": p["court_code"],
                    "court_name": p["court_name"],
                    "file": os.path.basename(p["file_path"]),
                    "amount": p.get("amount", ""),
                }
                try:
                    os.makedirs(os.path.dirname(registry_path), exist_ok=True)
                    with open(registry_path, "w", encoding="utf-8") as _wf:
                        json.dump(proof_registry, _wf, ensure_ascii=False, indent=2)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1262, exc_info=True)
                # DB dedup sync
                try:
                    from skills.ops.dedup_db import mark_done as _dd_mark
                    _dd_mark("payment_proof", p["raw_case_id"], metadata={
                        "court_code": p["court_code"], "court_name": p["court_name"],
                        "file": os.path.basename(p["file_path"]),
                        "source": "cmd_upload_payment_proofs_batch",
                    })
                except Exception:
                    pass
            elif result == "NotFound":
                _notify(f"⚠️ 找不到案件 — {label}", notify)
            else:
                _notify(f"❌ 繳費憑證上傳失敗 — {label}", notify)

            import time as _time
            _time.sleep(2)  # 上傳間隔

    finally:
        mgr.close()

    uploaded = sum(1 for r in results if r["result"] == "Uploaded")
    total = len(results)
    final_msg = f"📊 繳費憑證批次上傳完成: {uploaded}/{total} 成功"
    _notify(final_msg, notify)

    return {
        "success": uploaded > 0,
        "uploaded": uploaded,
        "total": total,
        "results": results,
        "message": final_msg,
    }


def cmd_upload_payment_proof_from_image(image_path: str, notify: bool = True) -> dict:
    """
    從通道（LINE/DC/TG）傳來的繳費截圖，自動解析案號並上傳至 OLA。

    流程:
    1. parse_payment_screenshot 解析截圖
    2. 去重檢查 (registry)
    3. cmd_upload_payment_proof 上傳
    """
    if not image_path or not os.path.exists(image_path):
        msg = "⚠️ 找不到繳費截圖檔案"
        _notify(msg, notify)
        return {"success": False, "error": "file not found", "message": msg}

    try:
        mod = _ensure_imports()
    except Exception as e:
        msg = f"❌ 載入閱卷模組失敗：{e}"
        _notify(msg, notify)
        return {"success": False, "error": str(e), "message": msg}

    # Step 1: 解析截圖
    logger.info("💰 Parsing payment screenshot from channel: %s", image_path)
    info = mod.FileReviewManager.parse_payment_screenshot(image_path)
    if not info or not info.get("court_code") or not info.get("year"):
        msg = (
            "⚠️ 無法從這張截圖解析出繳費案號資訊。\n"
            "請確認截圖包含案件繳費狀況查詢清單（含案號、法院、金額等欄位）。"
        )
        _notify(msg, notify)
        return {"success": False, "error": "parse_failed", "message": msg}

    court_code = info["court_code"]
    year = info["year"]
    case_type = info["case_type"]
    case_number = info["case_number"]
    raw_case_id = info.get("raw_case_id", f"{year}.{case_type}.{str(case_number).zfill(6)}")
    court_name = info.get("court_name", court_code)
    amount = info.get("amount", "?")

    logger.info("💰 Parsed: %s (%s) $%s", raw_case_id, court_name, amount)
    _notify(f"💰 解析繳費截圖: {raw_case_id} ({court_name}) ${amount}，開始上傳⋯", notify)

    # Step 2: 呼叫現有的單件上傳 (含去重 + OLA 登入 + 上傳)
    return cmd_upload_payment_proof(
        court_code=court_code,
        year=year,
        case_type=case_type,
        case_number=case_number,
        file_path=image_path,
        notify=notify,
    )


def cmd_download_payment_slips(max_days: int = 14, notify: bool = True) -> dict:
    """Download all pending payment slip PDFs and send via TG."""
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        return {"success": False, "error": "missing credentials"}

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Logging into SSO for payment slip download...")
            if not mgr.login():
                msg = "❌ 閱卷登入失敗"
                _notify(msg, notify)
                return {"success": False, "error": "sso_login_failed"}

            mgr.navigate_to_file_review()

            results = mgr.download_all_payment_slips(max_days=max_days)

            # Collect PDF paths — only newly downloaded (skip already_existed)
            pdf_paths = []
            case_labels = []
            for r in results:
                if r.get("already_existed"):
                    continue
                # 使用 all_paths 取得全部檔案，fallback 到 pdf_path
                paths = r.get("all_paths") or []
                if not paths:
                    p = r.get("pdf_path", "")
                    if p:
                        paths = [p]
                for path in paths:
                    if path and os.path.exists(path):
                        pdf_paths.append(path)
                party = r.get("party") or ""
                case_no = r.get("case_number") or ""
                if paths:
                    case_labels.append(f"{party}｜{case_no}")

            if pdf_paths:
                # Send via TG
                summary_lines = [f"💰 繳費單 PDF 下載完成（{len(pdf_paths)} 件）："]
                for i, label in enumerate(case_labels, 1):
                    summary_lines.append(f"  {i}. {label}")

                msg = "\n".join(summary_lines)

                # Send notification text first
                _notify(msg, notify)

                # Send PDFs via TG
                for i, pdf_path in enumerate(pdf_paths):
                    label = case_labels[i] if i < len(case_labels) else os.path.basename(pdf_path)
                    _notify_file(pdf_path, caption=f"📄 繳費單 ({i+1}/{len(pdf_paths)}): {label}", flag=notify)

                return {
                    "success": True,
                    "count": len(pdf_paths),
                    "pdf_paths": pdf_paths,
                    "cases": case_labels,
                    "message": msg,
                }
            else:
                msg = "ℹ️ 無待下載繳費單（可能全部已處理或無待繳費案件）"
                _notify(msg, notify)
                return {
                    "success": True,
                    "count": 0,
                    "pdf_paths": [],
                    "message": msg,
                }

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Download payment slips failed: %s", error_msg)
        _notify("❌ 繳費單下載失敗: " + error_msg, notify)
        return {"success": False, "error": error_msg, "traceback": traceback.format_exc()[-500:]}


def cmd_probe(court_code: str, year: str, case_type: str,
              case_number: str, client_name: str = "",
              notify: bool = True) -> dict:
    """Probe file-review status without submitting any report."""
    return cmd_apply(
        court_code=court_code,
        year=year,
        case_type=case_type,
        case_number=case_number,
        client_name=client_name,
        auto_submit=False,
        notify=notify,
    )


def cmd_download(case_number: str = "", notify: bool = True) -> dict:
    """Download approved file review materials."""
    case_number = str(case_number or "").strip()
    # 防呆：避免把「姓名/描述詞」誤當案號，造成只鎖單案下載。
    if case_number and not re.search(r"\d", case_number):
        logger.warning("download case_number looks non-numeric, fallback to all: %s", case_number)
        case_number = ""
    elif case_number and not (
        re.search(r"\d{2,4}\s*(?:年度)?\s*[^\d\s]{1,12}\s*(?:字)?\s*(?:第)?\s*\d+\s*(?:號)?", case_number)
        or re.search(r"\d{2,4}\.[^.\s]{1,12}\.\d+", case_number)
        or re.search(r"\d{6,8}-[A-Za-z]-\d{3,4}", case_number)
    ):
        logger.warning("download case_number format not recognized, fallback to all: %s", case_number)
        case_number = ""

    _eventlog("filereview:download:start", payload={"case_number": case_number, "notify": bool(notify)}, tags={"case_number": case_number} if case_number else {})
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        out = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_EEFILE_USERNAME/PASSWORD in .env"}
        _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
        return out

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Logging into SSO for download...")
            if not mgr.login():
                msg = "❌ 閱卷登入失敗，可能驗證碼連錯或系統維護，已中斷自動下載。"
                logger.error(msg)
                _notify(msg, notify)
                out = {"success": False, "error": "sso_login_failed"}
                _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
                return out

            mgr.navigate_to_file_review()

            logger.info("Checking and downloading available files...")
            downloaded = mgr.check_and_download_available(
                target_case_number=case_number if case_number else None
            )

            count = len(downloaded) if downloaded else 0
            # Build a readable summary (who/which case), fallback to filenames if no meta.
            archive = getattr(mgr, "_last_archive_report", {}) or {}
            items = archive.get("items") if isinstance(archive, dict) else None
            if not isinstance(items, list):
                items = []
            staged = archive.get("staged") if isinstance(archive, dict) else None
            if not isinstance(staged, list):
                staged = []
            unresolved_items = [it for it in items if isinstance(it, dict) and not (it.get("folder") or "").strip()]
            resolved_items = [it for it in items if isinstance(it, dict) and (it.get("folder") or "").strip()]
            smart_skipped = getattr(mgr, "_last_smart_skipped_files", []) or []
            review_items = [it for it in items if isinstance(it, dict) and _activity_artifact_kind(it) != "payment_slip"]
            payment_downloaded = [fp for fp in (downloaded or []) if os.path.basename(str(fp)).startswith("繳費單_")]
            review_downloaded = [fp for fp in (downloaded or []) if fp not in payment_downloaded]

            # ── Post-download: auto-bookmark downloaded PDFs ──
            if review_downloaded:
                _auto_bookmark_pdfs(review_downloaded)

            payment_count = len(payment_downloaded)
            review_count = len(review_downloaded)
            unresolved_review_items = [it for it in unresolved_items if _activity_artifact_kind(it) != "payment_slip"]
            resolved_review_items = [it for it in resolved_items if _activity_artifact_kind(it) != "payment_slip"]

            def _norm(s: str) -> str:
                return (s or "").strip()

            def _format_download_message() -> tuple[str, dict]:
                """
                Returns (message, exported) where exported is export_txt() result or {}.
                """
                header = f"📥 卷宗下載完成（{review_count} 個檔案）"
                if case_number:
                    header = f"📥 卷宗下載完成 — {case_number}（{review_count} 個檔案）"

                if review_count <= 0:
                    if smart_skipped:
                        lines = [header, f"已存在跳過 {len(smart_skipped)} 份："]
                        for it in smart_skipped[:10]:
                            fn = (it.get("file") or "").strip()
                            ep = (it.get("existing_path") or "").strip()
                            if fn and ep:
                                lines.append(f"- {fn} -> {ep}")
                            elif fn:
                                lines.append(f"- {fn}")
                        if len(smart_skipped) > 10:
                            lines.append(f"...（其餘 {len(smart_skipped) - 10} 份略）")
                        return "\n".join(lines).strip(), {}
                    return "", {}

                # Group by (party, court_case_no, folder)
                groups = {}
                for it in review_items:
                    if not isinstance(it, dict):
                        continue
                    party = _norm(it.get("party") or "")
                    court_case_no = _norm(it.get("court_case_no") or "")
                    folder = _norm(it.get("folder") or "")
                    key = (party, court_case_no, folder)
                    groups.setdefault(key, []).append(it)

                lines = [header]

                if groups:
                    # Prefer showing court_case_no (使用者要求閱卷通知以法院案號為主)
                    idx = 0
                    for (party, court_case_no, folder), its in groups.items():
                        idx += 1
                        label_parts = []
                        if party:
                            label_parts.append(party)
                        if court_case_no:
                            label_parts.append(court_case_no)
                        if not label_parts and folder:
                            label_parts.append(os.path.basename(folder))
                        label = "｜".join(label_parts) if label_parts else "（未能判斷案件）"
                        lines.append(f"{idx}. {label}")
                        for it in its:
                            fn = _norm(it.get("file") or "")
                            dst = _norm(it.get("dst") or "")
                            if fn and dst:
                                lines.append(f"- {fn} -> {dst}")
                            elif fn:
                                lines.append(f"- {fn}")
                        if folder:
                            lines.append(f"資料夾：{folder}")
                        lines.append("")
                else:
                    # Fallback: list filenames only
                    for fp in review_downloaded[:20]:
                        lines.append(f"- {os.path.basename(str(fp))}")
                    if len(review_downloaded) > 20:
                        lines.append(f"...（其餘 {len(review_downloaded) - 20} 份略）")

                detail = "\n".join([x for x in lines]).strip()
                if unresolved_review_items:
                    detail += f"\n\n⚠️ 待歸檔 {len(unresolved_review_items)} 份（案號歧義或資訊不足）"

                if smart_skipped:
                    detail += f"\n\n已存在跳過 {len(smart_skipped)} 份："
                    for it in smart_skipped[:10]:
                        fn = (it.get("file") or "").strip()
                        ep = (it.get("existing_path") or "").strip()
                        if fn and ep:
                            detail += f"\n- {fn} -> {ep}"
                        elif fn:
                            detail += f"\n- {fn}"
                    if len(smart_skipped) > 10:
                        detail += f"\n...（其餘 {len(smart_skipped) - 10} 份略）"

                # LINE 長度保護：過長就輸出 TXT，訊息只放摘要 + 下載連結/路徑
                if len(detail) <= 900:
                    return detail, {}

                short_lines = [header]
                shown = 0
                for line in detail.splitlines()[1:]:
                    if not line.strip():
                        continue
                    short_lines.append(line)
                    shown += 1
                    if shown >= 8:
                        break
                exported = export_txt(detail, prefix="magi_filereview") if export_txt else {}
                if exported and exported.get("success") and (exported.get("url") or exported.get("path")):
                    link = exported.get("url") or exported.get("path")
                    short_lines.append(f"明細：{link}")
                else:
                    short_lines.append("（明細過長，已省略）")
                return "\n".join(short_lines).strip(), (exported or {})

            msg, exported = _format_download_message()
            # 繳費單已改走獨立繳費通知，這裡不再發通知

            # Avoid noisy periodic pushes when auto worker finds nothing new.
            # Manual trigger can still force this by setting:
            #   MAGI_FILE_REVIEW_NOTIFY_EMPTY_DOWNLOAD=1
            notify_empty_download = _truthy(os.environ.get("MAGI_FILE_REVIEW_NOTIFY_EMPTY_DOWNLOAD", "0"))
            notify_smart_skips = _truthy(os.environ.get("MAGI_FILE_REVIEW_NOTIFY_SMART_SKIPS", "0"))
            should_notify = bool(notify) and bool(msg) and (
                review_count > 0
                or (bool(smart_skipped) and notify_smart_skips)
                or notify_empty_download
            )
            if should_notify:
                _notify(msg, True)
                # If long detail was exported to TXT, also send the file
                txt_path = exported.get("path", "") if exported else ""
                if txt_path and os.path.isfile(txt_path):
                    _notify_file(txt_path, caption="卷宗下載明細", flag=True)
            archive_summary = {
                "resolved_count": len(resolved_review_items),
                "unresolved_count": len(unresolved_review_items),
                "staged_count": len(staged),
                "case_candidates": len(archive.get("cases") or []) if isinstance(archive, dict) else 0,
                "review_download_count": review_count,
                "payment_download_count": payment_count,
            }

            _dl_base = os.path.dirname(creds.get("download_folder", DEFAULT_DOWNLOAD_FOLDER))
            if _dl_base:
                _cleanup_all_download_folders(_dl_base, max_days=15)

            out = {"success": True, "downloaded_count": count,
                   "files": [str(f) for f in (downloaded or [])[:10]],
                   "items": items[:50] if items else [],
                   "archive_summary": archive_summary,
                   "exported": exported if exported else None,
                   "review_download_count": review_count,
                   "payment_download_count": payment_count,
                   "message": msg}
            _eventlog("filereview:download:done", ok=True, payload={"case_number": case_number, "downloaded_count": count, "files": out.get("files", [])[:3]}, tags={"case_number": case_number} if case_number else {})
            return out

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Download failed: %s", error_msg)
        _notify("❌ 閱卷下載失敗: " + error_msg, notify)
        out = {"success": False, "error": error_msg}
        _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
        return out


def cmd_download_background(case_number: str = "", notify: bool = True) -> dict:
    """
    Queue download job in background and return immediately.
    """
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        return {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_EEFILE_USERNAME/PASSWORD in .env"}

    queue_notify = _truthy(os.environ.get("MAGI_FILE_REVIEW_DOWNLOAD_QUEUE_NOTIFY", "0"))
    singleton = _truthy(os.environ.get("MAGI_FILE_REVIEW_DOWNLOAD_BG_SINGLETON", "1"))
    if singleton:
        latest = _latest_download_job_id()
        if latest:
            st = _read_download_job(latest)
            pid = int(st.get("pid") or 0)
            if st.get("running") and pid > 1 and _pid_alive(pid):
                msg = f"📥 閱卷下載背景任務已執行中（job_id={latest}）"
                _notify(msg, notify and queue_notify)
                return {
                    "success": True,
                    "queued": True,
                    "deduped": True,
                    "job_id": latest,
                    "pid": pid,
                    "status": "already_running",
                    "message": msg,
                }

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    status_path, log_path = _download_job_paths(job_id)
    payload = {
        "job_id": job_id,
        "case_number": str(case_number or "").strip(),
        "notify": bool(notify),
    }
    _write_download_job(
        job_id,
        {
            "status": "queued",
            "running": False,
            "queued_at": datetime.now().isoformat(),
            "case_number": payload["case_number"],
            "notify": bool(notify),
            "status_path": status_path,
            "log_path": log_path,
        },
    )
    cmd = [
        sys.executable,
        os.path.abspath(__file__),
        "--task",
        "download_worker " + json.dumps(payload, ensure_ascii=False),
    ]
    env = os.environ.copy()
    env["MAGI_FILE_REVIEW_DOWNLOAD_BACKGROUND"] = "0"
    try:
        os.makedirs(BG_JOB_DIR, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        threading.Thread(target=proc.wait, daemon=True).start()
        _write_download_job(
            job_id,
            {
                "status": "running",
                "running": True,
                "pid": int(proc.pid),
                "started_at": datetime.now().isoformat(),
            },
        )
        msg = f"📥 閱卷下載已於背景啟動（job_id={job_id}）"
        _notify(msg, notify and queue_notify)
        _eventlog(
            "filereview:download:queued",
            ok=True,
            payload={"job_id": job_id, "case_number": payload["case_number"]},
            tags={"case_number": payload["case_number"]} if payload["case_number"] else {},
        )
        return {
            "success": True,
            "queued": True,
            "job_id": job_id,
            "pid": int(proc.pid),
            "status_path": status_path,
            "log_path": log_path,
            "message": msg,
        }
    except Exception as e:
        err = f"spawn_failed: {e}"
        _write_download_job(
            job_id,
            {
                "status": "failed",
                "running": False,
                "success": False,
                "error": err,
                "finished_at": datetime.now().isoformat(),
            },
        )
        _eventlog(
            "filereview:download:queued",
            ok=False,
            payload={"job_id": job_id, "error": err},
            tags={"case_number": payload["case_number"]} if payload["case_number"] else {},
        )
        return {"success": False, "error": err, "job_id": job_id}


def cmd_download_worker(payload: dict) -> dict:
    job_id = str((payload or {}).get("job_id") or "").strip()
    case_number = str((payload or {}).get("case_number") or "").strip()
    notify = bool((payload or {}).get("notify", True))

    if not job_id:
        return {"success": False, "error": "missing_job_id"}

    _write_download_job(
        job_id,
        {
            "status": "running",
            "running": True,
            "started_at": datetime.now().isoformat(),
            "case_number": case_number,
        },
    )
    out = cmd_download(case_number=case_number, notify=notify)
    _write_download_job(
        job_id,
        {
            "status": "done" if bool(out.get("success")) else "failed",
            "running": False,
            "success": bool(out.get("success")),
            "finished_at": datetime.now().isoformat(),
            "result": out,
        },
    )
    return {"success": bool(out.get("success")), "job_id": job_id}


def cmd_download_status(job_id: str = "") -> dict:
    jid = (job_id or "").strip()
    if not jid or jid == "latest":
        jid = _latest_download_job_id()
    if not jid:
        return {"success": False, "error": "no_background_job"}

    st = _read_download_job(jid)
    if not st:
        return {"success": False, "error": "job_not_found", "job_id": jid}

    pid = int(st.get("pid") or 0)
    if st.get("running") and pid > 1 and (not _pid_alive(pid)):
        status_name = str(st.get("status") or "")
        if status_name not in {"done", "failed"}:
            st = _write_download_job(jid, {"running": False, "status": "stopped", "finished_at": datetime.now().isoformat()})
        else:
            st = _write_download_job(jid, {"running": False})
    st["success"] = True
    return st


def _roc_to_iso(val: str) -> str:
    """民國緊湊日期（如 1150312）轉 YYYY-MM-DD。"""
    import re as _re
    s = _re.sub(r"\D", "", str(val or ""))
    if len(s) != 7:
        return str(val or "")
    try:
        y = int(s[:3]) + 1911
        m = int(s[3:5])
        d = int(s[5:7])
        return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:
        return str(val or "")


def _format_roc_deadline(val: str) -> str:
    """將民國緊湊日期轉為人可讀格式（如 115/03/12）。"""
    import re as _re
    s = _re.sub(r"\D", "", str(val or ""))
    if len(s) == 7:
        return f"{s[:3]}/{s[3:5]}/{s[5:7]}"
    return str(val or "") or "未知"


def _normalize_case_token(val: str) -> str:
    s = str(val or "").strip()
    if not s:
        return ""
    # Strip structural filler in Taiwan case numbers so that
    # "114年度原訴字第000084號" and "114.原訴.000084" normalise identically.
    s = re.sub(r"[年度字第號]", "", s)
    parts = re.findall(r"\d+|[^\d]+", s)
    out = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            try:
                out.append(str(int(part)))
            except Exception:
                out.append(part.lstrip("0") or "0")
            continue
        cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", part)
        if cleaned:
            out.append(cleaned.lower())
    return "".join(out)


def _portal_item_case_key(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    for field in ("court_case_no", "case_number"):
        norm = _normalize_case_token(item.get(field) or "")
        if norm:
            return f"case:{norm}"
    payid = str(item.get("payid") or "").strip()
    if payid:
        return f"payid:{payid}"
    rowid = str(item.get("rowid") or "").strip()
    if rowid:
        return f"rowid:{rowid}"
    party = _normalize_case_token(item.get("party") or "")
    if party:
        return f"party:{party}"
    return ""


def _portal_item_is_paid(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    return str(item.get("paystatus") or "").strip() == "1" or str(item.get("p_status") or "").strip().upper() == "Y"


def _portal_item_is_actionable_pending(item: dict) -> bool:
    if not isinstance(item, dict) or item.get("status") != "pending_payment":
        return False
    if _portal_item_is_paid(item):
        return False

    status_name = str(item.get("status_name") or "").strip()
    status_code = str(item.get("status_code") or "").strip()
    result_text = str(item.get("result_text") or "").strip()
    paystatus = str(item.get("paystatus") or "").strip()

    has_pending_signal = ("待繳費" in result_text) or paystatus == "2"
    has_approved_signal = ("同意" in status_name) or (not status_name and status_code in {"3", "6", ""})
    return has_pending_signal and has_approved_signal


def _portal_item_priority(item: dict) -> tuple:
    if not isinstance(item, dict):
        return (-1, "", "", "")
    status = str(item.get("status") or "").strip()
    base = 0
    if status == "downloadable":
        base = 30
    elif _portal_item_is_actionable_pending(item):
        base = 20
    elif status == "pending_payment":
        base = 10
    applydt = re.sub(r"\D", "", str(item.get("applydt") or ""))
    rowid = re.sub(r"\D", "", str(item.get("rowid") or ""))
    payid = re.sub(r"\D", "", str(item.get("payid") or ""))
    return (base, applydt, rowid, payid)


def _filter_not_yet_downloaded(dl_items: list, download_folder: str) -> list:
    """Filter out portal items whose case_number already exists in downloaded_registry.json or dedup DB."""
    if not dl_items:
        return []

    # ── DB-backed dedup (primary) ──
    db_downloaded: set[str] = set()
    try:
        from skills.ops.dedup_db import is_done as _dd_is_done
        _db_available = True
    except Exception:
        _db_available = False

    # ── JSON fallback ──
    registry_path = os.path.join(download_folder, "downloaded_registry.json") if download_folder else ""
    json_downloaded: set[str] = set()
    if registry_path and os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f) or {}
            for v in registry.values():
                y = (v.get("yyidno") or "").strip()
                if y:
                    json_downloaded.add(y)
        except Exception:
            pass

    result = []
    for it in dl_items:
        case_num = (it.get("case_number") or "").strip()
        if not case_num:
            result.append(it)
            continue
        # Check DB first, then JSON
        if _db_available:
            try:
                if _dd_is_done("download", case_num):
                    continue
            except Exception:
                pass
        if case_num in json_downloaded:
            continue
        result.append(it)
    return result


def _collapse_portal_items(items: list) -> dict:
    chosen = {}
    raw_items = [it for it in (items or []) if isinstance(it, dict)]
    for item in raw_items:
        key = _portal_item_case_key(item) or f"row:{len(chosen)}:{id(item)}"
        prev = chosen.get(key)
        if prev is None or _portal_item_priority(item) > _portal_item_priority(prev):
            chosen[key] = item

    merged = list(chosen.values())
    actionable = [
        it for it in merged
        if str(it.get("status") or "").strip() == "downloadable" or _portal_item_is_actionable_pending(it)
    ]
    downloadable = [it for it in actionable if str(it.get("status") or "").strip() == "downloadable"]
    pending = [it for it in actionable if _portal_item_is_actionable_pending(it)]
    merged.sort(key=lambda it: (
        0 if str(it.get("status") or "").strip() == "downloadable" else 1,
        _normalize_case_token(it.get("court_case_no") or it.get("case_number") or ""),
        _normalize_case_token(it.get("party") or ""),
    ))
    actionable.sort(key=lambda it: (
        0 if str(it.get("status") or "").strip() == "downloadable" else 1,
        _normalize_case_token(it.get("court_case_no") or it.get("case_number") or ""),
        _normalize_case_token(it.get("party") or ""),
    ))
    return {
        "raw_count": len(raw_items),
        "case_count": len(merged),
        "count": len(actionable),
        "downloadable_count": len(downloadable),
        "pending_payment_count": len(pending),
        "items": actionable,
        "all_items": merged,
    }


def _parse_iso_datetime(val: str) -> Optional[datetime]:
    s = str(val or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2027, exc_info=True)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _recent_activity_state_path(download_folder: str) -> str:
    base = str(download_folder or DEFAULT_DOWNLOAD_FOLDER).strip() or DEFAULT_DOWNLOAD_FOLDER
    return os.path.join(base, RECENT_ACTIVITY_STATE_FILE)


def _load_recent_activity_state(download_folder: str) -> tuple[dict, bool]:
    path = _recent_activity_state_path(download_folder)
    if not os.path.exists(path):
        return {
            "version": 1,
            "recent_payment_activity": {},
            "recent_review_download_activity": {},
        }, True
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("state_not_dict")
    except Exception:
        return {
            "version": 1,
            "recent_payment_activity": {},
            "recent_review_download_activity": {},
        }, True
    data.setdefault("version", 1)
    data.setdefault("recent_payment_activity", {})
    data.setdefault("recent_review_download_activity", {})
    if not isinstance(data.get("recent_payment_activity"), dict):
        data["recent_payment_activity"] = {}
    if not isinstance(data.get("recent_review_download_activity"), dict):
        data["recent_review_download_activity"] = {}
    return data, False


def _save_recent_activity_state(download_folder: str, state: dict) -> None:
    path = _recent_activity_state_path(download_folder)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state or {}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to save recent activity state %s: %s", path, e)


def _recent_activity_fingerprint(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    processed_at = item.get("processed_at")
    processed_at_text = processed_at.isoformat() if isinstance(processed_at, datetime) else str(processed_at or "").strip()
    case_key = _portal_item_case_key(
        {
            "case_number": item.get("case_number"),
            "court_case_no": item.get("court_case_no"),
            "party": item.get("party"),
            "payid": item.get("payid"),
        }
    ) or str(item.get("key") or "").strip()
    parts = [
        str(item.get("source") or "").strip(),
        _activity_artifact_kind(item),
        case_key,
        str(item.get("detail") or "").strip(),
        str(item.get("count") or "").strip(),
        processed_at_text,
    ]
    return "|".join(parts)


def _prune_recent_activity_bucket(bucket: dict, keep_days: int = 30) -> dict:
    if not isinstance(bucket, dict):
        return {}
    cutoff = datetime.now().timestamp() - (max(1, int(keep_days or 30)) * 86400)
    cleaned = {}
    for key, seen_at in bucket.items():
        dt = _parse_iso_datetime(seen_at)
        if dt is None or dt.timestamp() >= cutoff:
            cleaned[str(key)] = str(seen_at or "")
    return cleaned


def _filter_unnotified_recent_activity(records: list[dict], download_folder: str, bucket_name: str) -> list[dict]:
    if not records:
        return []
    state, is_new_state = _load_recent_activity_state(download_folder)
    bucket = _prune_recent_activity_bucket(state.get(bucket_name) or {})
    state[bucket_name] = bucket
    now_iso = datetime.now().isoformat()

    # DB dedup helper
    try:
        from skills.ops.dedup_db import is_done as _dd_is_done
        _db_avail = True
    except Exception:
        _db_avail = False

    # First run after deployment: seed the current backlog to avoid replaying old activity.
    if is_new_state:
        for item in records:
            fp = _recent_activity_fingerprint(item)
            if fp:
                bucket[fp] = now_iso
                # Also seed DB
                if _db_avail:
                    try:
                        from skills.ops.dedup_db import mark_done as _dd_mark
                        _dd_mark("recent_activity", fp, metadata={"bucket": bucket_name, "seeded": True})
                    except Exception:
                        pass
        state["initialized_at"] = now_iso
        _save_recent_activity_state(download_folder, state)
        return []

    fresh = []
    for item in records:
        fp = _recent_activity_fingerprint(item)
        if not fp:
            continue
        # DB 優先
        _already = False
        if _db_avail:
            try:
                _already = _dd_is_done("recent_activity", fp)
            except Exception:
                pass
        # JSON fallback
        if not _already:
            _already = fp in bucket
        if _already:
            continue
        fresh.append(item)
    return fresh


def _mark_recent_activity_notified(records: list[dict], download_folder: str, bucket_name: str) -> None:
    if not records:
        return
    state, _ = _load_recent_activity_state(download_folder)
    bucket = _prune_recent_activity_bucket(state.get(bucket_name) or {})
    now_iso = datetime.now().isoformat()
    for item in records:
        fp = _recent_activity_fingerprint(item)
        if fp:
            bucket[fp] = now_iso
            # DB dedup sync
            try:
                from skills.ops.dedup_db import mark_done as _dd_mark
                _dd_mark("recent_activity", fp, metadata={
                    "bucket": bucket_name,
                    "source": item.get("source", ""),
                    "case_number": item.get("case_number", ""),
                })
            except Exception:
                pass
    state[bucket_name] = bucket
    state["updated_at"] = now_iso
    _save_recent_activity_state(download_folder, state)


def _load_recent_payment_activity(download_folder: str, days: int = 7) -> list[dict]:
    registry_path = os.path.join(download_folder or DEFAULT_DOWNLOAD_FOLDER, "payment_registry.json")
    if not os.path.exists(registry_path):
        return []
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return []

    cutoff = datetime.now().timestamp() - (max(1, int(days or 7)) * 86400)
    chosen = {}
    for key, entry in (data or {}).items():
        if not isinstance(entry, dict):
            continue
        dt = _parse_iso_datetime(entry.get("processed_at") or "")
        if dt is None or dt.timestamp() < cutoff:
            continue
        files = entry.get("file_paths") if isinstance(entry.get("file_paths"), list) else []
        if not files and isinstance(entry.get("files"), list):
            files = entry.get("files") or []
        file_count = len([fp for fp in files if str(fp or "").strip()])
        case_number = str(entry.get("case_number") or entry.get("yyidno") or "").strip()
        party = str(entry.get("party") or "").strip()
        # Fallback: 從檔名解析當事人姓名（繳費單_[當事人H]_115.原金訴.000044.pdf）
        if not party:
            for fn in (entry.get("files") or []):
                fn_str = str(fn or "").strip()
                if fn_str.startswith("繳費單_") and "_" in fn_str[4:]:
                    parts = fn_str.split("_", 2)
                    if len(parts) >= 2 and parts[1]:
                        party = parts[1]
                        break
        record = {
            "processed_at": dt,
            "party": party,
            "case_number": case_number,
            "detail": f"已下載繳費單（{file_count} 份）" if file_count > 0 else "已處理待繳費",
            "count": file_count,
            "source": "payment_registry",
            "key": str(key or ""),
        }
        rec_key = _portal_item_case_key({"case_number": case_number, "party": party, "payid": str(entry.get("p_payid") or "")}) or f"payment:{key}"
        prev = chosen.get(rec_key)
        if prev is None or dt > prev["processed_at"]:
            chosen[rec_key] = record
    return list(chosen.values())


def _auto_bookmark_pdfs(pdf_paths: list[str]) -> None:
    """Post-download hook: auto-add bookmarks to downloaded court PDFs."""
    try:
        import importlib.util
        bm_path = os.path.join(os.path.dirname(__file__), "..", "pdf-bookmarker", "action.py")
        bm_path = os.path.normpath(bm_path)
        if not os.path.exists(bm_path):
            logger.debug("pdf-bookmarker not found, skipping auto-bookmark")
            return
        spec = importlib.util.spec_from_file_location("pdf_bookmarker_action", bm_path)
        bm_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bm_mod)
        scan_fn = getattr(bm_mod, "scan_and_bookmark", None)
        if not scan_fn:
            return
    except Exception as e:
        logger.warning(f"⚠ pdf-bookmarker import failed: {e}")
        return

    bookmarked = 0
    for fp in pdf_paths:
        if not str(fp).lower().endswith(".pdf"):
            continue
        try:
            result = scan_fn(str(fp), output_path=None, dry_run=False)
            if result.get("success") and result.get("bookmarks", 0) > 0:
                bookmarked += 1
                logger.info(f"📑 Auto-bookmarked: {os.path.basename(fp)} ({result['bookmarks']} bookmarks)")
            else:
                logger.debug(f"Bookmark skipped {os.path.basename(fp)}: {result.get('message', '')}")
        except Exception as e:
            logger.warning(f"⚠ Bookmark error for {os.path.basename(fp)}: {e}")
    if bookmarked:
        logger.info(f"📑 Auto-bookmark complete: {bookmarked}/{len(pdf_paths)} files bookmarked")


def _activity_artifact_kind(item: dict) -> str:
    if not isinstance(item, dict):
        return "review_download"

    raw = str(item.get("artifact_type") or item.get("kind") or "").strip().lower()
    if raw in {"payment", "payment_slip", "payment-slip"}:
        return "payment_slip"

    detail = str(item.get("detail") or "").strip()
    file_name = os.path.basename(str(item.get("file") or item.get("dst") or item.get("path") or "")).strip()
    if file_name.startswith("繳費單_") or "繳費單" in detail or "待繳費" in detail:
        return "payment_slip"

    return "review_download"


def _format_recent_activity_block(title: str, records: list[dict], limit: int = 8) -> list[str]:
    if not records:
        return []
    lines = [f"{title}（{len(records)} 件）："]
    for idx, it in enumerate(records[: max(1, int(limit or 8))], 1):
        dt = it.get("processed_at")
        dt_text = dt.strftime("%m/%d %H:%M") if isinstance(dt, datetime) else "最近"
        caseno = str(it.get("case_number") or "-").strip() or "-"
        party = str(it.get("party") or "(未知)").strip() or "(未知)"
        detail = str(it.get("detail") or "已處理").strip()
        lines.append(f"  {idx}. {dt_text} {party}｜{caseno} {detail}")
    if len(records) > limit:
        lines.append(f"  ...（另有 {len(records) - limit} 件）")
    return lines


def _load_recent_download_activity(days: int = 7) -> list[dict]:
    if not os.path.isdir(BG_JOB_DIR):
        return []
    cutoff = datetime.now().timestamp() - (max(1, int(days or 7)) * 86400)
    files = [
        os.path.join(BG_JOB_DIR, name)
        for name in os.listdir(BG_JOB_DIR)
        if name.startswith("download_") and name.endswith(".json")
    ]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    chosen = {}
    for path in files[:80]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                job = json.load(f) or {}
        except Exception:
            continue
        dt = _parse_iso_datetime(job.get("finished_at") or job.get("updated_at") or job.get("started_at") or "")
        if dt is None or dt.timestamp() < cutoff:
            continue
        if not bool(job.get("success")):
            continue
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        items = result.get("items") if isinstance(result.get("items"), list) else []
        grouped = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").strip().lower()
            if action in {"exists_skip", "target_exists_keep_src", "target_exists_isolate_src"}:
                continue
            party = str(item.get("party") or "").strip()
            case_number = str(item.get("court_case_no") or item.get("case_number") or "").strip()
            artifact_type = _activity_artifact_kind(item)
            if action in {"copied", "moved"}:
                detail = "已下載繳費單" if artifact_type == "payment_slip" else "已下載卷宗"
            elif action.startswith("staged"):
                detail = "已下載繳費單待歸檔" if artifact_type == "payment_slip" else "已下載卷宗待歸檔"
            else:
                continue
            base_key = _portal_item_case_key({"case_number": case_number, "party": party}) or f"download:{path}:{len(grouped)}"
            rec_key = f"{artifact_type}:{action}:{base_key}"
            grouped.setdefault(
                rec_key,
                {
                    "party": party,
                    "case_number": case_number,
                    "count": 0,
                    "artifact_type": artifact_type,
                    "detail": detail,
                },
            )
            grouped[rec_key]["count"] += 1
        for rec_key, payload in grouped.items():
            artifact_type = str(payload.get("artifact_type") or "review_download").strip()
            record = {
                "processed_at": dt,
                "party": payload["party"],
                "case_number": payload["case_number"],
                "detail": f"{payload.get('detail') or ('已下載繳費單' if artifact_type == 'payment_slip' else '已下載卷宗')}（{payload['count']} 份）",
                "count": payload["count"],
                "artifact_type": artifact_type,
                "source": "download_job",
                "key": os.path.basename(path),
            }
            prev = chosen.get(rec_key)
            if prev is None or dt > prev["processed_at"]:
                chosen[rec_key] = record
    return list(chosen.values())


def _load_recent_processed_activity(download_folder: str, days: int = 7, limit: int = 8) -> list[dict]:
    merged = _load_recent_payment_activity(download_folder, days=days) + _load_recent_download_activity(days=days)
    merged.sort(key=lambda it: it.get("processed_at") or datetime.min, reverse=True)
    out = []
    seen = set()
    for item in merged:
        artifact_type = _activity_artifact_kind(item)
        key = f"{item.get('source')}:{artifact_type}:{_portal_item_case_key({'case_number': item.get('case_number'), 'party': item.get('party')}) or item.get('key')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max(1, int(limit or 8)):
            break
    return out


def _filter_urgent_pending_payments(items: list, days: int = 7) -> dict:
    """
    過濾未繳費案件，分為三組：
    - overdue: 已逾期（繳費期限在今天之前）
    - urgent: N 天內到期
    - unknown: 無期限資料
    回傳 dict: {"overdue": [...], "urgent": [...], "unknown": [...]}
    """
    from datetime import datetime as _dt, date as _date
    overdue, urgent, unknown = [], [], []
    today = _date.today()
    for it in (items or []):
        if not _portal_item_is_actionable_pending(it):
            continue
        raw = it.get("pay_deadline") or it.get("deadline") or ""
        iso = _roc_to_iso(raw) if raw else ""
        if iso and len(iso) == 10:
            try:
                dl = _dt.strptime(iso, "%Y-%m-%d").date()
                diff = (dl - today).days
                if diff < 0:
                    # 只列入 14 天內逾期的，太久以前的不通知
                    if diff >= -14:
                        overdue.append(it)
                    # else: 超過14天逾期，靜默跳過
                elif diff <= days:
                    urgent.append(it)
                else:
                    continue  # 超過 N 天，不列入
                continue
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2359, exc_info=True)
        unknown.append(it)

    def _sort_key(x):
        raw = x.get("pay_deadline") or x.get("deadline") or ""
        iso = _roc_to_iso(raw) if raw else ""
        return iso if iso else "9999-99-99"
    overdue.sort(key=_sort_key)
    urgent.sort(key=_sort_key)
    return {"overdue": overdue, "urgent": urgent, "unknown": unknown}


def cmd_check_emails(notify: bool = True, notify_empty: bool = True) -> dict:
    """Scan Gmail for payment notices and delivery notifications."""
    _eventlog("filereview:gmail_check:start")
    _ensure_runtime_deps()
    cfg = _load_config()
    creds = _get_credentials(cfg)

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            gmail_credentials_path=_json_path("credentials.json"),
            gmail_token_path=_json_path("filereview_token.pickle"),
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Checking Gmail for file review notifications...")
            scan_summary = mgr.process_emails() or {}
            
            logger.info("Checking Gmail for non-LAF/Judicial auto-drafts...")
            mgr.process_auto_drafts()

            portal_summary = {
                "success": False,
                "count": 0,
                "downloadable_count": 0,
                "pending_payment_count": 0,
                "probe_module": "",
            }
            with_portal = (os.environ.get("MAGI_FILE_REVIEW_CHECK_WITH_PORTAL", "1") or "").strip().lower() in {"1", "true", "yes", "on"}
            if with_portal:
                try:
                    logger.info("Checking live portal list for pending-payment/downloadable rows...")
                    probe_mod = _ensure_portal_probe_imports()
                    probe_mgr = probe_mod.FileReviewManager(
                        username=creds["username"],
                        password=creds["password"],
                        download_folder=creds["download_folder"],
                        db_manager=db,
                        headless=True,
                        log_callback=lambda msg: logger.info(msg),
                    )
                    try:
                        portal_summary = probe_mgr.probe_downloadable_from_portal() or portal_summary
                        portal_summary["probe_module"] = getattr(probe_mod, "__file__", "")
                    finally:
                        probe_mgr.close()
                except Exception as portal_e:
                    logger.warning("Portal probe in check_emails failed: %s", portal_e)
                    portal_summary = {
                        "success": False,
                        "error": str(portal_e)[:200],
                        "count": 0,
                        "downloadable_count": 0,
                        "pending_payment_count": 0,
                        "probe_module": "",
                    }

            pay_hits = int(scan_summary.get("payment_hits") or 0)
            pay_notified = int(scan_summary.get("payment_notified") or 0)
            dl_hits = int(scan_summary.get("download_hits") or 0)
            ready_cnt = int(scan_summary.get("ready_to_download_count") or 0)
            errors = scan_summary.get("errors") if isinstance(scan_summary, dict) else []
            err_cnt = len(errors) if isinstance(errors, list) else 0
            portal_count = int(portal_summary.get("count") or 0)
            portal_items_raw = portal_summary.get("items") if isinstance(portal_summary.get("items"), list) else []
            portal_effective = _collapse_portal_items(portal_items_raw) if with_portal and bool(portal_summary.get("success")) else {
                "raw_count": portal_count,
                "case_count": 0,
                "count": 0,
                "downloadable_count": 0,
                "pending_payment_count": 0,
                "items": [],
            }
            portal_raw_count = int(portal_effective.get("raw_count") or portal_count or 0)
            portal_case_count = int(portal_effective.get("case_count") or 0)
            portal_count = int(portal_effective.get("count") or 0)
            portal_downloadable = int(portal_effective.get("downloadable_count") or 0)
            portal_pending = int(portal_effective.get("pending_payment_count") or 0)
            recent_activity_all = _load_recent_processed_activity(creds["download_folder"], days=7, limit=8)
            recent_payment_activity_all = [
                it for it in recent_activity_all if _activity_artifact_kind(it) == "payment_slip"
            ]
            recent_review_download_activity_all = [
                it for it in recent_activity_all if _activity_artifact_kind(it) != "payment_slip"
            ]
            recent_payment_activity = _filter_unnotified_recent_activity(
                recent_payment_activity_all,
                creds["download_folder"],
                "recent_payment_activity",
            )
            recent_review_download_activity = _filter_unnotified_recent_activity(
                recent_review_download_activity_all,
                creds["download_folder"],
                "recent_review_download_activity",
            )

            payment_lines = [
                "💰 繳費單檢查完成",
                f"- 繳費相關信件：{pay_hits} 封（已通知 {pay_notified} 封）",
            ]
            review_lines = [
                "📮 閱卷通知檢查完成",
                f"- 可下載通知：{dl_hits} 封（待下載佇列 {ready_cnt} 件）",
            ]
            if with_portal:
                if bool(portal_summary.get("success")):
                    payment_lines.append(f"- 入口列表待繳費：{portal_pending} 件")
                    review_lines.append(
                        f"- 入口列表可下載：{portal_downloadable} 件（同案合併後需回報 {portal_count} 案，原始 {portal_raw_count} 列）"
                    )
                    # 列出未繳費案件明細（分逾期/即將到期/無期限）
                    portal_items = portal_effective.get("items") or []
                    groups = _filter_urgent_pending_payments(portal_items, days=14)
                    overdue = groups.get("overdue", [])
                    urgent = groups.get("urgent", [])
                    unknown = groups.get("unknown", [])

                    def _fmt_payment_items(items, limit=15):
                        lines = []
                        for idx, it in enumerate(items[:limit], 1):
                            caseno = it.get("court_case_no") or it.get("case_number") or "-"
                            party = it.get("party") or "(未知)"
                            dl = _format_roc_deadline(it.get("pay_deadline") or it.get("deadline") or "")
                            fee = it.get("fee") or ""
                            fee_str = f" ${fee}" if fee and fee != "0" else ""
                            lines.append(f"  {idx}. {party}｜{caseno}{fee_str} 期限:{dl}")
                        if len(items) > limit:
                            lines.append(f"  ...（另有 {len(items) - limit} 件）")
                        return lines

                    if urgent:
                        payment_lines.append("")
                        payment_lines.append(f"14 天內到期（{len(urgent)} 件）：")
                        payment_lines.extend(_fmt_payment_items(urgent))
                    if overdue:
                        payment_lines.append("")
                        payment_lines.append(f"⚠️ 已逾期未繳（{len(overdue)} 件）：")
                        payment_lines.extend(_fmt_payment_items(overdue))
                    # 列出可下載案件明細（排除已下載的）
                    dl_items_all = [it for it in portal_items if str(it.get("status") or "").strip() == "downloadable"]
                    dl_items = _filter_not_yet_downloaded(dl_items_all, creds.get("download_folder") or "")
                    if dl_items:
                        review_lines.append("")
                        review_lines.append(f"可下載案件（共 {len(dl_items)} 件，已下載 {len(dl_items_all) - len(dl_items)} 件已略過）：")
                        for idx, it in enumerate(dl_items[:10], 1):
                            caseno = it.get("court_case_no") or it.get("case_number") or "-"
                            party = it.get("party") or "(未知)"
                            review_lines.append(f"  {idx}. {party}｜{caseno}")
                        if len(dl_items) > 10:
                            review_lines.append(f"  ...（另有 {len(dl_items) - 10} 件）")
                else:
                    review_lines.append(f"- ⚠️ 入口列表探測失敗：{str(portal_summary.get('error') or '')[:120]}")
            if recent_payment_activity:
                payment_lines.append("")
                payment_lines.extend(_format_recent_activity_block("🗂️ 最近繳費處理", recent_payment_activity, limit=6))
            download_lines = []
            if recent_review_download_activity:
                download_lines = ["📥 卷宗下載回報", ""]
                download_lines.extend(_format_recent_activity_block("最近卷宗下載", recent_review_download_activity, limit=6))
            if err_cnt > 0:
                review_lines.append(f"- ⚠️ 掃描錯誤：{err_cnt} 筆")
            # ── 門戶狀態去重：避免每小時重複通知同樣的可下載/待繳數 ──
            _portal_state_path = os.path.join(
                creds.get("download_folder") or DEFAULT_DOWNLOAD_FOLDER,
                ".portal_notify_state.json",
            )
            _portal_state_prev: dict = {}
            try:
                if os.path.exists(_portal_state_path):
                    with open(_portal_state_path, "r", encoding="utf-8") as _pf:
                        _portal_state_prev = json.load(_pf) or {}
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2551, exc_info=True)
            _prev_downloadable = int(_portal_state_prev.get("portal_downloadable", -1))
            _prev_pending = int(_portal_state_prev.get("portal_pending", -1))
            _portal_downloadable_changed = (portal_downloadable != _prev_downloadable)
            _portal_pending_changed = (portal_pending != _prev_pending)

            payment_signal = bool(
                pay_hits > 0
                or pay_notified > 0
                or (portal_pending > 0 and _portal_pending_changed)
                # recent_payment_activity 不再單獨觸發摘要推送
                # 避免每小時重複推送同一份「最近繳費處理」清單
            )
            review_signal = bool(
                dl_hits > 0
                or ready_cnt > 0
                or (portal_downloadable > 0 and _portal_downloadable_changed)
                or err_cnt > 0
                or (with_portal and not bool(portal_summary.get("success")))
            )
            download_signal = bool(recent_review_download_activity)
            section_messages: list[tuple[str, str]] = []  # (msg, topic_key)
            if payment_signal:
                section_messages.append(("\n".join(payment_lines), "filereview_payment"))
            if review_signal:
                section_messages.append(("\n".join(review_lines), "filereview_download"))
            if download_signal:
                section_messages.append(("\n".join(download_lines), "filereview_download"))
            msg = "\n\n".join(m for m, _ in section_messages) if section_messages else "📧 閱卷/繳費檢查完成\n- 目前無新通知"
            warn = getattr(mgr, "_last_gmail_error", "") or ""
            retried_with_backup = False
            warn_message = ""
            if warn and ("NEED_INTERACTIVE_OAUTH" in warn or "invalid_grant" in warn.lower()):
                auto_restore = (os.environ.get("MAGI_GMAIL_AUTO_RESTORE_BACKUP", "1") or "").strip().lower() in {"1", "true", "yes", "on"}
                if auto_restore:
                    rt = _restore_latest_token_backup(_json_path("filereview_token.pickle"))
                    if rt.get("success"):
                        retried_with_backup = True
                        warn = ""
                        logger.info("Gmail token restored from backup: %s", rt.get("restored_from"))
                        mgr.process_emails()
                        warn = getattr(mgr, "_last_gmail_error", "") or ""
            if warn and ("NEED_INTERACTIVE_OAUTH" in warn or "invalid_grant" in warn.lower()):
                warn_message = "⚠️ 注意：Gmail token 需要重新授權，請執行 `reauth_gmail`。"
                msg += f"\n{warn_message}"
            elif retried_with_backup:
                warn_message = "♻️ 已自動從備份 token 修復並重試。"
                msg += f"\n{warn_message}"
                
            has_something_to_notify = bool(
                payment_signal
                or review_signal
                or download_signal
            )
            should_notify_now = notify and (notify_empty or has_something_to_notify)
            if should_notify_now or (warn and notify_empty):
                if section_messages:
                    for section_msg, section_topic in section_messages:
                        _notify(section_msg, True, topic_key=section_topic)
                    if should_notify_now:
                        _mark_recent_activity_notified(
                            recent_payment_activity,
                            creds["download_folder"],
                            "recent_payment_activity",
                        )
                        _mark_recent_activity_notified(
                            recent_review_download_activity,
                            creds["download_folder"],
                            "recent_review_download_activity",
                        )
                        # 保存門戶通知狀態，避免下次重複通知同樣數量
                        try:
                            os.makedirs(os.path.dirname(_portal_state_path), exist_ok=True)
                            with open(_portal_state_path, "w", encoding="utf-8") as _pf:
                                json.dump({
                                    "portal_downloadable": portal_downloadable,
                                    "portal_pending": portal_pending,
                                    "notified_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                                }, _pf, ensure_ascii=False)
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2630, exc_info=True)
                    if warn_message:
                        _notify(warn_message, True)
                else:
                    _notify(msg, True)
            out = {
                "success": True,
                "message": msg,
                "payment_hits": pay_hits,
                "payment_notified": pay_notified,
                "download_hits": dl_hits,
                "ready_to_download_count": ready_cnt,
                "scan_errors": err_cnt,
                "portal_count": portal_count,
                "portal_raw_row_count": portal_raw_count,
                "portal_case_count": portal_case_count,
                "portal_downloadable_count": portal_downloadable,
                "portal_pending_payment_count": portal_pending,
                "portal_probe_ok": bool(portal_summary.get("success")),
                "portal_probe_module": str(portal_summary.get("probe_module") or ""),
                "recent_processed_count": len(recent_activity_all),
                "recent_unnotified_count": len(recent_payment_activity) + len(recent_review_download_activity),
                "recent_payment_processed_count": len(recent_payment_activity),
                "recent_review_download_count": len(recent_review_download_activity),
                "recent_payment_processed_total": len(recent_payment_activity_all),
                "recent_review_download_total": len(recent_review_download_activity_all),
            }
            _eventlog(
                "filereview:gmail_check:done",
                ok=True,
                payload={
                    "warn": warn[:200] if warn else "",
                    "payment_hits": pay_hits,
                    "payment_notified": pay_notified,
                    "download_hits": dl_hits,
                    "ready_to_download_count": ready_cnt,
                    "scan_errors": err_cnt,
                    "portal_count": portal_count,
                    "portal_raw_row_count": portal_raw_count,
                    "portal_case_count": portal_case_count,
                    "portal_downloadable_count": portal_downloadable,
                    "portal_pending_payment_count": portal_pending,
                    "portal_probe_ok": bool(portal_summary.get("success")),
                    "portal_probe_module": str(portal_summary.get("probe_module") or ""),
                    "recent_processed_count": len(recent_activity_all),
                },
            )
            return out

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Email check failed: %s", error_msg)
        out = {"success": False, "error": error_msg}
        _eventlog("filereview:gmail_check:done", ok=False, payload=out)
        return out

def cmd_preview_emails(days: int = 7) -> dict:
    """正式信件掃描 + 通知預覽（不下載、不標記 processed、不發通知）。"""
    try:
        day_n = int(days or os.environ.get("MAGI_FILE_REVIEW_PREVIEW_DAYS", "21") or "21")
    except Exception:
        day_n = 21
    try:
        max_n = int(os.environ.get("MAGI_FILE_REVIEW_PREVIEW_MAX_RESULTS", "60") or "60")
    except Exception:
        max_n = 60
    day_n = max(1, min(day_n, 120))
    max_n = max(10, min(max_n, 200))
    _eventlog("filereview:gmail_preview:start", payload={"days": day_n, "max_results": max_n})
    _ensure_runtime_deps()
    cfg = _load_config()
    creds = _get_credentials(cfg)

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            gmail_credentials_path=_json_path("credentials.json"),
            gmail_token_path=_json_path("filereview_token.pickle"),
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Previewing Gmail file review notifications...")
            items = mgr.preview_recent_emails(days=day_n, max_results=max_n, allow_interactive=False)
            warn = getattr(mgr, "_last_gmail_error", "") or ""
            if warn:
                wl0 = warn.lower()
                auto_restore = (os.environ.get("MAGI_GMAIL_AUTO_RESTORE_BACKUP", "1") or "").strip().lower() in {"1", "true", "yes", "on"}
                if auto_restore and (("need_interactive_oauth" in wl0) or ("invalid_grant" in wl0)):
                    rt = _restore_latest_token_backup(_json_path("filereview_token.pickle"))
                    if rt.get("success"):
                        logger.info("Preview Gmail restored token from backup: %s", rt.get("restored_from"))
                        items = mgr.preview_recent_emails(days=day_n, max_results=max_n, allow_interactive=False)
                        warn = getattr(mgr, "_last_gmail_error", "") or ""
            if warn:
                wl = warn.lower()
                if ("need_interactive_oauth" in wl) or ("invalid_grant" in wl) or ("insufficientpermissions" in wl) or ("insufficient authentication scopes" in wl):
                    return {
                        "success": False,
                        "error": warn,
                        "hint": "請執行 `reauth_gmail` 重新授權（會開啟瀏覽器授權）。",
                    }
            out = {"success": True, "count": len(items), "items": items}
            _eventlog("filereview:gmail_preview:done", ok=True, payload={"count": len(items)})
            return out
        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Email preview failed: %s", error_msg)
        out = {"success": False, "error": error_msg}
        _eventlog("filereview:gmail_preview:done", ok=False, payload=out)
        return out


def cmd_downloadable_probe(days: int = 30, notify: bool = False) -> dict:
    """
    法院端狀態掃描（唯讀，不下載、不改資料）：
    回傳法院入口列表中「目前有線上下載按鈕」或「待繳費」的案件。
    注意：這些是法院端尚未過期的項目，不代表本機是否已下載歸檔。
    1) 優先掃法院入口「列表式查看」（最接近實際可下載狀態）
    2) 補充 Gmail 通知預覽（避免漏看通知信）
    """
    try:
        day_n = int(days or os.environ.get("MAGI_FILE_REVIEW_PREVIEW_DAYS", "30") or "30")
    except Exception:
        day_n = 21
    day_n = max(1, min(day_n, 120))

    portal_r = {"success": False, "error": "portal_probe_not_run"}
    try:
        _ensure_runtime_deps()
        cfg = _load_config()
        creds = _get_credentials(cfg)
        if not creds["username"] or not creds["password"]:
            portal_r = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_EEFILE_USERNAME/PASSWORD in .env"}
        else:
            mod = _ensure_portal_probe_imports()
            db = _get_db_manager(cfg)
            mgr = mod.FileReviewManager(
                username=creds["username"],
                password=creds["password"],
                download_folder=creds["download_folder"],
                db_manager=db,
                headless=True,
                log_callback=lambda msg: logger.info(msg),
            )
            try:
                logger.info("Running portal downloadable probe...")
                portal_r = mgr.probe_downloadable_from_portal()
                portal_r["probe_module"] = getattr(mod, "__file__", "")
                logger.info(
                    "Portal probe done: success=%s count=%s downloadable=%s module=%s",
                    bool(portal_r.get("success")),
                    portal_r.get("count"),
                    portal_r.get("downloadable_count"),
                    getattr(mod, "__file__", ""),
                )
            finally:
                mgr.close()
    except Exception as e:
        portal_r = {"success": False, "error": str(e)[:240]}

    portal_ok = bool(portal_r.get("success"))
    force_gmail = (os.environ.get("MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", "0") or "").strip().lower() in {"1", "true", "yes", "on"}
    want_gmail = force_gmail or (not portal_ok)
    if want_gmail:
        gmail_r = cmd_preview_emails(days=day_n)
    else:
        gmail_r = {"success": False, "skipped": True, "message": "skipped_by_portal_primary"}
    gmail_items = gmail_r.get("items") if isinstance(gmail_r.get("items"), list) else []
    gmail_downloadable = [
        it for it in gmail_items
        if isinstance(it, dict) and str(it.get("type") or "").strip().lower() == "download"
    ]

    # 以入口列表作為主判定；失敗才回退到 Gmail
    source = "portal" if portal_ok else "gmail"
    try:
        report_limit = int(os.environ.get("MAGI_FILE_REVIEW_PROBE_REPORT_ITEMS", "120") or "120")
    except Exception:
        report_limit = 120
    report_limit = max(20, min(report_limit, 500))

    if source == "portal":
        raw_items = portal_r.get("items") if isinstance(portal_r.get("items"), list) else []
        portal_effective = _collapse_portal_items(raw_items)
        effective_items = portal_effective.get("items") or []
        items = effective_items[:report_limit]
        raw_count = int(portal_r.get("count") or len(raw_items) or 0)
        case_count = int(portal_effective.get("case_count") or 0)
        count = int(portal_effective.get("count") or 0)
        downloadable_count = int(portal_effective.get("downloadable_count") or 0)
        pending_payment_count = int(portal_effective.get("pending_payment_count") or 0)
        msg = (
            f"法院端狀態掃描完成（入口列表）：法院端可下載 {downloadable_count} 件（含已歸檔），"
            f"待繳費 {pending_payment_count} 件，同案合併後共 {count} 案（原始 {raw_count} 列）"
        )
        if bool(gmail_r.get("success")):
            msg += f"；Gmail 通知 {len(gmail_items)} 封（可下載型 {len(gmail_downloadable)} 封）"
        elif bool(gmail_r.get("skipped")):
            msg += "；Gmail 補掃描已略過（可用 MAGI_FILE_REVIEW_PROBE_WITH_GMAIL=1 開啟）"
        out = {
            "success": True,
            "source": source,
            "count": count,
            "downloadable_count": downloadable_count,
            "pending_payment_count": pending_payment_count,
            "items": items,
            "items_total": len(effective_items),
            "items_truncated": len(effective_items) > len(items),
            "portal": {
                "success": bool(portal_r.get("success")),
                "count": count,
                "raw_count": raw_count,
                "case_count": case_count,
                "downloadable_count": downloadable_count,
                "pending_payment_count": pending_payment_count,
                "items_total": len(effective_items),
                "error": portal_r.get("error") if not bool(portal_r.get("success")) else "",
                "probe_module": str(portal_r.get("probe_module") or ""),
            },
            "gmail": {
                "success": bool(gmail_r.get("success")),
                "count": len(gmail_items),
                "downloadable_count": len(gmail_downloadable),
                "error": gmail_r.get("error") if not bool(gmail_r.get("success")) else "",
            },
            "message": msg,
        }
    else:
        items = gmail_items[:report_limit]
        count = len(gmail_items)
        downloadable_count = len(gmail_downloadable)
        msg = f"可下載判定完成（Gmail 回退）：通知 {count} 封，可下載型 {downloadable_count} 封"
        if portal_r.get("error"):
            msg += f"；入口列表探測失敗：{portal_r.get('error')}"
        out = {
            "success": bool(gmail_r.get("success")),
            "source": source,
            "count": count,
            "downloadable_count": downloadable_count,
            "items": items,
            "items_total": len(gmail_items),
            "items_truncated": len(gmail_items) > len(items),
            "portal": {
                "success": bool(portal_r.get("success")),
                "count": int(portal_r.get("count") or 0),
                "downloadable_count": int(portal_r.get("downloadable_count") or 0),
                "pending_payment_count": int(portal_r.get("pending_payment_count") or 0),
                "error": portal_r.get("error") if not bool(portal_r.get("success")) else "",
                "probe_module": str(portal_r.get("probe_module") or ""),
            },
            "gmail": gmail_r,
            "message": msg,
        }

    if notify:
        _notify(f"📮 閱卷可下載判定：{out.get('message')}", True)

    _eventlog(
        "filereview:gmail_downloadable_probe:done",
        ok=bool(out.get("success")),
        payload={
            "source": source,
            "count": int(out.get("count") or 0),
            "downloadable_count": int(out.get("downloadable_count") or 0),
            "pending_payment_count": int(out.get("pending_payment_count") or 0),
            "portal_ok": bool(portal_r.get("success")),
            "portal_probe_module": str(portal_r.get("probe_module") or ""),
            "gmail_ok": bool(gmail_r.get("success")),
        },
    )
    return out


def cmd_reauth_gmail(notify: bool = True) -> dict:
    """互動式重新授權閱卷 Gmail（會開啟瀏覽器/本機 OAuth 回呼）。"""
    _eventlog("filereview:reauth:start")
    _ensure_runtime_deps()
    cfg = _load_config()
    creds = _get_credentials(cfg)

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            gmail_credentials_path=_json_path("credentials.json"),
            gmail_token_path=_json_path("filereview_token.pickle"),
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Reauth Gmail for file review...")
            ok = bool(mgr.reauth_gmail())
            msg = "✅ 閱卷信箱重新授權成功" if ok else "❌ 閱卷信箱重新授權失敗"
            _notify(msg, notify)
            out = {"success": ok, "message": msg}
            _eventlog("filereview:reauth:done", ok=bool(ok), payload=out)
            return out
        finally:
            mgr.close()
    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Reauth failed: %s", error_msg)
        out = {"success": False, "error": error_msg}
        _eventlog("filereview:reauth:done", ok=False, payload=out)
        return out


def cmd_check_stale(days: int = 90, notify: bool = True) -> dict:
    """Check for cases that haven't been reviewed in N days."""
    cfg = _load_config()
    creds = _get_credentials(cfg)

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            stale = mgr.check_stale_cases(
                review_folder_path=creds["download_folder"],
                days=days
            )

            count = len(stale) if stale else 0
            msg = f"⏰ 閱卷到期檢查完成 — {count} 件超過 {days} 天"
            if count > 0 and notify:
                details = "\n".join(str(s) for s in stale[:5])
                _notify(msg + "\n" + details, True)
            return {"success": True, "stale_count": count, "message": msg}

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Stale check failed: %s", error_msg)
        return {"success": False, "error": error_msg}


def cmd_dismiss_payment(case_keyword: str, reason: str = "") -> dict:
    """手動標記案件繳費通知為已處理（永久跳過通知）"""
    cfg = _load_config()
    creds = _get_credentials(cfg)
    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)
        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )
        try:
            result = mgr.dismiss_payment(case_keyword, reason=reason)
            return result
        finally:
            mgr.close()
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def cmd_undismiss_payment(case_keyword: str) -> dict:
    """取消手動跳過標記（恢復繳費通知）"""
    cfg = _load_config()
    creds = _get_credentials(cfg)
    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)
        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )
        try:
            return mgr.undismiss_payment(case_keyword)
        finally:
            mgr.close()
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def cmd_list_dismissed_payments() -> dict:
    """列出所有手動跳過的繳費通知"""
    cfg = _load_config()
    creds = _get_credentials(cfg)
    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)
        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )
        try:
            return mgr.list_dismissed_payments()
        finally:
            mgr.close()
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# LINE/DC Command Parsing
# ---------------------------------------------------------------------------
def parse_line_command(text: str) -> Optional[dict]:
    """
    Parse LINE/DC messages into skill commands.

    Supported:
        閱卷聲請 台北 114訴123 民事
        閱卷查核 台北 114訴123
        下載閱卷
        下載閱卷 114年度訴字第123號
        檢查閱卷信箱
        閱卷可下載判定
        閱卷到期檢查
    """
    t = (text or "").strip()
    if not t:
        return None

    # Apply triggers
    apply_triggers = ["閱卷聲請", "聲請閱卷", "申請閱卷"]
    for trigger in apply_triggers:
        if t.startswith(trigger):
            remainder = t[len(trigger):].strip()
            return _parse_apply_args(remainder)

    # Probe triggers
    probe_triggers = ["閱卷查核", "查核閱卷", "卷宗查核", "查核卷宗", "卷宗檢核", "檢核卷宗"]
    for trigger in probe_triggers:
        if t.startswith(trigger):
            remainder = t[len(trigger):].strip()
            return _parse_probe_args(remainder)

    # Download triggers
    dl_triggers = ["下載閱卷", "閱卷下載"]
    for trigger in dl_triggers:
        if t.startswith(trigger):
            remainder = t[len(trigger):].strip()
            if remainder:
                # 僅在 remainder 真的是案號格式時才套用單案下載；
                # 避免「下載閱卷 王小明案」這種語句把姓名誤當案號，導致只跑到單一案件。
                if (
                    re.search(r"\d{2,4}\s*(?:年度)?\s*[^\d\s]{1,12}\s*(?:字)?\s*(?:第)?\s*\d+\s*(?:號)?", remainder)
                    or re.search(r"\d{2,4}\.[^.\s]{1,12}\.\d+", remainder)
                    or re.search(r"\d{6,8}-[A-Za-z]-\d{3,4}", remainder)
                ):
                    return {"command": "download", "case_number": remainder}
                if remainder.lower() in {"all", "全部", "全案", "全部案件"}:
                    return {"command": "download"}
            return {"command": "download"}

    # Email check triggers
    if any(t.startswith(k) for k in ["檢查閱卷信箱", "閱卷信箱", "閱卷郵件"]):
        return {"command": "check_emails"}

    # Email preview triggers
    if any(t.startswith(k) for k in ["閱卷通知預覽", "預覽閱卷通知", "預覽閱卷信箱", "預覽閱卷郵件"]):
        return {"command": "preview_emails"}

    # Downloadable probe triggers
    if any(t.startswith(k) for k in ["閱卷可下載判定", "可下載判定", "判定可下載", "閱卷可下載"]):
        return {"command": "downloadable_probe"}

    if any(t.startswith(k) for k in ["重新授權閱卷信箱", "閱卷信箱重新授權", "閱卷Gmail重新授權"]):
        return {"command": "reauth_gmail"}

    # Stale check triggers
    if any(t.startswith(k) for k in ["閱卷到期", "閱卷過期", "閱卷期限"]):
        return {"command": "check_stale"}

    # Dismiss payment triggers
    dismiss_triggers = ["跳過繳費", "繳費跳過", "已繳費"]
    for trigger in dismiss_triggers:
        if t.startswith(trigger):
            remainder = t[len(trigger):].strip()
            if remainder:
                return {"command": "dismiss_payment", "case_keyword": remainder}
            return None
    # 反向：「BS000-A112071已繳費」「BS000-A112071 已繳費」（案號在前）
    m_dismiss = re.search(r"^(.+?)\s*(?:已繳費|已經繳費|繳費完畢|繳費了)$", t)
    if m_dismiss:
        kw = m_dismiss.group(1).strip()
        if kw:
            return {"command": "dismiss_payment", "case_keyword": kw}

    # Undismiss payment triggers
    undismiss_triggers = ["恢復繳費通知", "恢復繳費"]
    for trigger in undismiss_triggers:
        if t.startswith(trigger):
            remainder = t[len(trigger):].strip()
            if remainder:
                return {"command": "undismiss_payment", "case_keyword": remainder}
            return None

    # List dismissed payments
    if t in ("列出跳過繳費", "跳過繳費清單"):
        return {"command": "list_dismissed_payments"}

    return None


def _parse_case_spec(text: str) -> Optional[dict]:
    """Parse '<法院> <案號> [當事人]' natural language args."""
    if not text:
        return None

    parts = text.split()
    if len(parts) < 2:
        return None

    court = parts[0]
    case_text = parts[1] if len(parts) > 1 else ""

    # Parse case number like:
    # 114訴123 / 114年度訴字第123號 / 114 訴 123
    m = re.match(r"(\d{2,3})\s*(?:年度)?\s*([^\d\s]+)\s*(?:字)?\s*(?:第)?\s*(\d+)\s*(?:號)?", case_text)
    if m:
        case_type = re.sub(r"(字第|字|第)", "", (m.group(2) or "")).strip()
        result = {"court_code": court, "year": m.group(1), "case_type": case_type, "case_number": m.group(3)}
        # 第三個以後的 parts 當作當事人姓名
        if len(parts) >= 3:
            result["client_name"] = " ".join(parts[2:])
        return result

    return None


def _parse_apply_args(text: str) -> Optional[dict]:
    """Parse 'apply' arguments from natural language."""
    payload = _parse_case_spec(text)
    if not payload:
        return None
    payload["command"] = "apply"
    return payload


def _parse_probe_args(text: str) -> Optional[dict]:
    """Parse 'probe' arguments from natural language."""
    payload = _parse_case_spec(text)
    if not payload:
        return None
    payload["command"] = "probe"
    return payload


# ---------------------------------------------------------------------------
# Main / CLI
# ---------------------------------------------------------------------------
def _load_jsonish(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"case_number": text}


def main() -> int:
    ap = argparse.ArgumentParser(description="file-review-orchestrator skill")
    ap.add_argument("--task", default="help", help="task text")
    ap.add_argument("--json-cmd", action="store_true", help="read JSON command from stdin")
    args = ap.parse_args()

    # --json-cmd 模式：從 stdin 讀取 JSON 指令（供 orchestrator subprocess 呼叫）
    if args.json_cmd:
        try:
            raw = sys.stdin.read().strip()
            cmd_data = json.loads(raw) if raw else {}
        except Exception:
            print(json.dumps({"success": False, "error": "invalid JSON input"}))
            return 1
        cmd_name = cmd_data.get("cmd", "")
        if cmd_name == "upload_payment_proof_from_image":
            r = cmd_upload_payment_proof_from_image(
                image_path=cmd_data.get("image_path", ""),
                notify=cmd_data.get("notify", True),
            )
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r.get("success") else 1
        print(json.dumps({"success": False, "error": f"unknown json-cmd: {cmd_name}"}))
        return 1

    task = (args.task or "").strip()

    if task in {"help", "summary", "list"}:
        return _ok({
            "success": True,
            "product_profile": product_profile_report("file_review"),
            "commands": [
                "help",
                "self_test",
                "db_smoke",
                'probe {"court_code":"TPD","year":"114","case_type":"訴","case_number":"123"}',
                'apply {"court_code":"TPD","year":"114","case_type":"訴","case_number":"123"}',
                "download",
                "download_sync",
                'download {"case_number":"..."}',
                'download_status {"job_id":"latest"}',
                "download_payment_slips",
                'upload_payment_proof {"court_code":"HLD","year":"114","case_type":"原金訴","case_number":"166","file_path":"/path/to/screenshot.png"}',
                "upload_payment_proofs_batch",
                "check_emails",
                "preview_emails",
                "downloadable_probe",
                'downloadable_probe {"days":30}',
                "check_stale",
                "reauth_gmail",
                'dismiss_payment {"case_keyword":"114原金訴4"}',
                'undismiss_payment {"case_keyword":"114原金訴4"}',
                "list_dismissed_payments",
            ],
            "line_triggers": [
                "閱卷查核 <法院> <案號>",
                "閱卷聲請 <法院> <案號>",
                "下載閱卷",
                "下載閱卷 <案號>",
                "下載繳費單",
                "上傳繳費憑證",
                "批次上傳繳費憑證",
                "檢查閱卷信箱",
                "預覽閱卷通知",
                "閱卷可下載判定",
                "閱卷到期檢查",
                "重新授權閱卷信箱",
                "跳過繳費 <案號或當事人>",
                "恢復繳費通知 <案號或當事人>",
                "列出跳過繳費",
            ],
        })

    if task == "self_test":
        errors = []
        try:
            _ensure_imports()
        except Exception as e:
            errors.append("import file_review_automation failed: " + str(e)[:100])

        cfg = _load_config()
        creds = _get_credentials(cfg)
        if not creds["username"]:
            errors.append("missing MAGI_JUDICIAL_EEFILE_USERNAME in .env")
        if not creds["password"]:
            errors.append("missing MAGI_JUDICIAL_EEFILE_PASSWORD in .env")

        ok = len(errors) == 0
        return _ok({"success": ok, "errors": errors if errors else None,
                     "credentials_found": bool(creds["username"]),
                     "product_profile": product_profile_report("file_review", config=cfg)})

    if task.startswith("db_smoke"):
        payload = _load_jsonish(task[len("db_smoke"):].strip())
        r = cmd_db_smoke(prefer_profile=payload.get("prefer_profile", ""))
        return _ok(r)

    if task.startswith("probe"):
        payload = _load_jsonish(task[len("probe"):].strip())
        r = cmd_probe(
            court_code=payload.get("court_code", ""),
            year=payload.get("year", ""),
            case_type=payload.get("case_type", ""),
            case_number=payload.get("case_number", ""),
            client_name=payload.get("client_name", ""),
        )
        return _ok(r)

    if task.startswith("apply"):
        payload = _load_jsonish(task[len("apply"):].strip())
        r = cmd_apply(
            court_code=payload.get("court_code", ""),
            year=payload.get("year", ""),
            case_type=payload.get("case_type", ""),
            case_number=payload.get("case_number", ""),
            client_name=payload.get("client_name", ""),
            auto_submit=bool(payload.get("auto_submit", True)),
            folder_path=payload.get("folder_path", ""),
        )
        return _ok(r)

    if task.startswith("download_payment_slips") or task == "下載繳費單":
        payload = _load_jsonish(task[len("download_payment_slips"):].strip()) if task.startswith("download_payment_slips") else {}
        r = cmd_download_payment_slips(
            max_days=int(payload.get("max_days", 14) or 14),
        )
        return _ok(r)

    if task.startswith("upload_attachment"):
        payload = _load_jsonish(task[len("upload_attachment"):].strip())
        r = cmd_upload_attachment(
            court_code=payload.get("court_code", ""),
            year=payload.get("year", ""),
            case_type=payload.get("case_type", ""),
            case_number=payload.get("case_number", ""),
            client_name=payload.get("client_name", ""),
            file_path=payload.get("file_path", ""),
            file_remark=payload.get("file_remark", "委任狀"),
        )
        return _ok(r)

    if task.startswith("upload_payment_proofs_batch") or task == "批次上傳繳費憑證":
        payload = _load_jsonish(task[len("upload_payment_proofs_batch"):].strip()) if task.startswith("upload_payment_proofs_batch") else {}
        r = cmd_upload_payment_proofs_batch(
            screenshot_dir=payload.get("screenshot_dir", ""),
        )
        return _ok(r)

    if task.startswith("upload_payment_proof") or task == "上傳繳費憑證":
        payload = _load_jsonish(task[len("upload_payment_proof"):].strip()) if task.startswith("upload_payment_proof") else {}
        r = cmd_upload_payment_proof(
            court_code=payload.get("court_code", ""),
            year=payload.get("year", ""),
            case_type=payload.get("case_type", ""),
            case_number=payload.get("case_number", ""),
            client_name=payload.get("client_name", ""),
            file_path=payload.get("file_path", ""),
        )
        return _ok(r)

    if task.startswith("check_emails"):
        payload = _load_jsonish(task[len("check_emails"):].strip())
        notify_empty = bool(payload.get("notify_empty", True))
        r = cmd_check_emails(notify=True, notify_empty=notify_empty)
        return _ok(r)

    if task == "檢查閱卷信箱":
        r = cmd_check_emails()
        return _ok(r)

    if task in ("preview_emails", "閱卷通知預覽", "預覽閱卷通知"):
        r = cmd_preview_emails()
        return _ok(r)

    if task.startswith("downloadable_probe") or task in ("可下載判定", "閱卷可下載判定"):
        payload = _load_jsonish(task[len("downloadable_probe"):].strip()) if task.startswith("downloadable_probe") else {}
        r = cmd_downloadable_probe(days=int(payload.get("days", 30) or 30))
        return _ok(r)

    if task.startswith("download_status"):
        payload = _load_jsonish(task[len("download_status"):].strip())
        r = cmd_download_status(job_id=str(payload.get("job_id", "latest") or "latest"))
        return _ok(r)

    if task.startswith("download_worker"):
        payload = _load_jsonish(task[len("download_worker"):].strip())
        r = cmd_download_worker(payload if isinstance(payload, dict) else {})
        return _ok(r)

    if task.startswith("download_sync"):
        payload = _load_jsonish(task[len("download_sync"):].strip())
        cn = payload.get("case_number", "")
        r = cmd_download(case_number=cn)
        return _ok(r)

    if task == "download" or task.startswith("download "):
        payload = _load_jsonish(task[len("download"):].strip())
        cn = payload.get("case_number", "")
        if _truthy(os.environ.get("MAGI_FILE_REVIEW_DOWNLOAD_BACKGROUND", "1")):
            r = cmd_download_background(case_number=cn)
        else:
            r = cmd_download(case_number=cn)
        return _ok(r)

    if task in ("reauth_gmail", "重新授權閱卷信箱"):
        r = cmd_reauth_gmail()
        return _ok(r)

    if task.startswith("check_stale"):
        r = cmd_check_stale()
        return _ok(r)

    if task.startswith("dismiss_payment"):
        payload = _load_jsonish(task[len("dismiss_payment"):].strip())
        kw = payload.get("case_keyword") or payload.get("keyword") or ""
        reason = payload.get("reason", "")
        if not kw:
            return _ok({"success": False, "error": "missing case_keyword"})
        r = cmd_dismiss_payment(kw, reason=reason)
        return _ok(r)

    if task.startswith("undismiss_payment"):
        payload = _load_jsonish(task[len("undismiss_payment"):].strip())
        kw = payload.get("case_keyword") or payload.get("keyword") or ""
        if not kw:
            return _ok({"success": False, "error": "missing case_keyword"})
        r = cmd_undismiss_payment(kw)
        return _ok(r)

    if task in ("list_dismissed_payments", "列出跳過繳費"):
        r = cmd_list_dismissed_payments()
        return _ok(r)

    # Try as LINE command
    parsed = parse_line_command(task)
    if parsed:
        cmd = parsed["command"]
        if cmd == "apply":
            r = cmd_apply(
                court_code=parsed.get("court_code", ""),
                year=parsed.get("year", ""),
                case_type=parsed.get("case_type", ""),
                case_number=parsed.get("case_number", ""),
                client_name=parsed.get("client_name", ""),
            )
            return _ok(r)
        if cmd == "probe":
            r = cmd_probe(
                court_code=parsed.get("court_code", ""),
                year=parsed.get("year", ""),
                case_type=parsed.get("case_type", ""),
                case_number=parsed.get("case_number", ""),
            )
            return _ok(r)
        if cmd == "download":
            cn = parsed.get("case_number", "")
            if _truthy(os.environ.get("MAGI_FILE_REVIEW_DOWNLOAD_BACKGROUND", "1")):
                r = cmd_download_background(case_number=cn)
            else:
                r = cmd_download(case_number=cn)
            return _ok(r)
        if cmd == "check_emails":
            r = cmd_check_emails()
            return _ok(r)
        if cmd == "downloadable_probe":
            r = cmd_downloadable_probe()
            return _ok(r)
        if cmd == "preview_emails":
            r = cmd_preview_emails()
            return _ok(r)
        if cmd == "reauth_gmail":
            r = cmd_reauth_gmail()
            return _ok(r)
        if cmd == "check_stale":
            r = cmd_check_stale()
            return _ok(r)
        if cmd == "dismiss_payment":
            kw = parsed.get("case_keyword", "")
            if kw:
                r = cmd_dismiss_payment(kw)
                return _ok(r)
        if cmd == "undismiss_payment":
            kw = parsed.get("case_keyword", "")
            if kw:
                r = cmd_undismiss_payment(kw)
                return _ok(r)
        if cmd == "list_dismissed_payments":
            r = cmd_list_dismissed_payments()
            return _ok(r)

    return _ok({"success": False, "error": "unknown task: " + task})


if __name__ == "__main__":
    raise SystemExit(main())
