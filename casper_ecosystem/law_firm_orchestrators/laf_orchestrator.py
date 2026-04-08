# -*- coding: utf-8 -*-
"""
LAF Case Lifecycle Orchestrator
================================
Central coordinator for end-to-end LAF case automation.

Wires together:
  - LAFGmailMonitor (email trigger)
  - LAFCaseTypeParser (subject parsing)
  - LegalBridgeDB (duplicate check + DB writes)
  - LAFWebAutomation (portal file download)
  - LAFFolderBuilder (Synology Drive folder creation)
  - LAFNotifier (LINE/Discord notifications)

Usage:
    python laf_orchestrator.py --mode monitor    # Watch Gmail continuously
    python laf_orchestrator.py --mode closing    # Process 已結案待報結 cases
    python laf_orchestrator.py --mode dry-run    # Preview without writes
"""

import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import sys
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# -------------------------------------------------------------------
# Load .env (needed when invoked as subprocess)
# -------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 45, exc_info=True)

# -------------------------------------------------------------------
# Add project paths
# -------------------------------------------------------------------
MAGI_DIR = Path(os.environ.get("MAGI_ROOT_DIR", _MAGI_ROOT)).expanduser()
if str(MAGI_DIR) not in sys.path:
    sys.path.insert(0, str(MAGI_DIR))

from api.runtime_paths import ensure_path_on_sys_path, get_config_path, get_orch_dir
from api.case_path_mapper import canonical_case_roots, preferred_case_roots, translate_case_path_to_local, translate_local_path_to_canonical
from api.product_runtime import get_product_profile, resolve_laf_portal_targets

CODE_DIR = get_orch_dir()
SKILLS_DIR = MAGI_DIR / "skills"
# Max retry attempts for portal downloads before marking as exhausted
_PORTAL_RETRY_MAX_TRIES = int(os.environ.get("MAGI_LAF_PORTAL_MAX_RETRIES", "30") or "30")
CONDITION_MANUAL_DONE_PATH = CODE_DIR / "_laf_condition_manual_done.json"
CONFIG_PATH = get_config_path("config.json")

ensure_path_on_sys_path(CODE_DIR)
ensure_path_on_sys_path(SKILLS_DIR / "legal")
ensure_path_on_sys_path(SKILLS_DIR / "osc-orchestrator")

logger = logging.getLogger("laf_orchestrator")

# -------------------------------------------------------------------
# Event log (MemBridge / local JSONL) - best-effort
# -------------------------------------------------------------------
def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    try:
        if str(CODE_DIR) not in sys.path:
            sys.path.insert(0, str(CODE_DIR))
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(event, ok=ok, payload=payload or {}, tags=tags or {}, source="laf_orchestrator")
    except Exception:
        return

# -------------------------------------------------------------------
# Lazy imports (avoid import-time failures on missing deps)
# -------------------------------------------------------------------
from laf_vision import LAFVision
from laf_orchestrator_docmixins import LAFOrchestratorDocumentMixin
_db_manager = None
_legalbridge_db = None
_notifier = None
_folder_builder = None
_portal_retry_thread = None
_portal_retry_state_lock = threading.Lock()


