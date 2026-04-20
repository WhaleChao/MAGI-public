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
from typing import Any, Dict, Optional, Tuple
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
from api.openclaw_compat import get_legacy_telegram_settings, load_openclaw_config
from api.product_runtime import apply_product_runtime_env, product_profile_report
try:
    from skills.ops import flow_ledger as _flow_ledger
except ImportError:
    _flow_ledger = None

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


def _flow_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._") or "task"


def _safe_create_flow_mirror(task_name: str, *, metadata: Optional[Dict[str, Any]] = None) -> str:
    if str(task_name or "").strip() not in {"download", "download_all", "sync"}:
        return ""
    payload = dict(metadata or {})
    run_bits = [datetime.now().strftime("%Y%m%d_%H%M%S"), _flow_slug(task_name)]
    case_hint = str(payload.get("case_number") or "").strip()
    if case_hint:
        run_bits.append(_flow_slug(case_hint)[:48])
    try:
        flow = _flow_ledger.create_flow(
            parent_job_id=os.environ.get("MAGI_TRANSCRIPT_FLOW_PARENT_JOB_ID", "skill_transcript_downloader"),
            run_id="_".join(bit for bit in run_bits if bit),
            task=task_name,
            metadata={**payload, "source": "transcript-downloader"},
        )
        return str(flow.get("flow_id") or "")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 78, exc_info=True)
        return ""


