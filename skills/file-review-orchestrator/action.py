#!/usr/bin/env python3
"""
file-review-orchestrator -- 閱卷系統協調器
=============================================
包裝 file_review_automation.FileReviewManager，
提供 CASPER skill API 與 LINE/DC 指令介面。

Usage (CLI):
    python action.py --task 'apply {"court_code":"TPD","year":"114","case_type":"訴","case_number":"123"}'
    python action.py --task 'scheduled_check'
    python action.py --task 'download'
    python action.py --task 'check_emails'
    python action.py --task 'help'
"""
import argparse
import os
import sys

# A cron job executes this file by its absolute path.  In that mode Python
# places the skill directory, not the sealed release root, at sys.path[0].
# Bootstrap the release root before importing any MAGI package so the same
# command works without an ambient PYTHONPATH.
_magi_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _magi_root not in sys.path:
    sys.path.insert(0, _magi_root)

from magi_v3 import fcntl_compat as fcntl
from magi_v3.file_review_receipts import (
    PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
    normalize_signature_hashes,
    portal_download_snapshot,
    portal_observed_epoch,
    portal_snapshot_fingerprint,
    signature_set_hash,
)
import glob
import hashlib
import json
import logging
import re
import shutil
import stat
import threading
import traceback
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
import subprocess
import time
import uuid

# Ensure .env is loaded (critical when run as subprocess)
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
    get_file_review_pending_path,
    get_json_dir,
    get_module_path,
    get_orch_dir,
    get_payment_proof_registry_path,
    get_payment_proof_upload_queue_path,
    get_payment_proof_upload_store_dir,
    get_payment_registry_path,
    get_skill_python,
)
try:
    from api.openclaw_compat import get_legacy_telegram_settings, load_openclaw_config
except ImportError:
    pass
from api.case_path_mapper import translate_case_path_to_local
from api.case_display import display_client_name as _canonical_display_client_name
from api.product_runtime import apply_product_runtime_env, product_profile_report
from scripts.ops.background_task_locks import (
    FILE_REVIEW_PORTAL_LOCK_NAME,
    acquire_lock,
    file_review_portal_lock_path,
)
try:
    from skills.ops import flow_ledger as _flow_ledger
except ImportError:
    _flow_ledger = None

ORCH_DIR = str(get_orch_dir())
FILE_REVIEW_RUNTIME = apply_product_runtime_env("file_review", env=os.environ)

# ---------------------------------------------------------------------------
# Prefer project venv (avoids PEP 668 / Homebrew "externally-managed" pip issues)
# ---------------------------------------------------------------------------
_VENV_PY = str(get_skill_python())
try:
    _target_prefix = os.path.realpath(str(Path(_VENV_PY).expanduser().parent.parent))
    _current_prefix = os.path.realpath(sys.prefix)
    if (
        __name__ == "__main__"
        and os.environ.get("MAGI_DISABLE_SKILL_VENV_REEXEC") != "1"
        and os.path.exists(_VENV_PY)
        and _current_prefix != _target_prefix
    ):
        os.execv(_VENV_PY, [_VENV_PY, __file__, *sys.argv[1:]])
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 79, exc_info=True)

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------
CODE_DIR = ORCH_DIR
CONFIG_PATH = str(get_config_path("config.json"))

_FILE_REVIEW_STATE_OVERRIDE = os.environ.get("MAGI_FILE_REVIEW_STATE_DIR", "").strip()
_RUNTIME_OVERRIDE = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
_FILE_REVIEW_STATE_DIR = (
    os.path.abspath(os.path.expanduser(_FILE_REVIEW_STATE_OVERRIDE))
    if _FILE_REVIEW_STATE_OVERRIDE
    else (
        os.path.join(os.path.abspath(os.path.expanduser(_RUNTIME_OVERRIDE)), "file-review")
        if _RUNTIME_OVERRIDE
        else ""
    )
)


def _default_download_folder() -> str:
    if _FILE_REVIEW_STATE_DIR:
        return os.path.join(_FILE_REVIEW_STATE_DIR, "downloads")
    root = os.environ.get("MAGI_ROOT_DIR", _magi_root).strip() or _magi_root
    return os.path.join(os.path.abspath(os.path.expanduser(root)), "閱卷下載")


DEFAULT_DOWNLOAD_FOLDER = _default_download_folder()
JSON_DIR = str(get_json_dir())
BG_JOB_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("MAGI_FILE_REVIEW_BG_JOB_DIR", "").strip())
    or (
        os.path.join(_FILE_REVIEW_STATE_DIR, "bg-jobs")
        if _FILE_REVIEW_STATE_DIR
        else os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bg_jobs")
    )
)
RECENT_ACTIVITY_STATE_FILE = ".recent_activity_notified.json"
DOWNLOAD_NOTICE_LEDGER_SCHEMA = "magi.v3.file-review-download-notice-ledger/v1"
DOWNLOAD_NOTICE_LEDGER_FILE = ".download_notification_receipts.json"

# Resolve the mutable queue only when a queue operation is requested.  A sealed
# release must still fail closed at that point, but importing read-only portal
# checks must not require a write-path binding.  ``None`` also keeps the
# explicit test/runtime override surface backwards compatible.
PAYMENT_PROOF_UPLOAD_QUEUE_PATH: Path | None = None
PAYMENT_PROOF_UPLOAD_STORE_DIR = get_payment_proof_upload_store_dir()

# Safety-first defaults: never auto-route uncertain cases.
os.environ.setdefault("MAGI_ALLOW_RISKY_CASE_SCAN", "0")
os.environ.setdefault("MAGI_ALLOW_FILENAME_HEURISTIC_ARCHIVE", "1")
os.environ.setdefault("MAGI_REQUIRE_CASE_SIGNAL_FOR_AUTO", "1")
os.environ.setdefault("MAGI_ALLOW_LOOSE_CASE_FOLDER_FALLBACK", "0")
os.environ.setdefault("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "0")
# A successfully verified portal row must not be clicked every few minutes.
# The manager binds this registry to the portal row signature and expires it,
# so a changed row is retried immediately and an unchanged row is rechecked
# after the bounded TTL instead of being hidden forever.
os.environ.setdefault("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP", "1")
os.environ.setdefault("MAGI_ENABLE_PRECLICK_SMART_SKIP", "1")
# Court rows that retain the same stable portal signature are overwhelmingly
# immutable.  A daily blind re-click caused the same court bundle to be
# regenerated and announced on consecutive evenings.  Keep a bounded monthly
# audit for portals that fail to update their row signature; any real signature
# change still invalidates the receipt immediately.
os.environ.setdefault("MAGI_FILE_REVIEW_ROW_RECHECK_MINUTES", "43200")

logger = logging.getLogger("file-review-orchestrator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", stream=sys.stderr)


def _acquire_file_review_portal_lock(owner: str):
    """Try to reserve the one cross-process court/Playwright resource domain."""
    return acquire_lock(
        FILE_REVIEW_PORTAL_LOCK_NAME,
        owner=f"file-review:{owner}",
        kind="court_portal_playwright",
        blocking=False,
        path=file_review_portal_lock_path(),
    )


def _portal_deferred_result(lock, owner: str, *, success: bool = True) -> dict:
    active = dict(getattr(lock, "active_owner", None) or {})
    lock_info = lock.as_dict() if callable(getattr(lock, "as_dict", None)) else {
        "acquired": False,
        "domain": FILE_REVIEW_PORTAL_LOCK_NAME,
        "active_owner": active,
    }
    return {
        "success": bool(success),
        "ok": True,
        "status": "deferred",
        "deferred": True,
        "skipped": True,
        "reason": "file_review_portal_busy",
        "owner": owner,
        "active_pid": int(active.get("pid") or 0),
        "active_owner": str(active.get("owner") or ""),
        "lock": lock_info,
        "message": "法院入口目前由其他作業使用，本次安全延後，未啟動第二個 Chromium。",
    }


def _portal_serialized(owner: str):
    """Make a portal command nonblocking and mutually exclusive across processes."""
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            if (
                owner == "downloadable_probe"
                and not bool(kwargs.get("read_only"))
                and not _truthy(os.environ.get("MAGI_FILE_REVIEW_PRIMARY_OWNER", "0"))
                and not _truthy(os.environ.get("MAGI_FILE_REVIEW_FORCE_RUN", "0"))
            ):
                owner_state = _fresh_file_review_auto_owner_state()
                if owner_state.get("fresh"):
                    return {
                        "success": True,
                        "ok": True,
                        "status": "delegated",
                        "skipped": True,
                        "deferred": False,
                        "reason": "primary_owner_active",
                        "owner": "file_review_auto",
                        "owner_state": owner_state,
                        "count": 0,
                        "downloadable_count": 0,
                        "items": [],
                        "message": "閱卷常駐巡查正常；本排程為備援，未重複開啟法院入口。",
                    }
            lock = _acquire_file_review_portal_lock(owner)
            if not lock.acquired:
                logger.info("Court portal busy; deferring %s", owner)
                return _portal_deferred_result(lock, owner)
            try:
                return func(*args, **kwargs)
            finally:
                lock.release()
        return wrapped
    return decorator


def _lower_background_priority() -> bool:
    """Yield CPU to interactive apps while long portal downloads run."""
    try:
        increment = int(os.environ.get("MAGI_FILE_REVIEW_NICE_INCREMENT", "10") or "10")
    except (TypeError, ValueError):
        increment = 10
    if increment <= 0:
        return False

    try:
        current = os.getpriority(os.PRIO_PROCESS, 0)
        os.setpriority(os.PRIO_PROCESS, 0, min(19, current + increment))
        return True
    except (AttributeError, OSError):
        try:
            os.nice(increment)
            return True
        except (AttributeError, OSError):
            logger.debug("Unable to lower background worker priority", exc_info=True)
            return False


def _flow_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip()).strip("-._") or "task"


def _safe_create_flow_mirror(task_name: str, *, metadata: Optional[Dict[str, Any]] = None) -> str:
    if not str(task_name or "").strip():
        return ""
    if os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") == "real_entrypoint_fixture_v1":
        # Certification must not open the production flow ledger.  The task
        # body still runs to a terminal result against its bounded provider.
        return ""
    payload = dict(metadata or {})
    run_bits = [datetime.now().strftime("%Y%m%d_%H%M%S"), _flow_slug(task_name)]
    for key in ("case_number", "job_id", "court_code"):
        value = str(payload.get(key) or "").strip()
        if value:
            run_bits.append(_flow_slug(value)[:40])
            break
    try:
        flow = _flow_ledger.create_flow(
            parent_job_id=os.environ.get("MAGI_FILE_REVIEW_FLOW_PARENT_JOB_ID", "skill_file_review_orchestrator"),
            run_id="_".join(bit for bit in run_bits if bit),
            task=task_name,
            metadata={**payload, "source": "file-review-orchestrator"},
        )
        return str(flow.get("flow_id") or "")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 114, exc_info=True)
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
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 141, exc_info=True)


def _flow_artifacts_from_result(result: Dict[str, Any]) -> Dict[str, str]:
    artifacts: Dict[str, str] = {}
    if not isinstance(result, dict):
        return artifacts
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    for key in ("screenshot", "list_screenshot", "html"):
        value = str(evidence.get(key) or "").strip()
        if value:
            artifacts[key] = value
    for key in ("status_path", "log_path"):
        value = str(result.get(key) or "").strip()
        if value:
            artifacts[key] = value
    files = result.get("files") if isinstance(result.get("files"), list) else []
    for idx, value in enumerate(files[:3], start=1):
        if value:
            artifacts[f"file_{idx}"] = str(value)
    return artifacts


def _safe_finalize_flow(flow_id: str, result: Dict[str, Any]) -> None:
    if not flow_id or not isinstance(result, dict):
        return
    if bool(result.get("queued")) and not bool(result.get("deduped")):
        return
    try:
        result_key = str(result.get("result") or "").strip().lower()
        status_key = str(result.get("status") or "").strip().lower()
        ok = bool(result.get("success", result.get("ok")))
        blockers: List[str] = []
        flow_status = "succeeded" if ok else "failed"
        if bool(result.get("cancelled")) or status_key == "cancelled":
            flow_status = "cancelled"
            ok = False
            blockers.append("cancel_requested")
        elif result_key == "ready":
            flow_status = "blocked"
            ok = False
            blockers.append("manual_confirmation_required")
        elif bool(result.get("manual_required")):
            flow_status = "blocked"
            ok = False
            blockers.append(str(result.get("manual_reason") or "manual_required").strip())
        elif status_key == "already_running":
            flow_status = "succeeded"
            ok = True
        _flow_ledger.finalize_flow(
            flow_id,
            status=flow_status,
            ok=ok,
            summary=str(result.get("message") or result.get("error") or result.get("status") or result.get("result") or "").strip()[:300],
            blockers=[item for item in blockers if item],
            metadata={
                "status": str(result.get("status") or "").strip(),
                "result": str(result.get("result") or "").strip(),
                "queued": bool(result.get("queued")),
                "deduped": bool(result.get("deduped")),
                "downloaded_count": int(result.get("downloaded_count") or 0),
                "review_download_count": int(result.get("review_download_count") or 0),
                "payment_download_count": int(result.get("payment_download_count") or 0),
                "cancelled": bool(result.get("cancelled")),
            },
            artifacts=_flow_artifacts_from_result(result) or None,
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 194, exc_info=True)


def _mark_notify_step(flow_id: str, *, notify: bool, detail: str) -> None:
    _safe_flow_step_status(
        flow_id,
        "notify",
        status="succeeded" if notify else "skipped",
        ok=bool(notify),
        skipped=not notify,
        detail=detail[:240],
    )


def _result_step_status(result: Dict[str, Any]) -> Tuple[str, bool]:
    if not isinstance(result, dict):
        return "failed", False
    if bool(result.get("cancelled")) or str(result.get("status") or "").strip().lower() == "cancelled":
        return "cancelled", False
    ok = bool(result.get("success", result.get("ok")))
    if str(result.get("result") or "").strip().lower() == "ready" or bool(result.get("manual_required")):
        return "blocked", False
    if ok:
        return "succeeded", True
    return "failed", False


def _cancel_reason(flow_id: str) -> str:
    if not flow_id:
        return ""
    try:
        return _flow_ledger.get_cancel_reason(flow_id)
    except Exception:
        return ""


def _cancelled_result(flow_id: str, step_name: str, *, detail: str = "") -> Dict[str, Any]:
    reason = detail or _cancel_reason(flow_id) or "operator requested"
    message = f"cancel_requested: {reason}"[:240]
    _safe_flow_step_status(
        flow_id,
        step_name,
        status="cancelled",
        detail=message,
        ok=False,
        metadata={"cancel_requested": True},
    )
    return {
        "success": False,
        "cancelled": True,
        "status": "cancelled",
        "error": message,
        "message": "⏹️ 閱卷任務已取消",
    }


def _check_flow_cancelled(flow_id: str, step_name: str, *, detail: str = "") -> Optional[Dict[str, Any]]:
    if not flow_id:
        return None
    try:
        if _flow_ledger.is_cancel_requested(flow_id):
            return _cancelled_result(flow_id, step_name, detail=detail)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 252, exc_info=True)
    return None


def _run_with_flow(
    task_name: str,
    runner: Callable[[str], dict],
    *,
    metadata: Optional[Dict[str, Any]] = None,
    step_name: str = "",
    detail: str = "",
) -> dict:
    flow_id = _safe_create_flow_mirror(task_name, metadata=metadata)
    cancelled = _check_flow_cancelled(flow_id, step_name or task_name)
    if cancelled:
        _safe_finalize_flow(flow_id, cancelled)
        return cancelled
    if step_name:
        _safe_flow_step_status(flow_id, step_name, status="running", detail=detail or task_name)
    result = runner(flow_id)
    if step_name and not (bool(result.get("queued")) and not bool(result.get("deduped"))):
        status, ok = _result_step_status(result)
        _safe_flow_step_status(
            flow_id,
            step_name,
            status=status,
            detail=str(result.get("message") or result.get("error") or result.get("status") or result.get("result") or "").strip()[:240],
            ok=ok,
            skipped=False,
        )
    _safe_finalize_flow(flow_id, result)
    return result

def _coerce_retention_days(value: object, default: int) -> int:
    try:
        days = int(str(value).strip())
    except Exception:
        days = int(default)
    return max(1, days)


def _retention_days_from_env(name: str, default: int) -> int:
    return _coerce_retention_days(os.environ.get(name, default), default)


def _folder_name_age_days(name: str, *, today: Optional[datetime] = None) -> Optional[int]:
    if not (str(name or "").isdigit() and len(str(name or "")) == 8):
        return None
    try:
        folder_day = datetime.strptime(str(name), "%Y%m%d").date()
    except Exception:
        return None
    today_day = (today or datetime.now()).date()
    return (today_day - folder_day).days