def _get_config() -> dict:
    """Load config.json."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _get_db_manager():
    """Get or create DatabaseManager from osc.py (lazy)."""
    global _db_manager
    if _db_manager is None:
        try:
            from osc import DatabaseManager
            config = _get_config()

            prefer_local = (os.environ.get("MAGI_PREFER_LOCAL_DB", "").strip().lower() in {"1", "true", "yes", "on"})

            # Local first (when keeper/main DB is offline)
            if prefer_local:
                try:
                    from osc_headless.db import db_config_from_env

                    c = db_config_from_env()  # defaults to 127.0.0.1:3307
                    _db_manager = DatabaseManager(
                        {
                            "host": c.host,
                            "port": int(c.port),
                            "user": c.user,
                            "password": c.password,
                            "database": c.database,
                        }
                    )
                    logger.info("Connected to local DB first (MAGI_PREFER_LOCAL_DB=1): %s:%s/%s", c.host, c.port, c.database)
                    return _db_manager
                except Exception as e:
                    logger.warning("Local DB first attempt failed: %s", e)

            # Try MariaDB profiles in order
            for profile in config.get("mariadb_profiles", []):
                try:
                    _db_manager = DatabaseManager(profile["config"])
                    logger.info("Connected to DB: %s", profile["profile_name"])
                    break
                except Exception as e:
                    logger.warning("DB profile %s failed: %s", profile["profile_name"], e)

            # Fallback: Casper 本機 DB（主 DB 關機時仍可運作）
            if _db_manager is None:
                try:
                    from osc_headless.db import db_config_from_env

                    c = db_config_from_env()  # defaults to 127.0.0.1:3307
                    _db_manager = DatabaseManager(
                        {
                            "host": c.host,
                            "port": int(c.port),
                            "user": c.user,
                            "password": c.password,
                            "database": c.database,
                        }
                    )
                    logger.info("Connected to local DB via OSC_DB_*: %s:%s/%s", c.host, c.port, c.database)
                except Exception as e:
                    logger.warning("Local DB fallback failed: %s", e)
        except Exception as e:
            logger.error("Cannot import DatabaseManager: %s", e)
    return _db_manager


def _get_notifier():
    """Get or create LAFNotifier (lazy)."""
    global _notifier
    if _notifier is None:
        from line_notifier import LAFNotifier
        _notifier = LAFNotifier()
    return _notifier


def _get_folder_builder():
    """Get or create LAFFolderBuilder (lazy)."""
    global _folder_builder
    if _folder_builder is None:
        from laf_folder_builder import LAFFolderBuilder
        _folder_builder = LAFFolderBuilder()
    return _folder_builder


# ==============================================================================
# LAF Orchestrator
# ==============================================================================

class LAFOrchestrator(LAFOrchestratorDocumentMixin):
    """
    Central coordinator for LAF case lifecycle automation.

    Modes:
        - monitor: Watch Gmail for new case emails
        - closing: Process cases marked 已結案待報結
        - dry-run: Preview actions without writes
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.config = _get_config()
        self.laf_config = self.config.get("laf", {})
        self.product_profile = get_product_profile("laf", config=self.config)
        self.portal_targets = resolve_laf_portal_targets(config=self.config, profile=self.product_profile)
        self.laf_config["base_url"] = self.portal_targets.get("execute_base_url") or self.laf_config.get("base_url", "")
        self.laf_config["mock_mode"] = bool(self.portal_targets.get("execute_mock_mode"))
        # Iron Dome boundary: LAF dispatch may create case records and remind,
        # but portal opening drafts must be opt-in only.
        self.auto_portal_draft = bool(self.laf_config.get("auto_portal_draft", False))
        self.require_case_signal_for_auto = (
            os.environ.get("MAGI_REQUIRE_CASE_SIGNAL_FOR_AUTO", "1").strip().lower() in {"1", "true", "yes", "on"}
        )
        self.allow_loose_case_folder_fallback = (
            os.environ.get("MAGI_ALLOW_LOOSE_CASE_FOLDER_FALLBACK", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        self.require_handwritten_opening_notice = (
            os.environ.get("MAGI_LAF_REQUIRE_HANDWRITTEN_NOTICE", "1").strip().lower() in {"1", "true", "yes", "on"}
        )
        self.require_signature_for_general = (
            os.environ.get("MAGI_LAF_REQUIRE_SIGNATURE_ON_NOTICE", "0").strip().lower() in {"1", "true", "yes", "on"}
        )

        # Component references (lazy-loaded)
        self._db = None
        self._notifier = None
        self._folder_builder = None
        self._automation = None  # Shared browser session
        self._gmail_monitor = None
        self._last_portal_artifact = {}
        self._doc_hint_text_cache: Dict[str, str] = {}
        self._doc_hint_ocr_engine = None
        self._doc_hint_ocr_init_attempted = False
        self._portal_retry_state_path = MAGI_DIR / ".agent" / "laf_pending_portal_downloads.json"
        self._portal_retry_lock_path = MAGI_DIR / ".agent" / "laf_pending_portal_downloads.lock"

    @property
    def db(self):
        if self._db is None:
            self._db = _get_db_manager()
        return self._db

    @property
    def notifier(self):
        if self._notifier is None:
            self._notifier = _get_notifier()
        return self._notifier

    @property
    def folder_builder(self):
        if self._folder_builder is None:
            self._folder_builder = _get_folder_builder()
        return self._folder_builder

    # ==================================================================
    # Mode Entry Points
    # ==================================================================

    def run_monitor(self):
        """Start email monitoring loop."""
        logger.info("🚀 Starting LAF Email Monitor (dry_run=%s)", self.dry_run)
        _eventlog("laf:monitor:start", ok=None, payload={"dry_run": bool(self.dry_run)})

        try:
            from laf import LAFGmailMonitor
        except ImportError:
            logger.error("Cannot import LAFGmailMonitor — check skills/legal/laf.py")
            _eventlog("laf:monitor:start", ok=False, payload={"error": "import_failed_LAFGmailMonitor"})
            return

        gmail_cfg = self.config.get("gmail") if isinstance(self.config.get("gmail"), dict) else {}
        credentials_path = (
            os.environ.get("MAGI_GMAIL_CREDENTIALS_PATH", "").strip()
            or str(gmail_cfg.get("credentials_path") or "").strip()
            or str(self.config.get("google_credentials_path") or "").strip()
        )
        if not credentials_path:
            credentials_path = str(get_config_path("credentials.json"))

        token_path = (
            os.environ.get("MAGI_LAF_GMAIL_TOKEN_PATH", "").strip()
            or str(gmail_cfg.get("token_path") or "").strip()
            or str(self.config.get("google_token_path") or "").strip()
        )
        if not token_path:
            token_path = str(get_config_path("laf_gmail_token.pickle"))

        if not credentials_path or not os.path.exists(credentials_path):
            logger.error("Google credentials not found: %s", credentials_path)
            _eventlog(
                "laf:monitor:start",
                ok=False,
                payload={"error": "credentials_not_found", "credentials_path": credentials_path},
            )
            return

        monitor = LAFGmailMonitor(
            credentials_path=credentials_path,
            token_path=token_path,
            callback=self.on_new_email,
            log_callback=lambda msg: logger.info("[Gmail] %s", msg),
        )
        self._gmail_monitor = monitor

        interval = self.laf_config.get("check_interval", 300)
        logger.info("Monitoring every %ds...", interval)
        _eventlog(
            "laf:monitor:configured",
            ok=True,
            payload={
                "interval_sec": int(interval),
                "credentials_path": credentials_path,
                "token_path": token_path,
            },
        )
        # Start Gmail scanning FIRST — portal retries are slow (NAS + Selenium)
        # and must not block the critical email monitor.
        monitor.start_monitor(interval_seconds=interval)

        # Portal retries run after monitor is already active
        try:
            self._seed_pending_portal_retries_from_case_inventory(limit=80)
            self._retry_pending_portal_downloads(max_items=6)
            self._ensure_pending_portal_retry_loop(interval_seconds=interval)
        except Exception as e:
            logger.warning("Portal retry setup failed (non-fatal): %s", e)

    def _load_pending_portal_downloads(self) -> Dict[str, dict]:
        path = self._portal_retry_state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 322, exc_info=True)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # Backup corrupted file and start fresh
            try:
                backup = str(path) + f".corrupt.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                import shutil
                shutil.copy2(str(path), backup)
                logger.warning("Portal download queue corrupted, backed up: %s → %s", path, backup)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 335, exc_info=True)
            return {}
        except Exception as e:
            logger.warning("Failed to read pending portal download queue: %s", e)
            return {}

        items = payload.get("items", payload) if isinstance(payload, dict) else {}
        out: Dict[str, dict] = {}
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                laf_case_no = str(item.get("laf_case_number") or "").strip()
                if laf_case_no:
                    out[laf_case_no] = item
        elif isinstance(items, dict):
            for laf_case_no, item in items.items():
                laf_case_no = str(laf_case_no or "").strip()
                if not laf_case_no:
                    continue
                if isinstance(item, dict):
                    item = dict(item)
                    item.setdefault("laf_case_number", laf_case_no)
                    out[laf_case_no] = item
        return out

    def _save_pending_portal_downloads(self, items: Dict[str, dict]) -> None:
        path = self._portal_retry_state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 366, exc_info=True)
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "items": [
                dict(item, laf_case_number=laf_case_no)
                for laf_case_no, item in sorted((items or {}).items(), key=lambda kv: kv[0])
                if laf_case_no
            ],
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _acquire_pending_portal_retry_lock(self) -> bool:
        try:
            self._portal_retry_lock_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 383, exc_info=True)
        try:
            fd = os.open(str(self._portal_retry_lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n{datetime.now().isoformat()}\n")
            return True
        except FileExistsError:
            return False
        except Exception as e:
            logger.warning("Failed to acquire portal retry lock: %s", e)
            return False

    def _release_pending_portal_retry_lock(self) -> None:
        try:
            if self._portal_retry_lock_path.exists():
                self._portal_retry_lock_path.unlink()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 400, exc_info=True)

    def _resolve_case_folder_for_laf(self, laf_number: str, fallback: str = "") -> str:
        laf_case_no = str(laf_number or "").strip()
        if laf_case_no and self.db:
            try:
                row = self.db.fetch_one(
                    "SELECT `folder_path` FROM `cases` WHERE `legal_aid_number` = %s ORDER BY `id` DESC LIMIT 1",
                    (laf_case_no,),
                    as_dict=True,
                )
                if row:
                    folder_path = self._to_local_case_folder(str(row.get("folder_path") or ""))
                    if folder_path:
                        return folder_path
            except Exception as e:
                logger.warning("Resolve case folder by laf number failed (%s): %s", laf_case_no, e)
        return self._to_local_case_folder(str(fallback or ""))

    @staticmethod
    def _is_legacy_closed_archive_path(path_value: str) -> bool:
        canonical = translate_local_path_to_canonical(str(path_value or "")).replace("\\", "/")
        closed_roots = canonical_case_roots(include_closed=True)[1:]
        for root in closed_roots:
            root_norm = str(root or "").strip().replace("\\", "/").rstrip("/")
            if not root_norm:
                continue
            if canonical == root_norm or canonical.startswith(root_norm + "/"):
                return True
        return False

    def _queue_pending_portal_download(
        self,
        *,
        laf_number: str,
        client_name: str = "",
        case_type: str = "",
        case_reason: str = "",
        case_folder: str = "",
        case_number: str = "",
        reason: str = "",
        last_error: str = "",
    ) -> bool:
        laf_case_no = str(laf_number or "").strip()
        if not laf_case_no:
            return False

        now_iso = datetime.now().isoformat(timespec="seconds")
        with _portal_retry_state_lock:
            items = self._load_pending_portal_downloads()
            item = dict(items.get(laf_case_no) or {})

            # 防止已耗盡的項目被 startup backfill 重複入隊
            existing_status = str(item.get("status") or "").strip().lower()
            existing_tries = int(item.get("tries") or 0)
            if existing_status == "exhausted" or existing_tries > _PORTAL_RETRY_MAX_TRIES:
                logger.debug(
                    "Skip re-queuing exhausted item %s (tries=%d, status=%s)",
                    laf_case_no, existing_tries, existing_status,
                )
                return False

            first_queued_at = str(item.get("first_queued_at") or now_iso)
            item.update(
                {
                    "laf_case_number": laf_case_no,
                    "client_name": str(client_name or item.get("client_name") or "").strip(),
                    "case_type": str(case_type or item.get("case_type") or "").strip(),
                    "case_reason": str(case_reason or item.get("case_reason") or "").strip(),
                    "case_folder": str(case_folder or item.get("case_folder") or "").strip(),
                    "case_number": str(case_number or item.get("case_number") or "").strip(),
                    "status": "pending_retry",
                    "reason": str(reason or item.get("reason") or "portal_not_listed").strip(),
                    "last_error": str(last_error or item.get("last_error") or "").strip(),
                    "first_queued_at": first_queued_at,
                    "updated_at": now_iso,
                }
            )
            item.setdefault("origin_reason", str(reason or item.get("origin_reason") or item.get("reason") or "").strip())
            item.setdefault("tries", 0)
            item.setdefault("last_try_at", "")
            items[laf_case_no] = item
            self._save_pending_portal_downloads(items)

        _eventlog(
            "laf:portal:retry:queued",
            ok=True,
            payload={"reason": item.get("reason"), "case_folder": os.path.basename(str(item.get("case_folder") or "").rstrip("/\\"))},
            tags={"laf_case_no": laf_case_no, "client_name": str(item.get("client_name") or "")},
        )
        return True

    def _clear_pending_portal_download(self, laf_number: str) -> None:
        laf_case_no = str(laf_number or "").strip()
        if not laf_case_no:
            return
        with _portal_retry_state_lock:
            items = self._load_pending_portal_downloads()
            if laf_case_no in items:
                items.pop(laf_case_no, None)
                self._save_pending_portal_downloads(items)

    def _archive_portal_downloads(self, files: List[str], case_folder: str) -> dict:
        result = {
            "ok": False,
            "new_files": [],
            "skipped_existing": [],
            "zip_backups": [],
            "zip_backup_skipped": [],
            "error": "",
        }
        folder = str(case_folder or "").strip()
        if not folder or not os.path.isdir(folder):
            result["error"] = "missing_case_folder"
            return result
        try:
            from laf_automation_v2 import OSCCaseCreator

            archiver = OSCCaseCreator(
                db_manager=self.db,
                target_folder=self.laf_config.get("target_folder", ""),
                log_callback=lambda msg: logger.info("[LAF-ARCHIVE] %s", msg),
            )
            archived = archiver._archive_files_to_folder(files, folder) or {}
            if isinstance(archived, dict):
                result.update(archived)
            result["ok"] = True
            return result
        except Exception as e:
            logger.error("Archive portal downloads failed: %s", e)
            result["error"] = str(e)
            return result

    def _process_portal_download_result(
        self,
        *,
        laf_number: str,
        client_name: str = "",
        case_type: str = "",
        case_reason: str = "",
        case_folder: str = "",
        case_number: str = "",
        files: Optional[List[str]] = None,
        source: str = "initial",
        last_error: str = "",
    ) -> dict:
        laf_case_no = str(laf_number or "").strip()
        portal_files = [str(f) for f in (files or []) if f]
        folder = self._resolve_case_folder_for_laf(laf_case_no, fallback=case_folder)
        result = {
            "ok": True,
            "laf_case_number": laf_case_no,
            "source": str(source or "initial"),
            "downloaded_files": portal_files,
            "downloaded_count": len(portal_files),
            "case_folder": folder,
            "retry_queued": False,
            "retry_reason": "",
            "archive": {
                "ok": False,
                "new_files": [],
                "skipped_existing": [],
                "zip_backups": [],
                "zip_backup_skipped": [],
                "error": "",
            },
        }

        if portal_files:
            archived = self._archive_portal_downloads(portal_files, folder)
            result["archive"] = archived
            if not archived.get("ok"):
                queued = self._queue_pending_portal_download(
                    laf_number=laf_case_no,
                    client_name=client_name,
                    case_type=case_type,
                    case_reason=case_reason,
                    case_folder=folder or case_folder,
                    case_number=case_number,
                    reason="archive_failed",
                    last_error=str(archived.get("error") or ""),
                )
                result["retry_queued"] = queued
                result["retry_reason"] = "archive_failed"
                _eventlog(
                    "laf:portal:download:done",
                    ok=False,
                    payload={"error": str(archived.get("error") or "archive_failed")[:300]},
                    tags={"laf_case_no": laf_case_no, "client_name": client_name},
                )
                return result
            self._clear_pending_portal_download(laf_case_no)
            _eventlog(
                "laf:portal:retry:done" if source == "retry" else "laf:portal:download:done",
                ok=True,
                payload={
                    "downloaded_count": len(portal_files),
                    "new_count": len(archived.get("new_files") or []),
                    "skipped_existing_count": len(archived.get("skipped_existing") or []),
                },
                tags={"laf_case_no": laf_case_no, "client_name": client_name},
            )
            return result

        retry_reason = "portal_not_listed"
        if last_error:
            retry_reason = "portal_check_failed"
        queued = self._queue_pending_portal_download(
            laf_number=laf_case_no,
            client_name=client_name,
            case_type=case_type,
            case_reason=case_reason,
            case_folder=folder or case_folder,
            case_number=case_number,
            reason=retry_reason,
            last_error=last_error,
        )
        result["retry_queued"] = queued
        result["retry_reason"] = retry_reason
        _eventlog(
            "laf:portal:retry:waiting",
            ok=True,
            payload={"reason": retry_reason, "source": source, "last_error": str(last_error or "")[:300]},
            tags={"laf_case_no": laf_case_no, "client_name": client_name},
        )
        return result

    def _ensure_pending_portal_retry_loop(self, interval_seconds: int) -> None:
        global _portal_retry_thread
        if self.dry_run:
            return
        interval = max(60, int(interval_seconds or 300))
        if _portal_retry_thread and _portal_retry_thread.is_alive():
            return

        def _loop(owner: "LAFOrchestrator", every_sec: int) -> None:
            logger.info("🔁 LAF portal retry loop started (every %ss)", every_sec)
            _eventlog("laf:portal:retry:loop_start", ok=True, payload={"interval_sec": every_sec})
            while True:
                try:
                    owner._retry_pending_portal_downloads(max_items=6)
                except Exception as e:
                    logger.error("Pending portal retry loop failed: %s", e)
                time.sleep(every_sec)

        _portal_retry_thread = threading.Thread(
            target=_loop,
            args=(self, interval),
            daemon=True,
            name="laf-portal-retry-loop",
        )
        _portal_retry_thread.start()

    def _seed_pending_portal_retries_from_case_inventory(self, limit: int = 80) -> dict:
        if self.dry_run or not self.db:
            return {"ok": True, "seeded": 0, "scanned": 0, "skipped": "dry_run_or_no_db"}

        query = """
            SELECT `case_number`, `client_name`, `case_type`, `case_reason`,
                   `legal_aid_number`, `folder_path`, `status`, `created_date`
            FROM `cases`
            WHERE `case_category` = '法律扶助案件'
              AND `legal_aid_number` IS NOT NULL
              AND TRIM(`legal_aid_number`) <> ''
            ORDER BY `created_date` DESC
            LIMIT %s
        """
        try:
            rows = self.db.fetch_all(query, (int(limit or 40),))
        except Exception as e:
            logger.warning("Seed pending portal retries failed: %s", e)
            return {"ok": False, "error": str(e)}

        seeded = 0
        scanned = 0
        for row in rows or []:
            if isinstance(row, dict):
                case_number = str(row.get("case_number") or "").strip()
                client_name = str(row.get("client_name") or "").strip()
                case_type = str(row.get("case_type") or "").strip()
                case_reason = str(row.get("case_reason") or "").strip()
                laf_case_no = str(row.get("legal_aid_number") or "").strip()
                folder_path = str(row.get("folder_path") or "").strip()
            else:
                case_number, client_name, case_type, case_reason, laf_case_no, folder_path, _status, _created = row
                case_number = str(case_number or "").strip()
                client_name = str(client_name or "").strip()
                case_type = str(case_type or "").strip()
                case_reason = str(case_reason or "").strip()
                laf_case_no = str(laf_case_no or "").strip()
                folder_path = str(folder_path or "").strip()
                status_value = str(_status or "").strip()
            if isinstance(row, dict):
                status_value = str(row.get("status") or "").strip()

            if not laf_case_no:
                continue
            scanned += 1
            local_folder = self._to_local_case_folder(folder_path)
            docs = self._scan_case_folder_docs(local_folder)
            status_norm = status_value.strip().lower()
            is_closed = status_norm in {"已結案", "closed", "completed", "archived"}
            if is_closed and self._is_legacy_closed_archive_path(folder_path) and (not local_folder or not os.path.isdir(local_folder)):
                _eventlog(
                    "laf:portal:retry:seed_skipped",
                    ok=False,
                    payload={"reason": "missing_local_case_folder", "folder_path": folder_path},
                    tags={"laf_case_no": laf_case_no, "client_name": client_name},
                )
                continue
            if is_closed:
                needs_queue = (not docs.get("closing_fee_files")) or (not docs.get("change_review_notice_files"))
                queue_reason = "startup_backfill_missing_closing_docs"
            else:
                needs_queue = (not docs.get("opening_notice_files")) and (not docs.get("poa_files"))
                queue_reason = "startup_backfill_missing_opening_docs"
            if not needs_queue:
                continue
            if self._queue_pending_portal_download(
                laf_number=laf_case_no,
                client_name=client_name,
                case_type=case_type,
                case_reason=case_reason,
                case_folder=local_folder,
                case_number=case_number,
                reason=queue_reason,
            ):
                seeded += 1

        if seeded:
            logger.info("🔁 Seeded %d LAF cases into portal retry queue", seeded)
        return {"ok": True, "seeded": seeded, "scanned": scanned}

    def _retry_pending_portal_downloads(self, max_items: int = 6) -> dict:
        if self.dry_run:
            return {"ok": True, "skipped": True, "reason": "dry_run"}

        with _portal_retry_state_lock:
            items = self._load_pending_portal_downloads()

        pending_items = [
            dict(item)
            for item in items.values()
            if str(item.get("laf_case_number") or "").strip()
            and str(item.get("status") or "pending_retry").strip().lower() in ("pending_retry", "")
        ]
        if not pending_items:
            return {"ok": True, "scanned": 0, "processed": 0, "items": []}

        if not self._acquire_pending_portal_retry_lock():
            return {"ok": True, "skipped": True, "reason": "locked", "pending": len(pending_items)}

        processed: List[dict] = []
        try:
            username = os.environ.get("MAGI_LAF_USERNAME") or self.laf_config.get("username", "")
            password = os.environ.get("MAGI_LAF_PASSWORD") or self.laf_config.get("password", "")
            download_folder = self.laf_config.get("download_folder", "./laf_downloads")
            headless = self.laf_config.get("headless", True)
            if not username or not password:
                return {"ok": False, "error": "missing_credentials", "pending": len(pending_items)}

            from laf_automation_v2 import LAFWebAutomation

            ordered = sorted(
                pending_items,
                key=lambda item: (
                    str(item.get("last_try_at") or ""),
                    str(item.get("first_queued_at") or ""),
                    str(item.get("laf_case_number") or ""),
                ),
            )[: max(1, int(max_items or 1))]

            automation = LAFWebAutomation(
                username=username,
                password=password,
                download_folder=download_folder,
                headless=headless,
                log_callback=lambda msg: logger.info("[LAF-RETRY] %s", msg),
                browser_profile_dir=self.laf_config.get("browser_profile_dir", ""),
            )

            try:
                automation.login()
                for item in ordered:
                    laf_case_no = str(item.get("laf_case_number") or "").strip()
                    if not laf_case_no:
                        continue
                    now_iso = datetime.now().isoformat(timespec="seconds")
                    updated = dict(item)
                    raw_case_folder = str(updated.get("case_folder") or "").strip()
                    local_case_folder = self._to_local_case_folder(raw_case_folder)
                    if local_case_folder:
                        updated["case_folder"] = local_case_folder
                    if (
                        raw_case_folder
                        and self._is_legacy_closed_archive_path(raw_case_folder)
                        and (not local_case_folder or not os.path.isdir(local_case_folder))
                    ):
                        updated["status"] = "manual_review"
                        updated["last_error"] = "missing_local_case_folder"
                        updated["updated_at"] = now_iso
                        with _portal_retry_state_lock:
                            queue_items = self._load_pending_portal_downloads()
                            queue_items[laf_case_no] = dict(queue_items.get(laf_case_no) or {}, **updated)
                            self._save_pending_portal_downloads(queue_items)
                        _eventlog(
                            "laf:portal:retry:stopped",
                            ok=False,
                            payload={"reason": "missing_local_case_folder", "case_folder": raw_case_folder},
                            tags={"laf_case_no": laf_case_no, "client_name": str(updated.get("client_name") or "")},
                        )
                        processed.append(
                            {
                                "laf_case_number": laf_case_no,
                                "downloaded_count": 0,
                                "queued": False,
                                "status": "manual_review",
                                "error": "missing_local_case_folder",
                            }
                        )
                        continue
                    updated["tries"] = int(updated.get("tries") or 0) + 1
                    updated["last_try_at"] = now_iso
                    updated["updated_at"] = now_iso

                    # Max retries check: stop retrying and notify admin
                    if updated["tries"] > _PORTAL_RETRY_MAX_TRIES:
                        updated["status"] = "exhausted"
                        with _portal_retry_state_lock:
                            queue_items = self._load_pending_portal_downloads()
                            queue_items[laf_case_no] = dict(queue_items.get(laf_case_no) or {}, **updated)
                            self._save_pending_portal_downloads(queue_items)
                        _eventlog(
                            "laf:portal:retry:exhausted",
                            ok=False,
                            payload={"tries": updated["tries"], "max": _PORTAL_RETRY_MAX_TRIES},
                            tags={"laf_case_no": laf_case_no, "client_name": str(updated.get("client_name") or "")},
                        )
                        try:
                            self.notifier.notify_admin(
                                f"🚨 法扶附件下載重試已達上限\n"
                                f"案號: {laf_case_no}\n"
                                f"當事人: {updated.get('client_name', '')}\n"
                                f"已嘗試: {updated['tries']} 次（上限 {_PORTAL_RETRY_MAX_TRIES}）\n"
                                f"原因: {updated.get('reason', '')}\n"
                                f"請人工處理或清除佇列。"
                            )
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 848, exc_info=True)
                        processed.append({"laf_case_number": laf_case_no, "downloaded_count": 0, "status": "exhausted"})
                        continue

                    _eventlog(
                        "laf:portal:retry:attempt",
                        ok=None,
                        payload={"tries": updated["tries"]},
                        tags={"laf_case_no": laf_case_no, "client_name": str(updated.get("client_name") or "")},
                    )

                    try:
                        files = automation.download_case_files(laf_case_no)
                        result = self._process_portal_download_result(
                            laf_number=laf_case_no,
                            client_name=str(updated.get("client_name") or ""),
                            case_type=str(updated.get("case_type") or ""),
                            case_reason=str(updated.get("case_reason") or ""),
                            case_folder=str(updated.get("case_folder") or ""),
                            case_number=str(updated.get("case_number") or ""),
                            files=files,
                            source="retry",
                        )
                        if result.get("downloaded_count"):
                            archive = result.get("archive", {})
                            new_files = archive.get("new_files") or []
                            skipped_files = archive.get("skipped_existing") or []
                            new_count = len(new_files)
                            skipped_count = len(skipped_files)
                            notify_lines = [
                                "📥 法扶官網附件已補抓",
                                f"當事人: {updated.get('client_name') or ''}",
                                f"案號: {laf_case_no}",
                                f"新增檔案: {new_count} 份",
                            ]
                            if skipped_count:
                                notify_lines.append(f"去重略過: {skipped_count} 份")
                            # 列出實際下載的檔案名稱
                            for fn in new_files[:10]:
                                notify_lines.append(f"  ✓ {os.path.basename(fn)}")
                            for fn in skipped_files[:5]:
                                notify_lines.append(f"  ⏭️ {os.path.basename(fn)}")
                            # 只在有新增檔案時才附上開辦通知/委任狀統計
                            if new_count:
                                docs = self._scan_case_folder_docs(str(result.get("case_folder") or ""))
                                notify_lines.append(f"開辦通知: {len(docs.get('opening_notice_files') or [])} 份")
                                notify_lines.append(f"委任狀: {len(docs.get('poa_files') or [])} 份")
                            self.notifier.notify_admin("\n".join(notify_lines))
                        else:
                            with _portal_retry_state_lock:
                                queue_items = self._load_pending_portal_downloads()
                                queue_items[laf_case_no] = dict(queue_items.get(laf_case_no) or {}, **updated)
                                self._save_pending_portal_downloads(queue_items)
                        processed.append(
                            {
                                "laf_case_number": laf_case_no,
                                "downloaded_count": int(result.get("downloaded_count") or 0),
                                "queued": bool(result.get("retry_queued")),
                            }
                        )
                    except Exception as e:
                        updated["last_error"] = str(e)
                        with _portal_retry_state_lock:
                            queue_items = self._load_pending_portal_downloads()
                            queue_items[laf_case_no] = dict(queue_items.get(laf_case_no) or {}, **updated)
                            self._save_pending_portal_downloads(queue_items)
                        logger.error("Retry portal download failed for %s: %s", laf_case_no, e)
                        _eventlog(
                            "laf:portal:retry:attempt",
                            ok=False,
                            payload={"error": str(e)[:300], "tries": updated["tries"]},
                            tags={"laf_case_no": laf_case_no, "client_name": str(updated.get("client_name") or "")},
                        )
                        processed.append({"laf_case_number": laf_case_no, "downloaded_count": 0, "queued": True, "error": str(e)})
            finally:
                automation.close()
        finally:
            self._release_pending_portal_retry_lock()

        return {"ok": True, "scanned": len(pending_items), "processed": len(processed), "items": processed}

    def run_closing(self):
        """
        Process all cases marked 已結案待報結.
        This is the admin-in-the-loop closing flow.
        """
        logger.info("📋 Starting Closing Report Processor (dry_run=%s)", self.dry_run)
        _eventlog("laf:closing:start", ok=None, payload={"dry_run": bool(self.dry_run)})

        pending_cases = self._get_pending_closing_cases()
        if not pending_cases:
            logger.info("No pending closing cases found.")
            _eventlog("laf:closing:skipped", ok=True, payload={"reason": "no_pending_cases"})
            return

        logger.info("Found %d cases pending closing:", len(pending_cases))
        for case in pending_cases:
            case_num = case[0] if isinstance(case, (list, tuple)) else case.get("case_number", "")
            client = case[1] if isinstance(case, (list, tuple)) else case.get("client_name", "")
            logger.info("  - %s (%s)", client, case_num)

        # Process each case
        for case in pending_cases:
            self.prepare_closing_report(case)

    # ==================================================================
    # Email Handler (Go-Live / New Case)
    # ==================================================================

    def on_new_email(self, case_info):
        """
        Callback from LAFGmailMonitor when a new LAF email is detected.
        Routes to appropriate handler based on notification type.
        """
        logger.info("📧 New email: %s", getattr(case_info, 'subject', str(case_info)))

        # Determine event type from the parsed case info
        notification_type = getattr(case_info, 'notification_type', 'dispatch')
        client_name = getattr(case_info, 'client_name', '')
        laf_number = getattr(case_info, 'laf_case_number', '')

        logger.info("  Type: %s, Client: %s, LAF#: %s",
                     notification_type, client_name, laf_number)
        _eventlog(
            "laf:email:received",
            ok=True,
            payload={"notification_type": notification_type, "subject": (getattr(case_info, "subject", "") or "")[:200]},
            tags={"laf_case_no": laf_number, "client_name": client_name},
        )

        if notification_type in ('dispatch', '派案'):
            self.handle_go_live(case_info)
        elif notification_type in ('withdrawal', '撤回'):
            self.handle_withdrawal(case_info)
        elif notification_type in ('inquiry', '疑義'):
            self.handle_inquiry(case_info)
        elif notification_type in ('fee', '費用'):
            self.handle_fee_payment(case_info)
        else:
            # Default: treat as new case dispatch
            self.handle_go_live(case_info)

    # In-memory dedup for go_live to prevent duplicate notifications when
    # multiple emails arrive for the same case (e.g., 派案通知 + 附加檔案).
    _go_live_dedup: set = set()

    def handle_go_live(self, case_info):
        """
        開辦 flow:
        1. Check for duplicate in DB
        2. Create folder on SynologyDrive
        3. Insert/update DB record with Z: canonical path
        4. Download files from LAF portal
        5. Notify admin
        """
        client_name = getattr(case_info, 'client_name', '')
        case_type = getattr(case_info, 'case_type', '')
        case_reason = getattr(case_info, 'case_reason', '')
        laf_number = getattr(case_info, 'laf_case_number', '')

        # Dedup: skip if same laf_number already processed in this session
        if laf_number and laf_number in self._go_live_dedup:
            logger.info("⏭️ Go-Live dedup: %s (%s) already processed this session", client_name, laf_number)
            return
        if laf_number:
            self._go_live_dedup.add(laf_number)

        logger.info("🏁 Go-Live: %s (%s)", client_name, laf_number)
        _eventlog(
            "laf:go_live:start",
            ok=None,
            payload={"case_type": case_type, "case_reason": case_reason, "dry_run": bool(self.dry_run)},
            tags={"laf_case_no": laf_number, "client_name": client_name},
        )

        db_path = ""
        # Step 1: Duplicate check
        existing = self._check_duplicate(laf_number, client_name, case_type, case_reason)

        if existing:
            case_number = existing.get("case_number", "")
            logger.info("  ↳ Existing case found: %s", case_number)
            # 已開辦案件不再重複通知（進行中、已結案等狀態直接跳過）
            laf_status = str(existing.get("legal_aid_status") or "").strip()
            if laf_status and laf_status != "未開辦":
                logger.info("  ⏭️ 案件已為「%s」狀態，跳過重複通知", laf_status)
                _eventlog(
                    "laf:go_live:done",
                    ok=True,
                    payload={"skipped": True, "reason": f"already_{laf_status}", "case_number": case_number},
                    tags={"laf_case_no": laf_number, "client_name": client_name},
                )
                return
            db_path = self._to_local_case_folder(str(existing.get("folder_path") or ""))
            # Update legal_aid_number if not set
            if not existing.get("legal_aid_number") and laf_number:
                self._update_legal_aid_number(existing.get("id"), laf_number)
        else:
            # Step 2: Create folder
            case_number = ""
            if not self.dry_run:
                case_number = self._generate_case_number()
                if not case_number:
                    logger.error("  ❌ Standard case number generation failed")
                    self._log_event(laf_number, "go_live", {"error": "case_number_generation_failed"}, "failed")
                    _eventlog("laf:go_live:done", ok=False, payload={"error": "case_number_generation_failed"}, tags={"laf_case_no": laf_number, "client_name": client_name})
                    return
            folder_info = {
                "case_number": case_number,
                "client_name": client_name,
                "case_type": case_type,
                "case_reason": case_reason,
                "laf_case_number": laf_number,
                "case_stage": getattr(case_info, 'case_stage', ''),
            }

            if self.dry_run:
                logger.info("  [DRY RUN] Would create folder for %s", client_name)
                db_path = "Z:/lumi63181107/01_案件/法扶案件/" + f"{case_type}/{case_number or client_name}"
            else:
                db_path = self.folder_builder.create_case_folder(folder_info)

            if not db_path:
                logger.error("  ❌ Folder creation failed")
                self._log_event(laf_number, "go_live", {"error": "folder_creation_failed"}, "failed")
                _eventlog("laf:go_live:done", ok=False, payload={"error": "folder_creation_failed"}, tags={"laf_case_no": laf_number, "client_name": client_name})
                return

            # Step 3: Insert DB record
            if not self.dry_run:
                case_number = self._create_case_record(case_info, db_path, case_number=case_number) or ""

        local_case_folder = self._to_local_case_folder(db_path)
        email_attachment_result = {
            "ok": False,
            "downloaded_count": 0,
            "new_count": 0,
            "skipped_existing_count": 0,
            "error": "",
        }
        if local_case_folder and not self.dry_run:
            try:
                self._archive_case_email_snapshot(case_info, local_case_folder)
            except Exception as archive_email_error:
                logger.warning("Failed to archive LAF email snapshot for %s: %s", laf_number, archive_email_error)
            try:
                email_attachment_result = self._download_case_email_attachments(case_info, local_case_folder)
            except Exception as email_attachment_error:
                logger.warning("Failed to archive LAF email attachments for %s: %s", laf_number, email_attachment_error)

        # Step 4: Download files (if enabled)
        download_result = {}
        if self.laf_config.get("auto_create_case", True) and not self.dry_run:
            download_result = self._download_case_files(
                laf_number,
                case_folder=db_path,
                client_name=client_name,
                case_type=case_type,
                case_reason=case_reason,
                case_number=case_number,
            )

        # Vision Step: Extract Start Date from downloaded files
        # 全案件掃描（供二階段、結案等使用），但開辦判斷只認 02_開辦資料 內的檔案
        extracted_date = None
        poa_submit_date = None
        open_doc = None
        poa_doc = None
        docs = self._empty_docs_map()
        go_live_docs = self._empty_docs_map()
        if not self.dry_run and db_path:
            try:
                docs = self._scan_case_folder_docs(db_path)
                local_root = self._to_local_case_folder(db_path) or db_path
                go_live_dir = os.path.join(local_root, "02_開辦資料")
                go_live_docs = self._scan_case_folder_docs(go_live_dir) if os.path.isdir(go_live_dir) else self._empty_docs_map()
                open_doc = (go_live_docs.get("opening_notice_files") or [None])[0]
                poa_doc = (go_live_docs.get("poa_files") or [None])[0]
                if open_doc:
                    extracted_date = self._extract_best_date_from_doc(open_doc)
                if poa_doc:
                    poa_submit_date = self._extract_best_date_from_doc(poa_doc)
                if extracted_date:
                    logger.info("  🎯 開辦通知日期：%s", extracted_date)
                if poa_submit_date:
                    logger.info("  🎯 委任狀遞出日期：%s", poa_submit_date)
            except Exception as e:
                logger.error(f"  ❌ Vision extraction failed: {e}")

        _is_consumer_debt = self._is_consumer_debt_case_folder(db_path or "")
        # 消債案件只需開辦通知書；一般案件需要開辦通知書 + 遞狀證明（委任狀/書狀存底/回執）
        docs_ready_for_go_live = bool(open_doc) if _is_consumer_debt else bool(open_doc and poa_doc)

        # Step 4.5: 偵測遞狀日期 + 自動填寫開辦表單（預覽，不送出）
        submission_info: dict = {}
        go_live_remark = ""
        go_live_upload_file = ""
        go_live_draft_ok = False

        if not self.dry_run:
            try:
                # 消債案件也要嘗試自動開辦（有開辦通知書就夠）
                _can_try_go_live = docs_ready_for_go_live or (open_doc and not _is_consumer_debt)
                if _can_try_go_live or _is_consumer_debt:
                    submission_info = self._detect_poa_submission_info(db_path)
                    confidence = submission_info.get("confidence", "low")
                    logger.info("  📋 遞狀日期偵測: confidence=%s, info=%s", confidence, submission_info)

                    if confidence in ("high", "medium") or _is_consumer_debt:
                        go_live_remark = self._compose_go_live_remark(
                            submission_info, client_name, is_consumer_debt=_is_consumer_debt
                        )
                        go_live_upload_files = self._find_go_live_upload_files(
                            db_path, is_consumer_debt=_is_consumer_debt
                        )
                        logger.info("  📋 selRemark: %s", go_live_remark)
                        logger.info("  📋 上傳檔案: %s", [os.path.basename(f) for f in go_live_upload_files])

                        if go_live_upload_files and (go_live_remark or _is_consumer_debt):
                            if not go_live_remark and _is_consumer_debt:
                                go_live_remark = "已簽署開辦通知書。"
                            fields = {
                                "sel_result": "1",  # 已開辦
                                "remark": go_live_remark,
                                "upload_files": go_live_upload_files,
                            }
                            go_live_draft_ok = self.execute_portal_go_live_draft(
                                laf_number, client_name, fields
                            )
                            logger.info("  📋 開辦 draft 結果: %s", go_live_draft_ok)
            except Exception as gl_e:
                logger.error("  ❌ 開辦自動化失敗: %s", gl_e)

        go_live_reminder = ""
        if not self.dry_run:
            if go_live_draft_ok:
                go_live_reminder = "✅ 開辦表單已自動填寫（未送出），截圖已傳送，請確認後回覆。"
            elif docs_ready_for_go_live:
                if submission_info.get("confidence") == "low":
                    go_live_reminder = "⚠️ 開辦資料齊備，但找不到遞狀日期，請手動開辦。"
                else:
                    go_live_reminder = "⚠️ 開辦資料齊備，自動填寫失敗，請手動開辦。"
            else:
                missing_parts = []
                if not open_doc:
                    missing_parts.append("開辦通知")
                if not poa_doc:
                    missing_parts.append("委任狀")
                go_live_reminder = f"⚠️ 尚缺{'、'.join(missing_parts)}，請補齊後手動開辦。"
            logger.info("  📋 Go-live reminder: %s", go_live_reminder)

        # Step 5: Notify (text + opening notice image for confirmation)
        # 通知計數以 02_開辦資料 為準
        opening_notice_count = len(go_live_docs.get("opening_notice_files") or [])
        poa_count = len(go_live_docs.get("poa_files") or [])
        folder_label = os.path.basename(str(db_path or "").rstrip("/\\")) if db_path else ""
        _is_existing = existing is not None
        notify_lines = [
            "📝 已存在案件資料更新" if _is_existing else "📥 新法扶派案已建立",
            f"當事人: {client_name}",
            f"案號: {laf_number}",
            f"類型: {case_type}",
            f"案由: {case_reason}",
        ]
        if folder_label:
            notify_lines.append(f"資料夾: {folder_label}")
        if download_result.get("downloaded_count"):
            dl_archive = download_result.get("archive", {})
            dl_new_files = dl_archive.get("new_files") or []
            dl_skipped_files = dl_archive.get("skipped_existing") or []
            dl_new_count = len(dl_new_files)
            dl_skipped_count = len(dl_skipped_files)
            notify_lines.append(f"官網附件: 新增 {dl_new_count} 份")
            if dl_skipped_count:
                notify_lines.append(f"官網附件去重: 略過 {dl_skipped_count} 份")
            for fn in dl_new_files[:10]:
                notify_lines.append(f"  ✓ {os.path.basename(fn)}")
            for fn in dl_skipped_files[:5]:
                notify_lines.append(f"  ⏭️ {os.path.basename(fn)}")
        elif download_result.get("retry_queued"):
            notify_lines.append("⏳ 官網下載區本輪尚未列出附件，系統會每 5 分鐘自動補查。")
        elif download_result.get("error"):
            notify_lines.append(f"⚠️ 官網附件下載失敗: {download_result['error']}")
        notify_lines.append(f"開辦資料: 開辦通知 {opening_notice_count} 份、委任狀 {poa_count} 份")
        if email_attachment_result.get("downloaded_count"):
            notify_lines.append(f"專員來信附件: 新增 {int(email_attachment_result.get('new_count') or 0)} 份")
            skipped_email = int(email_attachment_result.get("skipped_existing_count") or 0)
            if skipped_email:
                notify_lines.append(f"專員來信附件去重: 略過 {skipped_email} 份")
        if extracted_date:
            notify_lines.append(f"📅 開辦日期: {extracted_date}")
        if submission_info.get("date_iso"):
            notify_lines.append(f"📅 委任狀遞出日期: {submission_info['date_iso']} ({submission_info.get('source', '?')})")
        elif poa_submit_date:
            notify_lines.append(f"📅 委任狀遞出日期: {poa_submit_date}")
        if go_live_remark:
            notify_lines.append(f"📝 說明欄: {go_live_remark}")
        if go_live_reminder:
            notify_lines.append(go_live_reminder)
        notify_msg = "\n".join(notify_lines)
        # Send opening notice document image for user visual confirmation
        confirm_files = []
        if open_doc and os.path.isfile(open_doc):
            confirm_files.append(open_doc)
        if poa_doc and os.path.isfile(poa_doc):
            confirm_files.append(poa_doc)
        if confirm_files:
            try:
                self.notifier.notify_admin_with_files(notify_msg, confirm_files, topic_key="laf_go_live")
            except Exception as nf_e:
                logger.warning("Failed to send go_live confirmation files: %s", nf_e)
                self.notifier.notify_admin(notify_msg, topic_key="laf_go_live")
        else:
            self.notifier.notify_admin(notify_msg, topic_key="laf_go_live")

        self._log_event(laf_number, "go_live", {
            "client_name": client_name,
            "case_type": case_type,
            "case_reason": case_reason,
            "is_duplicate": existing is not None,
            "vision_date": extracted_date,
            "poa_submit_date": submission_info.get("date_iso") or poa_submit_date,
            "submission_source": submission_info.get("source", ""),
            "go_live_draft_ok": go_live_draft_ok,
        }, "success")
        _eventlog(
            "laf:go_live:done",
            ok=True,
            payload={
                "case_type": case_type, "case_reason": case_reason,
                "is_duplicate": existing is not None,
                "vision_date": extracted_date,
                "go_live_draft_ok": go_live_draft_ok,
                "submission_confidence": submission_info.get("confidence", ""),
            },
            tags={"laf_case_no": laf_number, "client_name": client_name},
        )

    def _archive_case_email_snapshot(self, case_info, case_folder: str) -> str:
        """Persist a text snapshot of the original LAF email into 01_法扶資料/專員來信."""
        root = str(case_folder or "").strip()
        if not root:
            return ""
        target_dir = Path(root) / "01_法扶資料" / "專員來信"
        target_dir.mkdir(parents=True, exist_ok=True)

        message_id = str(getattr(case_info, "message_id", "") or "").strip()
        subject = str(getattr(case_info, "subject", "") or "").strip()
        sender = str(getattr(case_info, "sender", "") or "").strip()
        laf_number = str(getattr(case_info, "laf_case_number", "") or "").strip()
        client_name = str(getattr(case_info, "client_name", "") or "").strip()
        notification_type = str(getattr(case_info, "notification_type", "") or "").strip()
        received_at = getattr(case_info, "received_at", None)
        body = str(getattr(case_info, "body", "") or "").strip()
        attachments = list(getattr(case_info, "attachments", []) or [])
        staff_name = str(getattr(case_info, "staff_name", "") or "").strip()
        staff_phone = str(getattr(case_info, "staff_phone", "") or "").strip()
        staff_email = str(getattr(case_info, "staff_email", "") or "").strip()

        if isinstance(received_at, datetime):
            ts = received_at.strftime("%Y%m%d_%H%M%S")
            received_label = received_at.isoformat()
        else:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            received_label = ""

        safe_type = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", notification_type or "法扶來信").strip("_") or "法扶來信"
        safe_msg = re.sub(r"[^A-Za-z0-9_-]+", "", message_id[-10:]) if message_id else ""
        filename = f"{ts}_{safe_type}"
        if laf_number:
            filename += f"_{laf_number}"
        if safe_msg:
            filename += f"_{safe_msg}"
        file_path = target_dir / f"{filename}.txt"

        lines = [
            f"主旨: {subject}",
            f"寄件者: {sender}",
            f"接收時間: {received_label}",
            f"通知類型: {notification_type}",
            f"當事人: {client_name}",
            f"法扶案號: {laf_number}",
        ]
        if staff_name or staff_phone or staff_email:
            lines.append(f"承辦資訊: {staff_name} / {staff_phone} / {staff_email}")
        if attachments:
            lines.append("附件清單:")
            for att in attachments:
                lines.append(f"- {att.get('filename', '')} ({att.get('mimeType', '')})")
        else:
            lines.append("附件清單: 無")
        lines.append("")
        lines.append("內文:")
        lines.append(body or "（原始信件內文未保留）")
        content = "\n".join(lines).strip() + "\n"

        if file_path.exists():
            try:
                old = file_path.read_text(encoding="utf-8")
                if old == content:
                    return str(file_path)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1327, exc_info=True)
        file_path.write_text(content, encoding="utf-8")
        return str(file_path)

    def _download_case_email_attachments(self, case_info, case_folder: str) -> dict:
        """Download and archive actual LAF email attachments into 01_法扶資料/專員來信."""
        result = {
            "ok": False,
            "downloaded_count": 0,
            "new_count": 0,
            "skipped_existing_count": 0,
            "archived_files": [],
            "error": "",
        }
        root = str(case_folder or "").strip()
        message_id = str(getattr(case_info, "message_id", "") or "").strip()
        if not root or not message_id:
            result["error"] = "missing_case_folder_or_message_id"
            return result

        monitor = getattr(self, "_gmail_monitor", None)
        if monitor is None:
            result["error"] = "gmail_monitor_unavailable"
            return result

        try:
            if not getattr(monitor, "service", None):
                if not monitor.authenticate():
                    result["error"] = "gmail_auth_failed"
                    return result
        except Exception as e:
            result["error"] = f"gmail_auth_failed:{e}"
            return result

        try:
            from laf import OSCCaseCreator  # type: ignore
        except Exception as e:
            result["error"] = f"import_OSCCaseCreator_failed:{e}"
            return result

        _eventlog(
            "laf:email:attachments:start",
            ok=None,
            payload={"case_folder": os.path.basename(root.rstrip("/\\"))},
            tags={
                "laf_case_no": str(getattr(case_info, "laf_case_number", "") or ""),
                "client_name": str(getattr(case_info, "client_name", "") or ""),
            },
        )

        try:
            with tempfile.TemporaryDirectory(prefix="laf_email_att_") as tmpdir:
                downloaded = monitor.download_attachments_by_msg_id(message_id, tmpdir) or []
                result["downloaded_count"] = len(downloaded)
                if not downloaded:
                    result["ok"] = True
                    _eventlog(
                        "laf:email:attachments:done",
                        ok=True,
                        payload={"downloaded_count": 0, "new_count": 0, "skipped_existing_count": 0},
                        tags={
                            "laf_case_no": str(getattr(case_info, "laf_case_number", "") or ""),
                            "client_name": str(getattr(case_info, "client_name", "") or ""),
                        },
                    )
                    return result

                archiver = OSCCaseCreator(
                    db_manager=self.db,
                    target_folder=self.laf_config.get("target_folder", ""),
                    log_callback=lambda msg: logger.info("[LAF-EMAIL-ATT] %s", msg),
                )
                archived = archiver.archive_staff_email_attachments(downloaded, root) or []
                result["archived_files"] = [str(p) for p in archived]
                result["new_count"] = len(archived)
                result["skipped_existing_count"] = max(0, len(downloaded) - len(archived))
                result["ok"] = True
        except Exception as e:
            result["error"] = str(e)

        _eventlog(
            "laf:email:attachments:done",
            ok=bool(result.get("ok")),
            payload={
                "downloaded_count": int(result.get("downloaded_count") or 0),
                "new_count": int(result.get("new_count") or 0),
                "skipped_existing_count": int(result.get("skipped_existing_count") or 0),
                "error": str(result.get("error") or "")[:300],
            },
            tags={
                "laf_case_no": str(getattr(case_info, "laf_case_number", "") or ""),
                "client_name": str(getattr(case_info, "client_name", "") or ""),
            },
        )
        return result

    def handle_withdrawal(self, case_info, reason: str = ""):
        """
        撤回 — 由律師主動告知 CASPER 處理。
        CASPER 根據律師提供的原因自動判斷選項。
        """
        if isinstance(case_info, dict):
            client_name = case_info.get('client_name', '')
            laf_number = case_info.get('laf_case_number', '')
        else:
            client_name = getattr(case_info, 'client_name', '')
            laf_number = getattr(case_info, 'laf_case_number', '')

        # Auto-select withdrawal reason by keyword
        pb_reason = self._match_withdrawal_reason(reason)

        logger.info("⛔ Withdrawal: %s (%s) reason=%s → %s",
                     client_name, laf_number, reason, pb_reason)

        self._log_event(laf_number, "withdrawal", {
            "client_name": client_name,
            "user_reason": reason,
            "auto_selected": pb_reason,
        }, "ready")
        _eventlog("laf:withdrawal:ready", ok=True, payload={"auto_selected": pb_reason}, tags={"laf_case_no": laf_number, "client_name": client_name})

        result = {"pb_reason": pb_reason, "reason_text": reason}
        if (not self.dry_run) and self.auto_portal_draft and laf_number:
            # 撤回案件幾乎確定在辦理中，需填報結資料（辦理情形）
            _case_number = ""
            if isinstance(case_info, dict):
                _case_number = case_info.get("case_number", "")
            else:
                _case_number = getattr(case_info, "case_number", "")

            _closing_counts = None
            if _case_number:
                try:
                    _closing_counts = self._gather_case_counts(_case_number, client_name)
                    logger.info("⛔ Withdrawal closing counts for %s: %s", laf_number, _closing_counts)
                except Exception as e:
                    logger.warning("⚠️ Withdrawal: 無法取得辦理情形資料: %s", e)

            _fields = {
                "pb_reason": pb_reason,
                "reason_text": reason or "依律師指示暫存撤回資料",
                "pb_lawyer_status": "P",  # 辦理中
                "lawy_status": "P",
            }
            if _closing_counts:
                _fields["closing_counts"] = _closing_counts

            draft_result = self.execute_portal_action_draft(
                action="withdrawal",
                laf_case_number=laf_number,
                client_name=client_name,
                reason=reason or "依律師指示暫存撤回資料",
                fields=_fields,
            )
            result["portal_draft_saved"] = bool(draft_result.get("ok"))
            if _closing_counts:
                result["closing_summary_included"] = True
            if not draft_result.get("ok"):
                result["portal_draft_error"] = draft_result.get("error") or "withdrawal_draft_failed"
                if draft_result.get("missing"):
                    result["missing"] = list(draft_result.get("missing") or [])
        return result

    def handle_inquiry(self, case_info, reason: str = ""):
        """
        疑義 — 由律師主動告知 CASPER 處理。
        CASPER 根據律師提供的原因自動判斷主旨選項。
        """
        if isinstance(case_info, dict):
            client_name = case_info.get('client_name', '')
            laf_number = case_info.get('laf_case_number', '')
        else:
            client_name = getattr(case_info, 'client_name', '')
            laf_number = getattr(case_info, 'laf_case_number', '')

        # Auto-select inquiry reason by keyword
        rsm_reqsubj2 = self._match_inquiry_reason(reason)

        logger.info("❓ Inquiry: %s (%s) reason=%s → %s",
                     client_name, laf_number, reason, rsm_reqsubj2)

        self._log_event(laf_number, "inquiry", {
            "client_name": client_name,
            "user_reason": reason,
            "rsm_reqsubj1": "0001",
            "rsm_reqsubj2": rsm_reqsubj2,
        }, "ready")
        _eventlog("laf:inquiry:ready", ok=True, payload={"rsm_reqsubj1": "0001", "rsm_reqsubj2": rsm_reqsubj2}, tags={"laf_case_no": laf_number, "client_name": client_name})

        result = {"rsm_reqsubj1": "0001", "rsm_reqsubj2": rsm_reqsubj2, "desc": reason}
        if (not self.dry_run) and self.auto_portal_draft and laf_number:
            # 疑義案件幾乎確定在辦理中，需填報結資料（辦理情形）
            _case_number = ""
            if isinstance(case_info, dict):
                _case_number = case_info.get("case_number", "")
            else:
                _case_number = getattr(case_info, "case_number", "")

            _closing_counts = None
            if _case_number:
                try:
                    _closing_counts = self._gather_case_counts(_case_number, client_name)
                    logger.info("❓ Inquiry closing counts for %s: %s", laf_number, _closing_counts)
                except Exception as e:
                    logger.warning("⚠️ Inquiry: 無法取得辦理情形資料: %s", e)

            _fields = {
                "rsm_reqsubj1": "0001",
                "rsm_reqsubj2": rsm_reqsubj2,
                "desc": reason or "依律師指示暫存疑義資料",
                "rsm_lawyer_status": "P",  # 辦理中
                "lawy_status": "P",
            }
            if _closing_counts:
                _fields["closing_counts"] = _closing_counts

            ok = self.execute_portal_inquiry_draft(
                case_number=laf_number,
                client_name=client_name,
                fields=_fields,
            )
            result["portal_draft_saved"] = bool(ok)
            if _closing_counts:
                result["closing_summary_included"] = True
        return result

    def handle_fee_payment(self, case_info, reason: str = ""):
        """
        費用支付 — 由律師主動告知 CASPER 處理。
        CASPER 根據律師提供的說明自動判斷主旨選項。
        """
        if isinstance(case_info, dict):
            client_name = case_info.get('client_name', '')
            laf_number = case_info.get('laf_case_number', '')
        else:
            client_name = getattr(case_info, 'client_name', '')
            laf_number = getattr(case_info, 'laf_case_number', '')

        # Auto-select fee type by keyword
        subj1, subj2 = self._match_fee_type(reason)

        logger.info("💰 Fee: %s (%s) reason=%s → %s/%s",
                     client_name, laf_number, reason, subj1, subj2)

        self._log_event(laf_number, "fee_payment", {
            "client_name": client_name,
            "user_reason": reason,
            "lgfee_reqsubj1": subj1,
            "lgfee_reqsubj2": subj2,
        }, "ready")
        _eventlog("laf:fee_payment:ready", ok=True, payload={"lgfee_reqsubj1": subj1, "lgfee_reqsubj2": subj2}, tags={"laf_case_no": laf_number, "client_name": client_name})

        result = {"lgfee_reqsubj1": subj1, "lgfee_reqsubj2": subj2, "desc": reason}
        if (not self.dry_run) and self.auto_portal_draft and laf_number:
            ok = self.execute_portal_fee_draft(
                case_number=laf_number,
                client_name=client_name,
                fields={
                    "lgfee_reqsubj1": subj1,
                    "lgfee_reqsubj2": subj2,
                    "desc": reason or "依律師指示暫存費用支付資料",
                    "lgfee_lawyer_status": "N",
                },
            )
            result["portal_draft_saved"] = bool(ok)
        return result

    # --- Keyword → Dropdown Mapping Helpers ---

    @staticmethod
    def _match_withdrawal_reason(reason: str) -> str:
        """Map user's withdrawal reason to portal dropdown value."""
        r = reason.lower()
        if any(k in r for k in ('自行委任', '自己請律師', '另聘')):
            return '自行委任律師'
        if any(k in r for k in ('不配合', '不處理', '不願')):
            return '不願配合辦理'
        if any(k in r for k in ('撤回', '撤')):
            return '申請人撤回申請'
        return '其他'

    @staticmethod
    def _match_inquiry_reason(reason: str) -> str:
        """Map user's inquiry reason to portal reqsubj2 value.

        Portal actual option values (confirmed 20260326):
          0007: 資力不合標準
          0008: 案件顯無理由或其他不應扶助者
          0009: 有終止事由
          0010: 本案管轄有問題
          0117: 其他
        """
        r = reason.lower()
        if any(k in r for k in ('資力', '經濟', '收入')):
            return '0007'  # 資力不合標準
        if any(k in r for k in ('顯無理由', '不可能', '無理由')):
            return '0008'  # 案件顯無理由或其他不應扶助者
        if any(k in r for k in ('終止', '撤止', '中止')):
            return '0009'  # 有終止事由
        if any(k in r for k in ('管轄', '移轉管轄', '移送')):
            return '0010'  # 本案管轄有問題
        return '0117'  # 其他

    @staticmethod
    def _match_fee_type(reason: str) -> tuple:
        """Map user's fee description to portal lgfee_reqsubj1/2 values.

        Portal actual option values (confirmed 20260326):
          reqsubj1: 0116 (訴訟費用及必要費用之處理)
          reqsubj2: 0120 (支付裁判費) | 0121 (支付裁判費以外之費用)
          reqsubj3 (when reqsubj2=0120): 0132|0133|0134|0135|0136
        """
        r = reason.lower()
        subj1 = '0116'  # 訴訟費用及必要費用之處理 (default primary)
        if any(k in r for k in ('裁判費',)):
            return subj1, '0120'  # 支付裁判費
        if any(k in r for k in ('鑑定', '新鑑', '必要費用', '其他費用')):
            return subj1, '0121'  # 支付裁判費以外之費用
        return subj1, '0121'  # 預設歸類為裁判費以外之費用

    # ==================================================================
    # Closing Report Flow (Admin-in-the-Loop)
    # ==================================================================

    def prepare_closing_report(self, case_data):
        """
        Gather counts from DB and send LINE/DC for admin confirmation.

        Args:
            case_data: tuple (case_number, client_name, folder_path, ...) or dict
        """
        if isinstance(case_data, (list, tuple)):
            case_number = case_data[0]
            client_name = case_data[1]
            folder_path = case_data[2] if len(case_data) > 2 else ""
        else:
            case_number = case_data.get("case_number", "")
            client_name = case_data.get("client_name", "")
            folder_path = case_data.get("folder_path", "")

        logger.info("📊 Preparing closing report: %s (%s)", client_name, case_number)
        _eventlog("laf:closing:prepare:start", ok=None, payload={"case_number": case_number}, tags={"case_number": case_number, "client_name": client_name})

        # 判斷案件類型：看資料夾路徑
        _folder_str = str(folder_path or "").replace("\\", "/")
        if not _folder_str and self.db and case_number:
            try:
                _r = self.db.fetch_one(
                    "SELECT folder_path FROM cases WHERE case_number = %s", (case_number,), as_dict=True
                )
                if _r:
                    _folder_str = str(_r.get("folder_path") or "").replace("\\", "/")
            except Exception:
                pass
        _is_criminal_case = "/刑事/" in _folder_str
        # 資料夾名稱含「-偵查-」才是偵查階段（如 2026-0002-[當事人S]-偵查-過失致死）
        _is_investigation = "-偵查-" in _folder_str

        docs = self._scan_case_folder_docs(folder_path) if folder_path else self._empty_docs_map()
        if folder_path and not docs.get("closing_basis_files"):
            if _is_investigation:
                # 偵查案件：結案依據可能是不起訴處分書、偵結報告等，不強制要求
                logger.info("  ℹ️ 偵查案件 %s 無結案基礎文件，允許繼續", case_number)
            else:
                logger.warning("  ⚠️ 無法產生結案報告：%s 缺少結案基礎文件 (%s)", case_number, folder_path)
                self.notifier.notify_admin(
                    f"⚠️ 無法產生結案報告：\n案號：{case_number}\n當事人：{client_name}\n"
                    "原因：`10_判決書` 資料夾中找不到「起訴書/判決/裁定/不起訴處分書」檔案，請補齊後再試。"
                )
                return
        if folder_path and not docs.get("office_receipt_files"):
            if _is_investigation:
                # 偵查案件：文件由檢察署寄送，不一定有法院收文章
                logger.info("  ℹ️ 偵查案件 %s 無收文章證據，允許繼續", case_number)
            else:
                logger.warning("  ⚠️ 無法產生結案報告：%s 缺少收文章證據 (%s)", case_number, folder_path)
                self.notifier.notify_admin(
                    f"⚠️ 無法產生結案報告：\n案號：{case_number}\n當事人：{client_name}\n"
                    "原因：未偵測到法院收文章章戳或郵局回執，請補齊後再試。"
                )
                return

        # Gather counts from DB
        counts = self._gather_case_counts(case_number, client_name)

        # Check for warnings — 欄位與 Portal 一致
        # Portal: 討論次數(disc) = 面談(meet) + 電話(tel) + 律見(inq)
        warnings = []
        _disc_total = (int(counts.get("meeting_count", 0) or 0)
                       + int(counts.get("contact_count", 0) or 0)
                       + int(counts.get("inq_count", 0) or 0))
        _court = int(counts.get("court_count", 0) or 0)
        _review = int(counts.get("review_count", 0) or 0)
        _wc = int(counts.get("document_count", 0) or 0)

        if _disc_total < 1:
            warnings.append("討論次數（面談+電話+律見）為 0，日曆上可能未登記")
        if _court < 1:
            warnings.append("開庭次數為 0，日曆上可能未登記")
        if _review < 1 and not _is_investigation:
            warnings.append("閱卷次數為 0，日曆上可能未登記")
        if _wc < 1 and not _is_investigation:
            warnings.append("書狀次數為 0")

        _needs_noarrive = (_disc_total < 1 or _court < 1
                           or (not _is_investigation and (_review < 1 or _wc < 1)))
        if _needs_noarrive:
            warnings.append("有零次數欄位，法扶報結頁需填寫「扶助律師特別說明」，請回覆說明文字")

        # Send confirmation request
        if self.dry_run:
            logger.info("  [DRY RUN] Would send confirmation for %s", case_number)
            logger.info("  Counts: %s", counts)
            logger.info("  Warnings: %s", warnings)
            return

        self.notifier.send_closing_confirmation(
            case_name=client_name,
            case_number=case_number,
            counts=counts,
            warnings=warnings,
        )

        # Log the event
        self._log_event(case_number, "closing", {
            "counts": counts,
            "warnings": warnings,
            "status": "awaiting_admin_confirmation",
        }, "pending")
        _eventlog("laf:closing:prepare:done", ok=True, payload={"warnings_count": len(warnings)}, tags={"case_number": case_number, "client_name": client_name})

        logger.info("  ✅ Confirmation sent, awaiting admin reply")

    def on_admin_response(self, case_number: str, response: str,
                          pending_zero_fields: list = None) -> dict:
        """
        Parse admin's LINE/DC response and decide next action.

        Args:
            case_number: The case being confirmed
            response: Admin's text reply
            pending_zero_fields: list of field names with count 0 awaiting reason

        Returns:
            dict with action, updated_counts, and zero_reasons
        """
        response = response.strip()

        # Admin confirms → check if there are zero fields needing reasons
        if response.lower() in ("ok", "請報結", "報結", "確認", "proceed", "沒錯"):
            if pending_zero_fields:
                # Confirmed counts are correct but need reasons for zero fields
                logger.info("✅ Admin confirmed zero counts, asking for reasons")
                return {
                    "action": "ask_zero_reasons",
                    "zero_fields": pending_zero_fields,
                    "updated_counts": {},
                    "zero_reasons": {},
                }
            logger.info("✅ Admin confirmed closing for %s", case_number)
            return {"action": "save_draft", "updated_counts": {}, "zero_reasons": {}}

        # Admin tells CASPER to submit the draft (guarded by draft-only policy)
        if response.lower() in ("送出", "submit", "確認送出"):
            if os.environ.get("MAGI_LAF_DRAFT_ONLY", "1") == "1":
                logger.info("🔒 Draft-only policy blocks final submit for %s", case_number)
                return {"action": "blocked_draft_only", "updated_counts": {}, "zero_reasons": {}}
            logger.info("📤 Admin authorized final submit for %s", case_number)
            return {"action": "final_submit", "updated_counts": {}, "zero_reasons": {}}

        # Admin provides corrections, e.g. "聯繫 2" or "開會 3 聯繫 2"
        import re
        corrections = {}
        patterns = [
            (r'開會\s*(\d+)', 'meeting_count'),
            (r'聯繫\s*(\d+)', 'contact_count'),
            (r'開庭\s*(\d+)', 'court_count'),
            (r'書狀\s*(\d+)', 'document_count'),
            (r'閱卷\s*(\d+)', 'review_count'),
        ]
        for pattern, key in patterns:
            match = re.search(pattern, response)
            if match:
                corrections[key] = int(match.group(1))

        if corrections:
            logger.info("📝 Admin corrections for %s: %s", case_number, corrections)
            return {"action": "update_and_reconfirm", "updated_counts": corrections,
                    "zero_reasons": {}}

        # Admin provides a reason text for zero-count fields
        # e.g. "未閱卷" or "本案無閱卷必要"
        if pending_zero_fields:
            # Treat the response as the reason for the pending zero fields
            reasons = {field: response for field in pending_zero_fields}
            logger.info("📝 Zero-count reasons for %s: %s", case_number, reasons)
            return {"action": "save_draft", "updated_counts": {}, "zero_reasons": reasons}

        # Pause/manual
        if any(kw in response for kw in ("暫停", "手動", "不要", "取消")):
            logger.info("⏸️ Admin paused closing for %s", case_number)
            return {"action": "pause", "updated_counts": {}, "zero_reasons": {}}

        # Unknown — ask again
        logger.warning("❓ Unknown response for %s: %s", case_number, response)
        return {"action": "unknown", "updated_counts": {}, "zero_reasons": {}}

    def execute_portal_closing(
        self,
        case_number: str,
        confirmed_counts: dict,
        zero_reasons: dict = None,
        upload_files: Optional[List[str]] = None,
        client_name: str = "",
    ):
        """
        Execute portal closing: fill form and SAVE DRAFT (暫存).
        
        IMPORTANT: Always uses doSave (暫存), NEVER doFinalSave (送出).
        Admin must confirm the draft before final submission.

        Args:
            case_number: LAF case number
            confirmed_counts: dict of confirmed counts
            zero_reasons: dict of {field: reason_text} for zero-count fields
        """
        resolved_client_name = (client_name or "").strip()
        if not resolved_client_name:
            try:
                ident = self._lookup_case_identity(
                    laf_case_number=case_number,
                    case_number=case_number,
                    client_name="",
                )
                resolved_client_name = str(ident.get("client_name") or "").strip()
            except Exception:
                resolved_client_name = ""

        resolved_uploads = list(upload_files or [])
        if not resolved_uploads:
            try:
                # 先用 LAF 案號查；若找不到資料夾，用 DB 查內部案號再找
                ident = self._lookup_case_identity(
                    laf_case_number=case_number,
                    case_number=case_number,
                    client_name=resolved_client_name,
                )
                folder = (ident.get("case_folder") or "").strip()
                if not folder:
                    # LAF 案號與內部案號不同，嘗試用 DB 查出內部案號
                    try:
                        db_row = self._query_db(
                            "SELECT case_number, client_name FROM cases "
                            "WHERE laf_case_number = %s LIMIT 1",
                            (case_number,),
                        )
                        if db_row:
                            internal_no = str(db_row[0].get("case_number") or "").strip()
                            db_client = str(db_row[0].get("client_name") or "").strip()
                            if internal_no and internal_no != case_number:
                                ident = self._lookup_case_identity(
                                    laf_case_number=case_number,
                                    case_number=internal_no,
                                    client_name=db_client or resolved_client_name,
                                )
                                folder = (ident.get("case_folder") or "").strip()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1875, exc_info=True)
                laf_no = (ident.get("laf_case_number") or case_number or "").strip()
                if folder:
                    b = self._collect_progress_upload_pdfs(folder, laf_case_no=laf_no, action="closing")
                    resolved_uploads = list(b.get("pdf_files") or [])
                    logger.info("  📎 Auto-collected %d PDF(s) for upload", len(resolved_uploads))
            except Exception:
                resolved_uploads = list(upload_files or [])

        if zero_reasons:
            _reason_texts = list(dict.fromkeys(
                v.strip() for v in zero_reasons.values() if v and str(v).strip()
            ))
            if _reason_texts:
                confirmed_counts["noarrivereason"] = "；".join(_reason_texts)

        display_target = f"{resolved_client_name}（{case_number}）" if resolved_client_name else case_number
        logger.info("🌐 Executing portal closing (DRAFT) for %s", display_target)
        logger.info("  Confirmed counts: %s", confirmed_counts)
        if zero_reasons:
            logger.info("  Zero-count reasons: %s", zero_reasons)
        if resolved_uploads:
            logger.info("  Upload files prepared: %d", len(resolved_uploads))

        if self.dry_run:
            logger.info("  [DRY RUN] Would save draft on portal")
            return True

        # Execute portal automation (save draft only).
        try:
            from laf_automation_v2 import LAFWebAutomation, _export_file_to_static

            username = os.environ.get("MAGI_LAF_USERNAME") or self.laf_config.get("username", "")
            password = os.environ.get("MAGI_LAF_PASSWORD") or self.laf_config.get("password", "")
            download_folder = self.laf_config.get("download_folder", "./laf_downloads")
            headless = bool(self.laf_config.get("headless", True))
            base_url = (self.laf_config.get("base_url", "") or "").strip()
            browser_profile_dir = self.laf_config.get("browser_profile_dir", "")

            if not username or not password:
                raise RuntimeError("LAF credentials not configured (laf.username / laf.password)")

            automation = LAFWebAutomation(
                username=username,
                password=password,
                download_folder=download_folder,
                headless=headless,
                log_callback=lambda msg: logger.info("[LAF] %s", msg),
                base_url=base_url,
                mock_mode=bool(base_url and ("127.0.0.1" in base_url or "localhost" in base_url)),
                browser_profile_dir=browser_profile_dir,
            )
            try:
                if not automation.login():
                    raise RuntimeError("LAF login failed")

                ok = automation.save_closing_report_draft(
                    laf_case_number=case_number,
                    counts=confirmed_counts or {},
                    zero_reasons=zero_reasons or {},
                    upload_files=resolved_uploads,
                )
                if not ok:
                    raise RuntimeError("portal draft save failed")
                raw_art = getattr(automation, "last_debug_artifact", {}) or {}
                upload_res = getattr(automation, "last_upload_result", {}) or {}
                art = {}
                if isinstance(raw_art, dict) and raw_art:
                    art = dict(raw_art)
                    png = str(art.get("png") or "").strip()
                    html = str(art.get("html") or "").strip()
                    if png:
                        try:
                            ex = _export_file_to_static(Path(png), prefix="laf_closing_preview")
                            if isinstance(ex, dict):
                                art["png_export"] = ex
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1952, exc_info=True)
                    if html:
                        try:
                            ex2 = _export_file_to_static(Path(html), prefix="laf_closing_html")
                            if isinstance(ex2, dict):
                                art["html_export"] = ex2
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1959, exc_info=True)
                if upload_res:
                    art["upload_result"] = upload_res
                self._last_portal_artifact = art
            finally:
                automation.close()
        except Exception as e:
            # Do not silently pass. Report to admin and mark an error event.
            try:
                self.notifier.notify_admin(f"❌ 報結暫存失敗 — {case_number}\n原因：{e}", topic_key="laf_closing")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1970, exc_info=True)
            self._log_event(case_number, "closing", {"error": str(e)}, "error")
            return False
        self._log_event(case_number, "closing", {
            "confirmed_counts": confirmed_counts,
            "zero_reasons": zero_reasons or {},
            "upload_files_count": len(resolved_uploads or []),
            "portal_status": "draft_saved",
        }, "draft")

        # Avoid sending raw JSON to LINE/DC; use human-friendly lines.
        _cc = confirmed_counts or {}
        lines = [f"✅ 報結資料已暫存 — {case_number}"]

        # 統計摘要
        _stats = []
        for _key, _label in [
            ("meeting_count", "開會"), ("contact_count", "聯繫"),
            ("inq_count", "律見"), ("court_count", "開庭"),
            ("review_count", "閱卷"), ("document_count", "書狀"),
        ]:
            if _key in _cc:
                _stats.append(f"{_label} {int(_cc[_key] or 0)}")
        if _stats:
            lines.append(f"統計：{'／'.join(_stats)}")

        # 案件資訊
        _court_name = str(_cc.get("court_name") or "").strip()
        _case_year = str(_cc.get("court_case_year") or "").strip()
        _case_code = str(_cc.get("court_case_code") or "").strip()
        _case_no = str(_cc.get("court_case_no") or "").strip()
        if _court_name and _case_year:
            lines.append(f"案號：{_court_name}{_case_year}年度{_case_code}字第{_case_no}號")
        _result = str(_cc.get("closing_result") or "").strip()
        if _result:
            lines.append(f"結果：{_result[:80]}")
        _doc_type = str(_cc.get("closing_doc_type") or "").strip()
        _judg_eff = str(_cc.get("judg_eff") or "").strip()
        if _doc_type or _judg_eff:
            lines.append(f"裁判：{_doc_type}{'，' + _judg_eff if _judg_eff else ''}")

        # 零值警告
        _zero_labels = []
        for _key, _label in [("meeting_count", "開會"), ("contact_count", "聯繫"),
                              ("court_count", "開庭"), ("review_count", "閱卷"), ("document_count", "書狀")]:
            if int(_cc.get(_key, 0) or 0) == 0:
                _zero_labels.append(_label)
        if _zero_labels:
            lines.append(f"⚠️ 以下為 0：{'、'.join(_zero_labels)}，請確認「扶助律師特別說明」")

        if resolved_uploads:
            lines.append(f"上傳：{len(resolved_uploads or [])} 份（書狀／判決書）")
        if zero_reasons:
            _label_map = {"disc_times": "討論次數", "review_count": "閱卷", "court_count": "開庭", "document_count": "書狀"}
            lines.append("理由：")
            for k, v in zero_reasons.items():
                lines.append(f"- {_label_map.get(k, k)}：{v}")
        if os.environ.get("MAGI_LAF_DRAFT_ONLY", "1") == "1":
            lines.append("🔒 安全政策：目前僅暫存，不會代為送出。")
        else:
            lines.append("可回覆「送出」由 CASPER 代為送出（請先確認平台畫面）。")
        self.notifier.notify_admin("\n".join(lines), topic_key="laf_closing")
        return True

    def execute_portal_workflow_draft(
        self,
        workflow: str,
        case_number: str,
        client_name: str = "",
        fields: Optional[dict] = None,
    ) -> bool:
        """
        通用法扶 workflow 暫存（只暫存不送出）。
        workflow: go_live | condition | inquiry | withdrawal | fee
        """
        wf = (workflow or "").strip()
        if not wf:
            return False
        if not case_number and not client_name:
            logger.warning("Portal %s draft skipped: missing case_number/client_name", wf)
            return False
        if self.dry_run:
            logger.info("  [DRY RUN] Would save %s draft for %s/%s", wf, case_number, client_name)
            return True

        logger.info("🌐 Executing portal %s draft for %s (%s)", wf, client_name or "-", case_number or "-")
        self._last_portal_artifact = {}
        try:
            from laf_automation_v2 import LAFWebAutomation, _export_file_to_static

            username = os.environ.get("MAGI_LAF_USERNAME") or self.laf_config.get("username", "")
            password = os.environ.get("MAGI_LAF_PASSWORD") or self.laf_config.get("password", "")
            download_folder = self.laf_config.get("download_folder", "./laf_downloads")
            headless = bool(self.laf_config.get("headless", True))
            base_url = (self.laf_config.get("base_url", "") or "").strip()
            browser_profile_dir = self.laf_config.get("browser_profile_dir", "")

            if not username or not password:
                raise RuntimeError("LAF credentials not configured (laf.username / laf.password)")

            automation = self._get_automation()
            if not automation.login():
                 raise RuntimeError("LAF login failed")

            ok = automation.save_workflow_draft(
                workflow=wf,
                laf_case_number=case_number or "",
                client_name=client_name or "",
                fields=fields or {},
            )
            if not ok:
                raise RuntimeError(f"portal {wf} draft save failed")
            raw_art = getattr(automation, "last_debug_artifact", {}) or {}
            upload_res = getattr(automation, "last_upload_result", {}) or {}
            if isinstance(raw_art, dict) and raw_art:
                art = dict(raw_art)
                png = str(art.get("png") or "").strip()
                html = str(art.get("html") or "").strip()
                if png:
                    try:
                        ex = _export_file_to_static(Path(png), prefix=f"laf_{wf}_preview")
                        if isinstance(ex, dict):
                            art["png_export"] = ex
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2093, exc_info=True)
                if html:
                    try:
                        ex2 = _export_file_to_static(Path(html), prefix=f"laf_{wf}_html")
                        if isinstance(ex2, dict):
                            art["html_export"] = ex2
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2100, exc_info=True)
                if upload_res:
                    art["upload_result"] = upload_res
                self._last_portal_artifact = art
            elif upload_res:
                self._last_portal_artifact = {"upload_result": upload_res}
        except Exception as e:
            try:
                self.notifier.notify_admin(f"❌ {wf} 暫存失敗 — {case_number or client_name}\n原因：{e}")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2110, exc_info=True)
            self._log_event(case_number or client_name, wf, {"error": str(e), "fields": fields or {}}, "error")
            return False

        self._log_event(
            case_number or client_name,
            wf,
            {"portal_status": "draft_saved", "fields": fields or {}, "artifact": self._last_portal_artifact},
            "draft",
        )
        return True

    def execute_portal_workflow_submit(
        self,
        workflow: str,
        case_number: str,
        client_name: str = "",
        fields: Optional[dict] = None,
    ) -> bool:
        """
        通用送出（目前只允許 go_live，需由上層確認後啟用）。
        """
        wf = (workflow or "").strip()
        if wf != "go_live":
            logger.warning("Portal submit blocked: only go_live is allowed now (%s)", wf)
            return False
        if not case_number and not client_name:
            logger.warning("Portal submit skipped: missing case_number/client_name")
            return False
        if self.dry_run:
            logger.info("  [DRY RUN] Would submit %s for %s/%s", wf, case_number, client_name)
            return True

        logger.info("🌐 Executing portal %s submit for %s (%s)", wf, client_name or "-", case_number or "-")
        self._last_portal_artifact = {}
        try:
            from laf_automation_v2 import LAFWebAutomation, _export_file_to_static

            username = os.environ.get("MAGI_LAF_USERNAME") or self.laf_config.get("username", "")
            password = os.environ.get("MAGI_LAF_PASSWORD") or self.laf_config.get("password", "")
            if not username or not password:
                raise RuntimeError("LAF credentials not configured (laf.username / laf.password)")

            automation = self._get_automation()
            if not automation.login():
                raise RuntimeError("LAF login failed")

            ok = automation.submit_workflow(
                workflow=wf,
                laf_case_number=case_number or "",
                client_name=client_name or "",
                fields=fields or {},
            )
            if not ok:
                raise RuntimeError(f"portal {wf} submit failed")

            raw_art = getattr(automation, "last_debug_artifact", {}) or {}
            if isinstance(raw_art, dict) and raw_art:
                art = dict(raw_art)
                png = str(art.get("png") or "").strip()
                if png:
                    try:
                        ex = _export_file_to_static(Path(png), prefix=f"laf_{wf}_submit")
                        if isinstance(ex, dict):
                            art["png_export"] = ex
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2176, exc_info=True)
                self._last_portal_artifact = art
        except Exception as e:
            try:
                self.notifier.notify_admin(f"❌ {wf} 送出失敗 — {case_number or client_name}\n原因：{e}")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2182, exc_info=True)
            self._log_event(case_number or client_name, wf, {"error": str(e), "fields": fields or {}}, "error")
            return False

        self._log_event(
            case_number or client_name,
            wf,
            {"portal_status": "submitted", "fields": fields or {}, "artifact": self._last_portal_artifact},
            "submitted",
        )
        return True

    def _get_automation(self):
        """Get or create shared LAFWebAutomation instance."""
        from laf_automation_v2 import LAFWebAutomation
        if self._automation:
            # TODO: Add health check or expiry?
            # For now, rely on .login() inside scripts to check cookie validity.
            return self._automation

        username = os.environ.get("MAGI_LAF_USERNAME") or self.laf_config.get("username", "")
        password = os.environ.get("MAGI_LAF_PASSWORD") or self.laf_config.get("password", "")
        download_folder = self.laf_config.get("download_folder", "./laf_downloads")
        headless = bool(self.laf_config.get("headless", True))
        base_url = (self.laf_config.get("base_url", "") or "").strip()
        browser_profile_dir = self.laf_config.get("browser_profile_dir", "")
        
        self._automation = LAFWebAutomation(
            username=username,
            password=password,
            download_folder=download_folder,
            headless=headless,
            log_callback=lambda msg: logger.info("[LAF] %s", msg),
            base_url=base_url,
            mock_mode=False,
            browser_profile_dir=browser_profile_dir,
        )
        return self._automation

    def close(self):
        """Cleanup resources."""
        if self._automation:
            try:
                self._automation.close()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2227, exc_info=True)
            self._automation = None

    def execute_portal_go_live_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None) -> bool:
        ok = self.execute_portal_workflow_draft("go_live", case_number, client_name, fields)
        if ok and (not self.dry_run):
            notify_msg = f"✅ 已填寫開辦資料（未送出）— {client_name or '-'}（{case_number or '-'}）"
            # Send preview screenshot if available
            preview_png = ""
            if isinstance(self._last_portal_artifact, dict):
                preview_png = str(self._last_portal_artifact.get("png") or "").strip()
            if preview_png and os.path.isfile(preview_png):
                try:
                    self.notifier.notify_admin_with_files(notify_msg, [preview_png], topic_key="laf_go_live")
                except Exception:
                    self.notifier.notify_admin(notify_msg, topic_key="laf_go_live")
            else:
                self.notifier.notify_admin(notify_msg, topic_key="laf_go_live")
        return ok

    def execute_portal_go_live_submit(self, case_number: str, client_name: str = "", fields: Optional[dict] = None) -> bool:
        ok = self.execute_portal_workflow_submit("go_live", case_number, client_name, fields)
        if ok and (not self.dry_run):
            self.notifier.notify_admin(f"✅ 已送出開辦回報 — {client_name or '-'}（{case_number or '-'}）", topic_key="laf_go_live")
        return ok

    def execute_portal_withdrawal_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None) -> bool:
        ok = self.execute_portal_workflow_draft("withdrawal", case_number, client_name, fields)
        if ok and (not self.dry_run):
            self.notifier.notify_admin(f"✅ 已暫存撤回資料 — {client_name or '-'}（{case_number or '-'}）")
        return ok

    def execute_portal_inquiry_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None) -> bool:
        ok = self.execute_portal_workflow_draft("inquiry", case_number, client_name, fields)
        if ok and (not self.dry_run):
            reason = (fields or {}).get("desc", "")
            msg = f"✅ 已暫存疑義資料 — {client_name or '-'}（{case_number or '-'}）\n說明：{reason}\n請務必登入平台確認內容是否正確。"
            if "其他" in reason or "0117" in str((fields or {}).get("rsm_reqsubj2", "")):
                 msg += "\n(⚠️ 類別為'其他'，請手動補充細節)"
            self.notifier.notify_admin(msg)
        return ok

    def execute_portal_condition_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None) -> bool:
        ok = self.execute_portal_workflow_draft("condition", case_number, client_name, fields)
        if ok and (not self.dry_run):
            self.notifier.notify_admin(f"✅ 已暫存二階段資料 — {client_name or '-'}（{case_number or '-'}）")
        return ok

    def execute_portal_fee_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None) -> bool:
        ok = self.execute_portal_workflow_draft("fee", case_number, client_name, fields)
        if ok and (not self.dry_run):
            self.notifier.notify_admin(f"✅ 已暫存費用支付資料 — {client_name or '-'}（{case_number or '-'}）")
        return ok

    def _lookup_case_identity(
        self,
        *,
        laf_case_number: str = "",
        case_number: str = "",
        client_name: str = "",
        action: str = "",
    ) -> dict:
        """
        Resolve case identity for portal workflows.
        action: 用來根據 legal_aid_status 自動篩選候選案件
          - go_live → 只看 '未開辦'
          - closing → 只看 '進行中' / '已結案，待報結'
          - 其他 → 不篩選
        Returns best-effort fields:
          - laf_case_number
          - case_number (OSC)
          - client_name
          - folder_path (canonical)
          - case_folder (local translated)
        """
        out = {
            "laf_case_number": (laf_case_number or "").strip(),
            "case_number": (case_number or "").strip(),
            "client_name": (client_name or "").strip(),
            "folder_path": "",
            "case_folder": "",
            "candidate_count": 0,
            "confidence": "none",
            "matched_signals": [],
            "needs_manual_confirm": False,
            "manual_reason": "",
        }
        norm = self._norm_token
        req_laf = norm(out["laf_case_number"])
        req_case = norm(out["case_number"])
        req_client = norm(out["client_name"])

        if self.require_case_signal_for_auto and (not req_laf) and (not req_case):
            out["needs_manual_confirm"] = True
            out["manual_reason"] = "missing_case_or_laf_signal"

        if not self.db:
            return out

        def _query_candidates(where_sql: str, params: tuple) -> List[dict]:
            try:
                q = (
                    "SELECT `id`, `case_number`, `client_name`, `legal_aid_number`, `folder_path`, `legal_aid_status` "
                    "FROM `cases` "
                    f"WHERE {where_sql} "
                    "ORDER BY `id` DESC LIMIT 200"
                )
                rows = self.db.fetch_all(q, params, as_dict=True) or []
                return [r for r in rows if isinstance(r, dict)]
            except Exception as e:
                logger.warning("Case identity candidate query failed (%s): %s", where_sql, e)
                return []

        candidate_map: Dict[tuple, dict] = {}

        def _merge_row(row: dict, signal: str) -> None:
            cno = str(row.get("case_number") or "").strip()
            cname = str(row.get("client_name") or "").strip()
            laf_no = str(row.get("legal_aid_number") or "").strip()
            fpath = str(row.get("folder_path") or "").strip()
            laf_status = str(row.get("legal_aid_status") or "").strip()
            cfolder = self._to_local_case_folder(fpath)
            key = (norm(laf_no), norm(cno), norm(cname), norm(cfolder or fpath))
            if key not in candidate_map:
                candidate_map[key] = {
                    "id": str(row.get("id") or "").strip(),
                    "case_number": cno,
                    "client_name": cname,
                    "laf_case_number": laf_no,
                    "folder_path": fpath,
                    "case_folder": cfolder,
                    "legal_aid_status": laf_status,
                    "_signals": {signal},
                }
                return
            c = candidate_map[key]
            if not str(c.get("id") or "").strip():
                c["id"] = str(row.get("id") or "").strip()
            c["_signals"].add(signal)

        if req_laf:
            for r in _query_candidates("TRIM(`legal_aid_number`) = %s", (out["laf_case_number"],)):
                _merge_row(r, "laf_case_number")
        if req_case:
            for r in _query_candidates("TRIM(`case_number`) = %s", (out["case_number"],)):
                _merge_row(r, "case_number")
        if req_client:
            # Exact match first
            for r in _query_candidates(
                "LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(`client_name`), ' ', ''), '　', ''), '·', ''), '・', ''), '‧', ''), '．', '')) = %s "
                "AND (TRIM(COALESCE(`legal_aid_number`, '')) <> '' OR TRIM(COALESCE(`case_number`, '')) <> '')",
                (req_client,),
            ):
                _merge_row(r, "client_name")
            # Prefix match — handles foreign name suffixes, e.g. "[當事人N]" matches "[當事人N]Ayka lku"
            if not candidate_map and len(req_client) >= 2:
                for r in _query_candidates(
                    "LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(`client_name`), ' ', ''), '　', ''), '·', ''), '・', ''), '‧', ''), '．', '')) LIKE %s "
                    "AND (TRIM(COALESCE(`legal_aid_number`, '')) <> '' OR TRIM(COALESCE(`case_number`, '')) <> '')",
                    (req_client + "%",),
                ):
                    _merge_row(r, "client_name")

        candidates = list(candidate_map.values())
        out["candidate_count"] = len(candidates)

        # ── action-based status filtering ──
        # 根據 action 類型用 legal_aid_status 自動篩選，避免同名當事人歧義
        # go_live → 排除已結案（OSC 可能自動從「未開辦」改為「進行中」，但法扶平台尚未開辦）
        # closing → 排除未開辦
        _act = (action or "").strip().lower()
        _status_exclude: dict = {
            "go_live": {"已結案", "已結案，待報結"},  # 開辦 → 看未開辦+進行中，排除已結案
            "closing": {"未開辦", ""},                # 結案 → 排除未開辦
            "withdrawal": {"未開辦", "", "已結案"},     # 撤回 → 排除未開辦和已結案
        }
        _excluded = _status_exclude.get(_act)
        if _excluded and len(candidates) > 1:
            status_matched = [
                c for c in candidates
                if (c.get("legal_aid_status") or "") not in _excluded
            ]
            if status_matched:
                logger.info("  🔍 action=%s status filter: %d→%d candidates (excluded statuses: %s)",
                            _act, len(candidates), len(status_matched), _excluded)
                candidates = status_matched

        filtered: List[dict] = []
        rejected: List[dict] = []

        for cand in candidates:
            c_laf = norm(cand.get("laf_case_number", ""))
            c_case = norm(cand.get("case_number", ""))
            c_client = norm(cand.get("client_name", ""))
            conflict = ""
            # Empty DB field = unknown, not a conflict; only reject actual mismatches
            # 當 laf_case_number 或 case_number 完全匹配時，client_name 不符不算 conflict
            _has_strong_match = (req_laf and c_laf and c_laf == req_laf) or (req_case and c_case and c_case == req_case)
            if req_laf and c_laf and c_laf != req_laf:
                conflict = "laf_case_number_mismatch"
            elif req_case and c_case and c_case != req_case:
                conflict = "case_number_mismatch"
            elif req_client and c_client and c_client != req_client and not c_client.startswith(req_client) and not _has_strong_match:
                conflict = "client_name_mismatch"
            if conflict:
                rejected.append(
                    {
                        "laf_case_number": cand.get("laf_case_number", ""),
                        "case_number": cand.get("case_number", ""),
                        "client_name": cand.get("client_name", ""),
                        "reason": conflict,
                    }
                )
                continue
            score = 0
            matched: List[str] = []
            if req_laf and c_laf == req_laf:
                score += 100
                matched.append("laf_case_number")
            if req_case and c_case == req_case:
                score += 90
                matched.append("case_number")
            if req_client and (c_client == req_client or c_client.startswith(req_client)):
                score += 40
                matched.append("client_name")
            score += 5 * len(cand.get("_signals") or [])
            if cand.get("case_folder") and os.path.isdir(str(cand.get("case_folder") or "")):
                score += 5
            cand["score"] = score
            cand["matched"] = matched
            filtered.append(cand)

        if not filtered:
            if rejected:
                out["needs_manual_confirm"] = True
                out["manual_reason"] = "identity_signal_conflict"
                out["conflicts"] = rejected[:5]
                return out
            fallback = self._fallback_find_case_folders(
                client_name=out["client_name"],
                laf_case_number=out["laf_case_number"],
                limit=20,
            )
            if len(fallback) == 1 and (req_laf or req_case):
                fb = fallback[0]
                guessed_osc = self._guess_osc_case_no_from_folder(fb)
                guessed_laf = self._guess_laf_case_no_from_folder(fb)
                guessed_client = self._guess_client_name_from_folder(fb)
                if req_case and guessed_osc and norm(guessed_osc) != req_case:
                    out["needs_manual_confirm"] = True
                    out["manual_reason"] = "fallback_case_number_conflict"
                    return out
                if req_client and guessed_client and norm(guessed_client) != req_client and not norm(guessed_client).startswith(req_client):
                    out["needs_manual_confirm"] = True
                    out["manual_reason"] = "fallback_client_name_conflict"
                    return out
                out["case_folder"] = fb
                out["folder_path"] = fb
                out["case_number"] = out["case_number"] or guessed_osc
                out["laf_case_number"] = out["laf_case_number"] or guessed_laf
                out["client_name"] = out["client_name"] or guessed_client
                out["confidence"] = "low"
                out["matched_signals"] = ["folder_fallback"]
                return out
            out["needs_manual_confirm"] = True
            out["manual_reason"] = "identity_not_found"
            out["fallback_candidates"] = fallback[:5]
            return out

        filtered.sort(key=lambda x: (int(x.get("score") or 0), str(x.get("id") or "")), reverse=True)
        top = filtered[0]
        runner = filtered[1] if len(filtered) > 1 else None
        top_score = int(top.get("score") or 0)
        runner_score = int(runner.get("score") or 0) if runner else -1
        if runner and runner_score == top_score:
            out["needs_manual_confirm"] = True
            out["manual_reason"] = "identity_ambiguous"
            out["top_candidates"] = [
                {
                    "laf_case_number": str(c.get("laf_case_number") or ""),
                    "case_number": str(c.get("case_number") or ""),
                    "client_name": str(c.get("client_name") or ""),
                    "case_folder": str(c.get("case_folder") or ""),
                }
                for c in filtered[:5]
            ]
            return out

        matched = list(top.get("matched") or [])
        # Relax the strong-signal requirement when there is exactly ONE candidate
        # and client_name matches — this means the DB unambiguously identified the case.
        _sole_candidate_client_match = (
            len(filtered) == 1
            and "client_name" in matched
            and top.get("laf_case_number")
        )
        if (
            self.require_case_signal_for_auto
            and ("laf_case_number" not in matched)
            and ("case_number" not in matched)
            and not _sole_candidate_client_match
        ):
            out["needs_manual_confirm"] = True
            out["manual_reason"] = "missing_strong_identity_signal"
            out["top_candidates"] = [
                {
                    "laf_case_number": str(c.get("laf_case_number") or ""),
                    "case_number": str(c.get("case_number") or ""),
                    "client_name": str(c.get("client_name") or ""),
                    "case_folder": str(c.get("case_folder") or ""),
                }
                for c in filtered[:5]
            ]
            return out
        elif _sole_candidate_client_match:
            # Clear the early "missing_case_or_laf_signal" flag — DB lookup
            # unambiguously resolved the case via sole client_name match.
            out["needs_manual_confirm"] = False
            out["manual_reason"] = ""

        out["case_number"] = str(top.get("case_number") or out["case_number"]).strip()
        out["client_name"] = str(top.get("client_name") or out["client_name"]).strip()
        out["laf_case_number"] = str(top.get("laf_case_number") or out["laf_case_number"]).strip()
        out["folder_path"] = str(top.get("folder_path") or "").strip()
        out["case_folder"] = str(top.get("case_folder") or "").strip()
        out["matched_signals"] = matched
        if top_score >= 180:
            out["confidence"] = "high"
        elif top_score >= 100:
            out["confidence"] = "medium"
        else:
            out["confidence"] = "low"

        if (not out["case_folder"]) and (out["client_name"] or out["laf_case_number"]):
            fallback = self._fallback_find_case_folders(
                client_name=out["client_name"],
                laf_case_number=out["laf_case_number"],
                limit=20,
            )
            if len(fallback) == 1:
                out["case_folder"] = fallback[0]
                out["folder_path"] = fallback[0]
            elif len(fallback) > 1:
                out["needs_manual_confirm"] = True
                out["manual_reason"] = "case_folder_ambiguous"
                out["fallback_candidates"] = fallback[:5]
        return out

    @staticmethod
    def _guess_client_name_from_folder(folder_path: str) -> str:
        base = os.path.basename(str(folder_path or "").strip())
        if not base:
            return ""
        category_names = {
            "民事", "刑事", "行政", "家事", "法扶案件",
            "消費者債務清理", "無償案件", "指定辯護案件",
        }
        if base in category_names:
            return ""
        parts = [p for p in base.split("-") if p]
        if len(parts) >= 3:
            # ex: 2025-0047-[當事人D]-消費者債務清理-更生
            cand = parts[2].strip()
            if (cand not in category_names) and re.fullmatch(r"[一-龥A-Za-z0-9_ ]{2,30}", cand):
                return cand
        m = re.search(r"([一-龥]{2,5})", base)
        cand = (m.group(1) if m else "").strip()
        return "" if cand in category_names else cand

    @staticmethod
    def _is_case_folder_name(name: str) -> bool:
        return bool(re.match(r"^\d{4}-\d{4}-", str(name or "").strip()))

    @staticmethod
    def _laf_case_roots() -> List[str]:
        candidates = [
            os.path.join(root, "法扶案件")
            for root in preferred_case_roots(include_closed=False)
        ]
        return [p for p in candidates if os.path.isdir(p)]

    def _fallback_find_case_folder(self, client_name: str = "", laf_case_number: str = "") -> str:
        candidates = self._fallback_find_case_folders(client_name=client_name, laf_case_number=laf_case_number, limit=1)
        return candidates[0] if candidates else ""

    def _fallback_find_case_folders(self, client_name: str = "", laf_case_number: str = "", limit: int = 20) -> List[str]:
        cname = (client_name or "").strip()
        laf_no = (laf_case_number or "").strip()
        roots = self._laf_case_roots()
        if not roots:
            return []
        scored: List[tuple[int, float, str]] = []
        loose_candidates: List[str] = []
        for root in roots:
            try:
                for cat in os.listdir(root):
                    cat_path = os.path.join(root, cat)
                    if not os.path.isdir(cat_path):
                        continue
                    for d in os.listdir(cat_path):
                        case_path = os.path.join(cat_path, d)
                        if not os.path.isdir(case_path):
                            continue
                        if not self._is_case_folder_name(d):
                            continue
                        score = 0
                        if laf_no and (laf_no in d):
                            score += 4
                        if cname and (cname == self._guess_client_name_from_folder(case_path)):
                            score += 3
                        elif cname and (cname in d):
                            score += 2
                        if score <= 0 and laf_no:
                            # filename fallback (best-effort)
                            try:
                                found = False
                                for _b, _dirs, files in os.walk(case_path):
                                    if any(laf_no in f for f in files):
                                        found = True
                                        break
                                if found:
                                    score += 1
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2648, exc_info=True)
                        if score > 0:
                            try:
                                mtime = os.path.getmtime(case_path)
                            except Exception:
                                mtime = 0.0
                            scored.append((score, float(mtime), case_path))
                        else:
                            loose_candidates.append(case_path)
            except Exception:
                continue
        if scored:
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            out = [x[2] for x in scored]
            return out[: max(1, int(limit or 1))]
        if not loose_candidates:
            return []
        # Precision-first default: do not auto-pick "most recent" folder unless explicitly enabled.
        if not self.allow_loose_case_folder_fallback:
            return []
        loose_candidates = sorted(loose_candidates, key=lambda p: os.path.getmtime(p), reverse=True)
        return loose_candidates[: max(1, int(limit or 1))]

    def _pick_case_folder_for_action(
        self,
        *,
        action: str,
        current_folder: str = "",
        client_name: str = "",
        laf_case_number: str = "",
    ) -> tuple[str, dict]:
        """
        Pick the best case folder for a portal action.
        This mitigates stale DB folder_path rows by probing fallback folders.
        """
        action = (action or "").strip().lower()
        needs = {
            "go_live": lambda d: bool(d["opening_notice_files"] and d["poa_files"]),
            "condition": lambda d: bool(d["mediation_failure_files"]),
            "fee": lambda d: bool(d["pink_receipt_files"]),
            "withdrawal": lambda d: bool(self._get_withdrawal_pdf_candidates(d)),
        }
        wanted = needs.get(action, lambda _d: True)

        candidates: List[str] = []
        if current_folder and os.path.isdir(current_folder):
            candidates.append(current_folder)
        for p in self._fallback_find_case_folders(client_name=client_name, laf_case_number=laf_case_number, limit=40):
            if p not in candidates:
                candidates.append(p)

        if not candidates:
            return "", {
                "opening_notice_files": [],
                "poa_files": [],
                "mediation_failure_files": [],
                "mediation_success_files": [],
                "pink_receipt_files": [],
            }

        first_docs = None
        first_folder = ""
        for p in candidates:
            docs = self._scan_case_folder_docs(p)
            if first_docs is None:
                first_docs = docs
                first_folder = p
            if wanted(docs):
                return p, docs
        return first_folder, (first_docs or {
            "opening_notice_files": [],
            "poa_files": [],
            "mediation_failure_files": [],
            "mediation_success_files": [],
            "pink_receipt_files": [],
        })

    def _guess_laf_case_no_from_folder(self, folder_path: str) -> str:
        root = (folder_path or "").strip()
        if not root or not os.path.isdir(root):
            return ""
        pat = re.compile(r"(\d{6,8}-[A-Za-z]-\d{3})")
        # Search filenames first.
        try:
            for b, _d, fs in os.walk(root):
                for fn in fs:
                    m = pat.search(fn)
                    if m:
                        return m.group(1)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2738, exc_info=True)
        # Search folder name as fallback.
        m2 = pat.search(os.path.basename(root))
        return m2.group(1) if m2 else ""

    @staticmethod
    def _guess_osc_case_no_from_folder(folder_path: str) -> str:
        base = os.path.basename(str(folder_path or "").strip())
        if not base:
            return ""
        m = re.match(r"^(\d{4}-\d{4})-", base)
        return (m.group(1) if m else "").strip()

    @staticmethod
    def _normalize_date_text(raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return ""
        s = s.replace("年", "-").replace("月", "-").replace("日", "")
        s = s.replace("/", "-").replace(".", "-")
        s = re.sub(r"\s+", "", s)
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.search(r"(\d{3})-(\d{1,2})-(\d{1,2})", s)
        if m:
            y = int(m.group(1)) + 1911
            return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.search(r"(\d{4})(\d{2})(\d{2})", s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.search(r"(\d{3})(\d{2})(\d{2})", s)
        if m:
            y = int(m.group(1)) + 1911
            return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""

    def _extract_date_from_filename(self, path_value: str) -> str:
        base = os.path.basename(str(path_value or ""))
        return self._normalize_date_text(base)

    def _extract_date_with_vision(self, path_value: str) -> str:
        p = str(path_value or "").strip()
        if not p or not os.path.exists(p):
            return ""
        ext = Path(p).suffix.lower()
        img_path = p
        temp_img = ""
        try:
            if ext == ".pdf":
                import fitz
                doc = fitz.open(p)
                page = doc.load_page(0)
                pix = page.get_pixmap()
                temp_img = str(Path(p).with_suffix(".laf_tmp.jpg"))
                pix.save(temp_img)
                img_path = temp_img

            vision = LAFVision()
            raw = vision.extract_start_date(img_path) or ""
            return self._normalize_date_text(raw)
        except Exception as e:
            logger.warning("Vision date extraction failed for %s: %s", p, e)
            return ""
        finally:
            if temp_img and os.path.exists(temp_img):
                try:
                    import safe_fs
                    safe_fs.safe_remove(temp_img)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2808, exc_info=True)

    def _extract_best_date_from_doc(self, path_value: str) -> str:
        date_from_name = self._extract_date_from_filename(path_value)
        if date_from_name:
            return date_from_name
        return self._extract_date_with_vision(path_value)

    # ── 開辦自動化：遞狀日期偵測 + selRemark 生成 ──────────────────

    def _detect_poa_submission_info(self, case_folder: str) -> dict:
        """偵測遞狀日期。搜尋順序：
        1. 02_開辦資料 委任狀存底 → 檔名日期優先，檔名無日期才用 VLM
        2. 04_我方歷次書狀 第一份書狀存底 → 同上
        3. 11_回執 回執 → 同上
        4. 所有候選檔案的檔名日期 fallback

        Returns:
            {
                "date_roc": "115.3.27",
                "date_iso": "2026-03-27",
                "source": "stamp" | "receipt" | "pleading" | "filename",
                "source_file": "/path/to/file.pdf",
                "source_doc_type": "委任狀" | "書狀" | "回執" | "開辦通知書",
                "confidence": "high" | "medium" | "low",
            }
        """
        root = self._to_local_case_folder(case_folder) or case_folder
        if not root or not os.path.isdir(root):
            return {"confidence": "low"}

        go_live_dir = os.path.join(root, "02_開辦資料")
        pleading_dir = os.path.join(root, "04_我方歷次書狀")
        receipt_dir = os.path.join(root, "11_回執")

        def _try_filename_then_vision(path: str) -> tuple:
            """優先用檔名日期，沒有才用 VLM。Returns (date_iso, source_type)."""
            fn_date = self._extract_date_from_filename(path)
            if fn_date:
                return fn_date, "filename"
            # 檔名無日期 → 嘗試 VLM
            date_str = self._extract_date_with_vision(path)
            if date_str:
                date_iso = self._normalize_date_text(date_str) or date_str
                return date_iso, "stamp"
            return "", ""

        # ── 1) 02_開辦資料：委任狀存底 ──
        poa_candidates = []
        if os.path.isdir(go_live_dir):
            for fn in os.listdir(go_live_dir):
                if "委任狀" in fn and fn.lower().endswith(".pdf"):
                    poa_candidates.append(os.path.join(go_live_dir, fn))
        poa_candidates.sort(key=lambda p: ("存底" not in os.path.basename(p), p))

        for poa_path in poa_candidates:
            date_iso, src = _try_filename_then_vision(poa_path)
            if date_iso:
                date_roc = self._iso_to_roc(date_iso)
                logger.info("  🎯 委任狀日期: %s (%s, from %s)", date_roc, src, os.path.basename(poa_path))
                return {
                    "date_roc": date_roc, "date_iso": date_iso,
                    "source": src, "source_file": poa_path,
                    "source_doc_type": "委任狀",
                    "confidence": "high" if src == "stamp" else "medium",
                }

        # ── 2) 04_我方歷次書狀：第一份書狀存底（通常有收狀章）──
        pleading_candidates = []
        if os.path.isdir(pleading_dir):
            for sub in sorted(os.listdir(pleading_dir)):
                sub_path = os.path.join(pleading_dir, sub)
                if os.path.isdir(sub_path):
                    # 子資料夾（YYYYMMDD 書狀名稱），找裡面的存底 PDF
                    for fn in sorted(os.listdir(sub_path)):
                        if fn.lower().endswith(".pdf") and "存底" in fn:
                            pleading_candidates.append(os.path.join(sub_path, fn))
                elif sub.lower().endswith(".pdf") and "存底" in sub:
                    pleading_candidates.append(sub_path)
            # 只取第一份（最早的書狀）
            if not pleading_candidates and os.path.isdir(pleading_dir):
                # 沒有存底，就找第一份 PDF
                for sub in sorted(os.listdir(pleading_dir)):
                    sub_path = os.path.join(pleading_dir, sub)
                    if os.path.isdir(sub_path):
                        for fn in sorted(os.listdir(sub_path)):
                            if fn.lower().endswith(".pdf"):
                                pleading_candidates.append(os.path.join(sub_path, fn))
                                break
                    elif sub.lower().endswith(".pdf"):
                        pleading_candidates.append(sub_path)
                    if pleading_candidates:
                        break

        for pl_path in pleading_candidates[:1]:
            date_iso, src = _try_filename_then_vision(pl_path)
            if date_iso:
                date_roc = self._iso_to_roc(date_iso)
                logger.info("  🎯 書狀存底日期: %s (%s, from %s)", date_roc, src, os.path.basename(pl_path))
                return {
                    "date_roc": date_roc, "date_iso": date_iso,
                    "source": "pleading" if src == "stamp" else src,
                    "source_file": pl_path,
                    "source_doc_type": "書狀",
                    "confidence": "high" if src == "stamp" else "medium",
                }

        # ── 3) 11_回執：回執資料夾中所有 PDF 都是遞出證明 ──
        #   包括：郵局回執、書狀存底、委任狀存底等
        receipt_candidates = []
        if os.path.isdir(receipt_dir):
            for fn in sorted(os.listdir(receipt_dir), reverse=True):
                if fn.lower().endswith(".pdf"):
                    receipt_candidates.append(os.path.join(receipt_dir, fn))
        # 也看 02_開辦資料 裡的回執
        if os.path.isdir(go_live_dir):
            for fn in sorted(os.listdir(go_live_dir), reverse=True):
                if fn.lower().endswith(".pdf") and ("回執" in fn or "收件回執" in fn):
                    full = os.path.join(go_live_dir, fn)
                    if full not in receipt_candidates:
                        receipt_candidates.append(full)

        for receipt_path in receipt_candidates[:3]:
            date_iso, src = _try_filename_then_vision(receipt_path)
            if date_iso:
                date_roc = self._iso_to_roc(date_iso)
                # 回執的檔名日期 = 寄出日期
                logger.info("  🎯 回執日期: %s (%s, from %s)", date_roc, src, os.path.basename(receipt_path))
                return {
                    "date_roc": date_roc, "date_iso": date_iso,
                    "source": "receipt", "source_file": receipt_path,
                    "source_doc_type": "回執",
                    "confidence": "high" if src == "stamp" else "medium",
                }

        return {"confidence": "low"}

    @staticmethod
    def _iso_to_roc(iso_date: str) -> str:
        """西元日期 → 民國 (e.g. '2026-03-27' → '115.3.27')"""
        import re as _re
        m = _re.match(r"(\d{4})-?(\d{2})-?(\d{2})", str(iso_date or ""))
        if not m:
            return str(iso_date or "")
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y - 1911}.{mo}.{d}"

    def _compose_go_live_remark(self, submission_info: dict, client_name: str = "",
                                is_consumer_debt: bool = False) -> str:
        """根據遞狀日期資訊生成自然語言 selRemark。"""
        date_roc = submission_info.get("date_roc", "")
        if not date_roc:
            return ""

        source = submission_info.get("source", "")
        doc_type = submission_info.get("source_doc_type", "委任狀")

        # 消費者債務清理 — 不需要委任狀，只有開辦通知書
        if is_consumer_debt:
            return f"已於民國{date_roc}遞送聲請狀至法院。"

        # 根據文件來源生成不同措辭
        if doc_type == "回執":
            return f"已於民國{date_roc}以掛號郵寄相關書狀，詳見附件回執。"
        elif doc_type == "書狀":
            src_file = os.path.basename(submission_info.get("source_file", ""))
            return f"已於民國{date_roc}遞送書狀至法院。"
        else:
            # 委任狀（預設）
            if source == "receipt":
                return f"已於民國{date_roc}以掛號郵寄委任狀至法院。"
            return f"已於民國{date_roc}遞送委任狀至法院。"

    def _find_go_live_upload_files(self, case_folder: str, is_consumer_debt: bool = False) -> list:
        """找開辦上傳檔案。
        消債案件：只需開辦通知書。
        一般案件：委任狀存底 + 開辦通知書；無委任狀則找書狀存底或回執。
        """
        root = self._to_local_case_folder(case_folder) or case_folder
        if not root:
            return []
        go_live_dir = os.path.join(root, "02_開辦資料")
        pleading_dir = os.path.join(root, "04_我方歷次書狀")
        receipt_dir = os.path.join(root, "11_回執")
        result = []

        # ── 開辦通知書（所有案件都需要）──
        notice_candidates = []
        _notice_kw = ("開辦通知", "接案通知", "准予扶助", "開辦資料")
        if os.path.isdir(go_live_dir):
            for fn in os.listdir(go_live_dir):
                if fn.lower().endswith(".pdf") and any(k in fn for k in _notice_kw):
                    notice_candidates.append(os.path.join(go_live_dir, fn))
        notice_candidates.sort(reverse=True)

        if is_consumer_debt:
            # 消債案件：只需開辦通知書
            if notice_candidates:
                result.append(notice_candidates[0])
            return result

        # ── 一般案件：找遞狀證明文件 ──
        proof_file = None

        # 1) 02_開辦資料 委任狀存底
        poa_candidates = []
        if os.path.isdir(go_live_dir):
            for fn in os.listdir(go_live_dir):
                if fn.lower().endswith(".pdf") and "委任狀" in fn:
                    poa_candidates.append(os.path.join(go_live_dir, fn))
        poa_candidates.sort(key=lambda p: ("存底" not in os.path.basename(p), p))
        if poa_candidates:
            proof_file = poa_candidates[0]

        # 2) 找不到委任狀 → 04_我方歷次書狀 第一份存底
        if not proof_file and os.path.isdir(pleading_dir):
            for sub in sorted(os.listdir(pleading_dir)):
                sub_path = os.path.join(pleading_dir, sub)
                if os.path.isdir(sub_path):
                    for fn in sorted(os.listdir(sub_path)):
                        if fn.lower().endswith(".pdf") and "存底" in fn:
                            proof_file = os.path.join(sub_path, fn)
                            break
                    if not proof_file:
                        for fn in sorted(os.listdir(sub_path)):
                            if fn.lower().endswith(".pdf"):
                                proof_file = os.path.join(sub_path, fn)
                                break
                elif sub.lower().endswith(".pdf") and "存底" in sub:
                    proof_file = sub_path
                if proof_file:
                    break

        # 3) 找不到書狀 → 11_回執（資料夾中所有 PDF 都是遞出證明：
        #    回執、書狀存底、委任狀存底等）
        if not proof_file and os.path.isdir(receipt_dir):
            for fn in sorted(os.listdir(receipt_dir), reverse=True):
                if fn.lower().endswith(".pdf"):
                    proof_file = os.path.join(receipt_dir, fn)
                    break

        if proof_file:
            result.append(proof_file)
        if notice_candidates:
            result.append(notice_candidates[0])
        return result

    def _find_go_live_upload_file(self, case_folder: str) -> str:
        """向後相容：回傳第一個上傳檔案。"""
        files = self._find_go_live_upload_files(case_folder)
        return files[0] if files else ""

    # ── END 開辦自動化 ───────────────────────────────────────────

    def _scan_case_folder_docs(self, case_folder: str) -> dict:
        root = (case_folder or "").strip()
        out = {
            "opening_notice_files": [],
            "poa_files": [],
            "mediation_failure_files": [],
            "mediation_success_files": [],
            "pink_receipt_files": [],
        }
        if not root or (not os.path.isdir(root)):
            return out

        allowed = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
        for base, _dirs, files in os.walk(root):
            for fn in files:
                ext = Path(fn).suffix.lower()
                if ext not in allowed:
                    continue
                full = os.path.join(base, fn)
                low = fn.lower()

                if any(k in fn for k in ("開辦通知書", "接案通知書", "准予扶助證明書")):
                    out["opening_notice_files"].append(full)
                if "委任狀" in fn:
                    out["poa_files"].append(full)
                if any(k in fn for k in ("調解不成立證明書", "調解不成立")):
                    out["mediation_failure_files"].append(full)
                # 調解/和解成立證明（排除「不成立」）
                if any(k in fn for k in ("調解筆錄", "調解成立", "和解筆錄", "和解成立", "調解書")):
                    if "不成立" not in fn:
                        out["mediation_success_files"].append(full)
                if ("收據" in fn) or ("裁判費" in fn) or ("粉紅" in fn) or ("pink" in low):
                    out["pink_receipt_files"].append(full)
                if "回執" in fn or "收件回執" in fn:
                    out.setdefault("receipt_files", []).append(full)

        for k in out:
            out[k] = sorted(out[k])
        return out

    @staticmethod
    def _find_first_existing(paths: List[str]) -> str:
        for p in (paths or []):
            if p and os.path.exists(p):
                return p
        return ""

    def _to_pdf_for_portal(self, src_path: str, out_dir: str) -> str:
        """
        Convert a source file to PDF for portal upload.
        Returns generated PDF path; empty string on failure.
        """
        src = (src_path or "").strip()
        if not src or not os.path.isfile(src):
            return ""
        ext = Path(src).suffix.lower()
        out_root = Path(out_dir)
        out_root.mkdir(parents=True, exist_ok=True)

        stem = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", Path(src).stem).strip("_") or "doc"
        dst_pdf = out_root / f"{stem}.pdf"

        if ext == ".pdf":
            try:
                if os.path.abspath(src) != str(dst_pdf):
                    try:
                        shutil.copy2(src, dst_pdf)
                    except OSError:
                        # Fallback: buffered copy for NAS files with stale FD
                        with open(src, "rb") as fin, open(str(dst_pdf), "wb") as fout:
                            while True:
                                chunk = fin.read(1024 * 1024)
                                if not chunk:
                                    break
                                fout.write(chunk)
                return str(dst_pdf)
            except Exception:
                return src

        if ext in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}:
            try:
                from PIL import Image  # type: ignore
                with Image.open(src) as im:
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    im.save(dst_pdf, "PDF", resolution=300.0)
                return str(dst_pdf)
            except Exception as e:
                logger.warning("Image->PDF convert failed (%s): %s", src, e)

        soffice = self._find_first_existing(
            [
                os.environ.get("MAGI_SOFFICE_PATH", "").strip(),
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                "/opt/homebrew/bin/soffice",
                "/usr/local/bin/soffice",
            ]
        )
        if soffice:
            _lo_timeout = int(os.environ.get("MAGI_SOFFICE_TIMEOUT", "180") or "180")
            for _attempt in range(2):
                try:
                    if _attempt > 0:
                        # Kill stale soffice before retry
                        subprocess.run(["pkill", "-f", "soffice"], capture_output=True, timeout=5)
                        import time as _t; _t.sleep(2)
                    subprocess.run(
                        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_root), src],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=_lo_timeout,
                    )
                    cand = out_root / (Path(src).stem + ".pdf")
                    if cand.exists():
                        if cand != dst_pdf:
                            shutil.move(str(cand), str(dst_pdf))
                        return str(dst_pdf)
                except subprocess.TimeoutExpired:
                    logger.warning("LibreOffice convert timeout (attempt %d, %ds): %s", _attempt + 1, _lo_timeout, src)
                    if _attempt == 0:
                        continue  # retry after killing stale process
                except Exception as e:
                    logger.warning("LibreOffice convert failed (%s): %s", src, e)
                    break

        textutil = self._find_first_existing(
            [
                os.environ.get("MAGI_TEXTUTIL_PATH", "").strip(),
                "/usr/bin/textutil",
            ]
        )
        if textutil and ext in {".txt", ".rtf", ".rtfd", ".html", ".htm", ".md", ".csv", ".json", ".xml", ".log"}:
            try:
                subprocess.run(
                    [textutil, "-convert", "pdf", src, "-output", str(dst_pdf)],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=45,
                )
                if dst_pdf.exists():
                    return str(dst_pdf)
            except Exception as e:
                logger.warning("textutil convert failed (%s): %s", src, e)

        return ""

    def _collect_progress_upload_pdfs(self, case_folder: str, laf_case_no: str = "", action: str = "") -> dict:
        """
        Collect upload files for progress/closing workflows:
        - 04_我方歷次書狀 (recursive): convert all files to PDF
        - 10_判決書 (recursive): include all PDFs
        """
        root = (case_folder or "").strip()
        result = {
            "ok": False,
            "case_folder": root,
            "action": (action or "").strip(),
            "pleading_source_files": [],
            "judgment_pdf_files": [],
            "pdf_files": [],
            "converted": [],
            "failed": [],
            "staging_dir": "",
        }
        if not root or (not os.path.isdir(root)):
            result["error"] = "missing_case_folder"
            return result

        plead_root = os.path.join(root, "04_我方歷次書狀")
        judgment_root = os.path.join(root, "10_判決書")
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_laf = re.sub(r"[^\w\-]+", "_", (laf_case_no or "").strip()) or "unknown"
        safe_act = re.sub(r"[^\w\-]+", "_", (action or "").strip()) or "workflow"
        staging_dir = os.path.join(tempfile.gettempdir(), "magi_laf_upload_pdf", f"{run_id}_{safe_laf}_{safe_act}")
        os.makedirs(staging_dir, exist_ok=True)
        result["staging_dir"] = staging_dir

        # 非書狀的附件/證據關鍵字（這些檔案不應上傳到報結頁）
        _attachment_keywords = [
            "聲證", "證據", "附件", "債權人清冊", "財產及收入", "財產收入",
            "財產狀況", "收入狀況", "戶籍謄本", "診斷書", "薪資",
            "勞保", "國保", "稅務", "所得", "信用報告", "對話",
        ]

        pleading_files: List[str] = []
        if os.path.isdir(plead_root):
            # 每個子資料夾獨立篩選：
            # 1. 有「存底」或「留底」PDF → 只上傳那份
            # 2. 沒有 → 轉換最新的 WORD 檔（依 v2>v1、清稿>草稿、修改時間判斷）
            for base, _dirs, files in os.walk(plead_root):
                sorted_files = sorted(files)

                # 先篩出非隱藏、非附件/證據的檔案
                candidates = []
                for fn in sorted_files:
                    if fn.startswith(".") or fn.startswith("~"):
                        continue
                    full = os.path.join(base, fn)
                    if not os.path.isfile(full):
                        continue
                    if any(kw in fn for kw in _attachment_keywords):
                        logger.debug("  跳過非書狀（附件/證據）: %s", fn)
                        continue
                    candidates.append((fn, full))

                if not candidates:
                    continue

                # 找「存底」或「留底」PDF
                archive_pdfs = [
                    (fn, full) for fn, full in candidates
                    if fn.lower().endswith(".pdf") and ("存底" in fn or "留底" in fn)
                ]
                if archive_pdfs:
                    # 有存底/留底 → 只上傳這些，其餘全跳過
                    for fn, full in archive_pdfs:
                        pleading_files.append(full)
                        logger.debug("  選取存底/留底: %s", fn)
                    continue

                # 沒有存底/留底 → 找最新的 WORD 檔轉 PDF
                word_files = [
                    (fn, full) for fn, full in candidates
                    if Path(fn).suffix.lower() in (".docx", ".doc", ".odt")
                ]
                if word_files:
                    def _word_version_key(item):
                        """排序鍵：清稿 > 定稿 > v數字 > 修改時間"""
                        fn = item[0].lower()
                        full = item[1]
                        priority = 0
                        if "清稿" in fn or "定稿" in fn or "final" in fn:
                            priority = 100
                        # 提取版本號 v1, v2, v3...
                        import re as _re
                        vm = _re.search(r'v(\d+)', fn)
                        if vm:
                            priority = max(priority, int(vm.group(1)))
                        try:
                            mtime = os.path.getmtime(full)
                        except OSError:
                            mtime = 0
                        return (priority, mtime)

                    best_word = max(word_files, key=_word_version_key)
                    pleading_files.append(best_word[1])
                    logger.debug("  選取最新書狀 WORD: %s", best_word[0])
                    continue

                # 連 WORD 都沒有 → 找 PDF（排除含「暨」「附件」的合併檔）
                plain_pdfs = [
                    (fn, full) for fn, full in candidates
                    if fn.lower().endswith(".pdf") and "暨" not in fn
                ]
                if plain_pdfs:
                    # 取最新的一份
                    best_pdf = max(plain_pdfs, key=lambda x: os.path.getmtime(x[1]) if os.path.exists(x[1]) else 0)
                    pleading_files.append(best_pdf[1])
                    logger.debug("  選取最新書狀 PDF: %s", best_pdf[0])
        result["pleading_source_files"] = pleading_files

        judgment_pdfs: List[str] = []
        if os.path.isdir(judgment_root):
            for base, _dirs, files in os.walk(judgment_root):
                for fn in sorted(files):
                    if fn.startswith("."):
                        continue
                    full = os.path.join(base, fn)
                    if os.path.isfile(full) and fn.lower().endswith(".pdf"):
                        judgment_pdfs.append(full)
        result["judgment_pdf_files"] = judgment_pdfs

        out_pdf: List[str] = []
        converted: List[dict] = []
        failed: List[dict] = []
        dedup = set()
        max_files = int(os.environ.get("MAGI_LAF_MAX_UPLOAD_SOURCE_FILES", "400") or "400")

        for src in pleading_files[: max(1, max_files)]:
            pdf = self._to_pdf_for_portal(src, staging_dir)
            if pdf and (pdf not in dedup):
                out_pdf.append(pdf)
                dedup.add(pdf)
                converted.append({"source": src, "pdf": pdf})
            elif not pdf:
                failed.append({"source": src, "error": "convert_failed"})

        for src_pdf in judgment_pdfs[: max(1, max_files)]:
            try:
                dst = os.path.join(staging_dir, os.path.basename(src_pdf))
                if os.path.abspath(src_pdf) != os.path.abspath(dst):
                    try:
                        shutil.copy2(src_pdf, dst)
                    except OSError:
                        # Fallback: buffered copy for NAS files with stale FD
                        with open(src_pdf, "rb") as fin, open(dst, "wb") as fout:
                            while True:
                                chunk = fin.read(1024 * 1024)
                                if not chunk:
                                    break
                                fout.write(chunk)
                else:
                    dst = src_pdf
                if dst not in dedup:
                    out_pdf.append(dst)
                    dedup.add(dst)
            except Exception as e:
                failed.append({"source": src_pdf, "error": f"copy_failed:{e}"})

        result["converted"] = converted
        result["failed"] = failed
        result["pdf_files"] = out_pdf
        result["ok"] = bool(out_pdf)
        if not out_pdf:
            result["error"] = "no_pdf_generated"
        return result

    def _collect_selected_upload_pdfs(
        self,
        source_files: List[str],
        *,
        laf_case_no: str = "",
        action: str = "",
        label: str = "",
    ) -> dict:
        """
        Convert selected source files to upload PDFs.
        Used for workflows that should upload only specific evidence.
        """
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_laf = re.sub(r"[^\w\-]+", "_", (laf_case_no or "").strip()) or "unknown"
        safe_act = re.sub(r"[^\w\-]+", "_", (action or "").strip()) or "workflow"
        safe_label = re.sub(r"[^\w\-]+", "_", (label or "").strip()) or "selected"
        staging_dir = os.path.join(tempfile.gettempdir(), "magi_laf_upload_pdf", f"{run_id}_{safe_laf}_{safe_act}_{safe_label}")
        os.makedirs(staging_dir, exist_ok=True)

        result = {
            "ok": False,
            "action": (action or "").strip(),
            "label": (label or "").strip(),
            "source_files": list(source_files or []),
            "pdf_files": [],
            "converted": [],
            "failed": [],
            "staging_dir": staging_dir,
        }

        out_pdf: List[str] = []
        converted: List[dict] = []
        failed: List[dict] = []
        dedup = set()
        for src in (source_files or []):
            pdf = self._to_pdf_for_portal(src, staging_dir)
            if pdf and (pdf not in dedup):
                out_pdf.append(pdf)
                dedup.add(pdf)
                converted.append({"source": src, "pdf": pdf})
            elif not pdf:
                failed.append({"source": src, "error": "convert_failed"})

        result["pdf_files"] = out_pdf
        result["converted"] = converted
        result["failed"] = failed
        result["ok"] = bool(out_pdf)
        if not out_pdf:
            result["error"] = "no_pdf_generated"
        return result

    def execute_portal_action_draft(
        self,
        *,
        action: str,
        laf_case_number: str = "",
        case_number: str = "",
        client_name: str = "",
        reason: str = "",
        fields: Optional[dict] = None,
    ) -> dict:
        """
        Execute one portal workflow in draft-only mode.
        action: go_live | inquiry | fee | condition | withdrawal | closing
        """
        act = (action or "").strip().lower()
        fields = dict(fields or {})
        identity = self._lookup_case_identity(
            laf_case_number=laf_case_number,
            case_number=case_number,
            client_name=client_name,
            action=act,
        )
        if identity.get("needs_manual_confirm"):
            return {
                "ok": False,
                "error": "identity_needs_manual_confirmation",
                "action": act,
                "identity": identity,
            }
        laf_no = (identity.get("laf_case_number") or "").strip()
        osc_no = (identity.get("case_number") or "").strip()
        cname = (identity.get("client_name") or "").strip()
        case_folder = (identity.get("case_folder") or "").strip()
        docs = self._scan_case_folder_docs(case_folder) if case_folder else self._empty_docs_map()

        if act not in {"go_live", "inquiry", "fee", "condition", "withdrawal", "closing"}:
            return {"ok": False, "error": f"unknown_action:{act}"}

        # Workflows except closing can run with either LAF case no or client name.
        if act != "closing" and (not laf_no and not cname):
            return {"ok": False, "error": "missing_target", "action": act, "identity": identity}

        if act in {"go_live", "condition", "fee"}:
            picked_folder, picked_docs = self._pick_case_folder_for_action(
                action=act,
                current_folder=case_folder,
                client_name=cname,
                laf_case_number=laf_no,
            )
            if picked_folder:
                case_folder = picked_folder
                identity["case_folder"] = picked_folder
                identity["folder_path"] = picked_folder
            docs = picked_docs

        upload_bundle = {}
        if act in {"inquiry", "closing"} and case_folder:
            upload_bundle = self._collect_progress_upload_pdfs(case_folder, laf_case_no=laf_no, action=act)
            if upload_bundle.get("pdf_files"):
                fields.setdefault("upload_files", upload_bundle.get("pdf_files") or [])
                fields.setdefault("upload_mode", "replace")

        if act == "go_live":
            if not case_folder:
                return {"ok": False, "error": "missing_case_folder", "action": act, "identity": identity}
            # 開辦只認 02_開辦資料 內的文件
            _gl_dir = os.path.join(self._to_local_case_folder(case_folder) or case_folder, "02_開辦資料")
            _gl_docs = self._scan_case_folder_docs(_gl_dir) if os.path.isdir(_gl_dir) else self._empty_docs_map()
            _is_consumer_debt = self._is_consumer_debt_case_folder(case_folder)
            # 消債案件只需開辦通知書（簽名即可）；一般案件需要開辦通知書 + 委任狀
            _need_poa = not _is_consumer_debt
            if not _gl_docs["opening_notice_files"] or (_need_poa and not _gl_docs["poa_files"]):
                missing = []
                if not _gl_docs["opening_notice_files"]:
                    missing.append("開辦通知書/接案通知書")
                if _need_poa and not _gl_docs["poa_files"]:
                    missing.append("委任狀")
                hint = "請將開辦通知書放入 02_開辦資料 資料夾" if _is_consumer_debt else "請將開辦通知與委任狀放入 02_開辦資料 資料夾"
                return {
                    "ok": False,
                    "error": "missing_required_docs",
                    "action": act,
                    "identity": identity,
                    "missing": missing,
                    "hint": hint,
                    "docs": _gl_docs,
                }
            open_doc = _gl_docs["opening_notice_files"][0]
            poa_doc = _gl_docs["poa_files"][0] if _gl_docs["poa_files"] else ""
            open_date = self._extract_best_date_from_doc(open_doc)
            poa_date = self._extract_best_date_from_doc(poa_doc) if poa_doc else ""
            if not open_date or (_need_poa and not poa_date):
                missing_dates = []
                if not open_date:
                    missing_dates.append("開辦通知書日期")
                if _need_poa and not poa_date:
                    missing_dates.append("委任狀遞出日期")
                return {
                    "ok": False,
                    "error": "missing_required_dates",
                    "action": act,
                    "identity": identity,
                    "missing": missing_dates,
                    "docs": {"opening_notice": open_doc, "poa": poa_doc},
                }
            fields.setdefault("sel_result", "1")
            if _is_consumer_debt:
                default_remark = f"已簽署開辦通知書（開辦日期 {open_date}）。"
            else:
                default_remark = (
                    f"CASPER 開辦資料判讀：開辦日期 {open_date}；委任狀遞出日期 {poa_date}。"
                )
            fields.setdefault("remark", default_remark)
            # 找出要上傳的檔案（開辦通知書、消債不需委任狀）
            go_live_upload = self._find_go_live_upload_files(case_folder, is_consumer_debt=_is_consumer_debt)
            if go_live_upload:
                fields.setdefault("upload_files", go_live_upload)
            ok = self.execute_portal_go_live_draft(laf_no, cname, fields or {})
            result = {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "docs": {"opening_notice": open_doc, "poa": poa_doc},
                "dates": {"opening_date": open_date, "poa_submit_date": poa_date},
                "preview": self._last_portal_artifact,
            }
            if not ok:
                result["error"] = "portal_draft_failed"
            return result

        if act == "withdrawal":
            if not case_folder:
                return {"ok": False, "error": "missing_case_folder", "action": act, "identity": identity}
            withdrawal_pdf_files = self._get_withdrawal_pdf_candidates(docs)
            withdrawal_non_pdf_files = self._get_withdrawal_non_pdf_candidates(docs)
            withdrawal_template_files = self._get_withdrawal_template_candidates(docs)
            if not withdrawal_pdf_files:
                missing = ["已簽署撤回書 PDF"]
                if withdrawal_template_files:
                    missing.append("已找到制式撤回書母版，等待簽名後 PDF 版本，暫不撤案")
                elif withdrawal_non_pdf_files:
                    missing.append("目前僅找到未簽署或非 PDF 撤回書，不能直接撤案暫存")
                return {
                    "ok": False,
                    "error": "missing_required_docs",
                    "action": act,
                    "identity": identity,
                    "missing": missing,
                    "docs": {
                        "withdrawal_pdf_files": withdrawal_pdf_files,
                        "withdrawal_non_pdf_files": withdrawal_non_pdf_files,
                        "withdrawal_template_files": withdrawal_template_files,
                        "withdrawal_files": list(docs.get("withdrawal_files") or []),
                    },
                }
            counts = {}
            close_case_no = osc_no or case_number or laf_no
            if (not close_case_no) and case_folder:
                guessed_osc = self._guess_osc_case_no_from_folder(case_folder)
                guessed_laf = self._guess_laf_case_no_from_folder(case_folder)
                close_case_no = guessed_osc or guessed_laf or ""
                if guessed_osc and not identity.get("case_number"):
                    identity["case_number"] = guessed_osc
                if guessed_laf and not identity.get("laf_case_number"):
                    identity["laf_case_number"] = guessed_laf
            if close_case_no:
                try:
                    counts = self._gather_case_counts(close_case_no, cname)
                except Exception:
                    counts = {}

            if "pb_reason" not in fields and reason:
                fields["pb_reason"] = self._match_withdrawal_reason(reason)
            withdrawal_doc = withdrawal_pdf_files[0]
            upload_bundle = self._collect_selected_upload_pdfs(
                [withdrawal_doc],
                laf_case_no=laf_no,
                action=act,
                label="withdrawal_letter",
            )
            if not upload_bundle.get("pdf_files"):
                return {
                    "ok": False,
                    "error": "withdrawal_pdf_prepare_failed",
                    "action": act,
                    "identity": identity,
                    "docs": {"withdrawal_letter": withdrawal_doc},
                    "upload_bundle": upload_bundle,
                }
            if upload_bundle.get("pdf_files"):
                fields.setdefault("upload_files", upload_bundle.get("pdf_files") or [])
                fields.setdefault("upload_mode", "replace")
            if "reason_text" not in fields:
                base_reason = reason or f"依已簽署撤回書辦理受扶助人撤回（{os.path.basename(withdrawal_doc)}）。"
                if counts:
                    summary = (
                        f"辦理情形：開會{int(counts.get('meeting_count', 0) or 0)}次、"
                        f"聯繫{int(counts.get('contact_count', 0) or 0)}次、"
                        f"開庭{int(counts.get('court_count', 0) or 0)}次、"
                        f"書狀{int(counts.get('document_count', 0) or 0)}份、"
                        f"閱卷{int(counts.get('review_count', 0) or 0)}次。"
                    )
                    fields["reason_text"] = f"{base_reason} {summary}"
                else:
                    fields["reason_text"] = base_reason
            fields.setdefault("desc", fields.get("reason_text", ""))
            # 撤回案件需同步填寫結案資料彙整（辦理情形）
            if counts:
                fields.setdefault("closing_counts", counts)
                fields.setdefault("lawy_status", "P")  # 辦理中
                fields.setdefault("pb_lawyer_status", "P")
            ok = self.execute_portal_withdrawal_draft(laf_no, cname, fields or {})
            return {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "counts": counts,
                "docs": {
                    "withdrawal_letter": withdrawal_doc,
                    "withdrawal_pdf_files": withdrawal_pdf_files,
                    "withdrawal_non_pdf_files": withdrawal_non_pdf_files,
                    "withdrawal_template_files": withdrawal_template_files,
                },
                "upload_bundle": upload_bundle,
                "preview": self._last_portal_artifact,
            }

        if act == "inquiry":
            if not reason and (not str(fields.get("desc") or "").strip()):
                return {"ok": False, "error": "missing_reason", "action": act, "identity": identity}
            if "rsm_reqsubj1" not in fields:
                fields["rsm_reqsubj1"] = "0001"
            if "rsm_reqsubj2" not in fields:
                fields["rsm_reqsubj2"] = self._match_inquiry_reason(reason or "")
            if "desc" not in fields:
                fields["desc"] = reason
            # 疑義案件需同步填寫結案資料彙整（辦理情形）
            close_case_no = osc_no or case_number or laf_no
            if close_case_no and "closing_counts" not in fields:
                try:
                    _inq_counts = self._gather_case_counts(close_case_no, cname)
                    if _inq_counts:
                        fields["closing_counts"] = _inq_counts
                        fields.setdefault("lawy_status", "P")  # 辦理中
                        fields.setdefault("rsm_lawyer_status", "P")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3674, exc_info=True)
            ok = self.execute_portal_inquiry_draft(laf_no, cname, fields or {})
            return {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "upload_bundle": upload_bundle,
                "preview": self._last_portal_artifact,
            }

        if act == "fee":
            if not case_folder:
                return {"ok": False, "error": "missing_case_folder", "action": act, "identity": identity}
            if not docs["pink_receipt_files"]:
                return {
                    "ok": False,
                    "error": "missing_required_docs",
                    "action": act,
                    "identity": identity,
                    "missing": ["法院粉紅色收據（含收據/裁判費）"],
                    "docs": docs,
                }
            if "lgfee_reqsubj1" not in fields or "lgfee_reqsubj2" not in fields:
                subj1, subj2 = self._match_fee_type(reason or "")
                fields.setdefault("lgfee_reqsubj1", subj1)
                fields.setdefault("lgfee_reqsubj2", subj2)
            # reqsubj3 only visible when reqsubj2=0120 (支付裁判費); portal values: 0132-0136
            if fields.get("lgfee_reqsubj2") == "0120" and "lgfee_reqsubj3" not in fields:
                fields.setdefault("lgfee_reqsubj3", "0132")  # 預設：三千元以下之訴訟費用律師聲請本會墊付
            receipt_name = os.path.basename(docs["pink_receipt_files"][0])
            upload_bundle = self._collect_selected_upload_pdfs(
                docs["pink_receipt_files"],
                laf_case_no=laf_no,
                action=act,
                label="pink_receipt",
            )
            if upload_bundle.get("pdf_files"):
                fields.setdefault("upload_files", upload_bundle.get("pdf_files") or [])
                fields.setdefault("upload_mode", "replace")
            fields.setdefault("desc", reason or f"依法院收據辦理（{receipt_name}）")
            fields.setdefault("lgfee_lawyer_status", "N")
            ok = self.execute_portal_fee_draft(laf_no, cname, fields or {})
            return {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "docs": {"pink_receipt": docs["pink_receipt_files"][0]},
                "upload_bundle": upload_bundle,
                "preview": self._last_portal_artifact,
            }

        if act == "condition":
            if not case_folder:
                return {"ok": False, "error": "missing_case_folder", "action": act, "identity": identity}
            if not docs["mediation_failure_files"]:
                return {
                    "ok": False,
                    "error": "missing_required_docs",
                    "action": act,
                    "identity": identity,
                    "missing": ["調解不成立證明書"],
                    "docs": docs,
                }
            med_doc = docs["mediation_failure_files"][0]
            upload_bundle = self._collect_selected_upload_pdfs(
                [med_doc],
                laf_case_no=laf_no,
                action=act,
                label="mediation_failure",
            )
            if upload_bundle.get("pdf_files"):
                fields.setdefault("upload_files", upload_bundle.get("pdf_files") or [])
                fields.setdefault("upload_mode", "replace")
            fields.setdefault("at_ctype", "附條件審查")
            fields.setdefault("conditionrsn", reason or f"依調解不成立證明書辦理（{os.path.basename(med_doc)}）")
            ok = self.execute_portal_condition_draft(laf_no, cname, fields or {})
            return {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "docs": {"mediation_failure": med_doc},
                "upload_bundle": upload_bundle,
                "preview": self._last_portal_artifact,
            }

        # closing
        close_case_no = laf_no or case_number
        if (not close_case_no) and case_folder:
            guessed_osc = self._guess_osc_case_no_from_folder(case_folder)
            if guessed_osc:
                close_case_no = guessed_osc
                identity["case_number"] = guessed_osc
        if (not close_case_no) and cname:
            for p in self._fallback_find_case_folders(client_name=cname, laf_case_number=laf_no, limit=40):
                guessed_laf = self._guess_laf_case_no_from_folder(p)
                guessed_osc = self._guess_osc_case_no_from_folder(p)
                if guessed_laf or guessed_osc:
                    if guessed_laf:
                        laf_no = guessed_laf
                        identity["laf_case_number"] = guessed_laf
                    if guessed_osc:
                        identity["case_number"] = guessed_osc
                    identity["case_folder"] = p
                    identity["folder_path"] = p
                    close_case_no = guessed_laf or guessed_osc
                    break
        if not close_case_no:
            return {"ok": False, "error": "missing_case_number_for_closing", "action": act, "identity": identity}

        if not case_folder:
            return {
                "ok": False,
                "error": "missing_case_folder_for_closing",
                "action": act,
                "identity": identity,
            }

        override_basis_files = [
            str(p).strip()
            for p in (
                fields.get("closing_basis_files")
                or fields.get("basis_files")
                or []
            )
            if str(p).strip()
        ]

        if override_basis_files:
            case_root = os.path.realpath(case_folder)
            normalized_override_files = []
            invalid_override_files = []
            for src in override_basis_files:
                resolved = os.path.realpath(src)
                normalized_override_files.append(resolved)
                try:
                    if os.path.commonpath([case_root, resolved]) != case_root:
                        invalid_override_files.append(src)
                except Exception:
                    invalid_override_files.append(src)
            if invalid_override_files:
                return {
                    "ok": False,
                    "error": "closing_basis_outside_case_folder",
                    "action": act,
                    "identity": identity,
                    "invalid_files": invalid_override_files,
                    "case_folder": case_folder,
                }
            override_basis_files = normalized_override_files

        basis_files = override_basis_files or list(docs.get("closing_basis_files") or [])
        receipt_files = list(docs.get("office_receipt_files") or [])
        missing = []
        if not basis_files:
            missing.append("結案依據文件（起訴書/判決/裁定/不起訴處分書/確定證明書）")
        if not receipt_files:
            missing.append("事務所收文章證據（法院章/郵局回執）")
        if missing:
            return {
                "ok": False,
                "error": "missing_required_docs",
                "action": act,
                "identity": identity,
                "missing": missing,
                "docs": docs,
            }

        counts = self._gather_case_counts(osc_no or close_case_no, cname)
        # Use override or doc-scan basis files for metadata extraction
        _meta_basis = override_basis_files or basis_files
        if _meta_basis:
            _meta_basis = self._sort_closing_basis_files(_meta_basis)
            closing_meta = self._infer_closing_metadata_from_docs(
                _meta_basis, client_name=cname, folder_path=str(case_folder or "")
            )
            for key in (
                "court_kind",
                "court_name",
                "court_case_year",
                "court_case_code",
                "court_case_no",
                "closing_result",
                "closing_result_doc",
                "closing_doc_type",
                "judg_eff",
                "sentence_term",
                "reprieve_term",
            ):
                if str(closing_meta.get(key) or "").strip():
                    counts[key] = closing_meta.get(key)
            for src in _meta_basis:
                dt = self._extract_best_date_from_doc(src, is_poa=False)
                if dt:
                    counts["judg_dt"] = dt
                    break

        # 如果文件名沒有案號，從 DB 的 court_case_number 補充
        if not str(counts.get("court_case_year") or "").strip():
            try:
                _db_case = None
                if self.db and (osc_no or close_case_no):
                    _q = osc_no or close_case_no
                    _db_case = self.db.fetch_one(
                        "SELECT court_case_number, court_name FROM cases WHERE case_number = %s OR legal_aid_number = %s LIMIT 1",
                        (_q, _q)
                    )
                if _db_case:
                    _ccn = str((_db_case.get("court_case_number") if isinstance(_db_case, dict) else (_db_case[0] if _db_case else "")) or "").strip()
                    if _ccn:
                        import re as _re
                        _m = _re.search(r"(\d{2,4})年(?:度)?([^\s第號（）()]{1,16})字第0*(\d+)號", _ccn)
                        if _m:
                            _yr, _cd, _no = _m.groups()
                            if len(_yr) == 4 and _yr.startswith("20"):
                                _yr = str(int(_yr) - 1911)
                            counts.setdefault("court_case_year", _yr)
                            counts.setdefault("court_case_code", _cd)
                            counts.setdefault("court_case_no", _no)
                            logger.info("  📋 DB 補充案號: %s年%s字第%s號", _yr, _cd, _no)
                    # 同時補充法院名稱（如果文件沒抓到）
                    if not str(counts.get("court_name") or "").strip():
                        _cn = str((_db_case.get("court_name") if isinstance(_db_case, dict) else (_db_case[1] if len(_db_case) > 1 else "")) or "").strip()
                        if _cn:
                            counts["court_name"] = _cn
            except Exception as _e:
                logger.debug("DB case number fallback failed: %s", _e)

        for key in (
            "court_kind",
            "court_name",
            "court_case_year",
            "court_case_code",
            "court_case_no",
            "judg_dt",
            "closing_result",
            "closing_result_doc",
            "closing_doc_type",
            "judg_eff",
            "sentence_term",
            "reprieve_term",
        ):
            if str(fields.get(key) or "").strip():
                counts[key] = fields.get(key)

        # 偵測調解/和解成立：案件資料夾內有調解筆錄、和解筆錄等
        has_mediation_success = bool(docs.get("mediation_success_files"))
        counts["has_mediation_success"] = has_mediation_success
        # 調解連繫次數：有成功時預設等於 court_count（調解庭次數），至少 1
        if has_mediation_success:
            _court = int(counts.get("court_count") or 0)
            counts["mediation_contact_count"] = max(1, _court)

        # 結案類型 (casekd) 級聯選單路徑推算
        # Portal 使用 casekd → level1 → level2 → ... 的級聯下拉選單，
        # 選定後由 setClcate() 組合成 clcate 文字欄位。
        # 這裡根據案件屬性推算文字路徑，由 automation 端透過 AJAX 匹配實際 option value。
        counts["closing_clcate_path"] = self._determine_clcate_path(
            case_info=identity, counts=counts,
            has_mediation=has_mediation_success,
        )

        # document_count 改用實際要上傳的書狀份數（不含判決），
        # 而非 DB 的全部文件數（會包含證據、附件等）
        if upload_bundle.get("pleading_source_files"):
            _pleading_count = len(upload_bundle["pleading_source_files"])
            if _pleading_count > 0:
                counts["document_count"] = _pleading_count
                logger.info("  📋 document_count 以書狀數覆蓋: %d", _pleading_count)

        # 偵查階段判斷：用資料夾路徑
        _folder_for_inv = str(identity.get("case_folder") or identity.get("folder_path") or "").replace("\\", "/")
        _is_inv_case = "-偵查-" in _folder_for_inv

        # fields override: 使用者/呼叫端提供的數值覆蓋自動統計
        _count_overrides = {
            "disc_times": ["meeting_count", "contact_count", "inq_count"],
            "meeting_count": ["meeting_count"],
            "contact_count": ["contact_count"],
            "inq_count": ["inq_count"],
            "court_count": ["court_count"],
            "review_count": ["review_count"],
            "document_count": ["document_count"],
        }
        for fk, targets in _count_overrides.items():
            if fk in fields and fields[fk] is not None:
                val = int(fields[fk])
                if fk == "disc_times":
                    # disc_times 是總和，分配到 meeting_count
                    counts["meeting_count"] = max(val, counts.get("meeting_count", 0))
                else:
                    for t in targets:
                        counts[t] = max(val, counts.get(t, 0))
                logger.info("  🔧 fields override: %s = %d", fk, val)

        _disc_total = (int(counts.get("meeting_count", 0) or 0)
                       + int(counts.get("contact_count", 0) or 0)
                       + int(counts.get("inq_count", 0) or 0))
        low_fields = []
        # 偵查案件的 disc_times=0 屬合理情形，不卡住流程，自動填理由即可
        if _disc_total <= 0 and not _is_inv_case:
            low_fields.append("disc_times")
        if int(counts.get("review_count", 0) or 0) <= 0 and not _is_inv_case:
            low_fields.append("review_count")
        if int(counts.get("court_count", 0) or 0) <= 0 and not _is_inv_case:
            low_fields.append("court_count")
        if int(counts.get("document_count", 0) or 0) <= 0 and not _is_inv_case:
            low_fields.append("document_count")
        if low_fields and (not reason):
            return {
                "ok": False,
                "error": "need_reason_for_low_counts",
                "action": act,
                "identity": identity,
                "counts": counts,
                "low_fields": low_fields,
            }
        zero_reasons = {}
        if reason:
            for key in low_fields:
                    zero_reasons[key] = reason

        # 偵查案件：被豁免的零值欄位仍需填寫 noarrivereason（法扶入口網 checkData 會檢查）
        if _is_inv_case:
            _inv_zero_labels = []
            _label_map_inv = {"disc_times": "研討案情", "review_count": "閱卷", "court_count": "開庭", "document_count": "書狀"}
            for _k, _lbl in _label_map_inv.items():
                _val = _disc_total if _k == "disc_times" else int(counts.get(_k, 0) or 0)
                if _val <= 0:
                    _inv_zero_labels.append(_lbl)
            if _inv_zero_labels:
                _auto_reason = "本案為偵查案件，" + "、".join(_inv_zero_labels) + "次數為零屬正常情形。"
                if not counts.get("noarrivereason"):
                    counts["noarrivereason"] = _auto_reason
                # 同時填入 zero_reasons 供 automation 使用
                for _k in _label_map_inv:
                    _val = _disc_total if _k == "disc_times" else int(counts.get(_k, 0) or 0)
                    if _val <= 0 and _k not in zero_reasons:
                        zero_reasons[_k] = _auto_reason

        ok = self.execute_portal_closing(
            close_case_no,
            counts,
            zero_reasons,
            upload_files=(fields.get("upload_files") or upload_bundle.get("pdf_files") or []),
            client_name=cname,
        )
        if ok:
            # 暫存成功 → 回寫 DB status
            try:
                _upd_case = identity.get("case_number") or ""
                if _upd_case and self.db:
                    self.db.execute(
                        "UPDATE cases SET legal_aid_status = %s, status = %s WHERE case_number = %s",
                        ("已結案，待送出", "已結案，待送出", _upd_case)
                    )
                    logger.info("  📝 DB status 更新: %s → 已結案，待送出", _upd_case)
            except Exception as _db_err:
                logger.warning("  ⚠️ DB status 更新失敗: %s", _db_err)
        return {
            "ok": bool(ok),
            "action": act,
            "identity": identity,
            "fields": fields,
            "counts": counts,
            "zero_reasons": zero_reasons,
            "basis_files": override_basis_files or list(docs.get("closing_basis_files") or []),
            "upload_bundle": upload_bundle,
            "preview": self._last_portal_artifact,
        }

    def execute_portal_action_submit(
        self,
        *,
        action: str,
        laf_case_number: str = "",
        case_number: str = "",
        client_name: str = "",
        reason: str = "",
        fields: Optional[dict] = None,
    ) -> dict:
        """
        Execute explicit submit (currently only go_live).
        """
        act = (action or "").strip().lower()
        if act != "go_live":
            return {"ok": False, "error": "submit_only_supports_go_live", "action": act}

        identity = self._lookup_case_identity(
            laf_case_number=laf_case_number,
            case_number=case_number,
            client_name=client_name,
            action=act,
        )
        if identity.get("needs_manual_confirm"):
            return {
                "ok": False,
                "error": "identity_needs_manual_confirmation",
                "action": act,
                "identity": identity,
            }
        laf_no = (identity.get("laf_case_number") or "").strip()
        cname = (identity.get("client_name") or "").strip()
        case_folder = (identity.get("case_folder") or "").strip()
        docs = self._scan_case_folder_docs(case_folder) if case_folder else self._empty_docs_map()
        if not laf_no and not cname:
            return {"ok": False, "error": "missing_target", "action": act, "identity": identity}
        if not case_folder:
            return {"ok": False, "error": "missing_case_folder", "action": act, "identity": identity}
        # 開辦只認 02_開辦資料 內的文件
        _gl_dir = os.path.join(self._to_local_case_folder(case_folder) or case_folder, "02_開辦資料")
        _gl_docs = self._scan_case_folder_docs(_gl_dir) if os.path.isdir(_gl_dir) else self._empty_docs_map()
        _is_consumer_debt = self._is_consumer_debt_case_folder(case_folder)
        _need_poa = not _is_consumer_debt
        if not _gl_docs["opening_notice_files"] or (_need_poa and not _gl_docs["poa_files"]):
            missing = []
            if not _gl_docs["opening_notice_files"]:
                missing.append("開辦通知書/接案通知書")
            if _need_poa and not _gl_docs["poa_files"]:
                missing.append("委任狀")
            hint = "請將開辦通知書放入 02_開辦資料 資料夾" if _is_consumer_debt else "請將開辦通知與委任狀放入 02_開辦資料 資料夾"
            return {"ok": False, "error": "missing_required_docs", "action": act, "identity": identity, "missing": missing, "hint": hint}

        open_doc = _gl_docs["opening_notice_files"][0]
        poa_doc = _gl_docs["poa_files"][0] if _gl_docs["poa_files"] else ""
        open_date = self._extract_best_date_from_doc(open_doc)
        poa_date = self._extract_best_date_from_doc(poa_doc) if poa_doc else ""
        if not open_date or (_need_poa and not poa_date):
            missing_dates = []
            if not open_date:
                missing_dates.append("開辦通知書日期")
            if _need_poa and not poa_date:
                missing_dates.append("委任狀遞出日期")
            return {"ok": False, "error": "missing_required_dates", "action": act, "identity": identity, "missing": missing_dates}

        fields = dict(fields or {})
        fields.setdefault("sel_result", "1")
        if _is_consumer_debt:
            fields.setdefault("remark", f"已簽署開辦通知書（開辦日期 {open_date}）。")
        else:
            fields.setdefault(
                "remark",
                f"CASPER 開辦資料判讀：開辦日期 {open_date}；委任狀遞出日期 {poa_date}。",
            )
        # 找出要上傳的檔案
        go_live_upload = self._find_go_live_upload_files(case_folder, is_consumer_debt=_is_consumer_debt)
        if go_live_upload:
            fields.setdefault("upload_files", go_live_upload)
        ok = self.execute_portal_go_live_submit(laf_no, cname, fields)
        return {
            "ok": bool(ok),
            "action": act,
            "identity": identity,
            "fields": fields,
            "dates": {"opening_date": open_date, "poa_submit_date": poa_date},
            "docs": {"opening_notice": open_doc, "poa": poa_doc},
            "preview": self._last_portal_artifact,
        }

    # ==================================================================
    # Condition (WF5) auto-trigger for tick/nightly
    # ==================================================================

    # 法扶常見異體字對照表 —— 用於姓名比對正規化
    _VARIANT_MAP: dict[str, str] = {
        "裡": "里", "閒": "閑", "峯": "峰", "鏈": "鍊",
        "歎": "嘆", "啟": "啓", "爲": "為", "衆": "眾",
        "鑒": "鑑", "攷": "考", "卻": "却", "薦": "荐",
        "勳": "勛", "餵": "喂", "傑": "杰", "匯": "汇",
        "鏽": "銹", "繡": "綉",
    }

    @staticmethod
    def _norm_token(v: str) -> str:
        s = re.sub(r"[\s\u3000·・•‧∙．｡。]+", "", str(v or "").strip()).lower()
        for orig, repl in LAFOrchestrator._VARIANT_MAP.items():
            s = s.replace(orig, repl)
        return s

    def _load_condition_manual_done(self) -> dict:
        try:
            if CONDITION_MANUAL_DONE_PATH.exists():
                data = json.loads(CONDITION_MANUAL_DONE_PATH.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    return data
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4093, exc_info=True)
        return {"by_laf": {}, "by_osc": {}, "by_client": {}}

    def _save_condition_manual_done(self, data: dict) -> None:
        try:
            CONDITION_MANUAL_DONE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONDITION_MANUAL_DONE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data or {}, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(CONDITION_MANUAL_DONE_PATH)
        except Exception as e:
            logger.warning("Save condition manual-done registry failed: %s", e)

    def _condition_marker_paths(self, case_folder: str) -> List[Path]:
        root = Path((case_folder or "").strip())
        if not root:
            return []
        return [
            root / "01_法扶資料" / ".magi_condition_reported.done",
            root / ".magi_condition_reported.done",
        ]

    def _is_condition_manual_done(
        self,
        *,
        laf_case_number: str = "",
        osc_case_number: str = "",
        client_name: str = "",
        case_folder: str = "",
    ) -> bool:
        reg = self._load_condition_manual_done()
        laf = self._norm_token(laf_case_number)
        osc = self._norm_token(osc_case_number)
        cli = self._norm_token(client_name)
        try:
            if laf and laf in {self._norm_token(k) for k in (reg.get("by_laf") or {}).keys()}:
                return True
            if osc and osc in {self._norm_token(k) for k in (reg.get("by_osc") or {}).keys()}:
                return True
            if cli and cli in {self._norm_token(k) for k in (reg.get("by_client") or {}).keys()}:
                return True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4134, exc_info=True)
        for mp in self._condition_marker_paths(case_folder):
            try:
                if mp.exists():
                    return True
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4140, exc_info=True)
        return False

    def mark_condition_manual_done(
        self,
        *,
        laf_case_number: str = "",
        osc_case_number: str = "",
        client_name: str = "",
        case_folder: str = "",
        reason: str = "manual_reported_by_lawyer",
    ) -> dict:
        laf = (laf_case_number or "").strip()
        osc = (osc_case_number or "").strip()
        cli = (client_name or "").strip()
        folder = (case_folder or "").strip()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reg = self._load_condition_manual_done()
        reg.setdefault("by_laf", {})
        reg.setdefault("by_osc", {})
        reg.setdefault("by_client", {})
        payload = {
            "laf_case_number": laf,
            "osc_case_number": osc,
            "client_name": cli,
            "case_folder": folder,
            "reason": reason,
            "updated_at": now,
        }
        if laf:
            reg["by_laf"][laf] = payload
        if osc:
            reg["by_osc"][osc] = payload
        if cli:
            reg["by_client"][cli] = payload
        self._save_condition_manual_done(reg)

        marker_written = []
        for mp in self._condition_marker_paths(folder):
            try:
                mp.parent.mkdir(parents=True, exist_ok=True)
                mp.write_text(
                    json.dumps(
                        {
                            "manual_done": True,
                            "reason": reason,
                            "updated_at": now,
                            "laf_case_number": laf,
                            "osc_case_number": osc,
                            "client_name": cli,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                marker_written.append(str(mp))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4198, exc_info=True)

        key = (osc or laf or cli)
        if key:
            self._log_event(
                key,
                "condition_manual_done",
                {
                    "laf_case_number": laf,
                    "osc_case_number": osc,
                    "client_name": cli,
                    "reason": reason,
                    "marker_files": marker_written,
                },
                "manual_done",
            )
        _eventlog(
            "laf:condition:manual_done",
            ok=True,
            payload={
                "laf_case_number": laf,
                "osc_case_number": osc,
                "client_name": cli,
                "reason": reason,
            },
            tags={"laf_case_no": laf, "client_name": cli},
        )
        return {"ok": True, "payload": payload, "marker_files": marker_written}

    def _to_local_case_folder(self, path_value: str) -> str:
        p = (path_value or "").strip()
        if not p:
            return ""
        try:
            if self.db and hasattr(self.db, "translate_path_to_local"):
                p2 = self.db.translate_path_to_local(p)
                if p2:
                    p = str(p2)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4237, exc_info=True)
        return translate_case_path_to_local(p)

    def _collect_condition_trigger_files(self, case_folder: str) -> List[str]:
        """
        WF5（二階段）啟動條件文件：
        - 案件資料夾內有「調解不成立證明書/調解不成立」
        - 且位於「法院通知或程序裁定」對應子資料夾
        """
        root = (case_folder or "").strip()
        if not root or not os.path.isdir(root):
            return []
        try:
            docs = self._scan_case_folder_docs(root)
            out: List[str] = []
            for p in (docs.get("mediation_failure_files") or []):
                norm = str(p).replace("\\", "/")
                if "法院通知或程序裁定" in norm:
                    out.append(str(p))
            return self._dedupe_sorted(out)
        except Exception:
            return []

    def _has_condition_trigger_file(self, case_folder: str) -> bool:
        return bool(self._collect_condition_trigger_files(case_folder))

    def _has_phase2_receipt_file(self, case_folder: str) -> bool:
        """
        Backward-compat alias (舊函式名保留，語意已改為調解不成立觸發)。
        """
        return self._has_condition_trigger_file(case_folder)

    def _was_condition_drafted_recently(self, case_number: str, days: int = 30) -> bool:
        if not self.db or not case_number:
            return False
        try:
            q = (
                "SELECT COUNT(*) AS cnt FROM `laf_lifecycle_log` "
                "WHERE `case_number` = %s "
                "AND ("
                "(`event_type` = 'condition' AND `status` IN ('draft','success')) "
                "OR (`event_type` = 'condition_manual_done' AND `status` IN ('manual_done','success'))"
                ") "
                "AND `created_at` >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            )
            row = self.db.fetch_one(q, (case_number, int(days)), as_dict=True)
            if isinstance(row, dict):
                return int(row.get("cnt") or 0) > 0
            if isinstance(row, (tuple, list)) and row:
                return int(row[0] or 0) > 0
        except Exception:
            return False
        return False

    def _get_pending_condition_cases(self, max_cases: int = 3) -> List[dict]:
        if not self.db:
            return []
        order_expr = "id DESC"
        try:
            cols = self.db.fetch_all("SHOW COLUMNS FROM `cases`", as_dict=True) or []
            colset = {str((c or {}).get("Field") or "").strip().lower() for c in cols if isinstance(c, dict)}
            if "updated_at" in colset and "created_date" in colset:
                order_expr = "COALESCE(`updated_at`, `created_date`) DESC, `id` DESC"
            elif "updated_at" in colset:
                order_expr = "`updated_at` DESC, `id` DESC"
            elif "created_date" in colset:
                order_expr = "`created_date` DESC, `id` DESC"
            elif "updated_date" in colset and "created_date" in colset:
                order_expr = "COALESCE(`updated_date`, `created_date`) DESC, `id` DESC"
            elif "updated_date" in colset:
                order_expr = "`updated_date` DESC, `id` DESC"
        except Exception:
            order_expr = "id DESC"
        try:
            q = f"""
                SELECT `case_number`, `client_name`, `legal_aid_number`, `folder_path`, `status`
                FROM `cases`
                WHERE `case_category` = '法律扶助案件'
                  AND (`legal_aid_number` IS NOT NULL AND TRIM(`legal_aid_number`) <> '')
                  AND (
                      `status` IS NULL OR TRIM(`status`) = ''
                      OR LOWER(TRIM(`status`)) IN ('active','open','pending','processing','in_progress')
                      OR TRIM(`status`) IN ('進行中','處理中','辦理中','審理中','待處理')
                  )
                ORDER BY {order_expr}
                LIMIT 180
            """
            rows = self.db.fetch_all(q, as_dict=True) or []
        except Exception as e:
            logger.warning("Query pending condition cases failed: %s", e)
            return []

        out: List[dict] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            case_number = (r.get("legal_aid_number") or "").strip()
            client_name = (r.get("client_name") or "").strip()
            osc_case_no = (r.get("case_number") or "").strip()
            folder = self._to_local_case_folder(r.get("folder_path") or "")
            if not case_number or not folder:
                continue
            if not self._has_condition_trigger_file(folder):
                continue
            if self._is_condition_manual_done(
                laf_case_number=case_number,
                osc_case_number=osc_case_no,
                client_name=client_name,
                case_folder=folder,
            ):
                continue
            # Avoid re-saving draft too frequently.
            # NOTE:
            # lifecycle log "condition" events are written with LAF case number.
            # Keep backward compatibility by checking both LAF case no and OSC case no.
            if case_number and self._was_condition_drafted_recently(case_number, days=30):
                continue
            if osc_case_no and self._was_condition_drafted_recently(osc_case_no, days=30):
                continue
            out.append(
                {
                    "osc_case_number": osc_case_no,
                    "laf_case_number": case_number,
                    "client_name": client_name,
                    "folder_path": folder,
                }
            )
            if len(out) >= int(max_cases):
                break
        return out

    def run_condition_drafts(self, max_cases: int = 3) -> dict:
        """
        自動尋找「調解不成立證明書」已到位案件並執行 WF5 暫存。
        僅暫存，不送出。
        """
        candidates = self._get_pending_condition_cases(max_cases=max_cases)
        if not candidates:
            return {"ok": True, "scanned": 0, "processed": 0, "items": [], "message": "no_pending_condition_cases"}

        results = []
        ok_count = 0
        for c in candidates:
            laf_case_no = c.get("laf_case_number", "")
            client_name = c.get("client_name", "")
            case_folder = (c.get("folder_path") or "").strip()
            selected = self._collect_condition_trigger_files(case_folder)[:1] if case_folder else []
            upload_bundle = self._collect_selected_upload_pdfs(
                selected,
                laf_case_no=laf_case_no,
                action="condition",
                label="mediation_failure",
            ) if selected else {}
            ok = self.execute_portal_condition_draft(
                case_number=laf_case_no,
                client_name=client_name,
                fields={
                    "at_ctype": "附條件審查",
                    "conditionrsn": "依調解不成立證明書，先行暫存供律師確認",
                    "upload_files": (upload_bundle.get("pdf_files") or []),
                    "upload_mode": "replace",
                },
            )
            results.append(
                {
                    "ok": bool(ok),
                    "laf_case_number": laf_case_no,
                    "osc_case_number": c.get("osc_case_number", ""),
                    "client_name": client_name,
                    "upload_files": len((upload_bundle.get("pdf_files") or [])),
                }
            )
            if ok:
                ok_count += 1
        return {
            "ok": ok_count == len(candidates),
            "scanned": len(candidates),
            "processed": ok_count,
            "items": results,
        }

    # ==================================================================
    # Auto Closing Draft (報結自動暫存)
    # ==================================================================

    def _was_closing_drafted_recently(self, case_number: str, days: int = 30) -> bool:
        """Check if a closing draft was already saved for this case recently."""
        if not self.db or not case_number:
            return False
        try:
            q = (
                "SELECT COUNT(*) AS cnt FROM `laf_lifecycle_log` "
                "WHERE `case_number` = %s "
                "AND `event_type` = 'closing' "
                "AND `status` IN ('draft','success','pending') "
                "AND `created_at` >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            )
            row = self.db.fetch_one(q, (case_number, int(days)), as_dict=True)
            if isinstance(row, dict):
                return int(row.get("cnt") or 0) > 0
            if isinstance(row, (tuple, list)) and row:
                return int(row[0] or 0) > 0
        except Exception:
            return False
        return False

    def _get_pending_closing_draft_cases(self, max_cases: int = 3) -> List[dict]:
        """
        Find LAF cases ready for auto closing draft:
        - legal_aid_status in (進行中, 已開辦, 待報結, 已結案，待報結)
        - 10_判決書 has files
        - Not already drafted recently
        """
        if not self.db:
            return []
        try:
            q = """
                SELECT `case_number`, `client_name`, `legal_aid_number`,
                       `folder_path`, `case_reason`
                FROM `cases`
                WHERE `case_category` = '法律扶助案件'
                  AND (`legal_aid_number` IS NOT NULL AND TRIM(`legal_aid_number`) <> '')
                  AND TRIM(COALESCE(`legal_aid_status`, '')) IN ('進行中', '已開辦', '待報結', '已結案，待報結')
                ORDER BY `id` DESC
                LIMIT 200
            """
            rows = self.db.fetch_all(q, as_dict=True) or []
        except Exception as e:
            logger.warning("Query pending closing draft cases failed: %s", e)
            return []

        out: List[dict] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            laf_no = (r.get("legal_aid_number") or "").strip()
            osc_no = (r.get("case_number") or "").strip()
            client = (r.get("client_name") or "").strip()
            folder = self._to_local_case_folder(r.get("folder_path") or "")
            if not laf_no or not folder or not os.path.isdir(folder):
                continue
            # Must have judgment files
            judg_dir = os.path.join(folder, "10_判決書")
            if not os.path.isdir(judg_dir):
                continue
            try:
                has_file = any(not fn.startswith(".") for fn in os.listdir(judg_dir))
            except OSError:
                continue
            if not has_file:
                continue
            # Dedup
            if self._was_closing_drafted_recently(laf_no, days=30):
                continue
            if osc_no and self._was_closing_drafted_recently(osc_no, days=30):
                continue
            # Collect judgment PDFs as closing_basis_files
            basis = []
            allowed_ext = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
            try:
                judg_files = sorted(os.listdir(judg_dir))
            except OSError:
                continue
            for fn in judg_files:
                if fn.startswith("."):
                    continue
                if Path(fn).suffix.lower() in allowed_ext:
                    basis.append(os.path.join(judg_dir, fn))
            out.append({
                "osc_case_number": osc_no,
                "laf_case_number": laf_no,
                "client_name": client,
                "folder_path": folder,
                "case_reason": (r.get("case_reason") or "").strip(),
                "closing_basis_files": basis,
            })
            if len(out) >= int(max_cases):
                break
        return out

    def run_closing_drafts(self, max_cases: int = 3) -> dict:
        """
        自動找「10_判決書已到位」的進行中法扶案件，
        呼叫既有 execute_portal_action_draft(action=closing) 暫存。
        僅暫存，不送出。
        """
        candidates = self._get_pending_closing_draft_cases(max_cases=max_cases)
        if not candidates:
            return {"ok": True, "scanned": 0, "processed": 0, "items": [],
                    "message": "no_pending_closing_draft_cases"}

        results = []
        ok_count = 0
        for c in candidates:
            laf_no = c["laf_case_number"]
            osc_no = c.get("osc_case_number", "")
            client = c.get("client_name", "")
            basis = c.get("closing_basis_files", [])

            display = f"{client}（{laf_no}）" if client else laf_no
            logger.info("📋 Auto closing draft: %s", display)

            r = self.execute_portal_action_draft(
                action="closing",
                laf_case_number=laf_no,
                case_number=osc_no,
                client_name=client,
                reason="",  # 留空：若有 0 次數欄位，會走 need_reason_for_low_counts 通知流程
                fields={"closing_basis_files": basis},
            )
            ok = bool(r.get("ok"))
            results.append({
                "ok": ok,
                "laf_case_number": laf_no,
                "osc_case_number": osc_no,
                "client_name": client,
                "error": r.get("error", ""),
            })
            if ok:
                ok_count += 1

        return {
            "ok": ok_count == len(candidates),
            "scanned": len(candidates),
            "processed": ok_count,
            "items": results,
        }

    def run_condition_mark_by_mediation(self, max_scan: int = 8000) -> dict:
        """
        全量掃描案件資料夾；若偵測到「調解不成立證明書 / 調解不成立」，
        即寫入 condition manual-done 標記，避免再重複觸發二階段暫存。
        """
        if not self.db:
            return {"ok": False, "error": "db_unavailable", "scanned": 0, "matched": 0, "marked": 0, "already_done": 0}

        scanned = 0
        matched = 0
        marked = 0
        already_done = 0
        missing_folder = 0
        errors: List[dict] = []
        items: List[dict] = []

        try:
            q = (
                "SELECT `case_number`, `client_name`, `legal_aid_number`, `folder_path`, `case_category` "
                "FROM `cases` "
                "WHERE `folder_path` IS NOT NULL AND TRIM(`folder_path`) <> '' "
                "ORDER BY `id` DESC LIMIT %s"
            )
            rows = self.db.fetch_all(q, (int(max_scan),), as_dict=True) or []
        except Exception as e:
            return {"ok": False, "error": f"query_failed:{e}", "scanned": 0, "matched": 0, "marked": 0, "already_done": 0}

        for r in rows:
            if not isinstance(r, dict):
                continue
            scanned += 1
            try:
                osc_no = str(r.get("case_number") or "").strip()
                laf_no = str(r.get("legal_aid_number") or "").strip()
                cname = str(r.get("client_name") or "").strip()
                folder = self._to_local_case_folder(str(r.get("folder_path") or "").strip())
                if (not folder) or (not os.path.isdir(folder)):
                    missing_folder += 1
                    continue

                docs = self._scan_case_folder_docs(folder)
                med_files = list(docs.get("mediation_failure_files") or [])
                if not med_files:
                    continue

                matched += 1
                if self._is_condition_manual_done(
                    laf_case_number=laf_no,
                    osc_case_number=osc_no,
                    client_name=cname,
                    case_folder=folder,
                ):
                    already_done += 1
                    continue

                out = self.mark_condition_manual_done(
                    laf_case_number=laf_no,
                    osc_case_number=osc_no,
                    client_name=cname,
                    case_folder=folder,
                    reason="auto_detected_mediation_failure_doc",
                )
                if out.get("ok"):
                    marked += 1
                items.append(
                    {
                        "osc_case_number": osc_no,
                        "laf_case_number": laf_no,
                        "client_name": cname,
                        "case_folder": folder,
                        "mediation_failure_files": med_files[:3],
                        "marker_files": list(out.get("marker_files") or []),
                    }
                )
            except Exception as e:
                errors.append(
                    {
                        "case_number": str((r or {}).get("case_number") or ""),
                        "legal_aid_number": str((r or {}).get("legal_aid_number") or ""),
                        "error": str(e),
                    }
                )

        result = {
            "ok": True,
            "scanned": scanned,
            "matched": matched,
            "marked": marked,
            "already_done": already_done,
            "missing_folder": missing_folder,
            "errors": errors[:30],
            "items": items[:200],
        }
        _eventlog(
            "laf:condition:auto_mark_by_mediation",
            ok=True,
            payload={
                "scanned": scanned,
                "matched": matched,
                "marked": marked,
                "already_done": already_done,
                "missing_folder": missing_folder,
                "errors": len(errors),
            },
            tags={},
        )
        return result

    def execute_final_submit(self, case_number: str):
        """
        Final submit after admin confirms the saved draft.
        Uses doFinalSave('toCR') on the portal.
        """
        if os.environ.get("MAGI_LAF_DRAFT_ONLY", "1") == "1":
            logger.info("🔒 Draft-only policy blocks final submit for %s", case_number)
            try:
                self.notifier.notify_admin(f"🔒 安全政策：目前僅暫存，不允許『送出』— {case_number}")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4683, exc_info=True)
            self._log_event(case_number, "closing", {"blocked": "draft_only"}, "blocked")
            return False

        logger.info("📤 Final submit for %s (authorized by admin)", case_number)

        if self.dry_run:
            logger.info("  [DRY RUN] Would execute doFinalSave")
            return True

        try:
            from laf_automation_v2 import LAFWebAutomation

            username = os.environ.get("MAGI_LAF_USERNAME") or self.laf_config.get("username", "")
            password = os.environ.get("MAGI_LAF_PASSWORD") or self.laf_config.get("password", "")
            download_folder = self.laf_config.get("download_folder", "./laf_downloads")
            headless = bool(self.laf_config.get("headless", True))
            base_url = (self.laf_config.get("base_url", "") or "").strip()
            browser_profile_dir = self.laf_config.get("browser_profile_dir", "")

            if not username or not password:
                raise RuntimeError("LAF credentials not configured (laf.username / laf.password)")

            automation = self._get_automation()
            if not automation.login():
                raise RuntimeError("LAF login failed")

            ok = automation.final_submit_closing_report(laf_case_number=case_number)
            if not ok:
                raise RuntimeError("portal final submit failed")
        except Exception as e:
            try:
                self.notifier.notify_admin(f"❌ 報結送出失敗 — {case_number}\n原因：{e}")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4717, exc_info=True)
            self._log_event(case_number, "closing", {"error": str(e)}, "error")
            return False
        self._log_event(case_number, "closing", {
            "portal_status": "final_submitted",
        }, "completed")

        self.notifier.notify_admin(f"✅ 已為您送出報結 — {case_number}")
        return True

    # ==================================================================
    # DB Query Helpers
    # ==================================================================

    def _get_pending_closing_cases(self) -> list:
        """Get cases with status '已結案' that need closing reports."""
        if not self.db:
            logger.error("DB not available")
            return []

        target_status = self.laf_config.get("closing_target_status", "已結案")

        query = """
            SELECT `case_number`, `client_name`, `folder_path`,
                   `case_type`, `case_reason`, `legal_aid_number`
            FROM `cases`
            WHERE `case_category` = '法律扶助案件'
              AND `status` = %s
            ORDER BY `case_number` DESC
        """
        try:
            return self.db.fetch_all(query, (target_status,))
        except Exception as e:
            logger.error("Query failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # 結案類型 (casekd) 級聯選單路徑推算
    # ------------------------------------------------------------------
    def _determine_clcate_path(self, case_info: dict, counts: dict,
                               has_mediation: bool = False) -> list:
        """
        根據案件屬性推算法扶 Portal 結案類型的級聯選單路徑。

        Portal 使用 casekd → level1 → level2 → ... 級聯 select，每層選項由
        AJAX /lafcsp/getPLS12ByFnode 動態載入。這裡回傳文字標籤陣列，由
        automation 端透過 AJAX 匹配實際 option value 並設值。

        Returns:
            list of str — 每個元素對應一層 select 的文字標籤
            例如: ["扶助種類為訴訟代理或辯護", "民/家事案件", "消債事件程序", "更生程序"]
        """
        path = []
        _case_code = str(counts.get("court_case_code") or "").strip()
        _closing_result = str(counts.get("closing_result") or "").strip()
        _doc_type = str(counts.get("closing_doc_type") or "").strip()
        _case_reason = str(case_info.get("case_reason") or counts.get("case_reason") or "").strip()

        # --- Level 0: casekd ---
        # 依扶助種類判斷
        _aid_type = str(case_info.get("aid_type") or "").strip()
        if any(k in _aid_type for k in ("調解", "和解")):
            path.append("扶助種類為調解或和解")
            return path  # 調解/和解通常只有一層
        elif "法律文件" in _aid_type:
            path.append("扶助種類為法律文件之撰擬")
            return path
        elif "法律諮詢" in _aid_type or "研究性" in _aid_type:
            path.append("研究性法律諮詢")
            return path
        else:
            path.append("扶助種類為訴訟代理或辯護")

        # --- Level 1: 案件類型 ---
        # 資料夾路徑格式：.../法扶案件/{刑事|民事|消費者債務清理|行政|非訟}/...
        _folder = str(case_info.get("folder_path") or "").replace("\\", "/")
        _is_criminal = "/刑事/" in _folder
        _is_debt = "/消費者債務清理/" in _folder or any(k in _case_code for k in ("消債更", "消債清", "消債調", "消債抗"))
        _is_admin = "/行政/" in _folder

        # Portal 實際選項文字（from clcate_hierarchy.json）
        # 0678: "民/家/勞案件"  0006: "刑事案件"  0007: "行政案件"
        _is_constitutional = "憲法" in _case_code or "審裁" in _case_code or "憲法" in str(counts.get("court_name") or "")
        if _is_constitutional:
            path.append("法院裁定")
            path.append("憲法訴訟程序")
            return path
        elif _is_admin:
            path.append("行政案件")
        elif _is_debt or "消費者債務清理" in _case_reason:
            path.append("民/家/勞案件")
        elif _is_criminal:
            path.append("刑事案件")
        else:
            # 民事/非訟 → 民/家/勞案件
            path.append("民/家/勞案件")

        # --- Level 2+: 依案件類型細分 ---
        # ── 消債事件程序 (0345) ──
        if _is_debt or "消費者債務清理" in _case_reason:
            path.append("消債事件程序")

            if "消債更" in _case_code or "更生" in _case_reason:
                # 0348: 更生程序終結確定
                path.append("更生程序終結確定")
                if "認可" in _closing_result and "不" not in _closing_result.split("認可")[0][-2:]:
                    path.append("更生方案經法院裁定認可確定")  # 0351
                elif "不認可" in _closing_result or ("不被" in _closing_result and "認可" in _closing_result):
                    path.append("更生方案不被法院認可")  # 0352
                elif "駁回" in _closing_result:
                    # 已在更生分支內，「駁回」即為駁回更生聲請
                    path.append("法院駁回更生聲請確定")  # 0353

            elif "消債清" in _case_code or "清算" in _case_reason:
                # 0349: 清算程序終結確定
                path.append("清算程序終結確定")
                if "駁回" in _closing_result:
                    # 已在清算分支內，「駁回」即為駁回清算聲請
                    path.append("法院駁回清算聲請確定")  # 0355
                elif "終止" in _closing_result or "終結" in _closing_result:
                    # 0354: 經法院裁定終止或終結確定 → 再看免責/不免責
                    path.append("經法院裁定終止或終結確定")
                    if "不免責" in _closing_result:
                        path.append("不免責裁定")  # 0357
                    elif "免責" in _closing_result:
                        path.append("免責裁定")  # 0356 → 再看復權
                        if "復權" in _closing_result:
                            path.append("取得復權裁定")  # 0358
                        # else: 未取得復權裁定 (0359) 或留待手動

            elif "消債調" in _case_code or "協商" in _case_reason:
                if "協商" in _closing_result and "成立" in _closing_result:
                    path.append("協商成立")      # 0346
                    path.append("協商文件")      # 0719
                elif has_mediation or "調解" in _closing_result:
                    path.append("調解成立")      # 0347
                    path.append("調解文件")      # 0720

            elif "撤回" in _closing_result:
                path.append("向銀行或法院撤回申（聲）請")  # 0350
                path.append("撤回文件")                    # 0683

        # ── 刑事案件 (0006) ──
        elif _is_criminal or "刑事" in (path[1] if len(path) > 1 else ""):
            # 判斷偵查程序 vs 審判程序
            _folder_for_stage = str(case_info.get("folder_path") or case_info.get("case_folder") or "").replace("\\", "/")
            _is_investigation = (
                "-偵查-" in _folder_for_stage
                or _case_code in ("偵", "他", "相", "軍偵", "少偵")
                or "偵" in _case_code
            )
            # 結案依據檔案名稱也可判斷
            _basis_files = [os.path.basename(str(f)) for f in (counts.get("basis_files") or case_info.get("basis_files") or [])]
            _basis_text = " ".join(_basis_files).lower()
            _has_prosecution = any(k in _basis_text for k in ("起訴書", "起訴處分"))
            _has_indictment = "聲請簡易判決處刑" in _basis_text

            if _is_investigation:
                # ── 偵查程序 ──
                path.append("偵查程序")
                if "不起訴" in _closing_result or "不起訴" in _basis_text:
                    if "和解" in _closing_result or "撤回告訴" in _closing_result:
                        path.append("不起訴(和解或撤回告訴)")
                    else:
                        path.append("不起訴(犯罪嫌疑不足或其他)")
                    path.append("處分書")
                elif "緩起訴" in _closing_result or "緩起訴" in _basis_text:
                    path.append("緩起訴")
                    path.append("處分書")
                elif "簽結" in _closing_result:
                    path.append("簽結")
                    path.append("公文")
                elif _has_indictment or "簡易判決處刑" in _closing_result:
                    path.append("起訴")
                    path.append("聲請簡易判決處刑書")
                elif _has_prosecution or "起訴" in _closing_result or "起訴書" in _basis_text:
                    # 一般起訴（含依國民法官法起訴）
                    path.append("起訴")
                    path.append("處分書")
                else:
                    # 預設：偵查案件結案通常是起訴或不起訴
                    path.append("起訴")
                    path.append("處分書")
            else:
                # ── 審判程序 ──
                path.append("審判程序")
                if "無罪" in _closing_result:
                    path.append("無罪判決")
                    path.append("判決")
                elif "免訴" in _closing_result:
                    path.append("免訴判決")
                    path.append("判決")
                elif "免刑" in _closing_result:
                    path.append("免刑判決")
                    path.append("判決")
                elif "不受理" in _closing_result:
                    if "和解" in _closing_result or "撤回告訴" in _closing_result:
                        path.append("不受理判決(因和解或撤回告訴所致)")
                    else:
                        path.append("不受理判決(公訴)")
                    path.append("判決")
                elif "撤回" in _closing_result:
                    path.append("撤回自訴或公訴")
                    path.append("撤回書狀")
                elif "有期徒刑" in _closing_result or "拘役" in _closing_result or "罰金" in _closing_result:
                    path.append("科刑判決")
                    path.append("判決")
                elif _doc_type == "判決" or "判決" in _basis_text:
                    path.append("科刑判決")
                    path.append("判決")
                else:
                    path.append("其他")
                    path.append("判決")

        # ── 民/家/勞案件 — 非消債 (0005: 訴訟/非訟程序) ──
        else:
            # 所有民事/家事/勞動案件（改定子女、離婚、損害賠償等）
            # Portal 結構: 民/家/勞案件 → 訴訟/非訟程序 → 決定/調解或和解/撤回
            path.append("訴訟/非訟程序")  # 0005

            if has_mediation or "調解" in _closing_result or "和解" in _closing_result:
                path.append("調解或和解")           # 0020
                path.append("筆錄或其他和解文件")    # 0368 (terminal)
            elif "撤回" in _closing_result:
                path.append("撤回")                 # 0021
                path.append("撤回狀或筆錄等")       # 0046 (terminal)
            elif _doc_type in ("判決", "裁定") or "判決" in _closing_result or "裁定" in _closing_result:
                path.append("決定(非法律用語)")      # 0019
                # 依判決結果細分
                if "全部勝訴" in _closing_result or ("有利" in str(counts.get("judg_eff") or "") and "駁回" not in _closing_result):
                    path.append("全部勝訴判決(裁定)")  # 0145
                elif "全部敗訴" in _closing_result or "駁回" in _closing_result:
                    if "上訴" in _closing_result or "抗告" in _closing_result:
                        path.append("駁回上訴(抗告)判決(裁定)")  # 0360
                    else:
                        path.append("全部敗訴判決(裁定)")  # 0170
                elif "發回" in _closing_result:
                    path.append("廢棄原判決(裁定)，發回原審法院")  # 0226
                elif "勝敗互見" in _closing_result or ("一部" in _closing_result and ("准" in _closing_result or "駁" in _closing_result)):
                    path.append("勝敗互見判決(裁定)")  # 0171

        logger.info("  📋 Clcate path: %s", " → ".join(path))
        return path

    def _gather_case_counts(self, case_number: str, client_name: str = "") -> dict:
        """
        Gather all counts needed for a closing report.

        Queries:
            - meetings table: meeting count
            - case_todos table: contact + court date counts
            - document_index table: document count
        """
        counts = {
            "meeting_count": 0,
            "contact_count": 0,
            "inq_count": 0,
            "court_count": 0,
            "document_count": 0,
        }

        if not self.db:
            return counts

        # 日期清單（供報結頁面用 doAdd*Dt() 新增日期列）
        counts["court_dates"] = []
        counts["review_dates"] = []

        try:
            # 1. Meeting count (from meetings table)
            result = self.db.fetch_one(
                "SELECT COUNT(*) as cnt FROM `meetings` WHERE `case_number` = %s",
                (case_number,)
            )
            if result:
                counts["meeting_count"] = result[0] if isinstance(result, tuple) else result.get("cnt", 0)

            # 2. Contact count (from case_todos — type like '聯繫' or '通話')
            result = self.db.fetch_one(
                """SELECT COUNT(*) as cnt FROM `case_todos`
                   WHERE `case_number` = %s
                   AND (`todo_type` LIKE '%%聯繫%%' OR `todo_type` LIKE '%%通話%%'
                        OR `todo_type` LIKE '%%接見%%' OR `todo_type` LIKE '%%會面%%')""",
                (case_number,)
            )
            if result:
                counts["contact_count"] = result[0] if isinstance(result, tuple) else result.get("cnt", 0)

            # 2b. Inq count (律見次數 — from case_todos)
            result = self.db.fetch_one(
                """SELECT COUNT(*) as cnt FROM `case_todos`
                   WHERE `case_number` = %s
                   AND (`todo_type` LIKE '%%律見%%' OR `todo_type` LIKE '%%律師接見%%'
                        OR `todo_type` LIKE '%%接見%%')
                   AND `status` = 'completed'""",
                (case_number,)
            )
            if result:
                counts["inq_count"] = result[0] if isinstance(result, tuple) else result.get("cnt", 0)

            # 3. Court dates (from case_todos — hearings)
            _court_rows = self.db.fetch_all(
                """SELECT `todo_date` FROM `case_todos`
                   WHERE `case_number` = %s
                   AND `todo_type` IN ('言詞辯論', '準備程序', '審理程序', '調解', '開庭', '訊問')
                   AND `status` = 'completed'
                   ORDER BY `todo_date`""",
                (case_number,)
            ) or []
            _seen_court = set()
            for row in _court_rows:
                _d = row[0] if isinstance(row, tuple) else row.get("todo_date")
                if _d:
                    _d_str = str(_d)[:10]
                    if _d_str not in _seen_court:
                        _seen_court.add(_d_str)
                        counts["court_dates"].append(_d)
            counts["court_count"] = len(counts["court_dates"])

            # 4. Review dates (閱卷 — from case_todos)
            _review_rows = self.db.fetch_all(
                """SELECT `todo_date` FROM `case_todos`
                   WHERE `case_number` = %s
                   AND (`todo_type` LIKE '%%閱卷%%' OR `todo_type` LIKE '%%review%%')
                   AND `status` = 'completed'
                   ORDER BY `todo_date`""",
                (case_number,)
            ) or []
            _seen_review = set()
            for row in _review_rows:
                _d = row[0] if isinstance(row, tuple) else row.get("todo_date")
                if _d:
                    _d_str = str(_d)[:10]
                    if _d_str not in _seen_review:
                        _seen_review.add(_d_str)
                        counts["review_dates"].append(_d)
            counts["review_count"] = len(counts["review_dates"])

            # 5. Document count (書狀次數 — 只算 04_我方歷次書狀，用 case_number 精確匹配)
            _doc_key = case_number or client_name
            if _doc_key:
                result = self.db.fetch_one(
                    r"""SELECT COUNT(DISTINCT `subfolder_name`) as cnt FROM `document_index`
                       WHERE `case_full_name` LIKE %s
                       AND `file_path` LIKE '%%04\_我方歷次書狀%%'""",
                    (f"{_doc_key}%",)
                )
                if result:
                    counts["document_count"] = result[0] if isinstance(result, tuple) else result.get("cnt", 0)

        except Exception as e:
            logger.error("Error gathering counts for %s: %s", case_number, e)

        # 當計數為 0 時，從 calendar_events 表補數字（和 OSC 的 GCal 統計邏輯一致）
        # 只查派案日期之後的事件
        _zero_keys = [k for k in ("meeting_count", "contact_count", "court_count", "review_count")
                      if int(counts.get(k, 0) or 0) == 0]
        if _zero_keys and self.db and client_name:
            try:
                import re as _re
                _cn_parts = _re.findall(r'[\u4e00-\u9fff]+', client_name)
                _cn_only = "".join(_cn_parts) if _cn_parts else client_name

                # 取開辦日期（start_date）或 approval_date 作為派案日期
                _assign_dt = None
                for _q, _p in [
                    ("SELECT start_date, approval_date FROM cases WHERE legal_aid_number = %s LIMIT 1", (case_number,)),
                    ("SELECT start_date, approval_date FROM cases WHERE case_number = %s LIMIT 1", (case_number,)),
                ]:
                    _case_row = self.db.fetch_one(_q, _p, as_dict=True)
                    if _case_row:
                        _assign_dt = _case_row.get("start_date") or _case_row.get("approval_date")
                        break

                _date_clause = ""
                _params: list = []
                if _assign_dt:
                    from datetime import datetime, date
                    if isinstance(_assign_dt, (datetime, date)):
                        _date_clause = " AND start_date >= %s"
                        _params.append(_assign_dt.strftime('%Y-%m-%d') if hasattr(_assign_dt, 'strftime') else str(_assign_dt))
                    elif isinstance(_assign_dt, str) and _assign_dt.strip():
                        _date_clause = " AND start_date >= %s"
                        _params.append(_assign_dt.strip().split(' ')[0])

                _name_clause = " AND (summary LIKE %s)"
                _params.append(f"%{_cn_only}%")

                _events = self.db.fetch_all(
                    f"SELECT summary, start_date FROM calendar_events WHERE 1=1{_date_clause}{_name_clause}",
                    tuple(_params)
                ) or []

                if _events:
                    # 用 OSC 相同的關鍵字分類
                    _court_kw = ["開庭", "言詞辯論", "準備程序", "調解", "訊問", "審理"]
                    _meet_kw = ["會議", "來所", "碰面", "視訊", "面談", "開會", "交資料", "律見", "接見", "律師接見"]
                    _tel_kw = ["電話聯繫", "通話", "電聯", "聯繫", "聯絡"]
                    _review_kw = ["閱卷", "影卷", "調卷"]
                    _excl_kw = ["聲請改期", "改期", "取消", "不出席", "不到庭"]

                    _c_court = 0; _c_meet = 0; _c_tel = 0; _c_review = 0
                    _court_dates_cal = []; _review_dates_cal = []
                    for summary, start_date in _events:
                        s = str(summary or "")
                        if any(ex in s for ex in _excl_kw):
                            continue
                        if any(k in s for k in _court_kw):
                            _c_court += 1
                            if start_date:
                                _court_dates_cal.append(start_date)
                        elif any(k in s for k in _review_kw):
                            _c_review += 1
                            if start_date:
                                _review_dates_cal.append(start_date)
                        elif any(k in s for k in _meet_kw):
                            _c_meet += 1
                        elif any(k in s for k in _tel_kw):
                            _c_tel += 1

                    if "meeting_count" in _zero_keys and _c_meet > 0:
                        counts["meeting_count"] = _c_meet
                        logger.info("  📅 Calendar 補 meeting_count: %d", _c_meet)
                    if "contact_count" in _zero_keys and _c_tel > 0:
                        counts["contact_count"] = _c_tel
                        logger.info("  📅 Calendar 補 contact_count: %d", _c_tel)
                    if "court_count" in _zero_keys and _c_court > 0:
                        counts["court_count"] = _c_court
                        if not counts["court_dates"]:
                            counts["court_dates"] = _court_dates_cal
                        logger.info("  📅 Calendar 補 court_count: %d (dates: %s)", _c_court, _court_dates_cal)
                    if "review_count" in _zero_keys and _c_review > 0:
                        counts["review_count"] = _c_review
                        if not counts["review_dates"]:
                            counts["review_dates"] = _review_dates_cal
                        logger.info("  📅 Calendar 補 review_count: %d (dates: %s)", _c_review, _review_dates_cal)
            except Exception as e:
                logger.warning("Calendar fallback for zero counts failed: %s", e)

        # 第三層 fallback：若 DB calendar_events 表也沒資料，直接查 Google Calendar API
        _still_zero = [k for k in ("meeting_count", "contact_count", "court_count", "review_count")
                       if int(counts.get(k, 0) or 0) == 0]
        if _still_zero and client_name:
            try:
                self._gcal_fallback_counts(counts, _still_zero, case_number, client_name)
            except Exception as e:
                logger.warning("GCal API fallback for zero counts failed: %s", e)

        return counts

    def _gcal_fallback_counts(self, counts: dict, zero_keys: list, case_number: str, client_name: str) -> None:
        """Query Google Calendar API directly when DB has no matching events."""
        import re as _re
        from datetime import datetime, timedelta, timezone

        # 取 credentials / token
        credentials_path = os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH", "").strip()
        token_path = os.environ.get("MAGI_GOOGLE_CALENDAR_TOKEN_PATH", "").strip()
        if not credentials_path:
            credentials_path = str(get_config_path("credentials.json"))
        if not token_path:
            token_path = str(get_config_path("google_calendar_token.json"))
        if not os.path.exists(credentials_path):
            return

        try:
            from skills.osc_orchestrator.action import _build_google_calendar_service
        except ImportError:
            try:
                import importlib, sys
                _osc_action_path = os.path.join(_MAGI_ROOT, "skills", "osc-orchestrator", "action.py")
                spec = importlib.util.spec_from_file_location("osc_action", _osc_action_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _build_google_calendar_service = mod._build_google_calendar_service
            except Exception:
                logger.debug("Cannot import _build_google_calendar_service, skipping GCal fallback")
                return

        svc_result = _build_google_calendar_service(credentials_path, token_path, interactive=False)
        if not svc_result.get("ok"):
            logger.debug("GCal service init failed: %s", svc_result.get("error"))
            return
        service = svc_result["service"]

        # 查最近 2 年事件，按當事人姓名過濾
        _cn_parts = _re.findall(r'[\u4e00-\u9fff]+', client_name)
        _cn_only = "".join(_cn_parts) if _cn_parts else client_name
        if not _cn_only:
            return

        now = datetime.now(timezone.utc)
        time_min = (now - timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 搜尋所有日曆（primary + 其他日曆），不只 primary
        try:
            cal_list = service.calendarList().list().execute().get("items", [])
            cal_ids = [c["id"] for c in cal_list if c.get("id")]
        except Exception:
            cal_ids = ["primary"]
        if not cal_ids:
            cal_ids = ["primary"]

        events = []
        for _cal_id in cal_ids:
            try:
                _result = service.events().list(
                    calendarId=_cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=500,
                    singleEvents=True,
                    orderBy="startTime",
                    q=_cn_only,
                ).execute()
                _items = _result.get("items", [])
                if _items:
                    logger.info("  📅 GCal API: calendar '%s' → %d events for '%s'", _cal_id[:30], len(_items), _cn_only)
                    events.extend(_items)
            except Exception as e:
                logger.debug("GCal API list failed for calendar %s: %s", _cal_id[:30], e)

        if not events:
            logger.info("  📅 GCal API: no events found for '%s' across %d calendars", _cn_only, len(cal_ids))
            return

        logger.info("  📅 GCal API: total %d events for '%s' across %d calendars", len(events), _cn_only, len(cal_ids))

        _court_kw = ["開庭", "言詞辯論", "準備程序", "調解", "訊問", "審理"]
        _meet_kw = ["會議", "來所", "碰面", "視訊", "面談", "開會", "交資料", "律見", "接見", "律師接見"]
        _tel_kw = ["電話聯繫", "通話", "電聯", "聯繫", "聯絡"]
        _review_kw = ["閱卷", "影卷", "調卷"]
        _excl_kw = ["聲請改期", "改期", "取消", "不出席", "不到庭"]

        _c_court = 0; _c_meet = 0; _c_tel = 0; _c_review = 0
        _court_dates_gcal = []; _review_dates_gcal = []
        for ev in events:
            summary = ev.get("summary", "")
            if any(ex in summary for ex in _excl_kw):
                continue
            start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
            if any(k in summary for k in _court_kw):
                _c_court += 1
                if start:
                    _court_dates_gcal.append(start[:10])
            elif any(k in summary for k in _review_kw):
                _c_review += 1
                if start:
                    _review_dates_gcal.append(start[:10])
            elif any(k in summary for k in _meet_kw):
                _c_meet += 1
            elif any(k in summary for k in _tel_kw):
                _c_tel += 1

        if "meeting_count" in zero_keys and _c_meet > 0:
            counts["meeting_count"] = _c_meet
            logger.info("  📅 GCal API 補 meeting_count: %d", _c_meet)
        if "contact_count" in zero_keys and _c_tel > 0:
            counts["contact_count"] = _c_tel
            logger.info("  📅 GCal API 補 contact_count: %d", _c_tel)
        if "court_count" in zero_keys and _c_court > 0:
            counts["court_count"] = _c_court
            if not counts.get("court_dates"):
                counts["court_dates"] = _court_dates_gcal
            logger.info("  📅 GCal API 補 court_count: %d", _c_court)
        if "review_count" in zero_keys and _c_review > 0:
            counts["review_count"] = _c_review
            if not counts.get("review_dates"):
                counts["review_dates"] = _review_dates_gcal
            logger.info("  📅 GCal API 補 review_count: %d", _c_review)

    def _check_duplicate(self, laf_number, client_name, case_type, case_reason):
        """Check if case already exists in DB."""
        if not self.db:
            return None

        try:
            laf_number = str(laf_number or "").strip()
            client_key = self._norm_token(client_name)
            # Strategy 1: LAF number exact match
            if laf_number:
                result = self.db.fetch_one(
                    "SELECT * FROM `cases` WHERE `legal_aid_number` = %s LIMIT 1",
                    (laf_number,), as_dict=True
                )
                if result:
                    return result
                result = self.db.fetch_one(
                    "SELECT * FROM `cases` WHERE `notes` LIKE %s LIMIT 1",
                    (f"%{laf_number}%",), as_dict=True
                )
                if result:
                    return result

            # Strategy 2: Name + type + category (異體字在 Python 端比對)
            if client_key and case_type:
                rows = self.db.fetch_all(
                    """SELECT * FROM `cases`
                       WHERE `case_type` = %s
                       AND `case_category` = '法律扶助案件'
                       ORDER BY `created_date` DESC LIMIT 50""",
                    (case_type,), as_dict=True
                )
                for result in (rows or []):
                    if not isinstance(result, dict):
                        continue
                    # 異體字正規化比對 client_name
                    db_client_key = self._norm_token(result.get("client_name"))
                    if db_client_key != client_key:
                        continue
                    if laf_number:
                        existing_laf = str(result.get("legal_aid_number") or "").strip()
                        notes = str(result.get("notes") or "")
                        # 若 DB 的 legal_aid_number 非空且與本次不同，才跳過
                        # 若 DB 的 legal_aid_number 為空（尚未填入），允許依 case_reason 比對
                        if existing_laf and existing_laf != laf_number and laf_number not in notes:
                            continue
                    db_reason = str(result.get("case_reason") or "").strip()
                    if case_reason:
                        if db_reason and (case_reason in db_reason or db_reason in case_reason):
                            return result
                        continue
                    return result

        except Exception as e:
            logger.error("Duplicate check error: %s", e)

        return None

    def _update_legal_aid_number(self, case_id, laf_number):
        """Update legal_aid_number for an existing case."""
        if self.dry_run or not self.db:
            return
        try:
            self.db.execute_write(
                "UPDATE `cases` SET `legal_aid_number` = %s WHERE `id` = %s",
                (laf_number, case_id)
            )
        except Exception as e:
            logger.error("Update LAF number failed: %s", e)

    def _generate_case_number(self) -> str:
        """Generate a standard OSC case number (YYYY-NNNN)."""
        if not self.db:
            return ""
        try:
            if hasattr(self.db, "generate_case_number"):
                return str(self.db.generate_case_number() or "").strip()

            result = None
            if hasattr(self.db, "fetch_one"):
                result = self.db.fetch_one(
                    "SELECT `case_number` FROM `cases` WHERE `case_number` REGEXP '^20[0-9]{2}-[0-9]{4}$' ORDER BY `case_number` DESC LIMIT 1",
                    as_dict=True,
                )
            elif hasattr(self.db, "execute"):
                result = self.db.execute(
                    "SELECT `case_number` FROM `cases` WHERE `case_number` REGEXP '^20[0-9]{2}-[0-9]{4}$' ORDER BY `case_number` DESC LIMIT 1",
                    fetch="one",
                )

            current_year = datetime.now().year
            last_case = ""
            if isinstance(result, dict):
                last_case = str(result.get("case_number") or "").strip()
            elif isinstance(result, tuple) and result:
                last_case = str(result[0] or "").strip()

            if last_case.startswith(f"{current_year}-"):
                seq = int(last_case.split("-", 1)[1]) + 1
            else:
                seq = 1
            return f"{current_year}-{seq:04d}"
        except Exception as e:
            logger.error("Generate case number failed: %s", e)
            return ""

    def _create_case_record(self, case_info, folder_path, *, case_number: str = ""):
        """Insert a new case record into DB."""
        if not self.db:
            return ""

        import uuid
        case_id = str(uuid.uuid4())[:8]
        client_name = getattr(case_info, 'client_name', '')
        case_type = getattr(case_info, 'case_type', '')
        case_reason = getattr(case_info, 'case_reason', '')
        laf_number = getattr(case_info, 'laf_case_number', '')
        case_stage = getattr(case_info, 'case_stage', '')

        case_number = str(case_number or "").strip() or self._generate_case_number()
        if not case_number:
            logger.error("  ❌ Could not determine standard case_number for DB insert")
            return ""

        try:
            self.db.execute_write(
                """INSERT INTO `cases`
                   (`id`, `case_number`, `client_name`, `case_type`, `case_reason`,
                    `case_category`, `case_stage`, `status`, `folder_path`,
                    `legal_aid_number`, `start_date`, `lawyer`)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (case_id, case_number, client_name, case_type, case_reason,
                 '法律扶助案件', case_stage, 'Active', folder_path,
                 laf_number, datetime.now().date(), '喬政翔律師')
            )
            logger.info("  ✅ DB record created: %s (%s)", case_number, case_id)
            return case_number
        except Exception as e:
            logger.error("  ❌ DB insert failed: %s", e)
            return ""

    def _download_case_files(
        self,
        laf_number,
        *,
        case_folder: str = "",
        client_name: str = "",
        case_type: str = "",
        case_reason: str = "",
        case_number: str = "",
    ):
        """Download case files from LAF portal."""
        result = {
            "ok": True,
            "laf_case_number": str(laf_number or "").strip(),
            "downloaded_files": [],
            "downloaded_count": 0,
            "retry_queued": False,
            "retry_reason": "",
            "archive": {
                "ok": False,
                "new_files": [],
                "skipped_existing": [],
                "zip_backups": [],
                "zip_backup_skipped": [],
                "error": "",
            },
            "error": "",
        }
        if not laf_number:
            result["ok"] = False
            result["error"] = "missing_laf_number"
            return result

        try:
            from laf_automation_v2 import LAFWebAutomation

            username = os.environ.get("MAGI_LAF_USERNAME") or self.laf_config.get("username", "")
            password = os.environ.get("MAGI_LAF_PASSWORD") or self.laf_config.get("password", "")
            download_folder = self.laf_config.get("download_folder", "./laf_downloads")
            headless = self.laf_config.get("headless", True)

            if not username or not password:
                logger.warning("LAF credentials not configured, skipping download")
                result["ok"] = False
                result["error"] = "missing_credentials"
                return result

            automation = LAFWebAutomation(
                username=username,
                password=password,
                download_folder=download_folder,
                headless=headless,
                log_callback=lambda msg: logger.info("[LAF] %s", msg),
                browser_profile_dir=self.laf_config.get("browser_profile_dir", ""),
            )

            try:
                automation.login()
                files = automation.download_case_files(laf_number)
                logger.info("  📥 Downloaded %d files for %s", len(files), laf_number)
                result = self._process_portal_download_result(
                    laf_number=str(laf_number or ""),
                    client_name=client_name,
                    case_type=case_type,
                    case_reason=case_reason,
                    case_folder=case_folder,
                    case_number=case_number,
                    files=files,
                    source="initial",
                )
            finally:
                automation.close()

        except ImportError:
            logger.warning("LAFWebAutomation not available, skipping download")
            result["ok"] = False
            result["error"] = "automation_unavailable"
        except Exception as e:
            logger.error("Download failed: %s", e)
            result["ok"] = False
            result["error"] = str(e)
            failed_result = self._process_portal_download_result(
                laf_number=str(laf_number or ""),
                client_name=client_name,
                case_type=case_type,
                case_reason=case_reason,
                case_folder=case_folder,
                case_number=case_number,
                files=[],
                source="initial",
                last_error=str(e),
            )
            result["retry_queued"] = bool(failed_result.get("retry_queued"))
            result["retry_reason"] = str(failed_result.get("retry_reason") or "")
        return result

    # ==================================================================
    # Lifecycle Event Logging
    # ==================================================================

    def _log_event(self, case_number: str, event_type: str,
                   event_data: dict, status: str):
        """Log lifecycle event to laf_lifecycle_log table."""
        if self.dry_run:
            logger.info("  [DRY RUN] Log: %s/%s → %s", case_number, event_type, status)
            return

        if not self.db:
            return

        try:
            self.db.execute_write(
                """INSERT INTO `laf_lifecycle_log`
                   (`case_number`, `event_type`, `event_data`, `status`)
                   VALUES (%s, %s, %s, %s)""",
                (case_number, event_type,
                 json.dumps(event_data, ensure_ascii=False, default=str),
                 status)
            )
        except Exception as e:
            # Table might not exist yet — log but don't crash
            logger.warning("Lifecycle log write failed (table may not exist): %s", e)


# ==============================================================================
# CLI
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="LAF Case Lifecycle Orchestrator")
    parser.add_argument("--mode", choices=["monitor", "closing", "closing-draft", "condition-draft", "condition-mark-done", "condition-mark-by-mediation", "portal-draft", "portal-submit", "dry-run", "test-notify"],
                        default="dry-run",
                        help="monitor=watch Gmail, closing=process 待報結, dry-run=preview")
    parser.add_argument("--case", type=str, default=None,
                        help="Specific case number to process (for closing mode)")
    parser.add_argument("--laf-case-no", type=str, default="", help="LAF case number for portal-draft mode")
    parser.add_argument("--client", type=str, default="", help="Client name for portal-draft mode")
    parser.add_argument("--action", type=str, default="", help="Portal action: go_live|inquiry|fee|condition|withdrawal|closing")
    parser.add_argument("--reason", type=str, default="", help="Reason/description for inquiry/fee/condition/withdrawal/closing")
    parser.add_argument("--fields-json", type=str, default="", help="Optional JSON object for workflow fields")
    parser.add_argument("--max-cases", type=int, default=3, help="Max cases for condition-draft mode")
    parser.add_argument("--clients", type=str, default="", help="Comma-separated client names for condition-mark-done")
    parser.add_argument("--laf-list", type=str, default="", help="Comma-separated LAF case numbers for condition-mark-done")
    parser.add_argument("--osc-list", type=str, default="", help="Comma-separated OSC case numbers for condition-mark-done")
    parser.add_argument("--max-scan", type=int, default=8000, help="Max rows scan for condition-mark-by-mediation mode")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run regardless of mode")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    dry_run = bool(args.dry_run or args.mode == "dry-run")
    orchestrator = LAFOrchestrator(dry_run=dry_run)

    if args.mode == "monitor":
        orchestrator.run_monitor()

    elif args.mode in ("closing", "dry-run"):
        if args.case:
            # Process specific case
            case_data = {"case_number": args.case, "client_name": ""}
            # Try to look up client name from DB
            if orchestrator.db:
                result = orchestrator.db.fetch_one(
                    "SELECT `client_name` FROM `cases` WHERE `case_number` = %s",
                    (args.case,)
                )
                if result:
                    case_data["client_name"] = result[0] if isinstance(result, tuple) else result.get("client_name", "")

            orchestrator.prepare_closing_report(case_data)
        else:
            orchestrator.run_closing()

    elif args.mode == "closing-draft":
        r = orchestrator.run_closing_drafts(max_cases=int(args.max_cases or 3))
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif args.mode == "condition-draft":
        r = orchestrator.run_condition_drafts(max_cases=int(args.max_cases or 3))
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif args.mode == "condition-mark-done":
        clients = [x.strip() for x in str(args.clients or "").split(",") if x.strip()]
        laf_list = [x.strip() for x in str(args.laf_list or "").split(",") if x.strip()]
        osc_list = [x.strip() for x in str(args.osc_list or "").split(",") if x.strip()]
        rows = []
        if orchestrator.db:
            try:
                q = (
                    "SELECT `case_number`, `client_name`, `legal_aid_number`, `folder_path` "
                    "FROM `cases` WHERE `case_category` = '法律扶助案件' "
                    "ORDER BY `id` DESC LIMIT 3000"
                )
                rows = orchestrator.db.fetch_all(q, as_dict=True) or []
            except Exception:
                rows = []
        marked = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            osc_no = str(r.get("case_number") or "").strip()
            laf_no = str(r.get("legal_aid_number") or "").strip()
            cname = str(r.get("client_name") or "").strip()
            folder = orchestrator._to_local_case_folder(str(r.get("folder_path") or "").strip())
            match = False
            if clients and cname in clients:
                match = True
            if laf_list and laf_no in laf_list:
                match = True
            if osc_list and osc_no in osc_list:
                match = True
            if not (clients or laf_list or osc_list):
                continue
            if not match:
                continue
            out = orchestrator.mark_condition_manual_done(
                laf_case_number=laf_no,
                osc_case_number=osc_no,
                client_name=cname,
                case_folder=folder,
                reason="manual_reported_by_lawyer",
            )
            marked.append(out.get("payload") or {})
        print(json.dumps({"ok": True, "marked": len(marked), "items": marked}, ensure_ascii=False, indent=2))

    elif args.mode == "condition-mark-by-mediation":
        r = orchestrator.run_condition_mark_by_mediation(max_scan=int(args.max_scan or 8000))
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif args.mode == "portal-draft":
        fields = {}
        if args.fields_json:
            try:
                parsed = json.loads(args.fields_json)
                if isinstance(parsed, dict):
                    fields = parsed
            except Exception as e:
                print(json.dumps({"ok": False, "error": f"invalid_fields_json: {e}"}, ensure_ascii=False, indent=2))
                return
        result = orchestrator.execute_portal_action_draft(
            action=args.action,
            laf_case_number=args.laf_case_no or "",
            case_number=args.case or "",
            client_name=args.client or "",
            reason=args.reason or "",
            fields=fields,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.mode == "portal-submit":
        fields = {}
        if args.fields_json:
            try:
                parsed = json.loads(args.fields_json)
                if isinstance(parsed, dict):
                    fields = parsed
            except Exception as e:
                print(json.dumps({"ok": False, "error": f"invalid_fields_json: {e}"}, ensure_ascii=False, indent=2))
                return
        result = orchestrator.execute_portal_action_submit(
            action=args.action,
            laf_case_number=args.laf_case_no or "",
            case_number=args.case or "",
            client_name=args.client or "",
            reason=args.reason or "",
            fields=fields,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    elif args.mode == "test-notify":
        # Quick test of notification
        notifier = _get_notifier()
        notifier.notify_admin(
            "🧪 CASPER 法扶自動化通知測試\n"
            f"時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "如果您看到這則訊息，表示通知系統正常運作。"
        )
        print("✅ Test notification sent")


for _name in (
    "_text_contains_any",
    "_find_first_existing",
    "_normalize_date_text",
    "_extract_date_from_filename",
    "_extract_date_from_office_text",
    "_get_doc_hint_ocr_engine",
    "_ocr_text_from_image",
    "_should_sniff_doc_content",
    "_extract_document_hint_text",
    "_extract_date_with_vision",
    "_extract_best_date_from_doc",
    "_empty_docs_map",
    "_dedupe_sorted",
    "_is_consumer_debt_case_folder",
    "_is_consumer_debt_terminal_doc",
    "_is_fee_related_receipt_doc",
    "_filter_receipt_evidence_files",
    "_closing_basis_sort_key",
    "_sort_closing_basis_files",
    "_scan_case_folder_docs",
    "_infer_closing_metadata_from_docs",
):
    setattr(LAFOrchestrator, _name, LAFOrchestratorDocumentMixin.__dict__[_name])


if __name__ == "__main__":
    main()
