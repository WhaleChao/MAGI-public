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
import hashlib
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

from api.runtime_paths import (
    ensure_path_on_sys_path,
    get_config_path,
    get_laf_orchestrator_state_path,
    get_laf_processed_emails_path,
    get_orch_dir,
)
from api.case_path_mapper import _is_dir_accessible, canonical_case_roots, default_case_roots, local_synology_path_candidates, preferred_case_roots, resolve_case_path_for_write, translate_case_path_to_local, translate_local_path_to_canonical
from api.laf_case_classifier import normalize_laf_case_fields
from api.laf_go_live_rules import (
    go_live_missing_labels,
    go_live_notice_files,
    go_live_proof_files,
    is_go_live_ready,
    is_opening_notice_filename,
    is_stored_pleading_proof,
)
from api.osc.case_defaults import db_settings_getter, normalize_case_lawyer
from skills.bridge.shared_utils.judgment_folder_names import judgment_folder_aliases, judgment_folder_name
from api.product_runtime import get_product_profile, resolve_laf_portal_targets
from magi_v3.external_inputs import laf_download_directory
from magi_v3 import fcntl_compat as fcntl

CODE_DIR = get_orch_dir()
SKILLS_DIR = MAGI_DIR / "skills"
# Max retry attempts for portal downloads before marking as exhausted
# 168 次 × 1 小時 = 7 天（足夠等 portal 新案就緒）
_PORTAL_RETRY_MAX_TRIES = int(os.environ.get("MAGI_LAF_PORTAL_MAX_RETRIES", "168") or "168")
# Portal retry loop 間隔（獨立於 Gmail check interval，預設 3600 秒 = 1 小時）
# 避免和 Gmail 5 分鐘輪詢共用同一個 interval 導致 2.5 小時內打爆上限
_PORTAL_RETRY_LOOP_INTERVAL = int(os.environ.get("MAGI_LAF_PORTAL_RETRY_INTERVAL", "3600") or "3600")
# A portal browser can occasionally stop answering without raising an error.
# Keep the server recoverable instead of leaving the retry lock held forever.
_PORTAL_RETRY_CYCLE_TIMEOUT = int(os.environ.get("MAGI_LAF_PORTAL_RETRY_CYCLE_TIMEOUT", "1800") or "1800")
_PORTAL_ATTACHMENT_RETENTION_DAYS = max(
    1,
    int(os.environ.get("MAGI_LAF_ATTACHMENT_RETENTION_DAYS", "14") or "14"),
)


def _portal_retry_initial_delay_seconds(interval_seconds: int) -> int:
    """Delay the first heavy portal retry after an ordinary MAGI restart."""
    immediate = str(os.environ.get("MAGI_LAF_PORTAL_RETRY_IMMEDIATE", "0") or "0").strip().lower()
    return 0 if immediate in {"1", "true", "yes", "on"} else max(60, int(interval_seconds or 3600))
CONDITION_MANUAL_DONE_PATH = get_laf_orchestrator_state_path("_laf_condition_manual_done.json")
CONFIG_PATH = get_config_path("config.json")

ensure_path_on_sys_path(CODE_DIR)
ensure_path_on_sys_path(SKILLS_DIR / "legal")
ensure_path_on_sys_path(SKILLS_DIR / "osc-orchestrator")

logger = logging.getLogger("laf_orchestrator")

_LAF_UPLOAD_STAGING_SENTINEL = ".magi-self-repair-owned"
_LAF_UPLOAD_STAGING_SENTINEL_TEXT = "magi-self-repair-owned-v1"


def _sync_case_path_references(
    db,
    *,
    old_canonical: str,
    new_canonical: str,
    old_local: str = "",
    new_local: str = "",
) -> dict:
    """Update OSC database references and the mutable PDF-namer case index."""
    from api.osc.utils import _osc_replace_path_prefix_references

    def _exec(sql, params=(), fetch="none"):
        rowcount = db.execute_write(sql, params)
        return {"rowcount": int(rowcount or 0)}, None

    results = [
        _osc_replace_path_prefix_references(
            old_canonical,
            new_canonical,
            exec_fn=_exec,
        )
    ]
    if old_local and new_local:
        results.append(
            _osc_replace_path_prefix_references(
                old_local,
                new_local,
                exec_fn=_exec,
            )
        )
    return {
        "updated": sum(int(item.get("updated") or 0) for item in results),
        "attempted": sum(int(item.get("attempted") or 0) for item in results),
        "errors": [
            error
            for item in results
            for error in list(item.get("errors") or [])
        ],
    }


def _create_laf_upload_staging_dir(path: str) -> str:
    """Create a managed upload staging directory with a cleanup ownership proof."""
    staging = Path(path)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / _LAF_UPLOAD_STAGING_SENTINEL).write_text(
        _LAF_UPLOAD_STAGING_SENTINEL_TEXT + "\n",
        encoding="utf-8",
    )
    return str(staging)

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


def _safe_listdir(path: str, *, timeout_sec: float = 2.0) -> list[str]:
    """List a NAS/Synology Drive directory without letting stale mounts block LAF."""
    out: dict[str, list[str]] = {"items": []}

    def _runner() -> None:
        try:
            out["items"] = list(os.listdir(path))
        except Exception:
            out["items"] = []

    t = threading.Thread(target=_runner, daemon=True, name="laf-safe-listdir")
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        logger.warning("LAF safe listdir timeout after %.1fs: %s", timeout_sec, path)
        return []
    return out.get("items") or []


def _safe_getmtime(path: str, *, timeout_sec: float = 1.5) -> float:
    out: dict[str, float] = {"value": 0.0}

    def _runner() -> None:
        try:
            out["value"] = float(os.path.getmtime(path))
        except Exception:
            out["value"] = 0.0

    t = threading.Thread(target=_runner, daemon=True, name="laf-safe-getmtime")
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        logger.warning("LAF safe getmtime timeout after %.1fs: %s", timeout_sec, path)
        return 0.0
    return float(out.get("value") or 0.0)

# -------------------------------------------------------------------
# Lazy imports (avoid import-time failures on missing deps)
# -------------------------------------------------------------------
from laf_vision import LAFVision
from laf_orchestrator_docmixins import LAFOrchestratorDocumentMixin
from api.handlers.laf_handler import _expand_reason_keywords
_db_manager = None
_legalbridge_db = None
_notifier = None
_folder_builder = None
_portal_retry_thread = None
_portal_retry_state_lock = threading.Lock()


class _PortalRetryCycleTimeout(TimeoutError):
    """Raised only when the retry watchdog, rather than the portal, expires."""


class _DryRunNotifier:
    """Absorb every notification emitted while an orchestrator dry-run is active."""

    def _suppress(self, method: str, *args, **kwargs) -> bool:
        topic_key = str(kwargs.get("topic_key") or "-")
        logger.info("[DRY RUN] Notification suppressed (%s, topic=%s)", method, topic_key)
        return True

    def notify_admin(self, *args, **kwargs) -> bool:
        return self._suppress("notify_admin", *args, **kwargs)

    def notify_admin_with_files(self, *args, **kwargs) -> bool:
        return self._suppress("notify_admin_with_files", *args, **kwargs)

    def send_closing_confirmation(self, *args, **kwargs) -> bool:
        return self._suppress("send_closing_confirmation", *args, **kwargs)

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)

        def _suppressed(*args, **kwargs) -> bool:
            return self._suppress(method, *args, **kwargs)

        return _suppressed


def _load_workflow_provider_fixture() -> Tuple[Path, dict] | None:
    raw = os.environ.get("MAGI_LAF_WORKFLOW_PROVIDER_FIXTURE", "").strip()
    if not raw:
        return None
    if os.environ.get("MAGI_V3_REALISM_SANDBOX") != "1":
        raise RuntimeError("LAF workflow fixture requires the V3 realism sandbox")
    root_raw = os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT", "").strip()
    if not root_raw:
        raise RuntimeError("LAF workflow fixture root is missing")
    root = Path(root_raw).expanduser().resolve(strict=True)
    path = Path(raw).expanduser()
    if path.is_symlink():
        raise RuntimeError("LAF workflow fixture may not be a symlink")
    path = path.resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("LAF workflow fixture escapes the schedule sandbox") from exc
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("LAF workflow fixture is unreadable") from exc
    workflows = payload.get("allowed_workflows") if isinstance(payload, dict) else None
    if (
        not isinstance(workflows, list)
        or not workflows
        or any(
            str(item)
            not in {"condition", "inquiry", "withdrawal", "fee", "attachment_retry"}
            for item in workflows
        )
    ):
        raise RuntimeError("LAF workflow fixture allowlist is malformed")
    return root, payload


def _resolve_schedule_fixture_case_folder_for_write(folder: str) -> str:
    """Admit only the disposable attachment-retry case tree as writable.

    Production storage authority remains unchanged.  This narrow branch exists
    so the real scheduled job body can be tested under Seatbelt without being
    mistaken for an unapproved NAS mirror.
    """

    if os.environ.get("MAGI_V3_REALISM_SANDBOX") != "1":
        return ""
    try:
        fixture = _load_workflow_provider_fixture()
    except (OSError, RuntimeError, ValueError):
        return ""
    if fixture is None:
        return ""
    root, payload = fixture
    if set(str(item) for item in payload.get("allowed_workflows") or []) != {
        "attachment_retry"
    }:
        return ""
    marker = root / ".magi-v3-schedule-fixture"
    if marker.is_symlink() or not marker.is_file():
        return ""
    try:
        if marker.read_text(encoding="utf-8").strip() != "job_laf_portal_retry_once":
            return ""
        candidate_raw = Path(str(folder or "").strip()).expanduser()
        if not candidate_raw.is_absolute() or candidate_raw.is_symlink():
            return ""
        candidate = candidate_raw.resolve(strict=True)
        relative = candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return ""
    if not relative.parts or relative.parts[0] != "cases" or not candidate.is_dir():
        return ""
    return str(candidate)