def _dir_size_bytes(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except Exception:
        return 0
    return total


def _cleanup_old_downloads(
    download_folder: str,
    max_days: int = 7,
    *,
    pending_max_days: Optional[int] = None,
    quarantine_max_days: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Clean up disposable MAGI download staging folders older than max_days.

    Applies to: 閱卷下載/, 筆錄下載/, 法扶資料/ 下的 YYYYMMDD 暫存資料夾，
    plus disposable duplicate/error quarantine date-folders and _待歸檔 date
    folders.  _待歸檔 uses a longer retention because it may need human triage.
    """
    summary = {
        "success": True,
        "download_folder": download_folder,
        "dry_run": bool(dry_run),
        "max_days": max_days,
        "pending_max_days": pending_max_days,
        "quarantine_max_days": quarantine_max_days,
        "deleted": [],
        "would_delete": [],
        "freed_bytes": 0,
        "error": "",
    }
    if not download_folder or not os.path.exists(download_folder):
        summary["success"] = False
        summary["error"] = "download_folder_missing"
        return summary

    # [Safety Guard] Ensure we only delete inside a MAGI folder, protecting case folders
    abs_folder = os.path.abspath(download_folder)
    safe_markers = ("MAGI", "閱卷下載", "筆錄下載", "法扶資料")
    if not any(m in abs_folder for m in safe_markers):
        logger.warning("Safety abort: download_folder %s does not contain safe markers. Cleanup aborted to protect case folders.", abs_folder)
        summary["success"] = False
        summary["error"] = "unsafe_download_folder"
        return summary

    import time
    try:
        max_days = _retention_days_from_env("MAGI_FILE_REVIEW_STAGING_RETENTION_DAYS", max_days)
        pending_max_days = _retention_days_from_env(
            "MAGI_FILE_REVIEW_PENDING_RETENTION_DAYS",
            pending_max_days if pending_max_days is not None else 14,
        )
        quarantine_max_days = _retention_days_from_env(
            "MAGI_FILE_REVIEW_QUARANTINE_RETENTION_DAYS",
            quarantine_max_days if quarantine_max_days is not None else 14,
        )
        summary["max_days"] = max_days
        summary["pending_max_days"] = pending_max_days
        summary["quarantine_max_days"] = quarantine_max_days
        now = time.time()

        def _is_expired(item_path: str, item_name: str, days: int) -> bool:
            age_days = _folder_name_age_days(item_name)
            if age_days is not None:
                return age_days >= days
            try:
                return (now - os.path.getmtime(item_path)) > (days * 86400)
            except OSError:
                return False

        def _delete_dir(item_path: str, reason: str) -> None:
            size = _dir_size_bytes(item_path)
            row = {"path": item_path, "reason": reason, "size_bytes": size}
            if dry_run:
                summary["would_delete"].append(row)
                return
            shutil.rmtree(item_path, ignore_errors=True)
            summary["deleted"].append(row)
            summary["freed_bytes"] += size
            logger.info("Cleaned up old download staging folder: %s", item_path)

        for item in os.listdir(download_folder):
            item_path = os.path.join(download_folder, item)
            if not os.path.isdir(item_path) or _folder_name_age_days(item) is None:
                continue
            try:
                if _is_expired(item_path, item, max_days):
                    _delete_dir(item_path, "dated_staging")
            except Exception as e:
                logger.warning("Failed to check/cleanup %s: %s", item_path, e)

        for container, retention_days, reason in (
            ("_duplicate_downloads", quarantine_max_days, "duplicate_quarantine"),
            ("_ignored_downloads", quarantine_max_days, "ignored_downloads"),
            ("_待歸檔", pending_max_days, "pending_unarchived"),
        ):
            cpath = os.path.join(download_folder, container)
            if not os.path.isdir(cpath):
                continue
            for item in os.listdir(cpath):
                item_path = os.path.join(cpath, item)
                if not os.path.isdir(item_path) or _folder_name_age_days(item) is None:
                    continue
                try:
                    if _is_expired(item_path, item, retention_days):
                        _delete_dir(item_path, reason)
                except Exception as e:
                    logger.warning("Failed to check/cleanup %s: %s", item_path, e)
    except Exception as e:
        logger.warning("Cleanup old downloads failed: %s", e)
        summary["success"] = False
        summary["error"] = str(e)[:200]
    return summary


def _cleanup_all_download_folders(
    base_dir: str,
    max_days: int = 7,
    *,
    pending_max_days: Optional[int] = None,
    quarantine_max_days: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """對 閱卷下載/、筆錄下載/、法扶資料/ 都執行舊資料夾清理。"""
    out = {"success": True, "base_dir": base_dir, "folders": [], "deleted_count": 0, "would_delete_count": 0, "freed_bytes": 0}
    if not base_dir:
        out["success"] = False
        return out
    for sub in ("閱卷下載", "筆錄下載", "法扶資料"):
        folder = os.path.join(base_dir, sub)
        if os.path.isdir(folder):
            summary = _cleanup_old_downloads(
                folder,
                max_days=max_days,
                pending_max_days=pending_max_days,
                quarantine_max_days=quarantine_max_days,
                dry_run=dry_run,
            )
            out["folders"].append(summary)
            out["deleted_count"] += len(summary.get("deleted") or [])
            out["would_delete_count"] += len(summary.get("would_delete") or [])
            out["freed_bytes"] += int(summary.get("freed_bytes") or 0)
            if not summary.get("success", False):
                out["success"] = False
    return out


def _download_cleanup_base_candidates(download_folder: str = "") -> List[str]:
    """Return likely MAGI runtime roots that may contain download staging folders."""
    candidates: List[str] = []

    # V3 schedule certification must exercise the real cleanup body without
    # even enumerating live staging roots.  The caller supplies an owned
    # fixture root carrying a marker; a malformed adapter request fails closed
    # instead of falling through to the production candidates below.
    if os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") == "real_entrypoint_fixture_v1":
        fixture_raw = os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT", "").strip()
        fixture = Path(fixture_raw).expanduser() if fixture_raw else None
        marker = fixture / ".magi-v3-schedule-fixture" if fixture else None
        if (
            os.environ.get("MAGI_V3_SCHEDULE_DRY_RUN") != "1"
            or fixture is None
            or marker is None
            or not marker.is_file()
        ):
            return []
        fixture_resolved = fixture.resolve()
        requested = Path(download_folder).expanduser().resolve() if download_folder else None
        if requested is None or fixture_resolved not in requested.parents:
            return []
        base = requested.parent
        return [str(base)] if base.is_dir() else []

    def _add(path: str) -> None:
        raw = str(path or "").strip()
        if not raw:
            return
        expanded = os.path.abspath(os.path.expanduser(raw))
        if expanded not in candidates:
            candidates.append(expanded)

    if download_folder:
        _add(os.path.dirname(download_folder))
    env_download = os.environ.get("MAGI_EEFILE_DOWNLOAD_FOLDER", "").strip()
    if env_download:
        _add(os.path.dirname(env_download))
    _add(os.environ.get("MAGI_ROOT_DIR", ""))
    _add(_magi_root)
    _add(os.environ.get("MAGI_LIVE_RUNTIME_ROOT", ""))
    _add(os.environ.get("MAGI_RUNTIME_ROOT", ""))
    _add(os.path.join(Path.home(), "Library", "Application Support", "MAGI", "runtime", "MAGI_v3"))

    existing = []
    for base in candidates:
        if not os.path.isdir(base):
            continue
        if any(os.path.isdir(os.path.join(base, sub)) for sub in ("閱卷下載", "筆錄下載", "法扶資料")):
            existing.append(base)
    return existing


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


def _token_backups(token_path: str) -> List[str]:
    base = (token_path or "").strip()
    if not base:
        return []
    pats = [f"{base}.bak_*", f"{base}.invalid_*"]
    out: List[str] = []
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
    只提供 file_review_automation 需要的方法：
    execute/fetch_one/fetch_all/find_case/translate_path_to_local。
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

    def fetch_one(self, query: str, params: tuple = None, as_dict: bool = True):
        conn = None
        try:
            conn = self.get_connection()
            if as_dict:
                cur = conn.cursor()
            else:
                import pymysql
                cur = conn.cursor(pymysql.cursors.Cursor)
            cur.execute(query, params)
            return cur.fetchone()
        except Exception:
            return None
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                logging.getLogger(__name__).debug(
                    "silent-catch at %s:%s",
                    __name__,
                    "fetch_one_close",
                    exc_info=True,
                )

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
        if profiles:
            # Patch existing profiles with env-var overrides
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
        else:
            # config.json has no mariadb_profiles — synthesise one from OSC_DB_* / MAGI_REMOTE_DB_* env vars
            # This is the common case when MAGI_PREFER_LOCAL_DB=1 but no local profile is defined.
            synth_cfg: dict = {}
            if o_host:
                synth_cfg["host"] = o_host
            if o_port:
                try:
                    synth_cfg["port"] = int(o_port)
                except Exception:
                    synth_cfg["port"] = o_port
            if o_user:
                synth_cfg["user"] = o_user
            if o_pass:
                synth_cfg["password"] = o_pass
            if o_name:
                synth_cfg["database"] = o_name
            synth_cfg.setdefault("charset", "utf8mb4")
            synth_cfg.setdefault("connect_timeout", 8)
            profiles = [{"profile_name": "env_synth", "config": synth_cfg}]
            logger.info("DB manager: synthesised profile from OSC_DB_*/MAGI_REMOTE_DB_* env vars (host=%s)", o_host)
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
    try:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except BrokenPipeError:
        pass
    if isinstance(payload, dict):
        if payload.get("success") is False:
            return 1
        if payload.get("manual_required") or payload.get("blocked"):
            return 1
        if str(payload.get("result") or "").strip().lower() == "ready":
            return 1
    return 0


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _boolish(value, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _notifications_suppressed() -> bool:
    return _boolish(os.environ.get("MAGI_FILE_REVIEW_SUPPRESS_NOTIFY"), False)


def _download_job_paths(job_id: str) -> Tuple[str, str]:
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


def _portal_login_failure_message(mgr, *, action_label: str) -> Tuple[str, str, str]:
    code = str(
        getattr(mgr, "last_login_error_code", "")
        or getattr(getattr(mgr, "sso", None), "last_error_code", "")
        or "sso_login_failed"
    ).strip() or "sso_login_failed"
    detail = str(
        getattr(mgr, "last_login_error_detail", "")
        or getattr(getattr(mgr, "sso", None), "last_error_detail", "")
        or ""
    ).strip()

    if code == "driver_init_failed":
        return code, detail, f"❌ 閱卷登入失敗：Chrome 啟動異常，已中斷{action_label}。"
    if code == "captcha_failed":
        return code, detail, f"❌ 閱卷登入失敗：驗證碼未通過，已中斷{action_label}。"
    if code == "auth_failed":
        return code, detail, f"❌ 閱卷登入失敗：帳號或密碼被拒絕，已中斷{action_label}。"
    if code == "login_page_timeout":
        return code, detail, f"❌ 閱卷入口連線逾時，已中斷{action_label}；系統稍後會用全新連線重試。"
    if code == "login_redirect_unexpected":
        return code, detail, f"❌ 閱卷登入被導向非預期頁面，已中斷{action_label}。"
    if code == "login_contract_changed":
        return code, detail, f"❌ 閱卷登入頁欄位結構異常，已中斷{action_label}；需檢查法院入口頁。"
    return code, detail, f"❌ 閱卷登入失敗，可能驗證碼連錯或系統維護，已中斷{action_label}。"


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

def _missing_runtime_deps() -> List[str]:
    """Return missing runtime packages without modifying the environment."""
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
    return sorted(set(need))


def _ensure_runtime_deps():
    """
    Best-effort dependency bootstrap for non-read-only operational commands.

    Health probes must call ``_missing_runtime_deps`` instead so inspection
    never installs packages or contacts package indexes.
    """
    need = _missing_runtime_deps()
    if need:
        logger.info("Installing missing deps (best-effort): %s", ", ".join(need))
        _pip_install(need)

def _json_path(name: str) -> str:
    """Resolve credential/token file under JSON_DIR if present."""
    if name == "filereview_token.pickle":
        declared = os.environ.get("MAGI_FILE_REVIEW_TOKEN_PATH", "").strip()
        if declared:
            return declared
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
def _download_notice_ledger_path(download_folder: str) -> str:
    return os.path.join(os.path.abspath(download_folder), DOWNLOAD_NOTICE_LEDGER_FILE)


def _download_notice_lock_path(download_folder: str) -> str:
    return _download_notice_ledger_path(download_folder) + ".lock"


def _canonical_download_notice_root(download_folder: str) -> Path:
    raw = Path(str(download_folder or "")).expanduser()
    if not raw.is_absolute():
        raise RuntimeError("file_review_notice_root_not_absolute")
    try:
        resolved = raw.resolve(strict=True)
        info = raw.lstat()
    except OSError as exc:
        raise RuntimeError("file_review_notice_root_unavailable") from exc
    if resolved != raw or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("file_review_notice_root_not_canonical")
    return resolved


def _new_download_notice_ledger() -> dict:
    return {
        "schema": DOWNLOAD_NOTICE_LEDGER_SCHEMA,
        "artifacts": {},
        "events": {},
        "updated_at": "",
        "pii_included": False,
    }


def _read_download_notice_ledger(path: str) -> dict:
    if not os.path.exists(path):
        return _new_download_notice_ledger()
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError("file_review_notice_ledger_not_regular")
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or set(value) != {
        "schema", "artifacts", "events", "updated_at", "pii_included"
    }:
        raise RuntimeError("file_review_notice_ledger_schema_invalid")
    if value.get("schema") != DOWNLOAD_NOTICE_LEDGER_SCHEMA:
        raise RuntimeError("file_review_notice_ledger_version_invalid")
    if not isinstance(value.get("artifacts"), dict) or not isinstance(value.get("events"), dict):
        raise RuntimeError("file_review_notice_ledger_collections_invalid")
    if value.get("pii_included") is not False:
        raise RuntimeError("file_review_notice_ledger_privacy_invalid")
    return value


def _write_download_notice_ledger(path: str, value: dict) -> None:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    tmp_path = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    try:
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def _with_download_notice_ledger(download_folder: str, operation: Callable[[dict], Any]) -> Any:
    folder = str(_canonical_download_notice_root(download_folder))
    lock_path = _download_notice_lock_path(folder)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, flags, 0o600)
    try:
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
            raise RuntimeError("file_review_notice_lock_not_regular")
        with os.fdopen(lock_fd, "r+", encoding="utf-8", closefd=False) as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            path = _download_notice_ledger_path(folder)
            ledger = _read_download_notice_ledger(path)
            result = operation(ledger)
            ledger["updated_at"] = datetime.now().astimezone().isoformat()
            _write_download_notice_ledger(path, ledger)
            return result
    finally:
        os.close(lock_fd)


def _regular_download_artifact(path: str, *, allowed_root: Path) -> Optional[dict]:
    candidate = Path(str(path or "")).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        raw = candidate.resolve(strict=True)
        info = candidate.lstat()
    except OSError:
        return None
    if (
        raw != candidate
        or not raw.is_relative_to(allowed_root)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size <= 0
    ):
        return None
    digest = hashlib.sha256()
    with open(candidate, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size": int(info.st_size)}


def _archive_item_root(item: dict, *, download_root: Path) -> Path:
    """Return the canonical root that is allowed to contain an archived item."""
    folder_value = str(item.get("folder") or "").strip()
    if not folder_value:
        return download_root
    folder = Path(folder_value).expanduser()
    if not folder.is_absolute():
        raise RuntimeError("file_review_notice_archive_root_not_absolute")
    try:
        resolved = folder.resolve(strict=True)
        info = folder.lstat()
    except OSError as exc:
        raise RuntimeError("file_review_notice_archive_root_unavailable") from exc
    if resolved != folder or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("file_review_notice_archive_root_not_canonical")
    return resolved


def _validated_source_content_receipt(value: Any) -> Optional[dict]:
    if not isinstance(value, dict) or set(value) != {"sha256", "size"}:
        return None
    sha = value.get("sha256")
    size = value.get("size")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
        return None
    if type(size) is not int or size <= 0:
        return None
    return {"sha256": sha, "size": size}


def _canonical_review_artifact_filename(filename: str) -> str:
    # Chrome appends `` (1)`` before an extension when the portal emits the
    # same logical filename again.  It is not a new court-document identity.
    name = os.path.basename(str(filename or "")).strip().lower()
    name = re.sub(r"\s*\(\d+\)(?=\.[^.]+$)", "", name)
    return re.sub(r"\s+", " ", name)


def _logical_review_artifact_digest(case_number: str, filename: str) -> str:
    name = _canonical_review_artifact_filename(filename)
    case_token = re.sub(r"\s+", "", str(case_number or "").strip().lower())
    material = f"file-review-artifact/v1\0{case_token}\0{name}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _collect_download_notice_artifacts(
    items: list,
    review_downloaded: list,
    *,
    download_folder: str,
    content_receipts: Optional[dict] = None,
) -> list:
    allowed_root = _canonical_download_notice_root(download_folder)
    strict_source_receipts = content_receipts is not None
    trusted_receipts = content_receipts if isinstance(content_receipts, dict) else {}
    records: list[dict] = []
    seen: set[tuple[str, str]] = set()
    fallback_paths = {
        os.path.basename(str(path)): str(path)
        for path in (review_downloaded or [])
        if str(path or "").strip()
    }
    fallback_paths.update(
        {
            _canonical_review_artifact_filename(name): path
            for name, path in list(fallback_paths.items())
        }
    )
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if _activity_artifact_kind(item) == "payment_slip":
            continue
        if str(item.get("action") or "") in {
            "exists_skip", "target_exists_keep_src", "target_exists_isolate_src",
            "blocked_case_identity_mismatch", "ignored_invalid_artifact",
        }:
            continue
        filename = os.path.basename(str(item.get("file") or "").strip())
        source_path = fallback_paths.get(filename, "") or fallback_paths.get(
            _canonical_review_artifact_filename(filename), ""
        )
        source_receipt = _validated_source_content_receipt(
            trusted_receipts.get(source_path)
            or trusted_receipts.get(filename)
            or trusted_receipts.get(_canonical_review_artifact_filename(filename))
        )
        if strict_source_receipts and source_receipt is None:
            raise RuntimeError("file_review_notice_source_receipt_missing")

        destination = str(item.get("dst") or "").strip()
        destination_root = _archive_item_root(item, download_root=allowed_root)
        artifact = _regular_download_artifact(destination, allowed_root=destination_root)
        if artifact is None:
            raise RuntimeError("file_review_notice_final_artifact_invalid")
        if source_receipt is not None and artifact != source_receipt:
            raise RuntimeError("file_review_notice_final_artifact_mismatch")
        logical_identity = str(
            item.get("court_case_no")
            or item.get("folder")
            or item.get("party")
            or ""
        )
        logical = _logical_review_artifact_digest(logical_identity, filename)
        key = (logical, artifact["sha256"])
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "logical_digest": logical,
                "content_sha256": artifact["sha256"],
                "size": artifact["size"],
            }
        )
    if records:
        return sorted(records, key=lambda row: (row["logical_digest"], row["content_sha256"]))

    if strict_source_receipts and review_downloaded:
        raise RuntimeError("file_review_notice_final_artifact_mapping_missing")

    # A failed/disabled archive can still leave verified files in the download
    # directory.  Preserve duplicate protection without storing the raw path or
    # filename in the ledger.
    for raw in review_downloaded or []:
        artifact = _regular_download_artifact(str(raw), allowed_root=allowed_root)
        if artifact is None:
            continue
        filename = os.path.basename(str(raw))
        logical = _logical_review_artifact_digest("", filename)
        key = (logical, artifact["sha256"])
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "logical_digest": logical,
                "content_sha256": artifact["sha256"],
                "size": artifact["size"],
            }
        )
    return sorted(records, key=lambda row: (row["logical_digest"], row["content_sha256"]))


def _prepare_download_notice(
    download_folder: str,
    items: list,
    review_downloaded: list,
    *,
    notify_requested: bool,
    content_receipts: Optional[dict] = None,
) -> dict:
    artifacts = _collect_download_notice_artifacts(
        items,
        review_downloaded,
        download_folder=download_folder,
        content_receipts=content_receipts,
    )
    if not artifacts:
        return {
            "valid": True,
            "should_notify": False,
            "event_digest": "",
            "new_count": 0,
            "updated_count": 0,
            "duplicate_count": 0,
        }

    event_material = "\n".join(
        f"{row['logical_digest']}:{row['content_sha256']}:{row['size']}"
        for row in artifacts
    )
    event_digest = hashlib.sha256(
        ("file-review-download-notice/v1\0" + event_material).encode("utf-8")
    ).hexdigest()

    def operation(ledger: dict) -> dict:
        now = datetime.now().astimezone().isoformat()
        new_count = 0
        updated_count = 0
        duplicate_count = 0
        # A deliberate no-notify execution is observation-only.  It must not
        # consume the later notification right for this content.
        if not notify_requested:
            return {
                "valid": True,
                "should_notify": False,
                "event_digest": event_digest,
                "new_count": 0,
                "updated_count": 0,
                "duplicate_count": 0,
            }
        for row in artifacts:
            logical = row["logical_digest"]
            entry = ledger["artifacts"].get(logical)
            if entry is None:
                entry = {"versions": []}
                ledger["artifacts"][logical] = entry
            if not isinstance(entry, dict) or set(entry) != {"versions"} or not isinstance(entry["versions"], list):
                raise RuntimeError("file_review_notice_artifact_history_invalid")
            versions = entry["versions"]
            matched = any(
                isinstance(version, dict)
                and version.get("content_sha256") == row["content_sha256"]
                and version.get("size") == row["size"]
                for version in versions
            )
            if matched:
                duplicate_count += 1
                continue
            if versions:
                updated_count += 1
            else:
                new_count += 1
            versions.append(
                {
                    "content_sha256": row["content_sha256"],
                    "size": row["size"],
                    "observed_at": now,
                }
            )

        previous_event = ledger["events"].get(event_digest)
        delivered = isinstance(previous_event, dict) and previous_event.get("status") == "accepted"
        pending = isinstance(previous_event, dict) and previous_event.get("status") == "pending"
        if pending:
            new_count = int(previous_event.get("new_count") or 0)
            updated_count = int(previous_event.get("updated_count") or 0)
            duplicate_count = 0
        changed_count = new_count + updated_count
        should_notify = bool(
            notify_requested and not delivered and (changed_count > 0 or pending)
        )
        if should_notify:
            ledger["events"][event_digest] = {
                "status": "pending",
                "prepared_at": now,
                "completed_at": "",
                "artifact_count": len(artifacts),
                "new_count": new_count,
                "updated_count": updated_count,
                "pii_included": False,
            }
        return {
            "valid": True,
            "should_notify": should_notify,
            "event_digest": event_digest,
            "new_count": new_count,
            "updated_count": updated_count,
            "duplicate_count": duplicate_count,
        }

    return _with_download_notice_ledger(download_folder, operation)


def _complete_download_notice(download_folder: str, event_digest: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", str(event_digest or "")):
        raise RuntimeError("file_review_notice_event_digest_invalid")

    def operation(ledger: dict) -> None:
        event = ledger["events"].get(event_digest)
        if not isinstance(event, dict) or event.get("status") != "pending":
            raise RuntimeError("file_review_notice_event_not_pending")
        # ``send_telegram_push_with_status`` mirrors the same event to Discord
        # and may acknowledge either an immediate Telegram send or its durable
        # outbox.  Both mean this exact logical event must not be emitted again.
        event["status"] = "accepted"
        event["completed_at"] = datetime.now().astimezone().isoformat()

    _with_download_notice_ledger(download_folder, operation)


def _load_telegram_targets() -> Tuple[str, List[str]]:
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
            with open(_magi_cfg_path, "r", encoding="utf-8") as _cfg_f:
                _magi_cfg = json.loads(_cfg_f.read() or "{}")
            _magi_tg = _magi_cfg.get("telegram") or {}
            _magi_notify = _magi_tg.get("notifyTo") or []
            if isinstance(_magi_notify, list):
                notify_ids.extend([str(x).strip() for x in _magi_notify if str(x).strip()])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 602, exc_info=True)
    try:
        if 'get_legacy_telegram_settings' in globals():
            legacy = get_legacy_telegram_settings(load_openclaw_config())
            if not token:
                token = str(legacy.get("bot_token") or "").strip()
            notify_ids.extend([str(x).strip() for x in (legacy.get("notify_to") or []) if str(x).strip()])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at action.py:841", exc_info=True)
    dedup: List[str] = []
    seen: Set[str] = set()
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


def _notify(
    text: str,
    flag: bool = True,
    topic_key: str = "filereview",
    *,
    event_id: str = "",
):
    if not flag:
        return False
    if _notifications_suppressed():
        logger.info("Notification suppressed by MAGI_FILE_REVIEW_SUPPRESS_NOTIFY: %s", str(text or "")[:160])
        return False
    msg = str(text or "")
    try:
        from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore

        st = send_telegram_push_with_status(
            msg,
            severity="info",
            source="file_review_orchestrator",
            topic_key=topic_key,
            queue_on_fail=True,
            event_id=event_id,
        ) or {}
        if bool(st.get("telegram")) or bool(st.get("queued")):
            return True
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 670, exc_info=True)
    # The legacy direct fallback has no channel-scoped event receipt.  Keep it
    # only for callers without an immutable event id; otherwise leave the
    # ledger pending so the exact event can be retried safely.
    return _notify_tg(msg) if not event_id else False


def _notify_file(file_path: str, caption: str = "", flag: bool = True,
                 topic_key: str = "filereview"):
    """Send a file (image/PDF/etc.) to admin via TG and DC."""
    if not flag:
        return False
    if _notifications_suppressed():
        logger.info("File notification suppressed by MAGI_FILE_REVIEW_SUPPRESS_NOTIFY: %s", os.path.basename(file_path or ""))
        return False
    if not file_path or not os.path.isfile(file_path):
        logger.warning("_notify_file: file not found: %s", file_path)
        return False
    sent_any = False
    # 1) Telegram via LAFNotifier
    try:
        import sys
        if CODE_DIR not in sys.path:
            sys.path.insert(0, CODE_DIR)
        from line_notifier import LAFNotifier
        result = LAFNotifier().notify_admin_with_files(
            caption or os.path.basename(file_path), [file_path],
            topic_key=topic_key, source="file_review_orchestrator",
        )
        if _notification_file_result_ok(result):
            sent_any = True
            logger.info("File sent via LAFNotifier (TG): %s", os.path.basename(file_path))
        else:
            logger.warning("LAFNotifier file send did not confirm delivery for: %s result=%s", os.path.basename(file_path), result)
    except Exception as e:
        logger.warning("LAFNotifier send failed: %s", e)
        # TG fallback via red_phone
        try:
            from skills.ops.red_phone import send_file_admin  # type: ignore
            result = send_file_admin(file_path, caption=caption, topic_key=topic_key)
            if result.get("ok"):
                sent_any = True
                logger.info("File sent via red_phone (TG fallback): %s", os.path.basename(file_path))
            else:
                logger.warning("red_phone send_file_admin returned: %s", result)
        except Exception as e2:
            logger.warning("red_phone TG fallback also failed: %s", e2)
    # LAFNotifier already sends files to TG and DC. Use direct DC only as fallback.
    if not sent_any:
        try:
            from skills.ops.red_phone import send_discord_bot_file  # type: ignore
            ok = send_discord_bot_file(
                file_path,
                caption=caption or os.path.basename(file_path),
                topic_key=topic_key,
                source="file_review_orchestrator",
            )
            if ok:
                sent_any = True
                logger.info("File sent via Discord bot fallback: %s", os.path.basename(file_path))
            else:
                logger.warning("send_discord_bot_file returned False for: %s", os.path.basename(file_path))
        except Exception as e3:
            logger.warning("Discord file send failed: %s", e3)
    return sent_any


def _is_valid_payment_pdf_file(path: str) -> bool:
    """Return True only for real PDF payment files, not OLA JSON/HTML error payloads."""
    if not path or not os.path.isfile(path):
        return False
    lowered_path = str(path or "").lower()
    filename = os.path.basename(lowered_path)
    if "_ignored_downloads" in lowered_path or ".invalid_artifact" in filename:
        return False
    if not str(path).lower().endswith(".pdf"):
        return False
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
    except Exception:
        return False
    if not chunk.lstrip().startswith(b"%PDF"):
        return False
    lowered = chunk.decode("utf-8", errors="ignore").lower()
    return not any(marker in lowered for marker in ("<html", "<!doctype html", "messagetext", "\"status\"", "\"controller\""))


def _payment_delivery_state_path(download_folder: str) -> str:
    return os.path.join(download_folder or DEFAULT_DOWNLOAD_FOLDER, ".payment_pdf_delivery_state.json")


def _load_payment_delivery_state(download_folder: str) -> dict:
    path = _payment_delivery_state_path(download_folder)
    if not os.path.exists(path):
        return {"version": 1, "sent_files": {}, "pending_files": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("state_not_dict")
    except Exception:
        return {"version": 1, "sent_files": {}, "pending_files": {}}
    data.setdefault("version", 1)
    data.setdefault("sent_files", {})
    data.setdefault("pending_files", {})
    if not isinstance(data.get("sent_files"), dict):
        data["sent_files"] = {}
    if not isinstance(data.get("pending_files"), dict):
        data["pending_files"] = {}
    return data


def _save_payment_delivery_state(download_folder: str, state: dict) -> None:
    path = _payment_delivery_state_path(download_folder)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state or {}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to save payment PDF delivery state: %s", e)


def _payment_file_delivery_key(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except Exception:
        try:
            st = os.stat(path)
            return f"path:{os.path.realpath(path)}:{int(st.st_size)}:{int(st.st_mtime)}"
        except Exception:
            return f"path:{os.path.realpath(path or '')}"


def _payment_file_already_delivered(path: str, download_folder: str) -> bool:
    key = _payment_file_delivery_key(path)
    state = _load_payment_delivery_state(download_folder)
    return bool(key and key in (state.get("sent_files") or {}))


def _mark_payment_file_delivered(path: str, download_folder: str, caption: str = "") -> None:
    key = _payment_file_delivery_key(path)
    if not key:
        return
    state = _load_payment_delivery_state(download_folder)
    sent_files = state.setdefault("sent_files", {})
    sent_files[key] = {
        "path": os.path.realpath(path),
        "name": os.path.basename(path),
        "caption": caption,
        "sent_at": datetime.now().isoformat(),
        "size": os.path.getsize(path) if os.path.exists(path) else 0,
    }
    pending_files = state.setdefault("pending_files", {})
    if isinstance(pending_files, dict):
        pending_files.pop(key, None)
    state["updated_at"] = datetime.now().isoformat()
    _save_payment_delivery_state(download_folder, state)


def _mark_payment_file_delivery_failed(path: str, download_folder: str, caption: str = "", error: str = "") -> None:
    key = _payment_file_delivery_key(path)
    if not key:
        return
    state = _load_payment_delivery_state(download_folder)
    pending_files = state.setdefault("pending_files", {})
    previous = pending_files.get(key) if isinstance(pending_files, dict) else {}
    attempts = 0
    if isinstance(previous, dict):
        try:
            attempts = int(previous.get("attempts") or 0)
        except Exception:
            attempts = 0
    if isinstance(pending_files, dict):
        pending_files[key] = {
            "path": os.path.realpath(path),
            "name": os.path.basename(path),
            "caption": caption,
            "last_attempt_at": datetime.now().isoformat(),
            "attempts": attempts + 1,
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
            "last_error": str(error or "notify_file_returned_false")[:240],
        }
    state["updated_at"] = datetime.now().isoformat()
    _save_payment_delivery_state(download_folder, state)


def _notification_file_result_ok(result: object) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        for key in ("ok", "delivered", "telegram", "discord"):
            if bool(result.get(key)):
                return True
        acked = result.get("acked")
        if isinstance(acked, list) and acked:
            return True
        return False
    return bool(result)


def _payment_delivery_filename_component(value: str, default: str = "") -> str:
    text = str(value or "")
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", text)
    text = re.sub(r"\s+", "", text).strip("._- ")
    return (text or default or "").strip()[:90]


def _payment_proof_case_key(case_number: str) -> str:
    text = str(case_number or "").strip()
    if not text:
        return ""
    compact = re.sub(r"[\s._/-]+", "", text)
    compact = compact.replace("年度", "").replace("年", "").replace("字第", "").replace("字", "").replace("號", "")
    m = re.match(r"(\d{2,3})([^\d]+?)(0*\d+)$", compact)
    if not m:
        return ""
    return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)):06d}"


PAYMENT_PROOF_SCHEMA = "magi.payment-proof/v2"


def _payment_proof_event_identity(info: dict | None = None, explicit: str = "") -> str:
    """Return a stable review/payment occurrence identity when available."""
    if str(explicit or "").strip():
        return str(explicit).strip()[:512]
    info = info if isinstance(info, dict) else {}
    for key in ("payment_event_id", "portal_row_id", "rowid", "p_payid", "payid", "pay_id", "payment_id"):
        value = str(info.get(key) or "").strip()
        if value:
            return value[:512]
    return ""


def _payment_proof_dedup_key(raw_case_id: str, file_sha256: str, event_id: str = "") -> str:
    """Opaque v2 key; known portal/payment events cannot collide on case+SHA alone."""
    payload = "|".join((str(raw_case_id or "").strip(), str(file_sha256 or "").strip().lower(), str(event_id or "").strip()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_payment_proof_registry_atomic(path: str, registry: dict) -> None:
    """Durably replace the registry so a successful upload cannot corrupt it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(registry, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        try:
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            logging.getLogger(__name__).debug("registry directory fsync unavailable", exc_info=True)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _payment_proof_record_matches(record: object, file_sha256: str, event_id: str = "") -> bool:
    """Only a v2 proof with the same content and occurrence may be deduped."""
    if not isinstance(record, dict) or record.get("proof_schema") != PAYMENT_PROOF_SCHEMA:
        return False
    if str(record.get("file_sha256") or "").strip().lower() != str(file_sha256 or "").strip().lower():
        return False
    recorded_event = str(record.get("payment_event_id") or "").strip()
    return not event_id or recorded_event == event_id


def _payment_proof_registry_matches(
    registry: object, raw_case_id: str, file_sha256: str, event_id: str = ""
) -> bool:
    if not isinstance(registry, dict):
        return False
    record = registry.get(raw_case_id)
    if not isinstance(record, dict):
        return False
    candidates = [record]
    nested = record.get("proofs")
    if isinstance(nested, list):
        candidates.extend(nested)
    return any(_payment_proof_record_matches(item, file_sha256, event_id) for item in candidates)


def _payment_proof_registry_upsert(registry: dict, raw_case_id: str, record: dict) -> None:
    """Preserve multiple proof occurrences for one case without trusting legacy rows."""
    previous = registry.get(raw_case_id)
    proofs = []
    if isinstance(previous, dict):
        old_proofs = previous.get("proofs")
        if isinstance(old_proofs, list):
            proofs.extend(item for item in old_proofs if isinstance(item, dict))
        elif previous.get("proof_schema") == PAYMENT_PROOF_SCHEMA:
            proofs.append(previous)
    proofs.append(dict(record))
    registry[raw_case_id] = {**record, "proofs": proofs}


def _payment_proof_event_uploaded(case_number: str, download_folder: str, event_id: str) -> bool:
    event_id = str(event_id or "").strip()
    target_case = _normalize_case_token(case_number)
    if not event_id or not target_case:
        return False
    proof_path = str(get_payment_proof_registry_path(download_folder or DEFAULT_DOWNLOAD_FOLDER))
    try:
        with open(proof_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    for raw_case_id, record in data.items():
        # Registry rows are case-scoped; an event identifier from another
        # case must never suppress this case's new payment occurrence.
        case_key = _normalize_case_token(raw_case_id)
        if not case_key or case_key != target_case:
            continue
        candidates = [record] if isinstance(record, dict) else []
        if isinstance(record, dict) and isinstance(record.get("proofs"), list):
            candidates.extend(item for item in record["proofs"] if isinstance(item, dict))
        for candidate in candidates:
            if (
                candidate.get("proof_schema") == PAYMENT_PROOF_SCHEMA
                and str(candidate.get("file_sha256") or "").strip()
                and str(candidate.get("payment_event_id") or "").strip() == event_id
                and _normalize_case_token(str(candidate.get("raw_case_id") or raw_case_id)) == case_key
            ):
                return True
    return False


def _payment_proof_already_uploaded(case_number: str, download_folder: str, event_id: str = "") -> bool:
    """Case-only legacy rows never suppress a new payment occurrence."""
    return _payment_proof_event_uploaded(case_number, download_folder, event_id)


def _same_payment_file_payload(path_a: str, path_b: str) -> bool:
    try:
        if not (os.path.isfile(path_a) and os.path.isfile(path_b)):
            return False
        return _payment_file_delivery_key(path_a) == _payment_file_delivery_key(path_b)
    except Exception:
        return False


def _payment_delivery_copy_path(path: str, label: str, download_folder: str) -> str:
    parts = [
        _payment_delivery_filename_component(part)
        for part in re.split(r"[｜|]", str(label or ""))
        if _payment_delivery_filename_component(part)
    ]
    if len(parts) >= 2:
        party, case_no = parts[0], parts[1]
    elif len(parts) == 1:
        party = parts[0]
        case_no = ""
    else:
        party = ""
        case_no = ""

    if not party:
        party = "未辨識當事人"
    if not case_no:
        original_stem = _payment_delivery_filename_component(Path(path).stem, default="未辨識案號")
        case_no = original_stem if not original_stem.isdigit() else "未辨識案號"

    base_stem = "_".join(part for part in ("繳費單", party, case_no) if part)
    delivery_dir = os.path.join(download_folder or DEFAULT_DOWNLOAD_FOLDER, ".payment_delivery_files")
    os.makedirs(delivery_dir, exist_ok=True)
    ext = os.path.splitext(path)[1].lower() or ".pdf"
    dst = os.path.join(delivery_dir, f"{base_stem}{ext}")
    if os.path.exists(dst) and not _same_payment_file_payload(path, dst):
        short_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:8]
        dst = os.path.join(delivery_dir, f"{base_stem}_{short_hash}{ext}")
    return dst


def _prepare_payment_pdf_delivery_copy(path: str, label: str, download_folder: str) -> str:
    try:
        dst = _payment_delivery_copy_path(path, label, download_folder)
        if os.path.realpath(path) != os.path.realpath(dst):
            if not (os.path.exists(dst) and _same_payment_file_payload(path, dst)):
                shutil.copy2(path, dst)
        return dst
    except Exception as e:
        logger.warning("Failed to create renamed payment PDF delivery copy for %s: %s", os.path.basename(path), e)
        return path


def _send_payment_pdf_files(
    file_paths: List[str],
    *,
    download_folder: str,
    caption_prefix: str,
    notify: bool = True,
    captions_by_path: Optional[Dict[str, str]] = None,
    notice_keys_by_path: Optional[Dict[str, Iterable[str]]] = None,
) -> dict:
    """Send valid, not-yet-delivered payment PDFs and persist delivery state."""
    unique: List[str] = []
    seen: Set[str] = set()
    invalid = 0
    already_sent = 0
    notice_seen = 0
    for raw in file_paths or []:
        path = str(raw or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        if not _is_valid_payment_pdf_file(path):
            invalid += 1
            continue
        if _payment_file_already_delivered(path, download_folder):
            already_sent += 1
            continue
        # Text-notice dedup must never suppress attachment delivery. The file
        # SHA registry above is the authoritative delivery dedup.
        unique.append(path)

    sent = 0
    failed = 0
    skipped_notify = 0
    for idx, path in enumerate(unique, 1):
        label = (captions_by_path or {}).get(path) or os.path.basename(path)
        caption = f"{caption_prefix} ({idx}/{len(unique)}): {label}"
        if not notify:
            skipped_notify += 1
            continue
        delivery_path = _prepare_payment_pdf_delivery_copy(path, label, download_folder)
        if _notification_file_result_ok(_notify_file(delivery_path, caption=caption, flag=True, topic_key="filereview_payment")):
            sent += 1
            _mark_payment_file_delivered(path, download_folder, caption=caption)
        else:
            failed += 1
            _mark_payment_file_delivery_failed(path, download_folder, caption=caption)
    return {
        "eligible": len(unique),
        "sent": sent,
        "failed": failed,
        "skipped_notify": skipped_notify,
        "invalid": invalid,
        "already_sent": already_sent,
        "notice_seen": notice_seen,
        "pending": failed,
    }


# ---------------------------------------------------------------------------
# DB Helper
# ---------------------------------------------------------------------------
def _get_db_manager(cfg: dict, *, read_only: bool = False):
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
        if not read_only:
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
    "高院": "TPH", "高等法院": "TPH", "台灣高等法院": "TPH", "臺灣高等法院": "TPH",
    "高雄高分院": "KSH",
    "台中高分院": "TCH",
    "台南高分院": "TNH",
    "花蓮高分院": "HLH",
    # 橋頭地院
    "橋頭": "CTD",
    # 高等行政法院
    "臺北高等行政法院": "TPAA", "台北高等行政法院": "TPAA",
    "臺中高等行政法院": "TCAA", "台中高等行政法院": "TCAA",
    "高雄高等行政法院": "KSAA",
    # 專業法院
    "智慧財產及商業法院": "IPC", "智財法院": "IPC",
    "少年及家事法院": "KJF", "高雄少家法院": "KJF",
    # 最高法院
    "最高法院": "TPS",
    "最高行政法院": "TPA",
}


_ALL_COURT_CODES = {
    "TPD", "PCD", "SLD", "TYD", "SCD", "MLD", "TCD",
    "CHD", "NTD", "ULD", "CYD", "TND", "KSD", "PTD",
    "HLD", "TTD", "ILD", "KLD", "PHD", "KMD", "LCD",
    "CTD", "TPH", "KSH", "TCH", "TNH", "HLH",
    "TPAA", "TCAA", "KSAA", "TPA", "TPS", "IPC", "KJF",
}


def _resolve_court_code(text: str) -> str:
    """Resolve court name alias to code, with suffix stripping and 台→臺 normalization."""
    text = text.strip()
    # Direct code match
    up = text.upper()
    if up in _ALL_COURT_CODES:
        return up
    # Exact alias match
    if text in COURT_ALIASES:
        return COURT_ALIASES[text]
    # 台→臺 normalization then retry
    normalized = text.replace("台", "臺")
    if normalized in COURT_ALIASES:
        return COURT_ALIASES[normalized]
    # Strip common suffixes: "基隆地院" → "基隆" → KLD
    for suffix in ("地方法院", "地院", "法院", "高分院", "高等法院"):
        if text.endswith(suffix):
            core = text[:-len(suffix)]
            if core in COURT_ALIASES:
                return COURT_ALIASES[core]
            core_n = core.replace("台", "臺")
            if core_n in COURT_ALIASES:
                return COURT_ALIASES[core_n]
    return text


# ---------------------------------------------------------------------------
# 閱卷聲請確認碼 pending 管理
# ---------------------------------------------------------------------------
_REVIEW_PENDING_FILE = str(get_file_review_pending_path(_magi_root))


def _load_review_pending() -> dict:
    try:
        if os.path.exists(_REVIEW_PENDING_FILE):
            with open(_REVIEW_PENDING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def _save_review_pending(data: dict):
    try:
        os.makedirs(os.path.dirname(_REVIEW_PENDING_FILE) or ".", exist_ok=True)
        with open(_REVIEW_PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("無法儲存 review pending: %s", e)


def _register_review_confirm(case_info: dict, evidence: dict, paper: bool = False) -> str:
    """產生確認碼，儲存 pending 狀態，回傳 6 位 hex token。"""
    import secrets as _secrets
    import time as _time
    token = _secrets.token_hex(3).upper()
    ttl = int(os.environ.get("MAGI_FILE_REVIEW_CONFIRM_TTL_SEC", "1800") or "1800")
    now = _time.time()
    pending = _load_review_pending()
    # 清理過期的 pending
    expired = [k for k, v in pending.items() if isinstance(v, dict) and now > float(v.get("expires_at", 0) or 0)]
    for k in expired:
        pending.pop(k, None)
    # evidence 中的 screenshot 路徑保留，但移除不可序列化的內容
    safe_evidence = {}
    for ek, ev in (evidence or {}).items():
        if isinstance(ev, (str, int, float, bool, type(None))):
            safe_evidence[ek] = ev
    pending[token] = {
        "token": token,
        "case_info": case_info,
        "paper": paper,
        "evidence": safe_evidence,
        "created_at": now,
        "expires_at": now + ttl,
        "status": "pending",
    }
    _save_review_pending(pending)
    return token


def _resolve_review_confirm(token_str: str):
    """查找並消費確認碼。回傳 (token, entry) 或 (None, None)。"""
    import time as _time
    pending = _load_review_pending()
    tk = (token_str or "").strip().upper()
    import re as _re
    m = _re.search(r"([A-F0-9]{6,12})", tk)
    if m:
        tk = m.group(1)
    entry = pending.get(tk)
    if not entry or not isinstance(entry, dict):
        return None, None
    now = _time.time()
    if now > float(entry.get("expires_at", 0) or 0):
        pending.pop(tk, None)
        _save_review_pending(pending)
        return None, None
    if entry.get("status") != "pending":
        return None, None
    entry["status"] = "confirmed"
    entry["confirmed_at"] = now
    pending[tk] = entry
    _save_review_pending(pending)
    return tk, entry


# ---------------------------------------------------------------------------
# Core Commands
# ---------------------------------------------------------------------------
@_portal_serialized("apply")
def cmd_apply(court_code: str, year: str, case_type: str,
              case_number: str, client_name: str = "",
              auto_submit: bool = False, notify: bool = True,
              sys_type: str = "",
              folder_path: str = "",
              flow_id: str = "",
              skip_upload: bool = False,
              laf_only: bool = False) -> dict:
    """Apply for file review (閱卷聲請)."""
    if not all([court_code, year, case_type, case_number]):
        _safe_flow_step_status(flow_id, "preview_fill", status="failed", detail="missing required fields", ok=False)
        return {"success": False, "error": "missing required fields: court_code, year, case_type, case_number"}

    court_code = _resolve_court_code(court_code)
    if court_code.upper() not in _ALL_COURT_CODES:
        _safe_flow_step_status(flow_id, "preview_fill", status="failed", detail=f"unknown court_code: {court_code}", ok=False)
        return {"success": False, "error": f"無法識別法院名稱「{court_code}」，請使用如：基隆、台北、TPD 等格式"}
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        _safe_flow_step_status(flow_id, "portal_login", status="failed", detail="missing credentials", ok=False)
        return {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_EEFILE_USERNAME/PASSWORD in .env"}

    cancelled = _check_flow_cancelled(flow_id, "portal_login", detail="before login")
    if cancelled:
        return cancelled

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
            _safe_flow_step_status(flow_id, "portal_login", status="running", detail=f"{court_code} {year}-{case_type}-{case_number}")
            logger.info("Logging into SSO for file review...")
            if not mgr.login():
                msg = "❌ 閱卷登入失敗，可能驗證碼連錯或系統維護，已中斷自動聲請。"
                logger.error(msg)
                _notify(msg, notify)
                _safe_flow_step_status(flow_id, "portal_login", status="failed", detail="sso_login_failed", ok=False)
                _mark_notify_step(flow_id, notify=notify, detail=msg)
                return {"success": False, "error": "sso_login_failed"}
            _safe_flow_step_status(flow_id, "portal_login", status="succeeded", detail="SSO login ok", ok=True)

            mgr.navigate_to_file_review()

            cancelled = _check_flow_cancelled(flow_id, "preview_fill", detail="before apply_for_review")
            if cancelled:
                return cancelled

            # Apply
            case_info = {
                "court_code": court_code,
                "year": str(year),
                "case_type": case_type,
                "case_number": str(case_number),
                "client_name": client_name,
            }
            if sys_type:
                case_info["sys_type"] = str(sys_type).strip()
            if folder_path:
                case_info["folder_path"] = folder_path
            if skip_upload:
                case_info["skip_upload"] = True
            if laf_only:
                case_info["laf_only"] = True
            logger.info("Applying for review: %s", case_info)
            _safe_flow_step_status(flow_id, "preview_fill", status="running", detail=label if 'label' in locals() else f"{court_code} {year}-{case_type}-{case_number}")
            result = mgr.apply_for_review(case_info, auto_submit=auto_submit, skip_upload=skip_upload, laf_only=laf_only)

            label = f"{court_code} {year}年{case_type}字第{case_number}號"

            # Parse evidence from result (format: "Applied|{json}")
            evidence = {}
            fallback_evidence = getattr(mgr, "_last_apply_for_review_evidence", {}) or {}
            result_key = result
            if isinstance(result, str) and "|" in result:
                result_key, _, evidence_str = result.partition("|")
                try:
                    evidence = json.loads(evidence_str)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 867, exc_info=True)
            if not evidence and isinstance(fallback_evidence, dict):
                evidence = dict(fallback_evidence)

            if result_key == "Applied":
                app_no = evidence.get("application_number", "")
                msg = f"📋 閱卷聲請已送出 — {label}"
                if app_no:
                    msg += f"\n收件編號：{app_no}"
                if evidence.get("list_case_verified"):
                    msg += "\n列表確認：已找到本案聲請紀錄"
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail="application prepared and submitted", ok=True)
                _safe_flow_step_status(flow_id, "submit", status="succeeded", detail=result_key, ok=True)
            elif result_key == "Ready":
                # 產生確認碼，通知使用者截圖 + 確認碼
                confirm_token = _register_review_confirm(
                    case_info=case_info, evidence=evidence, paper=False,
                )
                msg = (
                    f"✅ 閱卷已填寫完成（待確認送出） — {label}"
                    f"\n\n📌 確認碼：{confirm_token}"
                    f"\n請確認截圖無誤後，回覆確認碼即可送出。"
                    f"\n（確認碼 30 分鐘內有效）"
                )
                evidence["confirm_token"] = confirm_token
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail="preview ready", ok=True)
                _safe_flow_step_status(flow_id, "submit", status="blocked", detail=f"confirm_token={confirm_token}", ok=True)
            elif result_key == "SubmitRejected":
                rejection = str(evidence.get("rejection_message") or "").strip()
                msg = f"❌ 閱卷聲請未送出 — {label}"
                if rejection:
                    msg += f"\n法院系統訊息：{rejection}"
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail=result_key, ok=True)
                _safe_flow_step_status(flow_id, "submit", status="failed", detail=rejection or result_key, ok=False)
            elif result_key == "SubmitUnverified":
                msg = (
                    f"❌ 閱卷聲請未確認送出 — {label}"
                    "\n未偵測到法院端「已受理」訊息，也未在列表精準找到本案；未視為已送出。"
                )
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail=result_key, ok=True)
                _safe_flow_step_status(flow_id, "submit", status="failed", detail=result_key, ok=False)
            else:
                msg = f"⚠️ 閱卷聲請結果: {result_key} — {label}"
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail=result_key, ok=True)
                _safe_flow_step_status(flow_id, "submit", status="failed", detail=result_key, ok=False)

            _notify(msg, notify, topic_key="filereview_apply")
            _mark_notify_step(flow_id, notify=notify, detail=msg)

            # Send evidence screenshot if available
            screenshot = evidence.get("screenshot", "")
            if screenshot and os.path.isfile(screenshot):
                _notify_file(screenshot, caption=f"閱卷預覽 — {label}", flag=notify,
                             topic_key="filereview_apply")
            list_screenshot = evidence.get("list_screenshot", "")
            if list_screenshot and os.path.isfile(list_screenshot):
                _notify_file(list_screenshot, caption=f"列表確認 — {label}", flag=notify,
                             topic_key="filereview_apply")
            html_path = evidence.get("html", "")
            if html_path and os.path.isfile(html_path):
                logger.info("預覽 HTML：%s", html_path)

            ok_result = result_key in {"Applied", "Ready"}
            response = {"success": ok_result, "result": result_key, "case": label,
                        "message": msg, "evidence": evidence}
            if not ok_result:
                response["error"] = msg
            return response

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Apply failed: %s", error_msg)
        _notify("❌ 閱卷聲請失敗: " + error_msg, notify, topic_key="filereview_apply")
        _safe_flow_step_status(flow_id, "submit", status="failed", detail=error_msg, ok=False)
        _mark_notify_step(flow_id, notify=notify, detail=error_msg)
        return {"success": False, "error": error_msg, "traceback": traceback.format_exc()[-500:]}


