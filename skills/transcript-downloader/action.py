#!/usr/bin/env python3
"""
transcript-downloader -- 電子筆錄調閱協調器
=============================================
包裝 judicial_automation_v2.CourtRecordDownloader，
提供 CASPER skill API 與 LINE/DC 指令介面。

Usage (CLI):
    python action.py --task 'download {"case_number":"114年度訴字第123號"}'
    python action.py --task 'download_all'
    python action.py --task 'sync'
    python action.py --task 'help'
"""
import argparse
import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional
from urllib import request as _urlreq

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------
_MAGI_ROOT_DEFAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MAGI_ROOT = os.environ.get("MAGI_ROOT", _MAGI_ROOT_DEFAULT)

# Ensure .env is loaded (critical when run as subprocess)
_env_path = os.path.join(MAGI_ROOT, ".env")
if os.path.isfile(_env_path):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path, override=False)
    except ImportError:
        with open(_env_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    _k = _k.strip()
                    _v = _v.strip()
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v

if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)
from api.runtime_paths import (
    get_config_path,
    get_module_path,
    get_orch_dir,
    get_skill_python,
)
from api.product_runtime import apply_product_runtime_env, product_profile_report

ORCH_DIR = str(get_orch_dir())
CODE_DIR = ORCH_DIR
VENV_PY = str(get_skill_python())
CONFIG_PATH = str(get_config_path("config.json"))
DEFAULT_DOWNLOAD_FOLDER = os.path.expanduser("~/Desktop/MAGI_v2/筆錄下載")
MANUAL_QUEUE_PATH = Path(
    os.environ.get(
        "MAGI_TRANSCRIPT_MANUAL_QUEUE_PATH",
        f"{MAGI_ROOT}/static/transcript_manual_queue.jsonl",
    )
)
TRANSCRIPT_RUNTIME = apply_product_runtime_env("transcript", env=os.environ)

logger = logging.getLogger("transcript-downloader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


def _maybe_reexec_venv() -> None:
    """
    Keep runtime consistent with other MAGI skills by preferring MAGI's venv.
    This avoids dependency drift (e.g. PyMuPDF missing under system python).
    """
    if os.environ.get("MAGI_TRANSCRIPT_NO_VENV", "").strip() == "1":
        return
    try:
        target_prefix = os.path.realpath(str(Path(VENV_PY).expanduser().parent.parent))
        current_prefix = os.path.realpath(sys.prefix)
        if os.path.exists(VENV_PY) and current_prefix != target_prefix:
            os.execv(VENV_PY, [VENV_PY, __file__, *sys.argv[1:]])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 88, exc_info=True)