def _safe_flow_step_status(
    flow_id: str,
    step_name: str,
    *,
    status: str,
    detail: str = "",
    ok: Optional[bool] = None,
    skipped: Optional[bool] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    if not flow_id:
        return
    try:
        _flow_ledger.set_step_status(
            flow_id,
            step_name,
            status=status,
            detail=detail,
            ok=ok,
            skipped=skipped,
            metadata=metadata,
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 105, exc_info=True)


def _safe_finalize_flow(flow_id: str, result: Dict[str, Any]) -> None:
    if not flow_id or not isinstance(result, dict):
        return
    try:
        if bool(result.get("cancelled")) or str(result.get("status") or "").strip().lower() == "cancelled":
            flow_status = "cancelled"
        elif bool(result.get("manual_required")):
            flow_status = "blocked"
        elif bool(result.get("success")):
            flow_status = "succeeded"
        else:
            flow_status = "failed"
        artifacts: Dict[str, str] = {}
        for idx, path_value in enumerate(result.get("files") or []):
            if idx >= 3:
                break
            if path_value:
                artifacts[f"file_{idx + 1}"] = str(path_value)
        _flow_ledger.finalize_flow(
            flow_id,
            status=flow_status,
            ok=bool(result.get("success")),
            summary=str(result.get("message") or result.get("error") or result.get("status") or "").strip()[:300],
            blockers=[
                item
                for item in (
                    ("cancel_requested" if bool(result.get("cancelled")) else ""),
                    (str(result.get("manual_reason") or "").strip() if bool(result.get("manual_required")) else ""),
                )
                if item
            ],
            metadata={
                "status": str(result.get("status") or "").strip(),
                "noop": bool(result.get("noop")),
                "downloaded_count": int(result.get("downloaded_count") or 0),
                "manual_required": bool(result.get("manual_required")),
                "cancelled": bool(result.get("cancelled")),
            },
            artifacts=artifacts or None,
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 138, exc_info=True)


def _mark_notify_step(flow_id: str, *, notify: bool, detail: str) -> None:
    _safe_flow_step_status(
        flow_id,
        "notify",
        status="succeeded" if notify else "skipped",
        ok=bool(notify),
        skipped=not notify,
        detail=detail[:240],
    )


def _cancel_reason(flow_id: str) -> str:
    if not flow_id:
        return ""
    try:
        return _flow_ledger.get_cancel_reason(flow_id)
    except Exception:
        return ""


def _cancelled_result(flow_id: str, step_name: str, *, case_number: str = "") -> Dict[str, Any]:
    reason = _cancel_reason(flow_id) or "operator requested"
    detail = f"cancel_requested: {reason}"[:240]
    _safe_flow_step_status(
        flow_id,
        step_name,
        status="cancelled",
        detail=detail,
        ok=False,
        metadata={"cancel_requested": True},
    )
    payload = {
        "success": False,
        "cancelled": True,
        "status": "cancelled",
        "error": detail,
        "message": "⏹️ 筆錄任務已取消",
    }
    if case_number:
        payload["case_number"] = case_number
    return payload


def _check_flow_cancelled(flow_id: str, step_name: str, *, case_number: str = "") -> Optional[Dict[str, Any]]:
    if not flow_id:
        return None
    try:
        if _flow_ledger.is_cancel_requested(flow_id):
            return _cancelled_result(flow_id, step_name, case_number=case_number)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 190, exc_info=True)
    return None


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
def _notify(text: str, flag: bool = True, topic_key: str = "transcript"):
    if not flag:
        return

    # Prefer centralized notifier with outbox/retry semantics.
    try:
        from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore

        st = send_telegram_push_with_status(
            str(text or ""),
            severity="info",
            source="transcript_downloader",
            topic_key=topic_key,
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
                legacy = get_legacy_telegram_settings(load_openclaw_config())
                if not token:
                    token = str(legacy.get("bot_token") or "").strip()
                targets.extend([str(x).strip() for x in (legacy.get("notify_to") or []) if str(x).strip()])
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


def _summarize_download_results(results: dict, *, max_cases: int = 20) -> Tuple[str, dict]:
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
        try:
            from legalbridge_core import LegalBridgeDB
            return LegalBridgeDB()
        except Exception:
            # Newer MAGI trees expose DatabaseManager/ConfigManager instead of
            # the older LegalBridgeDB facade.
            from legalbridge_core import ConfigManager as LegacyConfigManager
            from legalbridge_core import DatabaseManager as LegacyDatabaseManager

            legacy_cfg = LegacyConfigManager()
            if isinstance(cfg, dict) and cfg:
                legacy_cfg.config = dict(cfg)
            return LegacyDatabaseManager(legacy_cfg)
    except Exception as legacy_err:
        try:
            import importlib.util

            osc_compat_path = os.path.join(MAGI_ROOT, "osc.py")
            if os.path.isfile(osc_compat_path):
                spec = importlib.util.spec_from_file_location("magi_osc_compat", osc_compat_path)
                if spec and spec.loader:
                    osc_compat = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(osc_compat)
                    OscDatabaseManager = getattr(osc_compat, "DatabaseManager", None)
                else:
                    OscDatabaseManager = None
            else:
                OscDatabaseManager = None

            if OscDatabaseManager is None:
                from osc import DatabaseManager as OscDatabaseManager

            profiles = cfg.get("mariadb_profiles") or []
            for want in ("Home_Local_Test", "Studio_Local", "Studio_VPN_Remote"):
                for profile in profiles:
                    if str(profile.get("profile_name") or "").strip() != want:
                        continue
                    conf = profile.get("config") or {}
                    if conf.get("host") and conf.get("user") and conf.get("database"):
                        return OscDatabaseManager(conf)

            return OscDatabaseManager(
                {
                    "host": os.environ.get("OSC_DB_HOST", "127.0.0.1"),
                    "port": int(os.environ.get("OSC_DB_PORT", "3307") or "3307"),
                    "user": os.environ.get("OSC_DB_USER", "python_user"),
                    "password": os.environ.get("OSC_DB_PASSWORD", ""),
                    "database": os.environ.get("OSC_DB_NAME", "law_firm_data"),
                }
            )
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

    # Env overrides for DB connection
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
                 court_name: str = "", case_type: str = "",
                 flow_id: str = "") -> dict:
    """Download transcripts for a specific case number."""
    if not case_number:
        _safe_flow_step_status(flow_id, "case_lookup", status="failed", detail="missing case_number", ok=False)
        return {"success": False, "error": "missing case_number"}

    # Resolve short court alias (e.g. "花蓮", "TPD") to full name
    if court_name:
        court_name = _resolve_court_name(court_name)

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
        _safe_flow_step_status(flow_id, "portal_login", status="failed", detail="missing credentials", ok=False)
        out = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_RECORD_USERNAME/PASSWORD in .env"}
        _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
        return out

    cancelled = _check_flow_cancelled(flow_id, "portal_login", case_number=case_number)
    if cancelled:
        _eventlog("transcript:download:done", ok=False, payload=cancelled, tags={"case_number": case_number})
        return cancelled

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
            _safe_flow_step_status(flow_id, "portal_login", status="running", detail=f"login {case_number}")
            logger.info("Logging into ezlawyer SSO...")
            login_ok = downloader.login()
            if not login_ok:
                _safe_flow_step_status(flow_id, "portal_login", status="failed", detail="SSO login failed", ok=False)
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
                _mark_notify_step(flow_id, notify=notify, detail=msg)
                out = {"success": False, "error": msg}
                _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
                return out
            _safe_flow_step_status(flow_id, "portal_login", status="succeeded", detail="SSO login ok", ok=True)

            cancelled = _check_flow_cancelled(flow_id, "portal_query", case_number=case_number)
            if cancelled:
                _eventlog("transcript:download:done", ok=False, payload=cancelled, tags={"case_number": case_number})
                return cancelled

            # Build case object
            _safe_flow_step_status(flow_id, "case_lookup", status="running", detail=case_number)
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
                _safe_flow_step_status(flow_id, "case_lookup", status="failed", detail="missing court_name", ok=False)
                msg = (
                    "缺少法院資訊，無法執行筆錄下載。"
                    "請改用：download {\"case_number\":\"114年度原易字第000168號\",\"court_name\":\"臺灣臺東地方法院\",\"case_type\":\"刑事\"}"
                )
                out = {"success": False, "error": msg}
                _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
                return out
            _safe_flow_step_status(
                flow_id,
                "case_lookup",
                status="succeeded",
                detail=str(getattr(case, "court_case_number", "") or case_number),
                ok=True,
                metadata={"court_name": str(getattr(case, "court_name", "") or "").strip()},
            )

            # Download
            _safe_flow_step_status(flow_id, "portal_query", status="running", detail=str(getattr(case, "court_name", "") or case_number))
            logger.info("Downloading transcripts for: %s", case_number)
            downloaded_files = downloader.download_record(case) or []
            downloaded_count = len(downloaded_files)
            _safe_flow_step_status(
                flow_id,
                "portal_query",
                status="succeeded",
                detail=f"portal query complete ({downloaded_count} new files)",
                ok=True,
                metadata={"downloaded_count": downloaded_count},
            )

            if downloaded_count == 0:
                msg = "⚠️ 筆錄查詢完成，但目前沒有可下載的新檔案 — " + case_number
                _notify(msg, notify)
                _safe_flow_step_status(flow_id, "dedup", status="succeeded", detail="no new files after dedup", ok=True, metadata={"downloaded_count": 0, "noop": True})
                _safe_flow_step_status(flow_id, "archive", status="skipped", detail="no new files to archive", skipped=True, ok=True)
                _mark_notify_step(flow_id, notify=notify, detail=msg)
                out = {
                    "success": True,
                    "status": "no_new_files",
                    "noop": True,
                    "case_number": case_number,
                    "downloaded_count": 0,
                    "files": [],
                    "message": msg,
                }
                _eventlog("transcript:download:done", ok=True, payload={"case_number": case_number, "downloaded_count": 0, "message": msg}, tags={"case_number": case_number})
                return out

            # Archive to case folder
            logger.info("Archiving to case folder...")
            _safe_flow_step_status(flow_id, "dedup", status="succeeded", detail=f"{downloaded_count} new files", ok=True, metadata={"downloaded_count": downloaded_count})
            _safe_flow_step_status(flow_id, "archive", status="running", detail=f"archive {downloaded_count} files")
            downloader.move_to_case_folder(case, downloaded_files)
            _safe_flow_step_status(flow_id, "archive", status="succeeded", detail=f"archived {downloaded_count} files", ok=True)

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
            _mark_notify_step(flow_id, notify=notify, detail=msg)
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
        _safe_flow_step_status(flow_id, "portal_query", status="failed", detail=error_msg, ok=False)
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
        _mark_notify_step(flow_id, notify=notify, detail=error_msg)
        out = {"success": False, "error": error_msg, "traceback": traceback.format_exc()[-500:]}
        _eventlog("transcript:download:done", ok=False, payload=out, tags={"case_number": case_number})
        return out