@_portal_serialized("paper_apply")
def cmd_paper_apply(court_code: str, year: str, case_type: str,
                    case_number: str, client_name: str = "",
                    appointment_date: str = "", appointment_time: str = "下午",
                    court_division: str = "",
                    appointment_slots: list = None,
                    auto_submit: bool = False, notify: bool = True,
                    sys_type: str = "",
                    folder_path: str = "",
                    flow_id: str = "") -> dict:
    """Apply for paper file review (紙本閱卷聲請)."""
    if not all([court_code, year, case_type, case_number]):
        _safe_flow_step_status(flow_id, "preview_fill", status="failed", detail="missing required fields", ok=False)
        return {"success": False, "error": "missing required fields: court_code, year, case_type, case_number"}

    court_code = _resolve_court_code(court_code)
    if court_code.upper() not in _ALL_COURT_CODES:
        _safe_flow_step_status(flow_id, "preview_fill", status="failed", detail=f"unknown court_code: {court_code}", ok=False)
        return {"success": False, "error": f"無法識別法院名稱「{court_code}」，請使用如：基隆、台北、TPD 等格式"}
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        _safe_flow_step_status(flow_id, "portal_login", status="failed", detail="missing credentials", ok=False)
        return {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_EEFILE_USERNAME/PASSWORD in .env"}

    cancelled = _check_flow_cancelled(flow_id, "portal_login", detail="before login")
    if cancelled:
        return cancelled

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        # 當事人自動補齊
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
            logger.warning("⚠️ 未提供當事人姓名，閱卷系統可能拒絕聲請。")

        mgr = mod.FileReviewManager(
            username=creds["username"],
            password=creds["password"],
            download_folder=creds["download_folder"],
            db_manager=db,
            headless=True,
            log_callback=lambda msg: logger.info(msg),
        )

        try:
            logger.info("Logging into SSO for paper file review...")
            _safe_flow_step_status(flow_id, "portal_login", status="running", detail=f"{court_code} {year}-{case_type}-{case_number}")
            if not mgr.login():
                msg = "❌ 紙本閱卷登入失敗，可能驗證碼連錯或系統維護。"
                logger.error(msg)
                _notify(msg, notify)
                _safe_flow_step_status(flow_id, "portal_login", status="failed", detail="sso_login_failed", ok=False)
                _mark_notify_step(flow_id, notify=notify, detail=msg)
                return {"success": False, "error": "sso_login_failed"}
            _safe_flow_step_status(flow_id, "portal_login", status="succeeded", detail="SSO login ok", ok=True)

            mgr.navigate_to_file_review()

            cancelled = _check_flow_cancelled(flow_id, "preview_fill", detail="before paper apply_for_review")
            if cancelled:
                return cancelled

            case_info = {
                "court_code": court_code,
                "year": str(year),
                "case_type": case_type,
                "case_number": str(case_number),
                "client_name": client_name,
                "appointment_date": appointment_date,
                "appointment_time": appointment_time,
                "court_division": court_division,
            }
            if sys_type:
                case_info["sys_type"] = str(sys_type).strip()
            if appointment_slots:
                case_info["appointment_slots"] = appointment_slots
            if folder_path:
                case_info["folder_path"] = folder_path
            logger.info("Applying for paper review: %s", case_info)
            _safe_flow_step_status(flow_id, "preview_fill", status="running", detail=f"{court_code} {year}-{case_type}-{case_number}")
            result = mgr.apply_for_review(case_info, auto_submit=auto_submit, paper_review=True)

            label = f"{court_code} {year}年{case_type}字第{case_number}號 (紙本)"
            if appointment_slots and len(appointment_slots) > 1:
                _slot_strs = [f"{s['date']} {s['time']}" for s in appointment_slots]
                appt_label = f"\n預約：{', '.join(_slot_strs)}"
            elif appointment_date:
                appt_label = f"\n預約：{appointment_date} {appointment_time}"
            else:
                appt_label = ""

            evidence = {}
            fallback_evidence = getattr(mgr, "_last_apply_for_review_evidence", {}) or {}
            result_key = result
            if isinstance(result, str) and "|" in result:
                result_key, _, evidence_str = result.partition("|")
                try:
                    evidence = json.loads(evidence_str)
                except Exception:
                    pass
            if not evidence and isinstance(fallback_evidence, dict):
                evidence = dict(fallback_evidence)

            if result_key == "Applied":
                app_no = evidence.get("application_number", "")
                msg = f"📋 紙本閱卷聲請已送出 — {label}{appt_label}"
                if app_no:
                    msg += f"\n收件編號：{app_no}"
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail="paper application prepared and submitted", ok=True)
                _safe_flow_step_status(flow_id, "submit", status="succeeded", detail=result_key, ok=True)
            elif result_key == "Ready":
                confirm_token = _register_review_confirm(
                    case_info=case_info, evidence=evidence, paper=True,
                )
                msg = (
                    f"✅ 紙本閱卷已填寫完成（待確認送出） — {label}{appt_label}"
                    f"\n\n📌 確認碼：{confirm_token}"
                    f"\n請確認截圖無誤後，回覆確認碼即可送出。"
                    f"\n（確認碼 30 分鐘內有效）"
                )
                evidence["confirm_token"] = confirm_token
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail="paper preview ready", ok=True)
                _safe_flow_step_status(flow_id, "submit", status="blocked", detail=f"confirm_token={confirm_token}", ok=True)
            elif result_key == "SubmitRejected":
                rejection = str(evidence.get("rejection_message") or "").strip()
                msg = f"❌ 紙本閱卷聲請未送出 — {label}{appt_label}"
                if rejection:
                    msg += f"\n法院系統訊息：{rejection}"
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail=result_key, ok=True)
                _safe_flow_step_status(flow_id, "submit", status="failed", detail=rejection or result_key, ok=False)
            elif result_key == "SubmitUnverified":
                msg = (
                    f"❌ 紙本閱卷聲請未確認送出 — {label}{appt_label}"
                    "\n未偵測到法院端「已受理」訊息，也未在列表精準找到本案；未視為已送出。"
                )
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail=result_key, ok=True)
                _safe_flow_step_status(flow_id, "submit", status="failed", detail=result_key, ok=False)
            else:
                msg = f"⚠️ 紙本閱卷聲請結果: {result_key} — {label}"
                _safe_flow_step_status(flow_id, "preview_fill", status="succeeded", detail=result_key, ok=True)
                _safe_flow_step_status(flow_id, "submit", status="failed", detail=result_key, ok=False)

            _notify(msg, notify, topic_key="filereview_apply")
            _mark_notify_step(flow_id, notify=notify, detail=msg)

            screenshot = evidence.get("screenshot", "")
            if screenshot and os.path.isfile(screenshot):
                _notify_file(screenshot, caption=f"紙本閱卷預覽 — {label}", flag=notify,
                             topic_key="filereview_apply")
            html_path = evidence.get("html", "")
            if html_path and os.path.isfile(html_path):
                logger.info("紙本閱卷預覽 HTML：%s", html_path)

            ok_result = result_key in {"Applied", "Ready"}
            response = {"success": ok_result, "result": result_key, "case": label,
                        "message": msg, "evidence": evidence}
            if not ok_result:
                response["error"] = msg
            return response

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Paper apply failed: %s", error_msg)
        _notify("❌ 紙本閱卷聲請失敗: " + error_msg, notify, topic_key="filereview_apply")
        _safe_flow_step_status(flow_id, "submit", status="failed", detail=error_msg, ok=False)
        _mark_notify_step(flow_id, notify=notify, detail=error_msg)
        return {"success": False, "error": error_msg, "traceback": traceback.format_exc()[-500:]}


@_portal_serialized("upload_attachment")
def cmd_upload_attachment(court_code: str, year: str, case_type: str,
                         case_number: str, client_name: str = "",
                         file_path: str = "", file_remark: str = "委任狀",
                         notify: bool = True) -> dict:
    """Upload attachment to an existing file review application."""
    if not all([court_code, year, case_type, case_number]):
        return {"success": False, "error": "missing required fields"}

    court_code = _resolve_court_code(court_code)
    if court_code.upper() not in _ALL_COURT_CODES:
        return {"success": False, "error": f"無法識別法院名稱「{court_code}」，請使用如：基隆、台北、TPD 等格式"}

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
def _payment_proof_upload_queue_path() -> Path:
    return PAYMENT_PROOF_UPLOAD_QUEUE_PATH or get_payment_proof_upload_queue_path()


def _payment_proof_queue_lock_path() -> Path:
    return Path(str(_payment_proof_upload_queue_path()) + ".lock")


def _payment_proof_queue_read_unlocked() -> dict:
    path = _payment_proof_upload_queue_path()
    if not path.exists():
        return {"version": 1, "jobs": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Payment-proof queue is unreadable; preserving fail-closed state", exc_info=True)
        return {"version": 1, "jobs": {}, "read_error": True}
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), dict):
        return {"version": 1, "jobs": {}, "read_error": True}
    return value


def _payment_proof_queue_write_unlocked(value: dict) -> None:
    path = _payment_proof_upload_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(value or {})
    payload["version"] = 1
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _with_payment_proof_queue_lock(callback):
    lock_path = _payment_proof_queue_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            return callback()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _payment_proof_file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _enqueue_payment_proof_upload(
    *,
    image_path: str,
    info: dict,
    case_hint: str = "",
) -> dict:
    """Persist a portal-busy upload before the channel removes its temp file."""
    source = Path(image_path)
    if not source.is_file():
        return {"success": False, "error": "payment_proof_source_missing"}

    file_sha = _payment_proof_file_sha256(str(source))
    raw_case_id = str(info.get("raw_case_id") or "").strip()
    event_id = _payment_proof_event_identity(info)
    job_id = hashlib.sha256(f"{file_sha}|{raw_case_id}|{event_id}".encode("utf-8")).hexdigest()[:24]
    suffix = source.suffix.lower() if source.suffix else ".png"
    store_dir = Path(PAYMENT_PROOF_UPLOAD_STORE_DIR)
    store_dir.mkdir(parents=True, exist_ok=True)
    stored_path = store_dir / f"{job_id}{suffix}"
    if not stored_path.exists():
        tmp = store_dir / f".{stored_path.name}.{os.getpid()}.tmp"
        shutil.copy2(source, tmp)
        if _payment_proof_file_sha256(str(tmp)) != file_sha:
            tmp.unlink(missing_ok=True)
            return {"success": False, "error": "payment_proof_copy_hash_mismatch"}
        os.replace(tmp, stored_path)

    now = time.time()

    def update() -> dict:
        queue = _payment_proof_queue_read_unlocked()
        if queue.get("read_error"):
            return {"success": False, "error": "payment_proof_queue_unreadable"}
        jobs = queue.setdefault("jobs", {})
        previous = jobs.get(job_id) if isinstance(jobs.get(job_id), dict) else {}
        jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "created_at": previous.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "next_attempt_at": min(float(previous.get("next_attempt_at") or now + 60), now + 60),
            "attempts": int(previous.get("attempts") or 0),
            "file_path": str(stored_path),
            "file_sha256": file_sha,
            "source_filename": source.name,
            "case_hint": str(case_hint or "")[:500],
            "court_code": str(info.get("court_code") or ""),
            "year": str(info.get("year") or ""),
            "case_type": str(info.get("case_type") or ""),
            "case_number": str(info.get("case_number") or ""),
            "client_name": str(info.get("client_name") or ""),
            "raw_case_id": raw_case_id,
            "payment_event_id": event_id,
            "last_reason": "file_review_portal_busy",
        }
        _payment_proof_queue_write_unlocked(queue)
        return {"success": True, "job_id": job_id, "file_path": str(stored_path)}

    return _with_payment_proof_queue_lock(update)


def _payment_proof_queue_snapshot() -> dict:
    return _with_payment_proof_queue_lock(_payment_proof_queue_read_unlocked)


def cmd_process_payment_proof_queue(notify: bool = True, max_items: int = 3) -> dict:
    """Retry due payment-proof uploads; one portal owner is still enforced below."""
    now = time.time()
    queue = _payment_proof_queue_snapshot()
    jobs = queue.get("jobs") if isinstance(queue.get("jobs"), dict) else {}
    due = [
        dict(job)
        for job in jobs.values()
        if isinstance(job, dict)
        and str(job.get("status") or "pending") == "pending"
        and float(job.get("next_attempt_at") or 0) <= now
    ]
    due.sort(key=lambda job: (float(job.get("next_attempt_at") or 0), str(job.get("created_at") or "")))
    due = due[:max(1, min(int(max_items or 1), 10))]
    results: List[dict] = []

    for job in due:
        job_id = str(job.get("job_id") or "")
        file_path = str(job.get("file_path") or "")
        if not file_path or not os.path.isfile(file_path):
            result = {"success": False, "error": "queued_file_missing"}
        elif _payment_proof_file_sha256(file_path) != str(job.get("file_sha256") or ""):
            result = {"success": False, "error": "queued_file_hash_mismatch"}
        else:
            result = cmd_upload_payment_proof(
                court_code=str(job.get("court_code") or ""),
                year=str(job.get("year") or ""),
                case_type=str(job.get("case_type") or ""),
                case_number=str(job.get("case_number") or ""),
                client_name=str(job.get("client_name") or ""),
                file_path=file_path,
                payment_event_id=str(job.get("payment_event_id") or ""),
                notify=False,
            )
        results.append({"job_id": job_id, **dict(result or {})})

        def update_result() -> None:
            current = _payment_proof_queue_read_unlocked()
            current_jobs = current.setdefault("jobs", {})
            entry = current_jobs.get(job_id)
            if not isinstance(entry, dict):
                return
            terminal = bool(result.get("success")) and (
                (
                    str(result.get("result") or "") == "Uploaded"
                    and result.get("proof_receipt_committed") is True
                )
                or (
                    str(result.get("result") or "") == "exact_duplicate_verified"
                    and result.get("proof_schema") == PAYMENT_PROOF_SCHEMA
                    and bool(result.get("proof_sha256"))
                    and result.get("proof_receipt_committed") is True
                )
            )
            if terminal:
                current_jobs.pop(job_id, None)
                _payment_proof_queue_write_unlocked(current)
                try:
                    Path(file_path).unlink(missing_ok=True)
                except Exception:
                    logger.warning("Unable to remove completed payment-proof queue file", exc_info=True)
                return
            attempts = int(entry.get("attempts") or 0) + (0 if result.get("deferred") else 1)
            entry["attempts"] = attempts
            entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
            entry["last_reason"] = str(result.get("reason") or result.get("error") or "retry_failed")[:200]
            if attempts >= 12:
                entry["status"] = "needs_attention"
            else:
                delay = 120 if result.get("deferred") else min(3600, 60 * (2 ** min(attempts, 6)))
                entry["next_attempt_at"] = time.time() + delay
            current_jobs[job_id] = entry
            _payment_proof_queue_write_unlocked(current)

        _with_payment_proof_queue_lock(update_result)

        if bool(result.get("success")) and (
            (
                str(result.get("result") or "") == "Uploaded"
                and result.get("proof_receipt_committed") is True
            )
            or (
                str(result.get("result") or "") == "exact_duplicate_verified"
                and result.get("proof_schema") == PAYMENT_PROOF_SCHEMA
                and bool(result.get("proof_sha256"))
                and result.get("proof_receipt_committed") is True
            )
        ):
            _notify(str(result.get("message") or "✅ 繳費憑證已自動上傳。"), notify, topic_key="filereview_payment")
        elif result.get("deferred"):
            break

    after = _payment_proof_queue_snapshot()
    after_jobs = after.get("jobs") if isinstance(after.get("jobs"), dict) else {}
    pending_count = sum(1 for job in after_jobs.values() if isinstance(job, dict) and job.get("status") == "pending")
    needs_attention_count = sum(1 for job in after_jobs.values() if isinstance(job, dict) and job.get("status") == "needs_attention")
    return {
        "success": needs_attention_count == 0,
        "processed_count": len(results),
        "pending_count": pending_count,
        "needs_attention_count": needs_attention_count,
        "results": results,
    }