def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    """
    Best-effort：將筆錄流程的關鍵事件寫入向量記憶，供對話追溯。
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
            source="transcript_downloader",
        )
    except Exception:
        return


def _ok(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


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
        "username": os.environ.get("MAGI_JUDICIAL_RECORD_USERNAME") or jc.get("record_username", ""),
        "password": os.environ.get("MAGI_JUDICIAL_RECORD_PASSWORD") or jc.get("record_password", ""),
        "download_folder": jc.get("record_download_folder", DEFAULT_DOWNLOAD_FOLDER),
        "headless": jc.get("headless", True),
    }


def _ensure_imports():
    """Lazy import judicial_automation_v2, preferring MAGI's maintained copy."""
    import importlib.util

    candidates = [str(get_module_path("judicial_automation_v2.py"))]
    for idx, path in enumerate(candidates):
        if not os.path.exists(path):
            continue
        mod_name = f"magi_judicial_automation_v2_{idx}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("judicial_automation_v2.py not found in MAGI")


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------
def _notify(text: str, flag: bool = True):
    if not flag:
        return

    # Prefer centralized notifier with outbox/retry semantics.
    try:
        from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore

        st = send_telegram_push_with_status(
            str(text or ""),
            severity="info",
            source="transcript_downloader",
            topic_key="transcript_dl",
            queue_on_fail=True,
        ) or {}
        if bool(st.get("telegram")) or bool(st.get("queued")):
            return
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 171, exc_info=True)

    def _notify_tg(msg: str) -> bool:
        token = (
            os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")
            or os.environ.get("MAGI_TELEGRAM_BOT_TOKEN")
            or ""
        ).strip()
        targets = [
            x.strip()
            for x in (os.environ.get("MAGI_NOTIFY_TELEGRAM_IDS") or "").split(",")
            if x.strip()
        ]
        if (not token) or (not targets):
            try:
                _magi_cfg_path = Path(get_config_path("config.json"))
                if _magi_cfg_path.exists():
                    _magi_cfg = json.loads(_magi_cfg_path.read_text(encoding="utf-8")) or {}
                    _magi_tg = _magi_cfg.get("telegram") or {}
                    _magi_notify = _magi_tg.get("notifyTo") or []
                    if isinstance(_magi_notify, list):
                        targets.extend([str(x).strip() for x in _magi_notify if str(x).strip()])
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 194, exc_info=True)
            try:
                oc = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
                if oc.exists():
                    cfg = json.loads(oc.read_text(encoding="utf-8")) or {}
                    tg = ((cfg.get("channels") or {}).get("telegram") or {})
                    if not token:
                        token = str(tg.get("botToken") or "").strip()
                    nt = tg.get("notifyTo") or []
                    if isinstance(nt, list):
                        targets.extend([str(x).strip() for x in nt if str(x).strip()])
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 206, exc_info=True)
        if (not token) or (not targets):
            return False
        ok = False
        for cid in list(dict.fromkeys(targets)):
            try:
                req = _urlreq.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=json.dumps({"chat_id": cid, "text": msg}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _urlreq.urlopen(req, timeout=10) as resp:  # nosec B310
                    if int(getattr(resp, "status", 200)) < 300:
                        ok = True
            except Exception:
                continue
        return ok

    try:
        _notify_tg(text)
    except Exception as e:
        logger.warning("Notification failed: %s", e)


def _looks_like_captcha_error(msg: str) -> bool:
    s = (msg or "").strip().lower()
    if not s:
        return False
    keys = [
        "captcha",
        "驗證碼",
        "人機",
        "robot",
        "cloudflare",
        "cf challenge",
        "challenge required",
    ]
    return any(k in s for k in keys)


def _payload_contains_captcha(payload) -> bool:
    try:
        if payload is None:
            return False
        if isinstance(payload, str):
            return _looks_like_captcha_error(payload)
        if isinstance(payload, dict):
            for v in payload.values():
                if _payload_contains_captcha(v):
                    return True
            return False
        if isinstance(payload, (list, tuple, set)):
            for v in payload:
                if _payload_contains_captcha(v):
                    return True
            return False
    except Exception:
        return False
    return False


def _enqueue_manual_review(action: str, payload: dict, error_msg: str) -> str:
    ticket = f"tr_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
    row = {
        "ticket": ticket,
        "created_at": datetime.now().isoformat(),
        "status": "pending_manual",
        "action": str(action or "unknown"),
        "payload": payload or {},
        "reason": "captcha",
        "error": str(error_msg or "")[:500],
    }
    try:
        MANUAL_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MANUAL_QUEUE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Failed to write transcript manual queue: %s", e)
    return ticket


def _case_label(row: dict) -> str:
    party = str((row or {}).get("client_name") or "").strip()
    court_case_no = str((row or {}).get("court_case_number") or "").strip()
    case_no = str((row or {}).get("case_number") or "").strip()
    parts = [x for x in [party, court_case_no or case_no] if x]
    if parts:
        return "｜".join(parts)
    return court_case_no or case_no or "未判斷案件"


def _summarize_download_results(results: dict, *, max_cases: int = 20) -> tuple[str, dict]:
    try:
        max_cases = int(os.environ.get("MAGI_TRANSCRIPT_NOTIFY_MAX_CASES", str(max_cases)) or str(max_cases))
    except Exception:
        max_cases = int(max_cases)
    max_cases = max(5, min(max_cases, 50))
    rows = results.get("cases") if isinstance(results, dict) else []
    if not isinstance(rows, list):
        rows = []
    normalized_rows = [r for r in rows if isinstance(r, dict)]
    ok_rows = [r for r in normalized_rows if bool(r.get("success"))]
    failed_rows = [r for r in normalized_rows if not bool(r.get("success"))]
    total_files = 0
    case_summaries = []
    for row in ok_rows:
        files = row.get("files")
        file_list = files if isinstance(files, list) else []
        file_count = len(file_list)
        total_files += file_count
        case_summaries.append(
            {
                "case_number": str(row.get("case_number") or "").strip(),
                "court_case_number": str(row.get("court_case_number") or "").strip(),
                "client_name": str(row.get("client_name") or "").strip(),
                "file_count": file_count,
                "files": [str(fp) for fp in file_list[:10]],
            }
        )

    lines = [f"📥 筆錄批次下載完成（{total_files} 份，{len(ok_rows)} 案）"]
    for idx, row in enumerate(ok_rows[:max_cases], start=1):
        files = row.get("files")
        file_list = files if isinstance(files, list) else []
        lines.append(f"{idx}. {_case_label(row)}（{len(file_list)} 份）")
        for fp in file_list[:2]:
            lines.append(f"- {os.path.basename(str(fp))}")
    remaining = len(ok_rows) - min(len(ok_rows), max_cases)
    if remaining > 0:
        lines.append(f"...其餘 {remaining} 案略")
    # 區分「查無筆錄」(正常) 和「下載失敗」(需確認)
    no_data_rows = [r for r in failed_rows if not r.get("error")]
    error_rows   = [r for r in failed_rows if r.get("error")]
    if no_data_rows:
        lines.append(f"ℹ️ 查無筆錄：{len(no_data_rows)} 案（法院尚無資料，系統正常）")
    if error_rows:
        lines.append(f"❌ 下載失敗（需確認）：{len(error_rows)} 案")
        for r in error_rows[:5]:
            lines.append(f"  • {_case_label(r)}: {str(r.get('error') or '')[:80]}")

    summary = {
        "downloaded_count": total_files,
        "downloaded_cases_count": len(ok_rows),
        "no_data_cases_count": len(no_data_rows),
        "failed_cases_count": len(error_rows),
        "cases": case_summaries[:50],
    }
    return "\n".join(lines), summary


# ---------------------------------------------------------------------------
# DB Helper
# ---------------------------------------------------------------------------
def _get_db_manager(cfg: dict):
    """Try to create a LegalBridgeDB instance for case lookups."""
    try:
        if CODE_DIR not in sys.path:
            sys.path.insert(0, CODE_DIR)
        from legalbridge_core import LegalBridgeDB
        return LegalBridgeDB()
    except Exception as legacy_err:
        try:
            from legalbridge_core import ConfigManager, DatabaseManager
            cfg_mgr = ConfigManager(config_path=CONFIG_PATH)
            return DatabaseManager(cfg_mgr)
        except Exception as new_err:
            logger.warning("DB manager not available: legacy=%s ; new=%s", legacy_err, new_err)
            return None


def _ensure_local_cases_schema() -> None:
    """
    Best-effort migration for local MariaDB `cases` table.

    Nightly/排程通常在 Keeper (主 DB) 離線時改用本機 DB；舊 schema 可能缺少 `court_name`，
    會讓 `get_cases_from_db()` 直接噴 `Unknown column ...` 而整個筆錄同步退化成只做更名。

    Policy: NEVER delete data; only CREATE/ALTER to add missing columns/indexes.
    """
    try:
        import mysql.connector
    except Exception:
        return

    def _cfg_from_config_json() -> dict:
        try:
            cfg_path = str(get_config_path("config.json"))
            if not os.path.exists(cfg_path):
                return {}
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            profiles = cfg.get("mariadb_profiles") or []
            for want in ("Home_Local_Test", "Studio_Local"):
                for p in profiles:
                    if (p.get("profile_name") or "").strip() != want:
                        continue
                    c = (p.get("config") or {})
                    if c.get("host") and c.get("user") and c.get("database"):
                        return {
                            "host": c.get("host"),
                            "port": int(c.get("port") or 3306),
                            "user": c.get("user") or os.environ.get("OSC_DB_USER", "python_user"),
                            "password": c.get("password") or os.environ.get("OSC_DB_PASSWORD", ""),
                            "database": c.get("database"),
                        }
        except Exception:
            return {}
        return {}

    base = _cfg_from_config_json() or {
        "host": "127.0.0.1",
        "port": 3307,
        "user": "python_user",
        "password": "",
        "database": "law_firm_data",
    }

    # Env overrides (kept for compatibility with other headless modules)
    model = (os.environ.get("MAGI_WHISPER_MODEL") or "medium").strip() or "medium"
    timeout_sec = int(os.environ.get("MAGI_WHISPER_TIMEOUT_SEC", "3600") or "3600")
    timeout_sec = max(30, min(timeout_sec, 3600))
    # The `language` variable is not defined in this scope. Assuming it's a placeholder or intended to be `base["user"]`
    # as a fallback for a different context. For this function, it's likely not relevant.
    # Keeping the original `base["user"]` as a fallback for `forced_language` to maintain syntactic correctness.
    forced_language = (os.environ.get("MAGI_WHISPER_LANGUAGE") or "").strip() or base["user"]
    host = (os.environ.get("OSC_DB_HOST") or base["host"]).strip() or base["host"]
    port = int((os.environ.get("OSC_DB_PORT") or str(base["port"])).strip() or str(base["port"]))
    user = (os.environ.get("OSC_DB_USER") or base["user"]).strip() or base["user"]
    password = (os.environ.get("OSC_DB_PASSWORD") or base["password"]).strip()
    db_name = (os.environ.get("OSC_DB_NAME") or base["database"]).strip() or base["database"]
    if (db_name or "").lower() == "law_firm_db":
        db_name = "law_firm_data"

    conn = None
    cur = None
    try:
        conn = mysql.connector.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=db_name,
            autocommit=False,
            charset="utf8mb4",
            collation="utf8mb4_unicode_ci",
            use_pure=True,
            connection_timeout=3,
        )
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS `cases` (
              `id` int(11) NOT NULL AUTO_INCREMENT,
              `case_number` varchar(50) NOT NULL,
              `client_name` varchar(255) DEFAULT '',
              `case_type` varchar(100) DEFAULT '',
              `case_reason` text DEFAULT NULL,
              `case_category` varchar(50) DEFAULT '',
              `legal_aid_number` varchar(100) DEFAULT '',
              `court_case_number` varchar(255) DEFAULT '',
              `notes` text DEFAULT NULL,
              `folder_path` text DEFAULT NULL,
              `created_date` timestamp NULL DEFAULT current_timestamp(),
              PRIMARY KEY (`id`),
              KEY `idx_case_number` (`case_number`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )

        cur.execute("SHOW COLUMNS FROM `cases`")
        cols = {str(r[0]).strip().lower() for r in (cur.fetchall() or [])}

        def _add(sql: str) -> None:
            try:
                cur.execute(sql)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 484, exc_info=True)

        if "status" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `status` VARCHAR(50) DEFAULT ''")
        if "court_name" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `court_name` VARCHAR(255) DEFAULT ''")
        if "updated_at" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `updated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
        if "laf_case_no" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `laf_case_no` VARCHAR(120) DEFAULT ''")
        if "application_no" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `application_no` VARCHAR(120) DEFAULT ''")
        if "court_case_no" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `court_case_no` VARCHAR(255) DEFAULT ''")

        # Backfill split columns from legacy columns (idempotent; no delete).
        try:
            cur.execute(
                """
                UPDATE `cases`
                   SET `application_no` = COALESCE(NULLIF(`legal_aid_number`, ''), `application_no`)
                 WHERE COALESCE(`application_no`, '') = ''
                   AND COALESCE(`legal_aid_number`, '') <> ''
                """
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 510, exc_info=True)
        try:
            cur.execute(
                """
                UPDATE `cases`
                   SET `laf_case_no` = COALESCE(NULLIF(`application_no`, ''), NULLIF(`legal_aid_number`, ''), `laf_case_no`)
                 WHERE COALESCE(`laf_case_no`, '') = ''
                   AND (COALESCE(`application_no`, '') <> '' OR COALESCE(`legal_aid_number`, '') <> '')
                """
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 521, exc_info=True)
        try:
            cur.execute(
                """
                UPDATE `cases`
                   SET `court_case_no` = COALESCE(NULLIF(`court_case_number`, ''), `court_case_no`)
                 WHERE COALESCE(`court_case_no`, '') = ''
                   AND COALESCE(`court_case_number`, '') <> ''
                """
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 532, exc_info=True)

        # Indexes (best-effort).
        try:
            cur.execute("SHOW INDEX FROM `cases`")
            idx = {(str(r[2]).strip() or "").lower() for r in (cur.fetchall() or [])}
        except Exception:
            idx = set()

        if "idx_court_case_number" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_court_case_number` ON `cases` (`court_case_number`(100))")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 545, exc_info=True)
        if "idx_client_name" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_client_name` ON `cases` (`client_name`)")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 550, exc_info=True)
        if "idx_laf_case_no" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_laf_case_no` ON `cases` (`laf_case_no`(100))")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 555, exc_info=True)
        if "idx_application_no" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_application_no` ON `cases` (`application_no`(100))")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 560, exc_info=True)
        if "idx_court_case_no" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_court_case_no` ON `cases` (`court_case_no`(100))")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 565, exc_info=True)

        conn.commit()
    except Exception as e:
        logger.warning("Local DB schema ensure failed: %s", str(e)[:160])
        try:
            if conn:
                conn.rollback()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 574, exc_info=True)
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 580, exc_info=True)
        try:
            if conn:
                conn.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 585, exc_info=True)