def cmd_download_all(headless: bool = True, notify: bool = True, flow_id: str = "") -> dict:
    """Download transcripts for all active cases from DB."""
    _eventlog("transcript:download_all:start")
    os.environ.setdefault("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0")
    os.environ.setdefault("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0")
    os.environ.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")
    _ensure_local_cases_schema()
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        _safe_flow_step_status(flow_id, "portal_query", status="failed", detail="missing credentials", ok=False)
        out = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_RECORD_USERNAME/PASSWORD in .env"}
        _eventlog("transcript:download_all:done", ok=False, payload=out)
        return out

    cancelled = _check_flow_cancelled(flow_id, "portal_query")
    if cancelled:
        _eventlog("transcript:download_all:done", ok=False, payload=cancelled)
        return cancelled

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
            _safe_flow_step_status(flow_id, "portal_query", status="running", detail="download_all")
            results = downloader.download_all() or {}
            if _payload_contains_captcha(results):
                _safe_flow_step_status(flow_id, "portal_query", status="failed", detail="captcha detected", ok=False)
                ticket = _enqueue_manual_review("download_all", {"headless": bool(headless)}, "captcha in download_all results")
                msg = f"🧩 筆錄批次下載遇到 CAPTCHA，已轉人工佇列（ticket={ticket}）。"
                _notify(msg, notify)
                _mark_notify_step(flow_id, notify=notify, detail=msg)
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
            _safe_flow_step_status(
                flow_id,
                "portal_query",
                status="succeeded",
                detail=f"download_all complete ({int(summary.get('downloaded_count') or 0)} files)",
                ok=True,
                metadata=summary,
            )
            _safe_flow_step_status(
                flow_id,
                "dedup",
                status="succeeded",
                detail=f"new_files={int(summary.get('downloaded_count') or 0)}",
                ok=True,
                metadata=summary,
            )
            _safe_flow_step_status(
                flow_id,
                "archive",
                status="succeeded" if int(summary.get("downloaded_count") or 0) > 0 else "skipped",
                detail="download_all handles archive internally",
                ok=True,
                skipped=int(summary.get("downloaded_count") or 0) <= 0,
            )
            _notify(msg, notify)
            _mark_notify_step(flow_id, notify=notify, detail=msg)
            out = {"success": True, "message": msg}
            out.update(summary)
            _eventlog("transcript:download_all:done", ok=True, payload=out)
            return out
        finally:
            downloader.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Download all failed: %s", error_msg)
        _safe_flow_step_status(flow_id, "portal_query", status="failed", detail=error_msg, ok=False)
        if _looks_like_captcha_error(error_msg):
            ticket = _enqueue_manual_review("download_all", {"headless": bool(headless)}, error_msg)
            msg = f"🧩 筆錄批次下載遇到 CAPTCHA，已轉人工佇列（ticket={ticket}）。"
            _notify(msg, notify)
            _mark_notify_step(flow_id, notify=notify, detail=msg)
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
        _mark_notify_step(flow_id, notify=notify, detail=error_msg)
        out = {"success": False, "error": error_msg}
        _eventlog("transcript:download_all:done", ok=False, payload=out)
        return out