@_portal_serialized("upload_payment_proof")
def cmd_upload_payment_proof(court_code: str, year: str, case_type: str,
                             case_number: str, client_name: str = "",
                             file_path: str = "", notify: bool = True,
                             payment_event_id: str = "") -> dict:
    """Upload payment proof screenshot to an existing file review application."""
    if not all([court_code, year, case_type, case_number]):
        return {"success": False, "error": "missing required fields"}

    court_code = _resolve_court_code(court_code)
    if court_code.upper() not in _ALL_COURT_CODES:
        return {"success": False, "error": f"無法識別法院名稱「{court_code}」，請使用如：基隆、台北、TPD 等格式"}

    if not file_path or not os.path.exists(file_path):
        return {"success": False, "error": f"file not found: {file_path}"}

    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        return {"success": False, "error": "missing credentials"}

    # 去重檢查
    case_num_padded = str(case_number).zfill(6)
    raw_case_id = f"{year}.{case_type}.{case_num_padded}"
    file_sha256 = _payment_proof_file_sha256(file_path)
    event_id = _payment_proof_event_identity(explicit=payment_event_id)
    dedup_key = _payment_proof_dedup_key(raw_case_id, file_sha256, event_id)
    registry_path = str(get_payment_proof_registry_path(creds.get("download_folder", "./閱卷下載")))
    proof_registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as _rf:
                proof_registry = json.load(_rf)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1011, exc_info=True)

    # Legacy case-only rows (and legacy DB keys) are not proof identity.
    _proof_already_done = _payment_proof_registry_matches(
        proof_registry, raw_case_id, file_sha256, event_id
    )
    if not _proof_already_done:
        try:
            from skills.ops.dedup_db import is_done as _dd_is_done
            _proof_already_done = _dd_is_done("payment_proof", dedup_key)
        except Exception:
            pass
    if _proof_already_done:
        msg = f"ℹ️ {raw_case_id} 此繳費憑證已上傳過，跳過"
        logger.info(msg)
        _notify(msg, notify, topic_key="filereview_payment")
        return {
            "success": True,
            "result": "exact_duplicate_verified",
            "proof_schema": PAYMENT_PROOF_SCHEMA,
            "proof_sha256": file_sha256,
            "proof_receipt_committed": True,
            "message": msg,
        }

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
                _notify(msg, notify, topic_key="filereview_payment")
                return {"success": False, "error": "sso_login_failed"}

            mgr.navigate_to_file_review()

            case_info = {
                "court_code": court_code,
                "year": str(year),
                "case_type": case_type,
                "case_number": str(case_number),
                "client_name": client_name,
                "payment_event_id": event_id,
                "pay_id": event_id,
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
                _payment_proof_registry_upsert(proof_registry, raw_case_id, {
                    "proof_schema": PAYMENT_PROOF_SCHEMA,
                    "uploaded_at": _dt.now().isoformat(),
                    "court_code": court_code,
                    "file": os.path.basename(file_path),
                    "file_sha256": file_sha256,
                    "payment_event_id": event_id,
                })
                try:
                    _write_payment_proof_registry_atomic(registry_path, proof_registry)
                except Exception as exc:
                    logging.getLogger(__name__).warning("payment proof registry commit failed: %s", exc)
                    msg = "❌ 繳費憑證已送出但 terminal proof 未落盤，保留待 reconciliation"
                    _notify(msg, notify, topic_key="filereview_payment")
                    return {
                        "success": False,
                        "result": "registry_not_committed",
                        "deferred": True,
                        "reason": "payment_proof_registry_write_failed",
                        "proof_receipt_committed": False,
                        "message": msg,
                    }
                # DB dedup sync
                try:
                    from skills.ops.dedup_db import mark_done as _dd_mark
                    _dd_mark("payment_proof", dedup_key, metadata={
                        "court_code": court_code, "file": os.path.basename(file_path),
                        "file_sha256": file_sha256, "payment_event_id": event_id,
                        "proof_schema": PAYMENT_PROOF_SCHEMA, "source": "cmd_upload_payment_proof",
                    })
                except Exception:
                    pass
            elif result == "NotFound":
                msg = f"⚠️ 找不到案件 — {label}"
            else:
                msg = f"❌ 繳費憑證上傳失敗 — {label} (結果: {result})"

            _notify(msg, notify, topic_key="filereview_payment")
            return {"success": result == "Uploaded", "result": result,
                    "proof_receipt_committed": result == "Uploaded",
                    "case": label, "message": msg}

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Upload payment proof failed: %s", error_msg)
        _notify("❌ 繳費憑證上傳失敗: " + error_msg, notify, topic_key="filereview_payment")
        return {"success": False, "error": error_msg,
                "traceback": traceback.format_exc()[-500:]}


@_portal_serialized("upload_payment_proofs_batch")
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
        _notify(msg, notify, topic_key="filereview_payment")
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
        _notify(msg, notify, topic_key="filereview_payment")
        return {"success": False, "error": "no parseable screenshots",
                "candidates": len(candidates), "message": msg}

    # ── 去重: 載入已上傳記錄 ──
    cfg = _load_config()
    creds = _get_credentials(cfg)
    registry_path = str(get_payment_proof_registry_path(creds.get("download_folder", "./閱卷下載")))
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
        file_sha256 = _payment_proof_file_sha256(str(p.get("file_path") or ""))
        event_id = _payment_proof_event_identity(p)
        _already = _payment_proof_registry_matches(proof_registry, key, file_sha256, event_id)
        if not _already and key:
            try:
                from skills.ops.dedup_db import is_done as _dd_is_done
                _already = _dd_is_done("payment_proof", _payment_proof_dedup_key(key, file_sha256, event_id))
            except Exception:
                pass
        if _already:
            logger.info("  ⏭ 跳過已上傳: %s (上傳於 %s)", key, proof_registry.get(key, {}).get("uploaded_at", "?"))
        else:
            new_list.append(p)

    if not new_list and parsed_list:
        msg = f"ℹ️ {len(parsed_list)} 筆繳費憑證皆已上傳過，無需重複操作"
        _notify(msg, notify, topic_key="filereview_payment")
        return {"success": True, "skipped": len(parsed_list), "message": msg}

    parsed_list = new_list

    # 通知解析結果
    summary_lines = [f"📋 解析到 {len(parsed_list)} 筆繳費憑證:"]
    for p in parsed_list:
        summary_lines.append(
            f"  • {p['raw_case_id']} ({p['court_name']}) ${p.get('amount', '?')}"
        )
    _notify("\n".join(summary_lines), notify, topic_key="filereview_payment")

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
            _notify(msg, notify, topic_key="filereview_payment")
            return {"success": False, "error": "sso_login_failed"}

        mgr.navigate_to_file_review()

        for p in parsed_list:
            case_info = {
                "court_code": p["court_code"],
                "year": p["year"],
                "case_type": p["case_type"],
                "case_number": p["case_number"],
                "payment_event_id": _payment_proof_event_identity(p),
                "pay_id": str(p.get("pay_id") or ""),
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
                # 記錄到 registry 避免重複上傳
                from datetime import datetime as _dt
                proof_sha256 = _payment_proof_file_sha256(str(p["file_path"]))
                _payment_proof_registry_upsert(proof_registry, p["raw_case_id"], {
                    "proof_schema": PAYMENT_PROOF_SCHEMA,
                    "uploaded_at": _dt.now().isoformat(),
                    "court_code": p["court_code"],
                    "court_name": p["court_name"],
                    "file": os.path.basename(p["file_path"]),
                    "amount": p.get("amount", ""),
                    "file_sha256": proof_sha256,
                    "payment_event_id": _payment_proof_event_identity(p),
                })
                proof_receipt_committed = False
                try:
                    _write_payment_proof_registry_atomic(registry_path, proof_registry)
                    proof_receipt_committed = True
                except Exception as exc:
                    logging.getLogger(__name__).warning("payment proof batch registry commit failed: %s", exc)
                    results[-1]["result"] = "registry_not_committed"
                    results[-1]["proof_receipt_committed"] = False
                    continue
                results[-1]["proof_receipt_committed"] = proof_receipt_committed
                _notify(f"✅ 繳費憑證已上傳 — {label}", notify, topic_key="filereview_payment")
                # DB dedup sync
                try:
                    from skills.ops.dedup_db import mark_done as _dd_mark
                    _dd_mark("payment_proof", _payment_proof_dedup_key(
                        p["raw_case_id"], proof_sha256, _payment_proof_event_identity(p)
                    ), metadata={
                        "court_code": p["court_code"], "court_name": p["court_name"],
                        "file": os.path.basename(p["file_path"]),
                        "file_sha256": proof_sha256,
                        "payment_event_id": _payment_proof_event_identity(p),
                        "proof_schema": PAYMENT_PROOF_SCHEMA, "source": "cmd_upload_payment_proofs_batch",
                    })
                except Exception:
                    pass
            elif result == "NotFound":
                _notify(f"⚠️ 找不到案件 — {label}", notify, topic_key="filereview_payment")
            else:
                _notify(f"❌ 繳費憑證上傳失敗 — {label}", notify, topic_key="filereview_payment")

            import time as _time
            _time.sleep(2)  # 上傳間隔

    finally:
        mgr.close()

    uploaded = sum(1 for r in results if r["result"] == "Uploaded")
    total = len(results)
    final_msg = f"📊 繳費憑證批次上傳完成: {uploaded}/{total} 成功"
    _notify(final_msg, notify, topic_key="filereview_payment")

    return {
        "success": uploaded > 0,
        "uploaded": uploaded,
        "total": total,
        "results": results,
        "message": final_msg,
    }


def cmd_upload_payment_proof_from_image(
    image_path: str,
    notify: bool = True,
    case_hint: str = "",
) -> dict:
    """
    從通道（LINE/DC/TG）傳來的繳費截圖，自動解析案號並上傳至 OLA。

    流程:
    1. parse_payment_screenshot 解析截圖
    2. 去重檢查 (registry)
    3. cmd_upload_payment_proof 上傳
    """
    if not image_path or not os.path.exists(image_path):
        msg = "⚠️ 找不到繳費截圖檔案"
        _notify(msg, notify, topic_key="filereview_payment")
        return {"success": False, "error": "file not found", "message": msg}

    try:
        mod = _ensure_imports()
    except Exception as e:
        msg = f"❌ 載入閱卷模組失敗：{e}"
        _notify(msg, notify, topic_key="filereview_payment")
        return {"success": False, "error": str(e), "message": msg}

    # Step 1: 解析截圖
    logger.info("💰 Parsing payment screenshot from channel: %s", image_path)
    image_info = mod.FileReviewManager.parse_payment_screenshot(image_path) or {}
    hint_info = {}
    if str(case_hint or "").strip():
        hint_info = mod.FileReviewManager._parse_payment_text(
            str(case_hint),
            require_payment_context=False,
        ) or {}

    # 圖片與訊息若都含案號，兩者必須一致；衝突時 fail closed，避免上傳到別案。
    image_case = str(image_info.get("raw_case_id") or "").strip()
    hint_case = str(hint_info.get("raw_case_id") or "").strip()
    if image_case and hint_case and image_case != hint_case:
        msg = (
            "⚠️ 截圖與訊息中的案號不一致，為避免上傳到錯誤案件，已停止處理。\n"
            "請確認法院與案號後重新上傳。"
        )
        _notify(msg, notify, topic_key="filereview_payment")
        return {"success": False, "error": "case_hint_conflict", "message": msg}

    info = dict(image_info)
    for field in (
        "year", "case_type", "case_number", "court_name", "court_code",
        "raw_case_id", "amount", "payer",
    ):
        if not info.get(field) and hint_info.get(field):
            info[field] = hint_info[field]

    required = ("court_code", "year", "case_type", "case_number")
    if not all(info.get(field) for field in required):
        msg = (
            "⚠️ 已辨識為繳費憑證，但圖片中缺少可安全配對的法院或案號，"
            "因此沒有上傳。\n"
            "請在上傳訊息中一併註明法院與完整案號，例如："
            "「臺北地院 115年度訴字第123號 繳費」。"
        )
        _notify(msg, notify, topic_key="filereview_payment")
        return {
            "success": False,
            "error": "case_identity_incomplete",
            "message": msg,
            "missing_fields": [field for field in required if not info.get(field)],
        }

    court_code = info["court_code"]
    year = info["year"]
    case_type = info["case_type"]
    case_number = info["case_number"]
    raw_case_id = info.get("raw_case_id", f"{year}.{case_type}.{str(case_number).zfill(6)}")
    court_name = info.get("court_name", court_code)
    amount = info.get("amount", "?")

    logger.info("💰 Parsed: %s (%s) $%s", raw_case_id, court_name, amount)
    _notify(
        f"💰 解析繳費截圖: {raw_case_id} ({court_name}) ${amount}，開始上傳⋯",
        notify,
        topic_key="filereview_payment",
    )

    # Step 2: 呼叫現有的單件上傳 (含去重 + OLA 登入 + 上傳)
    upload_result = cmd_upload_payment_proof(
        court_code=court_code,
        year=year,
        case_type=case_type,
        case_number=case_number,
        file_path=image_path,
        notify=notify,
        payment_event_id=_payment_proof_event_identity(info),
    )
    if upload_result.get("deferred") and upload_result.get("reason") == "file_review_portal_busy":
        queued = _enqueue_payment_proof_upload(
            image_path=image_path,
            info=info,
            case_hint=case_hint,
        )
        if queued.get("success"):
            msg = (
                "📥 法院入口目前由其他作業使用，這張繳費憑證已加入自動上傳佇列。\n"
                "MAGI 會在入口釋放後自動重試，完成時通知；您不需要重新上傳。"
            )
            return {
                "success": True,
                "ok": True,
                "status": "queued",
                "queued": True,
                "deferred": True,
                "reason": "file_review_portal_busy",
                "job_id": queued.get("job_id"),
                "message": msg,
            }
        return {
            "success": False,
            "status": "queue_failed",
            "error": str(queued.get("error") or "payment_proof_queue_failed"),
            "message": "❌ 法院入口忙碌，且本次未能安全儲存繳費憑證；請保留原圖並稍後重試。",
        }
    return upload_result


@_portal_serialized("download_payment_slips")
def cmd_download_payment_slips(max_days: int = 14, notify: bool = True, target_case_number: str = "") -> dict:
    """Download all pending payment slip PDFs and send via TG."""
    if _scheduled_check_fixture_provider() is not None:
        return _fixture_download_payment_slips()
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

            results = mgr.download_all_payment_slips(
                max_days=max_days,
                target_case_number=target_case_number or None,
            )

            # Collect PDF paths.  Existing files still need delivery if the
            # previous run downloaded them but failed to send the PDF.
            pdf_paths = []
            captions_by_path: Dict[str, str] = {}
            notice_keys_by_path: Dict[str, Iterable[str]] = {}
            for r in results:
                # 使用 all_paths 取得全部檔案，fallback 到 pdf_path
                paths = r.get("all_paths") or []
                if not paths:
                    p = r.get("pdf_path", "")
                    if p:
                        paths = [p]
                valid_paths = [path for path in paths if _is_valid_payment_pdf_file(path)]
                if r.get("already_existed"):
                    valid_paths = [
                        path for path in valid_paths
                        if not _payment_file_already_delivered(path, creds["download_folder"])
                    ]
                party = r.get("party") or ""
                case_no = r.get("case_number") or ""
                label = f"{party}｜{case_no}" if (party or case_no) else ""
                notice_keys = _portal_payment_notice_keys({
                    "case_number": case_no,
                    "court_case_no": case_no,
                    "party": party,
                    "rowid": r.get("rowid") or "",
                    "payid": r.get("payid") or r.get("p_payid") or "",
                })
                for path in paths:
                    if path in valid_paths:
                        pdf_paths.append(path)
                        captions_by_path[path] = label or os.path.basename(path)
                        if r.get("already_existed") and notice_keys:
                            notice_keys_by_path[path] = notice_keys

            if pdf_paths:
                summary_lines = [f"💰 繳費單 PDF 下載完成（{len(pdf_paths)} 件）："]
                display_labels: List[str] = []
                seen_labels: Set[str] = set()
                for path in pdf_paths:
                    label = captions_by_path.get(path) or os.path.basename(path)
                    if label and label not in seen_labels:
                        seen_labels.add(label)
                        display_labels.append(label)
                for i, label in enumerate(display_labels, 1):
                    summary_lines.append(f"  {i}. {label}")

                msg = "\n".join(summary_lines)

                # Each PDF caption is the user-facing notification. Do not send
                # a separate summary first, or the same payment slip appears
                # twice with different wording.
                delivery = _send_payment_pdf_files(
                    pdf_paths,
                    download_folder=creds["download_folder"],
                    caption_prefix="💰 繳費單 PDF 下載完成",
                    notify=notify,
                    captions_by_path=captions_by_path,
                    notice_keys_by_path=notice_keys_by_path,
                )
                failed = int(delivery.get("failed") or 0)
                sent = int(delivery.get("sent") or 0)
                success = failed == 0

                return {
                    "success": success,
                    "count": len(pdf_paths),
                    "pdf_paths": pdf_paths,
                    "cases": display_labels,
                    "delivery": delivery,
                    "sent": sent,
                    "failed": failed,
                    "error": "payment_pdf_delivery_failed" if failed else "",
                    "message": msg,
                }
            else:
                msg = "ℹ️ 無待下載繳費單（可能全部已處理或無待繳費案件）"
                logger.info("Payment slip download found no pending PDFs; notification suppressed.")
                return {
                    "success": True,
                    "count": 0,
                    "pdf_paths": [],
                    "delivery": {
                        "sent": 0,
                        "failed": 0,
                        "suppressed_noop": True,
                    },
                    "sent": 0,
                    "failed": 0,
                    "message": msg,
                }

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Download payment slips failed: %s", error_msg)
        _notify("❌ 繳費單下載失敗: " + error_msg, notify)
        return {"success": False, "error": error_msg, "traceback": traceback.format_exc()[-500:]}


def _scheduled_check_fixture_provider() -> Tuple[Path, Path, dict] | None:
    """Load the bounded provider without manufacturing command results."""
    raw_path = str(os.environ.get("MAGI_FILE_REVIEW_SCHEDULE_FIXTURE_PATH") or "").strip()
    if not raw_path:
        return None
    fixture_raw = str(os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT") or "").strip()
    if (
        os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") != "real_entrypoint_fixture_v1"
        or os.environ.get("MAGI_V3_SCHEDULE_DRY_RUN") != "1"
        or not fixture_raw
    ):
        raise RuntimeError("file-review schedule fixture is not safely bound")
    fixture = Path(fixture_raw).expanduser().resolve()
    provider_path = Path(raw_path).expanduser().resolve()
    if (
        not (fixture / ".magi-v3-schedule-fixture").is_file()
        or not provider_path.is_file()
        or not provider_path.is_relative_to(fixture)
    ):
        raise RuntimeError("file-review schedule fixture escaped its owned root")
    try:
        provider = json.loads(provider_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("file-review schedule fixture is unreadable") from exc
    if provider.get("schema") != "magi.v3.file-review-scheduled-fixture/v1":
        raise RuntimeError("file-review schedule fixture schema is invalid")
    emails = provider.get("emails")
    portal_files = provider.get("portal_files")
    if not isinstance(emails, list) or not isinstance(portal_files, list):
        raise RuntimeError("file-review schedule fixture lists are invalid")
    if len(emails) > 10 or len(portal_files) > 10:
        raise RuntimeError("file-review schedule fixture exceeded its bound")
    return fixture, provider_path, provider


def _fixture_step_receipt(step: str, handler: str, evidence: dict) -> dict:
    loaded = _scheduled_check_fixture_provider()
    if loaded is None:
        raise RuntimeError("file-review receipt requires the bounded provider")
    fixture, provider_path, _provider = loaded
    created_at = datetime.now().isoformat(timespec="microseconds")
    nonce = uuid.uuid4().hex
    input_sha256 = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema": "magi.file-review-step-receipt/v1",
        "receipt_id": hashlib.sha256(
            f"{step}:{handler}:{created_at}:{os.getpid()}:{nonce}:{input_sha256}".encode()
        ).hexdigest(),
        "step": step,
        "handler": handler,
        "created_at": created_at,
        "pid": os.getpid(),
        "nonce": nonce,
        "input_sha256": input_sha256,
        "provider_sha256": hashlib.sha256(provider_path.read_bytes()).hexdigest(),
    }
    receipt_dir = fixture / "state" / "formal-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    target = receipt_dir / f"{step}-{nonce}.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        **payload,
        "receipt_path": target.relative_to(fixture).as_posix(),
        "receipt_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }


def _invoke_scheduled_formal_handler(
    step: str, handler: Callable[..., dict], **kwargs
) -> dict:
    expected = {
        "check_emails": "cmd_check_emails",
        "download_payment_slips": "cmd_download_payment_slips",
        "download": "cmd_download_background",
    }.get(step)
    if _scheduled_check_fixture_provider() is not None and (
        not expected
        or getattr(handler, "__module__", "") != __name__
        or getattr(handler, "__name__", "") != expected
    ):
        raise RuntimeError(f"file-review formal handler rejected for {step}")
    result = handler(**kwargs)
    if not isinstance(result, dict):
        raise RuntimeError(f"file-review formal handler returned invalid result for {step}")
    return result


def _publish_scheduled_check_state(result: dict) -> None:
    """Publish every formal LIVE portal check to the shared readiness state.

    The hourly scheduler and the always-on worker share one court-portal lock.
    Previously, only the worker wrote ``file_review_auto_state.json``.  When
    the hourly scheduler legitimately owned the portal, the worker recorded a
    benign ``download_already_running`` observation and the UI lost the actual
    verified portal result.  Publishing the formal result at its source keeps
    health, notifications, and download readiness in sync regardless of which
    authorized owner performed the scan.
    """
    explicit = str(os.environ.get("MAGI_FILE_REVIEW_AUTO_STATE") or "").strip()
    mutable_static = str(os.environ.get("MAGI_MUTABLE_STATIC_DIR") or "").strip()
    if not explicit and not mutable_static:
        return
    path = Path(explicit).expanduser() if explicit else Path(mutable_static).expanduser() / "file_review_auto_state.json"
    steps = result.get("steps") if isinstance(result.get("steps"), dict) else {}
    email_step = steps.get("check_emails") if isinstance(steps.get("check_emails"), dict) else {}
    payment_step = steps.get("download_payment_slips") if isinstance(steps.get("download_payment_slips"), dict) else {}
    download_step = steps.get("download") if isinstance(steps.get("download"), dict) else {}
    canonical = {
        "ok": bool(result.get("success")),
        "degraded": bool(result.get("deferred")),
        "skipped": bool(result.get("skipped")),
        "reason": str(result.get("status") or "scheduled_check"),
        "check": {
            "ok": email_step.get("success") is not False,
            "parsed": email_step,
        },
        "payment_slips": payment_step,
        "download": download_step,
        "portal_verified": bool(email_step.get("portal_probe_ok")),
        "portal_probe_deferred": bool(email_step.get("portal_probe_deferred")),
        "portal_raw_row_count": int(email_step.get("portal_raw_row_count") or 0),
        "portal_case_count": int(email_step.get("portal_case_count") or 0),
        "ready_to_download_count": int(email_step.get("ready_to_download_count") or 0),
    }
    try:
        previous: dict = {}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        update = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "phase": "scheduled_check_complete",
        }
        prior_result = previous.get("result") if isinstance(previous.get("result"), dict) else {}
        preserve_verified_proof = (
            bool(result.get("success"))
            and bool(result.get("deferred"))
            and bool(email_step.get("portal_probe_deferred"))
            and bool(prior_result.get("portal_verified"))
        )
        if preserve_verified_proof:
            # This run observed that another owned portal task has the lock; it
            # did not perform a new portal scan. Keep the last authoritative
            # proof as readiness truth and publish the defer separately.
            update["last_observation"] = canonical
        else:
            update["result"] = canonical
        previous.update(update)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        logger.exception("failed to publish scheduled file-review readiness state")


def _fresh_file_review_auto_owner_state(max_age_seconds: int = 1800) -> dict:
    """Return verified state for the single unattended FileReview owner.

    The always-on worker is the primary owner of Gmail, court-portal and
    download work.  Cron entries are recovery owners only: when this state is
    fresh and its PID is alive they must not open a second Gmail/Chromium
    session.  A missing, stale or dead owner deliberately returns ``fresh``
    false so the scheduled fallback still performs the complete check.
    """
    explicit = str(os.environ.get("MAGI_FILE_REVIEW_AUTO_STATE") or "").strip()
    mutable_static = str(os.environ.get("MAGI_MUTABLE_STATIC_DIR") or "").strip()
    if not explicit and not mutable_static:
        return {"fresh": False, "reason": "owner_state_path_unavailable"}
    path = (
        Path(explicit).expanduser()
        if explicit
        else Path(mutable_static).expanduser() / "file_review_auto_state.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid owner state")
        updated_at = datetime.fromisoformat(str(payload.get("updated_at") or ""))
        age_seconds = max(0, int((datetime.now(updated_at.tzinfo) - updated_at).total_seconds()))
        pid = int(payload.get("pid") or 0)
        if pid <= 0:
            return {"fresh": False, "reason": "owner_pid_missing", "age_seconds": age_seconds}
        os.kill(pid, 0)
        phase = str(payload.get("phase") or "")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        terminal_phase = phase in {"cycle_complete", "scheduled_check_complete", "failed", "stopped"}
        terminal_healthy = not terminal_phase or result.get("ok") is not False
        fresh = (
            age_seconds <= max(300, int(max_age_seconds))
            and phase not in {"failed", "stopped"}
            and terminal_healthy
        )
        return {
            "fresh": bool(fresh),
            "reason": (
                "owner_active"
                if fresh
                else ("owner_unhealthy" if not terminal_healthy else "owner_state_stale")
            ),
            "age_seconds": age_seconds,
            "pid": pid,
            "phase": phase,
            "portal_verified": bool(result.get("portal_verified")),
            "portal_probe_deferred": bool(result.get("portal_probe_deferred")),
            "downloaded_count": int(result.get("downloaded_count") or 0),
        }
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"fresh": False, "reason": "owner_state_unverified"}


def _fixture_check_emails() -> dict:
    loaded = _scheduled_check_fixture_provider()
    if loaded is None:
        raise RuntimeError("file-review email provider is unavailable")
    _fixture, _provider_path, provider = loaded
    emails = provider["emails"]
    downloadable = [
        item for item in emails
        if isinstance(item, dict) and item.get("kind") == "downloadable"
    ]
    ignored = [
        item for item in emails
        if isinstance(item, dict) and item.get("kind") != "downloadable"
    ]
    portal_rows = [
        {
            "status": "downloadable",
            "rowid": f"fixture-row-{index}",
            "upddt": "fixture-revision-v1",
        }
        for index, _raw in enumerate(provider["portal_files"])
    ]
    portal_receipt = portal_download_snapshot(portal_rows)
    receipt = _fixture_step_receipt(
        "check_emails",
        "cmd_check_emails",
        {"emails": emails, "downloadable": len(downloadable), "ignored": len(ignored)},
    )
    return {
        "success": True,
        "matched": len(downloadable),
        "ignored": len(ignored),
        "downloadable_case_numbers": [
            str(item.get("case_number") or "") for item in downloadable
        ],
        "ignored_kinds": [str(item.get("kind") or "") for item in ignored],
        "willingness_inquiries_excluded": sum(
            item.get("kind") == "willingness_inquiry" for item in ignored
        ),
        "portal_probe_ok": True,
        "portal_probe_deferred": False,
        "portal_pending_payment_count": 0,
        "portal_downloadable_count": len(portal_rows),
        "ready_to_download_count": 0,
        "download_hits": 0,
        **portal_receipt,
        "provider": "fixture_email_provider",
        "execution_receipt": receipt,
    }


def _fixture_download_payment_slips() -> dict:
    loaded = _scheduled_check_fixture_provider()
    if loaded is None:
        raise RuntimeError("file-review payment provider is unavailable")
    _fixture, _provider_path, provider = loaded
    payment_slips = provider.get("payment_slips") or []
    if not isinstance(payment_slips, list) or len(payment_slips) > 10:
        raise RuntimeError("file-review payment fixture exceeded its bound")
    receipt = _fixture_step_receipt(
        "download_payment_slips",
        "cmd_download_payment_slips",
        {"payment_slips": payment_slips},
    )
    return {
        "success": True,
        "count": 0,
        "pdf_paths": [],
        "provider": "fixture_payment_portal_provider",
        "execution_receipt": receipt,
    }


def _fixture_download_portal_files() -> dict:
    loaded = _scheduled_check_fixture_provider()
    if loaded is None:
        raise RuntimeError("file-review download provider is unavailable")
    fixture, _provider_path, provider = loaded
    unique_hashes: Dict[str, str] = {}
    duplicate_files: List[str] = []
    files: List[str] = []
    downloaded = fixture / "state" / "downloads"
    downloaded.mkdir(parents=True, exist_ok=True)
    for raw in provider["portal_files"]:
        path = (fixture / str(raw)).resolve()
        if not path.is_file() or path.is_symlink() or not path.is_relative_to(fixture):
            raise RuntimeError("file-review schedule fixture file is invalid")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(fixture).as_posix()
        if digest in unique_hashes:
            duplicate_files.append(relative)
            continue
        unique_hashes[digest] = relative
        target = downloaded / path.name
        shutil.copy2(path, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError("file-review fixture download hash changed")
        files.append(target.relative_to(fixture).as_posix())
    receipt = _fixture_step_receipt(
        "download",
        "cmd_download",
        {
            "portal_files": provider["portal_files"],
            "unique_hashes": sorted(unique_hashes),
            "duplicates": duplicate_files,
        },
    )
    portal_rows = [
        {
            "status": "downloadable",
            "rowid": f"fixture-row-{index}",
            "upddt": "fixture-revision-v1",
        }
        for index, _raw in enumerate(provider["portal_files"])
    ]
    processed_signatures = portal_download_snapshot(portal_rows)[
        "portal_download_signature_hashes"
    ]
    return {
        "success": True,
        "downloaded_count": len(files),
        "duplicate_count": len(duplicate_files),
        "files": files,
        "duplicates": duplicate_files,
        "content_hashes": sorted(unique_hashes),
        "portal_download_receipt_schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
        "processed_portal_signature_hashes": processed_signatures,
        "processed_portal_signature_set_hash": signature_set_hash(processed_signatures),
        "verified_existing_portal_signature_hashes": [],
        "verified_existing_portal_signature_set_hash": signature_set_hash([]),
        "handled_portal_signature_hashes": processed_signatures,
        "handled_portal_signature_set_hash": signature_set_hash(processed_signatures),
        "provider": "fixture_review_portal_provider",
        "execution_receipt": receipt,
    }


def _wait_fixture_download_terminal(queued: dict, timeout_sec: float = 30.0) -> dict:
    job_id = str(queued.get("job_id") or "")
    pid = int(queued.get("pid") or 0)
    if not job_id or pid <= 1:
        raise RuntimeError("file-review fixture background child was not started")
    clock = __import__("time")
    deadline = clock.monotonic() + max(1.0, timeout_sec)
    terminal = {"done", "failed", "cancelled", "deferred", "stopped"}
    while clock.monotonic() < deadline:
        state = _read_download_job(job_id)
        if str(state.get("status") or "").lower() in terminal and not state.get("running"):
            result = state.get("result") if isinstance(state.get("result"), dict) else {}
            return {
                **result,
                "success": bool(state.get("success")),
                "queued": True,
                "job_id": job_id,
                "pid": pid,
                "child_terminal": True,
                "child_status": str(state.get("status") or ""),
                "child_finished_at": str(state.get("finished_at") or ""),
                "queue_receipt": queued.get("execution_receipt"),
                "terminal_state_sha256": hashlib.sha256(
                    json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            }
        clock.sleep(0.02)
    raise RuntimeError(f"file-review fixture child did not reach terminal state: {job_id}")


def _reconcile_scheduled_download(
    email_result: dict, download_result: dict
) -> dict:
    """Bind a green download step to the portal rows that triggered it.

    A successful command invocation is not evidence that a currently
    downloadable portal row was handled.  In particular, the download worker
    may return a clean zero-file result while the preceding authoritative
    portal probe saw actionable rows.  Require those rows to be accounted for
    by either a verified PDF or the existing-file signature check.
    """
    result = dict(download_result or {})
    raw_expected = email_result.get("portal_downloadable_count")
    expected_count_contract = bool(
        type(raw_expected) is int and raw_expected >= 0
    )
    expected = raw_expected if expected_count_contract else 0

    def exact_signature_list(value) -> tuple[list[str], bool]:
        normalized = normalize_signature_hashes(value if type(value) is list else [])
        return normalized, bool(type(value) is list and value == normalized)

    raw_expected_signatures = email_result.get("portal_download_signature_hashes")
    expected_signatures, expected_signatures_exact = exact_signature_list(
        raw_expected_signatures
    )
    expected_set_hash = email_result.get("portal_download_signature_set_hash")
    probe_fingerprint = email_result.get("portal_probe_snapshot_fingerprint")
    probe_observed_at = str(
        email_result.get("portal_probe_observed_at") or ""
    ).strip()
    processed_signatures, processed_signatures_exact = exact_signature_list(
        result.get("processed_portal_signature_hashes")
    )
    verified_existing_signatures, verified_existing_signatures_exact = exact_signature_list(
        result.get("verified_existing_portal_signature_hashes")
    )
    mismatch_deferred_fields_present = bool(
        "mismatch_deferred_portal_signature_hashes" in result
        or "mismatch_deferred_portal_signature_set_hash" in result
    )
    mismatch_deferred_reason = bool(
        str(result.get("reason") or "").strip()
        == "court_payload_identity_mismatch"
    )
    mismatch_deferred_signatures, mismatch_deferred_signatures_exact = (
        exact_signature_list(result.get("mismatch_deferred_portal_signature_hashes"))
        if mismatch_deferred_fields_present or mismatch_deferred_reason
        else ([], True)
    )
    handled_signatures = normalize_signature_hashes(
        [*processed_signatures, *verified_existing_signatures]
    )
    declared_handled_signatures, declared_handled_signatures_exact = exact_signature_list(
        result.get("handled_portal_signature_hashes")
    )
    expected_signature_contract = bool(
        expected_count_contract
        and expected_signatures_exact
        and expected == len(expected_signatures)
        and expected_set_hash == signature_set_hash(expected_signatures)
        and probe_fingerprint == portal_snapshot_fingerprint(expected_signatures)
        and portal_observed_epoch(probe_observed_at) is not None
        and str(email_result.get("portal_download_receipt_schema") or "")
        == PORTAL_DOWNLOAD_RECEIPT_SCHEMA
    )
    handled_signature_contract = bool(
        processed_signatures_exact
        and verified_existing_signatures_exact
        and declared_handled_signatures_exact
        and declared_handled_signatures == handled_signatures
        and str(result.get("portal_download_receipt_schema") or "")
        == PORTAL_DOWNLOAD_RECEIPT_SCHEMA
        and result.get("processed_portal_signature_set_hash")
        == signature_set_hash(processed_signatures)
        and result.get("verified_existing_portal_signature_set_hash")
        == signature_set_hash(verified_existing_signatures)
        and result.get("handled_portal_signature_set_hash")
        == signature_set_hash(handled_signatures)
    )
    mismatch_deferred_contract = bool(
        mismatch_deferred_signatures_exact
        and (
            (
                not mismatch_deferred_fields_present
                and not mismatch_deferred_reason
            )
            or (
                mismatch_deferred_fields_present
                and result.get("mismatch_deferred_portal_signature_set_hash")
                == signature_set_hash(mismatch_deferred_signatures)
                and (
                    (
                        bool(mismatch_deferred_signatures)
                        and mismatch_deferred_reason
                        and result.get("deferred") is True
                        and set(mismatch_deferred_signatures).isdisjoint(
                            handled_signatures
                        )
                        and set(mismatch_deferred_signatures).issubset(
                            expected_signatures
                        )
                    )
                    or (
                        not mismatch_deferred_signatures
                        and not mismatch_deferred_reason
                    )
                )
            )
        )
    )
    accounted_signatures = normalize_signature_hashes(
        [*handled_signatures, *mismatch_deferred_signatures]
    )
    accounted = (
        len(set(expected_signatures) & set(accounted_signatures))
        if (
            expected_signature_contract
            and handled_signature_contract
            and mismatch_deferred_contract
        )
        else 0
    )
    verified = bool(
        result.get("success") is True
        and expected_signature_contract
        and handled_signature_contract
        and mismatch_deferred_contract
        and set(expected_signatures).issubset(accounted_signatures)
        and (
            not bool(result.get("deferred"))
            or bool(mismatch_deferred_signatures)
        )
    )
    result.update(
        {
            "expected_portal_downloadable_count": expected,
            "accounted_portal_downloadable_count": accounted,
            "download_reconciliation_verified": verified,
            "portal_download_receipt_schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
            "expected_portal_signature_hashes": expected_signatures,
            "expected_portal_signature_set_hash": signature_set_hash(expected_signatures),
            "handled_portal_signature_hashes": handled_signatures,
            "handled_portal_signature_set_hash": signature_set_hash(handled_signatures),
            "mismatch_deferred_portal_signature_hashes": mismatch_deferred_signatures,
            "mismatch_deferred_portal_signature_set_hash": signature_set_hash(
                mismatch_deferred_signatures
            ),
            "accounted_portal_signature_hashes": accounted_signatures,
            "accounted_portal_signature_set_hash": signature_set_hash(
                accounted_signatures
            ),
            "reconciled_probe_snapshot_fingerprint": probe_fingerprint,
            "reconciled_probe_observed_at": probe_observed_at,
        }
    )
    if (
        result.get("success") is True
        and not bool(result.get("deferred"))
        and not verified
    ):
        result.update(
            {
                "success": False,
                "status": "failed",
                "reason": "portal_downloadable_not_reconciled",
                "error": "portal_downloadable_not_reconciled",
                "message": (
                    "法院入口顯示可下載資料，但本輪沒有取得完整 PDF "
                    "或已存在檔案的簽章證據；已保留供下輪重試。"
                ),
            }
        )
    return result


def cmd_scheduled_check(notify: bool = True) -> dict:
    """Cron entrypoint for the complete file-review pipeline.

    The old cron invoked only ``download``, which covered review-volume download
    but not the payment/Gmail/portal scan.  Keep one explicit entrypoint so the
    scheduled job and health checks exercise the same path.

    Courts may upload review materials in batches.  Case-level skipping stays
    disabled, while verified button rows use the manager's signature-bound,
    expiring registry.  This still discovers changed rows immediately without
    repeatedly clicking the same unchanged download control every few minutes.
    """
    if (
        not _truthy(os.environ.get("MAGI_FILE_REVIEW_PRIMARY_OWNER", "0"))
        and not _truthy(os.environ.get("MAGI_FILE_REVIEW_FORCE_RUN", "0"))
    ):
        owner_state = _fresh_file_review_auto_owner_state()
        if owner_state.get("fresh"):
            return {
                "success": True,
                "status": "delegated",
                "skipped": True,
                "deferred": False,
                "reason": "primary_owner_active",
                "owner": "file_review_auto",
                "owner_state": owner_state,
                "steps": {},
                "message": "閱卷常駐巡查正常；本排程為備援，未重複掃描 Gmail 或法院入口。",
            }

    incremental_env_keys = (
        "MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP",
        "MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP",
    )
    previous_incremental_env = {key: os.environ.get(key) for key in incremental_env_keys}
    os.environ["MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP"] = "0"
    os.environ["MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP"] = "1"
    portal_env_key = "MAGI_FILE_REVIEW_CHECK_WITH_PORTAL"
    previous_portal_env = os.environ.get(portal_env_key)
    download_budget_env_key = "MAGI_FILE_REVIEW_DOWNLOAD_MAX_RUNTIME_SEC"
    previous_download_budget = os.environ.get(download_budget_env_key)
    if previous_download_budget is None:
        # The outer cron owns a 900-second envelope.  Leave enough time for
        # popup cleanup, browser shutdown and the structured result to land;
        # unfinished document buttons are persisted and resumed next hour.
        os.environ[download_budget_env_key] = "540"
    steps: Dict[str, dict] = {}

    try:
        fixture_provider = _scheduled_check_fixture_provider()
        if fixture_provider is None:
            # The regular lightweight email monitor may intentionally disable
            # portal access, but the formal hourly gate needs one authoritative
            # portal snapshot to decide whether payment/download work exists.
            os.environ[portal_env_key] = "1"
        email_result = _invoke_scheduled_formal_handler(
            "check_emails", cmd_check_emails, notify=notify, notify_empty=False
        )
        steps["check_emails"] = email_result
        portal_probe_transient_deferred = bool(
            not email_result.get("portal_probe_ok")
            and not email_result.get("portal_probe_deferred")
            and not email_result.get("portal_failure_alert")
            and _is_transient_portal_probe_failure(
                {
                    "error": email_result.get("portal_probe_error"),
                    "error_code": email_result.get("portal_probe_error_code"),
                }
            )
        )

        try:
            max_days = int(os.environ.get("MAGI_FILE_REVIEW_PAYMENT_SLIP_MAX_DAYS", "14") or "14")
        except Exception:
            max_days = 14
        max_days = max(1, min(max_days, 60))
        if fixture_provider is not None:
            steps["download_payment_slips"] = _invoke_scheduled_formal_handler(
                "download_payment_slips",
                cmd_download_payment_slips,
                max_days=max_days,
                notify=notify,
            )
        elif bool(email_result.get("portal_probe_deferred")):
            steps["download_payment_slips"] = {
                "success": True,
                "status": "deferred",
                "deferred": True,
                "skipped": True,
                "reason": "portal_probe_deferred",
            }
        elif not bool(email_result.get("portal_probe_ok")):
            steps["download_payment_slips"] = {
                "success": True,
                "status": "deferred",
                "deferred": True,
                "skipped": True,
                "reason": (
                    "portal_probe_transient_retry"
                    if portal_probe_transient_deferred
                    else "portal_probe_failed"
                ),
            }
        elif int(email_result.get("portal_pending_payment_count") or 0) <= 0:
            steps["download_payment_slips"] = {
                "success": True,
                "status": "skipped",
                "skipped": True,
                "deferred": False,
                "reason": "verified_no_pending_payment",
                "downloaded_count": 0,
            }
        else:
            steps["download_payment_slips"] = _invoke_scheduled_formal_handler(
                "download_payment_slips",
                cmd_download_payment_slips,
                max_days=max_days,
                notify=notify,
            )

        if fixture_provider is not None:
            download_queued = _invoke_scheduled_formal_handler(
                "download", cmd_download_background, case_number="", notify=notify
            )
            steps["download"] = (
                _wait_fixture_download_terminal(download_queued)
                if download_queued.get("success")
                else download_queued
            )
        elif bool(email_result.get("portal_probe_deferred")):
            # Another court-portal owner already holds the serialized resource.
            # A second login cannot add evidence and is likely to strand another
            # Chromium process, so preserve the deferred state for the next run.
            steps["download"] = {
                "success": True,
                "status": "deferred",
                "deferred": True,
                "skipped": True,
                "reason": "portal_probe_deferred",
                "message": "入口列表正由其他作業使用，卷宗下載安全延後。",
            }
        elif portal_probe_transient_deferred:
            steps["download"] = {
                "success": True,
                "status": "deferred",
                "deferred": True,
                "skipped": True,
                "reason": "portal_probe_transient_retry",
                "error": str(email_result.get("portal_probe_error") or "portal_probe_failed"),
                "message": "法院入口暫時無法開啟，本輪安全延後並於下個排程重試。",
            }
        elif not bool(email_result.get("portal_probe_ok")):
            steps["download"] = {
                "success": False,
                "status": "failed",
                "deferred": False,
                "skipped": True,
                "reason": "portal_probe_failed",
                "error": str(email_result.get("portal_probe_error") or "portal_probe_failed"),
                "message": "法院入口探測失敗，本輪不啟動完整下載；下個排程重試。",
            }
        elif (
            bool(email_result.get("portal_probe_ok"))
            and int(email_result.get("portal_downloadable_count") or 0) <= 0
            and int(email_result.get("ready_to_download_count") or 0) <= 0
            and int(email_result.get("download_hits") or 0) <= 0
        ):
            # The formal portal scan already proved that there is no actionable
            # review material.  Reopening SSO and walking every historical row
            # here used to consume the entire 900-second cron envelope and then
            # leave Playwright with a broken pipe.  The next hourly scan still
            # re-probes the portal, so this does not suppress future uploads.
            steps["download"] = {
                "success": True,
                "status": "skipped",
                "skipped": True,
                "deferred": False,
                "reason": "verified_no_download_signal",
                "downloaded_count": 0,
                "portal_download_receipt_schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
                "processed_portal_signature_hashes": [],
                "processed_portal_signature_set_hash": signature_set_hash([]),
                "verified_existing_portal_signature_hashes": [],
                "verified_existing_portal_signature_set_hash": signature_set_hash([]),
                "handled_portal_signature_hashes": [],
                "handled_portal_signature_set_hash": signature_set_hash([]),
                "message": "入口與信箱均已驗證無可下載卷宗，本輪不重複登入。",
            }
        else:
            # A queued child only proves that process creation succeeded. It
            # does not prove that the court popup reached a terminal state.
            # Keep cron attached to the real outcome so a popup timeout cannot
            # be recorded as a green scheduled run.
            steps["download"] = _invoke_scheduled_formal_handler(
                "download", cmd_download_sync, case_number="", notify=notify
            )
        steps["download"] = _reconcile_scheduled_download(
            email_result, steps.get("download") or {}
        )
    finally:
        if previous_portal_env is None:
            os.environ.pop(portal_env_key, None)
        else:
            os.environ[portal_env_key] = previous_portal_env
        if previous_download_budget is None:
            os.environ.pop(download_budget_env_key, None)
        else:
            os.environ[download_budget_env_key] = previous_download_budget
        for key, value in previous_incremental_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    failed_steps = [
        name
        for name, result in steps.items()
        if isinstance(result, dict) and result.get("success") is False
    ]
    deferred_steps = [
        name
        for name, result in steps.items()
        if isinstance(result, dict) and bool(result.get("deferred"))
    ]
    ok = not failed_steps
    message = (
        "閱卷排程本輪未完成項目已保留，將自動續跑"
        if ok and deferred_steps
        else "閱卷排程完整檢查完成"
        if ok
        else "閱卷排程完整檢查有步驟失敗：" + "、".join(failed_steps)
    )
    fixture_provider = _scheduled_check_fixture_provider()
    result = {
        "success": ok,
        "status": "deferred" if ok and deferred_steps else ("done" if ok else "failed"),
        "deferred": bool(deferred_steps),
        "skipped": bool(deferred_steps),
        "message": message,
        "failed_steps": failed_steps,
        "deferred_steps": deferred_steps,
        "steps": steps,
        "provider_quality_certified": False if fixture_provider is not None else None,
        "provider_role": "bounded_email_and_portal_fixture" if fixture_provider is not None else "live_email_and_portal",
    }
    if fixture_provider is not None:
        result["execution_receipt"] = _fixture_step_receipt(
            "scheduled_check",
            "cmd_scheduled_check",
            {
                "step_receipt_ids": [
                    str((value.get("execution_receipt") or {}).get("receipt_id") or "")
                    for value in steps.values()
                ],
                "child_terminal": bool((steps.get("download") or {}).get("child_terminal")),
            },
        )
    else:
        _publish_scheduled_check_state(result)
    return result


def cmd_confirm_apply(token: str, notify: bool = True, flow_id: str = "",
                      source: str = "") -> dict:
    """使用者回覆確認碼後，重新登入並送出閱卷聲請。

    安全：只有來自使用者的訊息（source 含 'user' / 'telegram' / 'discord'）
    或明確設定 MAGI_FILE_REVIEW_ALLOW_CONFIRM=1 才能觸發。
    CLI 直接呼叫會被擋住。
    """
    # --- 安全閘門 ---
    _allow = os.environ.get("MAGI_FILE_REVIEW_ALLOW_CONFIRM", "").strip()
    _src = (source or "").lower()
    _user_sources = ("user", "telegram", "discord", "tg", "dc", "line", "red_phone")
    if _allow != "1" and not any(s in _src for s in _user_sources):
        msg = (
            "⛔ confirm_apply 只能由使用者透過 TG/DC 回覆確認碼觸發。"
            "\n如需 CLI 測試，請設定 MAGI_FILE_REVIEW_ALLOW_CONFIRM=1"
        )
        logger.warning("confirm_apply blocked: source=%r, allow=%r", source, _allow)
        return {"success": False, "error": msg, "blocked": True}

    tk, entry = _resolve_review_confirm(token)
    if not tk or not entry:
        msg = f"❌ 確認碼無效或已過期：{token}"
        _notify(msg, notify, topic_key="filereview_apply")
        return {"success": False, "error": msg}

    case_info = entry.get("case_info") or {}
    is_paper = bool(entry.get("paper"))

    court_code = case_info.get("court_code", "")
    year = case_info.get("year", "")
    case_type_str = case_info.get("case_type", "")
    case_number = case_info.get("case_number", "")
    client_name = case_info.get("client_name", "")

    label = f"{court_code} {year}年{case_type_str}字第{case_number}號"
    if is_paper:
        label += " (紙本)"
    _notify(f"📤 確認碼 {tk} 已確認，正在重新登入送出 — {label}", notify,
            topic_key="filereview_apply")

    if is_paper:
        result = cmd_paper_apply(
            court_code=court_code, year=year, case_type=case_type_str,
            case_number=case_number, client_name=client_name,
            appointment_date=case_info.get("appointment_date", ""),
            appointment_time=case_info.get("appointment_time", "下午"),
            court_division=case_info.get("court_division", ""),
            appointment_slots=case_info.get("appointment_slots"),
            auto_submit=True, notify=notify,
            sys_type=case_info.get("sys_type", ""),
            folder_path=case_info.get("folder_path", ""),
            flow_id=flow_id,
        )
    else:
        result = cmd_apply(
            court_code=court_code, year=year, case_type=case_type_str,
            case_number=case_number, client_name=client_name,
            auto_submit=True, notify=notify,
            sys_type=case_info.get("sys_type", ""),
            folder_path=case_info.get("folder_path", ""),
            flow_id=flow_id,
            skip_upload=bool(case_info.get("skip_upload")),
            laf_only=bool(case_info.get("laf_only")),
        )
    try:
        pending = _load_review_pending()
        ent = pending.get(tk)
        if isinstance(ent, dict):
            result_key = str((result or {}).get("result") or "").strip()
            ent["status"] = "submitted" if (result or {}).get("success") and result_key == "Applied" else "failed"
            ent["finished_at"] = __import__("time").time()
            ent["submit_result"] = result_key
            pending[tk] = ent
            _save_review_pending(pending)
    except Exception as _status_err:
        logger.debug("confirm_apply status update skipped: %s", _status_err)
    return result


def cmd_probe(court_code: str, year: str, case_type: str,
              case_number: str, client_name: str = "",
              sys_type: str = "",
              notify: bool = True,
              flow_id: str = "") -> dict:
    """Probe file-review status without submitting any report."""
    return cmd_apply(
        court_code=court_code,
        year=year,
        case_type=case_type,
        case_number=case_number,
        client_name=client_name,
        sys_type=sys_type,
        auto_submit=False,
        notify=notify,
        flow_id=flow_id,
    )


@_portal_serialized("download")
def cmd_download(case_number: str = "", notify: bool = True, flow_id: str = "") -> dict:
    """Download approved file review materials."""
    if _scheduled_check_fixture_provider() is not None:
        return _fixture_download_portal_files()
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

    if not case_number and _truthy(os.environ.get("MAGI_FILE_REVIEW_BLOCK_BULK_DOWNLOAD", "0")):
        msg = (
            "已依 MAGI_FILE_REVIEW_BLOCK_BULK_DOWNLOAD 設定阻擋未指定案號的批次閱卷下載。"
            "請指定案號執行單案下載，或移除此環境變數。"
        )
        out = {"success": False, "error": "bulk_download_blocked_by_env", "message": msg}
        _eventlog("filereview:download:blocked", ok=False, payload=out, tags={})
        _notify("⚠️ " + msg, notify)
        return out

    _eventlog("filereview:download:start", payload={"case_number": case_number, "notify": bool(notify)}, tags={"case_number": case_number} if case_number else {})
    cancelled = _check_flow_cancelled(flow_id, "portal_login", detail="before download login")
    if cancelled:
        _eventlog("filereview:download:done", ok=False, payload=cancelled, tags={"case_number": case_number} if case_number else {})
        return cancelled
    cfg = _load_config()
    creds = _get_credentials(cfg)
    if not creds["username"] or not creds["password"]:
        _safe_flow_step_status(flow_id, "portal_login", status="failed", detail="missing credentials", ok=False)
        out = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_EEFILE_USERNAME/PASSWORD in .env"}
        _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
        return out

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg)

        def _new_download_manager():
            return mod.FileReviewManager(
                username=creds["username"],
                password=creds["password"],
                download_folder=creds["download_folder"],
                db_manager=db,
                headless=True,
                log_callback=lambda msg: logger.info(msg),
            )

        def _manager_nav_error_code(manager) -> str:
            return str(getattr(manager, "last_navigation_error_code", "") or "").strip()

        def _manager_download_error_code(manager) -> str:
            return str(getattr(manager, "last_download_error_code", "") or "").strip()

        def _manager_download_error_events(manager) -> list[dict]:
            raw = getattr(manager, "last_download_error_events", None)
            if isinstance(raw, list):
                events = [
                    {
                        "code": str(item.get("code") or "").strip(),
                        "detail": str(item.get("detail") or "").strip()[:500],
                    }
                    for item in raw
                    if isinstance(item, dict) and str(item.get("code") or "").strip()
                ]
                if events:
                    return events
            code = _manager_download_error_code(manager)
            if not code:
                return []
            return [
                {
                    "code": code,
                    "detail": str(
                        getattr(manager, "last_download_error_detail", "") or ""
                    ).strip()[:500],
                }
            ]

        mgr = _new_download_manager()

        try:
            logger.info("Logging into SSO for download...")
            _safe_flow_step_status(flow_id, "portal_login", status="running", detail=case_number or "all cases")
            if not mgr.login():
                error_code, error_detail, msg = _portal_login_failure_message(mgr, action_label="自動下載")
                logger.error(msg)
                if error_detail:
                    logger.error("閱卷登入失敗 detail: %s", error_detail)
                _notify(msg, notify)
                _safe_flow_step_status(flow_id, "portal_login", status="failed", detail=error_code, ok=False)
                _mark_notify_step(flow_id, notify=notify, detail=msg)
                out = {"success": False, "error": error_code}
                if error_detail:
                    out["error_detail"] = error_detail
                _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
                return out
            _safe_flow_step_status(flow_id, "portal_login", status="succeeded", detail="SSO login ok", ok=True)

            nav_ok = mgr.navigate_to_file_review()
            if not nav_ok and _manager_nav_error_code(mgr) == "invalid_csrf_token":
                logger.warning("法院入口 CSRF token 失效；關閉舊 session 後重試一次")
                _safe_flow_step_status(
                    flow_id,
                    "portal_login",
                    status="running",
                    detail="invalid_csrf_token; retrying fresh session",
                    ok=True,
                )
                try:
                    mgr.close()
                except Exception:
                    pass
                mgr = _new_download_manager()
                if not mgr.login():
                    error_code, error_detail, msg = _portal_login_failure_message(mgr, action_label="CSRF 重試下載")
                    logger.error(msg)
                    if error_detail:
                        logger.error("閱卷登入失敗 detail: %s", error_detail)
                    _notify(msg, notify)
                    _safe_flow_step_status(flow_id, "portal_login", status="failed", detail=error_code, ok=False)
                    _mark_notify_step(flow_id, notify=notify, detail=msg)
                    out = {"success": False, "error": error_code}
                    if error_detail:
                        out["error_detail"] = error_detail
                    _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
                    return out
                nav_ok = mgr.navigate_to_file_review()
                if not nav_ok and _manager_nav_error_code(mgr) == "invalid_csrf_token":
                    msg = "法院入口 CSRF token 失效，重開 session 後仍失敗。"
                    _notify("❌ 閱卷下載失敗: " + msg, notify)
                    _safe_flow_step_status(flow_id, "portal_login", status="failed", detail="invalid_csrf_token", ok=False)
                    _mark_notify_step(flow_id, notify=notify, detail=msg)
                    out = {"success": False, "error": "invalid_csrf_token", "error_detail": msg}
                    _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
                    return out
            if not nav_ok:
                logger.warning("navigate_to_file_review failed; will attempt download from current portal page")

            cancelled = _check_flow_cancelled(flow_id, "portal_download", detail="before portal download")
            if cancelled:
                _eventlog("filereview:download:done", ok=False, payload=cancelled, tags={"case_number": case_number} if case_number else {})
                return cancelled

            logger.info("Checking and downloading available files...")
            _safe_flow_step_status(flow_id, "portal_download", status="running", detail=case_number or "all cases")
            downloaded = mgr.check_and_download_available(
                target_case_number=case_number if case_number else None
            )
            if not downloaded and _manager_download_error_code(mgr) == "invalid_csrf_token":
                logger.warning("下載列表 CSRF token 失效；關閉舊 session 後重試一次")
                _safe_flow_step_status(
                    flow_id,
                    "portal_download",
                    status="running",
                    detail="invalid_csrf_token; retrying fresh session",
                    ok=True,
                )
                try:
                    mgr.close()
                except Exception:
                    pass
                mgr = _new_download_manager()
                if not mgr.login():
                    error_code, error_detail, msg = _portal_login_failure_message(mgr, action_label="CSRF 重試下載")
                    logger.error(msg)
                    if error_detail:
                        logger.error("閱卷登入失敗 detail: %s", error_detail)
                    _notify(msg, notify)
                    _safe_flow_step_status(flow_id, "portal_login", status="failed", detail=error_code, ok=False)
                    _mark_notify_step(flow_id, notify=notify, detail=msg)
                    out = {"success": False, "error": error_code}
                    if error_detail:
                        out["error_detail"] = error_detail
                    _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
                    return out
                nav_ok = mgr.navigate_to_file_review()
                if not nav_ok and _manager_nav_error_code(mgr) == "invalid_csrf_token":
                    msg = "法院入口 CSRF token 失效，重開 session 後仍失敗。"
                    _notify("❌ 閱卷下載失敗: " + msg, notify)
                    _safe_flow_step_status(flow_id, "portal_download", status="failed", detail="invalid_csrf_token", ok=False)
                    _mark_notify_step(flow_id, notify=notify, detail=msg)
                    out = {"success": False, "error": "invalid_csrf_token", "error_detail": msg}
                    _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
                    return out
                downloaded = mgr.check_and_download_available(
                    target_case_number=case_number if case_number else None
                )
                if not downloaded and _manager_download_error_code(mgr) == "invalid_csrf_token":
                    msg = "下載列表 CSRF token 失效，重開 session 後仍失敗。"
                    _notify("❌ 閱卷下載失敗: " + msg, notify)
                    _safe_flow_step_status(flow_id, "portal_download", status="failed", detail="invalid_csrf_token", ok=False)
                    _mark_notify_step(flow_id, notify=notify, detail=msg)
                    out = {"success": False, "error": "invalid_csrf_token", "error_detail": msg}
                    _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
                    return out

            download_error_events = _manager_download_error_events(mgr)
            blocking_download_errors = [
                item
                for item in download_error_events
                if item.get("code") not in _DOWNLOAD_TRANSIENT_ERRORS
            ]
            selected_download_error = (
                blocking_download_errors[-1]
                if blocking_download_errors
                else download_error_events[-1]
                if download_error_events
                else {}
            )
            download_error_code = str(
                selected_download_error.get("code") or ""
            ).strip()
            if download_error_code:
                error_detail = str(selected_download_error.get("detail") or "").strip()
                transient_only = bool(download_error_events) and not blocking_download_errors
                identity_mismatch_events = [
                    item
                    for item in download_error_events
                    if str(item.get("code") or "").strip()
                    == "case_identity_mismatch"
                ]
                if identity_mismatch_events:
                    # This is not an incomplete download: the court returned a
                    # complete PDF for another case.  The manager has already
                    # quarantined it and persisted a row-scoped cooldown.  Keep
                    # the immutable mismatch evidence, but do not let the
                    # generic incomplete-download streak turn this safely
                    # handled upstream condition into a recurring red alert.
                    _record_download_failure_state(
                        creds["download_folder"],
                        success=True,
                    )
                    unresolved_count = len(identity_mismatch_events)
                    downloaded_count = len(downloaded or [])
                    msg = (
                        "法院入口本次回傳其他案件的卷宗；MAGI 已隔離且未歸檔，"
                        "將在該列資料更新後自動重驗，不需人工重傳。"
                    )
                    logger.warning("%s count=%s", msg, unresolved_count)
                    processed_portal_signatures = normalize_signature_hashes(
                        getattr(
                            mgr,
                            "last_download_processed_signature_hashes",
                            set(),
                        )
                    )
                    verified_existing_portal_signatures = normalize_signature_hashes(
                        getattr(
                            mgr,
                            "last_download_verified_existing_signature_hashes",
                            set(),
                        )
                    )
                    mismatch_deferred_portal_signatures = normalize_signature_hashes(
                        getattr(
                            mgr,
                            "last_download_mismatch_deferred_signature_hashes",
                            set(),
                        )
                    )
                    handled_portal_signatures = normalize_signature_hashes(
                        [
                            *processed_portal_signatures,
                            *verified_existing_portal_signatures,
                        ]
                    )
                    _safe_flow_step_status(
                        flow_id,
                        "portal_download",
                        status="deferred",
                        detail="court_payload_identity_mismatch",
                        ok=True,
                    )
                    out = {
                        "success": True,
                        "status": "partial" if downloaded_count else "deferred",
                        "deferred": True,
                        "reason": "court_payload_identity_mismatch",
                        "message": msg,
                        "downloaded_count": downloaded_count,
                        "unresolved_count": unresolved_count,
                        "retry_streak": 0,
                        "files": [str(path) for path in (downloaded or [])[:10]],
                        "portal_download_receipt_schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
                        "processed_portal_signature_hashes": processed_portal_signatures,
                        "processed_portal_signature_set_hash": signature_set_hash(
                            processed_portal_signatures
                        ),
                        "verified_existing_portal_signature_hashes": verified_existing_portal_signatures,
                        "verified_existing_portal_signature_set_hash": signature_set_hash(
                            verified_existing_portal_signatures
                        ),
                        "handled_portal_signature_hashes": handled_portal_signatures,
                        "handled_portal_signature_set_hash": signature_set_hash(
                            handled_portal_signatures
                        ),
                        "mismatch_deferred_portal_signature_hashes": mismatch_deferred_portal_signatures,
                        "mismatch_deferred_portal_signature_set_hash": signature_set_hash(
                            mismatch_deferred_portal_signatures
                        ),
                    }
                    _eventlog(
                        "filereview:download:deferred",
                        ok=True,
                        payload=out,
                        tags={"case_number": case_number} if case_number else {},
                    )
                    return out
                if transient_only:
                    unresolved_count = len(download_error_events)
                    downloaded_count = len(downloaded or [])
                    # The aggregate alert says that *no* verifiable PDF was
                    # produced.  It is therefore factually wrong to advance
                    # that global streak when this same sweep already returned
                    # one or more complete PDFs.  Keep the unresolved controls
                    # queued, but treat verified progress as success for the
                    # zero-download circuit breaker.
                    retry_meta = _record_download_failure_state(
                        creds["download_folder"],
                        error_key=download_error_code,
                        success=bool(downloaded_count),
                    )
                    if not bool(retry_meta.get("should_alert")):
                        msg = (
                            f"閱卷下載部分完成，另有 {unresolved_count} 個法院下載項目"
                            "尚未產生完整 PDF，已保留至下一輪自動重試。"
                            if downloaded_count
                            else (
                                f"本輪有 {unresolved_count} 個法院下載項目尚未產生完整 PDF，"
                                "已安全延後並會在下一輪自動重試。"
                            )
                        )
                        logger.warning(
                            "%s code=%s streak=%s/%s detail=%s",
                            msg,
                            download_error_code,
                            retry_meta.get("failure_streak"),
                            retry_meta.get("threshold"),
                            error_detail,
                        )
                        _safe_flow_step_status(
                            flow_id,
                            "portal_download",
                            status="deferred",
                            detail="download_retry_pending",
                            ok=True,
                        )
                        out = {
                            "success": True,
                            "status": "partial" if downloaded_count else "deferred",
                            "deferred": True,
                            "reason": "download_retry_pending",
                            "message": msg,
                            "downloaded_count": downloaded_count,
                            "unresolved_count": unresolved_count,
                            "retry_streak": int(retry_meta.get("failure_streak") or 0),
                            "retry_threshold": int(retry_meta.get("threshold") or 0),
                            "files": [str(path) for path in (downloaded or [])[:10]],
                        }
                        _eventlog(
                            "filereview:download:deferred",
                            ok=True,
                            payload=out,
                            tags={"case_number": case_number} if case_number else {},
                        )
                        return out

                msg = (
                    "法院閱卷入口連續多輪未產生可驗證的完整 PDF，"
                    "已保留自動重試證據，需檢查入口下載狀態。"
                    if transient_only
                    else (
                        "法院閱卷入口未完成可驗證的下載流程："
                        f"{download_error_code}"
                    )
                )
                logger.error("%s detail=%s", msg, error_detail)
                _notify("❌ 閱卷下載失敗: " + msg, notify)
                _safe_flow_step_status(
                    flow_id,
                    "portal_download",
                    status="failed",
                    detail=download_error_code,
                    ok=False,
                )
                _mark_notify_step(flow_id, notify=notify, detail=msg)
                out = {
                    "success": False,
                    "error": download_error_code,
                    "error_detail": error_detail,
                    "downloaded_count": len(downloaded or []),
                    "files": [str(path) for path in (downloaded or [])[:10]],
                }
                _eventlog(
                    "filereview:download:done",
                    ok=False,
                    payload=out,
                    tags={"case_number": case_number} if case_number else {},
                )
                return out

            _record_download_failure_state(
                creds["download_folder"], success=True
            )

            download_deferred = bool(
                getattr(mgr, "last_download_deferred", False)
            )
            download_deferred_reason = str(
                getattr(mgr, "last_download_deferred_reason", "")
                or "download_time_budget_exhausted"
            )
            download_total_count = max(
                0, int(getattr(mgr, "last_download_total_count", 0) or 0)
            )
            download_processed_count = max(
                0, int(getattr(mgr, "last_download_processed_count", 0) or 0)
            )
            download_remaining_count = max(
                0, download_total_count - download_processed_count
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
            # ★ 排除 archive 階段判定為重複的檔案（exists_skip / target_exists_*）— 不算 "新檔案"
            _SKIP_ACTIONS = {"exists_skip", "target_exists_keep_src", "target_exists_isolate_src"}
            review_items = [
                it for it in items
                if isinstance(it, dict)
                and _activity_artifact_kind(it) != "payment_slip"
                and str(it.get("action") or "") not in _SKIP_ACTIONS
            ]
            payment_downloaded = [fp for fp in (downloaded or []) if os.path.basename(str(fp)).startswith("繳費單_")]
            # ★ 從 _last_archive_report 找出 dedup 跳過的 src，從 review_downloaded 剔除
            _archive_skipped_srcs = set()
            try:
                for it in items:
                    if isinstance(it, dict) and str(it.get("action") or "") in _SKIP_ACTIONS:
                        src_p = (it.get("file") or it.get("src") or "").strip()
                        if src_p:
                            _archive_skipped_srcs.add(src_p)
                            _archive_skipped_srcs.add(os.path.basename(src_p))
            except Exception:
                pass
            review_downloaded = [
                fp for fp in (downloaded or [])
                if fp not in payment_downloaded
                and fp not in _archive_skipped_srcs
                and os.path.basename(str(fp)) not in _archive_skipped_srcs
            ]
            _safe_flow_step_status(
                flow_id,
                "portal_download",
                status="deferred" if download_deferred else "succeeded",
                detail=(
                    f"download partial ({download_processed_count}/{download_total_count} rows)"
                    if download_deferred
                    else f"download complete ({count} files)"
                ),
                ok=True,
                metadata={
                    "downloaded_count": count,
                    "processed_count": download_processed_count,
                    "total_count": download_total_count,
                    "remaining_count": download_remaining_count,
                },
            )

            # ── Post-download: auto-bookmark downloaded PDFs ──
            #
            # Large OLA batches can contain dozens of long PDFs. Bookmarking is
            # useful enrichment, but it must not keep the background download
            # job from reaching a clean terminal state after the court files are
            # already downloaded and archived.
            auto_bookmark_enabled = _truthy(os.environ.get("MAGI_FILE_REVIEW_AUTO_BOOKMARK", "0"))
            if review_downloaded and auto_bookmark_enabled:
                _auto_bookmark_pdfs(review_downloaded)

            payment_count = len(payment_downloaded)
            review_count = len(review_downloaded)
            unresolved_review_items = [it for it in unresolved_items if _activity_artifact_kind(it) != "payment_slip"]
            resolved_review_items = [it for it in resolved_items if _activity_artifact_kind(it) != "payment_slip"]

            try:
                download_notice = _prepare_download_notice(
                    creds["download_folder"],
                    review_items,
                    review_downloaded,
                    notify_requested=bool(notify),
                    content_receipts=getattr(
                        mgr, "_last_download_content_receipts", {}
                    ),
                )
            except Exception as notice_error:
                # Fail closed for notification dedup.  The court files are
                # already safely archived, so do not turn a receipt-store
                # problem into another portal download or a duplicate push.
                logger.error("file-review download notice ledger rejected: %s", notice_error)
                download_notice = {
                    "valid": False,
                    "should_notify": False,
                    "event_digest": "",
                    "new_count": 0,
                    "updated_count": 0,
                    "duplicate_count": review_count,
                    "error": "download_notice_ledger_invalid",
                }

            def _norm(s: str) -> str:
                return (s or "").strip()

            def _format_download_message() -> Tuple[str, dict]:
                """
                Returns (message, exported) where exported is export_txt() result or {}.
                """
                notice_count = int(download_notice.get("new_count") or 0) + int(
                    download_notice.get("updated_count") or 0
                )
                if int(download_notice.get("updated_count") or 0) > 0 and int(
                    download_notice.get("new_count") or 0
                ) == 0:
                    header = f"📥 卷宗更新版下載完成（{notice_count} 個檔案）"
                elif int(download_notice.get("updated_count") or 0) > 0:
                    header = f"📥 卷宗下載／更新完成（{notice_count} 個檔案）"
                else:
                    header = f"📥 卷宗下載完成（{notice_count or review_count} 個檔案）"
                if case_number:
                    label = header.split("（", 1)[0].strip()
                    header = f"{label} — {case_number}（{notice_count or review_count} 個檔案）"

                if review_count <= 0:
                    if smart_skipped:
                        lines = [header, f"已存在跳過 {len(smart_skipped)} 份："]
                        for it in smart_skipped:
                            fn = (it.get("file") or "").strip()
                            ep = (it.get("existing_path") or "").strip()
                            if fn and ep:
                                lines.append(f"- {fn} -> {ep}")
                            elif fn:
                                lines.append(f"- {fn}")
                        return "\n".join(lines).strip(), {}
                    return "", {}

                # Group by canonical display party, court case no, and case folder.
                display_cache = {}
                groups = {}
                for it in review_items:
                    if not isinstance(it, dict):
                        continue
                    display_record = _case_display_record(it, db=db, cache=display_cache)
                    party = _norm(
                        _canonical_display_client_name(display_record, name_keys=("client_name", "party", "name"))
                        or "(未知)"
                    )
                    court_case_no = _norm(
                        display_record.get("court_case_no")
                        or display_record.get("court_case_number")
                        or it.get("court_case_no")
                        or ""
                    )
                    folder = _norm(display_record.get("folder_path") or it.get("folder") or "")
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
                    for fp in review_downloaded:
                        lines.append(f"- {os.path.basename(str(fp))}")

                detail = "\n".join([x for x in lines]).strip()
                if unresolved_review_items:
                    detail += f"\n\n⚠️ 待歸檔 {len(unresolved_review_items)} 份（案號歧義或資訊不足）"

                if smart_skipped:
                    detail += f"\n\n已存在跳過 {len(smart_skipped)} 份："
                    for it in smart_skipped:
                        fn = (it.get("file") or "").strip()
                        ep = (it.get("existing_path") or "").strip()
                        if fn and ep:
                            detail += f"\n- {fn} -> {ep}"
                        elif fn:
                            detail += f"\n- {fn}"

                exported = {}
                if len(detail) > 900 and export_txt:
                    exported = export_txt(detail, prefix="magi_filereview") or {}
                    if exported.get("success") and (exported.get("url") or exported.get("path")):
                        detail += f"\n\n完整明細：{exported.get('url') or exported.get('path')}"
                return detail, (exported or {})

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
            ) and (
                review_count <= 0 or bool(download_notice.get("should_notify"))
            )
            _safe_flow_step_status(
                flow_id,
                "archive",
                status="succeeded" if review_count > 0 else "skipped",
                detail=f"review_download_count={review_count}",
                ok=True,
                skipped=review_count <= 0,
                metadata={"review_download_count": review_count, "payment_download_count": payment_count},
            )
            if should_notify:
                notification_accepted = bool(
                    _notify(
                        msg,
                        True,
                        event_id=str(download_notice.get("event_digest") or "")
                        if review_count > 0
                        else "",
                    )
                )
                if notification_accepted and review_count > 0:
                    try:
                        _complete_download_notice(
                            creds["download_folder"],
                            str(download_notice.get("event_digest") or ""),
                        )
                    except Exception as notice_error:
                        logger.error(
                            "file-review download notice completion rejected: %s",
                            notice_error,
                        )
                # If long detail was exported to TXT, also send the file
                txt_path = exported.get("path", "") if exported else ""
                if notification_accepted and txt_path and os.path.isfile(txt_path):
                    _notify_file(txt_path, caption="卷宗下載明細", flag=True)
            _mark_notify_step(flow_id, notify=should_notify, detail=msg or "no notification sent")
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
                _cleanup_all_download_folders(_dl_base)

            verified_existing_count = len(smart_skipped)
            processed_portal_signatures = normalize_signature_hashes(
                getattr(mgr, "last_download_processed_signature_hashes", set())
            )
            verified_existing_portal_signatures = normalize_signature_hashes(
                getattr(
                    mgr,
                    "last_download_verified_existing_signature_hashes",
                    set(),
                )
            )
            mismatch_deferred_portal_signatures = normalize_signature_hashes(
                getattr(
                    mgr,
                    "last_download_mismatch_deferred_signature_hashes",
                    set(),
                )
            )
            handled_portal_signatures = normalize_signature_hashes(
                [
                    *processed_portal_signatures,
                    *verified_existing_portal_signatures,
                ]
            )
            out = {"success": True, "downloaded_count": count,
                   "files": [str(f) for f in (downloaded or [])[:10]],
                   "items": items[:50] if items else [],
                   "archive_summary": archive_summary,
                   "exported": exported if exported else None,
                   "review_download_count": review_count,
                   "payment_download_count": payment_count,
                   "verified_existing_count": verified_existing_count,
                   "accounted_downloadable_count": count + verified_existing_count,
                   "processed_count": download_processed_count,
                   "total_count": download_total_count,
                   "remaining_count": download_remaining_count,
                   "portal_download_receipt_schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
                   "processed_portal_signature_hashes": processed_portal_signatures,
                   "processed_portal_signature_set_hash": signature_set_hash(processed_portal_signatures),
                   "verified_existing_portal_signature_hashes": verified_existing_portal_signatures,
                   "verified_existing_portal_signature_set_hash": signature_set_hash(verified_existing_portal_signatures),
                   "handled_portal_signature_hashes": handled_portal_signatures,
                   "handled_portal_signature_set_hash": signature_set_hash(handled_portal_signatures),
                   "mismatch_deferred_portal_signature_hashes": mismatch_deferred_portal_signatures,
                   "mismatch_deferred_portal_signature_set_hash": signature_set_hash(mismatch_deferred_portal_signatures),
                   "download_notification_event_digest": str(download_notice.get("event_digest") or ""),
                   "download_notification_new_count": int(download_notice.get("new_count") or 0),
                   "download_notification_updated_count": int(download_notice.get("updated_count") or 0),
                   "download_notification_duplicate_count": int(download_notice.get("duplicate_count") or 0),
                   "download_notification_receipt_valid": bool(download_notice.get("valid")),
                   "download_notification_pii_included": False,
                   "message": msg}
            if download_deferred:
                out.update(
                    {
                        "status": "partial" if count else "deferred",
                        "deferred": True,
                        "reason": download_deferred_reason,
                        "processed_count": download_processed_count,
                        "total_count": download_total_count,
                        "remaining_count": download_remaining_count,
                    }
                )
            if count > 0:
                # Queue only cases with new evidence. This is best-effort;
                # the immutable archive receipt remains the source of truth.
                event_cases = {str(case_number or "").strip()}
                for item in items or []:
                    if isinstance(item, dict):
                        event_cases.add(str(item.get("case_number") or "").strip())
                event_source = hashlib.sha256(
                    "\0".join(sorted(str(value) for value in (downloaded or []))).encode(
                        "utf-8", errors="replace"
                    )
                ).hexdigest()
                for event_case in sorted(event_cases):
                    if re.fullmatch(r"20\d{2}-\d{4}", event_case):
                        try:
                            from magi_v3.business_events import emit_case_evidence_event

                            emit_case_evidence_event(
                                domain="file_review",
                                case_number=event_case,
                                source=event_source,
                                evidence_kind="court_download",
                            )
                        except Exception:
                            logger.warning("case evidence event could not be queued", exc_info=True)
            _eventlog("filereview:download:done", ok=True, payload={"case_number": case_number, "downloaded_count": count, "files": out.get("files", [])[:3]}, tags={"case_number": case_number} if case_number else {})
            return out

        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Download failed: %s", error_msg)
        _notify("❌ 閱卷下載失敗: " + error_msg, notify)
        _safe_flow_step_status(flow_id, "portal_download", status="failed", detail=error_msg, ok=False)
        _mark_notify_step(flow_id, notify=notify, detail=error_msg)
        out = {"success": False, "error": error_msg}
        _eventlog("filereview:download:done", ok=False, payload=out, tags={"case_number": case_number} if case_number else {})
        return out


def cmd_download_sync(case_number: str = "", notify: bool = True, flow_id: str = "") -> dict:
    """Alias for explicit sync mode: always force blocking download flow."""
    return cmd_download(case_number=case_number, notify=notify, flow_id=flow_id)


def cmd_download_background(case_number: str = "", notify: bool = True, flow_id: str = "") -> dict:
    """
    Queue download job in background and return immediately.
    """
    fixture_active = _scheduled_check_fixture_provider() is not None
    if not fixture_active:
        cfg = _load_config()
        creds = _get_credentials(cfg)
        if not creds["username"] or not creds["password"]:
            return {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_EEFILE_USERNAME/PASSWORD in .env"}

    case_number = str(case_number or "").strip()
    if not case_number and _truthy(os.environ.get("MAGI_FILE_REVIEW_BLOCK_BULK_DOWNLOAD", "0")):
        msg = (
            "已依 MAGI_FILE_REVIEW_BLOCK_BULK_DOWNLOAD 設定阻擋未指定案號的背景批次閱卷下載。"
            "請指定案號，或移除此環境變數。"
        )
        _safe_flow_step_status(flow_id, "queue", status="blocked", detail=msg, ok=False)
        _notify("⚠️ " + msg, notify)
        return {"success": False, "error": "bulk_download_blocked_by_env", "message": msg}

    cancelled = _check_flow_cancelled(flow_id, "queue", detail="before queue spawn")
    if cancelled:
        return cancelled

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
                _safe_flow_step_status(flow_id, "queue", status="succeeded", detail=msg, ok=True, metadata={"job_id": latest, "deduped": True})
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
        "flow_id": str(flow_id or "").strip(),
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
    # The worker must import the same source tree and persist into the exact
    # queue directory selected by its parent.  A legacy runtime .pth can
    # otherwise put the installed V2 package ahead of this candidate script,
    # and late environment changes can make parent/child poll different roots.
    inherited_pythonpath = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = str(_magi_root) + (
        os.pathsep + inherited_pythonpath if inherited_pythonpath else ""
    )
    env["MAGI_ROOT_DIR"] = str(_magi_root)
    env["MAGI_FILE_REVIEW_BG_JOB_DIR"] = BG_JOB_DIR
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
        _safe_flow_step_status(flow_id, "queue", status="succeeded", detail=msg, ok=True, metadata={"job_id": job_id})
        _eventlog(
            "filereview:download:queued",
            ok=True,
            payload={"job_id": job_id, "case_number": payload["case_number"]},
            tags={"case_number": payload["case_number"]} if payload["case_number"] else {},
        )
        result = {
            "success": True,
            "queued": True,
            "job_id": job_id,
            "pid": int(proc.pid),
            "status_path": status_path,
            "log_path": log_path,
            "message": msg,
        }
        if fixture_active:
            result["execution_receipt"] = _fixture_step_receipt(
                "download_queue",
                "cmd_download_background",
                {"job_id": job_id, "pid": int(proc.pid), "case_number": payload["case_number"]},
            )
        return result
    except Exception as e:
        err = f"spawn_failed: {e}"
        _safe_flow_step_status(flow_id, "queue", status="failed", detail=err, ok=False)
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
    flow_id = str((payload or {}).get("flow_id") or "").strip()

    if not job_id:
        return {"success": False, "error": "missing_job_id"}

    # Browser automation can briefly consume substantial CPU and memory.  Its
    # children inherit this niceness, preserving input-method responsiveness.
    _lower_background_priority()

    _write_download_job(
        job_id,
        {
            "status": "running",
            "running": True,
            "started_at": datetime.now().isoformat(),
            "case_number": case_number,
        },
    )
    cancelled = _check_flow_cancelled(flow_id, "portal_download", detail="before background portal download")
    if cancelled:
        _write_download_job(
            job_id,
            {
                "status": "cancelled",
                "running": False,
                "success": False,
                "finished_at": datetime.now().isoformat(),
                "result": cancelled,
            },
        )
        _safe_finalize_flow(flow_id, cancelled)
        return {"success": False, "job_id": job_id, "cancelled": True}
    out = cmd_download(case_number=case_number, notify=notify, flow_id=flow_id)
    deferred = bool(out.get("deferred"))
    _write_download_job(
        job_id,
        {
            "status": "deferred" if deferred else ("cancelled" if bool(out.get("cancelled")) else ("done" if bool(out.get("success")) else "failed")),
            "running": False,
            "success": bool(out.get("success")),
            "deferred": deferred,
            "skipped": bool(out.get("skipped")),
            "finished_at": datetime.now().isoformat(),
            "result": out,
        },
    )
    _safe_finalize_flow(flow_id, out)
    return {
        "success": bool(out.get("success")),
        "job_id": job_id,
        "status": "deferred" if deferred else ("done" if bool(out.get("success")) else "failed"),
        "deferred": deferred,
        "skipped": bool(out.get("skipped")),
        "reason": str(out.get("reason") or ""),
    }


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
    status_name = str(st.get("status") or "").strip().lower()
    if status_name in {"failed", "stopped", "cancelled"} or st.get("success") is False:
        st["success"] = False
    else:
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


def _load_dismissed_payments_cache(download_folder: str) -> dict:
    path = os.path.join(download_folder or DEFAULT_DOWNLOAD_FOLDER, "dismissed_payments.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge_dismissed_payment_maps(download_folder: str, dismissed_payments: Optional[dict] = None) -> dict:
    merged = {}
    if isinstance(dismissed_payments, dict):
        merged.update(dismissed_payments)
    for key, val in _load_dismissed_payments_cache(download_folder).items():
        merged.setdefault(key, val)
    return merged


def _load_payment_proof_case_tokens(download_folder: str) -> Set[str]:
    path = str(get_payment_proof_registry_path(download_folder or DEFAULT_DOWNLOAD_FOLDER))
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    tokens: Set[str] = set()
    for raw_case_id, record in data.items():
        candidates = [record] if isinstance(record, dict) else []
        if isinstance(record, dict) and isinstance(record.get("proofs"), list):
            candidates.extend(item for item in record["proofs"] if isinstance(item, dict))
        for candidate in candidates:
            if candidate.get("proof_schema") != PAYMENT_PROOF_SCHEMA or not candidate.get("file_sha256"):
                continue
            event_id = str(candidate.get("payment_event_id") or "").strip()
            case_token = _normalize_case_token(raw_case_id)
            if case_token and event_id:
                # Keep the occurrence proof case-scoped.  A pay id/row id is
                # not sufficient proof if another case happens to reuse it.
                tokens.add(f"{case_token}|{event_id}")
    return tokens


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


def _expand_payment_notice_key(key: object) -> Set[str]:
    """Expand legacy web_payment raw-case keys into normalized case keys."""
    raw_key = str(key or "").strip()
    if not raw_key.startswith("web_payment:"):
        return set()
    out: Set[str] = {raw_key}
    if raw_key.startswith("web_payment:case:") or raw_key.startswith("web_payment:payid:") or raw_key.startswith("web_payment:rowid:"):
        return out
    raw_case = raw_key[len("web_payment:"):].strip()
    if not raw_case:
        return out
    norm = _normalize_case_token(raw_case)
    if norm:
        out.add(f"web_payment:case:{norm}")
    return out


def _case_display_cache_key(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    parts = []
    for field in ("case_number", "court_case_no", "court_case_number", "party", "folder", "folder_path"):
        val = str(item.get(field) or "").strip()
        if val:
            parts.append(f"{field}:{val}")
    return "|".join(parts)


def _db_fetch_case_display_row(db, query: str, params: tuple) -> Optional[dict]:
    if not db:
        return None
    try:
        row = db.execute(query, params, fetch="one")
    except Exception:
        return None
    return row if isinstance(row, dict) else None


def _find_case_display_row(db, item: dict) -> Optional[dict]:
    if not db or not isinstance(item, dict):
        return None
    raw_values = [
        str(item.get("case_number") or "").strip(),
        str(item.get("court_case_no") or "").strip(),
        str(item.get("court_case_number") or "").strip(),
    ]
    for raw in raw_values:
        if not raw:
            continue
        if re.fullmatch(r"\d{4}-\d{4}", raw):
            row = _db_fetch_case_display_row(db, "SELECT * FROM cases WHERE case_number=%s LIMIT 1", (raw,))
            if row:
                return row
        for column in ("court_case_number", "court_case_no", "case_number", "laf_case_number", "legal_aid_number"):
            row = _db_fetch_case_display_row(db, f"SELECT * FROM cases WHERE `{column}`=%s LIMIT 1", (raw,))
            if row:
                return row
    party = str(item.get("party") or item.get("client_name") or "").strip()
    if party and not re.search(r"[○\[\]]|當事人", party):
        try:
            rows = db.execute(
                "SELECT * FROM cases WHERE client_name=%s ORDER BY id DESC LIMIT 2",
                (party,),
                fetch="all",
            )
        except Exception:
            rows = []
        if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
            return rows[0]
    return None


def _case_display_record(item: dict, db=None, cache: Optional[dict] = None) -> dict:
    record = dict(item or {})
    if record.get("folder") and not record.get("folder_path"):
        record["folder_path"] = record.get("folder")
    if record.get("party") and not record.get("client_name"):
        record["client_name"] = record.get("party")
    key = _case_display_cache_key(record)
    if cache is not None and key in cache:
        row = cache[key]
    else:
        row = _find_case_display_row(db, record) if db else None
        if cache is not None and key:
            cache[key] = row or {}
    if isinstance(row, dict) and row:
        enriched = dict(record)
        for field in (
            "case_number",
            "court_case_no",
            "court_case_number",
            "client_name",
            "folder_path",
            "laf_case_number",
            "legal_aid_number",
        ):
            if row.get(field):
                enriched[field] = row.get(field)
        return enriched
    return record


def _display_party_for_case_item(item: dict, db=None, cache: Optional[dict] = None) -> str:
    record = _case_display_record(item, db=db, cache=cache)
    return _canonical_display_client_name(record, name_keys=("client_name", "party", "name")) or "(未知)"


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
    paystatus = str(item.get("paystatus") or "").strip()
    p_status = str(item.get("p_status") or "").strip().upper()
    status_name = str(item.get("status_name") or item.get("statusnm") or "").strip()
    row_text = re.sub(r"\s+", "", str(item.get("row_text") or ""))
    explicit_paid = any(
        marker in row_text
        for marker in ("已繳費", "繳費完成", "繳訖", "收據", "繳費憑證")
    )
    explicit_waived = any(
        marker in row_text
        for marker in ("無須繳納費用", "無需繳費", "不需繳費", "免繳費")
    )
    explicit_pending = "待繳費" in row_text and not explicit_paid and not explicit_waived
    if explicit_pending:
        return False
    if explicit_paid or explicit_waived:
        return True
    # p_status=Y means that the payment slip has been generated and is
    # available for download.  paystatus meanings vary across OLA versions;
    # neither is payment proof.  Suppress only explicit terminal evidence.
    if p_status == "Y":
        return False
    return any(kw in status_name for kw in ("已繳", "繳費完成", "繳訖"))


_PORTAL_COURT_PICKUP_DONE_MARKERS = (
    "已到院閱卷",
    "已來院閱卷",
    "已至本院閱卷",
    "已至法院閱卷",
    "已完成閱卷",
    "完成閱卷",
    "閱卷完成",
    "閱卷完畢",
    "已閱卷",
    "已閱畢",
    "已取卷",
    "已下載",
    "下載完成",
    "已逾期",
    "逾期",
    "期限已過",
    "超過下載期限",
    "逾下載期限",
)


def _portal_item_combined_text(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return "".join(
        str(item.get(field) or "").strip()
        for field in (
            "status_name",
            "statusnm",
            "result_text",
            "row_text",
            "party",
            "court_case_no",
            "case_number",
        )
    )


def _portal_item_has_done_or_expired_marker(item: dict) -> bool:
    normalized = re.sub(r"\s+", "", _portal_item_combined_text(item))
    if not normalized:
        return False
    # OLA list rows are a history surface.  Phrases like "已到院閱卷" mean the
    # application was already consumed, not that the user has a new pickup task.
    return any(marker in normalized for marker in _PORTAL_COURT_PICKUP_DONE_MARKERS)


def _parse_portal_date(value: object):
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    candidates = []
    if len(digits) >= 8:
        candidates.append(digits[:8])
    if len(digits) >= 7:
        candidates.append(digits[:7])
    for token in candidates:
        try:
            if len(token) == 8:
                year = int(token[:4])
                month = int(token[4:6])
                day = int(token[6:8])
            elif len(token) == 7:
                year = int(token[:3]) + 1911
                month = int(token[3:5])
                day = int(token[5:7])
            else:
                continue
            if 2000 <= year <= 2100:
                return datetime(year, month, day).date()
        except Exception:
            continue
    return None


def _portal_item_is_recent_court_pickup(item: dict, *, days: Optional[int] = None) -> bool:
    if not isinstance(item, dict):
        return False
    if _portal_item_has_done_or_expired_marker(item):
        return False
    if days is None:
        try:
            days = int(os.environ.get("MAGI_FILE_REVIEW_PORTAL_PICKUP_ACTION_DAYS", "30") or "30")
        except Exception:
            days = 30
    days = max(1, min(int(days or 30), 180))
    today = datetime.now().date()
    # applydt is the best signal for "newly approved paper/on-site review".
    # deadline/downlimit can be a future availability window.  downdt means it
    # has already been downloaded/read and is therefore intentionally excluded.
    for field in ("applydt", "deadline", "downlimit", "dlmdate"):
        dt = _parse_portal_date(item.get(field))
        if not dt:
            continue
        if -days <= (dt - today).days <= days:
            return True
    return False


def _portal_item_is_court_pickup_ready(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    combined_text = _portal_item_combined_text(item)
    normalized = re.sub(r"\s+", "", combined_text)
    if not normalized:
        return False
    if _portal_item_has_done_or_expired_marker(item):
        return False
    status_name = str(item.get("status_name") or item.get("statusnm") or "").strip()
    status_code = str(item.get("status_code") or "").strip()
    result_text = re.sub(r"\s+", "", str(item.get("result_text") or "").strip())
    if any(kw in normalized for kw in ("不同意", "取消閱卷", "尚未回覆", "待法院回覆")):
        return False
    approved = ("同意" in status_name and "不同意" not in status_name) or status_code in {"3", ""}
    if not approved:
        return False

    pickup_keywords = (
        "到院閱卷",
        "來院閱卷",
        "現場閱卷",
        "本院閱卷",
        "法院閱卷",
        "紙本閱卷",
        "閱紙本卷",
        "閱覽紙本卷",
        "至本院閱卷",
        "至法院閱卷",
        "請至本院",
        "請至法院",
        "可至本院",
        "親至本院",
        "親至法院",
        "洽本院",
        "臨櫃",
        "時段閱卷",
    )
    no_payment_keywords = (
        "無需繳費",
        "不需繳費",
        "免繳費",
        "無繳費單",
        "不另製發繳費單",
        "不製發繳費單",
    )
    payment_keywords = ("待繳費", "繳費期限", "繳費單", "線上下載", "複製電子卷證費用", "處理費")

    signal_text = result_text or normalized
    has_pickup_signal = any(kw in signal_text for kw in pickup_keywords)
    has_no_payment_signal = any(kw in signal_text for kw in no_payment_keywords)
    if not (has_pickup_signal or has_no_payment_signal):
        return False
    if any(kw in normalized for kw in payment_keywords) and not has_no_payment_signal:
        return False
    return True


def _portal_item_is_actionable_pending(item: dict) -> bool:
    if not isinstance(item, dict) or item.get("status") != "pending_payment":
        return False
    if _portal_item_has_done_or_expired_marker(item):
        return False
    if _portal_item_is_court_pickup_ready(item):
        return False
    if _portal_item_is_paid(item):
        return False

    status_name = str(item.get("status_name") or item.get("statusnm") or "").strip()
    status_code = str(item.get("status_code") or "").strip()
    combined_text = " ".join(
        str(item.get(field) or "").strip()
        for field in ("result_text", "row_text")
    )
    paystatus = str(item.get("paystatus") or "").strip()

    payment_flag = str(item.get("payment_flag") or item.get("payment") or "").strip().upper()
    has_pending_signal = ("待繳費" in combined_text) or paystatus == "1" or payment_flag == "Y"
    has_approved_signal = ("同意" in status_name) or (not status_name and status_code in {"3", "6", ""})
    return has_pending_signal and has_approved_signal


def _portal_item_is_recent_payment(item: dict, *, days: Optional[int] = None) -> bool:
    """Keep the OLA history table from resurfacing years-old payment rows."""
    if not isinstance(item, dict):
        return False
    if days is None:
        try:
            days = int(os.environ.get("MAGI_FILE_REVIEW_PORTAL_PAYMENT_ACTION_DAYS", "120") or "120")
        except Exception:
            days = 120
    days = max(14, min(int(days or 120), 365))
    today = datetime.now().date()
    deadline = _parse_portal_date(item.get("pay_deadline") or item.get("deadline"))
    if deadline is not None and -14 <= (deadline - today).days <= 365:
        return True
    applied = _parse_portal_date(item.get("applydt"))
    return applied is not None and -days <= (applied - today).days <= 7


def _portal_item_search_blob(item: dict) -> Tuple[str, str]:
    if not isinstance(item, dict):
        return "", ""
    raw_parts = []
    for field in (
        "court_case_no",
        "case_number",
        "showyyidno",
        "yyidno",
        "party",
        "client_name",
        "payid",
        "rowid",
        "result_text",
        "status_name",
    ):
        val = str(item.get(field) or "").strip()
        if val:
            raw_parts.append(val)
    raw_blob = " ".join(raw_parts).lower()
    return raw_blob, _normalize_case_token(" ".join(raw_parts))


def _portal_item_has_uploaded_proof(item: dict, proof_case_tokens: Set[str]) -> bool:
    if not proof_case_tokens or not isinstance(item, dict):
        return False
    case_token = ""
    for field in ("court_case_no", "case_number", "showyyidno", "yyidno"):
        case_token = _normalize_case_token(item.get(field) or "")
        if case_token:
            break
    if not case_token:
        return False
    for field in ("rowid", "p_payid", "payid", "pay_id"):
        event_id = str(item.get(field) or "").strip()
        if event_id and f"{case_token}|{event_id}" in proof_case_tokens:
            return True
    return False


def _portal_payment_notice_keys(item: dict) -> List[str]:
    if not isinstance(item, dict):
        return []
    keys: List[str] = []
    payid = str(item.get("payid") or item.get("p_payid") or "").strip()
    rowid = str(item.get("rowid") or "").strip()
    # A case may receive more than one review-payment request.  Once OLA gives
    # us an occurrence id, case-only keys must not suppress a later request.
    if payid:
        keys.append(f"web_payment:payid:{payid}")
    if rowid:
        keys.append(f"web_payment:rowid:{rowid}")
    if keys:
        return keys

    party = str(item.get("party") or item.get("client_name") or "").strip()
    for field in ("court_case_no", "case_number", "showyyidno", "yyidno"):
        raw = str(item.get(field) or "").strip()
        if not raw:
            continue
        keys.append(f"web_payment:{raw}")
        norm = _normalize_case_token(raw)
        if norm:
            keys.append(f"web_payment:case:{norm}")
            if party:
                keys.append(f"web_payment:case:{norm}:{party}")
    out: List[str] = []
    seen: Set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _load_payment_notified_keys(download_folder: str) -> Set[str]:
    path = os.path.join(download_folder or DEFAULT_DOWNLOAD_FOLDER, "notified_cases.json")
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return set()
    raw_keys: Iterable[object]
    if isinstance(data, list):
        raw_keys = data
    elif isinstance(data, dict):
        raw_keys = data.keys()
    else:
        return set()
    out: Set[str] = set()
    for key in raw_keys:
        out.update(_expand_payment_notice_key(key))
    return out


def _load_processed_payment_tokens(download_folder: str) -> Set[str]:
    """Load occurrence ids, with case tokens only for legacy id-less rows."""
    path = str(get_payment_registry_path(download_folder or DEFAULT_DOWNLOAD_FOLDER))
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return set()
    tokens: Set[str] = set()
    for key, entry in (data or {}).items():
        if not isinstance(entry, dict):
            continue
        files = [str(x).strip() for x in (entry.get("file_paths") or []) if str(x).strip()]
        valid_existing_files = [fp for fp in files if _is_valid_payment_pdf_file(fp)]
        if not valid_existing_files:
            names = [str(x).strip() for x in (entry.get("files") or []) if str(x).strip()]
            # Legacy registries sometimes stored only a basename. A basename
            # is not completion evidence unless the actual PDF still exists in
            # the download root or its dated child directory.
            candidates: List[str] = []
            for name in names:
                candidates.append(os.path.join(download_folder, name))
                candidates.extend(glob.glob(os.path.join(download_folder, "*", name)))
            valid_existing_files = [fp for fp in candidates if _is_valid_payment_pdf_file(fp)]
        # Download success and notification success are distinct states. Keep
        # the portal item actionable until at least one valid PDF hash has an
        # acknowledged delivery receipt, so a failed TG/DC send is retried.
        if not any(
            _payment_file_already_delivered(fp, download_folder)
            for fp in valid_existing_files
        ):
            continue
        rowid = str(entry.get("rowid") or "").strip()
        payid = str(entry.get("p_payid") or entry.get("payid") or "").strip()
        key_text = str(key or "").strip()
        if not rowid and key_text.startswith("rowid:"):
            rowid = key_text.split(":", 1)[1].strip()
        if not payid and key_text.startswith("payid:"):
            payid = key_text.split(":", 1)[1].strip()
        if rowid:
            tokens.add(f"rowid:{rowid}")
        if payid:
            tokens.add(f"payid:{payid}")
        if rowid or payid:
            continue
        party = str(entry.get("party") or "").strip()
        for raw in (entry.get("case_number"), entry.get("yyidno"), entry.get("showyyidno")):
            norm = _normalize_case_token(raw or "")
            if not norm:
                continue
            tokens.add(norm)
            if party:
                tokens.add(f"{norm}:{party}")
        norm_key = _normalize_case_token(str(key or ""))
        if norm_key:
            tokens.add(norm_key)
    return tokens


def _payment_notice_keys_seen(keys: Iterable[str], notified_keys: Set[str]) -> bool:
    for key in keys or []:
        raw = str(key or "").strip()
        if not raw:
            continue
        expanded = _expand_payment_notice_key(raw) or {raw}
        if any(item in notified_keys for item in expanded):
            return True
    try:
        from skills.ops.dedup_db import is_done as _dd_is_done
        return any(
            _dd_is_done("filereview_payment", str(key or "").strip())
            for key in keys or []
            if str(key or "").strip()
        )
    except Exception:
        return False


def _portal_item_has_processed_payment(item: dict, processed_tokens: Set[str]) -> bool:
    if not processed_tokens or not isinstance(item, dict):
        return False
    rowid = str(item.get("rowid") or "").strip()
    payid = str(item.get("payid") or item.get("p_payid") or "").strip()
    occurrence_tokens = {
        token
        for token in (f"rowid:{rowid}" if rowid else "", f"payid:{payid}" if payid else "")
        if token
    }
    if occurrence_tokens:
        return bool(occurrence_tokens.intersection(processed_tokens))
    party = str(item.get("party") or item.get("client_name") or "").strip()
    for field in ("court_case_no", "case_number", "showyyidno", "yyidno"):
        norm = _normalize_case_token(item.get(field) or "")
        if not norm:
            continue
        if norm in processed_tokens:
            return True
        if party and f"{norm}:{party}" in processed_tokens:
            return True
    return False


def _is_portal_payment_notice_seen(item: dict, download_folder: str,
                                   notified_keys: Optional[Set[str]] = None,
                                   processed_tokens: Optional[Set[str]] = None) -> bool:
    """Return True only after a verifiable payment PDF was acquired.

    Notification keys are intentionally ignored here: an earlier text alert is
    not evidence that the payment-slip attachment was downloaded or delivered.
    """
    tokens = processed_tokens if processed_tokens is not None else _load_processed_payment_tokens(download_folder)
    return _portal_item_has_processed_payment(item, tokens)


def _portal_item_priority(item: dict) -> tuple:
    if not isinstance(item, dict):
        return (-1, "", "", "")
    status = str(item.get("status") or "").strip()
    base = 0
    if status == "downloadable":
        base = 30
    elif status == "court_pickup" or _portal_item_is_court_pickup_ready(item):
        base = 25
    elif _portal_item_is_actionable_pending(item):
        base = 20
    elif status == "pending_payment":
        base = 10
    applydt = re.sub(r"\D", "", str(item.get("applydt") or ""))
    rowid = re.sub(r"\D", "", str(item.get("rowid") or ""))
    payid = re.sub(r"\D", "", str(item.get("payid") or ""))
    return (base, applydt, rowid, payid)


def _review_download_case_tokens(item: dict) -> Set[str]:
    tokens: Set[str] = set()
    if not isinstance(item, dict):
        return tokens

    def _add(value: object) -> None:
        raw = str(value or "").strip()
        if not raw:
            return
        norm = _normalize_case_token(raw)
        if norm:
            tokens.add(norm)

    for field in (
        "case_number",
        "court_case_no",
        "showyyidno",
        "yyidno",
        "case_no",
        "case_id",
    ):
        _add(item.get(field))
    ci = item.get("case_info")
    if isinstance(ci, dict):
        for field in (
            "case_number",
            "court_case_no",
            "showyyidno",
            "yyidno",
            "case_no",
            "case_id",
        ):
            _add(ci.get(field))
    return tokens


def _load_downloaded_review_tokens(download_folder: str) -> Set[str]:
    """Return normalized case tokens already known as non-payment review downloads."""
    registry_path = os.path.join(download_folder, "downloaded_registry.json") if download_folder else ""
    tokens: Set[str] = set()
    if not registry_path or not os.path.exists(registry_path):
        return tokens
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            registry = json.load(f) or {}
    except Exception:
        return tokens
    if not isinstance(registry, dict):
        return tokens

    for key, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        if str(key).startswith(("/", "\\")):
            continue
        ci = entry.get("case_info") if isinstance(entry.get("case_info"), dict) else {}
        artifact = str(ci.get("artifact_type") or "").strip().lower()
        if artifact == "payment_slip":
            continue
        if "繳費單" in str(key):
            continue

        for value in (
            entry.get("yyidno"),
            entry.get("yyidno_norm"),
            entry.get("case_number"),
            entry.get("court_case_no"),
            ci.get("yyidno") if isinstance(ci, dict) else "",
            ci.get("case_number") if isinstance(ci, dict) else "",
            ci.get("court_case_no") if isinstance(ci, dict) else "",
            ci.get("showyyidno") if isinstance(ci, dict) else "",
        ):
            norm = _normalize_case_token(str(value or ""))
            if norm:
                tokens.add(norm)
    return tokens


def _manager_has_archived_review_files(file_review_manager: object, item: dict) -> bool:
    if isinstance(item, dict) and bool(item.get("archived_review_files") or item.get("has_archived_review_files")):
        return True
    if not file_review_manager or not isinstance(item, dict):
        return False
    checker = getattr(file_review_manager, "_case_review_folder_has_files", None)
    if not callable(checker):
        return False
    probe_item = dict(item)
    if probe_item.get("court_case_no") and not probe_item.get("showyyidno"):
        probe_item["showyyidno"] = probe_item.get("court_case_no")
    if probe_item.get("party") and not probe_item.get("clnm"):
        probe_item["clnm"] = probe_item.get("party")
    if probe_item.get("case_number") and not probe_item.get("yyidno"):
        probe_item["yyidno"] = probe_item.get("case_number")
    try:
        return bool(checker(probe_item))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3429, exc_info=True)
        return False


def _portal_item_has_downloaded_review(item: dict, downloaded_tokens: Set[str]) -> bool:
    if not isinstance(item, dict):
        return False
    if bool(item.get("archived_review_files") or item.get("has_archived_review_files")):
        return True
    tokens = _review_download_case_tokens(item)
    return bool(tokens and downloaded_tokens and tokens.intersection(downloaded_tokens))


def _filter_not_yet_downloaded(
    dl_items: list,
    download_folder: str,
    *,
    file_review_manager: Optional[object] = None,
) -> list:
    """Keep downloadable portal items with bounded row-level re-checks.

    整案永久跳過仍為 opt-in。按鈕級 registry 則只在 portal 列簽章未變且
    TTL 尚未屆滿時過濾；新附件導致列更新時會立即重新進入下載。
    """
    if not dl_items:
        return []
    button_level_skip = _truthy(os.environ.get("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP", "1"))
    case_level_skip = _truthy(os.environ.get("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "0"))

    # ── Button-level dedup（首選）──
    clicked_rowids: Dict[str, dict] = {}
    if download_folder:
        clicked_path = os.path.join(download_folder, "clicked_rowids.json")
        if os.path.exists(clicked_path):
            try:
                with open(clicked_path, "r", encoding="utf-8") as f:
                    clicked_data = json.load(f) or {}
                if isinstance(clicked_data, dict):
                    for key, value in clicked_data.items():
                        if str(key).strip() and isinstance(value, dict):
                            clicked_rowids[str(key).strip()] = value
            except Exception:
                pass

    # ── 卷宗檔案 dedup（fallback）：只認非 payment_slip 的 entry ──
    json_downloaded = _load_downloaded_review_tokens(download_folder)

    # ── DB-backed dedup ──
    try:
        from skills.ops.dedup_db import is_done as _dd_is_done
        _db_available = True
    except Exception:
        _db_available = False

    result = []
    for it in dl_items:
        case_num = (it.get("case_number") or "").strip()
        rowid = str(it.get("rowid") or "").strip()
        # A changed row is never filtered.  An unchanged row is filtered only
        # within the bounded re-check interval; legacy entries without a stored
        # signature still expire by timestamp.
        clicked_entry = clicked_rowids.get(rowid) if rowid else None
        skip_recent_unchanged_row = False
        if button_level_skip and rowid and isinstance(clicked_entry, dict):
            signature_fields = (
                "rowid", "no", "yyidno", "showyyidno", "c60yyidno", "isdown",
                "downdt", "upddt", "updated_at", "updtime", "limitdt", "paylimitdt",
            )
            current_signature = "|".join(
                f"{name}={str(it.get(name) or '').strip()}" for name in signature_fields
            )
            stored_signature = str(clicked_entry.get("row_signature") or "")
            try:
                clicked_at = datetime.fromisoformat(
                    str(clicked_entry.get("last_clicked") or clicked_entry.get("first_clicked") or "")
                )
                age_minutes = max(0.0, (datetime.now() - clicked_at).total_seconds() / 60.0)
            except Exception:
                age_minutes = float("inf")
            try:
                ttl_minutes = max(
                    1,
                    int(os.environ.get("MAGI_FILE_REVIEW_ROW_RECHECK_MINUTES", "43200") or "43200"),
                )
            except Exception:
                ttl_minutes = 43200
            signature_unchanged = not stored_signature or current_signature == stored_signature
            skip_recent_unchanged_row = signature_unchanged and age_minutes < ttl_minutes
        if skip_recent_unchanged_row:
            # A row is registered only after a complete review PDF is verified.
            # OLA does not reliably fill isdown/downdt, so those presentation
            # fields cannot override the signature-bound, expiring receipt.
            continue
        if not case_num:
            result.append(it)
            continue
        # Case-level skip is opt-in.  A previously downloaded case may receive
        # additional review files in a later court upload batch.
        if case_level_skip and _db_available:
            try:
                if _dd_is_done("download", case_num):
                    continue
            except Exception:
                pass
        item_tokens = _review_download_case_tokens(it)
        if case_level_skip:
            if item_tokens and item_tokens.intersection(json_downloaded):
                continue
            if case_num in json_downloaded:
                continue
            if _manager_has_archived_review_files(file_review_manager, it):
                continue
        result.append(it)
    return result


def _collapse_portal_items(
    items: list,
    *,
    download_folder: str = "",
    dismissed_payments: Optional[dict] = None,
    file_review_manager: Optional[object] = None,
) -> dict:
    chosen = {}
    raw_items = [it for it in (items or []) if isinstance(it, dict)]
    for item in raw_items:
        if _portal_item_is_actionable_pending(item):
            occurrence = str(item.get("rowid") or item.get("payid") or "").strip()
            key = f"payment:{occurrence}" if occurrence else _portal_item_case_key(item)
        else:
            key = _portal_item_case_key(item)
        key = key or f"row:{len(chosen)}:{id(item)}"
        prev = chosen.get(key)
        if prev is None or _portal_item_priority(item) > _portal_item_priority(prev):
            chosen[key] = item

    merged = list(chosen.values())
    dismissed_map = _merge_dismissed_payment_maps(download_folder, dismissed_payments)
    proof_case_tokens = _load_payment_proof_case_tokens(download_folder)
    downloaded_review_tokens = _load_downloaded_review_tokens(download_folder)
    payment_notified_keys = _load_payment_notified_keys(download_folder) if download_folder else set()
    processed_payment_tokens = _load_processed_payment_tokens(download_folder) if download_folder else set()
    downloadable = []
    downloadable_raw_count = 0
    downloadable_skipped_count = 0
    court_pickup = []
    court_pickup_history_count = 0
    pending = []
    for item in merged:
        status = str(item.get("status") or "").strip()
        if status == "downloadable":
            downloadable_raw_count += 1
            # 💡 檢查是否已在本地下載過（避開重複通知）
            if download_folder:
                filtered = _filter_not_yet_downloaded(
                    [item],
                    download_folder,
                    file_review_manager=file_review_manager,
                )
                if not filtered:
                    downloadable_skipped_count += 1
                    continue  # 已下載過 → 跳過
            downloadable.append(item)
            continue
        if status == "court_pickup" or _portal_item_is_court_pickup_ready(item):
            if status != "court_pickup":
                item = dict(item)
                item["status"] = "court_pickup"
            if not _portal_item_is_recent_court_pickup(item):
                court_pickup_history_count += 1
                continue
            court_pickup.append(item)
            continue
        if not _portal_item_is_actionable_pending(item):
            continue
        if not _portal_item_is_recent_payment(item):
            continue
        if dismissed_map and _is_portal_item_dismissed(item, dismissed_map):
            continue
        if proof_case_tokens and _portal_item_has_uploaded_proof(item, proof_case_tokens):
            continue
        if download_folder and _is_portal_payment_notice_seen(
            item,
            download_folder,
            notified_keys=payment_notified_keys,
            processed_tokens=processed_payment_tokens,
        ):
            continue
        if _portal_item_has_downloaded_review(item, downloaded_review_tokens):
            continue
        if _manager_has_archived_review_files(file_review_manager, item):
            continue
        pending.append(item)
    actionable = downloadable + court_pickup + pending
    status_order = {"downloadable": 0, "court_pickup": 1, "pending_payment": 2}
    merged.sort(key=lambda it: (
        status_order.get(str(it.get("status") or "").strip(), 9),
        _normalize_case_token(it.get("court_case_no") or it.get("case_number") or ""),
        _normalize_case_token(it.get("party") or ""),
    ))
    actionable.sort(key=lambda it: (
        status_order.get(str(it.get("status") or "").strip(), 9),
        _normalize_case_token(it.get("court_case_no") or it.get("case_number") or ""),
        _normalize_case_token(it.get("party") or ""),
    ))
    return {
        "raw_count": len(raw_items),
        "case_count": len(merged),
        "count": len(actionable),
        "downloadable_count": len(downloadable),
        "downloadable_raw_count": downloadable_raw_count,
        "downloadable_skipped_count": downloadable_skipped_count,
        "court_pickup_count": len(court_pickup),
        "court_pickup_history_count": court_pickup_history_count,
        "pending_payment_count": len(pending),
        "items": actionable,
        "all_items": merged,
    }


def _format_portal_probe_error(result: dict) -> str:
    """Turn portal probe diagnostics into a business-readable one-line reason."""
    error = str((result or {}).get("error") or "").strip()
    code = str((result or {}).get("error_code") or "").strip()
    label_map = {
        "sso_login_failed": "法院單一登入失敗，請重新登入或確認驗證碼",
        "navigate_failed": "無法進入閱卷系統入口",
        "invalid_csrf_token": "法院入口 CSRF token 失效，MAGI 會重開 session 後重試",
        "ola_error_page": "法院入口回傳錯誤頁，MAGI 已改用重試與連續失敗門檻處理",
        "popup_timeout": "法院入口新視窗逾時",
        "new_window_timeout": "法院入口新視窗逾時",
        "list_view_unavailable": "找不到入口列表頁",
        "list_page_auth_required": "法院入口要求重新登入或權限確認",
        "list_page_verification_failed": "入口列表沒有正確載入，可能法院端空白或頁面改版",
        "portal_probe_not_run": "入口列表尚未執行探測",
    }
    # Prefer the more specific navigation code (for example ola_error_page)
    # over the generic wrapper error navigate_failed.
    base = label_map.get(code) or label_map.get(error) or error or code or "未知原因"
    if "missing credentials" in base:
        base = "尚未設定法院入口帳號密碼"
    elif code and code not in {error, base} and code not in label_map:
        base = f"{base}（{code}）"

    detail = (result or {}).get("error_detail")
    preview = ""
    if isinstance(detail, dict):
        page_check = detail.get("page_check") if isinstance(detail.get("page_check"), dict) else {}
        preview = str(page_check.get("body_preview") or "").strip()
        if not preview:
            diagnostics = detail.get("frame_diagnostics")
            if isinstance(diagnostics, list):
                for frame in diagnostics:
                    if isinstance(frame, dict) and str(frame.get("body_preview") or "").strip():
                        preview = str(frame.get("body_preview") or "").strip()
                        break
    elif detail:
        preview = str(detail).strip()
    if preview:
        preview = re.sub(r"\s+", " ", preview)[:90]
        return f"{base}；頁面顯示：{preview}"
    return base


_PORTAL_PROBE_TRANSIENT_ERRORS = {
    "ola_error_page",
    "popup_timeout",
    "new_window_timeout",
    "navigate_failed",
    "navigation_exception",
    "invalid_csrf_token",
    "review_menu_not_found",
}

_DOWNLOAD_TRANSIENT_ERRORS = {
    "direct_download_incomplete",
    "popup_download_incomplete",
    # The list page can expose a download button before the nested popup frame
    # is ready.  A single miss is an upstream timing condition, not evidence
    # that MAGI or the case mapping failed.  Keep the row queued and escalate
    # only through the existing consecutive-failure threshold.
    "popup_nested_frame_timeout",
    # A late file owned by the preceding portal row is quarantined before it
    # can reach any case folder.  Keep the authoritative row/button queued and
    # retry automatically; only a consecutive threshold becomes actionable.
    "case_identity_mismatch",
}


def _portal_probe_error_key(result: dict) -> str:
    code = str((result or {}).get("error_code") or "").strip()
    error = str((result or {}).get("error") or "").strip()
    return code or error or "unknown"


def _is_transient_portal_probe_failure(result: dict) -> bool:
    key = _portal_probe_error_key(result)
    return key in _PORTAL_PROBE_TRANSIENT_ERRORS


def _portal_probe_failure_state_path(download_folder: str) -> str:
    base = str(download_folder or DEFAULT_DOWNLOAD_FOLDER).strip() or DEFAULT_DOWNLOAD_FOLDER
    return os.path.join(base, ".portal_probe_failure_state.json")


def _record_portal_probe_state(download_folder: str, result: dict) -> dict:
    """Return alert metadata for portal probe failures and suppress one-off noise."""
    path = _portal_probe_failure_state_path(download_folder)
    if bool((result or {}).get("success")):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_record_portal_probe_state/success", exc_info=True)
        return {"failure_streak": 0, "should_alert": False, "error_key": ""}

    key = _portal_probe_error_key(result)
    prev: dict = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                prev = json.load(f) or {}
    except Exception:
        prev = {}
    prev_key = str(prev.get("error_key") or "")
    streak = int(prev.get("failure_streak") or 0)
    streak = streak + 1 if prev_key == key else 1
    try:
        threshold = int(os.environ.get("MAGI_FILE_REVIEW_PORTAL_FAILURE_NOTIFY_STREAK", "3") or "3")
    except Exception:
        threshold = 3
    threshold = max(1, threshold)
    payload = {
        "error_key": key,
        "failure_streak": streak,
        "threshold": threshold,
        "last_failure_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "error": str((result or {}).get("error") or "")[:200],
        "error_code": str((result or {}).get("error_code") or "")[:200],
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_record_portal_probe_state/write", exc_info=True)
    payload["should_alert"] = bool(streak >= threshold)
    return payload


def _download_failure_state_path(download_folder: str) -> str:
    base = str(download_folder or DEFAULT_DOWNLOAD_FOLDER).strip() or DEFAULT_DOWNLOAD_FOLDER
    return os.path.join(base, ".download_failure_state.json")


def _record_download_failure_state(
    download_folder: str,
    *,
    error_key: str = "",
    success: bool = False,
) -> dict:
    """Persist bounded retry evidence for incomplete browser downloads.

    A single Chromium timing miss is not a system fault.  It remains queued
    for the next scheduled run and becomes actionable only after the same
    failure recurs for the configured number of consecutive download runs.
    """
    path = _download_failure_state_path(download_folder)
    if success:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            logging.getLogger(__name__).debug(
                "could not clear download failure state", exc_info=True
            )
        return {"failure_streak": 0, "should_alert": False, "error_key": ""}

    key = str(error_key or "unknown").strip() or "unknown"
    prev: dict = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                prev = json.load(fh) or {}
    except Exception:
        prev = {}
    previous_key = str(prev.get("error_key") or "")
    streak = int(prev.get("failure_streak") or 0)
    streak = streak + 1 if previous_key == key else 1
    try:
        threshold = int(
            os.environ.get("MAGI_FILE_REVIEW_DOWNLOAD_FAILURE_NOTIFY_STREAK", "3")
            or "3"
        )
    except Exception:
        threshold = 3
    threshold = max(1, threshold)
    payload = {
        "error_key": key,
        "failure_streak": streak,
        "threshold": threshold,
        "last_failure_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        logging.getLogger(__name__).debug(
            "could not persist download failure state", exc_info=True
        )
    payload["should_alert"] = bool(streak >= threshold)
    return payload


def _portal_probe_attempt_count(result: dict) -> int:
    try:
        return int((result or {}).get("attempts") or 1)
    except Exception:
        return 1


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


def _load_recent_activity_state(download_folder: str) -> Tuple[dict, bool]:
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
    ]
    return "|".join(parts)


def _recent_payment_activity_file_paths(item: dict) -> List[str]:
    if not isinstance(item, dict):
        return []
    paths = item.get("file_paths")
    if not isinstance(paths, list):
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for raw in paths:
        path = str(raw or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        if _is_valid_payment_pdf_file(path):
            out.append(path)
    return out


def _recent_payment_activity_has_undelivered_pdf(item: dict, download_folder: str) -> bool:
    for path in _recent_payment_activity_file_paths(item):
        if not _payment_file_already_delivered(path, download_folder):
            return True
    return False


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


def _filter_unnotified_recent_activity(records: List[dict], download_folder: str, bucket_name: str) -> List[dict]:
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
        if _already and not (
            bucket_name == "recent_payment_activity"
            and _recent_payment_activity_has_undelivered_pdf(item, download_folder)
        ):
            continue
        fresh.append(item)
    return fresh


def _mark_recent_activity_notified(records: List[dict], download_folder: str, bucket_name: str) -> None:
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


def _load_recent_payment_activity(download_folder: str, days: int = 7) -> List[dict]:
    registry_path = str(get_payment_registry_path(download_folder or DEFAULT_DOWNLOAD_FOLDER))
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
        file_paths = [str(fp).strip() for fp in (entry.get("file_paths") or []) if str(fp).strip()]
        valid_file_paths = [fp for fp in file_paths if _is_valid_payment_pdf_file(fp)]
        files = file_paths
        if not files and isinstance(entry.get("files"), list):
            files = entry.get("files") or []
        file_count = len(valid_file_paths) if valid_file_paths else len([fp for fp in files if str(fp or "").strip()])
        case_number = str(entry.get("case_number") or entry.get("yyidno") or "").strip()
        party = str(entry.get("party") or "").strip()
        if _payment_proof_already_uploaded(
            case_number, download_folder, _payment_proof_event_identity(entry)
        ):
            continue
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
            "file_paths": valid_file_paths,
        }
        rec_key = _portal_item_case_key({"case_number": case_number, "party": party, "payid": str(entry.get("p_payid") or "")}) or f"payment:{key}"
        prev = chosen.get(rec_key)
        if prev is None or dt > prev["processed_at"]:
            chosen[rec_key] = record
    return list(chosen.values())


def _auto_bookmark_pdfs(pdf_paths: List[str]) -> None:
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


def _format_recent_activity_block(title: str, records: List[dict], limit: int = 8) -> List[str]:
    if not records:
        return []
    lines = [f"{title}（{len(records)} 件）："]
    for idx, it in enumerate(records[: max(1, int(limit or 8))], 1):
        dt = it.get("processed_at")
        dt_text = dt.strftime("%m/%d %H:%M") if isinstance(dt, datetime) else "最近"
        caseno = str(it.get("case_number") or "-").strip() or "-"
        party = _canonical_display_client_name(it, name_keys=("party", "client_name", "name")) or "(未知)"
        detail = str(it.get("detail") or "已處理").strip()
        lines.append(f"  {idx}. {dt_text} {party}｜{caseno} {detail}")
    if len(records) > limit:
        lines.append(f"  ...（另有 {len(records) - limit} 件）")
    return lines


def _payment_pdf_text(path: str, max_chars: int = 2500) -> str:
    if not _is_valid_payment_pdf_file(path):
        return ""
    try:
        proc = subprocess.run(
            ["pdftotext", path, "-"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return (proc.stdout or "")[:max_chars]
    except Exception:
        return ""


def _payment_pdf_scan_roots(download_folder: str) -> List[str]:
    """Return the managed download root plus explicitly opted-in import roots.

    Never sweep a user's general Downloads directory implicitly: it may contain
    unrelated sensitive PDFs.  A manual-download inbox is included only when
    an operator configures it through ``MAGI_FILE_REVIEW_IMPORT_DIRS``.
    """
    roots = [download_folder or DEFAULT_DOWNLOAD_FOLDER]
    roots.extend(
        item.strip()
        for item in os.environ.get("MAGI_FILE_REVIEW_IMPORT_DIRS", "").split(os.pathsep)
        if item.strip()
    )
    seen: set[str] = set()
    return [root for root in roots if os.path.isdir(root) and not (root in seen or seen.add(root))]


def _load_recent_unregistered_payment_pdfs(download_folder: str, days: int = 2) -> List[dict]:
    """Find valid payment-slip PDFs that landed outside payment_registry."""
    roots = _payment_pdf_scan_roots(download_folder)
    if not roots:
        return []
    cutoff = datetime.now().timestamp() - (max(1, int(days or 2)) * 86400)
    candidates: List[str] = []
    for base in roots:
        for root in [base, os.path.join(base, datetime.now().strftime("%Y%m%d"))]:
            if not os.path.isdir(root):
                continue
            try:
                for name in os.listdir(root):
                    path = os.path.join(root, name)
                    if not os.path.isfile(path) or not name.lower().endswith(".pdf"):
                        continue
                    if os.path.getmtime(path) < cutoff:
                        continue
                    if _is_valid_payment_pdf_file(path):
                        candidates.append(path)
            except Exception:
                continue

    chosen: Dict[str, dict] = {}
    for path in candidates:
        text = _payment_pdf_text(path)
        if not text:
            continue
        compact = re.sub(r"\s+", "", text)
        if "規費繳款單" not in compact and "待補費案件繳費資訊" not in compact:
            continue
        case_number = ""
        m = re.search(r"案\s*號\s*[:：]\s*([0-9]{2,3})\s*年\s*([^\d\s]{1,12})\s*字\s*第?\s*0*([0-9]+)\s*號?", text)
        if m:
            case_number = f"{m.group(1)}年度{m.group(2)}字第{int(m.group(3)):06d}號"
        if not case_number:
            m = re.search(r"([0-9]{2,3})\s*年\s*([^\d\s]{1,12})\s*字\s*0*([0-9]+)\s*號", compact)
            if m:
                case_number = f"{m.group(1)}年度{m.group(2)}字第{int(m.group(3)):06d}號"
        party = ""
        m = re.search(r"應繳款人\s*[:：]?\s*([^\n\r]{1,20})", text)
        if m:
            party = re.sub(r"\s+", "", m.group(1)).strip()
        if not party:
            m = re.search(r"應繳款人\s*[:：]?\s*\n+\s*([^\n\r]{1,20})", text)
            if m:
                party = re.sub(r"\s+", "", m.group(1)).strip()
        # A raw PDF has no authoritative portal occurrence id.  Do not apply
        # case-level proof dedup here; surface it for explicit reconciliation.
        record_key = _portal_item_case_key({"case_number": case_number, "party": party}) or _payment_file_delivery_key(path)
        dt = datetime.fromtimestamp(os.path.getmtime(path))
        record = {
            "processed_at": dt,
            "party": party or "(未知)",
            "case_number": case_number or os.path.basename(path),
            "detail": "已下載繳費單（1 份）",
            "count": 1,
            "artifact_type": "payment_slip",
            "source": "payment_pdf_scan",
            "key": record_key,
            "file_paths": [path],
        }
        prev = chosen.get(record_key)
        if prev is None or dt > prev["processed_at"]:
            chosen[record_key] = record
    return list(chosen.values())


def _load_recent_download_activity(days: int = 7) -> List[dict]:
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
            folder = str(item.get("folder") or item.get("folder_path") or item.get("dst") or "").strip()
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
                    "folder_path": folder,
                    "count": 0,
                    "artifact_type": artifact_type,
                    "detail": detail,
                    "file_paths": [],
                },
            )
            grouped[rec_key]["count"] += 1
            for path_hint in (item.get("dst"), item.get("file"), item.get("path")):
                path_text = str(path_hint or "").strip()
                if path_text and os.path.isfile(path_text):
                    grouped[rec_key].setdefault("file_paths", []).append(path_text)
        for rec_key, payload in grouped.items():
            artifact_type = str(payload.get("artifact_type") or "review_download").strip()
            record = {
                "processed_at": dt,
                "party": payload["party"],
                "case_number": payload["case_number"],
                "folder_path": payload.get("folder_path") or "",
                "detail": f"{payload.get('detail') or ('已下載繳費單' if artifact_type == 'payment_slip' else '已下載卷宗')}（{payload['count']} 份）",
                "count": payload["count"],
                "artifact_type": artifact_type,
                "source": "download_job",
                "key": os.path.basename(path),
                "file_paths": [
                    fp for fp in (payload.get("file_paths") or [])
                    if _is_valid_payment_pdf_file(fp) or artifact_type != "payment_slip"
                ],
            }
            prev = chosen.get(rec_key)
            if prev is None or dt > prev["processed_at"]:
                chosen[rec_key] = record
    return list(chosen.values())


def _load_recent_processed_activity(download_folder: str, days: int = 7, limit: int = 8) -> List[dict]:
    merged = (
        _load_recent_payment_activity(download_folder, days=days)
        + _load_recent_unregistered_payment_pdfs(download_folder, days=min(days, 2))
        + _load_recent_download_activity(days=days)
    )
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


def _is_portal_item_dismissed(item: dict, dismissed_payments: dict) -> bool:
    """Check if a portal item matches any entry in dismissed_payments.

    Dismissed keys follow the format ``web_payment:case:{norm_case}:{party}``.
    We also do a fuzzy check: if any dismissed entry's *keyword* appears in
    the item's case number or party, treat it as dismissed.
    """
    caseno_raw = item.get("court_case_no") or item.get("case_number") or ""
    party_raw = item.get("party") or ""
    norm_case = _normalize_case_token(caseno_raw)
    party = party_raw.strip()
    raw_blob, norm_blob = _portal_item_search_blob(item)

    # 1. Exact key match  (most common path)
    if norm_case and party:
        exact_key = f"web_payment:case:{norm_case}:{party}"
        if exact_key in dismissed_payments:
            return True

    # 2. Fuzzy: check dismissed keyword against combined case identifiers
    for _dk, dv in dismissed_payments.items():
        kw = (dv.get("keyword", "") if isinstance(dv, dict) else "").strip()
        kw_norm = _normalize_case_token(kw)
        dk_norm = _normalize_case_token(_dk)
        if kw_norm and kw_norm in norm_blob:
            return True
        if kw and kw.lower() in raw_blob:
            return True
        if dk_norm and dk_norm in norm_blob:
            return True
        if norm_case and norm_case in dk_norm:
            return True
        if party and party.lower() in _dk.lower():
            return True

    return False


def _filter_urgent_pending_payments(items: list, days: int = 7,
                                    dismissed_payments: Optional[dict] = None,
                                    download_folder: str = "",
                                    notified_keys: Optional[Set[str]] = None,
                                    processed_tokens: Optional[Set[str]] = None) -> dict:
    """
    過濾未繳費案件，分為三組：
    - overdue: 已逾期（繳費期限在今天之前）
    - urgent: N 天內到期
    - unknown: 無期限資料
    回傳 dict: {"overdue": [...], "urgent": [...], "unknown": [...]}
    會跳過已在 dismissed_payments / notified_cases / payment_registry 中標記為已處理的案件。
    """
    from datetime import datetime as _dt, date as _date
    _dismissed = dismissed_payments or {}
    overdue, urgent, unknown = [], [], []
    today = _date.today()
    for it in (items or []):
        if not _portal_item_is_actionable_pending(it):
            continue
        # ── 跳過已標記為已繳費（dismissed）的案件 ──
        if _dismissed and _is_portal_item_dismissed(it, _dismissed):
            logger.debug("skip dismissed portal item: %s | %s",
                         it.get("court_case_no") or it.get("case_number") or "-",
                         it.get("party") or "?")
            continue
        if download_folder and _is_portal_payment_notice_seen(
            it,
            download_folder,
            notified_keys=notified_keys,
            processed_tokens=processed_tokens,
        ):
            logger.debug("skip already-notified/processed portal payment: %s | %s",
                         it.get("court_case_no") or it.get("case_number") or "-",
                         it.get("party") or "?")
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


def _portal_payment_scan_chain(items: list, *, download_folder: str = "") -> dict:
    """Pure scheduled-scan adapter: classify, collapse and build one notice queue.

    Keeping this side-effect-free makes the coverage contract testable before a
    scheduler or notifier is allowed to touch a real portal or message channel.
    """
    collapsed = _collapse_portal_items(items, download_folder=download_folder)
    groups = _filter_urgent_pending_payments(
        collapsed["items"], days=14, download_folder=download_folder
    )
    queue = groups["overdue"] + groups["urgent"] + groups["unknown"]
    return {
        "probe_candidate_count": int(collapsed["raw_count"]),
        "coverage_candidate_count": int(collapsed["count"]),
        "pending_payment_count": int(collapsed["pending_payment_count"]),
        "notification_queue_count": len(queue),
        "notification_queue": queue,
        "collapsed": collapsed,
    }


def _should_emit_payment_check_notice(
    *,
    pay_hits: int,
    pay_notified: int,
    portal_pending: int,
    portal_pending_changed: bool,
    portal_probe_ok: bool,
    portal_deferred: bool = False,
) -> bool:
    """Only emit payment-check notices for actionable payment work."""
    if int(pay_notified or 0) > 0:
        return True
    if int(portal_pending or 0) > 0 and bool(portal_pending_changed):
        return True
    # A Gmail search hit is observation evidence, not actionable work.  The
    # manager separately emits the real PDF/case notification and increments
    # ``pay_notified``.  Re-emitting a generic summary merely because portal
    # verification failed (or was deferred) repeats old, already-receipted mail.
    # Portal failures have their own streak/cooldown alert path.
    return False


def _should_emit_empty_check_warning(
    *,
    user_visible_warning: str,
    notify_empty: bool,
) -> bool:
    """Do not turn an internal Gmail warning into a misleading scan summary."""
    return bool(str(user_visible_warning or "").strip() and notify_empty)


def _save_portal_notify_state(
    state_path: str,
    *,
    portal_downloadable: int,
    portal_pickup: int,
    portal_pending: int,
) -> None:
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as _pf:
            json.dump({
                "portal_downloadable": int(portal_downloadable or 0),
                "portal_court_pickup": int(portal_pickup or 0),
                "portal_pending": int(portal_pending or 0),
                "notified_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }, _pf, ensure_ascii=False)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2630, exc_info=True)


def _should_emit_review_check_notice(
    *,
    download_email_hits: int,
    pickup_email_hits: int,
    ready_to_download_count: int,
    portal_downloadable: int,
    portal_downloadable_changed: bool,
    portal_pickup: int,
    portal_pickup_changed: bool,
    scan_errors: int,
    portal_failure_alert: bool,
) -> bool:
    """Only emit review-check notices for work the user can act on now.

    OLA can show a "線上下載" button before the clerk actually uploads files.
    Those rows and Gmail download notices are hints for the downloader, not
    user-facing events.  The user should only be notified after cmd_download
    has obtained and archived real review files.
    """
    _ = (
        download_email_hits,
        ready_to_download_count,
        portal_downloadable,
        portal_downloadable_changed,
    )
    if int(pickup_email_hits or 0) > 0:
        return True
    if int(portal_pickup or 0) > 0 and bool(portal_pickup_changed):
        return True
    if int(scan_errors or 0) > 0:
        return True
    if bool(portal_failure_alert):
        return True
    return False


def _ready_to_download_items(manager) -> list[dict]:
    items = []
    for info in getattr(manager, "ready_to_download", None) or []:
        items.append(
            {
                "court_case_no": str(getattr(info, "court_case_no", "") or ""),
                "laf_case_no": str(getattr(info, "laf_case_no", "") or ""),
                "application_no": str(getattr(info, "application_no", "") or ""),
                "client_name": str(getattr(info, "client_name", "") or ""),
                "court": str(getattr(info, "court", "") or ""),
            }
        )
    return items


def _probe_portal_for_email_check(creds: dict, db, empty_summary: dict) -> dict:
    """Run the optional live-portal portion without competing for Chromium."""
    lock = _acquire_file_review_portal_lock("check_emails.portal_probe")
    if not lock.acquired:
        logger.info("Court portal busy; deferring check_emails portal probe")
        return _portal_deferred_result(lock, "check_emails.portal_probe", success=False)

    try:
        logger.info("Checking live portal list for pending-payment/downloadable rows...")
        probe_mod = _ensure_portal_probe_imports()
        try:
            max_attempts = int(os.environ.get("MAGI_FILE_REVIEW_PORTAL_PROBE_RETRIES", "2") or "2")
        except Exception:
            max_attempts = 2
        max_attempts = max(1, min(max_attempts, 3))
        last_summary = empty_summary
        for attempt in range(1, max_attempts + 1):
            probe_mgr = probe_mod.FileReviewManager(
                username=creds["username"],
                password=creds["password"],
                download_folder=creds["download_folder"],
                db_manager=db,
                headless=True,
                log_callback=lambda msg: logger.info(msg),
            )
            try:
                last_summary = probe_mgr.probe_downloadable_from_portal() or empty_summary
                last_summary["probe_module"] = getattr(probe_mod, "__file__", "")
                last_summary["attempts"] = attempt
            finally:
                probe_mgr.close()
            if bool(last_summary.get("success")) or not _is_transient_portal_probe_failure(last_summary):
                break
            if attempt < max_attempts:
                logger.warning(
                    "Portal probe transient failure (%s); retrying with a fresh session (%d/%d)",
                    _portal_probe_error_key(last_summary),
                    attempt + 1,
                    max_attempts,
                )
        return last_summary
    except Exception as portal_e:
        logger.warning("Portal probe in check_emails failed: %s", portal_e)
        return {
            "success": False,
            "error": str(portal_e)[:200],
            "count": 0,
            "downloadable_count": 0,
            "court_pickup_count": 0,
            "pending_payment_count": 0,
            "probe_module": "",
        }
    finally:
        lock.release()


def cmd_check_emails(notify: bool = True, notify_empty: bool = True) -> dict:
    """Scan Gmail for payment notices and delivery notifications."""
    if _scheduled_check_fixture_provider() is not None:
        return _fixture_check_emails()
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
                "court_pickup_count": 0,
                "pending_payment_count": 0,
                "probe_module": "",
            }
            with_portal = (os.environ.get("MAGI_FILE_REVIEW_CHECK_WITH_PORTAL", "1") or "").strip().lower() in {"1", "true", "yes", "on"}
            if with_portal:
                portal_summary = _probe_portal_for_email_check(creds, db, portal_summary)
            portal_deferred = bool(portal_summary.get("deferred"))

            pay_hits = int(scan_summary.get("payment_hits") or 0)
            pay_notified = int(scan_summary.get("payment_notified") or 0)
            dl_hits = int(scan_summary.get("download_hits") or 0)
            pickup_hits = int(scan_summary.get("pickup_hits") or 0)
            ready_cnt = int(scan_summary.get("ready_to_download_count") or 0)
            errors = scan_summary.get("errors") if isinstance(scan_summary, dict) else []
            err_cnt = len(errors) if isinstance(errors, list) else 0
            portal_count = int(portal_summary.get("count") or 0)
            portal_items_raw = portal_summary.get("items") if isinstance(portal_summary.get("items"), list) else []
            _dismissed_map = _merge_dismissed_payment_maps(
                creds["download_folder"],
                getattr(mgr, "dismissed_payments", None) or {},
            )
            portal_effective = _collapse_portal_items(
                portal_items_raw,
                download_folder=creds["download_folder"],
                dismissed_payments=_dismissed_map,
                file_review_manager=mgr,
            ) if with_portal and bool(portal_summary.get("success")) else {
                "raw_count": portal_count,
                "case_count": 0,
                "count": 0,
                "downloadable_count": 0,
                "downloadable_raw_count": 0,
                "downloadable_skipped_count": 0,
                "court_pickup_count": 0,
                "court_pickup_history_count": 0,
                "pending_payment_count": 0,
                "items": [],
            }
            portal_raw_count = int(portal_effective.get("raw_count") or portal_count or 0)
            portal_case_count = int(portal_effective.get("case_count") or 0)
            portal_count = int(portal_effective.get("count") or 0)
            portal_downloadable = int(portal_effective.get("downloadable_count") or 0)
            portal_downloadable_skipped = int(portal_effective.get("downloadable_skipped_count") or 0)
            portal_pickup = int(portal_effective.get("court_pickup_count") or 0)
            portal_pickup_history = int(portal_effective.get("court_pickup_history_count") or 0)
            portal_pending = int(portal_effective.get("pending_payment_count") or 0)
            portal_download_receipt = portal_download_snapshot(
                portal_effective.get("items") or []
            )
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
                f"- 可下載通知：{dl_hits} 封，可到院閱卷通知：{pickup_hits} 封（待下載佇列 {ready_cnt} 件）",
            ]
            if with_portal:
                if portal_deferred:
                    review_lines.append("- ⏸️ 入口列表正由其他作業使用，本輪安全延後")
                elif bool(portal_summary.get("success")):
                    _downloadable_note = f"{portal_downloadable} 件"
                    if portal_downloadable_skipped:
                        _downloadable_note = f"{portal_downloadable} 件（已歸檔/已下載略過 {portal_downloadable_skipped} 件）"
                    _pickup_note = f"近期需到院閱卷：{portal_pickup} 件"
                    if portal_pickup_history:
                        _pickup_note += f"（歷史/已完成列已略過 {portal_pickup_history} 件）"
                    review_lines.append(
                        f"- 入口列表可下載：{_downloadable_note}，{_pickup_note}（同案合併後需回報 {portal_count} 案，原始 {portal_raw_count} 列）"
                    )
                    # 列出未繳費案件明細（分逾期/即將到期/無期限）
                    portal_items = portal_effective.get("items") or []
                    _portal_display_cache = {}
                    _payment_notified_keys = _load_payment_notified_keys(creds["download_folder"])
                    _processed_payment_tokens = _load_processed_payment_tokens(creds["download_folder"])
                    groups = _filter_urgent_pending_payments(portal_items, days=14,
                                                            dismissed_payments=_dismissed_map,
                                                            download_folder=creds["download_folder"],
                                                            notified_keys=_payment_notified_keys,
                                                            processed_tokens=_processed_payment_tokens)
                    overdue = groups.get("overdue", [])
                    urgent = groups.get("urgent", [])
                    unknown = groups.get("unknown", [])
                    portal_payment_due_count = len(overdue) + len(urgent) + len(unknown)
                    payment_lines.append(f"- 入口列表待繳費：{portal_payment_due_count} 件")

                    def _fmt_payment_items(items, limit=15):
                        lines = []
                        for idx, it in enumerate(items[:limit], 1):
                            caseno = it.get("court_case_no") or it.get("case_number") or "-"
                            party = _display_party_for_case_item(it, db=db, cache=_portal_display_cache)
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
                    if unknown:
                        payment_lines.append("")
                        payment_lines.append(f"期限不明但仍顯示待繳（{len(unknown)} 件）：")
                        payment_lines.extend(_fmt_payment_items(unknown))
                    # 列出可下載案件明細（排除已下載的）
                    dl_items_all = [it for it in portal_items if str(it.get("status") or "").strip() == "downloadable"]
                    dl_items = _filter_not_yet_downloaded(
                        dl_items_all,
                        creds.get("download_folder") or "",
                        file_review_manager=mgr,
                    )
                    if dl_items:
                        review_lines.append("")
                        _skipped_total = max(portal_downloadable_skipped, len(dl_items_all) - len(dl_items))
                        review_lines.append(f"可下載案件（共 {len(dl_items)} 件，已歸檔/已下載 {_skipped_total} 件已略過）：")
                        for idx, it in enumerate(dl_items[:10], 1):
                            caseno = it.get("court_case_no") or it.get("case_number") or "-"
                            party = _display_party_for_case_item(it, db=db, cache=_portal_display_cache)
                            review_lines.append(f"  {idx}. {party}｜{caseno}")
                        if len(dl_items) > 10:
                            review_lines.append(f"  ...（另有 {len(dl_items) - 10} 件）")
                    pickup_items = [
                        it for it in portal_items
                        if str(it.get("status") or "").strip() == "court_pickup"
                    ]
                    if pickup_items:
                        review_lines.append("")
                        review_lines.append(f"近期需到院閱卷案件（共 {len(pickup_items)} 件，不下載繳費單）：")
                        for idx, it in enumerate(pickup_items[:10], 1):
                            caseno = it.get("court_case_no") or it.get("case_number") or "-"
                            party = _display_party_for_case_item(it, db=db, cache=_portal_display_cache)
                            review_lines.append(f"  {idx}. {party}｜{caseno}")
                        if len(pickup_items) > 10:
                            review_lines.append(f"  ...（另有 {len(pickup_items) - 10} 件）")
                else:
                    review_lines.append(f"- ⚠️ 入口列表探測失敗：{_format_portal_probe_error(portal_summary)}")
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
            _prev_pickup = int(_portal_state_prev.get("portal_court_pickup", -1))
            _prev_pending = int(_portal_state_prev.get("portal_pending", -1))
            _portal_downloadable_changed = (portal_downloadable != _prev_downloadable)
            _portal_pickup_changed = (portal_pickup != _prev_pickup)
            _portal_pending_changed = (portal_pending != _prev_pending)
            _portal_probe_ok = bool(portal_summary.get("success")) if with_portal and not portal_deferred else False
            _portal_failure_meta = (
                {"failure_streak": 0, "should_alert": False, "error_key": ""}
                if portal_deferred or not with_portal
                else _record_portal_probe_state(creds["download_folder"], portal_summary)
            )
            _portal_failure_alert = bool(_portal_failure_meta.get("should_alert"))
            try:
                portal_payment_due_count
            except NameError:
                portal_payment_due_count = 0

            payment_signal = _should_emit_payment_check_notice(
                pay_hits=pay_hits,
                pay_notified=pay_notified,
                portal_pending=portal_payment_due_count,
                portal_pending_changed=_portal_pending_changed,
                portal_probe_ok=_portal_probe_ok,
                portal_deferred=portal_deferred,
            ) or bool(recent_payment_activity)
            review_signal = _should_emit_review_check_notice(
                download_email_hits=dl_hits,
                pickup_email_hits=pickup_hits,
                ready_to_download_count=ready_cnt,
                portal_downloadable=portal_downloadable,
                portal_downloadable_changed=_portal_downloadable_changed,
                portal_pickup=portal_pickup,
                portal_pickup_changed=_portal_pickup_changed,
                scan_errors=err_cnt,
                portal_failure_alert=_portal_failure_alert,
            )
            download_signal = bool(recent_review_download_activity)
            section_messages: List[Tuple[str, str]] = []  # (msg, topic_key)
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
            # 無新資訊時不推播；手動/cron 呼叫仍會在 out["message"] 回傳摘要。
            should_notify_now = notify and has_something_to_notify
            if should_notify_now or _should_emit_empty_check_warning(
                user_visible_warning=warn_message,
                notify_empty=notify_empty,
            ):
                if section_messages:
                    sent_payment_section = False
                    sent_review_section = False
                    for section_msg, section_topic in section_messages:
                        # check_emails 是巡檢摘要；單筆繳費通知與實際下載完成
                        # 已由各自流程發送。這裡固定 TG-only，避免同一案件
                        # 以「檢查完成 / 回報」文案再鏡像到業務 DC。
                        _notify(section_msg, True, topic_key="quiet_cron")
                        if section_topic == "filereview_payment":
                            sent_payment_section = True
                        elif section_topic == "filereview_download":
                            sent_review_section = True
                    if should_notify_now:
                        if sent_payment_section:
                            recent_payment_paths: List[str] = []
                            recent_payment_captions: Dict[str, str] = {}
                            for item in recent_payment_activity:
                                party = _canonical_display_client_name(
                                    item,
                                    name_keys=("party", "client_name", "name"),
                                )
                                case_no = str(item.get("case_number") or "").strip()
                                label = f"{party}｜{case_no}" if (party or case_no) else ""
                                for path in _recent_payment_activity_file_paths(item):
                                    recent_payment_paths.append(path)
                                    if label:
                                        recent_payment_captions[path] = label
                            _payment_file_stats = _send_payment_pdf_files(
                                recent_payment_paths,
                                download_folder=creds["download_folder"],
                                caption_prefix="📄 近期繳費單 PDF",
                                notify=True,
                                captions_by_path=recent_payment_captions,
                            )
                            if (
                                _payment_file_stats.get("sent", 0) > 0
                                or _payment_file_stats.get("eligible", 0) == 0
                            ):
                                _mark_recent_activity_notified(
                                    recent_payment_activity,
                                    creds["download_folder"],
                                    "recent_payment_activity",
                                )
                        if sent_review_section:
                            _mark_recent_activity_notified(
                                recent_review_download_activity,
                                creds["download_folder"],
                                "recent_review_download_activity",
                            )
                    if warn_message:
                        _notify(warn_message, True)
                else:
                    # With no actionable section, emit only the concrete repair
                    # notice.  Never disguise it as a successful payment scan.
                    _notify(warn_message, True, topic_key="quiet_cron")
            if _portal_probe_ok and (notify or portal_pending == 0):
                # 即使待繳費歸零且不發通知，也要寫入 0；否則舊的非零狀態
                # 會讓下一次真正出現待繳費時被誤判成「沒有變動」。
                _save_portal_notify_state(
                    _portal_state_path,
                    # 可下載按鈕不代表書記官已上傳卷宗；不寫入通知去重狀態，
                    # 讓下載器下輪仍可重試，直到真的下載並歸檔後才通知。
                    portal_downloadable=0,
                    portal_pickup=portal_pickup,
                    portal_pending=portal_pending,
                )
            out = {
                "success": True,
                "message": msg,
                "payment_hits": pay_hits,
                "payment_notified": pay_notified,
                "download_hits": dl_hits,
                "pickup_hits": pickup_hits,
                "ready_to_download_count": ready_cnt,
                "ready_to_download_items": _ready_to_download_items(mgr),
                "scan_errors": err_cnt,
                "portal_count": portal_count,
                "portal_raw_row_count": portal_raw_count,
                "portal_case_count": portal_case_count,
                "portal_downloadable_count": portal_downloadable,
                "portal_downloadable_skipped_count": portal_downloadable_skipped,
                "portal_court_pickup_count": portal_pickup,
                "portal_court_pickup_history_count": portal_pickup_history,
                "portal_pending_payment_count": portal_pending,
                **portal_download_receipt,
                "portal_status_semantics": "ola-current-state-v2",
                "portal_probe_ok": bool(portal_summary.get("success")),
                "portal_probe_deferred": portal_deferred,
                "portal_probe_error": "" if bool(portal_summary.get("success")) else str(portal_summary.get("error") or ""),
                "portal_probe_error_code": "" if bool(portal_summary.get("success")) else str(portal_summary.get("error_code") or ""),
                "portal_probe_module": str(portal_summary.get("probe_module") or ""),
                "portal_probe_attempts": _portal_probe_attempt_count(portal_summary),
                "portal_failure_streak": int(_portal_failure_meta.get("failure_streak") or 0),
                "portal_failure_alert": bool(_portal_failure_alert),
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
                    "pickup_hits": pickup_hits,
                    "ready_to_download_count": ready_cnt,
                    "scan_errors": err_cnt,
                    "portal_count": portal_count,
                    "portal_raw_row_count": portal_raw_count,
                    "portal_case_count": portal_case_count,
                    "portal_downloadable_count": portal_downloadable,
                    "portal_downloadable_skipped_count": portal_downloadable_skipped,
                    "portal_court_pickup_count": portal_pickup,
                    "portal_court_pickup_history_count": portal_pickup_history,
                    "portal_pending_payment_count": portal_pending,
                    "portal_status_semantics": "ola-current-state-v2",
                    "portal_probe_ok": bool(portal_summary.get("success")),
                    "portal_probe_deferred": portal_deferred,
                    "portal_probe_error": "" if bool(portal_summary.get("success")) else str(portal_summary.get("error") or ""),
                    "portal_probe_error_code": "" if bool(portal_summary.get("success")) else str(portal_summary.get("error_code") or ""),
                    "portal_probe_module": str(portal_summary.get("probe_module") or ""),
                    "portal_probe_attempts": _portal_probe_attempt_count(portal_summary),
                    "portal_failure_streak": int(_portal_failure_meta.get("failure_streak") or 0),
                    "portal_failure_alert": bool(_portal_failure_alert),
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

def cmd_preview_emails(days: int = 7, read_only: bool = False) -> dict:
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
    if read_only:
        missing_deps = _missing_runtime_deps()
        if missing_deps:
            return {
                "success": False,
                "error": "dependency_missing",
                "missing_dependencies": missing_deps,
            }
    else:
        _eventlog("filereview:gmail_preview:start", payload={"days": day_n, "max_results": max_n})
        _ensure_runtime_deps()
    cfg = _load_config()
    creds = _get_credentials(cfg)

    try:
        mod = _ensure_imports()
        db = _get_db_manager(cfg, read_only=read_only)

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
                if not read_only and auto_restore and (("need_interactive_oauth" in wl0) or ("invalid_grant" in wl0)):
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
            if not read_only:
                _eventlog("filereview:gmail_preview:done", ok=True, payload={"count": len(items)})
            return out
        finally:
            mgr.close()

    except Exception as e:
        error_msg = str(e)[:200]
        logger.error("Email preview failed: %s", error_msg)
        out = {"success": False, "error": error_msg}
        if not read_only:
            _eventlog("filereview:gmail_preview:done", ok=False, payload=out)
        return out


@_portal_serialized("downloadable_probe")
def cmd_downloadable_probe(days: int = 30, notify: bool = False,
                           target_case_number: str = "",
                           dump_raw: bool = False,
                           require_portal: bool = False,
                           read_only: bool = False) -> dict:
    """
    法院端狀態掃描（唯讀，不下載、不改資料）：
    回傳法院入口列表中「目前有線上下載按鈕」或「待繳費」的案件。
    已歸檔閱卷資料、已下載登錄與已上傳繳費證明的案件會先去重。
    1) 優先掃法院入口「列表式查看」（最接近實際可下載狀態）
    2) 補充 Gmail 通知預覽（避免漏看通知信）

    ``require_portal`` is for the production health gate: Gmail may provide
    diagnostics for interactive use, but cannot turn a portal outage green.
    ``read_only`` suppresses flow/event persistence, dependency bootstrap,
    token recovery, and notifications for health probes.
    """
    try:
        day_n = int(days or os.environ.get("MAGI_FILE_REVIEW_PREVIEW_DAYS", "30") or "30")
    except Exception:
        day_n = 21
    day_n = max(1, min(day_n, 120))

    if read_only:
        missing_deps = _missing_runtime_deps()
        if missing_deps:
            dependency_error = {
                "success": False,
                "error": "dependency_missing",
                "missing_dependencies": missing_deps,
            }
            return {
                "success": False,
                "error": "dependency_missing",
                "missing_dependencies": missing_deps,
                "source": "none",
                "count": 0,
                "downloadable_count": 0,
                "items": [],
                "items_total": 0,
                "items_truncated": False,
                "portal": dependency_error,
                "gmail": dependency_error,
                "message": "唯讀健檢缺少必要依賴，未執行 portal 或 Gmail 探測。",
            }

    portal_r = {"success": False, "error": "portal_probe_not_run"}
    portal_dismissed_map: dict = {}
    creds = {"download_folder": DEFAULT_DOWNLOAD_FOLDER}
    try:
        if not read_only:
            _ensure_runtime_deps()
        cfg = _load_config()
        creds = _get_credentials(cfg)
        if not creds["username"] or not creds["password"]:
            portal_r = {"success": False, "error": "missing credentials — set MAGI_JUDICIAL_EEFILE_USERNAME/PASSWORD in .env"}
        else:
            mod = _ensure_portal_probe_imports()
            db = _get_db_manager(cfg, read_only=read_only)
            try:
                max_attempts = int(os.environ.get("MAGI_FILE_REVIEW_PORTAL_PROBE_RETRIES", "2") or "2")
            except Exception:
                max_attempts = 2
            max_attempts = max(1, min(max_attempts, 3))
            for attempt in range(1, max_attempts + 1):
                mgr = mod.FileReviewManager(
                    username=creds["username"],
                    password=creds["password"],
                    download_folder=creds["download_folder"],
                    db_manager=db,
                    headless=True,
                    log_callback=lambda msg: logger.info(msg),
                )
                try:
                    logger.info("Running portal downloadable probe... attempt=%s/%s", attempt, max_attempts)
                    portal_dismissed_map = _merge_dismissed_payment_maps(
                        creds["download_folder"],
                        getattr(mgr, "dismissed_payments", None) or {},
                    )
                    portal_r = mgr.probe_downloadable_from_portal(target_case_number=target_case_number or None)
                    portal_r["probe_module"] = getattr(mod, "__file__", "")
                    portal_r["attempts"] = attempt
                    if bool(portal_r.get("success")):
                        portal_r["_effective"] = _collapse_portal_items(
                            portal_r.get("items") if isinstance(portal_r.get("items"), list) else [],
                            download_folder=creds.get("download_folder") or DEFAULT_DOWNLOAD_FOLDER,
                            dismissed_payments=portal_dismissed_map,
                            file_review_manager=mgr,
                        )
                    if dump_raw:
                        # debug mode: 印出每筆 raw row 的關鍵狀態欄位
                        raw_items = portal_r.get("items") or []
                        portal_r["dump_raw_items"] = [
                            {
                                "case_number": it.get("case_number"),
                                "court_case_no": it.get("court_case_no"),
                                "party": it.get("party"),
                                "status": it.get("status"),
                                "rowid": it.get("rowid"),
                                "paystatus": it.get("paystatus"),
                                "p_status": it.get("p_status"),
                                "payment_flag": it.get("payment_flag"),
                                "status_name": it.get("status_name"),
                                "result_text": (it.get("result_text") or "")[:120],
                            }
                            for it in raw_items
                        ]
                    logger.info(
                        "Portal probe done: success=%s count=%s downloadable=%s module=%s attempt=%s",
                        bool(portal_r.get("success")),
                        portal_r.get("count"),
                        portal_r.get("downloadable_count"),
                        getattr(mod, "__file__", ""),
                        attempt,
                    )
                finally:
                    mgr.close()
                if bool(portal_r.get("success")) or not _is_transient_portal_probe_failure(portal_r):
                    break
                if attempt < max_attempts:
                    logger.warning(
                        "Portal downloadable probe transient failure (%s); retrying with a fresh session (%d/%d)",
                        _portal_probe_error_key(portal_r),
                        attempt + 1,
                        max_attempts,
                    )
    except Exception as e:
        portal_r = {"success": False, "error": str(e)[:240]}

    portal_ok = bool(portal_r.get("success"))
    gmail_default = "0" if read_only else "1"
    force_gmail = (os.environ.get("MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", gmail_default) or "").strip().lower() in {"1", "true", "yes", "on"}
    want_gmail = force_gmail or (not portal_ok and not require_portal)
    if want_gmail:
        gmail_r = cmd_preview_emails(days=day_n, read_only=read_only)
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
        portal_effective = portal_r.get("_effective") if isinstance(portal_r.get("_effective"), dict) else None
        if portal_effective is None:
            portal_effective = _collapse_portal_items(
                raw_items,
                download_folder=creds.get("download_folder") or DEFAULT_DOWNLOAD_FOLDER,
                dismissed_payments=portal_dismissed_map,
            )
        effective_items = portal_effective.get("items") or []
        portal_download_receipt = portal_download_snapshot(effective_items)
        items = effective_items[:report_limit]
        raw_count = int(portal_r.get("count") or len(raw_items) or 0)
        case_count = int(portal_effective.get("case_count") or 0)
        count = int(portal_effective.get("count") or 0)
        downloadable_count = int(portal_effective.get("downloadable_count") or 0)
        downloadable_skipped_count = int(portal_effective.get("downloadable_skipped_count") or 0)
        court_pickup_count = int(portal_effective.get("court_pickup_count") or 0)
        court_pickup_history_count = int(portal_effective.get("court_pickup_history_count") or 0)
        pending_payment_count = int(portal_effective.get("pending_payment_count") or 0)
        downloadable_phrase = f"法院端可下載 {downloadable_count} 件"
        if downloadable_skipped_count:
            downloadable_phrase += f"（已歸檔/已下載略過 {downloadable_skipped_count} 件）"
        pickup_phrase = f"近期需到院閱卷 {court_pickup_count} 件"
        if court_pickup_history_count:
            pickup_phrase += f"（歷史/已完成列已略過 {court_pickup_history_count} 件）"
        msg = (
            f"法院端狀態掃描完成（入口列表）：{downloadable_phrase}，"
            f"{pickup_phrase}，待繳費 {pending_payment_count} 件，"
            f"同案合併後共 {count} 案（原始 {raw_count} 列）"
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
            "downloadable_skipped_count": downloadable_skipped_count,
            "court_pickup_count": court_pickup_count,
            "court_pickup_history_count": court_pickup_history_count,
            "pending_payment_count": pending_payment_count,
            **portal_download_receipt,
            "items": items,
            "items_total": len(effective_items),
            "items_truncated": len(effective_items) > len(items),
            "portal": {
                "success": bool(portal_r.get("success")),
                "count": count,
                "raw_count": raw_count,
                "case_count": case_count,
                "downloadable_count": downloadable_count,
                "downloadable_skipped_count": downloadable_skipped_count,
                "court_pickup_count": court_pickup_count,
                "court_pickup_history_count": court_pickup_history_count,
                "pending_payment_count": pending_payment_count,
                **portal_download_receipt,
                "items_total": len(effective_items),
                "error": portal_r.get("error") if not bool(portal_r.get("success")) else "",
                "probe_module": str(portal_r.get("probe_module") or ""),
                "dump_raw_items": portal_r.get("dump_raw_items"),
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
            msg += f"；入口列表探測失敗：{_format_portal_probe_error(portal_r)}"
        out = {
            "success": False if require_portal else bool(gmail_r.get("success")),
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
                "court_pickup_count": int(portal_r.get("court_pickup_count") or 0),
                "pending_payment_count": int(portal_r.get("pending_payment_count") or 0),
                "error": portal_r.get("error") if not bool(portal_r.get("success")) else "",
                "probe_module": str(portal_r.get("probe_module") or ""),
            },
            "gmail": gmail_r,
            "message": msg,
        }

    if notify and not read_only:
        _notify(f"📮 閱卷可下載判定：{out.get('message')}", True)

    if not read_only:
        _eventlog(
            "filereview:gmail_downloadable_probe:done",
            ok=bool(out.get("success")),
            payload={
                "source": source,
                "count": int(out.get("count") or 0),
                "downloadable_count": int(out.get("downloadable_count") or 0),
                "court_pickup_count": int(out.get("court_pickup_count") or 0),
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


def cmd_cleanup_downloads(
    max_days: int = 7,
    pending_max_days: int = 14,
    quarantine_max_days: int = 14,
    dry_run: bool = False,
) -> dict:
    """Clean disposable file-review download staging on a regular schedule."""
    cfg = _load_config()
    creds = _get_credentials(cfg)
    download_folder = creds.get("download_folder") or DEFAULT_DOWNLOAD_FOLDER
    base_dirs = _download_cleanup_base_candidates(download_folder)
    summary = {
        "success": True,
        "base_dirs": base_dirs,
        "runs": [],
        "deleted_count": 0,
        "would_delete_count": 0,
        "freed_bytes": 0,
    }
    for base_dir in base_dirs:
        run = _cleanup_all_download_folders(
            base_dir,
            max_days=_coerce_retention_days(max_days, 7),
            pending_max_days=_coerce_retention_days(pending_max_days, 14),
            quarantine_max_days=_coerce_retention_days(quarantine_max_days, 14),
            dry_run=bool(dry_run),
        )
        summary["runs"].append(run)
        summary["deleted_count"] += int(run.get("deleted_count") or 0)
        summary["would_delete_count"] += int(run.get("would_delete_count") or 0)
        summary["freed_bytes"] += int(run.get("freed_bytes") or 0)
        if not run.get("success", False):
            summary["success"] = False

    deleted_count = int(summary.get("deleted_count") or 0)
    would_delete_count = int(summary.get("would_delete_count") or 0)
    freed_bytes = int(summary.get("freed_bytes") or 0)
    if dry_run:
        msg = f"閱卷暫存清理 dry-run：將清 {would_delete_count} 個資料夾"
    else:
        msg = f"閱卷暫存清理完成：清除 {deleted_count} 個資料夾，釋放 {freed_bytes / (1024 ** 3):.2f} GB"
    out = {
        "success": bool(summary.get("success", True)),
        "message": msg,
        "download_folder": download_folder,
        "base_dirs": base_dirs,
        "max_days": max_days,
        "pending_max_days": pending_max_days,
        "quarantine_max_days": quarantine_max_days,
        "dry_run": bool(dry_run),
        "summary": summary,
    }
    _eventlog(
        "filereview:cleanup_downloads:done",
        ok=bool(out.get("success")),
        payload={
            "dry_run": bool(dry_run),
            "deleted_count": deleted_count,
            "would_delete_count": would_delete_count,
            "freed_bytes": freed_bytes,
            "base_dirs": base_dirs,
        },
    )
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
        紙本閱卷 台北 114訴123 王小明 0407下午
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

    # Paper apply triggers
    paper_apply_triggers = ["紙本閱卷", "紙本聲請閱卷", "聲請紙本閱卷"]
    for trigger in paper_apply_triggers:
        if t.startswith(trigger):
            remainder = t[len(trigger):].strip()
            return _parse_paper_args(remainder)

    # Apply triggers
    apply_triggers = ["閱卷聲請", "聲請閱卷", "申請閱卷"]
    # 已遞委任：已另行遞交委任狀，略過上傳步驟
    _SKIP_UPLOAD_KEYWORDS = [
        "已遞委任", "已送委任", "委任已送", "委任已遞",
        "不用上傳", "無需上傳", "跳過上傳", "略過上傳",
    ]
    # 法扶模式：只上傳開辦通知書/准予扶助證明書，略過委任狀
    _LAF_ONLY_KEYWORDS = ["法扶"]
    for trigger in apply_triggers:
        if t.startswith(trigger):
            remainder = t[len(trigger):].strip()
            # 偵測「已遞委任」類關鍵字（可出現在任意位置）
            skip_upload_detected = False
            for kw in _SKIP_UPLOAD_KEYWORDS:
                if kw in remainder:
                    remainder = remainder.replace(kw, "").strip()
                    skip_upload_detected = True
                    break
            # 偵測「法扶」模式：只上傳開辦通知書，略過委任狀
            laf_only_detected = False
            for kw in _LAF_ONLY_KEYWORDS:
                if kw in remainder:
                    remainder = remainder.replace(kw, "").strip()
                    laf_only_detected = True
                    break
            parsed = _parse_apply_args(remainder)
            if parsed:
                if skip_upload_detected:
                    parsed["skip_upload"] = True
                if laf_only_detected:
                    parsed["laf_only"] = True
            return parsed

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


def _parse_case_token(token: str) -> Optional[dict]:
    s = str(token or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{2,3})\s*(?:年度)?\s*([^\d\s]+)\s*(?:字)?\s*(?:第)?\s*(\d+)\s*(?:號)?", s)
    if not m:
        return None
    case_type = re.sub(r"(字第|字|第)", "", (m.group(2) or "")).strip()
    return {"year": m.group(1), "case_type": case_type, "case_number": m.group(3)}


def _looks_like_sys_type(token: str) -> bool:
    t = str(token or "").strip()
    if not t:
        return False
    up = t.upper()
    return up in {"H", "V", "U", "I", "A", "K", "C", "F", "M", "S", "AUTO"} or t in {"民事", "刑事", "行政", "少年", "家事", "民執"}


def _looks_like_court_token(token: str) -> bool:
    t = str(token or "").strip()
    if not t:
        return False
    return _resolve_court_code(t).upper() in _ALL_COURT_CODES


def _parse_case_spec(text: str) -> Optional[dict]:
    """Parse flexible natural-language args around court/case/client/sys_type."""
    if not text:
        return None

    parts = [p for p in text.split() if p]
    if len(parts) < 2:
        return None

    case_idx = -1
    case_payload = None
    for idx, token in enumerate(parts):
        parsed = _parse_case_token(token)
        if parsed:
            case_idx = idx
            case_payload = parsed
            break

    if case_idx < 0 or not case_payload:
        return None

    remainder = [p for idx, p in enumerate(parts) if idx != case_idx]

    court_token = ""
    sys_token = ""
    client_parts = []
    for token in remainder:
        if not court_token and _looks_like_court_token(token):
            court_token = token
            continue
        if not sys_token and _looks_like_sys_type(token):
            sys_token = token
            continue
        client_parts.append(token)

    if not court_token:
        if case_idx > 0:
            court_token = parts[0]
            client_parts = [p for idx, p in enumerate(parts) if idx not in {0, case_idx}]
        else:
            return None

    result = {
        "court_code": court_token,
        "year": case_payload["year"],
        "case_type": case_payload["case_type"],
        "case_number": case_payload["case_number"],
    }
    if client_parts:
        result["client_name"] = " ".join(client_parts)
    if sys_token:
        result["sys_type"] = sys_token
    return result


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


_RE_APPOINTMENT_SLOT = re.compile(r"^(?P<month>\d{2})(?P<day>\d{2})(?P<ampm>上午|下午|AM|PM)$", re.IGNORECASE)


def _split_paper_slot_tokens(tokens: List[str]) -> Tuple[List[str], List[dict]]:
    current_year = datetime.now().year
    remain: List[str] = []
    slots: List[dict] = []
    for token in tokens:
        m = _RE_APPOINTMENT_SLOT.match(str(token or "").strip())
        if not m:
            remain.append(token)
            continue
        try:
            month = int(m.group("month"))
            day = int(m.group("day"))
            dt = datetime(current_year, month, day)
        except Exception:
            remain.append(token)
            continue
        ampm = (m.group("ampm") or "").upper()
        slots.append(
            {
                "date": dt.strftime("%Y-%m-%d"),
                "time": "上午" if ampm == "AM" or "上午" in token else "下午",
            }
        )
    return remain, slots


def _parse_paper_args(text: str) -> Optional[dict]:
    """Parse 'paper_apply' arguments from natural language."""
    tokens = [p for p in str(text or "").split() if p]
    if not tokens:
        return None
    remain, slots = _split_paper_slot_tokens(tokens)
    payload = _parse_case_spec(" ".join(remain))
    if not payload:
        return None
    payload["command"] = "paper_apply"
    if slots:
        payload["appointment_slots"] = slots
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
            try:
                print(json.dumps({"success": False, "error": "invalid JSON input"}))
            except BrokenPipeError:
                pass
            return 1
        cmd_name = cmd_data.get("cmd", "")
        if cmd_name == "upload_payment_proof_from_image":
            r = cmd_upload_payment_proof_from_image(
                image_path=cmd_data.get("image_path", ""),
                notify=cmd_data.get("notify", True),
                case_hint=cmd_data.get("case_hint", ""),
            )
            try:
                print(json.dumps(r, ensure_ascii=False))
            except BrokenPipeError:
                pass
            return 0 if r.get("success") else 1
        try:
            print(json.dumps({"success": False, "error": f"unknown json-cmd: {cmd_name}"}))
        except BrokenPipeError:
            pass
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
                'apply {"court_code":"ILD","year":"115","case_type":"原訴","case_number":"36","client_name":"[當事人C]","skip_upload":true}  # 已遞委任模式：略過上傳',
                'apply {"court_code":"ILD","year":"115","case_type":"原訴","case_number":"36","client_name":"[當事人C]","laf_only":true}  # 法扶模式：只上傳開辦/接案通知書',
                'paper_apply {"court_code":"HLD","year":"114","case_type":"花補","case_number":"502","client_name":"謝廷延","appointment_date":"2026-04-07","appointment_time":"下午"}',
                "download",
                "download_sync",
                'download {"case_number":"..."}',
                'download_status {"job_id":"latest"}',
                "download_payment_slips",
                'cleanup_downloads {"max_days":7,"pending_max_days":14,"quarantine_max_days":14,"dry_run":true}',
                'upload_payment_proof {"court_code":"HLD","year":"114","case_type":"原金訴","case_number":"166","file_path":"/path/to/screenshot.png"}',
                "process_payment_proof_queue",
                "upload_payment_proofs_batch",
                "check_emails",
                "preview_emails",
                "downloadable_probe",
                'downloadable_probe {"days":30}',
                "check_stale",
                "reauth_gmail",
                'paper_apply {"court_code":"花蓮","year":"114","case_type":"花補","case_number":"502","client_name":"謝廷延","appointment_slots":[{"date":"2026-04-07","time":"下午"}],"court_division":"簡易"}',
                'dismiss_payment {"case_keyword":"114原金訴4"}',
                'undismiss_payment {"case_keyword":"114原金訴4"}',
                "list_dismissed_payments",
            ],
            "line_triggers": [
                "閱卷查核 <法院> <案號>",
                "閱卷聲請 <法院> <案號> [<當事人>]",
                "閱卷聲請 <法院> <案號> <當事人> 已遞委任  ← 已另行遞交委任狀，略過附件上傳直接聲請",
                "閱卷聲請 <法院> <案號> <當事人> 法扶  ← 法扶案件只上傳開辦/接案通知書，不上傳委任狀",
                "紙本閱卷 <法院> <案號> <當事人> <MMDD時段> ...（如 0407下午 0408上午）",
                "下載閱卷",
                "下載閱卷 <案號>",
                "排程閱卷檢查",
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

    if task.startswith("confirm_apply") or task.startswith("confirm"):
        payload = _load_jsonish(task.split(None, 1)[1].strip() if " " in task else "{}")
        _token = payload.get("token") or payload.get("confirm_token") or task.split()[-1].strip()
        _source = payload.get("source", "cli")  # CLI 呼叫預設 source=cli → 會被安全閘門擋住
        r = _run_with_flow(
            "confirm_apply",
            lambda flow_id: cmd_confirm_apply(
                token=_token,
                notify=_boolish(payload.get("notify"), True),
                flow_id=flow_id,
                source=_source,
            ),
            metadata={"token": _token},
        )
        return _ok(r)

    if task.startswith("probe"):
        payload = _load_jsonish(task[len("probe"):].strip())
        r = _run_with_flow(
            "probe",
            lambda flow_id: cmd_probe(
                court_code=payload.get("court_code", ""),
                year=payload.get("year", ""),
                case_type=payload.get("case_type", ""),
                case_number=payload.get("case_number", ""),
                client_name=payload.get("client_name", ""),
                sys_type=payload.get("sys_type", ""),
                notify=_boolish(payload.get("notify"), True),
                flow_id=flow_id,
            ),
            metadata={
                "court_code": payload.get("court_code", ""),
                "case_number": payload.get("case_number", ""),
                "case_type": payload.get("case_type", ""),
            },
        )
        return _ok(r)

    if task.startswith("paper_apply"):
        payload = _load_jsonish(task[len("paper_apply"):].strip())
        r = _run_with_flow(
            "paper_apply",
            lambda flow_id: cmd_paper_apply(
                court_code=payload.get("court_code", ""),
                year=payload.get("year", ""),
                case_type=payload.get("case_type", ""),
                case_number=payload.get("case_number", ""),
                client_name=payload.get("client_name", ""),
                appointment_date=payload.get("appointment_date", ""),
                appointment_time=payload.get("appointment_time", "下午"),
                court_division=payload.get("court_division", ""),
                appointment_slots=payload.get("appointment_slots"),
                auto_submit=_boolish(payload.get("auto_submit"), False),
                notify=_boolish(payload.get("notify"), True),
                sys_type=payload.get("sys_type", ""),
                folder_path=payload.get("folder_path", ""),
                flow_id=flow_id,
            ),
            metadata={
                "court_code": payload.get("court_code", ""),
                "case_number": payload.get("case_number", ""),
                "case_type": payload.get("case_type", ""),
            },
        )
        return _ok(r)

    if task.startswith("apply"):
        payload = _load_jsonish(task[len("apply"):].strip())
        r = _run_with_flow(
            "apply",
            lambda flow_id: cmd_apply(
                court_code=payload.get("court_code", ""),
                year=payload.get("year", ""),
                case_type=payload.get("case_type", ""),
                case_number=payload.get("case_number", ""),
                client_name=payload.get("client_name", ""),
                auto_submit=_boolish(payload.get("auto_submit"), False),
                notify=_boolish(payload.get("notify"), True),
                sys_type=payload.get("sys_type", ""),
                folder_path=payload.get("folder_path", ""),
                flow_id=flow_id,
                skip_upload=_boolish(payload.get("skip_upload"), False),
                laf_only=_boolish(payload.get("laf_only"), False),
            ),
            metadata={
                "court_code": payload.get("court_code", ""),
                "case_number": payload.get("case_number", ""),
                "case_type": payload.get("case_type", ""),
            },
        )
        return _ok(r)

    if task.startswith("download_payment_slips") or task == "下載繳費單":
        payload = _load_jsonish(task[len("download_payment_slips"):].strip()) if task.startswith("download_payment_slips") else {}
        r = _run_with_flow(
            "download_payment_slips",
            lambda flow_id: cmd_download_payment_slips(
                max_days=int(payload.get("max_days", 14) or 14),
                notify=_boolish(payload.get("notify"), True),
                target_case_number=str(payload.get("target_case_number") or payload.get("case_number") or "").strip(),
            ),
            metadata={
                "max_days": int(payload.get("max_days", 14) or 14),
                "target_case_number": str(payload.get("target_case_number") or payload.get("case_number") or "").strip(),
            },
            step_name="payment_slip_scan",
            detail=f"max_days={int(payload.get('max_days', 14) or 14)}",
        )
        return _ok(r)

    if task.startswith("cleanup_downloads"):
        payload = _load_jsonish(task[len("cleanup_downloads"):].strip())
        r = cmd_cleanup_downloads(
            max_days=int(payload.get("max_days", payload.get("days", 7)) or 7),
            pending_max_days=int(payload.get("pending_max_days", 14) or 14),
            quarantine_max_days=int(payload.get("quarantine_max_days", 14) or 14),
            dry_run=_boolish(payload.get("dry_run"), False),
        )
        return _ok(r)

    if task.startswith("scheduled_check") or task in ("排程閱卷檢查", "閱卷排程檢查"):
        payload = _load_jsonish(task[len("scheduled_check"):].strip()) if task.startswith("scheduled_check") else {}
        r = _run_with_flow(
            "scheduled_check",
            lambda flow_id: cmd_scheduled_check(notify=_boolish(payload.get("notify"), True)),
            metadata={"source": "cron"},
            step_name="scheduled_file_review_check",
            detail="check_emails + download_payment_slips + download",
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
            notify=_boolish(payload.get("notify"), True),
        )
        return _ok(r)

    if task.startswith("process_payment_proof_queue"):
        payload = _load_jsonish(task[len("process_payment_proof_queue"):].strip())
        return _ok(cmd_process_payment_proof_queue(
            notify=_boolish(payload.get("notify"), True),
            max_items=int(payload.get("max_items", 3) or 3),
        ))

    if task.startswith("upload_payment_proofs_batch") or task == "批次上傳繳費憑證":
        payload = _load_jsonish(task[len("upload_payment_proofs_batch"):].strip()) if task.startswith("upload_payment_proofs_batch") else {}
        r = _run_with_flow(
            "upload_payment_proofs_batch",
            lambda flow_id: cmd_upload_payment_proofs_batch(
                screenshot_dir=payload.get("screenshot_dir", ""),
                notify=_boolish(payload.get("notify"), True),
            ),
            metadata={"screenshot_dir": payload.get("screenshot_dir", "")},
            step_name="payment_proof_upload",
            detail=payload.get("screenshot_dir", "") or "batch upload",
        )
        return _ok(r)

    if task.startswith("upload_payment_proof") or task == "上傳繳費憑證":
        payload = _load_jsonish(task[len("upload_payment_proof"):].strip()) if task.startswith("upload_payment_proof") else {}
        r = _run_with_flow(
            "upload_payment_proof",
            lambda flow_id: cmd_upload_payment_proof(
                court_code=payload.get("court_code", ""),
                year=payload.get("year", ""),
                case_type=payload.get("case_type", ""),
                case_number=payload.get("case_number", ""),
                client_name=payload.get("client_name", ""),
                file_path=payload.get("file_path", ""),
                notify=_boolish(payload.get("notify"), True),
            ),
            metadata={
                "court_code": payload.get("court_code", ""),
                "case_number": payload.get("case_number", ""),
                "file_path": payload.get("file_path", ""),
            },
            step_name="payment_proof_upload",
            detail=payload.get("file_path", "") or payload.get("case_number", ""),
        )
        return _ok(r)

    if task.startswith("check_emails"):
        payload = _load_jsonish(task[len("check_emails"):].strip())
        notify_empty = bool(payload.get("notify_empty", True))
        r = _run_with_flow(
            "check_emails",
            lambda flow_id: cmd_check_emails(
                notify=_boolish(payload.get("notify"), True),
                notify_empty=_boolish(payload.get("notify_empty"), True),
            ),
            metadata={"notify_empty": notify_empty},
            step_name="email_scan",
            detail=f"notify_empty={notify_empty}",
        )
        return _ok(r)

    if task == "檢查閱卷信箱":
        r = _run_with_flow(
            "check_emails",
            lambda flow_id: cmd_check_emails(),
            metadata={"source": "line_command"},
            step_name="email_scan",
            detail="line command",
        )
        return _ok(r)

    if task.startswith("preview_emails") or task in ("閱卷通知預覽", "預覽閱卷通知"):
        payload = _load_jsonish(task[len("preview_emails"):].strip()) if task.startswith("preview_emails") else {}
        kwargs = {
            "days": int(payload.get("days", 7) or 7),
            "read_only": _boolish(payload.get("read_only"), False),
        }
        if kwargs["read_only"]:
            r = cmd_preview_emails(**kwargs)
        else:
            r = _run_with_flow(
                "preview_emails",
                lambda flow_id: cmd_preview_emails(**kwargs),
                step_name="email_preview",
                detail="preview emails",
            )
        return _ok(r)

    if task.startswith("downloadable_probe") or task in ("可下載判定", "閱卷可下載判定"):
        payload = _load_jsonish(task[len("downloadable_probe"):].strip()) if task.startswith("downloadable_probe") else {}
        kwargs = {
            "days": int(payload.get("days", 30) or 30),
            "notify": _boolish(payload.get("notify"), False),
            "target_case_number": str(payload.get("target_case_number") or "").strip(),
            "dump_raw": _boolish(payload.get("dump_raw"), False),
            "require_portal": _boolish(payload.get("require_portal"), False),
            "read_only": _boolish(payload.get("read_only"), False),
        }
        if kwargs["read_only"]:
            r = cmd_downloadable_probe(**kwargs)
        else:
            r = _run_with_flow(
                "downloadable_probe",
                lambda flow_id: cmd_downloadable_probe(
                    **kwargs,
                ),
                metadata={"days": kwargs["days"]},
                step_name="downloadable_probe",
                detail=f"days={kwargs['days']}",
            )
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
        r = _run_with_flow(
            "download_sync",
            lambda flow_id: cmd_download_sync(case_number=cn, notify=_boolish(payload.get("notify"), True), flow_id=flow_id),
            metadata={"case_number": cn},
        )
        return _ok(r)

    if task == "download" or task.startswith("download "):
        payload = _load_jsonish(task[len("download"):].strip())
        cn = payload.get("case_number", "")
        notify_flag = _boolish(payload.get("notify"), True)
        if _truthy(os.environ.get("MAGI_FILE_REVIEW_DOWNLOAD_BACKGROUND", "1")):
            r = _run_with_flow(
                "download",
                lambda flow_id: cmd_download_background(case_number=cn, notify=notify_flag, flow_id=flow_id),
                metadata={"case_number": cn, "background": True},
            )
        else:
            r = _run_with_flow(
                "download",
                lambda flow_id: cmd_download(case_number=cn, notify=notify_flag, flow_id=flow_id),
                metadata={"case_number": cn, "background": False},
            )
        return _ok(r)

    if task in ("reauth_gmail", "重新授權閱卷信箱"):
        r = _run_with_flow(
            "reauth_gmail",
            lambda flow_id: cmd_reauth_gmail(notify=True),
            step_name="gmail_reauth",
            detail="reauth_gmail",
        )
        return _ok(r)

    if task.startswith("check_stale"):
        payload = _load_jsonish(task[len("check_stale"):].strip())
        r = _run_with_flow(
            "check_stale",
            lambda flow_id: cmd_check_stale(
                days=int(payload.get("days", 90) or 90),
                notify=_boolish(payload.get("notify"), True),
            ),
            metadata={"days": int(payload.get("days", 90) or 90)},
            step_name="stale_check",
            detail=f"days={int(payload.get('days', 90) or 90)}",
        )
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
        if cmd == "paper_apply":
            r = _run_with_flow(
                "paper_apply",
                lambda flow_id: cmd_paper_apply(
                    court_code=parsed.get("court_code", ""),
                    year=parsed.get("year", ""),
                    case_type=parsed.get("case_type", ""),
                    case_number=parsed.get("case_number", ""),
                    client_name=parsed.get("client_name", ""),
                    appointment_slots=parsed.get("appointment_slots"),
                    sys_type=parsed.get("sys_type", ""),
                    flow_id=flow_id,
                ),
                metadata={"source": "line_command", "case_number": parsed.get("case_number", ""), "court_code": parsed.get("court_code", "")},
            )
            return _ok(r)
        if cmd == "apply":
            r = _run_with_flow(
                "apply",
                lambda flow_id: cmd_apply(
                    court_code=parsed.get("court_code", ""),
                    year=parsed.get("year", ""),
                    case_type=parsed.get("case_type", ""),
                    case_number=parsed.get("case_number", ""),
                    client_name=parsed.get("client_name", ""),
                    sys_type=parsed.get("sys_type", ""),
                    flow_id=flow_id,
                    skip_upload=_boolish(parsed.get("skip_upload"), False),
                    laf_only=_boolish(parsed.get("laf_only"), False),
                ),
                metadata={"source": "line_command", "case_number": parsed.get("case_number", ""), "court_code": parsed.get("court_code", "")},
            )
            return _ok(r)
        if cmd == "probe":
            r = _run_with_flow(
                "probe",
                lambda flow_id: cmd_probe(
                    court_code=parsed.get("court_code", ""),
                    year=parsed.get("year", ""),
                    case_type=parsed.get("case_type", ""),
                    case_number=parsed.get("case_number", ""),
                    client_name=parsed.get("client_name", ""),
                    sys_type=parsed.get("sys_type", ""),
                    flow_id=flow_id,
                ),
                metadata={"source": "line_command", "case_number": parsed.get("case_number", ""), "court_code": parsed.get("court_code", "")},
            )
            return _ok(r)
        if cmd == "download":
            cn = parsed.get("case_number", "")
            if _truthy(os.environ.get("MAGI_FILE_REVIEW_DOWNLOAD_BACKGROUND", "1")):
                r = _run_with_flow(
                    "download",
                    lambda flow_id: cmd_download_background(case_number=cn, flow_id=flow_id),
                    metadata={"source": "line_command", "case_number": cn, "background": True},
                )
            else:
                r = _run_with_flow(
                    "download",
                    lambda flow_id: cmd_download(case_number=cn, flow_id=flow_id),
                    metadata={"source": "line_command", "case_number": cn, "background": False},
                )
            return _ok(r)
        if cmd == "check_emails":
            r = _run_with_flow(
                "check_emails",
                lambda flow_id: cmd_check_emails(),
                metadata={"source": "line_command"},
                step_name="email_scan",
                detail="line command",
            )
            return _ok(r)
        if cmd == "downloadable_probe":
            r = _run_with_flow(
                "downloadable_probe",
                lambda flow_id: cmd_downloadable_probe(),
                metadata={"source": "line_command"},
                step_name="downloadable_probe",
                detail="line command",
            )
            return _ok(r)
        if cmd == "preview_emails":
            r = _run_with_flow(
                "preview_emails",
                lambda flow_id: cmd_preview_emails(),
                metadata={"source": "line_command"},
                step_name="email_preview",
                detail="line command",
            )
            return _ok(r)
        if cmd == "reauth_gmail":
            r = _run_with_flow(
                "reauth_gmail",
                lambda flow_id: cmd_reauth_gmail(),
                metadata={"source": "line_command"},
                step_name="gmail_reauth",
                detail="line command",
            )
            return _ok(r)
        if cmd == "check_stale":
            r = _run_with_flow(
                "check_stale",
                lambda flow_id: cmd_check_stale(),
                metadata={"source": "line_command"},
                step_name="stale_check",
                detail="line command",
            )
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