# ---------------------------------------------------------------------------
# Core Commands
# ---------------------------------------------------------------------------
def cmd_download(case_number: str, out_folder: str = "", headless: bool = True,
                 timeout_sec: int = 3600, notify: bool = True,
                 skip_existing: bool = True, transcript_type_filter: str = "T",
                 court_name: str = "", case_type: str = "") -> dict:
    """Download transcripts for a specific case number."""
    if not case_number:
        return {"success": False, "error": "missing case_number"}

    _eventlog(
        "transcript:download:start",
        ok=None,
        payload={"case_number": case_number, "court_name": court_name, "case_type": case_type, "headless": headless},
        tags={"case_number": case_number},
    )

    # Default policy for scheduled/automation contexts:
    # ezlawyer transcript site typically allows login without solving captcha.
    # Keep it disabled unless explicitly overridden by the caller.
    os.environ.setdefault("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0")
    os.environ.setdefault("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0")
    os.environ.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")

    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        out = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_RECORD_USERNAME/PASSWORD in .env"}
        _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
        return out

    try:
        _ensure_local_cases_schema()
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        downloader = mod.CourtRecordDownloader(
            username=creds["username"],
            password=creds["password"],
            db_manager=db,
            download_folder=creds["download_folder"],
            headless=headless,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            # Login
            logger.info("Logging into ezlawyer SSO...")
            login_ok = downloader.login()
            if not login_ok:
                msg = "SSO login failed"
                if os.environ.get("MAGI_TRANSCRIPT_LOGIN_FAIL_QUEUE", "1").strip().lower() in {"1", "true", "yes", "on"}:
                    ticket = _enqueue_manual_review(
                        "download",
                        {"case_number": case_number, "court_name": court_name, "case_type": case_type, "headless": bool(headless)},
                        msg,
                    )
                    notify_msg = f"🧩 筆錄登入失敗，已轉人工佇列（ticket={ticket}）。請檢查帳密/驗證狀態後重試。"
                    _notify(notify_msg, notify)
                    out = {
                        "success": False,
                        "error": msg,
                        "manual_required": True,
                        "manual_reason": "login_failed",
                        "manual_ticket": ticket,
                    }
                    _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
                    return out
                _notify("❌ 筆錄下載失敗：" + msg, notify)
                out = {"success": False, "error": msg}
                _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
                return out

            # Build case object
            case = mod.CourtCase(
                case_number=case_number,
                court_case_number=case_number,
                court_name=(court_name or "").strip(),
                case_type=(case_type or "").strip(),
            )

            # Try to enrich from DB
            if db:
                try:
                    db_cases = downloader.get_cases_from_db()
                    for c in db_cases:
                        if case_number in (c.case_number or "") or case_number in (c.court_case_number or ""):
                            case = c
                            break
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 679, exc_info=True)

            if not (case.court_name or "").strip():
                msg = (
                    "缺少法院資訊，無法執行筆錄下載。"
                    "請改用：download {\"case_number\":\"114年度原易字第000168號\",\"court_name\":\"臺灣臺東地方法院\",\"case_type\":\"刑事\"}"
                )
                out = {"success": False, "error": msg}
                _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
                return out

            # Download
            logger.info("Downloading transcripts for: %s", case_number)
            downloaded_files = downloader.download_record(case) or []
            downloaded_count = len(downloaded_files)

            if downloaded_count == 0:
                msg = "⚠️ 筆錄查詢完成，但目前沒有可下載的新檔案 — " + case_number
                _notify(msg, notify)
                out = {
                    "success": False,
                    "case_number": case_number,
                    "downloaded_count": 0,
                    "files": [],
                    "message": msg,
                }
                _eventlog("transcript:download:done", ok=True, payload={"case_number": case_number, "downloaded_count": 0, "message": msg}, tags={"case_number": case_number})
                return out

            # Archive to case folder
            logger.info("Archiving to case folder...")
            downloader.move_to_case_folder(case, downloaded_files)

            msg = "📥 筆錄下載完成 — " + case_number
            label_parts = []
            if str(getattr(case, "client_name", "") or "").strip():
                label_parts.append(str(getattr(case, "client_name", "")).strip())
            if str(getattr(case, "court_case_number", "") or "").strip():
                label_parts.append(str(getattr(case, "court_case_number", "")).strip())
            elif str(getattr(case, "case_number", "") or "").strip():
                label_parts.append(str(getattr(case, "case_number", "")).strip())
            if label_parts:
                msg = f"📥 筆錄下載完成 — {'｜'.join(label_parts)}（{downloaded_count} 份）"
            _notify(msg, notify)
            out = {
                "success": True,
                "case_number": case_number,
                "court_case_number": str(getattr(case, "court_case_number", "") or "").strip(),
                "client_name": str(getattr(case, "client_name", "") or "").strip(),
                "downloaded_count": downloaded_count,
                "files": [str(f) for f in downloaded_files[:10]],
                "message": msg,
            }
            _eventlog("transcript:download:done", ok=True, payload={"case_number": case_number, "downloaded_count": downloaded_count, "files": [str(f) for f in downloaded_files[:3]]}, tags={"case_number": case_number})
            return out

        finally:
            downloader.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Download failed: %s", error_msg)
        if _looks_like_captcha_error(error_msg):
            ticket = _enqueue_manual_review(
                "download",
                {"case_number": case_number, "court_name": court_name, "case_type": case_type, "headless": bool(headless)},
                error_msg,
            )
            msg = f"🧩 筆錄下載遇到 CAPTCHA，已轉人工佇列（ticket={ticket}）。請完成人工驗證後再重試。"
            _notify(msg, notify)
            out = {
                "success": False,
                "error": error_msg,
                "manual_required": True,
                "manual_reason": "captcha",
                "manual_ticket": ticket,
            }
            _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
            return out
        _notify("❌ 筆錄下載失敗 — " + case_number + ": " + error_msg, notify)
        out = {"success": False, "error": error_msg, "traceback": traceback.format_exc()[-500:]}
        _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
        return out