def cmd_sync(rename: bool = True, headless: bool = True, notify: bool = True, flow_id: str = "") -> dict:
    """Full sync: MD5 scan -> download all -> rename all transcripts."""
    _eventlog("transcript:sync:start", payload={"rename": bool(rename), "headless": bool(headless)})
    os.environ.setdefault("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0")
    os.environ.setdefault("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0")
    os.environ.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")
    _ensure_local_cases_schema()
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        _safe_flow_step_status(flow_id, "portal_query", status="failed", detail="missing credentials", ok=False)
        out = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_RECORD_USERNAME/PASSWORD in .env"}
        _eventlog("transcript:sync:done", ok=False, payload=out)
        return out

    cancelled = _check_flow_cancelled(flow_id, "case_scan")
    if cancelled:
        _eventlog("transcript:sync:done", ok=False, payload=cancelled)
        return cancelled

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
            _safe_flow_step_status(flow_id, "case_scan", status="running", detail="scan_case_folders_for_md5")
            downloader.scan_case_folders_for_md5(rename_files=False)
            _safe_flow_step_status(flow_id, "case_scan", status="succeeded", detail="case folder scan complete", ok=True)

            cancelled = _check_flow_cancelled(flow_id, "portal_query")
            if cancelled:
                _eventlog("transcript:sync:done", ok=False, payload=cancelled)
                return cancelled

            _safe_flow_step_status(flow_id, "portal_query", status="running", detail="sync download_all")
            results = downloader.download_all() or {}
            if _payload_contains_captcha(results):
                _safe_flow_step_status(flow_id, "portal_query", status="failed", detail="captcha detected", ok=False)
                ticket = _enqueue_manual_review(
                    "sync",
                    {"rename": bool(rename), "headless": bool(headless)},
                    "captcha in sync download results",
                )
                msg = f"🧩 筆錄同步遇到 CAPTCHA，已轉人工佇列（ticket={ticket}）。"
                _notify(msg, notify)
                _mark_notify_step(flow_id, notify=notify, detail=msg)
                out = {
                    "success": False,
                    "error": "captcha detected",
                    "manual_required": True,
                    "manual_reason": "captcha",
                    "manual_ticket": ticket,
                }
                _eventlog("transcript:sync:done", ok=False, payload=out)
                return out
            dl_msg, summary = _summarize_download_results(results)
            _safe_flow_step_status(
                flow_id,
                "portal_query",
                status="succeeded",
                detail=f"sync download complete ({int(summary.get('downloaded_count') or 0)} files)",
                ok=True,
                metadata=summary,
            )
            _safe_flow_step_status(
                flow_id,
                "dedup",
                status="succeeded",
                detail=f"new_files={int(summary.get('downloaded_count') or 0)}",
                ok=True,
                metadata=summary,
            )
            if rename:
                _safe_flow_step_status(flow_id, "rename", status="running", detail="rename_all_transcripts")
                downloader.rename_all_transcripts()
                _safe_flow_step_status(flow_id, "rename", status="succeeded", detail="rename complete", ok=True)
            else:
                _safe_flow_step_status(flow_id, "rename", status="skipped", detail="rename disabled", ok=True, skipped=True)

            suffix = "（含更名）" if rename else ""
            msg = f"🔄 筆錄全同步完成{suffix}\n{dl_msg}"
            _notify(msg, notify, topic_key="transcript" if int(summary.get("downloaded_count") or 0) > 0 else "quiet_cron")
            _mark_notify_step(flow_id, notify=notify, detail=msg)
            out = {"success": True, "message": msg}
            out.update(summary)
            _eventlog("transcript:sync:done", ok=True, payload=out)
            return out
        finally:
            downloader.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Sync failed: %s", error_msg)
        _safe_flow_step_status(flow_id, "portal_query", status="failed", detail=error_msg, ok=False)
        if _looks_like_captcha_error(error_msg):
            ticket = _enqueue_manual_review(
                "sync",
                {"rename": bool(rename), "headless": bool(headless)},
                error_msg,
            )
            msg = f"🧩 筆錄同步遇到 CAPTCHA，已轉人工佇列（ticket={ticket}）。"
            _notify(msg, notify)
            _mark_notify_step(flow_id, notify=notify, detail=msg)
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
        _mark_notify_step(flow_id, notify=notify, detail=error_msg)
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