class _FixtureWorkflowAutomation:
    """External portal boundary for real draft orchestration under Seatbelt."""

    def __init__(self, root: Path, payload: dict):
        self.root = root
        self.payload = payload
        self.transcript: List[dict] = []
        self.last_debug_artifact: dict = {}
        self.last_upload_result: dict = {}

    def _record(self, action: str, **values) -> None:
        self.transcript.append({"action": action, **values})
        target = self.root / "workflow_provider_transcript.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.transcript, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)

    def login(self) -> bool:
        self._record("login", ok=True)
        return True

    def save_workflow_draft(
        self,
        *,
        workflow: str,
        laf_case_number: str,
        client_name: str,
        fields: dict,
    ) -> bool:
        allowed = {str(item) for item in self.payload["allowed_workflows"]}
        if workflow not in allowed:
            self._record("save_workflow_draft", workflow=workflow, accepted=False)
            return False
        uploads: List[dict] = []
        for raw in fields.get("upload_files") or []:
            path = Path(str(raw)).expanduser()
            if path.is_symlink():
                return False
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self.root)
            except (OSError, ValueError):
                return False
            uploads.append(
                {
                    "path": resolved.relative_to(self.root).as_posix(),
                    "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
                }
            )
        draft = {
            "workflow": workflow,
            "laf_case_number": str(laf_case_number),
            "client_name": str(client_name),
            "fields": {
                key: value for key, value in fields.items() if key != "upload_files"
            },
            "uploads": uploads,
            "saved": True,
        }
        target = self.root / "workflow_draft.json"
        target.write_text(
            json.dumps(draft, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self.last_upload_result = {
            "status": "uploaded" if uploads else "no_uploads",
            "uploaded": len(uploads),
        }
        self.last_debug_artifact = {"fixture_draft": str(target)}
        self._record(
            "save_workflow_draft",
            workflow=workflow,
            laf_case_number=str(laf_case_number),
            upload_count=len(uploads),
            accepted=True,
        )
        return True

    def submit_workflow(self, **_kwargs) -> bool:
        self._record("submit_workflow", accepted=False)
        return False

    def download_case_files(self, case_number: str, row_element=None) -> List[str]:
        allowed = {str(item) for item in self.payload["allowed_workflows"]}
        if "attachment_retry" not in allowed:
            self._record(
                "download_case_files",
                case_number=str(case_number),
                accepted=False,
            )
            return []
        download_root = self.root / "provider-downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        filename = f"接案通知書_{str(case_number)}_fixture.pdf"
        target = download_root / filename
        target.write_bytes(b"%PDF-1.4\n% MAGI disposable LAF retry fixture\n%%EOF\n")
        self._record(
            "download_case_files",
            case_number=str(case_number),
            row_element=str(row_element or ""),
            accepted=True,
            files=[filename],
        )
        return [str(target)]

    def close(self) -> None:
        self._record("close", ok=True)


def _get_config() -> dict:
    """Load the hash-bound external LAF config for sealed V3 releases."""
    from magi_v3.external_inputs import load_bound_laf_config

    return load_bound_laf_config(
        MAGI_DIR,
        source_fallback=CONFIG_PATH,
    ).config


def _get_db_manager():
    """Get or create DatabaseManager from osc.py (lazy)."""
    global _db_manager
    # A disposable schedule fixture must never fall through to the workstation
    # database merely because the adapter does not need SQL. Adapters that do
    # require a database bind an explicit OSC_DB_HOST to their disposable DB.
    if (
        os.environ.get("MAGI_V3_REALISM_SANDBOX", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and os.environ.get("MAGI_LAF_WORKFLOW_PROVIDER_FIXTURE", "").strip()
    ):
        provider_fixture = _load_workflow_provider_fixture()
        allowed = (
            provider_fixture[1].get("allowed_workflows")
            if provider_fixture is not None
            else []
        )
        if set(allowed or []) == {"attachment_retry"}:
            logger.info("Skipping DB initialization for isolated attachment-retry fixture")
            return None
    if _db_manager is None:
        try:
            # Import DatabaseManager from MAGI root osc.py.
            # Must use absolute path because laf_orchestrator.py lives inside
            # casper_ecosystem/law_firm_orchestrators/ which has a local osc/
            # sub-package that shadows the root osc.py when using plain "from osc import".
            DatabaseManager = None
            _osc_root_py = os.path.join(MAGI_DIR, "osc.py")
            if os.path.exists(_osc_root_py):
                try:
                    import importlib.util as _ilu
                    _spec = _ilu.spec_from_file_location("osc_root", _osc_root_py)
                    _osc_mod = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_osc_mod)
                    DatabaseManager = _osc_mod.DatabaseManager
                except Exception as _e:
                    logger.warning("importlib osc.py load failed: %s", _e)
            if DatabaseManager is None:
                # Last-resort: use regular import (may get wrong osc/ package,
                # but legalbridge_core.DatabaseManager is type-incompatible)
                try:
                    from osc import DatabaseManager  # type: ignore[import]
                except (ImportError, AttributeError):
                    from legalbridge_core import DatabaseManager  # type: ignore[import]
            config = _get_config()

            prefer_local = (os.environ.get("MAGI_PREFER_LOCAL_DB", "").strip().lower() in {"1", "true", "yes", "on"})

            # Local first (when keeper/main DB is offline)
            if prefer_local:
                try:
                    from osc_headless.db import db_config_from_env
                    c = db_config_from_env()
                    _db_manager = DatabaseManager(
                        {"host": c.host, "port": int(c.port), "user": c.user, "password": c.password, "database": c.database}
                    )
                    logger.info("Connected to local DB first (MAGI_PREFER_LOCAL_DB=1): %s:%s/%s", c.host, c.port, c.database)
                    return _db_manager
                except Exception as e:
                    logger.warning("Local DB first attempt failed: %s", e)

            # Try MariaDB profiles from config.json
            for profile in config.get("mariadb_profiles", []):
                try:
                    _db_manager = DatabaseManager(profile["config"])
                    logger.info("Connected to DB: %s", profile["profile_name"])
                    break
                except Exception as e:
                    logger.warning("DB profile %s failed: %s", profile["profile_name"], e)

            # Direct OSC_DB_* env vars (bypasses osc_headless which may not be installed)
            if _db_manager is None:
                _db_host = os.environ.get("OSC_DB_HOST", "").strip()
                _db_port = int(os.environ.get("OSC_DB_PORT", "3306") or "3306")
                _db_user = os.environ.get("OSC_DB_USER", "").strip()
                _db_pass = os.environ.get("OSC_DB_PASSWORD", "").strip()
                _db_name = os.environ.get("OSC_DB_NAME", "law_firm_data").strip()
                if _db_host and _db_user:
                    try:
                        _db_manager = DatabaseManager(
                            {"host": _db_host, "port": _db_port, "user": _db_user, "password": _db_pass, "database": _db_name}
                        )
                        logger.info("Connected to DB via OSC_DB_* env: %s:%s/%s", _db_host, _db_port, _db_name)
                    except Exception as e:
                        logger.warning("OSC_DB_* env connection failed: %s", e)

            # Legacy osc_headless fallback
            if _db_manager is None:
                try:
                    from osc_headless.db import db_config_from_env
                    c = db_config_from_env()
                    _db_manager = DatabaseManager(
                        {"host": c.host, "port": int(c.port), "user": c.user, "password": c.password, "database": c.database}
                    )
                    logger.info("Connected to local DB via osc_headless: %s:%s/%s", c.host, c.port, c.database)
                except Exception as e:
                    logger.warning("osc_headless fallback failed: %s", e)

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
        self.laf_config["download_folder"] = str(laf_download_directory(MAGI_DIR))
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

        # Per-instance dedup set (avoids class-level shared mutable)
        self._go_live_dedup = set()

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
        from magi_v3.external_inputs import bound_shared_directory

        agent_dir = bound_shared_directory(
            MAGI_DIR,
            env_name="MAGI_AGENT_DIR",
            shared_leaf="agent",
            source_fallback=".agent",
        )
        mutable_static_dir = bound_shared_directory(
            MAGI_DIR,
            env_name="MAGI_MUTABLE_STATIC_DIR",
            shared_leaf="static",
            source_fallback="static",
        )
        self._portal_retry_state_path = agent_dir / "laf_pending_portal_downloads.json"
        self._portal_retry_lock_path = agent_dir / "laf_pending_portal_downloads.lock"
        # This lock protects only the short queue read-modify-write critical
        # section.  It is deliberately separate from the long-lived browser
        # cycle lock above so Gmail ingestion never waits for a portal session.
        self._portal_retry_state_lock_path = agent_dir / "laf_pending_portal_downloads.state.lock"
        self._portal_seed_skip_path = agent_dir / "laf_seed_permanently_skipped.json"
        self._portal_retry_heartbeat_path = mutable_static_dir / "laf_portal_retry_state.json"
        self._last_portal_retry_receipts: Dict[str, str] = {}

    @property
    def db(self):
        if self._db is None:
            self._db = _get_db_manager()
        return self._db

    @property
    def notifier(self):
        if self._notifier is None:
            # A dry-run may traverse validation branches which normally alert a
            # human. Keep its result observable in logs, but never deliver it.
            self._notifier = _DryRunNotifier() if self.dry_run else _get_notifier()
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

        # Proactive DB check: alert immediately if DB is unavailable at startup.
        # This prevents silent go_live failures later (case_number_generation_failed).
        if not self.dry_run:
            try:
                _db_probe = self.db
                if _db_probe is None:
                    logger.error("🚨 LAF Monitor: DatabaseManager 初始化失敗（self.db=None），go_live 將無法建立案號")
                    try:
                        self.notifier.notify_admin(
                            "🚨 LAF Gmail Monitor 啟動警告\n"
                            "DatabaseManager 初始化失敗（self.db=None）\n"
                            "這不是 DB 斷線，而是 _get_db_manager() import 鏈錯誤（osc 子包遮蔽問題）。\n"
                            "派案通知收到後 go_live 將無法產生案號、建立資料夾或寫入 DB。\n"
                            "請重啟 MAGI 或檢查 laf_orchestrator.py 的 importlib osc.py 路徑。"
                        )
                    except Exception as _ne:
                        logger.warning("DB=None startup alert send failed: %s", _ne)
                else:
                    logger.info("✅ LAF Monitor: DB 連線確認正常")
            except Exception as _dbe:
                logger.warning("DB probe at monitor start failed: %s", _dbe)

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
            processed_ids_file=str(get_laf_processed_emails_path()),
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

        # Portal retries are intentionally opt-in.  On restart this path can
        # touch multiple case folders and remote portal sessions at once; that
        # is too much surprise load for NAS/OSC during ordinary MAGI boot.
        _retry_on_start = str(os.environ.get("MAGI_LAF_PORTAL_RETRY_ON_START", "0")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        if _retry_on_start:
            # 注意：portal retry loop 使用獨立的 _PORTAL_RETRY_LOOP_INTERVAL（預設 3600s = 1 小時）
            # 而不是 Gmail check interval（300s = 5 分鐘），避免頻繁重試快速打爆 30 次上限
            try:
                self._ensure_pending_portal_retry_loop(interval_seconds=_PORTAL_RETRY_LOOP_INTERVAL)
            except Exception as e:
                logger.warning("Portal retry setup failed (non-fatal): %s", e)
        else:
            logger.info("Portal retry on startup disabled (set MAGI_LAF_PORTAL_RETRY_ON_START=1 to enable)")

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
        # A fixed ``.tmp`` name lets independent cron processes collide.  Use
        # a unique temporary file, fsync it, then atomically replace the queue.
        tmp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
        )
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            try:
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some filesystems do not support directory fsync.  The atomic
                # file replacement above remains the required correctness edge.
                pass
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _mutate_pending_portal_downloads(self, mutator) -> tuple[Any, Dict[str, dict]]:
        """Serialize a short queue mutation across Gmail and retry processes.

        The historical module lock only protected threads in one interpreter.
        Gmail dispatch and the portal retry cron are separate processes, so a
        stale whole-file save could otherwise erase a just-enqueued retry.
        """

        state_path = self._portal_retry_state_path
        lock_path = getattr(
            self,
            "_portal_retry_state_lock_path",
            state_path.with_name(f"{state_path.name}.state.lock"),
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _portal_retry_state_lock:
            with lock_path.open("a+", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    items = self._load_pending_portal_downloads()
                    result = mutator(items)
                    self._save_pending_portal_downloads(items)
                    persisted = self._load_pending_portal_downloads()
                    return result, persisted
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _merge_pending_portal_downloads(
        self,
        updates: Dict[str, dict] | None = None,
        *,
        remove_keys: List[str] | None = None,
    ) -> Dict[str, dict]:
        """Merge only touched case rows so unrelated concurrent rows survive."""

        normalized_updates = {
            str(key or "").strip(): dict(value or {})
            for key, value in (updates or {}).items()
            if str(key or "").strip()
        }
        normalized_removals = {
            str(key or "").strip() for key in (remove_keys or []) if str(key or "").strip()
        }

        def _apply(items: Dict[str, dict]) -> None:
            for key in normalized_removals:
                items.pop(key, None)
            for key, update in normalized_updates.items():
                current = dict(items.get(key) or {})
                current_token = str(current.get("queue_token") or "")
                update_token = str(update.get("queue_token") or "")
                if current_token and current_token != update_token:
                    # A newer Gmail event replaced the retry row after this
                    # worker took its snapshot.  Its deadline and receipt are
                    # authoritative; an old retry outcome must not erase them.
                    continue
                items[key] = dict(current, **update)

        _unused, persisted = self._mutate_pending_portal_downloads(_apply)
        return persisted

    def _write_portal_retry_heartbeat(
        self,
        *,
        status: str,
        interval_sec: int,
        pending_count: int | None = None,
        processed_count: int | None = None,
        error_type: str = "",
    ) -> None:
        path = self._portal_retry_heartbeat_path
        payload = {
            "ok": status not in {"error", "stopped"},
            "status": status,
            "enabled": True,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "interval_sec": max(60, int(interval_sec or 300)),
            "thread": "laf-portal-retry-loop",
            "pending_count": pending_count,
            "processed_count": processed_count,
            "error_type": str(error_type or "")[:80],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(path)
        except Exception:
            logger.debug("Unable to write LAF portal retry heartbeat", exc_info=True)

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
            # Check if the lock holder is still alive; if dead, reclaim
            try:
                lock_content = self._portal_retry_lock_path.read_text(encoding="utf-8").strip()
                stale_pid = int(lock_content.split("\n")[0])
                os.kill(stale_pid, 0)  # raises OSError if process dead
            except (OSError, ValueError):
                # Process is dead or PID unreadable — stale lock
                try:
                    self._portal_retry_lock_path.unlink()
                except Exception:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 472, exc_info=True)
                # Retry once
                try:
                    fd = os.open(str(self._portal_retry_lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(f"{os.getpid()}\n{datetime.now().isoformat()}\n")
                    return True
                except Exception:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 480, exc_info=True)
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

    def _find_authoritative_case_folder_by_identity(
        self,
        *,
        case_number: str = "",
        client_name: str = "",
        laf_case_number: str = "",
        prefer_closed: bool = False,
    ) -> str:
        """Find one durable case folder after an active-to-closed move.

        Portal retry rows can outlive the OSC row that originally supplied
        ``case_folder``.  In particular, a completed case may move from the
        active homes share to ``10_結案`` while the retry still points at a
        CloudStorage mirror.  Resolve that drift by the stable OSC case number
        and client/LAF identity, but fail closed when the result is ambiguous.
        """

        osc_no = str(case_number or "").strip()
        if re.fullmatch(r"20\d{2}-\d{4}", osc_no) is None:
            return ""
        client = str(client_name or "").strip()
        laf_no = str(laf_case_number or "").strip()
        roots = list(self._laf_case_roots())
        if prefer_closed:
            roots.sort(
                key=lambda value: (
                    "/03_工作資料/10_結案/" not in str(value).replace("\\", "/"),
                    str(value),
                )
            )

        matches: List[str] = []
        seen: set[str] = set()
        for root in roots:
            root_text = str(root or "").strip()
            if not root_text or not _is_dir_accessible(root_text):
                continue
            root_is_closed = (
                "/03_工作資料/10_結案/"
                in root_text.replace("\\", "/") + "/"
            )
            root_matches: List[str] = []
            for category in _safe_listdir(root_text):
                category_path = os.path.join(root_text, category)
                if not _is_dir_accessible(category_path):
                    continue
                for name in _safe_listdir(category_path):
                    if not str(name).startswith(f"{osc_no}-"):
                        continue
                    candidate = os.path.join(category_path, name)
                    if not _is_dir_accessible(candidate):
                        continue
                    guessed_client = self._guess_client_name_from_folder(candidate)
                    if client and guessed_client and guessed_client != client:
                        continue
                    if client and not guessed_client and client not in str(name):
                        continue
                    if not client and laf_no:
                        shallow_names: List[str] = []
                        for probe in (
                            candidate,
                            os.path.join(candidate, "01_法扶資料"),
                        ):
                            if _is_dir_accessible(probe):
                                shallow_names.extend(_safe_listdir(probe))
                        if not any(laf_no in value for value in shallow_names):
                            continue
                    authoritative = self._resolve_authoritative_case_folder_for_write(
                        candidate
                    )
                    if not authoritative:
                        continue
                    alias = self._case_folder_alias_key(authoritative)
                    if alias in seen:
                        continue
                    seen.add(alias)
                    root_matches.append(authoritative)
            if prefer_closed and root_is_closed:
                if len(root_matches) == 1:
                    return root_matches[0]
                if len(root_matches) > 1:
                    return ""
            matches.extend(root_matches)

        return matches[0] if len(matches) == 1 else ""

    def _resolve_case_folder_for_laf(
        self,
        laf_number: str,
        fallback: str = "",
        *,
        case_number: str = "",
        client_name: str = "",
        prefer_closed: bool = False,
    ) -> str:
        laf_case_no = str(laf_number or "").strip()
        resolved_case_number = str(case_number or "").strip()
        resolved_client_name = str(client_name or "").strip()
        candidate_paths: List[str] = []
        if laf_case_no and self.db:
            try:
                row = self.db.fetch_one(
                    "SELECT `case_number`, `client_name`, `status`, `folder_path` "
                    "FROM `cases` WHERE `legal_aid_number` = %s "
                    "ORDER BY `id` DESC LIMIT 1",
                    (laf_case_no,),
                    as_dict=True,
                )
                if row:
                    resolved_case_number = str(
                        row.get("case_number") or resolved_case_number
                    ).strip()
                    resolved_client_name = str(
                        row.get("client_name") or resolved_client_name
                    ).strip()
                    status = str(row.get("status") or "").strip().lower()
                    prefer_closed = prefer_closed or status in {
                        "已結案",
                        "closed",
                        "completed",
                        "archived",
                    }
                    candidate_paths.append(str(row.get("folder_path") or ""))
            except Exception as e:
                logger.warning("Resolve case folder by laf number failed (%s): %s", laf_case_no, e)
        candidate_paths.append(str(fallback or ""))

        first_local = ""
        for raw_path in candidate_paths:
            if not str(raw_path or "").strip():
                continue
            local = self._to_local_case_folder(str(raw_path))
            if local and not first_local:
                first_local = local
            authoritative = self._resolve_authoritative_case_folder_for_write(
                local or str(raw_path)
            )
            if authoritative:
                return authoritative

        moved = self._find_authoritative_case_folder_by_identity(
            case_number=resolved_case_number,
            client_name=resolved_client_name,
            laf_case_number=laf_case_no,
            prefer_closed=prefer_closed,
        )
        return moved or first_local

    def _current_laf_case_state(self, laf_number: str) -> dict:
        """Return the authoritative OSC state for one LAF case."""

        laf_case_no = str(laf_number or "").strip()
        if not laf_case_no or not self.db:
            return {}
        try:
            return self.db.fetch_one(
                "SELECT `case_number`, `status`, `legal_aid_status`, "
                "`legal_aid_approval_status`, `folder_path` "
                "FROM `cases` WHERE `legal_aid_number` = %s "
                "ORDER BY `id` DESC LIMIT 1",
                (laf_case_no,),
                as_dict=True,
            ) or {}
        except Exception as exc:
            logger.warning(
                "Resolve current LAF case state failed (%s): %s",
                laf_case_no,
                exc,
            )
            return {}

    @staticmethod
    def _opening_retry_is_resolved_by_case_state(item: dict, case_state: dict) -> bool:
        """Whether an old opening retry is obsolete according to OSC."""

        reason = str(
            item.get("origin_reason") or item.get("reason") or ""
        ).strip()
        legal_aid_status = str(case_state.get("legal_aid_status") or "").strip()
        return (
            reason
            in {
                "go_live",
                "portal_not_listed",
                "startup_backfill_missing_opening_docs",
            }
            and legal_aid_status not in {"", "未開辦"}
        )

    @staticmethod
    def _closing_retry_is_resolved_by_case_state(item: dict, case_state: dict) -> bool:
        """Whether a closing attachment retry is obsolete after LAF acceptance."""

        reason = str(
            item.get("origin_reason") or item.get("reason") or ""
        ).strip().lower()
        approval_status = str(
            case_state.get("legal_aid_approval_status") or ""
        ).strip()
        closing_trigger = any(
            token in reason
            for token in (
                "closing",
                "laf_closing",
                "結案",
                "酬金",
            )
        )
        # A result-download email is itself proof that a new portal artefact may
        # be available.  Acceptance/transfer state alone cannot prove that the
        # artefact was downloaded, so that route must retain its retry.
        if any(token in reason for token in ("review_result", "result_download")):
            return False
        return closing_trigger and approval_status in {
            "待轉入",
            "已補件待轉入",
            "已轉入",
        }

    @staticmethod
    def _portal_retry_item_is_pending(item: dict) -> bool:
        status = str(item.get("status") or "pending_retry").strip().lower()
        if status in {"", "pending_retry"}:
            expires_at = str(item.get("expires_at") or "").strip()
            if expires_at:
                try:
                    current, expiry = LAFOrchestrator._portal_retry_expiry_pair(
                        expires_at
                    )
                    return current < expiry
                except (TypeError, ValueError):
                    logger.warning(
                        "invalid LAF portal retry expiry timestamp: %r", expires_at,
                        exc_info=True,
                    )
            return True
        return status == "manual_review" and str(item.get("last_error") or "") == "missing_local_case_folder"

    @staticmethod
    def _portal_retry_expiry_pair(
        expires_at: str,
        *,
        now: Optional[datetime] = None,
    ) -> Tuple[datetime, datetime]:
        """Return comparable local instants for legacy and timezone-aware rows.

        Historical retry rows store local naive timestamps, while newer Gmail
        receipts can carry an explicit offset.  Python intentionally refuses
        to compare those shapes directly.  Preserve the legacy local-time
        meaning for naive rows and compare aware rows in their declared zone.
        """

        expiry = datetime.fromisoformat(str(expires_at or "").strip())
        expiry_is_aware = expiry.tzinfo is not None and expiry.utcoffset() is not None
        current = now
        if current is None:
            current = datetime.now(expiry.tzinfo) if expiry_is_aware else datetime.now()
        current_is_aware = (
            current.tzinfo is not None and current.utcoffset() is not None
        )
        if expiry_is_aware:
            if not current_is_aware:
                current = current.astimezone()
            current = current.astimezone(expiry.tzinfo)
        elif current_is_aware:
            current = current.astimezone().replace(tzinfo=None)
        return current, expiry

    @staticmethod
    def _portal_retry_expired(item: dict, *, now: Optional[datetime] = None) -> bool:
        expires_at = str(item.get("expires_at") or "").strip()
        if not expires_at:
            return False
        try:
            current, expiry = LAFOrchestrator._portal_retry_expiry_pair(
                expires_at,
                now=now,
            )
            return current >= expiry
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _portal_retry_token_for_trigger(trigger_id: str) -> str:
        """Return the privacy-safe receipt shared by queue and clear paths."""

        trigger_value = str(trigger_id or "").strip()
        if not trigger_value:
            return ""
        return hashlib.sha256(
            f"laf-portal-retry-v1\0{trigger_value}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _portal_retry_token_for_legacy_item(laf_case_no: str, item: dict) -> str:
        """Give a pre-receipt queue row an exact, stable migration receipt."""

        material = "\0".join(
            [
                "laf-portal-retry-legacy-v1",
                str(laf_case_no or "").strip(),
                str(item.get("first_queued_at") or "").strip(),
                str(item.get("origin_reason") or item.get("reason") or "").strip(),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

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
        trigger_id: str = "",
        trigger_received_at=None,
    ) -> bool:
        laf_case_no = str(laf_number or "").strip()
        if not laf_case_no:
            return False

        now_iso = datetime.now().isoformat(timespec="seconds")
        trigger_token = self._portal_retry_token_for_trigger(trigger_id)
        event_received_at = ""
        if trigger_received_at:
            try:
                observed_at = trigger_received_at
                if not isinstance(observed_at, datetime):
                    observed_at = datetime.fromisoformat(
                        str(observed_at).strip().replace("Z", "+00:00")
                    )
                if observed_at.tzinfo is not None and observed_at.utcoffset() is not None:
                    observed_at = observed_at.astimezone().replace(tzinfo=None)
                event_received_at = observed_at.isoformat(timespec="seconds")
            except (TypeError, ValueError):
                logger.warning("Invalid portal trigger timestamp; using queue time")
        persisted_item: dict = {}

        def _enqueue(items: Dict[str, dict]) -> None:
            nonlocal persisted_item
            item = dict(items.get(laf_case_no) or {})

            # 防止已終止的歷史項目被 startup backfill 重複入隊。
            # 新的信件觸發（例如 review_result_download）仍可重開一個
            # 新的下載觀察期；但庫存回補不能讓已過期舊案永久循環。
            existing_status = str(item.get("status") or "").strip().lower()
            existing_tries = int(item.get("tries") or 0)
            effective_reason = str(reason or item.get("reason") or "portal_not_listed").strip()
            inventory_backfill = any(
                token in effective_reason.lower()
                for token in ("startup_backfill", "inventory_backfill")
            )
            new_trigger = bool(
                trigger_token and trigger_token != str(item.get("queue_token") or "")
            )
            logically_expired = bool(item and self._portal_retry_expired(item))
            if (
                not new_trigger
                and (
                    existing_status == "exhausted"
                    or existing_tries >= _PORTAL_RETRY_MAX_TRIES
                    or (
                        inventory_backfill
                        and existing_status in {"expired", "archived"}
                    )
                )
            ):
                logger.debug(
                    "Skip re-queuing terminal item %s (tries=%d, status=%s, reason=%s)",
                    laf_case_no, existing_tries, existing_status,
                    effective_reason,
                )
                persisted_item = {}
                return

            if (
                not inventory_backfill
                and (
                    new_trigger
                    or (
                        not trigger_token
                        and (
                            existing_status in {"expired", "archived"}
                            or logically_expired
                        )
                    )
                )
            ):
                # 真正的新信件代表新的附件事件，必須重新計算
                # 觀察與保存期。特別注意：舊列可能仍標成
                # pending_retry，但 expires_at 已過；不可因為狀態字串
                # 還沒被夜間對帳改寫就沿用過期時間。
                item = {}
                existing_tries = 0

            first_queued_at = str(item.get("first_queued_at") or now_iso)
            persisted_event_received_at = str(
                item.get("event_received_at") or event_received_at
            ).strip()
            first_observed_at = str(
                item.get("first_observed_at")
                or persisted_event_received_at
                or first_queued_at
            )
            expires_at = str(item.get("expires_at") or "").strip()
            if not expires_at:
                try:
                    expires_at = (
                        datetime.fromisoformat(first_observed_at)
                        + timedelta(days=_PORTAL_ATTACHMENT_RETENTION_DAYS)
                    ).isoformat(timespec="seconds")
                except (TypeError, ValueError):
                    expires_at = (
                        datetime.now() + timedelta(days=_PORTAL_ATTACHMENT_RETENTION_DAYS)
                    ).isoformat(timespec="seconds")
            item.update(
                {
                    "laf_case_number": laf_case_no,
                    "client_name": str(client_name or item.get("client_name") or "").strip(),
                    "case_type": str(case_type or item.get("case_type") or "").strip(),
                    "case_reason": str(case_reason or item.get("case_reason") or "").strip(),
                    "case_folder": str(case_folder or item.get("case_folder") or "").strip(),
                    "case_number": str(case_number or item.get("case_number") or "").strip(),
                    "status": "pending_retry",
                    "reason": effective_reason,
                    "last_error": str(last_error or item.get("last_error") or "").strip(),
                    "first_queued_at": first_queued_at,
                    "first_observed_at": first_observed_at,
                    "expires_at": expires_at,
                    "updated_at": now_iso,
                }
            )
            if trigger_token:
                item["queue_token"] = trigger_token
            elif not item.get("queue_token"):
                item["queue_token"] = hashlib.sha256(
                    f"laf-portal-retry-v1\0{laf_case_no}\0{now_iso}\0{time.time_ns()}".encode("utf-8")
                ).hexdigest()
            if persisted_event_received_at:
                item["event_received_at"] = persisted_event_received_at
            if effective_reason and effective_reason not in {"portal_not_listed", "portal_check_failed", "login_failed"}:
                item["origin_reason"] = effective_reason
            else:
                item.setdefault("origin_reason", str(item.get("origin_reason") or item.get("reason") or "").strip())
            item.setdefault("tries", 0)
            item.setdefault("last_try_at", "")
            items[laf_case_no] = item
            persisted_item = dict(item)

        _unused, persisted = self._mutate_pending_portal_downloads(_enqueue)
        observed = dict(persisted.get(laf_case_no) or {})
        expected_token = str(persisted_item.get("queue_token") or "")
        durable = bool(
            persisted_item
            and expected_token
            and str(observed.get("queue_token") or "") == expected_token
            and self._portal_retry_item_is_pending(observed)
        )
        receipts = getattr(self, "_last_portal_retry_receipts", None)
        if not isinstance(receipts, dict):
            receipts = {}
            self._last_portal_retry_receipts = receipts
        if durable:
            receipts[laf_case_no] = expected_token
        else:
            receipts.pop(laf_case_no, None)

        _eventlog(
            "laf:portal:retry:queued",
            ok=durable,
            payload={
                "reason": observed.get("reason"),
                "case_folder": os.path.basename(str(observed.get("case_folder") or "").rstrip("/\\")),
                "durable": durable,
            },
            tags={"laf_case_no": laf_case_no, "client_name": str(observed.get("client_name") or "")},
        )
        return durable

    @staticmethod
    def _evidence_paths_since(paths: List[str], evidence_after: str = "") -> List[str]:
        """Keep evidence created after a mail-triggered retry was queued.

        Startup/backfill reconciliation may use any existing file.  A specific
        result-download email, however, must not be satisfied by an older
        closing document from an earlier workflow.
        """

        threshold_raw = str(evidence_after or "").strip()
        if not threshold_raw:
            return [str(path) for path in (paths or []) if path]
        try:
            threshold = datetime.fromisoformat(threshold_raw).timestamp() - 2.0
        except (TypeError, ValueError, OSError):
            return []
        current: List[str] = []
        for path in paths or []:
            candidate = str(path or "").strip()
            if not candidate:
                continue
            try:
                if os.path.getmtime(candidate) >= threshold:
                    current.append(candidate)
            except OSError:
                continue
        return current

    def _clear_pending_portal_download(
        self,
        laf_number: str,
        *,
        expected_queue_token: str = "",
        successful_evidence_at=None,
    ) -> None:
        target_laf_case_no = str(laf_number or "").strip()
        if not target_laf_case_no:
            return
        expected_token = str(expected_queue_token or "").strip()
        def _clear(items: Dict[str, dict]) -> None:
            now = datetime.now()
            for queued_laf_case_no, raw_item in list(items.items()):
                item = dict(raw_item or {})
                if (
                    str(item.get("status") or "pending_retry").strip().lower()
                    in {"", "pending_retry"}
                    and self._portal_retry_expired(item, now=now)
                ):
                    # 官網保存期屆滿後已無可自動執行的動作。保留
                    # 稽核證據，但歸檔為歷史狀態，不再要求使用者
                    # 每小時重複確認。
                    item["status"] = "archived"
                    item["last_error"] = "portal_attachment_retention_expired"
                    item["resolution_reason"] = "portal_retention_expired_archived"
                    item["updated_at"] = now.isoformat(timespec="seconds")
                    items[queued_laf_case_no] = item
                    _eventlog(
                        "laf:portal:retry:expired",
                        ok=False,
                        payload={
                            "first_observed_at": item.get("first_observed_at"),
                            "expires_at": item.get("expires_at"),
                        },
                        tags={
                            "laf_case_no": queued_laf_case_no,
                            "client_name": str(item.get("client_name") or ""),
                        },
                    )
            current = dict(items.get(target_laf_case_no) or {})
            token_matches = bool(
                expected_token
                and str(current.get("queue_token") or "") == expected_token
            )
            evidence_covers_current = self._portal_retry_success_covers_row(
                current, successful_evidence_at
            )
            if token_matches or evidence_covers_current:
                items.pop(target_laf_case_no, None)

        self._mutate_pending_portal_downloads(_clear)

    @staticmethod
    def _portal_retry_success_covers_row(item: dict, evidence_at) -> bool:
        """Whether a successful download is at least as new as the queue row."""

        if not isinstance(item, dict) or not item or not evidence_at:
            return False
        observed_raw = str(
            item.get("event_received_at")
            or item.get("first_observed_at")
            or item.get("first_queued_at")
            or ""
        ).strip()
        if not observed_raw:
            return False
        try:
            observed = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
            evidence = (
                evidence_at
                if isinstance(evidence_at, datetime)
                else datetime.fromisoformat(str(evidence_at).strip().replace("Z", "+00:00"))
            )
        except (TypeError, ValueError):
            return False
        observed_aware = observed.tzinfo is not None and observed.utcoffset() is not None
        evidence_aware = evidence.tzinfo is not None and evidence.utcoffset() is not None
        if observed_aware and not evidence_aware:
            evidence = evidence.astimezone(observed.tzinfo)
        elif evidence_aware and not observed_aware:
            evidence = evidence.astimezone().replace(tzinfo=None)
        elif observed_aware and evidence_aware:
            evidence = evidence.astimezone(observed.tzinfo)
        return observed <= evidence

    # ── Seed permanent skip list ──

    def _load_seed_skip_list(self) -> set:
        """載入永久跳過清單（exhausted 後不再 seed 入佇列）。"""
        try:
            with open(self._portal_seed_skip_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data.get("skipped", []))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def _add_to_seed_skip_list(self, laf_case_no: str, reason: str = "") -> None:
        skipped = self._load_seed_skip_list()
        skipped.add(str(laf_case_no or "").strip())
        try:
            os.makedirs(os.path.dirname(self._portal_seed_skip_path), exist_ok=True)
            with open(self._portal_seed_skip_path, "w", encoding="utf-8") as f:
                json.dump({
                    "skipped": sorted(skipped),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to write seed skip list: %s", e)

    def _archive_portal_downloads(self, files: List[str], case_folder: str) -> dict:
        result = {
            "ok": False,
            "new_files": [],
            "skipped_existing": [],
            "zip_backups": [],
            "zip_backup_skipped": [],
            "error": "",
        }
        folder = self._resolve_authoritative_case_folder_for_write(case_folder)
        if not folder:
            result["error"] = "authoritative_case_storage_unavailable"
            return result
        try:
            from skills.legal.laf import OSCCaseCreator

            archiver = OSCCaseCreator(
                # File classification/archiving does not need a database.
                # Avoid opening a production DB connection from the isolated
                # retry worker merely to move already-downloaded files.
                db_manager=self._db,
                # The folder was resolved and verified above. Reuse it as the
                # archiver root so construction cannot fall back to creating
                # ./法扶資料 inside a sealed/read-only release directory.
                target_folder=folder,
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
        trigger_reason: str = "",
        trigger_id: str = "",
        trigger_received_at=None,
        expected_queue_token: str = "",
    ) -> dict:
        laf_case_no = str(laf_number or "").strip()
        portal_files = [str(f) for f in (files or []) if f]
        folder = self._resolve_case_folder_for_laf(laf_case_no, fallback=case_folder)
        folder = self._resolve_authoritative_case_folder_for_write(folder)
        result = {
            "ok": True,
            "laf_case_number": laf_case_no,
            "source": str(source or "initial"),
            "downloaded_files": portal_files,
            "downloaded_count": len(portal_files),
            "case_folder": folder,
            "retry_queued": False,
            "retry_reason": "",
            "retry_queue_token": "",
            "error": "",
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
                    trigger_id=trigger_id,
                    trigger_received_at=trigger_received_at,
                )
                result["retry_queued"] = queued
                result["retry_reason"] = "archive_failed"
                result["retry_queue_token"] = str(
                    getattr(self, "_last_portal_retry_receipts", {}).get(laf_case_no) or ""
                )
                if not queued:
                    result["ok"] = False
                    result["error"] = "portal_retry_queue_not_durable"
                _eventlog(
                    "laf:portal:download:done",
                    ok=False,
                    payload={"error": str(archived.get("error") or "archive_failed")[:300]},
                    tags={"laf_case_no": laf_case_no, "client_name": client_name},
                )
                return result
            clear_token = str(expected_queue_token or "").strip()
            if not clear_token:
                clear_token = self._portal_retry_token_for_trigger(trigger_id)
            self._clear_pending_portal_download(
                laf_case_no,
                expected_queue_token=clear_token,
                successful_evidence_at=trigger_received_at,
            )
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
            if (
                archived.get("new_files")
                and re.fullmatch(r"20\d{2}-\d{4}", str(case_number or "").strip())
            ):
                try:
                    from magi_v3.business_events import emit_case_evidence_event

                    event_source = hashlib.sha256(
                        json.dumps(
                            archived.get("new_files") or archived.get("skipped_existing") or [],
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ).encode("utf-8", errors="replace")
                    ).hexdigest()
                    emit_case_evidence_event(
                        domain="laf",
                        case_number=str(case_number).strip(),
                        source=event_source,
                        evidence_kind="laf_portal_download",
                    )
                except Exception:
                    logger.warning("case evidence event could not be queued", exc_info=True)
            return result

        retry_reason = str(trigger_reason or "").strip() or "portal_not_listed"
        if last_error and not trigger_reason:
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
            trigger_id=trigger_id,
            trigger_received_at=trigger_received_at,
        )
        result["retry_queued"] = queued
        result["retry_reason"] = retry_reason
        result["retry_queue_token"] = str(
            getattr(self, "_last_portal_retry_receipts", {}).get(laf_case_no) or ""
        )
        if not queued:
            result["ok"] = False
            result["error"] = "portal_retry_queue_not_durable"
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
            owner._write_portal_retry_heartbeat(status="starting", interval_sec=every_sec)
            _consecutive_fails = 0
            _admin_notified = False
            try:
                owner._seed_pending_portal_retries_from_case_inventory(limit=80)
            except Exception as e:
                logger.warning("Portal retry background seed failed (non-fatal): %s", e)
            initial_delay = _portal_retry_initial_delay_seconds(every_sec)
            if initial_delay:
                logger.info("LAF portal retry first cycle delayed by %ss after startup", initial_delay)
                owner._write_portal_retry_heartbeat(status="idle", interval_sec=every_sec)
                remaining = initial_delay
                while remaining > 0:
                    step = min(60, remaining)
                    time.sleep(step)
                    remaining -= step
                    owner._write_portal_retry_heartbeat(status="idle", interval_sec=every_sec)
            while True:  # daemon=True ensures cleanup on process exit; no explicit shutdown needed
                try:
                    result = owner._run_pending_portal_retry_cycle_with_watchdog(
                        max_items=6,
                        interval_sec=every_sec,
                        timeout_sec=_PORTAL_RETRY_CYCLE_TIMEOUT,
                    )
                    _consecutive_fails = 0
                    _admin_notified = False
                    owner._write_portal_retry_heartbeat(
                        status="ok",
                        interval_sec=every_sec,
                        pending_count=int(result.get("scanned") or result.get("pending") or 0) if isinstance(result, dict) else None,
                        processed_count=int(result.get("processed") or 0) if isinstance(result, dict) else None,
                    )
                except Exception as e:
                    _consecutive_fails += 1
                    logger.error("Pending portal retry loop failed (consecutive=%d): %s", _consecutive_fails, e)
                    owner._write_portal_retry_heartbeat(
                        status="error",
                        interval_sec=every_sec,
                        error_type=type(e).__name__,
                    )
                    if isinstance(e, _PortalRetryCycleTimeout):
                        logger.critical("LAF portal retry cycle timed out; restarting MAGI for recovery")
                        try:
                            owner.notifier.notify_admin(
                                "LAF 附件重試逾時，MAGI 將自動重啟後繼續處理。",
                                topic_key="laf_dispatch",
                            )
                        except Exception:
                            logger.warning("Unable to notify portal retry timeout", exc_info=True)
                        owner._restart_after_portal_retry_timeout()
                        return
                    if _consecutive_fails >= 10 and not _admin_notified:
                        _admin_notified = True
                        try:
                            from api.discord_channel_router import send as _dc_send
                            _dc_send("admin", f"🚨 LAF portal retry loop 連續 {_consecutive_fails} 次失敗，請檢查: {str(e)[:200]}")
                        except Exception:
                            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 774, exc_info=True)
                # Exponential backoff on failures, capped at 1 hour
                _sleep = min(every_sec * (2 ** min(_consecutive_fails, 4)), 3600) if _consecutive_fails else every_sec
                remaining = max(60, int(_sleep))
                while remaining > 0:
                    step = min(60, remaining)
                    time.sleep(step)
                    remaining -= step
                    if not _consecutive_fails:
                        owner._write_portal_retry_heartbeat(status="idle", interval_sec=every_sec)

        _portal_retry_thread = threading.Thread(
            target=_loop,
            args=(self, interval),
            daemon=True,
            name="laf-portal-retry-loop",
        )
        _portal_retry_thread.start()

    def _run_pending_portal_retry_cycle_with_watchdog(
        self,
        *,
        max_items: int,
        interval_sec: int,
        timeout_sec: int,
    ) -> dict:
        """Run one retry cycle while keeping its heartbeat observable."""
        outcome: dict = {}
        finished = threading.Event()

        def _run() -> None:
            try:
                outcome["result"] = self._retry_pending_portal_downloads(max_items=max_items)
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                finished.set()

        worker = threading.Thread(
            target=_run,
            daemon=True,
            name="laf-portal-retry-cycle",
        )
        worker.start()
        deadline = time.monotonic() + max(1, int(timeout_sec or 1))
        self._write_portal_retry_heartbeat(status="running", interval_sec=interval_sec)

        while not finished.wait(timeout=min(60, max(0.1, deadline - time.monotonic()))):
            self._write_portal_retry_heartbeat(status="running", interval_sec=interval_sec)
            if time.monotonic() >= deadline:
                raise _PortalRetryCycleTimeout(f"portal retry cycle exceeded {int(timeout_sec)} seconds")

        error = outcome.get("error")
        if error is not None:
            raise error
        result = outcome.get("result")
        return result if isinstance(result, dict) else {"ok": False, "error": "invalid_retry_result"}

    def run_portal_retry_once(
        self,
        *,
        max_items: int = 6,
        timeout_sec: int = _PORTAL_RETRY_CYCLE_TIMEOUT,
        interval_sec: int = _PORTAL_RETRY_LOOP_INTERVAL,
    ) -> dict:
        """Run one bounded attachment retry cycle without a resident monitor.

        Gmail dispatch and portal attachment recovery have different resource
        profiles.  Keeping this as a scheduled one-shot means disabling the
        resident Gmail monitor cannot silently disable attachment recovery.
        """
        interval = max(60, int(interval_sec or _PORTAL_RETRY_LOOP_INTERVAL))
        self._write_portal_retry_heartbeat(
            status="starting",
            interval_sec=interval,
        )
        try:
            result = self._run_pending_portal_retry_cycle_with_watchdog(
                max_items=max(1, int(max_items or 1)),
                interval_sec=interval,
                timeout_sec=max(30, int(timeout_sec or _PORTAL_RETRY_CYCLE_TIMEOUT)),
            )
            ok = bool(result.get("ok"))
            self._write_portal_retry_heartbeat(
                status="ok" if ok else "error",
                interval_sec=interval,
                pending_count=int(result.get("scanned") or result.get("pending") or 0),
                processed_count=int(result.get("processed") or 0),
                error_type="" if ok else str(result.get("error") or "retry_failed"),
            )
            return result
        except BaseException as exc:
            self._write_portal_retry_heartbeat(
                status="error",
                interval_sec=interval,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            self.close()

    def _restart_after_portal_retry_timeout(self) -> None:
        """Exit so the launch supervisor can recreate all browser threads cleanly."""
        os._exit(75)

    def _seed_pending_portal_retries_from_case_inventory(self, limit: int = 80) -> dict:
        if self.dry_run or not self.db:
            return {"ok": True, "seeded": 0, "scanned": 0, "skipped": "dry_run_or_no_db"}

        # Legacy seed skip files were keyed only by LAF case number.  They are
        # intentionally ignored: a later Gmail/portal event for the same case
        # must get a fresh trigger-bound queue token.  Terminal retry state is
        # already protected by the per-event queue token below.

        query = """
            SELECT `case_number`, `client_name`, `case_type`, `case_reason`,
                   `legal_aid_number`, `folder_path`, `status`,
                   `legal_aid_status`, `created_date`
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
            _created_date_raw = None
            if isinstance(row, dict):
                case_number = str(row.get("case_number") or "").strip()
                client_name = str(row.get("client_name") or "").strip()
                case_type = str(row.get("case_type") or "").strip()
                case_reason = str(row.get("case_reason") or "").strip()
                laf_case_no = str(row.get("legal_aid_number") or "").strip()
                folder_path = str(row.get("folder_path") or "").strip()
            else:
                (
                    case_number,
                    client_name,
                    case_type,
                    case_reason,
                    laf_case_no,
                    folder_path,
                    _status,
                    _laf_status,
                    _created,
                ) = row
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
            # ── 消費者債務清理 case_reason 正規化 ─────────────────────────────
            # 與 handle_go_live() / _create_case_record() 相同邏輯：
            # email 原始文字「消費者債務清理程序」在 DB 中有時尚未被正規化，
            # 資料夾名稱卻已由 laf_folder_builder 寫成「更生」，導致路徑不符。
            if case_type == '消費者債務清理' or '消費者債務清理' in case_reason:
                if '清算' not in case_reason:
                    case_reason = '更生'
            # ── 資料夾路徑同步正規化 ─────────────────────────────────────────
            # 若 DB folder_path 的 basename 仍含舊名（消費者債務清理程序），
            # 但 NAS 實際資料夾已用正規化後的名稱（更生），需修正 basename 才能
            # 讓 _resolve_case_folder_with_fallback() 找到真實目錄。
            if folder_path and '消費者債務清理程序' in folder_path:
                folder_path = folder_path.replace('消費者債務清理程序', case_reason)
            status_norm = status_value.strip().lower()
            is_closed = status_norm in {"已結案", "closed", "completed", "archived"}
            if isinstance(row, dict):
                _laf_status_raw = str(row.get("legal_aid_status") or "").strip()
                _created_date_raw = row.get("created_date")
            else:
                _laf_status_raw = str(_laf_status or "").strip()
                _created_date_raw = _created

            # OSC workflow state is authoritative.  Once a case is already
            # active, do not inspect stale/missing sync roots and do not seed an
            # opening-document retry.
            if (not is_closed) and _laf_status_raw not in {"", "未開辦"}:
                continue

            local_folder = self._to_local_case_folder(folder_path)
            resolved_folder = (
                self._resolve_case_folder_with_fallback(local_folder)
                if local_folder
                else ""
            )
            if resolved_folder:
                local_folder = resolved_folder
            docs = self._scan_case_folder_docs(
                local_folder,
                action="closing" if is_closed else "go_live",
            )
            if is_closed and self._is_legacy_closed_archive_path(folder_path) and (not local_folder or not os.path.isdir(local_folder)):
                _eventlog(
                    "laf:portal:retry:seed_skipped",
                    ok=False,
                    payload={"reason": "missing_local_case_folder", "folder_path": folder_path},
                    tags={"laf_case_no": laf_case_no, "client_name": client_name},
                )
                continue
            if is_closed:
                # 只要 closing_fee_files（酬金/律師費收據）有了就代表結案文件已下載，不再重試。
                # change_review_notice_files（變更審查通知）不是所有案件都有，
                # 不能作為強制條件，否則沒有該文件的案件會無限觸發無效下載。
                queue_reason = "startup_backfill_missing_closing_docs"
                needs_queue = not docs.get("closing_fee_files")
                if needs_queue and local_folder:
                    # 「已轉入／結案通知」也是完整流程的正式完成證據。
                    # retry reconciliation 會把既有項目標成 done；seed 端也必須
                    # 使用同一套判斷，否則下一輪 inventory scan 會立刻把它
                    # 從 done 重新改回 pending_retry，造成永久假紅燈。
                    try:
                        satisfied, _resolution_reason = self._nas_satisfies_trigger(
                            queue_reason,
                            local_folder,
                        )
                    except Exception:
                        satisfied = False
                    if satisfied:
                        needs_queue = False
            else:
                # 建案未滿 24 小時的新案，portal 可能尚未備妥文件，跳過 backfill
                # 讓正常的 go-live 流程（Gmail monitor → handle_go_live）在 portal 就緒後再下載
                if _created_date_raw:
                    try:
                        _cd = _created_date_raw if isinstance(_created_date_raw, datetime) else datetime.strptime(str(_created_date_raw)[:19], "%Y-%m-%d %H:%M:%S")
                        _age_hours = (datetime.now() - _cd).total_seconds() / 3600
                        if _age_hours < 24:
                            logger.debug("Skip backfill for new case %s (age=%.1fh < 24h)", laf_case_no, _age_hours)
                            continue
                        # 建案超過 90 天的案件，portal 文件很可能已過期或不再提供
                        if _age_hours > 90 * 24:
                            continue
                    except (ValueError, TypeError):
                        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 897, exc_info=True)
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

        # Reconcile every unresolved item before deciding whether it may still
        # contact the portal.  Expired items must also pass through this step:
        # an attachment already present on NAS, or a closing report already
        # accepted/transferred by LAF, is a completed workflow rather than a
        # fault requiring manual intervention.
        reconciled: List[dict] = []
        queue_changed = False
        queue_updates: Dict[str, dict] = {}
        for laf_case_no, raw_item in list(items.items()):
            item = dict(raw_item or {})
            status = str(item.get("status") or "pending_retry").strip().lower()
            if status == "done":
                origin = str(item.get("origin_reason") or item.get("reason") or "").lower()
                local_resolution = str(item.get("resolution_reason") or "") in {
                    "nas_has_closing_fee",
                    "nas_has_change_review_notice",
                    "nas_has_closing_docs",
                    "local_attachment_exists",
                }
                if not (
                    local_resolution
                    and any(token in origin for token in ("review_result", "result_download"))
                ):
                    continue
                # Revalidate legacy completions under the strict, time-bound
                # result-download rule.  False historical completions are
                # reopened instead of remaining permanently invisible.
                local_case_folder = self._resolve_case_folder_for_laf(
                    laf_case_no,
                    fallback=str(item.get("case_folder") or ""),
                    case_number=str(item.get("case_number") or ""),
                    client_name=str(item.get("client_name") or ""),
                    prefer_closed="結案" in str(item.get("case_reason") or ""),
                )
                satisfied, _reason = self._nas_satisfies_trigger(
                    origin,
                    local_case_folder,
                    evidence_after=str(
                        item.get("event_received_at")
                        or item.get("first_observed_at")
                        or item.get("first_queued_at")
                        or ""
                    ),
                )
                if satisfied:
                    continue
                item["status"] = "pending_retry"
                item["resolution_reason"] = ""
                item["reopened_reason"] = "invalid_legacy_result_evidence"
                item["updated_at"] = datetime.now().isoformat(timespec="seconds")
                items[laf_case_no] = item
                queue_changed = True
                queue_updates[laf_case_no] = dict(item)
                status = "pending_retry"
            if not str(item.get("queue_token") or "").strip():
                item["queue_token"] = self._portal_retry_token_for_legacy_item(
                    laf_case_no,
                    item,
                )
                items[laf_case_no] = item
                queue_changed = True
                queue_updates[laf_case_no] = dict(item)
            current_case = self._current_laf_case_state(laf_case_no)
            resolution_reason = ""
            if self._opening_retry_is_resolved_by_case_state(item, current_case):
                resolution_reason = "db_laf_status_active"
            else:
                raw_case_folder = str(
                    item.get("case_folder")
                    or current_case.get("folder_path")
                    or ""
                ).strip()
                local_case_folder = self._resolve_case_folder_for_laf(
                    laf_case_no,
                    fallback=raw_case_folder,
                    case_number=str(
                        item.get("case_number")
                        or current_case.get("case_number")
                        or ""
                    ),
                    client_name=str(item.get("client_name") or ""),
                    prefer_closed=(
                        "結案" in str(item.get("case_reason") or "")
                        or str(current_case.get("status") or "").strip().lower()
                        in {"已結案", "closed", "completed", "archived"}
                    ),
                )
                if local_case_folder:
                    item["case_folder"] = local_case_folder
                    try:
                        satisfied, local_reason = self._nas_satisfies_trigger(
                            str(item.get("origin_reason") or item.get("reason") or ""),
                            local_case_folder,
                            evidence_after=str(
                                item.get("event_received_at")
                                or item.get("first_observed_at")
                                or item.get("first_queued_at")
                                or ""
                            ),
                        )
                    except Exception:
                        satisfied, local_reason = False, ""
                    if satisfied:
                        resolution_reason = local_reason or "local_attachment_exists"
                if (
                    not resolution_reason
                    and self._closing_retry_is_resolved_by_case_state(
                        item, current_case
                    )
                ):
                    resolution_reason = "laf_closing_accepted"
            if not resolution_reason and status == "expired":
                # 舊版本留下的 expired 項目已無法從官網補抓。
                # 經過上方 NAS/DB 對帳後仍無完成證據時，保留
                # 歷史記錄即可，不得繼續當成即時故障或重新入隊。
                item["status"] = "archived"
                item["last_error"] = "portal_attachment_retention_expired"
                item["updated_at"] = datetime.now().isoformat(timespec="seconds")
                item["resolution_reason"] = "portal_retention_expired_archived"
                items[laf_case_no] = item
                queue_changed = True
                queue_updates[laf_case_no] = dict(item)
                reconciled.append(
                    {
                        "laf_case_number": laf_case_no,
                        "downloaded_count": 0,
                        "status": "archived",
                        "reason": "portal_retention_expired_archived",
                    }
                )
                continue
            if not resolution_reason:
                items[laf_case_no] = item
                continue
            item["status"] = "done"
            item["last_error"] = ""
            item["updated_at"] = datetime.now().isoformat(timespec="seconds")
            item["resolution_reason"] = resolution_reason
            item["reopened_reason"] = ""
            items[laf_case_no] = item
            queue_changed = True
            queue_updates[laf_case_no] = dict(item)
            reconciled.append(
                {
                    "laf_case_number": laf_case_no,
                    "downloaded_count": 0,
                    "status": "done",
                    "reason": resolution_reason,
                }
            )
        if queue_changed:
            self._merge_pending_portal_downloads(queue_updates)

        pending_items = [
            dict(item)
            for item in items.values()
            if str(item.get("laf_case_number") or "").strip()
            and self._portal_retry_item_is_pending(item)
        ]
        if not pending_items:
            return {
                "ok": True,
                "scanned": len(items),
                "processed": len(reconciled),
                "items": reconciled,
            }

        if not self._acquire_pending_portal_retry_lock():
            return {"ok": True, "skipped": True, "reason": "locked", "pending": len(pending_items)}

        processed: List[dict] = list(reconciled)
        try:
            ordered = sorted(
                pending_items,
                key=lambda item: (
                    str(item.get("last_try_at") or ""),
                    str(item.get("first_queued_at") or ""),
                    str(item.get("laf_case_number") or ""),
                ),
            )[: max(1, int(max_items or 1))]

            # Clear obsolete opening retries before creating a browser
            # session.  OSC is authoritative for workflow state; an item that
            # is already 進行中/已結案 must not keep hammering the portal.
            unresolved = []
            for item in ordered:
                laf_case_no = str(item.get("laf_case_number") or "").strip()
                current_case = self._current_laf_case_state(laf_case_no)
                if not self._opening_retry_is_resolved_by_case_state(
                    item, current_case
                ):
                    unresolved.append(item)
                    continue
                now_iso = datetime.now().isoformat(timespec="seconds")
                updated = dict(item)
                updated["status"] = "done"
                updated["last_error"] = ""
                updated["updated_at"] = now_iso
                updated["resolution_reason"] = "db_laf_status_active"
                self._merge_pending_portal_downloads({laf_case_no: updated})
                processed.append(
                    {
                        "laf_case_number": laf_case_no,
                        "downloaded_count": 0,
                        "status": "done",
                        "reason": "db_laf_status_active",
                    }
                )
            ordered = unresolved
            if not ordered:
                return {
                    "ok": True,
                    "scanned": len(pending_items),
                    "processed": len(processed),
                    "items": processed,
                }

            # Playwright sync objects are thread-bound and can become stale after
            # a long idle period. Retry cycles therefore own a fresh session.
            automation = self._get_automation(fresh=True)
            try:
                if not automation.login():
                    now_iso = datetime.now().isoformat(timespec="seconds")
                    failed_updates: Dict[str, dict] = {}
                    for item in ordered:
                        laf_case_no = str(item.get("laf_case_number") or "").strip()
                        if not laf_case_no:
                            continue
                        updated = dict(item)
                        updated["last_error"] = "login_failed"
                        updated["updated_at"] = now_iso
                        failed_updates[laf_case_no] = updated
                        processed.append({
                            "laf_case_number": laf_case_no,
                            "downloaded_count": 0,
                            "queued": True,
                            "error": "login_failed",
                        })
                    if failed_updates:
                        self._merge_pending_portal_downloads(failed_updates)
                    _eventlog(
                        "laf:portal:retry:login_failed",
                        ok=False,
                        payload={"items": len(processed)},
                        tags={},
                    )
                    return {"ok": False, "error": "login_failed", "scanned": len(pending_items), "processed": len(processed), "items": processed}

                for item in ordered:
                    laf_case_no = str(item.get("laf_case_number") or "").strip()
                    if not laf_case_no:
                        continue
                    now_iso = datetime.now().isoformat(timespec="seconds")
                    updated = dict(item)
                    raw_case_folder = str(updated.get("case_folder") or "").strip()
                    local_case_folder = self._to_local_case_folder(raw_case_folder)
                    if not local_case_folder or not os.path.isdir(local_case_folder):
                        local_case_folder = self._resolve_case_folder_for_laf(laf_case_no, fallback=raw_case_folder)
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
                        self._merge_pending_portal_downloads({laf_case_no: updated})
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

                    # NAS 預檢：依觸發類型判斷 NAS 是否已有對應檔案，有則標 done 不上網
                    # origin_reason 記錄原始觸發（go_live、closing、backfill 等）
                    # 不同觸發 → 不同必要檔案，只有「剛好抓到的那種」算滿足
                    # NAS/Synology Drive 雙向 fallback：任一路徑可存取即做預檢
                    _origin = str(updated.get("origin_reason") or updated.get("reason") or "")
                    _resolved_for_nas_check = self._resolve_case_folder_with_fallback(local_case_folder) if local_case_folder else ""
                    if _resolved_for_nas_check:
                        try:
                            _satisfied, _sat_reason = self._nas_satisfies_trigger(
                                _origin,
                                _resolved_for_nas_check,
                                evidence_after=str(updated.get("first_queued_at") or ""),
                            )
                            if _satisfied:
                                logger.info(
                                    "[LAF-RETRY] %s NAS 已有對應檔案（%s），跳過 portal，標記 done",
                                    laf_case_no, _sat_reason,
                                )
                                updated["status"] = "done"
                                updated["last_error"] = ""
                                updated["updated_at"] = now_iso
                                self._merge_pending_portal_downloads({laf_case_no: updated})
                                processed.append({
                                    "laf_case_number": laf_case_no,
                                    "downloaded_count": 0,
                                    "status": "done",
                                    "reason": _sat_reason,
                                })
                                continue
                        except Exception:
                            # Keep the portal fallback available, but make a failed
                            # NAS preflight observable instead of silently masking it.
                            logger.warning(
                                "[LAF-RETRY] %s NAS 預檢失敗，改走 portal fallback",
                                laf_case_no,
                                exc_info=True,
                            )

                    completed_tries = max(0, int(updated.get("tries") or 0))
                    # The stored counter is the number of portal attempts that
                    # actually ran.  Do not increment it merely to discover
                    # that the retry budget was already exhausted; doing so
                    # reported 169 attempts for a 168-attempt budget even
                    # though no 169th portal request was made.
                    if completed_tries >= _PORTAL_RETRY_MAX_TRIES:
                        updated["tries"] = _PORTAL_RETRY_MAX_TRIES
                        updated["updated_at"] = now_iso
                        updated["status"] = "exhausted"
                        self._merge_pending_portal_downloads({laf_case_no: updated})
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
                                f"請人工處理或清除佇列。",
                                topic_key="laf_dispatch",
                            )
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 848, exc_info=True)
                        # Do not write a permanent case-only skip marker here.
                        # The queue token is bound to the triggering event; a
                        # later event must be allowed to open a new window.
                        processed.append({"laf_case_number": laf_case_no, "downloaded_count": 0, "status": "exhausted"})
                        continue

                    updated["tries"] = completed_tries + 1
                    updated["last_try_at"] = now_iso
                    updated["updated_at"] = now_iso
                    # The shared browser session has already logged in
                    # successfully.  Do not carry a previous cycle's login
                    # failure into a current, healthy "attachment not listed"
                    # observation.
                    updated["last_error"] = ""

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
                            trigger_reason=str(updated.get("origin_reason") or updated.get("reason") or ""),
                            expected_queue_token=str(updated.get("queue_token") or ""),
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
                                docs = self._scan_case_folder_docs(str(result.get("case_folder") or ""), action="go_live")
                                notify_lines.append(f"開辦通知: {len(docs.get('opening_notice_files') or [])} 份")
                                notify_lines.append(f"委任狀: {len(docs.get('poa_files') or [])} 份")
                            self.notifier.notify_admin("\n".join(notify_lines), topic_key="laf_dispatch")
                        else:
                            self._merge_pending_portal_downloads({laf_case_no: updated})
                        processed.append(
                            {
                                "laf_case_number": laf_case_no,
                                "downloaded_count": int(result.get("downloaded_count") or 0),
                                "queued": bool(result.get("retry_queued")),
                            }
                        )
                    except Exception as e:
                        updated["last_error"] = str(e)
                        self._merge_pending_portal_downloads({laf_case_no: updated})
                        logger.error("Retry portal download failed for %s: %s", laf_case_no, e)
                        _eventlog(
                            "laf:portal:retry:attempt",
                            ok=False,
                            payload={"error": str(e)[:300], "tries": updated["tries"]},
                            tags={"laf_case_no": laf_case_no, "client_name": str(updated.get("client_name") or "")},
                        )
                        processed.append({"laf_case_number": laf_case_no, "downloaded_count": 0, "queued": True, "error": str(e)})
            finally:
                try:
                    automation.close()
                except Exception:
                    logger.warning("Unable to close LAF retry browser session", exc_info=True)
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

    @staticmethod
    def _extract_laf_case_number_from_text(*texts: str) -> str:
        for text in texts:
            if not text:
                continue
            match = re.search(r"(\d{7}-[A-Z]-\d{3})", str(text))
            if match:
                return match.group(1).strip()
        return ""

    def _resolve_email_route(self, case_info, notification_type: str) -> str:
        """Resolve incoming LAF email route, with explicit review-result download handling."""
        ntype = str(notification_type or "").strip()
        subject = str(getattr(case_info, "subject", "") or "")
        snippet = str(getattr(case_info, "snippet", "") or "")
        body = str(getattr(case_info, "body", "") or "")
        sender = str(getattr(case_info, "sender", "") or "")
        client_name = str(getattr(case_info, "client_name", "") or "").strip()
        decisive = "\n".join([ntype, subject])
        merged = "\n".join([decisive, snippet, body])

        # 意願徵詢信內容可能引用「審查通知」或「領款單」等流程文字；
        # 因此必須只根據 notification type 與 subject 高優先阻擋，不可讓
        # body 關鍵字把它誤路由為審查結果並 fallback 建案。
        if self._is_willingness_notice(case_info, notification_type):
            return "willingness"

        has_laf_case_no = bool(
            self._extract_laf_case_number_from_text(
                str(getattr(case_info, "laf_case_number", "") or ""),
                subject,
                snippet,
                body,
            )
        )
        # 只有官方結果通知主旨，或信件內文明確指示進入系統「下載」，才是
        # portal attachment event。內文引用流程名、酬金或領款單都不是充分證據。
        has_review_notice = any(k in decisive for k in ("審核結果通知", "審查結果通知", "審查通知"))
        has_report_notice = bool(re.search(r"回報[（(](?:結案|附條件)[)）]", decisive))
        compact_download_text = re.sub(r"\s+", "", "\n".join([subject, snippet, body]))
        has_explicit_portal_download = bool(
            "律師線上操作系統下載表單" in compact_download_text
            or re.search(r"律師線上操作系統.{0,40}?(?:可供)?下載", compact_download_text)
            or re.search(r"(?:下載|可供下載).{0,40}?律師線上操作系統", compact_download_text)
        )
        is_transfer_confirmation = bool(
            has_laf_case_no
            and not has_explicit_portal_download
            and (
                has_report_notice
                or "業經分會轉入系統" in merged
                or ntype in {"結案回報通知", "附條件回報通知", "結案轉入通知"}
            )
        )
        is_staff_material = ntype in {"staff_material", "專員來信", "原民中心案件資料", "中心案件資料"}
        is_indigenous_material = (
            has_laf_case_no
            and "原民中心" in merged
            and "寄送" in merged
            and "案件資料" in merged
            and "派案通知" not in merged
        )
        # Some centers use this exact, official structured delivery subject for
        # the first assignment instead of the words "派案通知".  Keep ordinary
        # staff/reference mail out of go-live, but do not discard an official
        # bracketed delivery that contains a complete case number and party.
        is_structured_case_delivery = bool(
            has_laf_case_no
            and client_name
            and "@laf.org.tw" in sender.lower()
            and re.search(r"【[^】]*法扶[^】]*寄送[^】]*】", subject)
            and "案件資料" in subject
        )

        # 「審核/審查結果通知」只是通知類型，不是必然存在官網附件的
        # 證明。只有信件內文明確指示至入口下載，才能建立 portal retry。
        if has_laf_case_no and has_explicit_portal_download:
            return "result_download"
        if is_transfer_confirmation:
            return "transfer_confirmation"
        if has_laf_case_no and has_review_notice:
            return "review_notice"
        if is_structured_case_delivery:
            return "dispatch"
        if is_staff_material or is_indigenous_material:
            return "staff_material"

        if ntype in ("dispatch", "派案", "派案通知"):
            return "dispatch"
        if ntype in ("withdrawal", "撤回"):
            return "withdrawal"
        if ntype in ("inquiry", "疑義"):
            return "inquiry"
        if ntype in ("fee", "費用"):
            return "fee"
        if ntype in ("progress", "進度", "進度回報"):
            return "progress"
        return "unknown"

    @staticmethod
    def _is_willingness_notice(case_info, notification_type: str = "") -> bool:
        ntype = str(notification_type or getattr(case_info, "notification_type", "") or "")
        subject = str(getattr(case_info, "subject", "") or "")
        decisive_text = re.sub(r"\s+", "", f"{ntype}\n{subject}")
        return any(
            marker in decisive_text
            for marker in ("接案意願徵詢", "接案意願回覆", "已收到接案意願")
        )

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

        route = self._resolve_email_route(case_info, notification_type)
        logger.info("  Route: %s", route)

        def _callback_result(handler_result, resolved_route: str):
            if isinstance(handler_result, dict):
                result = dict(handler_result)
                result.setdefault("route", resolved_route)
                result.setdefault("ok", not bool(result.get("error")))
                return result
            return {"ok": True, "route": resolved_route}

        try:
            handler_result = None
            if route == "result_download":
                handler_result = self.handle_review_result_download(case_info)
            elif route == "review_notice":
                logger.info("ℹ️ 審查結果通知未包含明確入口下載指示，不建立附件重試: %s", laf_number)
                _eventlog(
                    "laf:email:review_notice_no_download",
                    ok=True,
                    payload={
                        "notification_type": str(notification_type or ""),
                        "created_case": False,
                        "portal_download_requested": False,
                    },
                    tags={"laf_case_no": laf_number, "client_name": client_name},
                )
                return {
                    "ok": True,
                    "route": "review_notice",
                    "ignored": True,
                    "created_case": False,
                    "portal_download_requested": False,
                }
            elif route == "dispatch":
                handler_result = self.handle_go_live(case_info)
            elif route == "willingness":
                logger.info("ℹ️ 接案意願信僅標記已處理，不建案、不下載、不開辦: %s", laf_number)
                _eventlog(
                    "laf:email:willingness_ignored",
                    ok=True,
                    payload={"notification_type": str(notification_type or ""), "created_case": False},
                    tags={"laf_case_no": laf_number},
                )
                return {"ok": True, "route": "willingness", "ignored": True, "created_case": False}
            elif route == "transfer_confirmation":
                logger.info("ℹ️ 分會轉入確認僅更新流程狀態，不建立附件下載任務: %s", laf_number)
                # 若同一封信曾被舊版錯誤排入下載佇列，只能以該 Gmail message
                # 對應的精確 queue token 移除；不得清掉其他真正的下載通知。
                message_id = str(getattr(case_info, "message_id", "") or "").strip()
                if not self.dry_run and laf_number and message_id:
                    self._clear_pending_portal_download(
                        laf_number,
                        expected_queue_token=self._portal_retry_token_for_trigger(message_id),
                    )
                self._log_event(
                    laf_number,
                    "transfer_confirmation",
                    {
                        "notification_type": str(notification_type or ""),
                        "subject": str(getattr(case_info, "subject", "") or "")[:220],
                        "created_case": False,
                        "portal_download_requested": False,
                    },
                    "success",
                )
                _eventlog(
                    "laf:email:transfer_confirmation",
                    ok=True,
                    payload={
                        "notification_type": str(notification_type or ""),
                        "created_case": False,
                        "portal_download_requested": False,
                    },
                    tags={"laf_case_no": laf_number},
                )
                return {
                    "ok": True,
                    "route": "transfer_confirmation",
                    "ignored": True,
                    "created_case": False,
                    "portal_download_requested": False,
                }
            elif route == "staff_material":
                handler_result = self.handle_staff_material(case_info)
            elif route == "withdrawal":
                handler_result = self.handle_withdrawal(case_info)
            elif route == "inquiry":
                handler_result = self.handle_inquiry(case_info)
            elif route == "fee":
                handler_result = self.handle_fee_payment(case_info)
            elif route == "progress":
                handler_result = self.handle_progress_report(case_info)
            else:
                # 嘗試 progress 關鍵字偵測（進度/案件 同時出現）
                try:
                    from skills.legal.laf import _classify_progress_email, _PROGRESS_PRIORITY_TYPES
                    subject = getattr(case_info, 'subject', '')
                    snippet = getattr(case_info, 'snippet', '')
                    if (notification_type not in _PROGRESS_PRIORITY_TYPES
                            and _classify_progress_email(subject, snippet)):
                        logger.info("📋 Progress email detected: %s", client_name)
                        handler_result = self.handle_progress_report(case_info)
                        return _callback_result(handler_result, "progress")
                except Exception as _pe:
                    logger.debug("progress detect skip: %s", _pe)
                # Unknown mail must not silently become a new case.  This prevents
                # staff/reference messages from accidentally triggering go-live.
                logger.warning("⚠️ Unknown LAF email route; archived/ignored without go-live: %s", getattr(case_info, "subject", "")[:160])
                _eventlog(
                    "laf:email:unknown_route",
                    ok=False,
                    payload={
                        "notification_type": str(notification_type or ""),
                        "subject": str(getattr(case_info, "subject", "") or "")[:220],
                    },
                    tags={"laf_case_no": laf_number, "client_name": client_name},
                )
                return {"ok": True, "route": "unknown", "ignored": True}
            return _callback_result(handler_result, route)
        except Exception as exc:
            logger.exception("LAF email callback failed: route=%s laf=%s client=%s", route, laf_number, client_name)
            _eventlog(
                "laf:email:callback_failed",
                ok=False,
                payload={
                    "route": route,
                    "notification_type": str(notification_type or ""),
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "subject": str(getattr(case_info, "subject", "") or "")[:220],
                },
                tags={"laf_case_no": laf_number, "client_name": client_name},
            )
            return {"ok": False, "route": route, "error": f"{type(exc).__name__}: {exc}"}

    def handle_staff_material(self, case_info):
        """Archive LAF staff/center material without treating it as formal dispatch."""
        subject = str(getattr(case_info, "subject", "") or "").strip()
        laf_number = self._extract_laf_case_number_from_text(
            str(getattr(case_info, "laf_case_number", "") or "").strip(),
            subject,
            str(getattr(case_info, "snippet", "") or ""),
            str(getattr(case_info, "body", "") or ""),
        )
        client_name = str(getattr(case_info, "client_name", "") or "").strip()
        case_type = str(getattr(case_info, "case_type", "") or "").strip()
        case_stage = str(getattr(case_info, "case_stage", "") or "").strip()
        case_reason = str(getattr(case_info, "case_reason", "") or "").strip()

        db_case = {}
        if laf_number and self.db:
            try:
                db_case = self.db.fetch_one(
                    "SELECT `case_number`, `client_name`, `case_type`, `case_stage`, `case_reason`, `folder_path` "
                    "FROM `cases` WHERE `legal_aid_number` = %s ORDER BY `id` DESC LIMIT 1",
                    (laf_number,),
                    as_dict=True,
                ) or {}
            except Exception as e:
                logger.warning("Staff-material DB lookup failed (%s): %s", laf_number, e)

        if db_case and self.db:
            updates = {}
            if case_type and case_type not in {"民事", "待確認"} and case_type != str(db_case.get("case_type") or ""):
                updates["case_type"] = case_type
            if case_stage and case_stage not in {"一審", "待確認"} and case_stage != str(db_case.get("case_stage") or ""):
                updates["case_stage"] = case_stage
            if case_reason and case_reason != "待確認" and case_reason != str(db_case.get("case_reason") or ""):
                updates["case_reason"] = case_reason
            if client_name and not str(db_case.get("client_name") or "").strip():
                updates["client_name"] = client_name
            if updates:
                try:
                    assignments = ", ".join(f"`{key}`=%s" for key in updates)
                    params = list(updates.values()) + [laf_number]
                    self.db.execute(
                        f"UPDATE `cases` SET {assignments}, `updated_at`=NOW() WHERE `legal_aid_number`=%s",
                        tuple(params),
                    )
                    logger.info("📝 Staff material updated DB for %s: %s", laf_number, ", ".join(updates))
                except Exception as e:
                    logger.warning("Staff-material DB update failed (%s): %s", laf_number, e)

        case_folder = self._resolve_case_folder_for_laf(
            laf_number,
            fallback=str(db_case.get("folder_path") or ""),
        )
        snapshot = ""
        attachment_result = {"ok": False, "new_count": 0, "downloaded_count": 0, "error": ""}
        if case_folder and os.path.isdir(case_folder):
            try:
                snapshot = self._archive_case_email_snapshot(case_info, case_folder)
            except Exception as e:
                logger.warning("Staff-material snapshot failed (%s): %s", laf_number, e)
            try:
                attachment_result = self._download_case_email_attachments(case_info, case_folder)
            except Exception as e:
                attachment_result = {"ok": False, "new_count": 0, "downloaded_count": 0, "error": str(e)}
                logger.warning("Staff-material attachment archive failed (%s): %s", laf_number, e)

            if self.notifier and int(attachment_result.get("new_count") or 0) > 0:
                self.notifier.notify_admin(
                    "📎 法扶補充資料已歸檔\n"
                    f"案號：{laf_number or '-'}\n"
                    f"當事人：{client_name or db_case.get('client_name') or '-'}\n"
                    f"新增附件：{attachment_result.get('new_count', 0)} 份\n"
                    "（此信不是正式派案通知，未啟動開辦流程）",
                    topic_key="laf_general",
                )
        else:
            logger.info(
                "ℹ️ Staff material has no existing case folder yet; waiting formal dispatch: %s %s",
                laf_number,
                subject[:120],
            )

        _eventlog(
            "laf:email:staff_material",
            ok=bool(case_folder),
            payload={
                "subject": subject[:220],
                "case_folder": os.path.basename(case_folder.rstrip("/\\")) if case_folder else "",
                "snapshot": snapshot,
                "attachments": attachment_result,
                "db_matched": bool(db_case),
            },
            tags={"laf_case_no": laf_number, "client_name": client_name},
        )

    def handle_review_result_download(self, case_info):
        """Handle review/result notifications that should trigger portal attachment download."""
        subject = str(getattr(case_info, "subject", "") or "").strip()
        notification_type = str(getattr(case_info, "notification_type", "") or "").strip()
        laf_number = self._extract_laf_case_number_from_text(
            str(getattr(case_info, "laf_case_number", "") or "").strip(),
            subject,
            str(getattr(case_info, "snippet", "") or ""),
            str(getattr(case_info, "body", "") or ""),
        )
        client_name = str(getattr(case_info, "client_name", "") or "").strip()
        case_type = str(getattr(case_info, "case_type", "") or "").strip()
        case_reason = str(getattr(case_info, "case_reason", "") or "").strip()

        if not laf_number:
            logger.warning("⚠️ Review-result email missing LAF number, fallback to go-live: %s", subject[:160])
            return self.handle_go_live(case_info) or {"ok": True, "route": "dispatch_fallback", "reason": "missing_laf_number"}

        db_case = {}
        if self.db:
            try:
                db_case = self.db.fetch_one(
                    "SELECT `case_number`, `client_name`, `case_type`, `case_reason`, `folder_path`, `legal_aid_status` "
                    "FROM `cases` WHERE `legal_aid_number` = %s ORDER BY `id` DESC LIMIT 1",
                    (laf_number,),
                    as_dict=True,
                ) or {}
            except Exception as e:
                logger.warning("Review-result DB lookup failed (%s): %s", laf_number, e)

        if not db_case:
            # 相容少數「審核結果通知」其實是初次派案的分會格式
            logger.info("ℹ️ Review-result email has no existing DB case, fallback to go-live: %s", laf_number)
            return self.handle_go_live(case_info) or {"ok": True, "route": "dispatch_fallback", "reason": "missing_existing_case"}

        case_number = str(db_case.get("case_number") or "").strip()
        client_name = client_name or str(db_case.get("client_name") or "").strip()
        case_type = case_type or str(db_case.get("case_type") or "").strip()
        case_reason = case_reason or str(db_case.get("case_reason") or "").strip()
        case_folder = self._resolve_case_folder_for_laf(
            laf_number,
            fallback=str(db_case.get("folder_path") or ""),
        )

        logger.info(
            "📥 Review-result download: type=%s, client=%s, laf=%s, case=%s",
            notification_type,
            client_name,
            laf_number,
            case_number,
        )
        _eventlog(
            "laf:review_result:start",
            ok=None,
            payload={"notification_type": notification_type, "subject": subject[:200]},
            tags={"laf_case_no": laf_number, "client_name": client_name},
        )

        email_attachment_result = {
            "ok": False,
            "downloaded_count": 0,
            "new_count": 0,
            "skipped_existing_count": 0,
            "error": "",
        }
        if case_folder and not self.dry_run:
            try:
                self._archive_case_email_snapshot(case_info, case_folder)
            except Exception as archive_email_error:
                logger.warning("Failed to archive review-result email snapshot for %s: %s", laf_number, archive_email_error)
            try:
                email_attachment_result = self._download_case_email_attachments(case_info, case_folder)
            except Exception as email_attachment_error:
                logger.warning("Failed to archive review-result email attachments for %s: %s", laf_number, email_attachment_error)

        download_result = {}
        if self.laf_config.get("auto_create_case", True) and not self.dry_run:
            download_result = self._download_case_files(
                laf_number,
                case_folder=case_folder,
                client_name=client_name,
                case_type=case_type,
                case_reason=case_reason,
                case_number=case_number,
                trigger_reason="review_result_download",
                trigger_id=str(getattr(case_info, "message_id", "") or ""),
                trigger_received_at=getattr(case_info, "received_at", None),
            )

        notify_lines = [
            "📥 法扶審核結果通知已觸發官網附件下載",
            f"當事人: {client_name}",
            f"案號: {laf_number}",
        ]
        if case_number:
            notify_lines.append(f"OSC案號: {case_number}")
        if subject:
            notify_lines.append(f"主旨: {subject}")

        dl_archive = download_result.get("archive", {}) if isinstance(download_result, dict) else {}
        dl_new_files = dl_archive.get("new_files") or []
        dl_skipped_files = dl_archive.get("skipped_existing") or []
        email_new_count = int(email_attachment_result.get("new_count") or 0)
        email_skipped_count = int(email_attachment_result.get("skipped_existing_count") or 0)
        if download_result.get("downloaded_count") and dl_new_files:
            notify_lines.append(f"官網附件: 新增 {len(dl_new_files)} 份")
            if dl_skipped_files:
                notify_lines.append(f"官網附件去重: 略過 {len(dl_skipped_files)} 份")
            for fn in dl_new_files[:10]:
                notify_lines.append(f"  ✓ {os.path.basename(fn)}")
            for fn in dl_skipped_files[:5]:
                notify_lines.append(f"  ⏭️ {os.path.basename(fn)}")
        if download_result.get("retry_queued"):
            notify_lines.append("官網目前尚未列出可下載資料，已排入自動重試。")
            notify_lines.append("無需重新上傳或操作；取得新檔案後會再通知。")
            logger.info("Review-result portal retry queued without new files: laf=%s case=%s", laf_number, case_number)
        if download_result.get("error"):
            notify_lines.append("⚠️ 官網附件本次未完成，系統已保留重試任務。")

        if email_attachment_result.get("downloaded_count") and email_new_count:
            notify_lines.append(f"專員來信附件: 新增 {email_new_count} 份")
            if email_skipped_count:
                notify_lines.append(f"專員來信附件去重: 略過 {email_skipped_count} 份")

        notify_msg = "\n".join(notify_lines)
        should_notify = bool(
            dl_new_files
            or email_new_count
            or download_result.get("retry_queued")
            or download_result.get("error")
        )
        notify_dedup_key = ""
        notify_dedup_metadata = {}
        notify_mark_done = None
        if should_notify:
            msg_id = str(getattr(case_info, "message_id", "") or "").strip()
            state = "new"
            if download_result.get("retry_queued"):
                state = "queued"
            if download_result.get("error"):
                state = "error"
            material = "|".join(
                [
                    laf_number,
                    msg_id,
                    state,
                    str(len(dl_new_files)),
                    str(email_new_count),
                    str(download_result.get("error") or "")[:160],
                    *[os.path.basename(str(fn)) for fn in dl_new_files[:20]],
                ]
            )
            notify_dedup_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
            try:
                from skills.ops.dedup_db import is_done as _dd_is_done, mark_done as _dd_mark_done

                if _dd_is_done("laf_review_result_notice", notify_dedup_key):
                    should_notify = False
                else:
                    notify_mark_done = _dd_mark_done
                    notify_dedup_metadata = {
                        "laf_case_no": laf_number,
                        "case_number": case_number,
                        "client_name": client_name,
                        "message_id": msg_id,
                        "state": state,
                        "portal_new": len(dl_new_files),
                        "email_new": email_new_count,
                        "subject": subject[:220],
                    }
            except Exception as dedup_error:
                logger.debug("Review-result notice dedup skipped (%s): %s", laf_number, dedup_error)
        notified = False
        if not self.dry_run and should_notify:
            notify_ok = self.notifier.notify_admin(notify_msg, topic_key="laf_closing")
            notified = bool(notify_ok)
            if notify_ok and notify_mark_done and notify_dedup_key:
                try:
                    notify_mark_done("laf_review_result_notice", notify_dedup_key, metadata=notify_dedup_metadata)
                except Exception as mark_error:
                    logger.debug("Review-result notice dedup mark failed (%s): %s", laf_number, mark_error)
        elif not self.dry_run:
            logger.info(
                "🔕 Review-result notification suppressed: laf=%s case=%s portal_new=%s email_new=%s retry=%s error=%s dedup=%s",
                laf_number,
                case_number,
                len(dl_new_files),
                email_new_count,
                bool(download_result.get("retry_queued")),
                bool(download_result.get("error")),
                bool(notify_dedup_key),
            )

        self._log_event(
            laf_number,
            "review_result_download",
            {
                "notification_type": notification_type,
                "subject": subject,
                "case_number": case_number,
                "client_name": client_name,
                "downloaded_count": int(download_result.get("downloaded_count") or 0),
                "portal_new_count": len(dl_new_files),
                "email_new_count": email_new_count,
                "retry_queued": bool(download_result.get("retry_queued")),
                "error": str(download_result.get("error") or ""),
                "notified": notified,
            },
            "success",
        )
        _eventlog(
            "laf:review_result:done",
            ok=not bool(download_result.get("error")),
            payload={
                "downloaded_count": int(download_result.get("downloaded_count") or 0),
                "retry_queued": bool(download_result.get("retry_queued")),
                "error": str(download_result.get("error") or "")[:300],
            },
            tags={"laf_case_no": laf_number, "client_name": client_name},
        )
        return {
            "ok": not bool(download_result.get("error")),
            "route": "result_download",
            "laf_case_number": laf_number,
            "case_number": case_number,
            "client_name": client_name,
            "downloaded_count": int(download_result.get("downloaded_count") or 0),
            "portal_new_count": len(dl_new_files),
            "email_new_count": email_new_count,
            "retry_queued": bool(download_result.get("retry_queued")),
            "retry_queue_token": str(download_result.get("retry_queue_token") or ""),
            "download_error": str(download_result.get("error") or ""),
            "notified": notified,
        }

    # In-memory dedup for go_live to prevent duplicate notifications when
    # multiple emails arrive for the same case (e.g., 派案通知 + 附加檔案).
    # NOTE: initialized per-instance in __init__ to avoid cross-instance leakage.

    def handle_go_live(self, case_info):
        """
        開辦 flow:
        1. Check for duplicate in DB
        2. Create folder on SynologyDrive
        3. Insert/update DB record with Z: canonical path
        4. Download files from LAF portal
        5. Notify admin
        """
        # Defense in depth: even a caller that bypasses _resolve_email_route()
        # must never turn a willingness inquiry/acknowledgement into a case.
        if self._is_willingness_notice(case_info):
            logger.info("⏭️ Go-Live blocked for willingness-only email")
            return {"ok": True, "route": "willingness", "ignored": True, "created_case": False}

        client_name = getattr(case_info, 'client_name', '')
        case_type = getattr(case_info, 'case_type', '')
        case_reason = getattr(case_info, 'case_reason', '')
        laf_number = getattr(case_info, 'laf_case_number', '')
        case_type, normalized_stage, case_reason = normalize_laf_case_fields(
            case_type,
            getattr(case_info, 'case_stage', ''),
            case_reason,
            getattr(case_info, 'laf_case_type', ''),
        )
        try:
            case_info.case_type = case_type
            case_info.case_stage = normalized_stage
            case_info.case_reason = case_reason
        except Exception:
            logger.debug("LAF case_info is immutable; using normalized local fields")

        # 消費者債務清理案件 — 案由一律正規化為「更生」（程序切換時再手動改）
        # 資料來源（email/DB/手動）不同可能帶入「消費者債務清理程序」等原始文字，必須在此攔截
        if case_type == '消費者債務清理' or '消費者債務清理' in case_reason:
            if '清算' not in case_reason:
                case_reason = '更生'

        _PLACEHOLDER_INVALID_CHARS = set(")(<>[]{}!@#$%^&*+=|\\;:\"'?/`~")
        _PLACEHOLDER_NOISE_TOKENS = ("案情", "文件", "卷宗", "附件", "信件", "資料夾")
        _PLACEHOLDER_REASON_TOKENS = {"", "待確認", "未確認"}
        _name_for_placeholder = str(client_name or "").strip()
        _reason_for_placeholder = str(case_reason or "").strip()
        _name_bad = (
            not _name_for_placeholder
            or any(c in _PLACEHOLDER_INVALID_CHARS for c in _name_for_placeholder)
            or "--" in _name_for_placeholder
            or any(tok in _name_for_placeholder for tok in _PLACEHOLDER_NOISE_TOKENS)
            or len(_name_for_placeholder) > 30
        )
        _reason_bad = _reason_for_placeholder in _PLACEHOLDER_REASON_TOKENS
        _is_placeholder_case = bool(_name_bad or _reason_bad)

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

            # A service/stage label such as 「偵查中辯護」 was historically
            # allowed to leak into case_reason.  That made an investigation
            # case look like 民事／一審 and, because it was not a placeholder,
            # later portal metadata was announced without being persisted.
            # Only repair this deterministic legacy shape; arbitrary
            # differences remain human-owned.
            normalized_existing = self._normalized_investigation_case_fields(existing)
            if normalized_existing:
                logger.info(
                    "  🔧 Deterministic classification reconcile: DB('%s' / '%s' / '%s') → ('%s' / '%s' / '%s')",
                    existing.get("case_type"),
                    existing.get("case_stage"),
                    existing.get("case_reason"),
                    normalized_existing["case_type"],
                    normalized_existing["case_stage"],
                    normalized_existing["case_reason"],
                )
                if not self._reconcile_normalized_case_record(existing, normalized_existing):
                    logger.error("  ❌ Deterministic classification reconcile failed; stopping go_live")
                    return {
                        "ok": False,
                        "created_case": False,
                        "case_number": case_number,
                        "error": "normalized_case_reconcile_failed",
                    }
                existing.update(normalized_existing)

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
                if laf_number:
                    self._go_live_dedup.add(laf_number)
                return {
                    "ok": True,
                    "created_case": True,
                    "created_case_id": str(existing.get("id") or case_number or ""),
                    "case_number": case_number,
                    "folder_path": str(existing.get("folder_path") or ""),
                    "existing": True,
                }
            db_path = self._to_local_case_folder(str(existing.get("folder_path") or ""))
            # Update legal_aid_number if not set
            if not existing.get("legal_aid_number") and laf_number:
                self._update_legal_aid_number(existing.get("id"), laf_number)

            # ★ Placeholder reconcile：existing 是 placeholder（_is_placeholder_case 必為 False，
            # 因為到這層的 client_name/case_reason 已從第二封 email 解析出乾淨資料）→
            # 比對 DB 既有 client_name/case_reason，若 DB 是 placeholder 而 email 給好資料 → UPDATE DB + rename folder
            try:
                _existing_name = str(existing.get("client_name") or "").strip()
                _existing_reason = str(existing.get("case_reason") or "").strip()
                _existing_stage = str(existing.get("case_stage") or "").strip()
                # 重用本函數已偵測過的 placeholder 規則
                _existing_name_bad = (
                    not _existing_name
                    or any(c in _PLACEHOLDER_INVALID_CHARS for c in _existing_name)
                    or "--" in _existing_name
                    or any(tok in _existing_name for tok in _PLACEHOLDER_NOISE_TOKENS)
                    or len(_existing_name) > 30
                )
                _existing_reason_bad = _existing_reason in _PLACEHOLDER_REASON_TOKENS
                _existing_is_placeholder = bool(_existing_name_bad or _existing_reason_bad)
                if _existing_is_placeholder and not _is_placeholder_case:
                    logger.info(
                        "  🔧 Placeholder reconcile: DB('%s' / '%s' / '%s') → email('%s' / '%s' / '%s')",
                        _existing_name, _existing_reason, _existing_stage,
                        client_name, case_reason, getattr(case_info, 'case_stage', '') or '',
                    )
                    self._reconcile_placeholder_record(
                        existing,
                        new_client_name=client_name,
                        new_case_reason=case_reason,
                        new_case_stage=getattr(case_info, 'case_stage', '') or _existing_stage,
                        new_case_type=case_type or str(existing.get("case_type") or ""),
                    )
                    # 重新讀取 db_path（rename 後可能已變）
                    if self.db:
                        try:
                            _refreshed = self.db.fetch_one(
                                "SELECT folder_path FROM cases WHERE id = %s",
                                (existing.get("id"),), as_dict=True,
                            )
                            if _refreshed:
                                db_path = self._to_local_case_folder(str(_refreshed.get("folder_path") or "")) or db_path
                        except Exception:
                            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1556, exc_info=True)
            except Exception as _rec_e:
                logger.warning("Placeholder reconcile failed: %s", _rec_e)
        else:
            # Step 2: Create folder
            case_number = ""
            if not self.dry_run:
                case_number = self._generate_case_number()
                if not case_number:
                    logger.error("  ❌ Standard case number generation failed")
                    self._log_event(laf_number, "go_live", {"error": "case_number_generation_failed"}, "failed")
                    _eventlog("laf:go_live:done", ok=False, payload={"error": "case_number_generation_failed"}, tags={"laf_case_no": laf_number, "client_name": client_name})
                    try:
                        self.notifier.notify_admin(
                            f"🚨 法扶派案 go_live 失敗（DatabaseManager 初始化異常）\n"
                            f"當事人：{client_name}\n"
                            f"法扶案號：{laf_number}\n"
                            f"原因：self.db=None（_get_db_manager() import 鏈錯誤，非 DB 斷線）\n"
                            f"email 不會標記為已處理，後續排程會保留重試資格。\n"
                            f"⚠️ 請先排除資料庫初始化問題；必要時可在 Discord 輸入「{laf_number} {client_name} 開辦」補跑"
                        )
                    except Exception as _ne:
                        logger.warning("go_live failure alert send failed: %s", _ne)
                    return {
                        "ok": False,
                        "created_case": False,
                        "error": "case_number_generation_failed",
                    }
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
                active_share = (os.environ.get("MAGI_NAS_HOME_USER") or os.environ.get("MAGI_NAS_USER") or "home").strip().strip("/\\") or "home"
                db_path = f"Z:/{active_share}/01_案件/法扶案件/" + f"{case_type}/{case_number or client_name}"
            else:
                db_path = self.folder_builder.create_case_folder(folder_info)

            if not db_path:
                logger.error("  ❌ Folder creation failed")
                self._log_event(laf_number, "go_live", {"error": "folder_creation_failed"}, "failed")
                _eventlog("laf:go_live:done", ok=False, payload={"error": "folder_creation_failed"}, tags={"laf_case_no": laf_number, "client_name": client_name})
                try:
                    self.notifier.notify_admin(
                        f"🚨 法扶派案 go_live 失敗（資料夾建立失敗）\n"
                        f"當事人：{client_name}\n"
                        f"法扶案號：{laf_number}\n"
                        f"原因：NAS 資料夾建立失敗（可能是 NAS 斷線或路徑錯誤）\n"
                        f"⚠️ 請確認 NAS 掛載狀態後，手動在 Discord 觸發開辦流程"
                    )
                except Exception as _ne:
                    logger.warning("go_live failure alert send failed: %s", _ne)
                return {
                    "ok": False,
                    "created_case": False,
                    "error": "folder_creation_failed",
                }

            # Step 3: Insert DB record
            if not self.dry_run:
                case_number = self._create_case_record(case_info, db_path, case_number=case_number) or ""
                if not case_number:
                    logger.error("  ❌ Case record creation failed")
                    self._log_event(
                        laf_number,
                        "go_live",
                        {"error": "case_record_creation_failed"},
                        "failed",
                    )
                    _eventlog(
                        "laf:go_live:done",
                        ok=False,
                        payload={"error": "case_record_creation_failed"},
                        tags={"laf_case_no": laf_number, "client_name": client_name},
                    )
                    return {
                        "ok": False,
                        "created_case": False,
                        "error": "case_record_creation_failed",
                    }

        verified_case = existing
        if not self.dry_run:
            verified_case = self._check_duplicate(
                laf_number, client_name, case_type, case_reason
            )
            if not verified_case:
                logger.error("  ❌ Case record is not observable after go-live")
                self._log_event(
                    laf_number,
                    "go_live",
                    {"error": "case_record_not_observable_after_write"},
                    "failed",
                )
                _eventlog(
                    "laf:go_live:done",
                    ok=False,
                    payload={"error": "case_record_not_observable_after_write"},
                    tags={"laf_case_no": laf_number, "client_name": client_name},
                )
                return {
                    "ok": False,
                    "created_case": False,
                    "error": "case_record_not_observable_after_write",
                }
            if laf_number:
                self._go_live_dedup.add(laf_number)

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

        # Vision Step: Extract Start Date from prepared go-live files.
        # 01_法扶資料保存官網下載的空白表件；只有 02_開辦資料的已簽/已填文件
        # 可以作為開辦上傳依據，避免把空白委任狀誤認為已可開辦。
        extracted_date = None
        poa_submit_date = None
        open_doc = None
        poa_doc = None
        docs = self._empty_docs_map()
        go_live_docs = self._empty_docs_map()
        if not self.dry_run and db_path:
            try:
                docs = self._scan_case_folder_docs(db_path, action="go_live")
                go_live_docs, go_live_scan_scope = self._scan_go_live_docs(db_path)
                if go_live_scan_scope:
                    logger.info("  🔎 開辦文件掃描範圍：%s", go_live_scan_scope)
                open_doc = (go_live_docs.get("opening_notice_files") or [None])[0]
                poa_doc = (go_live_proof_files(go_live_docs) or [None])[0]
                if open_doc:
                    extracted_date = self._extract_best_date_from_doc(open_doc)
                if poa_doc:
                    poa_submit_date = self._extract_best_date_from_doc(poa_doc)
                if extracted_date:
                    logger.info("  🎯 開辦通知日期：%s", extracted_date)
                if poa_submit_date:
                    logger.info("  🎯 遞狀證明日期：%s", poa_submit_date)
            except Exception as e:
                logger.error(f"  ❌ Vision extraction failed: {e}")

        _is_consumer_debt = self._is_consumer_debt_case_folder(db_path or "")
        # 消債案件只需開辦通知書；一般案件需要開辦通知書 + 遞狀證明（委任狀/書狀存底/回執）
        docs_ready_for_go_live = is_go_live_ready(go_live_docs, is_consumer_debt=_is_consumer_debt)

        # Step 4.5: 偵測遞狀日期 + 自動填寫開辦表單（預覽，不送出）
        submission_info: dict = {}
        go_live_remark = ""
        go_live_upload_file = ""
        go_live_prefill_ok = False

        # Placeholder 偵測：派案 email 解析不完整（client_name 含特殊字元 / case_reason='待確認'）
        # 條件還沒齊 → 不啟動 portal go_live prefill（避免錯資料送出）
        # 等接案清冊 reconcile_placeholder_cases() 修正後，下次自然 retry 才開辦
        # NOTE: 規則 mirror laf_nightly_audit._is_placeholder_*；這裡 inline 避免 import 整個 module
        _is_placeholder_case = False
        try:
            _name = str(client_name or "").strip()
            _reason = str(case_reason or "").strip()
            _name_bad = (
                not _name
                or any(c in _PLACEHOLDER_INVALID_CHARS for c in _name)
                or "--" in _name
                or any(tok in _name for tok in _PLACEHOLDER_NOISE_TOKENS)
                or len(_name) > 30
            )
            _reason_bad = _reason in _PLACEHOLDER_REASON_TOKENS
            _is_placeholder_case = bool(_name_bad or _reason_bad)
            if _is_placeholder_case:
                logger.info(
                    "  ⏸️ Placeholder 案件偵測（client=%r, reason=%r）— 跳過 portal go_live prefill，"
                    "等 reconcile_placeholder 修正後再開辦",
                    client_name, case_reason,
                )
        except Exception as _placeholder_e:
            logger.debug("placeholder detection skipped: %s", _placeholder_e)

        if not self.dry_run and not _is_placeholder_case:
            try:
                # 消債案件也要嘗試自動開辦（有開辦通知書就夠）
                _can_try_go_live = docs_ready_for_go_live or (open_doc and not _is_consumer_debt)
                if _can_try_go_live or _is_consumer_debt:
                    submission_info = self._detect_poa_submission_info(db_path)
                    confidence = submission_info.get("confidence", "low")
                    logger.info("  📋 遞狀日期偵測: confidence=%s, info=%s", confidence, submission_info)

                    if confidence in ("high", "medium") or _is_consumer_debt:
                        go_live_remark = self._compose_go_live_remark(
                            submission_info, client_name, is_consumer_debt=_is_consumer_debt,
                            open_doc_date=extracted_date or "",
                        )
                        go_live_upload_files = self._find_go_live_upload_files(
                            db_path, is_consumer_debt=_is_consumer_debt
                        )
                        logger.info("  📋 selRemark: %s", go_live_remark)
                        logger.info("  📋 上傳檔案: %s", [os.path.basename(f) for f in go_live_upload_files])

                        if go_live_upload_files and (go_live_remark or _is_consumer_debt):
                            if not go_live_remark and _is_consumer_debt:
                                go_live_remark = "已首次實質討論案情。"
                            fields = {
                                "sel_result": "1",  # 已開辦
                                "remark": go_live_remark,
                                "upload_files": go_live_upload_files,
                            }
                            go_live_prefill_ok = self.execute_portal_go_live_draft(
                                laf_number, client_name, fields
                            )
                            logger.info("  📋 開辦預填結果: %s", go_live_prefill_ok)
            except Exception as gl_e:
                logger.error("  ❌ 開辦自動化失敗: %s", gl_e)

        go_live_reminder = ""
        if not self.dry_run:
            if _is_placeholder_case:
                go_live_reminder = (
                    "⚠️ 派案 email 資料不完整（當事人/案由），已建立臨時資料夾。"
                    "系統會每小時自動從接案清冊修正 DB 與資料夾名稱後再啟動開辦。"
                )
            elif go_live_prefill_ok:
                go_live_reminder = "✅ 開辦表單已自動填寫（未送出），截圖已傳送，請確認後回覆。"
            elif docs_ready_for_go_live:
                if submission_info.get("confidence") == "low":
                    go_live_reminder = "⚠️ 開辦資料齊備，但找不到遞狀日期，請手動開辦。"
                else:
                    go_live_reminder = "⚠️ 開辦資料齊備，自動填寫失敗，請手動開辦。"
            else:
                missing_parts = []
                if not open_doc:
                    missing_parts.append("開辦通知/接案通知/回報單")
                if not _is_consumer_debt and not go_live_proof_files(go_live_docs):
                    missing_parts.append("委任狀或書狀存底/回執")
                go_live_reminder = (
                    f"⚠️ 尚缺 02_開辦資料 的已簽/已填{'、'.join(missing_parts)}，"
                    "請補齊後手動開辦；01_法扶資料的官網空白表件不視為可開辦文件。"
                )
            logger.info("  📋 Go-live reminder: %s", go_live_reminder)

        # Step 5: Notify (text + opening notice image for confirmation)
        # 通知計數以 02_開辦資料 為準
        opening_notice_count = len(go_live_docs.get("opening_notice_files") or [])
        poa_count = len(go_live_docs.get("poa_files") or [])
        proof_count = len(go_live_proof_files(go_live_docs))
        portal_existing_files = self._existing_laf_portal_attachment_files(db_path)
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
            notify_lines.append(f"官網附件: 本輪新增 {dl_new_count} 份")
            if dl_skipped_count:
                notify_lines.append(f"官網附件去重: 略過 {dl_skipped_count} 份")
            for fn in dl_new_files[:10]:
                notify_lines.append(f"  ✓ {os.path.basename(fn)}")
            for fn in dl_skipped_files[:5]:
                notify_lines.append(f"  ⏭️ {os.path.basename(fn)}")
        elif download_result.get("retry_queued"):
            logger.info("Go-live portal retry queued without new files: laf=%s case=%s", laf_number, case_number)
        elif download_result.get("error"):
            notify_lines.append(f"⚠️ 官網附件下載失敗: {download_result['error']}")
        if portal_existing_files:
            notify_lines.append(f"官網附件既有: {len(portal_existing_files)} 份（01_法扶資料）")
        notify_lines.append(f"開辦資料（02_開辦資料，已簽/已填）: 開辦通知 {opening_notice_count} 份、委任狀 {poa_count} 份")
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
        # 路由：尚未真正開辦（缺開辦通知 + 委任狀）→ laf_dispatch（派案頻道）；
        # 已具備可開辦條件 → laf_go_live（開辦頻道）
        _topic_route = "laf_go_live" if (opening_notice_count > 0 or proof_count > 0) else "laf_dispatch"
        should_send_go_live_notice = True
        notice_dedup_key = ""
        notice_dedup_metadata = {}
        notice_mark_done = None
        dl_archive_for_notice = download_result.get("archive", {}) if isinstance(download_result, dict) else {}
        dl_new_for_notice = dl_archive_for_notice.get("new_files") or []
        email_new_for_notice = int(email_attachment_result.get("new_count") or 0)
        unchanged_existing_notice = bool(
            _is_existing
            and not dl_new_for_notice
            and email_new_for_notice <= 0
            and not download_result.get("error")
            and not go_live_prefill_ok
        )
        if unchanged_existing_notice:
            material = json.dumps(
                {
                    "laf": laf_number,
                    "case": case_number,
                    "client": client_name,
                    "case_type": case_type,
                    "case_reason": case_reason,
                    "folder": folder_label,
                    "portal_existing": len(portal_existing_files),
                    "opening_notice_count": opening_notice_count,
                    "poa_count": poa_count,
                    "proof_count": proof_count,
                    "reminder": go_live_reminder,
                    "topic": _topic_route,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            notice_dedup_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
            try:
                from skills.ops.dedup_db import is_done as _dd_is_done, mark_done as _dd_mark_done

                if _dd_is_done("laf_go_live_existing_status_notice", notice_dedup_key):
                    should_send_go_live_notice = False
                else:
                    notice_mark_done = _dd_mark_done
                    notice_dedup_metadata = {
                        "laf_case_no": laf_number,
                        "case_number": case_number,
                        "client_name": client_name,
                        "case_type": case_type,
                        "case_reason": case_reason,
                        "folder": folder_label,
                        "portal_existing": len(portal_existing_files),
                        "opening_notice_count": opening_notice_count,
                        "poa_count": poa_count,
                        "proof_count": proof_count,
                        "topic": _topic_route,
                    }
            except Exception as dedup_error:
                logger.debug("Go-live existing-case notice dedup skipped (%s): %s", laf_number, dedup_error)

        notify_ok = False
        if should_send_go_live_notice and confirm_files:
            try:
                notify_result = self.notifier.notify_admin_with_files(notify_msg, confirm_files, topic_key=_topic_route)
                notify_ok = True if notify_result is None else bool(notify_result)
            except Exception as nf_e:
                logger.warning("Failed to send go_live confirmation files: %s", nf_e)
                notify_result = self.notifier.notify_admin(notify_msg, topic_key=_topic_route)
                notify_ok = True if notify_result is None else bool(notify_result)
        elif should_send_go_live_notice:
            notify_result = self.notifier.notify_admin(notify_msg, topic_key=_topic_route)
            notify_ok = True if notify_result is None else bool(notify_result)
        else:
            logger.info(
                "🔕 Go-live existing-case notification suppressed: laf=%s case=%s folder=%s topic=%s",
                laf_number,
                case_number,
                folder_label,
                _topic_route,
            )
        if notify_ok and notice_mark_done and notice_dedup_key:
            try:
                notice_mark_done(
                    "laf_go_live_existing_status_notice",
                    notice_dedup_key,
                    metadata=notice_dedup_metadata,
                )
            except Exception as mark_error:
                logger.debug("Go-live existing-case notice dedup mark failed (%s): %s", laf_number, mark_error)

        self._log_event(laf_number, "go_live", {
            "client_name": client_name,
            "case_type": case_type,
            "case_reason": case_reason,
            "is_duplicate": existing is not None,
            "vision_date": extracted_date,
            "poa_submit_date": submission_info.get("date_iso") or poa_submit_date,
            "submission_source": submission_info.get("source", ""),
            "go_live_prefill_ok": go_live_prefill_ok,
        }, "success")
        _eventlog(
            "laf:go_live:done",
            ok=True,
            payload={
                "case_type": case_type, "case_reason": case_reason,
                "is_duplicate": existing is not None,
                "vision_date": extracted_date,
                "go_live_prefill_ok": go_live_prefill_ok,
                "submission_confidence": submission_info.get("confidence", ""),
            },
            tags={"laf_case_no": laf_number, "client_name": client_name},
        )
        return {
            "ok": True,
            "created_case": not self.dry_run and bool(verified_case),
            "created_case_id": str((verified_case or {}).get("id") or case_number or ""),
            "case_number": str((verified_case or {}).get("case_number") or case_number or ""),
            "folder_path": str((verified_case or {}).get("folder_path") or db_path or ""),
            "existing": existing is not None,
        }

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

    def handle_progress_report(self, case_info, force: bool = False, suppress_notify: bool = False):
        """
        T3 未結案件進度定期回報 (Phase 1 draft-only).
        0. Gate 1: mark email ID processed immediately (even if draft fails)
        0. Gate 2: if pending token exists for this case, send reminder and return
        1. 確認案件在 DB
        2. 揀選最新 court/doc PDF
        3. 建 remark
        4. 呼叫 skill portal-action progress draft
        5. 產 confirm_token 送 laf_progress 頻道
        force=True bypasses Gates 1 & 2 (manual re-trigger).
        """
        client_name = getattr(case_info, 'client_name', '') or ''
        laf_case_no = getattr(case_info, 'laf_case_number', '') or ''
        subject = getattr(case_info, 'subject', '') or ''
        message_id = getattr(case_info, 'message_id', '') or ''
        logger.info("📋 handle_progress_report: client=%s laf_no=%s", client_name, laf_case_no)

        if not force:
            # Gate 1: mark this Gmail message_id as processed immediately,
            # so that lookback scans won't retry the same email even if draft fails.
            if message_id:
                try:
                    import json as _json
                    _processed_path = str(get_laf_processed_emails_path())
                    _data: dict = {}
                    if os.path.exists(_processed_path):
                        with open(_processed_path, 'r', encoding='utf-8') as _f:
                            _data = _json.load(_f) or {}
                    if isinstance(_data, list):
                        # Legacy format: list of IDs
                        if message_id not in _data:
                            _data.append(message_id)
                    elif isinstance(_data, dict):
                        _data[message_id] = True
                    os.makedirs(os.path.dirname(_processed_path), exist_ok=True)
                    _tmp = _processed_path + '.tmp'
                    with open(_tmp, 'w', encoding='utf-8') as _f:
                        _json.dump(_data, _f, ensure_ascii=False)
                    os.replace(_tmp, _processed_path)
                except Exception as _ge:
                    logger.debug("progress Gate 1 write failed: %s", _ge)

            # Gate 2: if an unexpired pending token already exists for this case,
            # send a reminder and skip creating a new draft.
            if laf_case_no:
                try:
                    from api.domains.laf_flow import find_pending_progress_token_for_case
                    _existing_tok, _existing_ent = find_pending_progress_token_for_case(self, laf_case_no)
                    if _existing_tok and _existing_ent:
                        _exp = float(_existing_ent.get('expires_at', 0) or 0)
                        _remain = max(0, int((_exp - time.time()) / 60))
                        _reminder = (
                            f"ℹ️ 案件 {laf_case_no}（{client_name}）已有待確認的進度回報\n"
                            f"確認碼：`{_existing_tok}`（剩餘約 {_remain} 分鐘）\n"
                            f"請回覆確認碼以送出，或回覆「取消」重新填寫。"
                        )
                        if (not suppress_notify) and hasattr(self, 'notifier') and self.notifier:
                            try:
                                # 修正既有 bug（2026-04-18 commit 0eb584fd 寫錯）：notify→notify_admin, topic→topic_key
                                self.notifier.notify_admin(_reminder, topic_key='laf_progress')
                            except Exception:
                                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 2081, exc_info=True)
                        return
                except Exception as _g2e:
                    logger.debug("progress Gate 2 check failed: %s", _g2e)

        # 1. 確認 DB 有此案
        if laf_case_no:
            existing = self._check_duplicate(laf_case_no, client_name, '', '')
        else:
            existing = None
        if not existing:
            msg = (
                f"⚠️ 進度回報 email 收到（{client_name} / {laf_case_no}），"
                f"但 DB 找不到對應案件。請先手動建案後重新觸發。"
            )
            logger.warning(msg)
            if hasattr(self, 'notifier') and self.notifier:
                try:
                    self.notifier.notify_admin(msg)
                except Exception:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 2101, exc_info=True)
            return

        # 2. NAS folder
        try:
            from api.case_path_mapper import translate_case_path_to_local
            folder_path = translate_case_path_to_local(
                str(existing.get('folder_path') or existing.get('case_folder_path') or '')
            )
        except Exception:
            folder_path = ''

        if not folder_path or not os.path.isdir(str(folder_path)):
            msg = (
                f"⚠️ 進度回報：找不到 NAS 資料夾（{client_name} / {laf_case_no}）。"
                f"請確認資料夾路徑正確後手動觸發。"
            )
            logger.warning(msg)
            if hasattr(self, 'notifier') and self.notifier:
                try:
                    self.notifier.notify_admin(msg)
                except Exception:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 2123, exc_info=True)
            return

        # 3. 揀選 PDF
        try:
            from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import (
                pick_latest_pdf, build_progress_remark,
            )
            from pathlib import Path
            court_pdf = pick_latest_pdf(Path(folder_path), 'court')
            doc_pdf = pick_latest_pdf(Path(folder_path), 'doc')
            remark = build_progress_remark(court_pdf, doc_pdf)
        except Exception as e:
            msg = (
                f"⚠️ 進度回報：找不到法院通知/書狀 PDF（{client_name} / {laf_case_no}）：{e}"
            )
            logger.warning(msg)
            if hasattr(self, 'notifier') and self.notifier:
                try:
                    self.notifier.notify_admin(msg)
                except Exception:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 2144, exc_info=True)
            return

        # 4. Phase 1 draft — spawn laf_orchestrator.py --mode portal-draft directly.
        # Skip action.py intermediate to keep the chain at 2 levels only.
        # laf_orchestrator.py calls os._exit(0) after printing JSON to avoid
        # Playwright asyncio cleanup hang in the grandchild process.
        draft_result = {}
        try:
            import subprocess as _sp
            import sys as _sys
            import json as _json
            _orch_script = os.path.abspath(__file__)
            _skill_py = str(_sys.executable)
            portal_timeout = int(os.environ.get("MAGI_LAF_PORTAL_DRAFT_TIMEOUT_SEC", "900"))
            _cmd = [
                _skill_py, _orch_script,
                '--mode', 'portal-draft',
                '--action', 'progress',
                '--laf-case-no', laf_case_no,
                '--client', client_name,
                '--no-notify',
            ]
            if remark:
                _cmd += ['--reason', remark]
            # P1-1: pass PDF files to draft subprocess so portal can upload them
            _upload_files = [str(p) for p in [court_pdf, doc_pdf] if p]
            if _upload_files:
                _cmd += ['--fields-json', _json.dumps({"upload_files": _upload_files})]
            _env = os.environ.copy()
            _env['MAGI_TRANSLATOR_NO_VENV'] = '1'
            logger.info("progress draft subprocess starting (timeout=%ds)", portal_timeout)
            _cp = _sp.run(_cmd, capture_output=True, text=True,
                          timeout=portal_timeout, env=_env)
            logger.info("progress draft subprocess done rc=%d", _cp.returncode)
            _stdout = (_cp.stdout or '').strip()
            # Try full stdout as JSON (indent=2 multi-line)
            try:
                _parsed = _json.loads(_stdout)
                if isinstance(_parsed, dict):
                    draft_result = _parsed
            except Exception:
                # Fallback: scan reversed lines for last single-line JSON dict
                for _line in reversed(_stdout.splitlines()):
                    _line = _line.strip()
                    if _line.startswith('{') and _line.endswith('}'):
                        try:
                            _parsed = _json.loads(_line)
                            if isinstance(_parsed, dict):
                                draft_result = _parsed
                                break
                        except Exception:
                            continue
        except Exception as e:
            logger.error("progress draft skill failed: %s", e)
            draft_result = {}

        # P0-3: Only register confirm_token if the draft actually succeeded.
        # draft_result.get('ok') is set by execute_portal_workflow_draft;
        # draft_result.get('success') covers other success-style returns.
        _draft_ok = isinstance(draft_result, dict) and (
            draft_result.get('ok') or draft_result.get('success')
        )
        if not _draft_ok:
            _draft_err = (draft_result or {}).get('error') or 'portal_draft_failed'
            _fail_msg = (
                f"⚠️ 進度回報草稿失敗（{client_name} / {laf_case_no}）：{_draft_err}\n"
                f"請手動觸發或稍後重試。"
            )
            logger.error(_fail_msg)
            if hasattr(self, 'notifier') and self.notifier:
                try:
                    self.notifier.notify_admin(_fail_msg)
                except Exception:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 2218, exc_info=True)
            return

        # 5. Register confirm_token and notify laf_progress channel
        try:
            from api.domains.laf_flow import register_laf_progress_submit_pending
            token = register_laf_progress_submit_pending(
                self,
                platform='discord',
                requester_user_id='',
                payload={
                    'laf_case_no': laf_case_no,
                    'client_name': client_name,
                    'remark': remark,
                    'court_pdf': str(court_pdf) if court_pdf else '',
                    'doc_pdf': str(doc_pdf) if doc_pdf else '',
                },
                result_data=draft_result,
            )
        except Exception as e:
            logger.error("register_laf_progress_submit_pending failed: %s", e)
            token = ''

        if not isinstance(draft_result, dict):
            draft_result = {}
        # screenshot_path may live in preview.png (portal-draft result format)
        _preview = draft_result.get('preview') or {}
        screenshot_path = (
            draft_result.get('screenshot_path')
            or (_preview.get('png') if isinstance(_preview, dict) else '')
            or ''
        )
        try:
            if (not suppress_notify) and hasattr(self, 'notifier') and self.notifier:
                court_name = str(court_pdf.name) if court_pdf else '（未找到）'
                doc_name = str(doc_pdf.name) if doc_pdf else '（未找到）'
                lines = [
                    f"📋 案件進度回報草稿已填寫",
                    f"案號：{laf_case_no}",
                    f"當事人：{client_name}",
                    f"remark：{remark}",
                    f"已上傳：{court_name} / {doc_name}",
                ]
                if token:
                    lines.append(f"確認碼：`{token}`（30 分鐘內回覆此碼以送出）")
                # 修正既有 bug（2026-04-18 commit 0eb584fd）：notify→notify_admin/_with_files，topic→topic_key
                # attachment kwarg 不存在；notify_admin_with_files 用 file_paths list
                _msg = '\n'.join(lines)
                if screenshot_path:
                    self.notifier.notify_admin_with_files(
                        _msg,
                        file_paths=[screenshot_path],
                        topic_key='laf_progress',
                    )
                else:
                    self.notifier.notify_admin(_msg, topic_key='laf_progress')
        except Exception as e:
            logger.error("progress notify failed: %s", e)

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
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 2534, exc_info=True)
        _is_criminal_case = "/刑事/" in _folder_str
        # 資料夾名稱含「-偵查-」才是偵查階段（如 2026-0002-[當事人S]-偵查-過失致死）
        _is_investigation = "-偵查-" in _folder_str

        docs = self._scan_case_folder_docs(folder_path, action="closing") if folder_path else self._empty_docs_map()
        if folder_path and not docs.get("closing_basis_files"):
            if _is_investigation:
                # 偵查案件：結案依據可能是不起訴處分書、偵結報告等，不強制要求
                logger.info("  ℹ️ 偵查案件 %s 無結案基礎文件，允許繼續", case_number)
            else:
                logger.warning("  ⚠️ 無法產生結案報告：%s 缺少結案基礎文件 (%s)", case_number, folder_path)
                self.notifier.notify_admin(
                    f"⚠️ 無法產生結案報告：\n案號：{case_number}\n當事人：{client_name}\n"
                    f"原因：`{judgment_folder_name(10)}` 資料夾中找不到「起訴書/判決/裁定/不起訴處分書/確定證明書」檔案；強制執行案件可放入執行命令。"
                )
                return
        # 註：不再檢查 office_receipt_files（收文章/回執）。
        # LAF portal 結案只需要判決/裁定/不起訴處分書，收文章證據是事務所內部流程，
        # 律師在 Discord 觸發「準備結案」時已代表親自確認文件齊全，系統不再二次把關。

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
        *,
        suppress_notify: bool = False,
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
                            "SELECT `case_number`, `client_name` FROM `cases` "
                            "WHERE `legal_aid_number` = %s OR `laf_case_no` = %s OR `application_no` = %s "
                            "OR `notes` LIKE %s LIMIT 1",
                            (case_number, case_number, case_number, f"%{case_number}%"),
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
        self._last_portal_error = ""
        self._last_portal_artifact = {}
        automation = None
        try:
            from skills.legal.laf import _export_file_to_static

            username = os.environ.get("MAGI_LAF_USERNAME") or self.laf_config.get("username", "")
            password = os.environ.get("MAGI_LAF_PASSWORD") or self.laf_config.get("password", "")

            if not username or not password:
                raise RuntimeError("LAF credentials not configured (laf.username / laf.password)")

            automation = self._get_automation()
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
                    _portal_error = str(getattr(automation, "last_portal_error", "") or "").strip()
                    if not _portal_error:
                        _portal_error = "portal draft save failed"
                    raw_art = getattr(automation, "last_debug_artifact", {}) or {}
                    upload_res = getattr(automation, "last_upload_result", {}) or {}
                    art = {}
                    if isinstance(raw_art, dict) and raw_art:
                        art = dict(raw_art)
                    if upload_res:
                        art["upload_result"] = upload_res
                    self._last_portal_artifact = art
                    raise RuntimeError(_portal_error)
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
                # Keep the shared report/download portal session alive for follow-up attachments.
                pass
        except Exception as e:
            # Do not silently pass. Report to admin and mark an error event.
            self._last_portal_error = str(e)
            try:
                if automation is not None:
                    raw_art = getattr(automation, "last_debug_artifact", {}) or {}
                    upload_res = getattr(automation, "last_upload_result", {}) or {}
                    art = dict(raw_art) if isinstance(raw_art, dict) else {}
                    if upload_res:
                        art["upload_result"] = upload_res
                    if art:
                        self._last_portal_artifact = art
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2989, exc_info=True)
            if not suppress_notify:
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
        if not suppress_notify:
            self.notifier.notify_admin("\n".join(lines), topic_key="laf_closing")
        return True

    def execute_portal_workflow_draft(
        self,
        workflow: str,
        case_number: str,
        client_name: str = "",
        fields: Optional[dict] = None,
        *,
        suppress_notify: bool = False,
    ) -> bool:
        """
        通用法扶 workflow 填寫。
        go_live（開辦）沒有暫存狀態，只能預填截圖或正式送出；其他 workflow 仍可暫存。
        workflow: go_live | condition | inquiry | withdrawal | fee
        """
        wf = (workflow or "").strip()
        is_go_live = wf == "go_live"
        workflow_label = "開辦預填" if is_go_live else f"{wf} 暫存"
        portal_status = "prefilled" if is_go_live else "draft_saved"
        event_status = "prefill" if is_go_live else "draft"
        self._last_portal_error = ""
        if not wf:
            self._last_portal_error = "missing_workflow"
            return False
        if not case_number and not client_name:
            logger.warning("Portal %s skipped: missing case_number/client_name", workflow_label)
            self._last_portal_error = "missing case_number/client_name"
            return False
        if self.dry_run:
            logger.info("  [DRY RUN] Would prepare %s for %s/%s", workflow_label, case_number, client_name)
            return True

        logger.info("🌐 Executing portal %s for %s (%s)", workflow_label, client_name or "-", case_number or "-")
        self._last_portal_artifact = {}
        try:
            from skills.legal.laf import _export_file_to_static

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
                raise RuntimeError(f"portal {wf} {'prefill' if is_go_live else 'draft save'} failed")
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
            self._last_portal_error = str(e)
            try:
                if not suppress_notify:
                    if is_go_live:
                        self.notifier.notify_admin(
                            f"❌ 開辦預填失敗 — {case_number or client_name}\n原因：{e}",
                            topic_key="laf_go_live",
                        )
                    else:
                        self.notifier.notify_admin(f"❌ {wf} 暫存失敗 — {case_number or client_name}\n原因：{e}")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2110, exc_info=True)
            self._log_event(case_number or client_name, wf, {"error": str(e), "fields": fields or {}}, "error")
            return False

        self._log_event(
            case_number or client_name,
            wf,
            {"portal_status": portal_status, "fields": fields or {}, "artifact": self._last_portal_artifact},
            event_status,
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
        if wf not in {"go_live", "progress"}:
            logger.warning("Portal submit blocked: only go_live/progress are allowed (%s)", wf)
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
            from skills.legal.laf import _export_file_to_static

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

    def _get_automation(self, *, fresh: bool = False):
        """Get or create the v2 shared LAFWebAutomation instance.

        Portal workflows such as closing/go_live/condition require methods that
        do not exist in the legacy ``skills.legal.laf.LAFWebAutomation`` class.
        Always use the v2 automation here so report closing cannot silently fall
        back to the old downloader-only implementation.
        """
        if self._automation and not fresh:
            # TODO: Add health check or expiry?
            # For now, rely on .login() inside scripts to check cookie validity.
            return self._automation

        provider_fixture = _load_workflow_provider_fixture()
        if provider_fixture is not None:
            automation = _FixtureWorkflowAutomation(*provider_fixture)
            if fresh:
                return automation
            self._automation = automation
            return self._automation

        from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFWebAutomation

        username = os.environ.get("MAGI_LAF_USERNAME") or self.laf_config.get("username", "")
        password = os.environ.get("MAGI_LAF_PASSWORD") or self.laf_config.get("password", "")
        download_folder = self.laf_config.get("download_folder", "./laf_downloads")
        headless = bool(self.laf_config.get("headless", True))
        base_url = (self.laf_config.get("base_url", "") or "").strip()
        browser_profile_dir = self.laf_config.get("browser_profile_dir", "")
        
        automation = LAFWebAutomation(
            username=username,
            password=password,
            download_folder=download_folder,
            headless=headless,
            log_callback=lambda msg: logger.info("[LAF] %s", msg),
            base_url=base_url,
            mock_mode=False,
            browser_profile_dir=browser_profile_dir,
        )
        if fresh:
            return automation
        self._automation = automation
        return self._automation

    def close(self):
        """Cleanup resources."""
        if self._automation:
            try:
                self._automation.close()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2227, exc_info=True)
            self._automation = None

    def execute_portal_go_live_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None, *, suppress_notify: bool = False) -> bool:
        ok = self.execute_portal_workflow_draft("go_live", case_number, client_name, fields, suppress_notify=suppress_notify)
        if ok and (not self.dry_run) and (not suppress_notify):
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

    def execute_portal_go_live_submit(self, case_number: str, client_name: str = "", fields: Optional[dict] = None, *, suppress_notify: bool = False) -> bool:
        ok = self.execute_portal_workflow_submit("go_live", case_number, client_name, fields)
        if ok and (not self.dry_run) and (not suppress_notify):
            self.notifier.notify_admin(f"✅ 已送出開辦回報 — {client_name or '-'}（{case_number or '-'}）", topic_key="laf_go_live")
        return ok

    def execute_portal_withdrawal_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None, *, suppress_notify: bool = False) -> bool:
        ok = self.execute_portal_workflow_draft("withdrawal", case_number, client_name, fields, suppress_notify=suppress_notify)
        if ok and (not self.dry_run) and (not suppress_notify):
            self.notifier.notify_admin(f"✅ 已暫存撤回資料 — {client_name or '-'}（{case_number or '-'}）")
        return ok

    def execute_portal_inquiry_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None, *, suppress_notify: bool = False) -> bool:
        ok = self.execute_portal_workflow_draft("inquiry", case_number, client_name, fields, suppress_notify=suppress_notify)
        if ok and (not self.dry_run) and (not suppress_notify):
            reason = (fields or {}).get("desc", "")
            msg = f"✅ 已暫存疑義資料 — {client_name or '-'}（{case_number or '-'}）\n說明：{reason}\n請務必登入平台確認內容是否正確。"
            if "其他" in reason or "0117" in str((fields or {}).get("rsm_reqsubj2", "")):
                 msg += "\n(⚠️ 類別為'其他'，請手動補充細節)"
            self.notifier.notify_admin(msg)
        return ok

    def execute_portal_condition_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None, *, suppress_notify: bool = False) -> bool:
        ok = self.execute_portal_workflow_draft("condition", case_number, client_name, fields, suppress_notify=suppress_notify)
        if ok and (not self.dry_run) and (not suppress_notify):
            self.notifier.notify_admin(f"✅ 已暫存二階段資料 — {client_name or '-'}（{case_number or '-'}）")
        return ok

    def execute_portal_fee_draft(self, case_number: str, client_name: str = "", fields: Optional[dict] = None, *, suppress_notify: bool = False) -> bool:
        ok = self.execute_portal_workflow_draft("fee", case_number, client_name, fields, suppress_notify=suppress_notify)
        if ok and (not self.dry_run) and (not suppress_notify):
            self.notifier.notify_admin(f"✅ 已暫存費用支付資料 — {client_name or '-'}（{case_number or '-'}）")
        return ok

    def _lookup_case_identity(
        self,
        *,
        laf_case_number: str = "",
        case_number: str = "",
        client_name: str = "",
        reason_hint: str = "",
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
                    "SELECT `id`, `case_number`, `client_name`, `legal_aid_number`, "
                    "`laf_case_no`, `application_no`, `notes`, `folder_path`, `legal_aid_status` "
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
            laf_no = (
                str(row.get("legal_aid_number") or "").strip()
                or str(row.get("laf_case_no") or "").strip()
                or str(row.get("application_no") or "").strip()
            )
            if not laf_no:
                laf_no = self._extract_laf_case_number_from_text(str(row.get("notes") or "").strip())
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
            for r in _query_candidates(
                "TRIM(COALESCE(`legal_aid_number`, '')) = %s "
                "OR TRIM(COALESCE(`laf_case_no`, '')) = %s "
                "OR TRIM(COALESCE(`application_no`, '')) = %s "
                "OR COALESCE(`notes`, '') LIKE %s",
                (out["laf_case_number"], out["laf_case_number"], out["laf_case_number"], f"%{out['laf_case_number']}%"),
            ):
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


        filtered.sort(key=lambda x: (int(x.get("score") or 0), str(x.get("id") or "")), reverse=True)

        # ── Status Prioritization & Keyword Disambiguation ──
        # If still ambiguous, try to pick based on status (進行中 preferred)
        # and keywords (reason_hint).
        if len(filtered) > 1:
            # 1. Expand reason_hint to keywords
            _keywords = self._expand_reason_keywords(reason_hint) if reason_hint else []
            if _keywords:
                logger.debug("  🔍 Disambiguating with keywords: %s", _keywords)
                keyword_matched = []
                for cand in filtered:
                    _fpath = str(cand.get("folder_path") or "").lower()
                    _cno = str(cand.get("case_number") or "").lower()
                    if any((k in _fpath or k in _cno) for k in _keywords):
                        keyword_matched.append(cand)
                
                if keyword_matched:
                    logger.info("  🔍 Keyword match filtered candidates: %d -> %d", len(filtered), len(keyword_matched))
                    filtered = keyword_matched

            # 2. Prioritize "進行中" (In Progress)
            if len(filtered) > 1:
                in_progress = [c for c in filtered if c.get("legal_aid_status") == "進行中"]
                if in_progress:
                    logger.info("  🔍 Prioritizing '進行中' cases: %d -> %d", len(filtered), len(in_progress))
                    filtered = in_progress

        # Final check for ambiguity after all filters
        if len(filtered) > 1:
            # Check if they are actually the same instance (identical folder or IDs)
            _unique_ids = {c.get("id") for c in filtered if c.get("id")}
            if len(_unique_ids) > 1:
                out["needs_manual_confirm"] = True
                out["manual_reason"] = "identity_still_ambiguous"
                out["manual_hint"] = "發現多個審級或資料夾，請補上更具體的案由關鍵字"
                out["top_candidates"] = [
                    {
                        "laf_case_number": str(c.get("laf_case_number") or ""),
                        "case_number": str(c.get("case_number") or ""),
                        "client_name": str(c.get("client_name") or ""),
                        "case_folder": str(c.get("case_folder") or ""),
                        "status": str(c.get("legal_aid_status") or ""),
                    }
                    for c in filtered[:5]
                ]
                return out
            else:
                # Same logical record found multiple times (odd but handled)
                filtered = [filtered[0]]

        if not filtered:
            if rejected:
                out["needs_manual_confirm"] = True
                out["manual_reason"] = "identity_signal_conflict"
                out["conflicts"] = rejected[:5]
                return out
            
            # fallback_find_case_folders logic remains here...
            fallback = self._fallback_find_case_folders(
                client_name=out["client_name"],
                laf_case_number=out["laf_case_number"],
                limit=20,
            )
            # Filter fallback by keywords too
            if len(fallback) > 1 and reason_hint:
                _keywords = self._expand_reason_keywords(reason_hint)
                keyword_matched_fallback = [fb for fb in fallback if any(k in fb for k in _keywords)]
                if keyword_matched_fallback:
                    fallback = keyword_matched_fallback
            
            if len(fallback) == 1 and (req_laf or req_case or req_client):
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
                # Clear confirm flag if we found a unique fallback
                out["needs_manual_confirm"] = False
                out["manual_reason"] = ""
                return out

            out["needs_manual_confirm"] = True
            out["manual_reason"] = "identity_not_found"
            out["fallback_candidates"] = fallback[:5]
            return out

        top = filtered[0]
        top_score = int(top.get("score") or 0)

        matched = list(top.get("matched") or [])
        # Relax the strong-signal requirement when there is exactly ONE candidate
        # and client_name matches (Smart resolution)
        _sole_candidate_client_match = (
            len(filtered) == 1
            and "client_name" in matched
            # As long as we have a result that isn't ambiguous, we allow it.
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
            # Clear the early "missing_case_or_laf_signal" flag
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

        if out["laf_case_number"] and str(top.get("id") or "").strip():
            self._update_legal_aid_number(str(top.get("id") or "").strip(), out["laf_case_number"])

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
    def _expand_reason_keywords(reason_hint: str) -> list[str]:
        return _expand_reason_keywords(reason_hint)

    @staticmethod
    def _is_case_folder_name(name: str) -> bool:
        return bool(re.match(r"^\d{4}-\d{4}-", str(name or "").strip()))

    @staticmethod
    def _laf_case_roots() -> List[str]:
        roots: List[str] = []
        for root in list(preferred_case_roots(include_closed=True)) + list(default_case_roots(include_closed=True)):
            p = os.path.join(root, "法扶案件")
            if p not in roots and _is_dir_accessible(p):
                roots.append(p)
        return roots

    def _fallback_find_case_folder(self, client_name: str = "", laf_case_number: str = "") -> str:
        candidates = self._fallback_find_case_folders(client_name=client_name, laf_case_number=laf_case_number, limit=1)
        return candidates[0] if candidates else ""

    @staticmethod
    def _case_folder_alias_key(path: str) -> str:
        """Collapse NAS and local-sync aliases of the same logical case folder.

        Active and archived copies deliberately keep different prefixes, so an
        archived folder remains available to the closing-document probe.
        """
        normalized = str(path or "").strip().replace("\\", "/").rstrip("/")
        lowered = normalized.casefold()
        markers = (
            ("/03_工作資料/10_結案/", "closed:"),
            ("/01_案件/", "active:"),
        )
        for marker, prefix in markers:
            index = lowered.find(marker.casefold())
            if index >= 0:
                return prefix + lowered[index + len(marker):]
        return "path:" + lowered

    def _fallback_find_case_folders(self, client_name: str = "", laf_case_number: str = "", limit: int = 20) -> List[str]:
        cname = (client_name or "").strip()
        laf_no = (laf_case_number or "").strip()
        roots = self._laf_case_roots()
        if not roots:
            return []
        try:
            scan_budget_sec = max(1.0, float(os.environ.get("MAGI_LAF_FALLBACK_SCAN_BUDGET_SEC", "5") or "5"))
        except Exception:
            scan_budget_sec = 5.0
        deadline = time.monotonic() + scan_budget_sec

        def _budget_exhausted() -> bool:
            return time.monotonic() >= deadline

        # ``roots`` is already ordered by storage preference.  Keep that
        # ordering in the score instead of matching workstation-specific
        # absolute paths, because the same release may run on another host.
        root_priority = {
            os.path.normcase(os.path.normpath(root)): len(roots) - index
            for index, root in enumerate(roots)
        }
        scored: List[tuple[int, int, float, str]] = []
        loose_candidates: List[str] = []
        for root in roots:
            if _budget_exhausted():
                logger.warning("LAF fallback folder scan stopped by %.1fs budget before root: %s", scan_budget_sec, root)
                break
            try:
                for cat in _safe_listdir(root):
                    if _budget_exhausted():
                        break
                    cat_path = os.path.join(root, cat)
                    if not _is_dir_accessible(cat_path):
                        continue
                    for d in _safe_listdir(cat_path):
                        if _budget_exhausted():
                            break
                        case_path = os.path.join(cat_path, d)
                        if not _is_dir_accessible(case_path):
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
                            # Shallow filename check (top-level + 01_法扶資料 only)
                            # Avoid os.walk — NAS directories with many files cause I/O hang.
                            try:
                                found = False
                                for _check_dir in [case_path, os.path.join(case_path, "01_法扶資料")]:
                                    if _is_dir_accessible(_check_dir):
                                        if any(laf_no in f for f in _safe_listdir(_check_dir)):
                                            found = True
                                            break
                                if found:
                                    score += 1
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2648, exc_info=True)
                        if score > 0:
                            mtime = _safe_getmtime(case_path)
                            normalized_root = os.path.normcase(os.path.normpath(root))
                            scored.append((score, root_priority.get(normalized_root, 0), float(mtime), case_path))
                        else:
                            if len(loose_candidates) < max(20, int(limit or 1) * 4):
                                loose_candidates.append(case_path)
            except Exception:
                continue
        if scored:
            scored.sort(
                key=lambda x: (
                    x[0],
                    x[1],
                    x[2],
                ),
                reverse=True,
            )
            out: List[str] = []
            seen_aliases: set[str] = set()
            for _score, _root_priority, _mtime, path in scored:
                alias_key = self._case_folder_alias_key(path)
                if alias_key in seen_aliases:
                    continue
                seen_aliases.add(alias_key)
                out.append(path)
            return out[: max(1, int(limit or 1))]
        if not loose_candidates:
            return []
        # Precision-first default: do not auto-pick "most recent" folder unless explicitly enabled.
        if not self.allow_loose_case_folder_fallback:
            return []
        loose_candidates = sorted(loose_candidates, key=lambda p: _safe_getmtime(p), reverse=True)
        return loose_candidates[: max(1, int(limit or 1))]

    @staticmethod
    def _same_case_folder_path_candidates(folder: str) -> List[str]:
        """Map a known case folder path to all active/closed case roots."""
        text = str(folder or "").strip().replace("\\", "/")
        if not text:
            return []
        rels: List[str] = []
        for marker in ("/01_案件/", "/03_工作資料/10_結案/"):
            if marker in text:
                rel = text.split(marker, 1)[1].strip("/")
                if rel and rel not in rels:
                    rels.append(rel)
        if "/法扶案件/" in text:
            rel = "法扶案件/" + text.split("/法扶案件/", 1)[1].strip("/")
            if rel and rel not in rels:
                rels.append(rel)
        if not rels:
            return []

        out: List[str] = []
        roots = list(preferred_case_roots(include_closed=True)) + list(default_case_roots(include_closed=True))
        for root in roots:
            for rel in rels:
                cand = os.path.join(str(root).rstrip("/"), rel)
                if cand not in out and _is_dir_accessible(cand):
                    out.append(cand)
        return out

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
            "go_live": lambda d: is_go_live_ready(d, is_consumer_debt=False),
            "condition": lambda d: bool(d["mediation_failure_files"]),
            "fee": lambda d: bool(d["pink_receipt_files"]),
            "withdrawal": lambda d: bool(self._get_withdrawal_pdf_candidates(d)),
            "closing": lambda d: bool(d.get("closing_basis_files") or d.get("mediation_success_files")),
        }
        wanted = needs.get(action, lambda _d: True)

        candidates: List[str] = []
        if current_folder and os.path.isdir(current_folder):
            candidates.append(current_folder)
        if action == "closing" and current_folder:
            for p in self._same_case_folder_path_candidates(current_folder):
                if p not in candidates:
                    candidates.append(p)
        prechecked: set[str] = set()
        prechecked_first_docs = None
        prechecked_first_folder = ""
        if action == "closing" and candidates:
            for p in list(candidates):
                prechecked.add(p)
                docs = self._scan_case_folder_docs(p, action=action)
                if prechecked_first_docs is None:
                    prechecked_first_docs = docs
                    prechecked_first_folder = p
                if wanted(docs):
                    return p, docs
        # Closing must also probe archives when the DB folder is stale or has
        # already been moved to 10_結案.
        if not candidates or action == "closing":
            for p in self._fallback_find_case_folders(client_name=client_name, laf_case_number=laf_case_number, limit=5):
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
        for p in [p for p in candidates if p not in prechecked]:
            if action == "go_live":
                docs, _ = self._scan_go_live_docs(p)
            else:
                docs = self._scan_case_folder_docs(p, action=action)
            if first_docs is None:
                first_docs = docs
                first_folder = p
            if action == "go_live":
                if is_go_live_ready(docs, is_consumer_debt=self._is_consumer_debt_case_folder(p)):
                    return p, docs
            elif wanted(docs):
                return p, docs
        if first_docs is None and prechecked_first_docs is not None:
            return prechecked_first_folder, prechecked_first_docs
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
        # Search shallow filenames first.  Avoid os.walk here: large LAF cases
        # may have tens of thousands of review/evidence files.
        try:
            for check_dir in [root, os.path.join(root, "01_法扶資料")]:
                if not os.path.isdir(check_dir):
                    continue
                for fn in sorted(os.listdir(check_dir))[:300]:
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
        """優先用 Vision/OCR 辨識手寫簽署日期；找不到才用檔名日期 fallback。

        檔名日期通常是 LAF 派發日（如 `_1150414`），律師實際簽署日可能晚數日；
        因此**先用 Vision 抓 PDF 內手寫文字 / 列印日期**，找不到才退回檔名。
        """
        date_from_vision = self._extract_date_with_vision(path_value)
        if date_from_vision:
            return date_from_vision
        return self._extract_date_from_filename(path_value)

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
                        full = os.path.join(sub_path, fn)
                        if fn.lower().endswith(".pdf") and is_stored_pleading_proof(fn, full_path=full, subdir="04_我方歷次書狀"):
                            pleading_candidates.append(full)
                elif sub.lower().endswith(".pdf") and is_stored_pleading_proof(sub, full_path=sub_path, subdir="04_我方歷次書狀"):
                    pleading_candidates.append(sub_path)

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

        # 回執的檔名日期 = 收到回執的日期（不是寄出日期）。
        # 有回執就不會有委任狀存底（沒有收狀章），郵戳也讀不到。
        # 所以回執只記錄「收受回執日期」，remark 由 _compose_go_live_remark 處理措辭。
        for receipt_path in receipt_candidates[:3]:
            fn_date = self._extract_date_from_filename(receipt_path)
            if fn_date:
                date_roc = self._iso_to_roc(fn_date)
                logger.info("  🎯 回執收受日期: %s (from %s)", date_roc, os.path.basename(receipt_path))
                return {
                    "date_roc": date_roc, "date_iso": fn_date,
                    "source": "receipt", "source_file": receipt_path,
                    "source_doc_type": "回執",
                    "confidence": "high",
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

    @staticmethod
    def _extract_doc_name_from_filename(filename: str) -> str:
        """從檔名中提取文件種類名稱（如「委任狀」「上訴狀」「聲請狀」等）。

        例如：
          "20260407 委任狀回執.pdf" → "委任狀"
          "20260320 上訴狀存底.pdf" → "上訴狀"
          "20260315 民事答辯狀.pdf" → "答辯狀"
          "20260401 開辦通知書.pdf" → "" (開辦通知書不算寄出文件)
        """
        import re
        base = os.path.splitext(os.path.basename(filename or ""))[0]
        # 移除日期前綴和常見後綴
        base = re.sub(r"^\d{4,8}\s*", "", base)
        base = re.sub(r"(存底|回執|收件回執|掃描|影本|正本|副本)$", "", base).strip()
        # 匹配已知的法律文件種類
        # 長名稱在前，避免「聲請狀」先於「家事聲請狀」匹配
        known = [
            "民事聲請狀", "刑事聲請狀", "家事聲請狀",
            "民事答辯狀", "民事起訴狀", "補充理由狀",
            "委任狀", "上訴狀", "抗告狀", "聲請狀", "答辯狀", "準備狀",
            "起訴狀", "告訴狀", "自訴狀", "陳報狀", "異議狀", "反訴狀",
            "追加狀", "辯護狀",
        ]
        for k in known:
            if k in base:
                return k
        # 如果檔名含「狀」但不在已知列表，嘗試擷取 X狀
        m = re.search(r"[\u4e00-\u9fff]{1,4}狀", base)
        if m:
            return m.group(0)
        return ""

    def _compose_go_live_remark(self, submission_info: dict, client_name: str = "",
                                is_consumer_debt: bool = False,
                                open_doc_date: str = "") -> str:
        """根據遞狀日期資訊生成自然語言 selRemark。

        消債案件：優先使用開辦通知書簽署日（Vision OCR 抓到的）；找不到才退回遞狀日。
        一般案件：用委任狀/書狀/回執日期。
        """
        date_roc = submission_info.get("date_roc", "")
        source = submission_info.get("source", "")
        doc_type = submission_info.get("source_doc_type", "委任狀")
        src_file = submission_info.get("source_file", "")

        # 消費者債務清理 — 開辦通知書簽署日 = 首次實質討論案情日（法扶實質開辦標準）
        # 統一用語：消債/訴訟代理一律寫「首次實質討論案情」，不再用「簽署接案通知書」。
        if is_consumer_debt:
            # open_doc_date 是 ISO format（_extract_best_date_from_doc 回傳）
            open_roc = self._iso_to_roc(open_doc_date) if open_doc_date else ""
            if open_roc and date_roc:
                return f"已於民國{open_roc}首次實質討論案情。已於民國{date_roc}遞送聲請狀至法院。"
            if open_roc:
                return f"已於民國{open_roc}首次實質討論案情。"
            if date_roc:
                return f"已於民國{date_roc}遞送聲請狀至法院。"
            return ""

        # open_roc：開辦通知書日期 = 首次實質討論案情日期
        # 法扶實質開辦標準：訴訟代理及辯護「遞送委任狀」、調解/和解/消債/法律文件/諮詢
        # 「首次實質討論案情」。對律師而言開辦通知書上的日期就是首次實質討論案情日期，
        # 無論案件類別都應一併寫進 selRemark（對訴訟類也只是雙保險，無害）。
        open_roc = self._iso_to_roc(open_doc_date) if open_doc_date else ""

        if not date_roc:
            # 沒有委任狀/書狀/回執日期 — 至少回報首次實質討論案情日期
            if open_roc:
                return f"已於民國{open_roc}首次實質討論案情。"
            return ""

        # 從檔名提取實際文件種類（如「委任狀」「上訴狀」等）
        doc_name = self._extract_doc_name_from_filename(src_file) or "委任狀"

        # 根據文件來源生成不同措辭
        if doc_type == "回執":
            # date_roc 是收到回執的日期（不是寄出日期），措辭要反映這個語義
            base = f"已寄出{doc_name}予法院，民國{date_roc}收受回執"
        elif doc_type == "書狀":
            base = f"已於民國{date_roc}遞送{doc_name}至法院"
        else:
            # 委任狀（預設）
            if source == "receipt":
                base = f"已於民國{date_roc}以掛號郵寄{doc_name}至法院"
            else:
                base = f"已於民國{date_roc}遞送{doc_name}至法院"

        if open_roc:
            return f"已於民國{open_roc}首次實質討論案情。{base}。"
        return f"{base}。"

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
        _notice_kw = ("開辦通知", "接案通知", "准予扶助", "開辦資料", "回報單", "開辦回報")
        if os.path.isdir(go_live_dir):
            for fn in os.listdir(go_live_dir):
                if fn.lower().endswith(".pdf") and (
                    any(k in fn for k in _notice_kw)
                    or is_opening_notice_filename(fn, full_path=os.path.join(go_live_dir, fn), subdir="02_開辦資料")
                ):
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
                        full = os.path.join(sub_path, fn)
                        if fn.lower().endswith(".pdf") and is_stored_pleading_proof(fn, full_path=full, subdir="04_我方歷次書狀"):
                            proof_file = full
                            break
                elif sub.lower().endswith(".pdf") and is_stored_pleading_proof(sub, full_path=sub_path, subdir="04_我方歷次書狀"):
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

    def _scan_case_folder_docs(self, case_folder: str, action: str = "") -> dict:
        # 使用 mixin 的完整版文件分類：
        # 含 closing_fee_files / change_review_notice_files 等鍵值，
        # 供 portal retry seed 與結案流程正確判斷是否已抓到酬金領款單。
        return super()._scan_case_folder_docs(case_folder, action=action)

    def _scan_go_live_docs(self, case_folder: str) -> tuple[dict, str]:
        """Scan prepared go-live source folder.

        01_法扶資料保存 portal 下載的空白表件；不可當成已簽/已填的開辦資料。
        """
        base = self._to_local_case_folder(case_folder) or case_folder
        docs = self._empty_docs_map()
        scan_dirs = [
            ("02_開辦資料", os.path.join(base, "02_開辦資料")),
            ("04_我方歷次書狀", os.path.join(base, "04_我方歷次書狀")),
            ("11_回執", os.path.join(base, "11_回執")),
        ]
        scanned: list[str] = []
        for label, scan_dir in scan_dirs:
            if not os.path.isdir(scan_dir):
                continue
            scanned.append(label)
            part = self._scan_case_folder_docs(scan_dir, action="go_live")
            for key, value in (part or {}).items():
                if isinstance(value, list):
                    docs.setdefault(key, [])
                    docs[key].extend(x for x in value if x not in docs[key])
        return docs, "、".join(scanned) if scanned else os.path.join(base, "02_開辦資料")

    def _existing_laf_portal_attachment_files(self, case_folder: str) -> list[str]:
        """Return existing official portal attachments under 01_法扶資料.

        These are downloaded/reference forms from the LAF portal.  They are
        intentionally counted separately from 02_開辦資料 because blank portal
        forms must not make a case look ready for go-live.
        """
        base = self._to_local_case_folder(case_folder) or translate_case_path_to_local(case_folder, require_existing=True) or case_folder
        laf_dir = os.path.join(base, "01_法扶資料")
        if not os.path.isdir(laf_dir):
            return []
        official_keywords = (
            "扶助律師接案通知書",
            "接案通知書",
            "委任狀",
            "法律扶助申請書",
            "案件概述單",
            "資力詢問表",
            "審查表",
            "准予扶助證明書",
            "預付酬金領款單",
            "結案回報書",
            "結案審查通知書",
            "結案酬金領款單",
        )
        out: list[str] = []
        for fn in _safe_listdir(laf_dir):
            if not fn or fn.startswith("."):
                continue
            full = os.path.join(laf_dir, fn)
            if not os.path.isfile(full):
                continue
            lower = fn.lower()
            if lower.endswith(".zip") or lower.endswith(".txt"):
                continue
            if any(keyword in fn for keyword in official_keywords):
                out.append(full)
        return sorted(out)

    def _dump_missing_docs_diagnostics(
        self,
        *,
        mode: str,
        case_folder: str,
        gl_dir: str,
        gl_docs: dict,
        missing: list,
        is_consumer_debt: bool,
    ) -> None:
        """記錄 missing_required_docs 失敗時的實際資料夾內容，供事後追查。

        2026-05-04 新增：解決 Synology Drive cloud-only placeholder 或路徑翻譯
        錯誤造成的「檔案明明在但 scan 不到」誤報。寫入 .runtime/laf_go_live_missing_diagnostics.jsonl。
        """
        try:
            from api.runtime_paths import get_runtime_dir as _get_runtime_dir
            runtime_dir = str(_get_runtime_dir())
        except Exception:
            runtime_dir = os.path.join(_MAGI_ROOT, ".runtime")
        try:
            os.makedirs(runtime_dir, exist_ok=True)
        except Exception:
            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 4134, exc_info=True)
        diag_path = os.path.join(runtime_dir, "laf_go_live_missing_diagnostics.jsonl")
        # 取實際 listdir 結果（含每個檔案的 size + mtime）
        listdir_entries = []
        try:
            if os.path.isdir(gl_dir):
                for fn in sorted(os.listdir(gl_dir)):
                    full = os.path.join(gl_dir, fn)
                    try:
                        st = os.stat(full)
                        listdir_entries.append({
                            "name": fn,
                            "size": int(st.st_size),
                            "mtime": float(st.st_mtime),
                            "is_file": os.path.isfile(full),
                            "is_dir": os.path.isdir(full),
                        })
                    except OSError as _e:
                        listdir_entries.append({"name": fn, "stat_error": str(_e)})
        except OSError as _e:
            listdir_entries.append({"listdir_error": str(_e)})
        record = {
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "mode": mode,
            "case_folder": case_folder,
            "gl_dir": gl_dir,
            "gl_dir_exists": os.path.isdir(gl_dir),
            "is_consumer_debt": bool(is_consumer_debt),
            "missing": list(missing or []),
            "scanner_opening_notice": list(gl_docs.get("opening_notice_files") or []),
            "scanner_poa": list(gl_docs.get("poa_files") or []),
            "scanner_opening_proof": list(gl_docs.get("opening_proof_files") or []),
            "scanner_stored_pleading": list(gl_docs.get("stored_pleading_files") or []),
            "raw_listdir": listdir_entries,
        }
        try:
            with open(diag_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 4172, exc_info=True)
        try:
            logger.warning(
                "[LAF go_live missing_required_docs] mode=%s missing=%s gl_dir=%s entries=%d",
                mode, missing, gl_dir, len(listdir_entries),
            )
        except Exception:
            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 4179, exc_info=True)

    @staticmethod
    def _classify_doc_file(fn: str, full_path: str, out: dict) -> None:
        """Classify a single document file into the appropriate category."""
        if is_opening_notice_filename(fn, full_path=full_path):
            out["opening_notice_files"].append(full_path)
        if "委任狀" in fn:
            out["poa_files"].append(full_path)
            out.setdefault("opening_proof_files", []).append(full_path)
        if any(k in fn for k in ("調解不成立證明書", "調解不成立")):
            out["mediation_failure_files"].append(full_path)
        if any(k in fn for k in ("調解筆錄", "調解成立", "和解筆錄", "和解成立", "調解書")):
            if "不成立" not in fn:
                out["mediation_success_files"].append(full_path)
        low = fn.lower()
        if ("收據" in fn) or ("裁判費" in fn) or ("粉紅" in fn) or ("pink" in low):
            out["pink_receipt_files"].append(full_path)
        if "回執" in fn or "收件回執" in fn:
            out.setdefault("receipt_files", []).append(full_path)

    def _scan_closing_docs(self, case_folder: str) -> List[str]:
        """掃描結案相關文件（結案通知書、酬金明細等）。"""
        _CLOSING_KW = (
            "結案通知書",
            "酬金明細",
            "服務費用",
            "法律扶助費用",
            "終結通知",
            "結案通知",
            "結案轉入通知",
            "已轉入通知",
        )
        _SUBDIRS = [
            "",
            "01_法扶資料",
            os.path.join("01_法扶資料", "專員來信"),
            "02_開辦資料",
            "08_結案資料",
            "結案",
        ]
        # 專員寄送的轉入通知會以 .txt 歸檔；它是正式流程完成證據，
        # 不應因官網附件保存期限屆滿而被當成缺檔故障。
        allowed = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".txt"}
        results: List[str] = []
        root = str(case_folder or "").strip()
        if not root or not os.path.isdir(root):
            return results
        for subdir in _SUBDIRS:
            scan_path = os.path.join(root, subdir) if subdir else root
            if not os.path.isdir(scan_path):
                continue
            try:
                for fn in os.listdir(scan_path):
                    if Path(fn).suffix.lower() in allowed:
                        if any(k in fn for k in _CLOSING_KW):
                            results.append(os.path.join(scan_path, fn))
            except OSError:
                continue
        return results

    def _nas_satisfies_trigger(
        self,
        origin_reason: str,
        case_folder: str,
        *,
        evidence_after: str = "",
    ):
        """
        依 portal retry 觸發原因，判斷 NAS 是否已有對應檔案。
        回傳 (satisfied: bool, reason_str: str)。

        觸發類型對應規則：
          - go_live / opening / backfill → 需要已填開辦通知/回報單，或委任狀/書狀存底/回執
          - review_result / 審核結果 / 回報 → 需要酬金/領款單/審查結果類文件
          - closing / 結案 / 酬金 / fee → 需要結案通知書或酬金明細
          - archive_failed / portal_check_failed → 不預檢（強制 portal 重試）
          - 未知 → 不預檢
        """
        t = str(origin_reason or "").lower()
        folder = self._resolve_authoritative_case_folder_for_write(case_folder)
        if not folder:
            return False, ""

        # 審核結果/回報類：不能用開辦文件抵掉，需看到結案酬金或審查結果類文件。
        if any(k in t for k in ("review_result", "result_download", "審核", "審查", "回報")):
            docs = self._scan_case_folder_docs(folder, action="closing")
            closing_fee_files = self._evidence_paths_since(
                docs.get("closing_fee_files") or [], evidence_after
            )
            review_notice_files = self._evidence_paths_since(
                docs.get("change_review_notice_files") or [], evidence_after
            )
            if closing_fee_files:
                return True, "nas_has_closing_fee"
            if review_notice_files:
                return True, "nas_has_change_review_notice"
            # An archived email snapshot or an old generic closing notice proves
            # that mail arrived, not that the newly announced portal attachment
            # was actually downloaded.
            return False, ""

        # 結案類須優先於 generic backfill；否則
        # startup_backfill_missing_closing_docs 會被誤判成開辦附件。
        if any(k in t for k in ("closing", "結案", "酬金", "fee", "laf_closing")):
            docs = self._scan_case_folder_docs(folder, action="closing")
            if len(docs.get("closing_fee_files") or []) > 0:
                return True, "nas_has_closing_fee"
            closing = self._scan_closing_docs(folder)
            if closing:
                return True, "nas_has_closing_docs"
            return False, ""

        # 開辦類：只看準備資料夾，不以 01_法扶資料的官網空白表件抵掉。
        if any(k in t for k in ("go_live", "opening", "backfill")):
            docs, _scan_scope = self._scan_go_live_docs(folder)
            is_consumer_debt = self._is_consumer_debt_case_folder(folder)
            if is_go_live_ready(docs, is_consumer_debt=is_consumer_debt):
                if len(docs.get("opening_notice_files") or []) > 0:
                    return True, "nas_has_opening_notice"
                if len(go_live_proof_files(docs)) > 0:
                    return True, "nas_has_opening_proof"
            return False, ""

        # archive_failed / portal_check_failed → 強制重試，不預檢
        if any(k in t for k in ("archive_failed", "portal_check_failed")):
            return False, ""

        # 未知觸發類型 → 保守，不預檢
        return False, ""

    @staticmethod
    def _find_first_existing(paths: List[str]) -> str:
        for p in (paths or []):
            if p and os.path.exists(p):
                return p
        return ""

    @staticmethod
    def _resolve_authoritative_case_folder_for_write(folder: str) -> str:
        """Resolve a case folder to durable storage, never a local mirror."""

        raw = str(folder or "").strip()
        if not raw:
            return ""
        fixture_folder = _resolve_schedule_fixture_case_folder_for_write(raw)
        if fixture_folder:
            return fixture_folder
        canonical = translate_local_path_to_canonical(raw)
        for candidate in (canonical, raw):
            resolved = resolve_case_path_for_write(candidate)
            if resolved.get("ok") is True and resolved.get("local_path"):
                return str(resolved["local_path"])
        return ""

    def _resolve_case_folder_with_fallback(self, folder: str) -> str:
        """NAS 找不到時自動 fallback 到 Synology Drive，反之亦然。

        按 local_synology_path_candidates() 的候選順序嘗試所有已知掛載路徑
        （NAS /Volumes/、SynologyDrive CloudStation、user-level ~/.magi_mounts/），
        回傳第一個實際存在的資料夾路徑；所有候選都不存在時回空字串。

        This helper is read-only.  Portal downloads and NAS completion proofs
        must use ``_resolve_authoritative_case_folder_for_write`` instead.
        """
        f = (folder or "").strip()
        if not f:
            return ""
        # 快速路徑：原路徑已存在
        if os.path.isdir(f):
            return f
        # 嘗試所有候選路徑（NAS + Synology Drive 各種掛載變體）
        try:
            for cand in local_synology_path_candidates(f):
                if cand and cand != f and os.path.isdir(cand):
                    logger.debug(
                        "[LAF] 資料夾路徑 fallback: %s → %s",
                        os.path.basename(f.rstrip("/")),
                        cand,
                    )
                    return cand
        except Exception:
            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 4307, exc_info=True)
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
            if str(os.environ.get("MAGI_LAF_COPY_PDF_UPLOADS", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
                return src
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
        - 10_判決書或終局裁定及處分 (recursive): include all PDFs
        """
        root = (case_folder or "").strip()
        result = {
            "ok": False,
            "case_folder": root,
            "action": (action or "").strip(),
            "pleading_source_files": [],
            "judgment_pdf_files": [],
            "procedural_ruling_pdf_files": [],
            "mediation_success_pdf_files": [],
            "pdf_files": [],
            "converted": [],
            "failed": [],
            "staging_dir": "",
            "large_case_guard": {},
        }
        if not root or (not os.path.isdir(root)):
            result["error"] = "missing_case_folder"
            return result

        plead_root = os.path.join(root, "04_我方歷次書狀")
        judgment_roots = [os.path.join(root, name) for name in judgment_folder_aliases(10)]
        transcript_root = os.path.join(root, "08_筆錄")
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_laf = re.sub(r"[^\w\-]+", "_", (laf_case_no or "").strip()) or "unknown"
        safe_act = re.sub(r"[^\w\-]+", "_", (action or "").strip()) or "workflow"
        staging_dir = _create_laf_upload_staging_dir(
            os.path.join(tempfile.gettempdir(), "magi_laf_upload_pdf", f"{run_id}_{safe_laf}_{safe_act}")
        )
        result["staging_dir"] = staging_dir

        # 非書狀的附件/證據關鍵字。注意「調查證據聲請狀」本身是書狀，
        # 不能因為檔名含「證據」就被排除。
        _hard_attachment_keywords = [
            "筆錄", "譯文", "節文", "詰問", "卷證索引",
        ]
        _soft_attachment_keywords = [
            "聲證", "證據", "附件", "債權人清冊", "財產及收入", "財產收入",
            "財產狀況", "收入狀況", "戶籍謄本", "診斷書", "薪資",
            "勞保", "國保", "稅務", "所得", "信用報告", "對話",
        ]

        def _looks_like_pleading_filename(fn: str) -> bool:
            name = str(fn or "")
            if not name:
                return False
            if "狀" in name:
                return True
            return any(k in name for k in ("答辯", "抗告", "上訴", "陳報", "準備", "辯護意旨", "補正"))

        def _is_non_pleading_attachment(fn: str) -> bool:
            name = str(fn or "")
            if any(k in name for k in _hard_attachment_keywords):
                return True
            if any(k in name for k in _soft_attachment_keywords) and not _looks_like_pleading_filename(name):
                return True
            return False

        max_walk_dirs = max(1, int(os.environ.get("MAGI_LAF_UPLOAD_SCAN_MAX_DIRS", "240") or "240"))
        max_files_per_dir = max(1, int(os.environ.get("MAGI_LAF_UPLOAD_SCAN_MAX_FILES_PER_DIR", "250") or "250"))
        max_judgment_files = max(1, int(os.environ.get("MAGI_LAF_UPLOAD_MAX_JUDGMENT_FILES", "120") or "120"))
        max_upload_mb = max(1.0, float(os.environ.get("MAGI_LAF_PORTAL_MAX_UPLOAD_MB", "8") or "8"))
        max_upload_bytes = int(max_upload_mb * 1024 * 1024)
        _skip_walk_dir_keywords = (
            "@eaDir", "__MACOSX", ".sync", ".SynologyWorkingDirectory",
            "06_閱卷", "閱卷資料", "05_證據", "證據資料",
        )

        def _iter_upload_dirs(base_dir: str):
            """Yield base + one-level child dirs only for large-case NAS safety."""
            if not os.path.isdir(base_dir):
                return
            yielded = 0
            yield base_dir
            yielded += 1
            try:
                names = sorted(os.listdir(base_dir))
            except OSError:
                return
            for name in names:
                if yielded >= max_walk_dirs:
                    result["large_case_guard"]["dir_limit"] = max_walk_dirs
                    break
                if name.startswith(".") or any(k in name for k in _skip_walk_dir_keywords):
                    continue
                child = os.path.join(base_dir, name)
                try:
                    if os.path.isdir(child):
                        yield child
                        yielded += 1
                except OSError:
                    continue

        def _fits_portal_upload_limit(src: str) -> bool:
            try:
                return os.path.getsize(src) <= max_upload_bytes
            except OSError:
                return True

        def _find_final_word_near(src: str) -> str:
            """Find a nearby final/clean Word file to upload when PDF cannot be used."""
            base_dir = os.path.dirname(str(src or ""))
            if not base_dir or not os.path.isdir(base_dir):
                return ""
            try:
                items = os.listdir(base_dir)
            except OSError:
                return ""
            candidates = []
            for fn in items[:max_files_per_dir]:
                if fn.startswith(".") or fn.startswith("~"):
                    continue
                ext = Path(fn).suffix.lower()
                if ext not in (".docx", ".doc", ".odt"):
                    continue
                if not any(k in fn for k in ("定稿", "清稿", "final", "Final", "FINAL")):
                    continue
                full = os.path.join(base_dir, fn)
                if os.path.isfile(full):
                    priority = 0
                    if "清稿" in fn:
                        priority += 30
                    if "定稿" in fn:
                        priority += 20
                    if "final" in fn.lower():
                        priority += 10
                    try:
                        mtime = os.path.getmtime(full)
                    except OSError:
                        mtime = 0
                    candidates.append((priority, mtime, fn, full))
            if not candidates:
                return ""
            candidates.sort(reverse=True)
            return candidates[0][3]

        def _make_portal_sized_pdf(src: str, label: str = "") -> str:
            """Create a compressed temp PDF for portal upload without touching the original file."""
            if not src or not os.path.isfile(src):
                return ""
            if _fits_portal_upload_limit(src):
                return src
            if str(os.environ.get("MAGI_LAF_COMPRESS_OVERSIZE_PDFS", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
                return ""
            stem = Path(src).stem
            safe_stem = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", stem)[:80] or "document"
            out = os.path.join(staging_dir, f"{safe_stem}.portal.pdf")
            gs = shutil.which("gs") or "/opt/homebrew/bin/gs"
            if os.path.exists(gs):
                compression_timeout = max(15, int(os.environ.get("MAGI_LAF_PDF_COMPRESS_TIMEOUT_SEC", "20") or "20"))
                for setting in ("/ebook", "/screen"):
                    try:
                        cmd = [
                            gs,
                            "-q",
                            "-dNOPAUSE",
                            "-dBATCH",
                            "-dSAFER",
                            "-sDEVICE=pdfwrite",
                            "-dCompatibilityLevel=1.4",
                            f"-dPDFSETTINGS={setting}",
                            "-dDetectDuplicateImages=true",
                            "-dCompressFonts=true",
                            "-dSubsetFonts=true",
                            f"-sOutputFile={out}",
                            src,
                        ]
                        subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=compression_timeout)
                        if os.path.exists(out) and _fits_portal_upload_limit(out):
                            result["large_case_guard"].setdefault("compressed_oversize_files", 0)
                            result["large_case_guard"]["compressed_oversize_files"] += 1
                            return out
                    except Exception as e:
                        logger.debug("PDF compression failed (%s, %s): %s", src, setting, e)
            qpdf_enabled = str(os.environ.get("MAGI_LAF_TRY_QPDF_OVERSIZE", "0")).strip().lower() in {"1", "true", "yes", "on"}
            qpdf = shutil.which("qpdf") or "/opt/homebrew/bin/qpdf"
            if qpdf_enabled and os.path.exists(qpdf):
                try:
                    qout = os.path.join(staging_dir, f"{safe_stem}.linearized.pdf")
                    qpdf_timeout = max(10, int(os.environ.get("MAGI_LAF_QPDF_TIMEOUT_SEC", "20") or "20"))
                    subprocess.run([qpdf, "--linearize", src, qout], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=qpdf_timeout)
                    if os.path.exists(qout) and _fits_portal_upload_limit(qout):
                        result["large_case_guard"].setdefault("compressed_oversize_files", 0)
                        result["large_case_guard"]["compressed_oversize_files"] += 1
                        return qout
                except Exception as e:
                    logger.debug("qpdf linearize failed (%s): %s", src, e)
            return ""

        max_files = int(os.environ.get("MAGI_LAF_MAX_UPLOAD_SOURCE_FILES", "400") or "400")
        include_closing_pleadings = str(
            os.environ.get("MAGI_LAF_CLOSING_INCLUDE_PLEADINGS", "1")
        ).strip().lower() in {"1", "true", "yes", "on"}
        if (action or "").strip().lower() == "closing" and not include_closing_pleadings:
            docs = self._scan_case_folder_docs(root, action="closing")
            raw_basis_pdfs = [
                p for p in self._sort_closing_basis_files(list(docs.get("closing_basis_files") or []))
                if str(p or "").lower().endswith(".pdf")
            ]
            raw_basis_pdfs.extend(
                p for p in sorted(set(docs.get("mediation_success_files") or []))
                if str(p or "").lower().endswith(".pdf") and p not in raw_basis_pdfs
            )
            basis_pdfs = []
            for p in raw_basis_pdfs:
                uploadable = _make_portal_sized_pdf(p, "closing_basis")
                if uploadable:
                    basis_pdfs.append(uploadable)
                    if uploadable != p:
                        result["converted"].append({"source": p, "pdf": uploadable, "fallback": "compressed_oversize_pdf"})
                else:
                    result["failed"].append({
                        "source": p,
                        "error": f"oversize_skipped>{max_upload_mb:g}MB",
                    })
            result["judgment_pdf_files"] = basis_pdfs
            result["pleading_source_files"] = []
            result["procedural_ruling_pdf_files"] = []
            result["pdf_files"] = basis_pdfs[: max(1, max_files)]
            result["ok"] = bool(result["pdf_files"])
            result["large_case_guard"]["closing_basis_only"] = True
            if result["failed"]:
                result["large_case_guard"]["skipped_oversize_files"] = len(result["failed"])
                result["large_case_guard"]["portal_max_upload_mb"] = max_upload_mb
            if not result["ok"]:
                result["error"] = "no_pdf_generated"
            return result

        pleading_files: List[str] = []
        if os.path.isdir(plead_root):
            # 每個子資料夾獨立篩選：
            # 1. 有「存底」或「留底」PDF → 只上傳那份
            # 2. 沒有 → 轉換最新的 WORD 檔（依 v2>v1、清稿>草稿、修改時間判斷）
            for base in _iter_upload_dirs(plead_root):
                try:
                    files = os.listdir(base)
                except OSError:
                    continue
                sorted_files = sorted(files)[:max_files_per_dir]

                # 先篩出非隱藏、非附件/證據的檔案
                candidates = []
                for fn in sorted_files:
                    if fn.startswith(".") or fn.startswith("~"):
                        continue
                    full = os.path.join(base, fn)
                    if not os.path.isfile(full):
                        continue
                    if _is_non_pleading_attachment(fn):
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
                        return (priority, fn)

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
                    best_pdf = max(plain_pdfs, key=lambda x: x[0])
                    pleading_files.append(best_pdf[1])
                    logger.debug("  選取最新書狀 PDF: %s", best_pdf[0])
        result["pleading_source_files"] = pleading_files

        judgment_pdfs: List[str] = []
        for judgment_root in judgment_roots:
            if not os.path.isdir(judgment_root):
                continue
            for base in _iter_upload_dirs(judgment_root):
                try:
                    files = os.listdir(base)
                except OSError:
                    continue
                for fn in sorted(files)[:max_files_per_dir]:
                    if fn.startswith("."):
                        continue
                    full = os.path.join(base, fn)
                    if os.path.isfile(full) and fn.lower().endswith(".pdf"):
                        judgment_pdfs.append(full)
                        if len(judgment_pdfs) >= max_judgment_files:
                            result["large_case_guard"]["judgment_file_limit"] = max_judgment_files
                            break
                if len(judgment_pdfs) >= max_judgment_files:
                    break
            if len(judgment_pdfs) >= max_judgment_files:
                break
        result["judgment_pdf_files"] = judgment_pdfs

        # Closing basis uploads come from the final-doc folder only.  Court
        # notices/procedural rulings in 09 stay as case history and should be
        # moved/copied to the judgment folder if they are truly final.
        procedural_ruling_pdfs: List[str] = []
        result["procedural_ruling_pdf_files"] = procedural_ruling_pdfs

        mediation_success_pdfs: List[str] = []
        if (action or "").strip().lower() == "closing" and os.path.isdir(transcript_root):
            for base in _iter_upload_dirs(transcript_root):
                try:
                    files = os.listdir(base)
                except OSError:
                    continue
                for fn in sorted(files)[:max_files_per_dir]:
                    if fn.startswith(".") or fn.startswith("~"):
                        continue
                    if not fn.lower().endswith(".pdf"):
                        continue
                    if "不成立" in fn:
                        continue
                    if not any(k in fn for k in ("調解筆錄", "調解成立", "和解筆錄", "和解成立", "調解書")):
                        continue
                    full = os.path.join(base, fn)
                    if os.path.isfile(full):
                        mediation_success_pdfs.append(full)
        result["mediation_success_pdf_files"] = mediation_success_pdfs

        out_pdf: List[str] = []
        converted: List[dict] = []
        failed: List[dict] = []
        dedup = set()

        for src in pleading_files[: max(1, max_files)]:
            src_ext = Path(src).suffix.lower()
            if src_ext == ".pdf" and not _fits_portal_upload_limit(src):
                fallback_word = _find_final_word_near(src)
                if fallback_word:
                    if fallback_word not in dedup:
                        out_pdf.append(fallback_word)
                        dedup.add(fallback_word)
                        converted.append({
                            "source": src,
                            "pdf": fallback_word,
                            "fallback": "oversize_pdf_to_final_word",
                        })
                    continue
                failed.append({"source": src, "error": f"oversize_skipped>{max_upload_mb:g}MB"})
                continue
            pdf = self._to_pdf_for_portal(src, staging_dir)
            if pdf and (pdf not in dedup):
                out_pdf.append(pdf)
                dedup.add(pdf)
                converted.append({"source": src, "pdf": pdf})
            elif not pdf:
                if src_ext in (".docx", ".doc", ".odt") and (
                    "定稿" in os.path.basename(src)
                    or "清稿" in os.path.basename(src)
                    or "final" in os.path.basename(src).lower()
                ):
                    out_pdf.append(src)
                    dedup.add(src)
                    converted.append({"source": src, "pdf": src, "fallback": "word_upload"})
                else:
                    fallback_word = _find_final_word_near(src)
                    if fallback_word and fallback_word not in dedup:
                        out_pdf.append(fallback_word)
                        dedup.add(fallback_word)
                        converted.append({
                            "source": src,
                            "pdf": fallback_word,
                            "fallback": "convert_failed_to_final_word",
                        })
                    else:
                        failed.append({"source": src, "error": "convert_failed"})

        non_pleading_name_dedup: set[str] = set()
        for src_pdf in (judgment_pdfs + procedural_ruling_pdfs + mediation_success_pdfs)[: max(1, max_files)]:
            try:
                name_key = os.path.basename(str(src_pdf or "")).casefold()
                if name_key and name_key in non_pleading_name_dedup:
                    continue
                if name_key:
                    non_pleading_name_dedup.add(name_key)
                if not _fits_portal_upload_limit(src_pdf):
                    compressed_pdf = _make_portal_sized_pdf(src_pdf, "judgment")
                    if compressed_pdf:
                        if compressed_pdf not in dedup:
                            out_pdf.append(compressed_pdf)
                            dedup.add(compressed_pdf)
                            converted.append({"source": src_pdf, "pdf": compressed_pdf, "fallback": "compressed_oversize_pdf"})
                        continue
                    failed.append({"source": src_pdf, "error": f"oversize_skipped>{max_upload_mb:g}MB"})
                    continue
                if str(os.environ.get("MAGI_LAF_COPY_PDF_UPLOADS", "0")).strip().lower() in {"1", "true", "yes", "on"}:
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
        staging_dir = _create_laf_upload_staging_dir(
            os.path.join(tempfile.gettempdir(), "magi_laf_upload_pdf", f"{run_id}_{safe_laf}_{safe_act}_{safe_label}")
        )

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
        suppress_notify: bool = False,
    ) -> dict:
        """
        Execute one portal workflow in draft-only mode.
        action: go_live | inquiry | fee | condition | withdrawal | closing
        suppress_notify: True = 不發 Discord/Telegram 通知（CLI 測試用）
        """
        act = (action or "").strip().lower()
        fields = dict(fields or {})
        identity = self._lookup_case_identity(
            laf_case_number=laf_case_number,
            case_number=case_number,
            client_name=client_name,
            reason_hint=reason,
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
        docs = self._scan_case_folder_docs(case_folder, action=act) if case_folder else self._empty_docs_map()

        preview_only_actions = {
            "complaint",
            "termination",
            "fee_review",
            "preservation_guarantee",
            "enforcement_guarantee",
            "subject_amount_opinion",
            "final_litigation_fee",
            "police_attendance",
        }
        if act not in {
            "go_live",
            "inquiry",
            "fee",
            "condition",
            "withdrawal",
            "closing",
            "progress",
            *preview_only_actions,
        }:
            return {"ok": False, "error": f"unknown_action:{act}"}

        # Workflows except closing can run with either LAF case no or client name.
        if act != "closing" and (not laf_no and not cname):
            return {"ok": False, "error": "missing_target", "action": act, "identity": identity}

        if act in {"go_live", "condition", "fee", "closing"}:
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
            _gl_docs, _gl_dir = self._scan_go_live_docs(case_folder)
            _is_consumer_debt = self._is_consumer_debt_case_folder(case_folder)
            # 消債案件只需開辦通知書/回報單；一般案件需要開辦通知/回報單 + 遞狀證明
            _need_poa = not _is_consumer_debt
            if not is_go_live_ready(_gl_docs, is_consumer_debt=_is_consumer_debt):
                missing = go_live_missing_labels(_gl_docs, is_consumer_debt=_is_consumer_debt)
                hint = "請將已填/已簽開辦通知書或回報單放入 02_開辦資料；01_法扶資料的官網空白表件不算" if _is_consumer_debt else "請將已填/已簽開辦通知或回報單放入 02_開辦資料，並備妥委任狀、我方歷次書狀存底或回執；01_法扶資料的官網空白表件不算"
                self._dump_missing_docs_diagnostics(
                    mode="portal_draft",
                    case_folder=case_folder,
                    gl_dir=_gl_dir,
                    gl_docs=_gl_docs,
                    missing=missing,
                    is_consumer_debt=_is_consumer_debt,
                )
                return {
                    "ok": False,
                    "error": "missing_required_docs",
                    "action": act,
                    "identity": identity,
                    "missing": missing,
                    "hint": hint,
                    "docs": _gl_docs,
                }
            open_doc = go_live_notice_files(_gl_docs)[0]
            poa_doc = go_live_proof_files(_gl_docs)[0] if go_live_proof_files(_gl_docs) else ""
            open_date = self._extract_best_date_from_doc(open_doc)
            poa_date = self._extract_best_date_from_doc(poa_doc) if poa_doc else ""
            if not open_date or (_need_poa and not poa_date):
                missing_dates = []
                if not open_date:
                    missing_dates.append("開辦通知書日期")
                if _need_poa and not poa_date:
                    missing_dates.append("委任狀或書狀存底/回執日期")
                return {
                    "ok": False,
                    "error": "missing_required_dates",
                    "action": act,
                    "identity": identity,
                    "missing": missing_dates,
                    "docs": {"opening_notice": open_doc, "poa": poa_doc},
                }
            fields.setdefault("sel_result", "1")
            # 生成自然語言 remark — 與 email 自動流程使用同一套邏輯
            submission_info = self._detect_poa_submission_info(case_folder)
            if submission_info.get("date_roc") or _is_consumer_debt:
                default_remark = self._compose_go_live_remark(
                    submission_info, cname, _is_consumer_debt,
                    open_doc_date=open_date or "",
                )
            elif _is_consumer_debt:
                # 消債：簽署開辦通知書 = 首次實質討論案情。統一用語。
                _open_roc_cd = self._iso_to_roc(open_date) if open_date else ""
                default_remark = (
                    f"已於民國{_open_roc_cd}首次實質討論案情。"
                    if _open_roc_cd else f"已首次實質討論案情（開辦日期 {open_date}）。"
                )
            else:
                # fallback: 沒拿到 _detect_poa_submission_info 結果，自己組
                poa_roc = self._iso_to_roc(poa_date) if poa_date else ""
                open_roc = self._iso_to_roc(open_date) if open_date else ""
                if poa_roc and open_roc:
                    default_remark = f"已於民國{open_roc}首次實質討論案情。已於民國{poa_roc}遞送委任狀至法院。"
                elif poa_roc:
                    default_remark = f"已於民國{poa_roc}遞送委任狀至法院。"
                elif open_roc:
                    default_remark = f"已於民國{open_roc}首次實質討論案情。"
                else:
                    default_remark = f"已首次實質討論案情（開辦日期 {open_date}）。"
            fields.setdefault("remark", default_remark)
            # 找出要上傳的檔案（開辦通知書、消債不需委任狀）
            go_live_upload = self._find_go_live_upload_files(case_folder, is_consumer_debt=_is_consumer_debt)
            if go_live_upload:
                fields.setdefault("upload_files", go_live_upload)
            ok = self.execute_portal_go_live_draft(laf_no, cname, fields or {}, suppress_notify=suppress_notify)
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
                result["error"] = "portal_prefill_failed"
                result["detail"] = str(getattr(self, "_last_portal_error", "") or "")
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
            ok = self.execute_portal_withdrawal_draft(laf_no, cname, fields or {}, suppress_notify=suppress_notify)
            result = {
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
            if not ok:
                result["error"] = "portal_draft_failed"
                result["detail"] = str(getattr(self, "_last_portal_error", "") or "")
            return result

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
            ok = self.execute_portal_inquiry_draft(laf_no, cname, fields or {}, suppress_notify=suppress_notify)
            result = {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "upload_bundle": upload_bundle,
                "preview": self._last_portal_artifact,
            }
            if not ok:
                result["error"] = "portal_draft_failed"
                result["detail"] = str(getattr(self, "_last_portal_error", "") or "")
            return result

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
            ok = self.execute_portal_fee_draft(laf_no, cname, fields or {}, suppress_notify=suppress_notify)
            result = {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "docs": {"pink_receipt": docs["pink_receipt_files"][0]},
                "upload_bundle": upload_bundle,
                "preview": self._last_portal_artifact,
            }
            if not ok:
                result["error"] = "portal_draft_failed"
                result["detail"] = str(getattr(self, "_last_portal_error", "") or "")
            return result

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
            ok = self.execute_portal_condition_draft(laf_no, cname, fields or {}, suppress_notify=suppress_notify)
            result = {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "docs": {"mediation_failure": med_doc},
                "upload_bundle": upload_bundle,
                "preview": self._last_portal_artifact,
            }
            try:
                upload_status = (
                    (self._last_portal_artifact or {})
                    .get("upload_result", {})
                    .get("status", "")
                )
                if upload_status:
                    result["portal_status"] = upload_status
                if upload_status == "already_in_progress":
                    result["noop"] = True
                    result["message"] = "portal_already_has_draft_in_progress"
            except Exception:
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 5228, exc_info=True)
            if not ok:
                result["error"] = "portal_draft_failed"
                result["detail"] = str(getattr(self, "_last_portal_error", "") or "")
            return result

        if act == "progress":
            # 進度回報：不需必要文件，remark 由呼叫端提供或留空給 portal 填
            fields.setdefault("remark", reason or "")
            # 傳入自動填寫 noarrivereason 的模板（automation.fill_workflow_fields 使用）
            fields.setdefault(
                "auto_zero_reason_template",
                (
                    "本回報週期內，以下項目尚未發生：{ZERO_FIELDS}。"
                    "若有遺漏請律師上 portal 暫存頁面補充。"
                    f"（MAGI 自動填寫，{datetime.now().strftime('%Y-%m-%d')}）"
                ),
            )
            ok = self.execute_portal_workflow_draft("progress", laf_no, cname, fields or {}, suppress_notify=suppress_notify)
            # 取 automation 偵測到的零次數欄位（由 fill_workflow_fields 設定 last_zero_fields）
            detected_zero_fields = []
            try:
                automation = self._get_automation()
                detected_zero_fields = list(getattr(automation, 'last_zero_fields', []) or [])
            except Exception:
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 5253, exc_info=True)
            result = {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "zero_fields_detected": detected_zero_fields,
                "preview": self._last_portal_artifact,
            }
            if not ok:
                result["error"] = "portal_draft_failed"

            # ── Plan C：兩階段確認碼 ──────────────────────────────────────
            # 填表 + 截圖完成後，產生 6-hex token，寫入 progress pending file，
            # 通知律師預覽截圖並附上確認碼；律師回覆後才真正送出。
            token = ""
            if ok and not suppress_notify:
                try:
                    from api.domains.laf_flow import register_laf_progress_submit_pending
                    _platform = str(fields.get("_platform") or "discord").strip() or "discord"
                    _requester = str(fields.get("_requester_user_id") or "").strip()
                    _preview_data = self._last_portal_artifact or {}
                    _preview_url = ""
                    if isinstance(_preview_data, dict):
                        _png_exp = _preview_data.get("png_export") or {}
                        if isinstance(_png_exp, dict):
                            _preview_url = str(_png_exp.get("url") or "").strip()
                    token = register_laf_progress_submit_pending(
                        self,
                        platform=_platform,
                        requester_user_id=_requester,
                        payload={
                            "laf_case_no": laf_no,
                            "client_name": cname,
                            "remark": str(fields.get("remark") or reason or "").strip(),
                            "fields": {k: v for k, v in fields.items() if not k.startswith("_")},
                        },
                        result_data={
                            "zero_fields_detected": detected_zero_fields,
                            "preview_url": _preview_url,
                        },
                    )
                    result["pending_token"] = token
                    logger.info("progress pending token registered: %s (platform=%s)", token, _platform)
                except Exception as _te:
                    logger.exception("register_laf_progress_submit_pending failed: %s", _te)

                try:
                    msg = f"📋 進度回報草稿已填寫（{cname} / {laf_no}）"
                    if detected_zero_fields:
                        msg += (
                            "\n\n⚠️ 偵測到下列項目次數為零"
                            "（已自動填預設說明，請上 portal 確認補充）：\n"
                            + "\n".join(f"   - {f}" for f in detected_zero_fields)
                        )
                    _preview_url_msg = ""
                    if isinstance(self._last_portal_artifact, dict):
                        _pe = self._last_portal_artifact.get("png_export") or {}
                        if isinstance(_pe, dict):
                            _preview_url_msg = str(_pe.get("url") or "").strip()
                    if _preview_url_msg:
                        msg += f"\n\n📷 預覽截圖：{_preview_url_msg}"
                    if token:
                        msg += f"\n\n✅ 確認無誤後請回覆此確認碼以送出：`{token}`\n（30 分鐘內有效）"
                    # 修正 Plan B bug：LAFNotifier 沒 notify() 方法，正確是 notify_admin(text, topic_key=...)
                    # 既有 closing/go_live 都用 notify_admin（line 1401/1721/1724/1726 等共 5 處）
                    _notify_result = self.notifier.notify_admin(msg, topic_key="laf_progress")
                    logger.info("progress notify_admin result: %s (zero_fields=%d, token=%s)",
                                _notify_result, len(detected_zero_fields), token or "(none)")
                except Exception as _ne:
                    logger.exception("progress notify_admin failed: %s", _ne)
            return result

        if act in preview_only_actions:
            description = str(
                reason
                or fields.get("desc")
                or fields.get("reason_text")
                or fields.get("comments")
                or ""
            ).strip()
            if not description:
                return {
                    "ok": False,
                    "error": "missing_reason",
                    "action": act,
                    "identity": identity,
                    "safety_mode": "preview_only",
                }
            fields.setdefault("desc", description)
            fields.setdefault("reason_text", description)

            # Only attach documents selected explicitly by the caller.  These
            # less-common workflows have materially different evidence rules;
            # scanning and uploading every PDF in a case folder could disclose
            # an unrelated document.
            selected_uploads = [
                str(path).strip()
                for path in (fields.get("upload_files") or [])
                if str(path).strip()
            ]
            if selected_uploads:
                upload_bundle = self._collect_selected_upload_pdfs(
                    selected_uploads,
                    laf_case_no=laf_no,
                    action=act,
                    label="explicit_evidence",
                )
                if not upload_bundle.get("ok"):
                    return {
                        "ok": False,
                        "error": "selected_upload_prepare_failed",
                        "action": act,
                        "identity": identity,
                        "upload_bundle": upload_bundle,
                        "safety_mode": "preview_only",
                    }
                fields["upload_files"] = upload_bundle.get("pdf_files") or []

            if act == "termination":
                basis_files = list(docs.get("closing_basis_files") or [])
                if not basis_files and not selected_uploads:
                    return {
                        "ok": False,
                        "error": "missing_required_docs",
                        "action": act,
                        "identity": identity,
                        "missing": ["終止／撤銷已確定的依據文件及結案資料"],
                        "safety_mode": "preview_only",
                    }
            elif act == "final_litigation_fee" and not selected_uploads:
                return {
                    "ok": False,
                    "error": "missing_required_docs",
                    "action": act,
                    "identity": identity,
                    "missing": [
                        "簽名申請書",
                        "事件確定文件",
                        "確定訴訟費用額裁定及確定證明書",
                    ],
                    "safety_mode": "preview_only",
                }

            ok = self.execute_portal_workflow_draft(
                act,
                laf_no,
                cname,
                fields or {},
                suppress_notify=suppress_notify,
            )
            return {
                "ok": bool(ok),
                "action": act,
                "identity": identity,
                "fields": fields,
                "preview": self._last_portal_artifact,
                "safety_mode": "preview_only",
                "saved": False,
                "submitted": False,
                **(
                    {}
                    if ok
                    else {
                        "error": "portal_preview_failed",
                        "detail": str(getattr(self, "_last_portal_error", "") or ""),
                    }
                ),
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

        basis_files = override_basis_files or self._sort_closing_basis_files(list(docs.get("closing_basis_files") or []))
        if not basis_files and docs.get("mediation_success_files"):
            basis_files = list(docs.get("mediation_success_files") or [])
        misfiled_basis_files = self._sort_closing_basis_files(list(docs.get("misfiled_closing_basis_files") or []))
        unrecognized_basis_files = sorted(
            set(str(p) for p in (docs.get("unrecognized_closing_folder_files") or []) if str(p).strip())
        )
        # 結案只檢查結案依據文件；強制執行案件可用執行命令作為結案依據。
        # 收文章/回執是「開辦」才需要的內部驗證。
        missing = []
        if not basis_files:
            missing.append("結案依據文件（起訴書/判決/裁定/不起訴處分書/確定證明書；強制執行案件可用執行命令）")
        if missing:
            hint = ""
            if misfiled_basis_files:
                hint = (
                    "發現疑似可報結的終局文件放在 08/09_法院通知或程序裁定；"
                    "法扶報結只使用 10_判決書或終局裁定及處分，請先將該檔整理/複製到 10 資料夾後再重試。"
                )
            return {
                "ok": False,
                "error": "missing_required_docs",
                "action": act,
                "identity": identity,
                "missing": missing,
                "hint": hint,
                "misfiled_closing_basis_files": misfiled_basis_files,
                "unrecognized_closing_folder_files": unrecognized_basis_files,
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
        # ── 零值欄位自動補查（不再因 review_count=0 就擋住流程） ──
        # 閱卷次數：從閱卷資料夾日期子目錄數量補
        # 排除只有繳費單的子目錄（繳費單不是閱卷）
        if int(counts.get("review_count", 0) or 0) <= 0 and case_folder:
            _review_count_from_folder = 0
            _review_dir_used = ""
            for _review_dir_name in ("06_閱卷資料", "04_閱卷資料", "03_閱卷資料"):
                _review_dir = os.path.join(case_folder, _review_dir_name)
                if os.path.isdir(_review_dir):
                    try:
                        for _sub in os.listdir(_review_dir):
                            _sub_path = os.path.join(_review_dir, _sub)
                            if not os.path.isdir(_sub_path) or _sub.startswith("."):
                                continue
                            # 檢查子目錄裡是否有非繳費單的檔案（卷宗 PDF、OCR 等）
                            _has_real_files = False
                            try:
                                for _fn in os.listdir(_sub_path):
                                    _fn_lower = _fn.lower()
                                    if _fn_lower.startswith("."):
                                        continue
                                    # 繳費單判定：檔名含「繳費」
                                    if "繳費" in _fn:
                                        continue
                                    # 有任何非繳費單的檔案 → 算真正閱卷
                                    _has_real_files = True
                                    break
                            except OSError:
                                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 5565, exc_info=True)
                            if _has_real_files:
                                _review_count_from_folder += 1
                    except OSError:
                        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 5569, exc_info=True)
                    if _review_count_from_folder > 0:
                        _review_dir_used = _review_dir_name
                        break
            if _review_count_from_folder > 0:
                counts["review_count"] = _review_count_from_folder
                logger.info("  📂 閱卷次數從資料夾補齊: %d (from %s, 排除純繳費單目錄)", _review_count_from_folder, _review_dir_used)

        # 規則：若「開會+律見」皆為 0、但有開庭，則以開庭次數補會議次數。
        # 理由：律師出庭當天必與當事人在法院碰面，視同會議/面談一次。
        _mc = int(counts.get("meeting_count", 0) or 0)
        _iq = int(counts.get("inq_count", 0) or 0)
        _cc = int(counts.get("court_count", 0) or 0)
        if _mc == 0 and _iq == 0 and _cc > 0:
            counts["meeting_count"] = _cc
            # 同步把開庭日期補進 meeting_dates（若該欄未被其他邏輯使用，至少保留來源）
            _cd = counts.get("court_dates") or []
            if _cd and not counts.get("meeting_dates"):
                counts["meeting_dates"] = list(_cd)
            logger.info("  🔁 開會/律見皆 0，以開庭次數 %d 作為會議次數（出庭當天視同碰面）", _cc)
            # 重算 _disc_total 讓下方 low_fields / zero_reasons 判斷正確
            _disc_total = (
                int(counts.get("meeting_count", 0) or 0)
                + int(counts.get("contact_count", 0) or 0)
                + int(counts.get("inq_count", 0) or 0)
            )

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

        # 零值不再擋住流程：自動填寫理由，並在特別說明提醒律師
        if low_fields and (not reason):
            _label_map = {"disc_times": "研討案情", "review_count": "閱卷", "court_count": "開庭", "document_count": "書狀"}
            _zero_labels = [_label_map.get(k, k) for k in low_fields]
            reason = "以下項目次數為零，請律師確認：" + "、".join(_zero_labels) + "。"
            logger.info("  ⚠️ 零值欄位自動填寫理由: %s", reason)
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
            suppress_notify=suppress_notify,
        )
        if ok and not self.dry_run:
            # 暫存成功 → 回寫 DB status（dry-run 不寫）
            try:
                _upd_case = identity.get("case_number") or ""
                if _upd_case and self.db:
                    _skip_status_write = False
                    if fields.get("_auto_closing_draft"):
                        _cur = self.db.fetch_one(
                            "SELECT legal_aid_status FROM cases WHERE case_number = %s LIMIT 1",
                            (_upd_case,),
                            as_dict=True,
                        ) or {}
                        _cur_status = str(_cur.get("legal_aid_status") or "").strip()
                        if _cur_status not in {"待報結", "已結案，待報結"}:
                            logger.warning(
                                "  🔒 Auto closing draft skipped DB status write: %s current=%s",
                                _upd_case,
                                _cur_status,
                            )
                            _skip_status_write = True
                    if not _skip_status_write:
                        try:
                            self.db.execute_write(
                                """
                                UPDATE cases
                                SET legal_aid_status = %s,
                                    status = CASE WHEN COALESCE(manual_status_lock, 0) = 1 THEN status ELSE %s END
                                WHERE case_number = %s
                                """,
                                ("已結案，待送出", "結案中", _upd_case)
                            )
                        except Exception as inner:
                            if "manual_status_lock" not in str(inner) and "Unknown column" not in str(inner):
                                raise
                            self.db.execute_write(
                                "UPDATE cases SET legal_aid_status = %s, status = %s WHERE case_number = %s",
                                ("已結案，待送出", "結案中", _upd_case)
                            )
                        logger.info("  📝 DB status 更新: %s → 已結案，待送出", _upd_case)
            except Exception as _db_err:
                logger.warning("  ⚠️ DB status 更新失敗: %s", _db_err)
        result = {
            "ok": bool(ok),
            "action": act,
            "identity": identity,
            "fields": fields,
            "counts": counts,
            "zero_reasons": zero_reasons,
            "basis_files": list(basis_files or []),
            "misfiled_closing_basis_files": misfiled_basis_files,
            "upload_bundle": upload_bundle,
            "preview": self._last_portal_artifact,
        }
        if misfiled_basis_files:
            result["warnings"] = [
                "發現疑似可報結終局文件仍在 08/09_法院通知或程序裁定；報結已只使用 10_判決書或終局裁定及處分，請整理歸檔。"
            ]
        if not ok:
            result["error"] = "portal_draft_failed"
            result["detail"] = str(getattr(self, "_last_portal_error", "") or "closing_portal_save_failed")
            result["portal_error"] = result["detail"]
        return result

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
        if act not in {"go_live", "progress"}:
            return {"ok": False, "error": "submit_not_supported_for_action", "action": act}

        # ── progress submit: re-open form and send for real ─────────────────
        if act == "progress":
            allow = str(os.environ.get("MAGI_LAF_ALLOW_PROGRESS_SUBMIT", "0")).strip().lower() in {
                "1", "true", "yes", "on"
            }
            if not allow:
                return {"ok": False, "error": "MAGI_LAF_ALLOW_PROGRESS_SUBMIT not set", "action": act}
            _pid = self._lookup_case_identity(
                laf_case_number=laf_case_number,
                case_number=case_number,
                client_name=client_name,
                action=act,
            )
            if _pid.get("needs_manual_confirm"):
                return {"ok": False, "error": "identity_needs_manual_confirmation",
                        "action": act, "identity": _pid}
            _laf_no = (_pid.get("laf_case_number") or "").strip()
            _cname = (_pid.get("client_name") or "").strip()
            if not _laf_no and not _cname:
                return {"ok": False, "error": "missing_target", "action": act, "identity": _pid}
            _flds = dict(fields or {})
            if reason and "remark" not in _flds:
                _flds["remark"] = reason
            ok = self.execute_portal_workflow_submit("progress", _laf_no or _cname, _cname, _flds)
            return {
                "ok": ok,
                "action": act,
                "laf_case_number": _laf_no,
                "client_name": _cname,
                "artifact": self._last_portal_artifact if isinstance(self._last_portal_artifact, dict) else {},
            }

        identity = self._lookup_case_identity(
            laf_case_number=laf_case_number,
            case_number=case_number,
            client_name=client_name,
            reason_hint=reason,
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
        docs = self._scan_case_folder_docs(case_folder, action=act) if case_folder else self._empty_docs_map()
        if not laf_no and not cname:
            return {"ok": False, "error": "missing_target", "action": act, "identity": identity}
        if not case_folder:
            return {"ok": False, "error": "missing_case_folder", "action": act, "identity": identity}
        _gl_docs, _gl_dir = self._scan_go_live_docs(case_folder)
        _is_consumer_debt = self._is_consumer_debt_case_folder(case_folder)
        _need_poa = not _is_consumer_debt
        if not is_go_live_ready(_gl_docs, is_consumer_debt=_is_consumer_debt):
            missing = go_live_missing_labels(_gl_docs, is_consumer_debt=_is_consumer_debt)
            hint = "請將已填/已簽開辦通知書或回報單放入 02_開辦資料；01_法扶資料的官網空白表件不算" if _is_consumer_debt else "請將已填/已簽開辦通知或回報單放入 02_開辦資料，並備妥委任狀、我方歷次書狀存底或回執；01_法扶資料的官網空白表件不算"
            self._dump_missing_docs_diagnostics(
                mode="portal_submit",
                case_folder=case_folder,
                gl_dir=_gl_dir,
                gl_docs=_gl_docs,
                missing=missing,
                is_consumer_debt=_is_consumer_debt,
            )
            return {"ok": False, "error": "missing_required_docs", "action": act, "identity": identity, "missing": missing, "hint": hint}

        open_doc = go_live_notice_files(_gl_docs)[0]
        poa_doc = go_live_proof_files(_gl_docs)[0] if go_live_proof_files(_gl_docs) else ""
        open_date = self._extract_best_date_from_doc(open_doc)
        poa_date = self._extract_best_date_from_doc(poa_doc) if poa_doc else ""
        if not open_date or (_need_poa and not poa_date):
            missing_dates = []
            if not open_date:
                missing_dates.append("開辦通知書日期")
            if _need_poa and not poa_date:
                missing_dates.append("委任狀或書狀存底/回執日期")
            return {"ok": False, "error": "missing_required_dates", "action": act, "identity": identity, "missing": missing_dates}

        fields = dict(fields or {})
        fields.setdefault("sel_result", "1")
        # 統一用語：消債/訴訟代理一律寫「首次實質討論案情」（= 開辦通知書日）
        _open_roc_sub = self._iso_to_roc(open_date) if open_date else ""
        if _is_consumer_debt:
            fields.setdefault(
                "remark",
                f"已於民國{_open_roc_sub}首次實質討論案情。" if _open_roc_sub
                else f"已首次實質討論案情（開辦日期 {open_date}）。",
            )
        else:
            submission_info = self._detect_poa_submission_info(case_folder)
            if not submission_info.get("date_roc") and poa_date:
                submission_info = {
                    "date_roc": self._iso_to_roc(poa_date),
                    "date_iso": poa_date,
                    "source": "filename",
                    "source_file": poa_doc,
                    "source_doc_type": "書狀" if "04_我方歷次書狀" in str(poa_doc) else "委任狀",
                    "confidence": "medium",
                }
            _r = self._compose_go_live_remark(
                submission_info,
                cname,
                is_consumer_debt=False,
                open_doc_date=open_date or "",
            )
            if not _r:
                _r = f"已於民國{_open_roc_sub}首次實質討論案情。" if _open_roc_sub else "已首次實質討論案情。"
            fields.setdefault("remark", _r)
        # 找出要上傳的檔案
        go_live_upload = self._find_go_live_upload_files(case_folder, is_consumer_debt=_is_consumer_debt)
        if go_live_upload:
            fields.setdefault("upload_files", go_live_upload)
        # 由 portal-submit subprocess 觸發 → API 端會發送詳細確認訊息（含截圖 URL +
        # token），子程序內就不再重複發 "已送出開辦回報"，避免雙重通知。
        ok = self.execute_portal_go_live_submit(laf_no, cname, fields, suppress_notify=True)
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
        try:
            from api.case_display import normalize_person_name
            s = normalize_person_name(s)
        except Exception:
            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 5842, exc_info=True)
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
        if (
            os.environ.get("MAGI_V3_REALISM_SANDBOX", "").strip().lower()
            in {"1", "true", "yes", "on"}
        ):
            fixture_value = os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT", "").strip()
            if not fixture_value:
                return ""
            fixture = Path(fixture_value).expanduser()
            candidate = Path(p).expanduser()
            if (
                not fixture.is_absolute()
                or not candidate.is_absolute()
                or fixture.is_symlink()
                or candidate.is_symlink()
            ):
                return ""
            fixture = fixture.resolve(strict=False)
            candidate = candidate.resolve(strict=False)
            if candidate.is_relative_to(fixture) and candidate.is_dir():
                return str(candidate)
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
            docs = self._scan_case_folder_docs(root, action="condition")
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
        """檢查此案是否已不應再重複 condition draft。

        永久條件（不論 days 參數）：
        - portal 已收到分會轉入通知（新紀錄為 transfer_confirmation；舊紀錄仍相容
          review_result_download 含「業經分會轉入系統」或審核/審查結果通知）
          → portal 端已進入下一階段，再 draft 必失敗
        - condition 已成功 draft 過（status='draft' 或 'success'）— 律師若想重做應手動 reset

        days 參數保留作 backward compat 但實質上 condition 任何成功 draft 都永久 dedup。
        """
        if not self.db or not case_number:
            return False
        try:
            # 1. 永久 dedup：condition 已成功 draft / manual_done（不限時間）
            q1 = (
                "SELECT COUNT(*) AS cnt FROM `laf_lifecycle_log` "
                "WHERE `case_number` = %s "
                "AND ("
                "(`event_type` = 'condition' AND `status` IN ('draft','success')) "
                "OR (`event_type` = 'condition_manual_done' AND `status` IN ('manual_done','success'))"
                ")"
            )
            row = self.db.fetch_one(q1, (case_number,), as_dict=True)
            cnt = 0
            if isinstance(row, dict):
                cnt = int(row.get("cnt") or 0)
            elif isinstance(row, (tuple, list)) and row:
                cnt = int(row[0] or 0)
            if cnt > 0:
                return True

            # 2. portal 已轉入系統。新分類使用 transfer_confirmation；舊資料仍查
            # review_result_download，避免發布後失去既有永久 dedup 證據。
            q2 = (
                "SELECT `event_data` FROM `laf_lifecycle_log` "
                "WHERE `case_number` = %s "
                "AND `event_type` IN ('transfer_confirmation','review_result_download') "
                "AND `status` = 'success' "
                "ORDER BY `id` DESC LIMIT 5"
            )
            rows = self.db.fetch_all(q2, (case_number,), as_dict=True) or []
            for r in rows:
                ed = str((r or {}).get("event_data") or "")
                if any(k in ed for k in ("業經分會轉入系統", "審核結果通知", "審查結果通知", "回報(附條件)", "回報（附條件）")):
                    logger.info("  ⏭️ condition skip：%s portal 已轉入系統 (transfer confirmation)", case_number)
                    return True
        except Exception:
            return False
        return False

    def _get_pending_condition_cases(self, max_cases: int = 0) -> List[dict]:
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
            if max_cases > 0 and len(out) >= int(max_cases):
                break
        return out

    def run_condition_drafts(self, max_cases: int = 0, *, suppress_notify: bool = True) -> dict:
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
                suppress_notify=suppress_notify,
            )
            err_detail = ""
            if not ok:
                err_detail = str(getattr(self, "_last_portal_error", "") or "portal_draft_failed")
                logger.warning("condition draft failed for %s: %s", laf_case_no, err_detail)
            portal_status = ""
            try:
                portal_status = (
                    (self._last_portal_artifact or {})
                    .get("upload_result", {})
                    .get("status", "")
                )
            except Exception:
                portal_status = ""
            results.append(
                {
                    "ok": bool(ok),
                    "laf_case_number": laf_case_no,
                    "osc_case_number": c.get("osc_case_number", ""),
                    "client_name": client_name,
                    "upload_files": len((upload_bundle.get("pdf_files") or [])),
                    "error": "" if ok else err_detail,
                    "portal_status": portal_status,
                    "noop": bool(portal_status == "already_in_progress"),
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
        """Check if a closing draft was already saved for this case.

        Closing drafts are admin-in-the-loop: once MAGI has created one,
        repeated batch runs must not keep re-drafting/re-reporting the same
        case. The ``days`` argument is retained for callers, but successful
        draft/pending records are treated as permanent dedup signals.
        """
        if not self.db or not case_number:
            return False
        try:
            q = (
                "SELECT COUNT(*) AS cnt FROM `laf_lifecycle_log` "
                "WHERE `case_number` = %s "
                "AND `event_type` = 'closing' "
                "AND `status` IN ('draft','success','pending')"
            )
            row = self.db.fetch_one(q, (case_number,), as_dict=True)
            if isinstance(row, dict):
                return int(row.get("cnt") or 0) > 0
            if isinstance(row, (tuple, list)) and row:
                return int(row[0] or 0) > 0
        except Exception:
            return False
        return False

    def _get_pending_closing_draft_cases(self, max_cases: int = 0) -> List[dict]:
        """
        Find LAF cases ready for auto closing draft:
        - legal_aid_status is already in a closing-report state
        - unified closing scanner finds terminal closing-basis files
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
                  AND TRIM(COALESCE(`legal_aid_status`, '')) IN ('待報結', '已結案，待報結')
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
            # Dedup
            if self._was_closing_drafted_recently(laf_no, days=30):
                continue
            if osc_no and self._was_closing_drafted_recently(osc_no, days=30):
                continue

            case_reason = (r.get("case_reason") or "").strip()
            docs = self._scan_case_folder_docs(folder, action="closing")
            basis = self._sort_closing_basis_files([
                p for p in list(docs.get("closing_basis_files") or [])
                if self._is_auto_closing_basis_candidate(p, case_reason=case_reason, folder_path=folder)
            ])
            if not basis:
                continue
            out.append({
                "osc_case_number": osc_no,
                "laf_case_number": laf_no,
                "client_name": client,
                "folder_path": folder,
                "case_reason": case_reason,
                "closing_basis_files": basis,
            })
            if max_cases > 0 and len(out) >= int(max_cases):
                break
        return out

    def run_closing_drafts(self, max_cases: int = 0) -> dict:
        """
        自動找「已進入待報結狀態」且判決書或終局裁定及處分資料夾已有嚴格終局文件的法扶案件，
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
                fields={"closing_basis_files": basis, "_auto_closing_draft": True},
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

                docs = self._scan_case_folder_docs(folder, action="condition")
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
            "mediation_contact_count": 0,
        }

        if not self.db:
            return counts

        # 日期清單（供報結頁面用 doAdd*Dt() 新增日期列）
        counts["court_dates"] = []
        counts["review_dates"] = []
        _assign_day = ""
        _is_criminal_laf_case = False
        if self.db and case_number:
            try:
                _case_row = self.db.fetch_one(
                    "SELECT start_date, approval_date, case_type, case_category, legal_aid_number, folder_path "
                    "FROM cases WHERE case_number = %s OR legal_aid_number = %s LIMIT 1",
                    (case_number, case_number),
                    as_dict=True,
                )
                _assign_raw = (_case_row or {}).get("start_date") or (_case_row or {}).get("approval_date")
                if _assign_raw:
                    _assign_day = str(_assign_raw)[:10]
                _case_text = " ".join(
                    str((_case_row or {}).get(k) or "")
                    for k in ("case_type", "case_category", "legal_aid_number", "folder_path")
                )
                _is_criminal_laf_case = ("刑事" in _case_text) and ("法扶" in _case_text or bool((_case_row or {}).get("legal_aid_number")))
            except Exception:
                _assign_day = ""
        _todo_start_sql = " AND (`todo_date` IS NULL OR `todo_date` >= %s)" if _assign_day else ""
        _meeting_start_sql = " AND (`datetime` IS NULL OR DATE(`datetime`) >= %s)" if _assign_day else ""

        try:
            # 1. Meeting count (from meetings table)
            _meeting_params = [case_number]
            if _assign_day:
                _meeting_params.append(_assign_day)
            result = self.db.fetch_one(
                f"""SELECT COUNT(*) as cnt FROM `meetings`
                   WHERE `case_number` = %s
                   {_meeting_start_sql}
                   AND (`datetime` IS NULL OR DATE(`datetime`) <= CURDATE())
                   AND COALESCE(`status`, '') NOT IN ('cancelled', 'canceled', '取消')""",
                tuple(_meeting_params)
            )
            if result:
                counts["meeting_count"] = result[0] if isinstance(result, tuple) else result.get("cnt", 0)

            _todo_meeting_params = [case_number]
            if _assign_day:
                _todo_meeting_params.append(_assign_day)
            result = self.db.fetch_one(
                f"""SELECT COUNT(DISTINCT CONCAT(COALESCE(`todo_date`, ''), '|', COALESCE(`todo_time`, ''), '|', COALESCE(`description`, ''))) as cnt
                   FROM `case_todos`
                   WHERE `case_number` = %s
                   {_todo_start_sql}
                   AND (`todo_date` IS NULL OR `todo_date` <= CURDATE())
                   AND (
                        `todo_type` LIKE '%%會議%%'
                        OR `todo_type` LIKE '%%會面%%'
                        OR `todo_type` LIKE '%%面談%%'
                        OR `todo_type` LIKE '%%開會%%'
                        OR `description` LIKE '%%會議%%'
                        OR `description` LIKE '%%會面%%'
                        OR `description` LIKE '%%面談%%'
                        OR `description` LIKE '%%開會%%'
                   )
                   AND COALESCE(`description`, '') NOT LIKE '%%U會議%%'
                   AND COALESCE(`description`, '') NOT LIKE '%%Ｕ會議%%'
                   AND COALESCE(`todo_type`, '') NOT LIKE '%%視訊會議%%'
                   AND COALESCE(`description`, '') NOT LIKE '%%視訊會議%%'
                   AND COALESCE(`todo_type`, '') NOT LIKE '%%律見%%'
                   AND COALESCE(`todo_type`, '') NOT LIKE '%%律師接見%%'
                   AND COALESCE(`description`, '') NOT LIKE '%%律見%%'
                   AND COALESCE(`description`, '') NOT LIKE '%%律師接見%%'
                   AND COALESCE(`status`, '') NOT IN ('cancelled', 'canceled', '取消')""",
                tuple(_todo_meeting_params),
            )
            if result:
                counts["meeting_count"] = max(
                    int(counts.get("meeting_count", 0) or 0),
                    int(result[0] if isinstance(result, tuple) else result.get("cnt", 0) or 0),
                )

            # 2. Contact count (from case_todos — phone/contact only; meeting/接見 are counted elsewhere)
            _contact_params = [case_number]
            if _assign_day:
                _contact_params.append(_assign_day)
            result = self.db.fetch_one(
                f"""SELECT COUNT(*) as cnt FROM `case_todos`
                   WHERE `case_number` = %s
                   {_todo_start_sql}
                   AND (`todo_type` LIKE '%%電話%%' OR `todo_type` LIKE '%%聯繫%%'
                        OR `todo_type` LIKE '%%聯絡%%' OR `todo_type` LIKE '%%通話%%'
                        OR `todo_type` LIKE '%%電聯%%')
                   AND (`todo_date` IS NULL OR `todo_date` <= CURDATE())
                   AND COALESCE(`status`, '') NOT IN ('cancelled', 'canceled', '取消')""",
                tuple(_contact_params)
            )
            if result:
                counts["contact_count"] = result[0] if isinstance(result, tuple) else result.get("cnt", 0)

            # 2b. Inq count (律見次數 — from case_todos)
            _inq_params = [case_number]
            if _assign_day:
                _inq_params.append(_assign_day)
            result = self.db.fetch_one(
                f"""SELECT COUNT(*) as cnt FROM `case_todos`
                   WHERE `case_number` = %s
                   {_todo_start_sql}
                   AND (`todo_type` LIKE '%%律見%%' OR `todo_type` LIKE '%%律師接見%%'
                        OR `todo_type` LIKE '%%接見%%'
                        OR `description` LIKE '%%律見%%' OR `description` LIKE '%%律師接見%%'
                        OR (
                            `description` LIKE '%%接見%%'
                            AND COALESCE(`description`, '') NOT LIKE '%%禁止接見%%'
                            AND COALESCE(`description`, '') NOT LIKE '%%限制接見%%'
                            AND COALESCE(`description`, '') NOT LIKE '%%接見、通信%%'
                            AND COALESCE(`description`, '') NOT LIKE '%%接見通信%%'
                        ))
                   AND (`status` = 'completed' OR `source_file` LIKE 'gcal_import%%')
                   AND (`todo_date` IS NULL OR `todo_date` <= CURDATE())
                   AND COALESCE(`status`, '') NOT IN ('cancelled', 'canceled', '取消')""",
                tuple(_inq_params)
            )
            if result:
                counts["inq_count"] = result[0] if isinstance(result, tuple) else result.get("cnt", 0)

            # 3. Court dates (from case_todos — hearings)
            _court_params = [case_number]
            if _assign_day:
                _court_params.append(_assign_day)
            _court_rows = self.db.fetch_all(
                f"""SELECT `todo_date` FROM `case_todos`
                   WHERE `case_number` = %s
                   {_todo_start_sql}
                   AND (
                        `todo_type` IN ('言詞辯論', '準備程序', '審理程序', '調解', '開庭', '訊問', '協商程序', '調查', '調查程序')
                        OR `todo_type` LIKE '%%言詞辯論%%'
                        OR `todo_type` LIKE '%%準備程序%%'
                        OR `todo_type` LIKE '%%審理%%'
                        OR `todo_type` LIKE '%%調解%%'
                        OR `todo_type` LIKE '%%開庭%%'
                        OR `todo_type` LIKE '%%訊問%%'
                        OR `todo_type` LIKE '%%協商程序%%'
                        OR `todo_type` LIKE '%%調查%%'
                   )
                   AND (`status` = 'completed' OR `source_file` LIKE 'gcal_import%%')
                   AND (`todo_date` IS NULL OR `todo_date` <= CURDATE())
                   AND COALESCE(`description`, '') NOT LIKE '%%宣判%%'
                   AND COALESCE(`description`, '') NOT LIKE '%%宣示判決%%'
                   AND COALESCE(`status`, '') NOT IN ('cancelled', 'canceled', '取消')
                   ORDER BY `todo_date`""",
                tuple(_court_params)
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
            _review_params = [case_number]
            if _assign_day:
                _review_params.append(_assign_day)
            _review_rows = self.db.fetch_all(
                f"""SELECT `todo_date` FROM `case_todos`
                   WHERE `case_number` = %s
                   {_todo_start_sql}
                   AND (`todo_type` LIKE '%%閱卷%%' OR `todo_type` LIKE '%%review%%')
                   AND (`status` = 'completed' OR `source_file` LIKE 'gcal_import%%')
                   AND (`todo_date` IS NULL OR `todo_date` <= CURDATE())
                   AND COALESCE(`status`, '') NOT IN ('cancelled', 'canceled', '取消')
                   ORDER BY `todo_date`""",
                tuple(_review_params)
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
                       WHERE (`case_number` = %s OR `case_full_name` LIKE %s)
                       AND `file_path` LIKE '%%04\_我方歷次書狀%%'""",
                    (_doc_key, f"{_doc_key}%")
                )
                if result:
                    counts["document_count"] = result[0] if isinstance(result, tuple) else result.get("cnt", 0)

        except Exception as e:
            logger.error("Error gathering counts for %s: %s", case_number, e)

        # 當計數為 0 時，從 calendar_events 表補數字（和 OSC 的 GCal 統計邏輯一致）
        # 只查派案日期之後的事件
        _zero_keys = [k for k in ("meeting_count", "contact_count", "inq_count", "court_count", "review_count")
                      if int(counts.get(k, 0) or 0) == 0]
        if _zero_keys and self.db and client_name:
            try:
                import re as _re
                from datetime import date as _date
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

                _name_clause = " AND (summary LIKE %s OR title LIKE %s OR description LIKE %s OR case_number = %s)"
                _params.extend([f"%{_cn_only}%", f"%{_cn_only}%", f"%{_cn_only}%", case_number])

                _events = self.db.fetch_all(
                    f"SELECT title, summary, description, start_date FROM calendar_events WHERE 1=1{_date_clause}{_name_clause}",
                    tuple(_params)
                ) or []

                if _events:
                    # 用 OSC 相同的關鍵字分類
                    _court_kw = ["開庭", "言詞辯論", "準備程序", "調解", "調解庭", "訊問", "詢問庭", "審理", "審理程序", "審查庭", "免責庭", "協商程序", "調查", "調查程序"]
                    _meet_kw = ["會議", "來所", "碰面", "面談", "開會", "交資料"]
                    _tel_kw = ["電話", "電話聯繫", "通話", "電聯", "聯繫", "聯絡"]
                    _review_kw = ["閱卷", "影卷", "調卷"]
                    _mediation_kw = ["調解", "調解庭", "和解", "調和解"]
                    _excl_kw = ["聲請改期", "改期", "取消", "不出席", "不到庭", "宣判", "宣示判決", "法扶開辦末日", "法扶上訴", "法扶再議", "停班", "停課", "放假", "颱風", "天然災害", "U會議", "Ｕ會議"]

                    _c_court = 0; _c_meet = 0; _c_tel = 0; _c_inq = 0; _c_review = 0; _c_mediation = 0
                    _court_dates_cal = []; _review_dates_cal = []
                    _seen_inq_slots: set = set()
                    _seen_mediation_slots: set = set()
                    for row in _events:
                        if isinstance(row, dict):
                            start_date = row.get("start_date")
                            s = " ".join(
                                str(row.get(k) or "")
                                for k in ("title", "summary", "description")
                            )
                        else:
                            title, summary, description, start_date = row
                            s = " ".join(str(v or "") for v in (title, summary, description))
                        if any(ex in s for ex in _excl_kw):
                            continue
                        if str(start_date or "")[:10] > _date.today().isoformat():
                            continue
                        if any(k in s for k in _mediation_kw):
                            _slot = str(start_date or "")[:16] if start_date else s[:80]
                            if _slot and _slot not in _seen_mediation_slots:
                                _c_mediation += 1
                                _seen_mediation_slots.add(_slot)
                        if any(k in s for k in _court_kw):
                            _c_court += 1
                            if start_date:
                                _court_dates_cal.append(start_date)
                        elif any(k in s for k in _review_kw):
                            _c_review += 1
                            if start_date:
                                _review_dates_cal.append(start_date)
                        elif self._is_laf_inquiry_text(s, criminal_laf=_is_criminal_laf_case):
                            _slot = str(start_date or "")[:16] if start_date else s[:80]
                            if _slot and _slot not in _seen_inq_slots:
                                _c_inq += 1
                                _seen_inq_slots.add(_slot)
                        elif "視訊會議" in s:
                            continue
                        elif any(k in s for k in _meet_kw):
                            _c_meet += 1
                        elif any(k in s for k in _tel_kw):
                            _c_tel += 1

                    # Take max across DB sources: existing case_todos value vs calendar_events.
                    # Reason: 王淑婷 case_todos 只有 1 次開庭標完成，calendar_events 也只有 1 筆，
                    # 但 GCal 其實有 2 筆 — 後續 GCal 階段會再取 max。
                    if _c_meet > int(counts.get("meeting_count", 0) or 0):
                        counts["meeting_count"] = _c_meet
                        logger.info("  📅 Calendar 補 meeting_count: %d", _c_meet)
                    if _c_tel > int(counts.get("contact_count", 0) or 0):
                        counts["contact_count"] = _c_tel
                        logger.info("  📅 Calendar 補 contact_count: %d", _c_tel)
                    if _c_inq > int(counts.get("inq_count", 0) or 0):
                        counts["inq_count"] = _c_inq
                        logger.info("  📅 Calendar 補 inq_count: %d", _c_inq)
                    if _c_court > int(counts.get("court_count", 0) or 0):
                        counts["court_count"] = _c_court
                        if not counts["court_dates"]:
                            counts["court_dates"] = _court_dates_cal
                        logger.info("  📅 Calendar 補 court_count: %d (dates: %s)", _c_court, _court_dates_cal)
                    if _c_review > int(counts.get("review_count", 0) or 0):
                        counts["review_count"] = _c_review
                        if not counts["review_dates"]:
                            counts["review_dates"] = _review_dates_cal
                        logger.info("  📅 Calendar 補 review_count: %d (dates: %s)", _c_review, _review_dates_cal)
                    if _c_mediation > int(counts.get("mediation_contact_count", 0) or 0):
                        counts["mediation_contact_count"] = _c_mediation
                        logger.info("  📅 Calendar 補 mediation_contact_count: %d", _c_mediation)
            except Exception as e:
                logger.warning("Calendar fallback for zero counts failed: %s", e)

        # 第三層 fallback：無論 DB 有無，都查 Google Calendar API 取 max（live 永遠最準確）
        # 傳全部 keys 讓 _gcal_fallback_counts 自己判斷是否 > 目前值
        if client_name:
            try:
                _all_keys = ["meeting_count", "contact_count", "inq_count", "court_count", "review_count", "mediation_contact_count"]
                self._gcal_fallback_counts(
                    counts,
                    _all_keys,
                    case_number,
                    client_name,
                    criminal_laf_case=_is_criminal_laf_case,
                )
            except Exception as e:
                logger.warning("GCal API fallback for zero counts failed: %s", e)

        if int(counts.get("review_count", 0) or 0) <= 0:
            try:
                _case_row = self.db.fetch_one(
                    "SELECT folder_path FROM cases WHERE case_number = %s OR legal_aid_number = %s LIMIT 1",
                    (case_number, case_number),
                    as_dict=True,
                )
                _folder_path = str((_case_row or {}).get("folder_path") or "").strip()
                if _folder_path and hasattr(self.db, "translate_path_to_local"):
                    _folder_path = self.db.translate_path_to_local(_folder_path) or _folder_path
                _review_count_from_folder = 0
                _review_dates_from_folder: list[str] = []
                _review_dir_used = ""
                if _folder_path and os.path.isdir(_folder_path):
                    for _review_dir_name in ("06_閱卷資料", "04_閱卷資料", "03_閱卷資料"):
                        _review_dir = os.path.join(_folder_path, _review_dir_name)
                        if not os.path.isdir(_review_dir):
                            continue
                        try:
                            for _sub in os.listdir(_review_dir):
                                _sub_path = os.path.join(_review_dir, _sub)
                                if not os.path.isdir(_sub_path) or _sub.startswith("."):
                                    continue
                                _has_real_files = False
                                try:
                                    for _fn in os.listdir(_sub_path):
                                        _fn_lower = _fn.lower()
                                        if _fn_lower.startswith("."):
                                            continue
                                        if any(_k in _fn for _k in ("繳費", "規費", "繳款單", "繳費單")):
                                            continue
                                        _has_real_files = True
                                        break
                                except OSError:
                                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 7161, exc_info=True)
                                if _has_real_files:
                                    _review_count_from_folder += 1
                                    _review_dates_from_folder.append(str(_sub)[:10])
                        except OSError:
                            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 7166, exc_info=True)
                        if _review_count_from_folder > 0:
                            _review_dir_used = _review_dir_name
                            break
                if _review_count_from_folder > 0:
                    counts["review_count"] = _review_count_from_folder
                    if not counts.get("review_dates"):
                        counts["review_dates"] = _review_dates_from_folder
                    logger.info("  📂 閱卷次數從資料夾補齊: %d (from %s, 排除純繳費單目錄)", _review_count_from_folder, _review_dir_used)
            except Exception as e:
                logger.warning("Review folder fallback failed: %s", e)

        return counts

    @staticmethod
    def _is_laf_inquiry_text(text: str, criminal_laf: bool = False) -> bool:
        """Return True when text describes an actual lawyer detention visit."""
        s = str(text or "")
        if not s:
            return False
        if "律見" in s or "律師接見" in s:
            return True
        if "接見" not in s:
            return False
        # Court rulings often say "禁止接見、通信"; those are not visits.
        blocked = ("禁止接見", "限制接見", "接見、通信", "接見通信")
        return not any(k in s for k in blocked)

    def _gcal_fallback_counts(
        self,
        counts: dict,
        zero_keys: list,
        case_number: str,
        client_name: str,
        criminal_laf_case: bool = False,
    ) -> None:
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

        # 按當事人姓名過濾；舊案若有派案日，必須從派案日往後查全期間，
        # 否則超過 OSC 建置時間的法扶案件會漏掉早期開庭、會議與聯繫紀錄。
        _cn_parts = _re.findall(r'[\u4e00-\u9fff]+', client_name)
        _cn_only = "".join(_cn_parts) if _cn_parts else client_name
        if not _cn_only:
            return

        now = datetime.now(timezone.utc)
        fallback_days = int(os.environ.get("MAGI_LAF_GCAL_LOOKBACK_DAYS", "730") or "730")
        time_min_dt = now - timedelta(days=fallback_days)
        _assign_day = ""
        try:
            if self.db and case_number:
                _case_row = self.db.fetch_one(
                    "SELECT start_date, approval_date FROM cases WHERE case_number = %s OR legal_aid_number = %s LIMIT 1",
                    (case_number, case_number),
                    as_dict=True,
                )
                _assign_raw = (_case_row or {}).get("start_date") or (_case_row or {}).get("approval_date")
                if _assign_raw:
                    _assign_day = str(_assign_raw)[:10]
                    _assign_dt = datetime.strptime(_assign_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    time_min_dt = _assign_dt
        except Exception as e:
            logger.debug("GCal assignment-date lookup failed: %s", e)
        time_min = time_min_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
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
        _seen_keys: set = set()
        _raw_total = 0
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
                    _raw_total += len(_items)
                    for _ev in _items:
                        # 跨日曆去重：同一事件若同時出現在主日曆+事務所共用日曆，
                        # event.id 通常相同；若是手動複製則用 (date + summary) 去重。
                        _ev_id = (_ev.get("id") or "").strip()
                        _st = _ev.get("start", {}).get("dateTime") or _ev.get("start", {}).get("date", "") or ""
                        _sm = _ev.get("summary", "") or ""
                        _k = _ev_id or f"{_st[:10]}|{_sm[:80]}"
                        if _k in _seen_keys:
                            continue
                        _seen_keys.add(_k)
                        events.append(_ev)
            except Exception as e:
                logger.debug("GCal API list failed for calendar %s: %s", _cal_id[:30], e)

        if not events:
            logger.info("  📅 GCal API: no events found for '%s' across %d calendars", _cn_only, len(cal_ids))
            return

        logger.info("  📅 GCal API: total %d events for '%s' across %d calendars (raw=%d, dedup=%d)",
                    len(events), _cn_only, len(cal_ids), _raw_total, _raw_total - len(events))

        _court_kw = ["開庭", "言詞辯論", "準備程序", "調解", "調解庭", "訊問", "詢問庭", "審理", "審理程序", "審查庭", "免責庭", "協商程序", "調查", "調查程序"]
        _meet_kw = ["會議", "來所", "碰面", "面談", "開會", "交資料"]
        _tel_kw = ["電話", "電話聯繫", "通話", "電聯", "聯繫", "聯絡"]
        _review_kw = ["閱卷", "影卷", "調卷"]
        _mediation_kw = ["調解", "調解庭", "和解", "調和解"]
        _excl_kw = ["聲請改期", "改期", "取消", "不出席", "不到庭", "宣判", "宣示判決", "法扶開辦末日", "法扶上訴", "法扶再議", "停班", "停課", "放假", "颱風", "天然災害", "U會議", "Ｕ會議"]

        _c_court = 0; _c_meet = 0; _c_tel = 0; _c_inq = 0; _c_review = 0; _c_mediation = 0
        _court_dates_gcal = []; _review_dates_gcal = []
        # 同日同類事件只算一次（不同日曆用不同 summary 的情形常見，例如
        # 「王淑婷審理」vs「[2025-0024] 王淑婷 - 審理程序」同是 2026-03-12 一場庭）
        _seen_court_dates: set = set()
        _seen_review_dates: set = set()
        _seen_meet_slots: set = set()
        _seen_tel_slots: set = set()
        _seen_inq_slots: set = set()
        _seen_mediation_slots: set = set()
        for ev in events:
            summary = ev.get("summary", "")
            if any(ex in summary for ex in _excl_kw):
                continue
            start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
            _dk = (start or "")[:10]
            if _assign_day and _dk and _dk < _assign_day:
                continue
            if _dk and _dk > datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                continue
            if any(k in summary for k in _mediation_kw):
                _slot = (start or "")[:16] if start else _dk
                if _slot and _slot not in _seen_mediation_slots:
                    _c_mediation += 1
                    _seen_mediation_slots.add(_slot)
            if any(k in summary for k in _court_kw):
                if _dk and _dk in _seen_court_dates:
                    continue
                _c_court += 1
                if _dk:
                    _seen_court_dates.add(_dk)
                    _court_dates_gcal.append(_dk)
            elif any(k in summary for k in _review_kw):
                if _dk and _dk in _seen_review_dates:
                    continue
                _c_review += 1
                if _dk:
                    _seen_review_dates.add(_dk)
                    _review_dates_gcal.append(_dk)
            elif self._is_laf_inquiry_text(summary, criminal_laf=criminal_laf_case):
                _slot = (start or "")[:16] if start else _dk
                if _slot and _slot in _seen_inq_slots:
                    continue
                _c_inq += 1
                if _slot:
                    _seen_inq_slots.add(_slot)
            elif "視訊會議" in summary:
                continue
            elif any(k in summary for k in _meet_kw):
                _slot = (start or "")[:16] if start else _dk
                if _slot and _slot in _seen_meet_slots:
                    continue
                _c_meet += 1
                if _slot:
                    _seen_meet_slots.add(_slot)
            elif any(k in summary for k in _tel_kw):
                _slot = (start or "")[:16] if start else _dk
                if _slot and _slot in _seen_tel_slots:
                    continue
                _c_tel += 1
                if _slot:
                    _seen_tel_slots.add(_slot)

        # Take max across all sources: GCal API is live and usually most complete.
        # 只補值 when GCal 數字 > 目前值（避免 GCal 關鍵字漏配造成倒退）
        if "meeting_count" in zero_keys and _c_meet > int(counts.get("meeting_count", 0) or 0):
            counts["meeting_count"] = _c_meet
            logger.info("  📅 GCal API 補 meeting_count: %d", _c_meet)
        if "contact_count" in zero_keys and _c_tel > int(counts.get("contact_count", 0) or 0):
            counts["contact_count"] = _c_tel
            logger.info("  📅 GCal API 補 contact_count: %d", _c_tel)
        if "inq_count" in zero_keys and _c_inq > int(counts.get("inq_count", 0) or 0):
            counts["inq_count"] = _c_inq
            logger.info("  📅 GCal API 補 inq_count: %d", _c_inq)
        if "court_count" in zero_keys and _c_court > int(counts.get("court_count", 0) or 0):
            counts["court_count"] = _c_court
            # 以 GCal 的實際日期覆蓋（更完整）
            if _court_dates_gcal:
                counts["court_dates"] = _court_dates_gcal
            logger.info("  📅 GCal API 補 court_count: %d (dates: %s)", _c_court, _court_dates_gcal)
        if "review_count" in zero_keys and _c_review > int(counts.get("review_count", 0) or 0):
            counts["review_count"] = _c_review
            if _review_dates_gcal:
                counts["review_dates"] = _review_dates_gcal
            logger.info("  📅 GCal API 補 review_count: %d", _c_review)
        if "mediation_contact_count" in zero_keys and _c_mediation > int(counts.get("mediation_contact_count", 0) or 0):
            counts["mediation_contact_count"] = _c_mediation
            logger.info("  📅 GCal API 補 mediation_contact_count: %d", _c_mediation)

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
                    """
                    SELECT * FROM `cases`
                    WHERE TRIM(COALESCE(`legal_aid_number`, '')) = %s
                       OR TRIM(COALESCE(`laf_case_no`, '')) = %s
                       OR TRIM(COALESCE(`application_no`, '')) = %s
                    ORDER BY `created_date` DESC
                    LIMIT 1
                    """,
                    (laf_number, laf_number, laf_number), as_dict=True
                )
                if result:
                    logger.debug(
                        "Duplicate matched by=laf_number_columns, laf_no=%s, client=%s, row_id=%s",
                        laf_number, result.get("client_name"), result.get("id")
                    )
                    return result

                # Strategy 1b: notes LIKE — 必須同時驗 client_name，避免案號 substring 誤判
                rows_by_notes = self.db.fetch_all(
                    "SELECT * FROM `cases` WHERE `notes` LIKE %s LIMIT 20",
                    (f"%{laf_number}%",), as_dict=True
                )
                for result in (rows_by_notes or []):
                    if not isinstance(result, dict):
                        continue
                    db_client_key = self._norm_token(result.get("client_name"))
                    if client_key and db_client_key and db_client_key != client_key:
                        logger.debug(
                            "Duplicate skip notes-match: laf_no=%s matched row client=%s != input client=%s",
                            laf_number, result.get("client_name"), client_name
                        )
                        continue
                    logger.debug(
                        "Duplicate matched by=notes, laf_no=%s, client=%s, row_id=%s",
                        laf_number, result.get("client_name"), result.get("id")
                    )
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
                    db_client_key = self._norm_token(result.get("client_name"))
                    if db_client_key != client_key:
                        continue

                    existing_laf = str(result.get("legal_aid_number") or "").strip()
                    db_reason = str(result.get("case_reason") or "").strip()
                    _reason_empty = not case_reason or case_reason in ("待確認", "")

                    if laf_number:
                        notes = str(result.get("notes") or "")
                        if existing_laf and existing_laf != laf_number and laf_number not in notes:
                            continue
                        # Strategy 2 特殊規則：case_reason 為空/待確認時，
                        # 必須要求 laf_case_no 完全相符，避免不同案件因同姓名誤合
                        if _reason_empty and existing_laf and existing_laf != laf_number:
                            logger.debug(
                                "Duplicate skip S2 empty-reason: laf_no=%s != existing=%s, client=%s",
                                laf_number, existing_laf, client_name
                            )
                            continue

                    if case_reason and not _reason_empty:
                        if db_reason and (case_reason in db_reason or db_reason in case_reason):
                            logger.debug(
                                "Duplicate matched by=name+type+reason, laf_no=%s, client=%s, row_id=%s",
                                laf_number, result.get("client_name"), result.get("id")
                            )
                            return result
                        continue

                    logger.debug(
                        "Duplicate matched by=name+type(fallback), laf_no=%s, client=%s, row_id=%s",
                        laf_number, result.get("client_name"), result.get("id")
                    )
                    return result

        except Exception as e:
            logger.error("Duplicate check error: %s", e)

        return None

    def _update_legal_aid_number(self, case_id, laf_number):
        """Backfill structured LAF identifiers for an existing case."""
        if self.dry_run or not self.db:
            return
        try:
            self.db.execute_write(
                "UPDATE `cases` SET "
                "`legal_aid_number` = CASE WHEN `legal_aid_number` IS NULL OR `legal_aid_number` = '' THEN %s ELSE `legal_aid_number` END, "
                "`laf_case_no` = CASE WHEN `laf_case_no` IS NULL OR `laf_case_no` = '' THEN %s ELSE `laf_case_no` END, "
                "`application_no` = CASE WHEN `application_no` IS NULL OR `application_no` = '' THEN %s ELSE `application_no` END "
                "WHERE `id` = %s",
                (laf_number, laf_number, laf_number, case_id)
            )
        except Exception as e:
            logger.error("Update LAF number failed: %s", e)

    @staticmethod
    def _normalized_investigation_case_fields(existing: dict) -> dict:
        """Return a provable legacy LAF service-label correction, or no change.

        This accepts only deterministic service labels: an investigation label
        embedded in ``case_reason`` or a portal criminal-procedure label such as
        ``刑事一審辯護`` stored in type/stage.  It must not reinterpret arbitrary
        existing human-edited fields.
        """
        current_type = str(existing.get("case_type") or "").strip()
        current_stage = str(existing.get("case_stage") or "").strip()
        current_reason = str(existing.get("case_reason") or "").strip()
        investigation_shape = bool(re.match(
            r"^(?:刑事)?偵查(?:中)?辯護(?:案件)?[\-－—:：/、]*",
            re.sub(r"\s+", "", current_reason),
        ))
        service_stage_shape = bool(re.search(
            r"刑事(?:通常程序)?(?:第)?(?:一|二|三)審辯護(?:案件)?|刑事(?:通常程序)?更審辯護(?:案件)?",
            re.sub(r"\s+", "", f"{current_type} {current_stage}"),
        ))
        if not investigation_shape and not service_stage_shape:
            return {}

        normalized_type, normalized_stage, normalized_reason = normalize_laf_case_fields(
            current_type,
            current_stage,
            current_reason,
            "",
        )
        if (normalized_type, normalized_stage, normalized_reason) == (
            current_type,
            current_stage,
            current_reason,
        ):
            return {}
        if investigation_shape and not normalized_reason:
            # A service label without a substantive reason still needs human
            # confirmation; never invent one during an automatic repair.
            return {}
        return {
            "case_type": normalized_type,
            "case_stage": normalized_stage,
            "case_reason": normalized_reason,
        }

    def _reconcile_normalized_case_record(self, existing: dict, normalized: dict) -> bool:
        """Atomically align a deterministic DB correction with its NAS folder.

        The NAS move is performed first.  If the DB write fails, the move is
        rolled back.  An occupied/missing source or an existing destination is
        fail-closed so OSC can never report a green correction while DB and NAS
        disagree.
        """
        if getattr(self, "dry_run", False) or not self.db:
            return False

        case_id = str(existing.get("id") or "").strip()
        case_number = str(existing.get("case_number") or "").strip()
        client_name = str(existing.get("client_name") or "").strip()
        old_canonical = str(existing.get("folder_path") or "").strip()
        new_type = str(normalized.get("case_type") or "").strip()
        new_stage = str(normalized.get("case_stage") or "").strip()
        new_reason = str(normalized.get("case_reason") or "").strip()
        if not all((case_id, case_number, client_name, new_type, new_stage, new_reason)):
            logger.error("Normalized case repair rejected: incomplete case identity or fields")
            return False

        try:
            from laf_folder_builder import LAFFolderBuilder  # type: ignore

            builder = LAFFolderBuilder()
            folder_info = {
                "case_number": case_number,
                "client_name": client_name,
                "case_type": new_type,
                "case_stage": new_stage,
                "case_reason": new_reason,
            }
            new_basename = builder._build_folder_name(folder_info)
            new_local = builder._get_local_path(new_basename, folder_info)
            new_canonical = builder._local_to_canonical(new_local)
        except Exception as e:
            logger.error("Normalized case repair could not build target path: %s", e)
            return False

        old_local = ""
        if old_canonical:
            try:
                for candidate in local_synology_path_candidates(old_canonical) or []:
                    if os.path.isdir(candidate):
                        old_local = candidate
                        break
            except Exception:
                logger.debug("Canonical path mapping failed during normalized repair", exc_info=True)
            if not old_local and os.path.isdir(old_canonical):
                old_local = old_canonical
            if not old_local:
                logger.error("Normalized case repair source folder is unavailable: %s", old_canonical)
                return False

        moved = False
        if old_local and os.path.abspath(old_local) != os.path.abspath(new_local):
            if os.path.exists(new_local):
                logger.error("Normalized case repair target already exists: %s", new_local)
                return False

            try:
                proc = subprocess.run(
                    ["lsof", "+D", old_local],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                open_lines = [
                    line
                    for line in (proc.stdout or "").splitlines()
                    if line and not line.startswith("COMMAND")
                ]
            except subprocess.TimeoutExpired:
                logger.error("Normalized case repair lsof check timed out: %s", old_local)
                return False
            except Exception as e:
                logger.error("Normalized case repair lsof check failed: %s", e)
                return False
            if open_lines:
                logger.error("Normalized case repair source is in use: %s", old_local)
                return False

            try:
                builder._safe_makedirs(os.path.dirname(new_local))
                os.rename(old_local, new_local)
                moved = True
            except OSError as e:
                logger.error("Normalized case repair folder move failed: %s", e)
                return False

        final_canonical = new_canonical if old_local else old_canonical
        update_ok = self.db.execute_write(
            """UPDATE `cases`
               SET `case_type` = %s,
                   `case_stage` = %s,
                   `case_reason` = %s,
                   `folder_path` = %s
               WHERE `id` = %s""",
            (new_type, new_stage, new_reason, final_canonical, case_id),
        )
        if not update_ok:
            if moved:
                try:
                    os.rename(new_local, old_local)
                except OSError:
                    logger.critical(
                        "Normalized case repair DB write and NAS rollback both failed: %s",
                        case_number,
                        exc_info=True,
                    )
            return False

        try:
            verified = self.db.fetch_one(
                """SELECT `case_type`, `case_stage`, `case_reason`, `folder_path`
                     FROM `cases` WHERE `id` = %s""",
                (case_id,),
                as_dict=True,
            )
        except Exception:
            # The write may already be committed.  Do not move the folder back
            # on a read outage and create a new mismatch; report failure so the
            # next bounded run re-checks the durable state.
            logger.error(
                "Normalized case repair could not verify the committed DB row: %s",
                case_number,
                exc_info=True,
            )
            return False

        expected_db = (new_type, new_stage, new_reason, final_canonical)
        observed_db = (
            str((verified or {}).get("case_type") or "").strip(),
            str((verified or {}).get("case_stage") or "").strip(),
            str((verified or {}).get("case_reason") or "").strip(),
            str((verified or {}).get("folder_path") or "").strip(),
        )
        if observed_db != expected_db:
            logger.error(
                "Normalized case repair DB verification mismatch: expected=%r observed=%r",
                expected_db,
                observed_db,
            )
            rollback_ok = self.db.execute_write(
                """UPDATE `cases`
                   SET `case_type` = %s,
                       `case_stage` = %s,
                       `case_reason` = %s,
                       `folder_path` = %s
                   WHERE `id` = %s""",
                (
                    str(existing.get("case_type") or "").strip(),
                    str(existing.get("case_stage") or "").strip(),
                    str(existing.get("case_reason") or "").strip(),
                    old_canonical,
                    case_id,
                ),
            )
            if rollback_ok and moved:
                try:
                    os.rename(new_local, old_local)
                except OSError:
                    logger.critical(
                        "Normalized case repair verification rollback could not restore NAS: %s",
                        case_number,
                        exc_info=True,
                    )
            return False

        existing["folder_path"] = final_canonical
        path_updates = _sync_case_path_references(
            self.db,
            old_canonical=old_canonical or old_local,
            new_canonical=final_canonical,
            old_local=old_local,
            new_local=new_local,
        )
        if path_updates["errors"]:
            logger.warning(
                "Normalized case path references need retry for %s: %s",
                case_number,
                path_updates["errors"],
            )
        logger.info(
            "  ✅ DB/NAS classification reconciled: %s → %s",
            case_number,
            new_basename,
        )
        return True

    def _reconcile_placeholder_record(self, existing: dict, *, new_client_name: str,
                                      new_case_reason: str, new_case_stage: str,
                                      new_case_type: str) -> bool:
        """修補 DB 中的 placeholder 案件（client_name/case_reason 是垃圾），用第二封 email 的乾淨資料。

        - UPDATE DB: client_name, case_reason, case_stage, case_type, folder_path
        - Rename 資料夾（lsof 偵測到被開啟則跳過 rename，但 DB 仍更新；通知律師）
        - 不影響 legal_aid_number / legal_aid_status / case_number / case_category 等其他欄位
        Returns True if any change applied.
        """
        if self.dry_run or not self.db:
            return False
        case_id = existing.get("id")
        case_number = existing.get("case_number") or ""
        old_folder_canonical = str(existing.get("folder_path") or "").strip()
        old_client_name = str(existing.get("client_name") or "")
        old_case_reason = str(existing.get("case_reason") or "")

        # 消費者債務清理特殊處理（同 handle_go_live 開頭）
        if new_case_type == "消費者債務清理" or "消費者債務清理" in (new_case_reason or ""):
            if "清算" not in (new_case_reason or ""):
                new_case_reason = "更生"

        # 1. 建構新資料夾名（用 LAFFolderBuilder 的 _build_folder_name 規則保持一致）
        new_basename = ""
        try:
            from laf_folder_builder import LAFFolderBuilder  # type: ignore
            fb = LAFFolderBuilder()
            new_basename = fb._build_folder_name({
                "case_number": case_number,
                "client_name": new_client_name,
                "case_type": new_case_type,
                "case_stage": new_case_stage,
                "case_reason": new_case_reason,
            })
        except Exception as e:
            logger.warning("build folder name failed: %s", e)
            return False

        # 2. UPDATE DB（client_name/reason/stage/type）
        try:
            self.db.execute_write(
                """UPDATE `cases`
                   SET `client_name` = %s,
                       `case_reason` = %s,
                       `case_stage` = CASE WHEN COALESCE(`case_stage`,'') IN ('','待確認','未確認') THEN %s ELSE `case_stage` END,
                       `case_type` = CASE WHEN COALESCE(`case_type`,'') IN ('','待確認') THEN %s ELSE `case_type` END
                   WHERE `id` = %s""",
                (new_client_name, new_case_reason, new_case_stage, new_case_type, case_id),
            )
            logger.info("  📝 DB updated: client_name=%s case_reason=%s case_stage=%s",
                        new_client_name, new_case_reason, new_case_stage)
        except Exception as e:
            logger.error("DB update failed: %s", e)
            return False

        # 3. Rename 資料夾（lsof 偵測 + 安全 rename）
        rename_ok = False
        new_canonical = old_folder_canonical
        if old_folder_canonical:
            try:
                from api.case_path_mapper import local_synology_path_candidates
                old_local = ""
                for cand in (local_synology_path_candidates(old_folder_canonical) or []):
                    if os.path.isdir(cand):
                        old_local = cand
                        break
                if old_local:
                    new_local = os.path.join(os.path.dirname(old_local), new_basename)
                    if os.path.exists(new_local):
                        logger.info("  ⚠️ 目標資料夾已存在，不 rename: %s", new_local)
                    else:
                        # lsof 偵測
                        is_open = False
                        try:
                            import subprocess as _subp
                            proc = _subp.run(
                                ["lsof", "+D", old_local],
                                capture_output=True, text=True, timeout=10,
                            )
                            out_lines = [
                                ln for ln in (proc.stdout or "").splitlines()
                                if ln and not ln.startswith("COMMAND")
                                and not (ln.split() and ln.split()[0] in {"python3", "python", "lsof", "Python"})
                            ]
                            is_open = bool(out_lines)
                        except Exception:
                            is_open = False
                        if is_open:
                            logger.info("  ⚠️ 資料夾被其他應用開啟，跳過 rename（DB 已更新）")
                            try:
                                self.notifier.notify_admin(
                                    f"⚠️ 法扶 placeholder 修正：DB 已更新但資料夾被開啟未 rename\n"
                                    f"案件: {case_number} ({existing.get('legal_aid_number') or ''})\n"
                                    f"當事人: 「{old_client_name}」 → 「{new_client_name}」\n"
                                    f"案由: 「{old_case_reason}」 → 「{new_case_reason}」\n"
                                    f"舊資料夾: {os.path.basename(old_local)}\n"
                                    f"新資料夾: {new_basename}\n"
                                    f"請關閉相關應用後手動 rename。"
                                )
                            except Exception:
                                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 7628, exc_info=True)
                        else:
                            try:
                                os.rename(old_local, new_local)
                                rename_ok = True
                                # 同步更新 canonical 路徑
                                if "\\" in old_folder_canonical:
                                    sep = "\\"
                                else:
                                    sep = "/"
                                parts = old_folder_canonical.rstrip("/\\").split(sep)
                                parts[-1] = new_basename
                                new_canonical = sep.join(parts)
                                self.db.execute_write(
                                    "UPDATE `cases` SET `folder_path` = %s WHERE `id` = %s",
                                    (new_canonical, case_id),
                                )
                                path_updates = _sync_case_path_references(
                                    self.db,
                                    old_canonical=old_folder_canonical or old_local,
                                    new_canonical=new_canonical,
                                    old_local=old_local,
                                    new_local=new_local,
                                )
                                if path_updates["errors"]:
                                    logger.warning(
                                        "placeholder path references need retry for %s: %s",
                                        case_number,
                                        path_updates["errors"],
                                    )
                                logger.info("  📁 資料夾已 rename → %s", new_basename)
                            except OSError as e:
                                logger.warning("  ⚠️ rename 失敗: %s", e)
            except Exception as e:
                logger.warning("rename folder block failed: %s", e)

        # 4. 通知律師（成功 rename 時）
        if rename_ok:
            try:
                self.notifier.notify_admin(
                    f"📝 法扶 placeholder 已自動修正\n"
                    f"案件: {case_number} ({existing.get('legal_aid_number') or ''})\n"
                    f"當事人: 「{old_client_name}」 → 「{new_client_name}」\n"
                    f"案由: 「{old_case_reason}」 → 「{new_case_reason}」\n"
                    f"📁 資料夾: {new_basename}"
                )
            except Exception:
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 7662, exc_info=True)
        return True

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

        # 消費者債務清理案件 — 案由正規化（確保即使 case_info 來自非 email 路徑也正確）
        if case_type == '消費者債務清理' or '消費者債務清理' in case_reason:
            if '清算' not in case_reason:
                case_reason = '更生'

        case_number = str(case_number or "").strip() or self._generate_case_number()
        if not case_number:
            logger.error("  ❌ Could not determine standard case_number for DB insert")
            return ""

        branch = str(getattr(case_info, 'branch', '') or '').strip()
        notes = f"法扶案號: {laf_number}\n"
        if branch:
            notes += f"分會: {branch}\n"

        default_lawyer = normalize_case_lawyer(
            "",
            allow_default=True,
            case_type=case_type,
            case_reason=case_reason,
            case_category="法律扶助案件",
            settings_getter=db_settings_getter(getattr(self, "db", None)),
        )

        try:
            self.db.execute_write(
                """INSERT INTO `cases`
                   (`id`, `case_number`, `client_name`, `case_type`, `case_reason`,
                    `case_category`, `case_stage`, `status`, `folder_path`,
                    `legal_aid_number`, `laf_case_no`, `application_no`,
                    `legal_aid_status`, `notes`,
                    `start_date`, `lawyer`)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (case_id, case_number, client_name, case_type, case_reason,
                 '法律扶助案件', case_stage, '進行中', folder_path,
                 laf_number, laf_number, laf_number,
                 '未開辦', notes,
                 datetime.now().date(), default_lawyer)
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
        trigger_reason: str = "",
        trigger_id: str = "",
        trigger_received_at=None,
    ):
        """Download case files from LAF portal."""
        result = {
            "ok": True,
            "laf_case_number": str(laf_number or "").strip(),
            "downloaded_files": [],
            "downloaded_count": 0,
            "retry_queued": False,
            "retry_reason": "",
            "retry_queue_token": "",
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
            automation = self._get_automation()
            if not automation.login():
                logger.warning("LAF login failed, queueing portal download retry for %s", laf_number)
                result["ok"] = False
                result["error"] = "login_failed"
                failed_result = self._process_portal_download_result(
                    laf_number=str(laf_number or ""),
                    client_name=client_name,
                    case_type=case_type,
                    case_reason=case_reason,
                    case_folder=case_folder,
                    case_number=case_number,
                    files=[],
                    source="initial",
                    last_error="login_failed",
                    trigger_reason=trigger_reason,
                    trigger_id=trigger_id,
                    trigger_received_at=trigger_received_at,
                )
                result["retry_queued"] = bool(failed_result.get("retry_queued"))
                result["retry_reason"] = str(failed_result.get("retry_reason") or "")
                result["retry_queue_token"] = str(failed_result.get("retry_queue_token") or "")
                return result

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
                trigger_reason=trigger_reason,
                trigger_id=trigger_id,
                trigger_received_at=trigger_received_at,
            )

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
                trigger_reason=trigger_reason,
                trigger_id=trigger_id,
                trigger_received_at=trigger_received_at,
            )
            result["retry_queued"] = bool(failed_result.get("retry_queued"))
            result["retry_reason"] = str(failed_result.get("retry_reason") or "")
            result["retry_queue_token"] = str(failed_result.get("retry_queue_token") or "")
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

# Portal 操作互斥鎖：避免同時兩個 laf_orchestrator.py subprocess 搶 LAF session，
# 造成其中一個誤報「portal ... draft save failed」。
_PORTAL_LOCK_FD = None


# =============================================================================
# Subprocess result sentinel
# -----------------------------------------------------------------------------
# CLI modes that hand stdout JSON back to the API layer (portal-draft /
# portal-submit) MUST emit the JSON between these markers. The API parser then
# extracts the sentinel-delimited block, avoiding the legacy
# `re.search(r"(\{[\s\S]*\})\s*$")` greedy-match which can swallow logger /
# Playwright noise printed before the result and corrupt the JSON.
# =============================================================================
_MAGI_RESULT_SENTINEL_START = "===MAGI_RESULT_JSON_START==="
_MAGI_RESULT_SENTINEL_END = "===MAGI_RESULT_JSON_END==="


def _print_result_with_sentinel(result: dict) -> None:
    """Print result JSON wrapped in MAGI_RESULT sentinel markers."""
    try:
        body = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except Exception as _e:
        body = json.dumps({"ok": False, "error": f"json_dumps_failed: {_e}"}, ensure_ascii=False)
    print(_MAGI_RESULT_SENTINEL_START)
    print(body)
    print(_MAGI_RESULT_SENTINEL_END)


def _acquire_portal_lock(wait_sec: int = 900):
    """取得 LAF portal 全域檔案鎖；同時間只允許一個 portal-draft/submit 跑。

    等待上限 wait_sec 秒（預設 900s 與 portal-draft subprocess timeout 對齊）；
    超時則回傳 (None, False)，由 caller 中止本次 portal 操作。
    """
    global _PORTAL_LOCK_FD
    try:
        from magi_v3 import fcntl_compat as _fcntl
    except ImportError:
        return None, True  # 非 POSIX 平台不鎖，fail-open
    try:
        from api.runtime_paths import get_runtime_dir as _get_runtime_dir
        _lock_dir = str(_get_runtime_dir())
    except Exception:
        _lock_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime")
    try:
        os.makedirs(_lock_dir, exist_ok=True)
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 7927, exc_info=True)
    _lock_path = os.path.join(_lock_dir, "laf_portal.lock")
    try:
        # Do not open with "w": that truncates the owner record before the
        # process actually owns the fcntl lock, making portal hangs impossible
        # to diagnose after a daemon restart.  We overwrite only after LOCK_EX.
        fd = open(_lock_path, 'a+')
    except Exception as e:
        logger.warning("Cannot open portal lock file %s: %s", _lock_path, e)
        return None, True
    started = time.time()
    deadline = started + max(1, int(wait_sec))
    announced = False
    while time.time() < deadline:
        try:
            _fcntl.flock(fd.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            try:
                fd.seek(0)
                fd.truncate(0)
                fd.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
                fd.flush()
            except Exception:
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 7946, exc_info=True)
            _PORTAL_LOCK_FD = fd
            return fd, True
        except BlockingIOError:
            if not announced:
                owner = ""
                try:
                    fd.seek(0)
                    owner = (fd.read() or "").strip()
                except Exception:
                    owner = ""
                if owner:
                    logger.info(
                        "⏳ 另一個 LAF portal 操作正在執行，本程序排隊等候（最久 %ds；鎖持有者：%s）",
                        int(wait_sec),
                        owner[:120],
                    )
                else:
                    logger.info("⏳ 另一個 LAF portal 操作正在執行，本程序排隊等候（最久 %ds）", int(wait_sec))
                announced = True
            time.sleep(2)
    logger.warning("⚠️ 等候 LAF portal 鎖超時（%ds），中止本次 portal 操作，避免並發衝突", wait_sec)
    try:
        fd.close()
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 7958, exc_info=True)
    return None, False


def _release_portal_lock():
    global _PORTAL_LOCK_FD
    fd = _PORTAL_LOCK_FD
    _PORTAL_LOCK_FD = None
    if fd is None:
        return
    try:
        from magi_v3 import fcntl_compat as _fcntl
        _fcntl.flock(fd.fileno(), _fcntl.LOCK_UN)
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 7972, exc_info=True)
    try:
        fd.close()
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 7976, exc_info=True)


def main():
    parser = argparse.ArgumentParser(description="LAF Case Lifecycle Orchestrator")
    parser.add_argument("--mode", choices=["monitor", "closing", "closing-draft", "condition-draft", "condition-mark-done", "condition-mark-by-mediation", "portal-retry-once", "portal-draft", "portal-submit", "redo-go-live", "dry-run", "test-notify"],
                        default="dry-run",
                        help="monitor=watch Gmail, closing=process 待報結, redo-go-live=從 DB 重跑開辦流程（reconcile 修正 placeholder 後使用）, dry-run=preview")
    parser.add_argument("--case", type=str, default=None,
                        help="Specific case number to process (for closing mode)")
    parser.add_argument("--laf-case-no", type=str, default="", help="LAF case number for portal-draft mode")
    parser.add_argument("--client", type=str, default="", help="Client name for portal-draft mode")
    parser.add_argument("--action", type=str, default="", help="Portal action: go_live|inquiry|fee|condition|withdrawal|closing")
    parser.add_argument("--reason", type=str, default="", help="Reason/description for inquiry/fee/condition/withdrawal/closing")
    parser.add_argument("--fields-json", type=str, default="", help="Optional JSON object for workflow fields")
    parser.add_argument("--max-cases", type=int, default=3, help="Max cases for condition-draft mode")
    parser.add_argument("--max-items", type=int, default=6, help="Max queued attachments for portal-retry-once")
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=_PORTAL_RETRY_CYCLE_TIMEOUT,
        help="Watchdog timeout for portal-retry-once",
    )
    parser.add_argument("--clients", type=str, default="", help="Comma-separated client names for condition-mark-done")
    parser.add_argument("--laf-list", type=str, default="", help="Comma-separated LAF case numbers for condition-mark-done")
    parser.add_argument("--osc-list", type=str, default="", help="Comma-separated OSC case numbers for condition-mark-done")
    parser.add_argument("--max-scan", type=int, default=8000, help="Max rows scan for condition-mark-by-mediation mode")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run regardless of mode")
    parser.add_argument("--no-notify", action="store_true", help="Suppress Discord/Telegram notifications (for CLI testing)")
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
    if args.no_notify:
        orchestrator._notifier = _DryRunNotifier()

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
        try:
            r = orchestrator.run_condition_drafts(max_cases=int(args.max_cases or 3))
        finally:
            # A scheduled one-shot must close the browser/provider even when
            # draft discovery or persistence raises.  This keeps repeated
            # condition runs from retaining Playwright resources until exit.
            orchestrator.close()
        print(json.dumps(r, ensure_ascii=False, indent=2))

    elif args.mode == "portal-retry-once":
        r = orchestrator.run_portal_retry_once(
            max_items=int(args.max_items or 6),
            timeout_sec=int(args.timeout_sec or _PORTAL_RETRY_CYCLE_TIMEOUT),
        )
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if not r.get("ok"):
            raise SystemExit(1)

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
                _print_result_with_sentinel({"ok": False, "error": f"invalid_fields_json: {e}"})
                return
        # 取互斥鎖，避免與另一個 portal-draft/submit 並發搶 LAF session
        _, lock_ok = _acquire_portal_lock(wait_sec=int(os.environ.get("MAGI_LAF_PORTAL_LOCK_WAIT_SEC", "2400")))
        if not lock_ok:
            _print_result_with_sentinel({
                "ok": False,
                "error": "laf_portal_lock_timeout",
                "message": "法扶官網目前仍有其他操作佔用，已中止本次草稿建立以避免並發衝突。請稍後重試。",
            })
            return
        try:
            result = orchestrator.execute_portal_action_draft(
                action=args.action,
                laf_case_number=args.laf_case_no or "",
                case_number=args.case or "",
                client_name=args.client or "",
                reason=args.reason or "",
                fields=fields,
                suppress_notify=bool(getattr(args, 'no_notify', False)),
            )
        finally:
            _release_portal_lock()
        _print_result_with_sentinel(result)
        # Explicitly close the browser before exit to prevent Playwright async loop hang.
        try:
            orchestrator.close()
        except Exception:
            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 8118, exc_info=True)
        import sys as _sys; _sys.stdout.flush(); os._exit(0)

    elif args.mode == "portal-submit":
        fields = {}
        if args.fields_json:
            try:
                parsed = json.loads(args.fields_json)
                if isinstance(parsed, dict):
                    fields = parsed
            except Exception as e:
                _print_result_with_sentinel({"ok": False, "error": f"invalid_fields_json: {e}"})
                return
        _, lock_ok = _acquire_portal_lock(wait_sec=int(os.environ.get("MAGI_LAF_PORTAL_LOCK_WAIT_SEC", "2400")))
        if not lock_ok:
            _print_result_with_sentinel({
                "ok": False,
                "error": "laf_portal_lock_timeout",
                "message": "法扶官網目前仍有其他操作佔用，已中止本次送出以避免並發衝突。請稍後重試。",
            })
            return
        try:
            result = orchestrator.execute_portal_action_submit(
                action=args.action,
                laf_case_number=args.laf_case_no or "",
                case_number=args.case or "",
                client_name=args.client or "",
                reason=args.reason or "",
                fields=fields,
            )
        finally:
            _release_portal_lock()
        _print_result_with_sentinel(result)
        # Force exit to bypass Playwright asyncio cleanup hang (same fix as portal-draft).
        # JSON result is already printed above; os._exit skips Python teardown.
        import sys as _sys; _sys.stdout.flush()
        import os as _os
        _os._exit(0 if result.get("ok") else 1)

    elif args.mode == "redo-go-live":
        # 從 DB 讀現有案件資料，重新觸發 handle_go_live 完整流程
        # 用途：reconcile_placeholder_cases 修正 client_name/case_reason 後，重跑開辦流程
        laf_no = (args.laf_case_no or "").strip()
        if not laf_no:
            print(json.dumps({"success": False, "error": "missing --laf-case-no"}, ensure_ascii=False))
        elif not orchestrator.db:
            print(json.dumps({"success": False, "error": "db not available"}, ensure_ascii=False))
        else:
            try:
                row = orchestrator.db.fetch_one(
                    "SELECT * FROM `cases` WHERE `legal_aid_number` = %s LIMIT 1",
                    (laf_no,), as_dict=True,
                )
                if not row:
                    print(json.dumps({"success": False, "error": f"case not found: {laf_no}"}, ensure_ascii=False))
                else:
                    laf_status = str(row.get("legal_aid_status") or "").strip()
                    if laf_status and laf_status != "未開辦":
                        print(json.dumps({
                            "success": False,
                            "error": f"案件狀態為「{laf_status}」，不是未開辦；redo-go-live 只能對未開辦案件使用",
                            "case_number": row.get("case_number"),
                        }, ensure_ascii=False))
                    else:
                        try:
                            from skills.legal.laf import LAFCaseInfo  # type: ignore
                        except Exception:
                            from skills.legal.laf import LAFCaseInfo  # type: ignore
                        case_info = LAFCaseInfo(
                            laf_case_number=laf_no,
                            client_name=str(row.get("client_name") or ""),
                            case_type=str(row.get("case_type") or ""),
                            case_stage=str(row.get("case_stage") or ""),
                            case_reason=str(row.get("case_reason") or ""),
                            subject=f"[redo-go-live] {laf_no} {row.get('client_name','')}",
                        )
                        # 清除 session dedup 確保完整重跑
                        try:
                            orchestrator._go_live_dedup.discard(laf_no)
                        except Exception:
                            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 8191, exc_info=True)
                        logger.info("🔁 Redo go-live 開始: %s (%s) %s",
                                    row.get("case_number"), laf_no, row.get("client_name"))
                        orchestrator.handle_go_live(case_info)
                        print(json.dumps({
                            "success": True,
                            "message": f"redo go-live triggered for {laf_no}",
                            "case_number": row.get("case_number"),
                            "client_name": row.get("client_name"),
                        }, ensure_ascii=False))
            except Exception as redo_e:
                logger.exception("redo-go-live failed: %s", redo_e)
                print(json.dumps({"success": False, "error": str(redo_e)[:300]}, ensure_ascii=False))

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
    "_is_procedural_nonclosing_doc",
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