def cmd_download_all(headless: bool = True, notify: bool = True) -> dict:
    """Download transcripts for all active cases from DB."""
    _eventlog("transcript:download_all:start")
    os.environ.setdefault("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0")
    os.environ.setdefault("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0")
    os.environ.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")
    _ensure_local_cases_schema()
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        out = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_RECORD_USERNAME/PASSWORD in .env"}
        _eventlog("transcript:download_all:done", ok=False, payload=out)
        return out

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        downloader = mod.CourtRecordDownloader(
            username=creds["username"],
            password=creds["password"],
            db_manager=db,
            download_folder=creds["download_folder"],
            headless=headless,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Running download_all (all active cases)...")
            results = downloader.download_all() or {}
            if _payload_contains_captcha(results):
                ticket = _enqueue_manual_review("download_all", {"headless": bool(headless)}, "captcha in download_all results")
                msg = f"🧩 筆錄批次下載遇到 CAPTCHA，已轉人工佇列（ticket={ticket}）。"
                _notify(msg, notify)
                out = {
                    "success": False,
                    "error": "captcha detected",
                    "manual_required": True,
                    "manual_reason": "captcha",
                    "manual_ticket": ticket,
                }
                _eventlog("transcript:download_all:done", ok=False, payload=out)
                return out
            msg, summary = _summarize_download_results(results)
            _notify(msg, notify)
            out = {"success": True, "message": msg}
            out.update(summary)
            _eventlog("transcript:download_all:done", ok=True, payload=out)
            return out
        finally:
            downloader.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Download all failed: %s", error_msg)
        if _looks_like_captcha_error(error_msg):
            ticket = _enqueue_manual_review("download_all", {"headless": bool(headless)}, error_msg)
            msg = f"🧩 筆錄批次下載遇到 CAPTCHA，已轉人工佇列（ticket={ticket}）。"
            _notify(msg, notify)
            out = {
                "success": False,
                "error": error_msg,
                "manual_required": True,
                "manual_reason": "captcha",
                "manual_ticket": ticket,
            }
            _eventlog("transcript:download_all:done", ok=False, payload=out)
            return out
        _notify("❌ 筆錄批次下載失敗: " + error_msg, notify)
        out = {"success": False, "error": error_msg}
        _eventlog("transcript:download_all:done", ok=False, payload=out)
        return out


def cmd_sync(rename: bool = True, headless: bool = True, notify: bool = True) -> dict:
    """Full sync: MD5 scan -> download all -> rename all transcripts."""
    _eventlog("transcript:sync:start", payload={"rename": bool(rename), "headless": bool(headless)})
    os.environ.setdefault("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0")
    os.environ.setdefault("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0")
    os.environ.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")
    _ensure_local_cases_schema()
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        out = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_RECORD_USERNAME/PASSWORD in .env"}
        _eventlog("transcript:sync:done", ok=False, payload=out)
        return out

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        downloader = mod.CourtRecordDownloader(
            username=creds["username"],
            password=creds["password"],
            db_manager=db,
            download_folder=creds["download_folder"],
            headless=headless,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Running full sync (MD5 scan + download + rename)...")
            downloader.scan_case_folders_for_md5(rename_files=False)
            results = downloader.download_all() or {}
            if _payload_contains_captcha(results):
                ticket = _enqueue_manual_review(
                    "sync",
                    {"rename": bool(rename), "headless": bool(headless)},
                    "captcha in sync download results",
                )
                msg = f"🧩 筆錄同步遇到 CAPTCHA，已轉人工佇列（ticket={ticket}）。"
                _notify(msg, notify)
                out = {
                    "success": False,
                    "error": "captcha detected",
                    "manual_required": True,
                    "manual_reason": "captcha",
                    "manual_ticket": ticket,
                }
                _eventlog("transcript:sync:done", ok=False, payload=out)
                return out
            if rename:
                downloader.rename_all_transcripts()

            dl_msg, summary = _summarize_download_results(results)
            suffix = "（含更名）" if rename else ""
            msg = f"🔄 筆錄全同步完成{suffix}\n{dl_msg}"
            _notify(msg, notify)
            out = {"success": True, "message": msg}
            out.update(summary)
            _eventlog("transcript:sync:done", ok=True, payload=out)
            return out
        finally:
            downloader.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Sync failed: %s", error_msg)
        if _looks_like_captcha_error(error_msg):
            ticket = _enqueue_manual_review(
                "sync",
                {"rename": bool(rename), "headless": bool(headless)},
                error_msg,
            )
            msg = f"🧩 筆錄同步遇到 CAPTCHA，已轉人工佇列（ticket={ticket}）。"
            _notify(msg, notify)
            out = {
                "success": False,
                "error": error_msg,
                "manual_required": True,
                "manual_reason": "captcha",
                "manual_ticket": ticket,
            }
            _eventlog("transcript:sync:done", ok=False, payload=out)
            return out
        _notify("❌ 筆錄同步失敗: " + error_msg, notify)
        out = {"success": False, "error": error_msg}
        _eventlog("transcript:sync:done", ok=False, payload=out)
        return out


def cmd_rename(notify: bool = True) -> dict:
    """Rename all downloaded transcripts to standard format."""
    cfg = _load_config()
    creds = _get_credentials(cfg)

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        downloader = mod.CourtRecordDownloader(
            username=creds["username"],
            password=creds["password"],
            db_manager=db,
            download_folder=creds["download_folder"],
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Renaming all transcripts...")
            downloader.rename_all_transcripts()

            msg = "✏️ 筆錄更名完成"
            _notify(msg, notify)
            return {"success": True, "message": msg}
        finally:
            downloader.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Rename failed: %s", error_msg)
        return {"success": False, "error": error_msg}


# ---------------------------------------------------------------------------
# LINE/DC Command Parsing
# ---------------------------------------------------------------------------
def parse_line_command(text: str) -> Optional[dict]:
    """
    Parse LINE/DC messages into skill commands.

    Supported:
        下載筆錄 花蓮 114訴123
        下載筆錄 114訴123
        筆錄下載 114年度訴字第123號
        筆錄同步
    """
    t = (text or "").strip()
    if not t:
        return None

    # Sync triggers
    sync_triggers = ["筆錄同步", "同步筆錄", "筆錄全同步"]
    for trigger in sync_triggers:
        if t.startswith(trigger):
            return {"command": "sync"}

    # Download triggers
    dl_triggers = ["下載筆錄", "筆錄下載", "調閱筆錄", "筆錄調閱"]
    for trigger in dl_triggers:
        if t.startswith(trigger):
            remainder = t[len(trigger):].strip()
            if remainder:
                parsed = _parse_download_args(remainder)
                if parsed:
                    parsed["command"] = "download"
                    return parsed
            return None

    # Rename trigger
    if t in ("筆錄更名", "更名筆錄"):
        return {"command": "rename"}

    return None


def _parse_download_args(text: str) -> Optional[dict]:
    """
    Parse natural language download args.
    Accepted:
      - <案號>
      - <法院> <案號>
    """
    s = (text or "").strip()
    if not s:
        return None
    parts = s.split()
    if len(parts) == 1:
        return {"case_number": parts[0]}
    if len(parts) >= 2:
        return {"court_name": parts[0], "case_number": parts[1]}
    return None


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
    _maybe_reexec_venv()
    ap = argparse.ArgumentParser(description="transcript-downloader skill")
    ap.add_argument("--task", default="help", help="task text")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "summary", "list"}:
        return _ok({
            "success": True,
            "product_profile": product_profile_report("transcript"),
            "commands": [
                "help",
                "self_test",
                "db_probe",
                'download {"case_number":"..."}',
                'download {"case_number":"...","court_name":"臺灣臺東地方法院","case_type":"刑事"}',
                "download_all",
                "sync",
                "rename",
            ],
            "line_triggers": [
                "下載筆錄 <法院> <案號>",
                "下載筆錄 <案號>",
                "筆錄同步",
                "筆錄更名",
            ],
        })

    if task == "self_test":
        # Verify imports and config without actually logging in
        errors = []
        try:
            _ensure_imports()
        except Exception as e:
            errors.append("import judicial_automation_v2 failed: " + str(e)[:100])

        cfg = _load_config()
        creds = _get_credentials(cfg)
        if not creds["username"]:
            errors.append("missing judicial.record_username in config.json")
        if not creds["password"]:
            errors.append("missing judicial.record_password in config.json")

        ok = len(errors) == 0
        return _ok({"success": ok, "errors": errors if errors else None,
                     "credentials_found": bool(creds["username"]),
                     "product_profile": product_profile_report("transcript", config=cfg)})

    if task == "db_probe":
        # Verify DB connectivity and whether we have eligible cases (no website login).
        try:
            _ensure_local_cases_schema()
            mod = _ensure_imports()
            cfg = _load_config()
            db = _get_db_manager(cfg)
            if not db:
                return _ok({"success": False, "error": "db_manager not available"})
            downloader = mod.CourtRecordDownloader(
                username="",
                password="",
                db_manager=db,
                download_folder=_get_credentials(cfg).get("download_folder", DEFAULT_DOWNLOAD_FOLDER),
                headless=True,
                log_callback=lambda msg: None,
            )
            try:
                cases = downloader.get_cases_from_db() or []
            finally:
                try:
                    downloader.close()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1105, exc_info=True)
            sample = []
            for c in cases[:10]:
                sample.append({
                    "case_number": getattr(c, "case_number", ""),
                    "court_name": getattr(c, "court_name", ""),
                    "court_case_number": getattr(c, "court_case_number", ""),
                    "case_type": getattr(c, "case_type", ""),
                })
            return _ok({"success": True, "eligible_cases": len(cases), "sample": sample})
        except Exception as e:
            return _ok({"success": False, "error": str(e)[:200]})

    if task.startswith("download_all"):
        r = cmd_download_all()
        return _ok(r)

    if task.startswith("download"):
        payload = _load_jsonish(task[len("download"):].strip())
        cn = payload.get("case_number", "")
        r = cmd_download(
            case_number=payload.get("case_number", ""),
            out_folder=payload.get("out_folder", ""),
            headless=bool(payload.get("headless", True)),
            timeout_sec=int(payload.get("timeout_sec", 3600)),
            notify=bool(payload.get("notify", True)),
            skip_existing=bool(payload.get("skip_existing", True)),
            transcript_type_filter=payload.get("transcript_type_filter", "T"),
            court_name=payload.get("court_name", ""),
            case_type=payload.get("case_type", ""),
        )
        return _ok(r)

    if task in ("sync", "筆錄同步", "全同步"):
        r = cmd_sync()
        return _ok(r)

    if task in ("rename", "筆錄更名"):
        r = cmd_rename()
        return _ok(r)

    # Try as LINE command
    parsed = parse_line_command(task)
    if parsed:
        cmd = parsed["command"]
        if cmd == "sync":
            r = cmd_sync()
            return _ok(r)
        if cmd == "download":
            r = cmd_download(
                case_number=parsed.get("case_number", ""),
                court_name=parsed.get("court_name", ""),
                case_type=parsed.get("case_type", ""),
            )
            return _ok(r)
        if cmd == "rename":
            r = cmd_rename()
            return _ok(r)

    return _ok({"success": False, "error": "unknown task: " + task})


if __name__ == "__main__":
    raise SystemExit(main())