_COURT_ALIAS_TO_FULL: Dict[str, str] = {
    "基隆": "臺灣基隆地方法院", "臺北": "臺灣臺北地方法院", "台北": "臺灣臺北地方法院",
    "新北": "臺灣新北地方法院", "桃園": "臺灣桃園地方法院", "新竹": "臺灣新竹地方法院",
    "苗栗": "臺灣苗栗地方法院", "臺中": "臺灣臺中地方法院", "台中": "臺灣臺中地方法院",
    "彰化": "臺灣彰化地方法院", "南投": "臺灣南投地方法院", "雲林": "臺灣雲林地方法院",
    "嘉義": "臺灣嘉義地方法院", "臺南": "臺灣臺南地方法院", "台南": "臺灣臺南地方法院",
    "高雄": "臺灣高雄地方法院", "屏東": "臺灣屏東地方法院", "花蓮": "臺灣花蓮地方法院",
    "臺東": "臺灣臺東地方法院", "台東": "臺灣臺東地方法院", "宜蘭": "臺灣宜蘭地方法院",
    "澎湖": "臺灣澎湖地方法院", "金門": "福建金門地方法院", "連江": "福建連江地方法院",
    "士林": "臺灣士林地方法院", "橋頭": "臺灣橋頭地方法院",
    # 高等法院
    "高等法院": "臺灣高等法院", "臺灣高等法院": "臺灣高等法院",
    "高雄高分院": "臺灣高等法院高雄分院", "臺中高分院": "臺灣高等法院臺中分院",
    "臺南高分院": "臺灣高等法院臺南分院", "花蓮高分院": "臺灣高等法院花蓮分院",
    # Codes
    "TPD": "臺灣臺北地方法院", "PCD": "臺灣新北地方法院", "SLD": "臺灣士林地方法院",
    "TYD": "臺灣桃園地方法院", "SCD": "臺灣新竹地方法院", "MLD": "臺灣苗栗地方法院",
    "TCD": "臺灣臺中地方法院", "CHD": "臺灣彰化地方法院", "NTD": "臺灣南投地方法院",
    "ULD": "臺灣雲林地方法院", "CYD": "臺灣嘉義地方法院", "TND": "臺灣臺南地方法院",
    "KSD": "臺灣高雄地方法院", "PTD": "臺灣屏東地方法院", "HLD": "臺灣花蓮地方法院",
    "TTD": "臺灣臺東地方法院", "ILD": "臺灣宜蘭地方法院", "KLD": "臺灣基隆地方法院",
    "PHD": "臺灣澎湖地方法院", "KMD": "福建金門地方法院", "LCD": "福建連江地方法院",
    "CTD": "臺灣橋頭地方法院",
}


def _resolve_court_name(text: str) -> str:
    """Resolve a short court alias or code to the full official court name."""
    text = text.strip()
    if not text:
        return text
    # Already a full court name (contains "法院")
    if "法院" in text:
        return text.replace("台", "臺")
    # Lookup by alias / code
    resolved = _COURT_ALIAS_TO_FULL.get(text)
    if resolved:
        return resolved
    normalized = text.replace("台", "臺")
    resolved = _COURT_ALIAS_TO_FULL.get(normalized)
    if resolved:
        return resolved
    up = text.upper()
    resolved = _COURT_ALIAS_TO_FULL.get(up)
    if resolved:
        return resolved
    return text


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
        court_name = _resolve_court_name(parts[0])
        return {"court_name": court_name, "case_number": parts[1]}
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
        # Verify imports, config, DB, and ezlawyer site reachability (no login)
        import urllib.request as _urllib_req
        errors = []
        warnings = []
        checks = {}
        try:
            _ensure_imports()
            checks["import"] = True
        except Exception as e:
            errors.append("import judicial_automation_v2 failed: " + str(e)[:100])
            checks["import"] = False

        cfg = _load_config()
        creds = _get_credentials(cfg)
        if not creds["username"]:
            errors.append("missing judicial.record_username in config.json")
        if not creds["password"]:
            errors.append("missing judicial.record_password in config.json")
        checks["credentials"] = bool(creds["username"] and creds["password"])

        # DB probe (non-blocking)
        try:
            _ensure_local_cases_schema()
            db = _get_db_manager(cfg)
            checks["db"] = db is not None
            if not db:
                warnings.append("db_manager unavailable; transcript dedup will use JSON fallback")
        except Exception as e:
            warnings.append("db probe failed: " + str(e)[:80])
            checks["db"] = False

        # ezlawyer site reachability (HEAD, no login)
        try:
            _req = _urllib_req.Request(
                "https://www.ezlawyer.com.tw/eb/user/loginPage",
                method="HEAD",
            )
            _req.add_header("User-Agent", "MAGI-self-test/1.0")
            with _urllib_req.urlopen(_req, timeout=10) as _resp:
                checks["site_reachable"] = _resp.status < 500
        except Exception as e:
            warnings.append("ezlawyer site unreachable: " + str(e)[:80])
            checks["site_reachable"] = False

        ok = len(errors) == 0
        return _ok({
            "success": ok,
            "checks": checks,
            "errors": errors if errors else None,
            "warnings": warnings if warnings else None,
            "credentials_found": bool(creds["username"]),
            "product_profile": product_profile_report("transcript", config=cfg),
        })

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
        flow_id = _safe_create_flow_mirror("download_all")
        r = cmd_download_all(flow_id=flow_id)
        _safe_finalize_flow(flow_id, r)
        return _ok(r)

    if task.startswith("download"):
        payload = _load_jsonish(task[len("download"):].strip())
        flow_id = _safe_create_flow_mirror(
            "download",
            metadata={
                "case_number": payload.get("case_number", ""),
                "court_name": payload.get("court_name", ""),
                "case_type": payload.get("case_type", ""),
            },
        )
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
            flow_id=flow_id,
        )
        _safe_finalize_flow(flow_id, r)
        return _ok(r)

    if task in ("sync", "筆錄同步", "全同步"):
        flow_id = _safe_create_flow_mirror("sync", metadata={"rename": True})
        r = cmd_sync(flow_id=flow_id)
        _safe_finalize_flow(flow_id, r)
        return _ok(r)

    if task in ("rename", "筆錄更名"):
        r = cmd_rename()
        return _ok(r)

    # Try as LINE command
    parsed = parse_line_command(task)
    if parsed:
        cmd = parsed["command"]
        if cmd == "sync":
            flow_id = _safe_create_flow_mirror("sync", metadata={"source": "line_command"})
            r = cmd_sync(flow_id=flow_id)
            _safe_finalize_flow(flow_id, r)
            return _ok(r)
        if cmd == "download":
            flow_id = _safe_create_flow_mirror(
                "download",
                metadata={
                    "source": "line_command",
                    "case_number": parsed.get("case_number", ""),
                    "court_name": parsed.get("court_name", ""),
                    "case_type": parsed.get("case_type", ""),
                },
            )
            r = cmd_download(
                case_number=parsed.get("case_number", ""),
                court_name=parsed.get("court_name", ""),
                case_type=parsed.get("case_type", ""),
                flow_id=flow_id,
            )
            _safe_finalize_flow(flow_id, r)
            return _ok(r)
        if cmd == "rename":
            r = cmd_rename()
            return _ok(r)

    return _ok({"success": False, "error": "unknown task: " + task})


if __name__ == "__main__":
    raise SystemExit(main())
