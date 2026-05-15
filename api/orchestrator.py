import logging
import os
import re
import sys
import time
import json
import sqlite3
import secrets
import subprocess
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from api.thread_pools import io_pool, inference_pool
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
import threading as _threading

from api.model_config import TEXT_PRIMARY_MODEL, VISION_MODEL as _VISION_MODEL
# Thread-local storage for per-request correlation ID.
_orchestrator_tls = _threading.local()

from skills.bridge.grounded_ai import ask_casper
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

# Stability-first default:
# disable distributed inference unless explicitly turned back on.
os.environ.setdefault("MAGI_AVOID_DISTRIBUTED", "1")

# Inject local venv site-packages to ensure Playwright/psutil are available
# even when MAGI is launched by a non-venv interpreter.
_repo_root = Path(__file__).resolve().parents[1]
_venv_candidates: list[str] = []
for _venv_name in ("venv", ".venv"):
    _lib_dir = _repo_root / _venv_name / "lib"
    _current = _lib_dir / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    if _current.exists():
        _venv_candidates.append(str(_current))
    if _lib_dir.exists():
        for _path in sorted(_lib_dir.glob("python*/site-packages"), reverse=True):
            _candidate = str(_path)
            if _candidate not in _venv_candidates:
                _venv_candidates.append(_candidate)
for venv_site_pkgs in _venv_candidates:
    if venv_site_pkgs not in sys.path:
        sys.path.insert(0, venv_site_pkgs)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.events import TaskLifecycleEvent
from api.hooks import HookBus
from api.permissions import (
    PermissionEnforcer,
    PermissionMode,
    PermissionPolicy,
    deny_command,
    deny_path,
)
from api.orchestrator_core import RuntimeFoundations
from api.runtime_paths import get_laf_script, get_legacy_code_root, get_magi_root_dir, get_orch_dir, get_skill_python
from api.session import SessionContextBuilder, SessionStore
from api.tasks import TaskRuntime, TaskStatus

from skills.bridge.intention_classifier import IntentionClassifier
from skills.ops.red_phone import alert_iron_dome_violation
from skills.bridge.legal_bridge import execute_skill
from skills.bridge.melchior_bridge import analyze_image
from skills.bridge import melchior_client
from skills.bridge.inference_gateway import InferenceGateway

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 62, exc_info=True)

# Lazy-loaded bridge functions (heavy modules, not needed at startup)
def summarize_text(*args, **kwargs):
    from skills.bridge.balthasar_bridge import summarize_text as _fn
    globals()["summarize_text"] = _fn  # replace self on first call
    return _fn(*args, **kwargs)

def apply_manual_command(*args, **kwargs):
    from skills.bridge.openclaw_codex_bridge import apply_manual_command as _fn
    globals()["apply_manual_command"] = _fn
    return _fn(*args, **kwargs)

def public_status_report(*args, **kwargs):
    from skills.bridge.openclaw_codex_bridge import public_status_report as _fn
    globals()["public_status_report"] = _fn
    return _fn(*args, **kwargs)
# Handler modules — lazy-loaded on first access via _get_handler()
_dh = _tp = _laf = _tr = _sh = None

def _get_handler(name: str):
    """Lazy-import handler modules on first use."""
    global _dh, _tp, _laf, _tr, _sh
    if name == "dh":
        if _dh is None:
            from api.handlers import document_handler as _mod
            _dh = _mod
        return _dh
    elif name == "tp":
        if _tp is None:
            from api.handlers import text_processing_handler as _mod
            _tp = _mod
        return _tp
    elif name == "laf":
        if _laf is None:
            from api.handlers import laf_handler as _mod
            _laf = _mod
        return _laf
    elif name == "tr":
        if _tr is None:
            from api.handlers import translation_handler as _mod
            _tr = _mod
        return _tr
    elif name == "sh":
        if _sh is None:
            from api.handlers import summary_handler as _mod
            _sh = _mod
        return _sh
    raise ValueError(f"Unknown handler: {name}")
# Lazy-loaded web research functions
def search_web(*a, **kw):
    from skills.research.web_research import search_web as _fn
    globals()["search_web"] = _fn
    return _fn(*a, **kw)
def research_topic(*a, **kw):
    from skills.research.web_research import research_topic as _fn
    globals()["research_topic"] = _fn
    return _fn(*a, **kw)
def fetch_url_content(*a, **kw):
    from skills.research.web_research import fetch_url_content as _fn
    globals()["fetch_url_content"] = _fn
    return _fn(*a, **kw)
def fetch_url_sections(*a, **kw):
    from skills.research.web_research import fetch_url_sections as _fn
    globals()["fetch_url_sections"] = _fn
    return _fn(*a, **kw)
# Lazy-loaded brain_manager functions (only used on explicit brain mode commands)
def _lazy_brain(fn_name):
    def _wrapper(*a, **kw):
        import skills.brain_manager.action as _bm
        globals()[fn_name] = getattr(_bm, fn_name)
        return getattr(_bm, fn_name)(*a, **kw)
    return _wrapper
switch_brain_mode = _lazy_brain("switch_brain_mode")
get_brain_status = _lazy_brain("get_brain_status")
get_brain_mode = _lazy_brain("get_brain_mode")
get_melchior_runtime_status = _lazy_brain("get_melchior_runtime_status")
repair_big_brain = _lazy_brain("repair_big_brain")
calibrate_distributed_ngl = _lazy_brain("calibrate_distributed_ngl")
try:
    from api.tw_output_guard import (
        normalize_output_text as _normalize_output_text,
        detect_output_guard_issues as _detect_output_guard_issues,
        mark_non_authoritative_context as _mark_non_authoritative_context,
        mark_unverified_reply as _mark_unverified_reply,
    )
except Exception:
    _normalize_output_text = None
    _detect_output_guard_issues = None
    _mark_non_authoritative_context = None
    _mark_unverified_reply = None

# ---------------------------------------------------------------------------
# Extracted pipeline / domain modules (Phase 1 — incremental delegation)
#
# These modules contain standalone implementations of methods that were
# previously only in the Orchestrator class.  Phase 1 delegates only the
# most isolated, side-effect-free functions.  Remaining methods will be
# migrated incrementally in follow-up PRs.
#
#   api.pipelines.chat_pipeline        - conversation history & chat flow
#   api.pipelines.command_pipeline     - command dispatch helpers
#   api.pipelines.attachment_pipeline  - multimedia / attachment tracking
#   api.domains.market_flow            - stock watchlist & briefing
#   api.domains.judgment_flow          - judgment collection & trend
#   api.domains.skill_interview_flow   - SKILL interview wizard
#   api.domains.laf_flow               - LAF submission workflow
# ---------------------------------------------------------------------------
from api.domains import market_flow as _market_flow
from api.domains import judgment_flow as _judgment_flow
from api.domains import memory_flow as _memory_flow
from api.domains import codex_flow as _codex_flow
from api.domains import skill_interview_flow as _skill_interview_flow
from api.domains import laf_flow as _laf_flow
from api.pipelines import message_router as _message_router
from api.pipelines import skill_dispatch as _skill_dispatch
from api.pipelines import specialized_commands as _spec_cmds
from api.pipelines import chat_pipeline as _chat_pipeline
from api.pipelines import attachment_pipeline as _attachment_pipeline

# Configure Logging
# Use a dedicated "Orchestrator" logger with its own file handler for casper.log.
# Do NOT add handlers to the root logger — server.py owns root logging config.
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
_orch_log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Orchestrator")
if not logger.handlers:
    _orch_file_handler = _RotatingFileHandler(f'{_MAGI_ROOT}/casper.log', maxBytes=5*1024*1024, backupCount=3)
    _orch_file_handler.setFormatter(_orch_log_formatter)
    logger.addHandler(_orch_file_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = True  # Also propagate to root for server.log aggregation

from api.command_registry import CommandRegistry, CommandContext
# Global command registry — commands registered below after class definition
_cmd_registry = CommandRegistry()

# Register Apple-native commands (Spotlight, EventKit, notifications)
try:
    from api.commands.apple_commands import register_apple_commands
    register_apple_commands(_cmd_registry)
except Exception:
    pass  # Apple commands are optional — fail silently on non-macOS

class Orchestrator:
    def __init__(self):
        self.classifier = IntentionClassifier()
        self._inference_gw = InferenceGateway()  # shared instance — avoids per-call re-init
        self._cmd_registry = _cmd_registry
        self._TOPIC_HANDLERS = {}  # topic_key -> handler function (populated by channel plugins)
        self.notification_callback = self._default_notification_callback
        self.user_history = defaultdict(lambda: deque(maxlen=40))
        self._history_summaries: dict = {}  # user_id -> latest rolling summary str
        self._history_summaries_maxsize = 2000
        self._history_summaries_lock = threading.Lock()
        self.profile_fact_cache = set()
        self._profile_fact_cache_maxsize = 10000
        self._chatlog_last_write = {}  # (user_id, platform, role) -> ts
        self._chatlog_last_write_maxsize = 5000
        self._chatlog_last_write_lock = threading.Lock()
        self._rule_last_write = {}  # (user_id, platform) -> ts
        self._rule_last_write_lock = threading.Lock()
        self._forge_locks: dict = {}  # user_id -> threading.Lock for forge concurrency guard
        self._admin_allowlist_cache = {
            "ts": 0.0,
            "line_admin_ids": set(),
        }
        self._agent_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".agent")
        os.makedirs(self._agent_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self._agent_dir, 0o700)
        except OSError:
            pass
        self._runtime_events_file = os.path.join(self._agent_dir, "runtime_events.jsonl")
        self._runtime_events_sink_registered = False
        self._hook_bus = None
        self._task_runtime = None
        self._session_store = None
        self._session_context_builder = None
        self._permission_enforcer = None
        self._tool_registry = None
        self._agent_coordinator = None
        self._runtime_foundations = None
        self._memory_pending_file = os.path.join(self._agent_dir, "memory_pending.json")
        self._skill_interview_pending_file = os.path.join(self._agent_dir, "skill_interview_pending.json")
        self._laf_submit_pending_file = os.path.join(self._agent_dir, "laf_submit_pending.json")
        self._recent_attachment_file = os.path.join(self._agent_dir, "recent_attachments.json")
        self._route_trace_file = os.path.join(self._agent_dir, "route_trace.jsonl")
        self._route_trace_lock = threading.Lock()
        # In-memory caches with dirty-flag deferred flush
        self._state_cache_lock = threading.Lock()
        self._memory_pending_cache = self.__load_json_safe(self._memory_pending_file)
        self._skill_interview_cache = self.__load_json_safe(self._skill_interview_pending_file)
        self._recent_attachments_cache = self.__load_json_safe(self._recent_attachment_file)
        self._state_dirty = set()  # tracks which caches need flushing
        self._flush_timer = None
        from api.thread_pools import inference_pool, io_pool
        self._timeout_pool = inference_pool
        self._bg_task_pool = io_pool
        self._ensure_runtime_foundations()
        # Non-blocking oMLX health check at startup
        try:
            import urllib.request
            try:
                from api.routing.service_registry import get_service_url as _gsurl
                _omlx_base = _gsurl("omlx_inference")
            except Exception:
                _omlx_base = "http://localhost:8080"
            _omlx_url = os.environ.get("OMLX_BASE_URL", _omlx_base) + "/v1/models"
            req = urllib.request.Request(_omlx_url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    logger.info("✅ oMLX reachable at startup")
                else:
                    logger.warning(f"⚠️ oMLX returned status {resp.status} at startup — inference may fail")
        except Exception as _e:
            logger.warning(f"⚠️ oMLX unreachable at startup ({_e}) — inference may fail until oMLX is running")
        # ── Skill Plugin Registry ─────────────────────────────────────
        self._last_dispatch_message = ""
        self._last_dispatch_user_id = ""
        # ── Heavy task tracker: lets chat know when oMLX is occupied ──
        self._heavy_task_lock = threading.Lock()
        self._heavy_tasks: dict[str, dict] = {}  # task_id -> {"label", "user_id", "start_ts"}
        self._heavy_task_done_event = threading.Event()  # signaled when all heavy tasks clear
        try:
            from skills.skill_loader import load_all_skills
            load_all_skills(self)
            from skills.plugin import skill_registry
            self._skill_registry = skill_registry
        except Exception as _sl_err:
            logger.warning("SkillLoader init failed (non-fatal): %s", _sl_err)
            self._skill_registry = None
        logger.info("🎹 Orchestrator Initialized: Ready to conduct.")

    @staticmethod
    def _default_notification_callback(user_id: str, text: str, platform: str, *, topic_key: str = "", **kwargs):
        """Fallback notification: log instead of sending if no real callback is set."""
        logger.warning(f"📨 [Notification lost — no callback] user={user_id} platform={platform} text={text[:120]}")

    @staticmethod
    def _default_permission_rules(root_dir: str, agent_dir: str) -> list:
        return [
            deny_command(
                name="deny-rm-rf",
                commands=("rm -rf", "rm -fr"),
                reason="destructive recursive deletion is blocked",
                priority=1,
            ),
            deny_command(
                name="deny-system-destruction",
                commands=("mkfs", "shutdown", "reboot", "diskutil eraseDisk"),
                reason="destructive system commands are blocked",
                priority=1,
            ),
            deny_path(
                name="deny-agent-state",
                paths=(agent_dir,),
                reason="agent runtime state is not a valid execution target",
                priority=5,
            ),
            deny_path(
                name="deny-env-secrets",
                paths=(os.path.join(root_dir, ".env"), os.path.expanduser("~/.ssh")),
                reason="secret-bearing paths remain blocked",
                priority=5,
            ),
            deny_path(
                name="deny-static-secrets",
                paths=(os.path.join(root_dir, "static", "secrets"),),
                reason="static secret artifacts remain blocked",
                priority=5,
            ),
        ]

    def _build_permission_enforcer(self) -> PermissionEnforcer:
        root_dir = get_magi_root_dir()
        policy = PermissionPolicy.from_rules(
            self._default_permission_rules(root_dir, getattr(self, "_agent_dir", os.path.join(root_dir, ".agent"))),
            mode=PermissionMode.PERMISSIVE,
        )
        return PermissionEnforcer(policy=policy)

    @staticmethod
    def _current_correlation_id() -> str:
        return str(getattr(_orchestrator_tls, "correlation_id", "") or "")

    def _ensure_runtime_foundations(self) -> None:
        if not hasattr(self, "_task_runtime") or self._task_runtime is None:
            self._task_runtime = TaskRuntime()
        if not hasattr(self, "_session_store") or self._session_store is None:
            self._session_store = SessionStore()
        if not hasattr(self, "_session_context_builder") or self._session_context_builder is None:
            self._session_context_builder = SessionContextBuilder(self._session_store)
        if not hasattr(self, "_permission_enforcer") or self._permission_enforcer is None:
            self._permission_enforcer = self._build_permission_enforcer()
        if not hasattr(self, "_hook_bus") or self._hook_bus is None:
            self._hook_bus = HookBus(source="magi.orchestrator")
        if not hasattr(self, "_tool_registry") or self._tool_registry is None:
            try:
                from api.tools import get_global_tool_registry

                self._tool_registry = get_global_tool_registry()
            except Exception:
                self._tool_registry = None
        if not hasattr(self, "_agent_coordinator") or self._agent_coordinator is None:
            try:
                from api.coordinator import AgentCoordinator

                self._agent_coordinator = AgentCoordinator(name="magi")
            except Exception:
                self._agent_coordinator = None
        try:
            self._runtime_foundations = RuntimeFoundations(
                task_runtime=self._task_runtime,
                session_store=self._session_store,
                session_context_builder=self._session_context_builder,
                permission_enforcer=self._permission_enforcer,
                hook_bus=self._hook_bus,
                tool_registry=self._tool_registry,
                agent_coordinator=self._agent_coordinator,
            )
        except Exception:
            self._runtime_foundations = None
        if (
            getattr(self, "_runtime_events_file", "")
            and not getattr(self, "_runtime_events_sink_registered", False)
        ):
            try:
                self._hook_bus.add_jsonl_sink(self._runtime_events_file)
                self._runtime_events_sink_registered = True
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 295, exc_info=True)

    def _emit_task_lifecycle(
        self,
        task_id: str,
        task_name: str,
        status: str,
        *,
        progress: Optional[float] = None,
        user_id: str = "",
        detail: Optional[dict] = None,
    ) -> None:
        self._ensure_runtime_foundations()
        try:
            self._hook_bus.emitter.emit(
                TaskLifecycleEvent(
                    task_id=str(task_id or ""),
                    task_name=str(task_name or ""),
                    status=str(status or ""),
                    progress=progress,
                    user_id=str(user_id or ""),
                    detail=dict(detail or {}),
                    source="magi.orchestrator",
                    correlation_id=self._current_correlation_id(),
                )
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 319, exc_info=True)

    # ── Heavy task tracking ──────────────────────────────────────────
    def _ensure_heavy_task_primitives(self) -> None:
        """Lazily initialize heavy-task primitives for partial/test instances."""
        if not hasattr(self, "_heavy_task_lock") or self._heavy_task_lock is None:
            self._heavy_task_lock = threading.Lock()
        if not hasattr(self, "_heavy_tasks") or self._heavy_tasks is None:
            self._heavy_tasks = {}
        if not hasattr(self, "_heavy_task_done_event") or self._heavy_task_done_event is None:
            self._heavy_task_done_event = threading.Event()

    def register_heavy_task(self, task_id: str, label: str, user_id: str = "") -> None:
        """Register a heavy LLM task (translation, summary, transcription) so chat can detect it."""
        self._ensure_heavy_task_primitives()
        self._ensure_runtime_foundations()
        with self._heavy_task_lock:
            self._heavy_tasks[task_id] = {
                "label": label,
                "user_id": user_id,
                "start_ts": time.time(),
            }
        try:
            self._task_runtime.register(
                task_id,
                label,
                description=f"heavy task: {label}",
                metadata={"kind": "heavy", "user_id": str(user_id or "")},
            )
            self._task_runtime.update(
                task_id,
                status=TaskStatus.RUNNING,
                progress=0.0,
                metadata={"kind": "heavy", "user_id": str(user_id or ""), "label": str(label or "")},
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 335, exc_info=True)
        self._emit_task_lifecycle(
            task_id,
            label,
            TaskStatus.RUNNING.value,
            progress=0.0,
            user_id=user_id,
            detail={"kind": "heavy"},
        )
        logger.info(f"🏋️ Heavy task started: {label} (id={task_id})")

    def unregister_heavy_task(self, task_id: str) -> None:
        """Remove a completed heavy task. Signals waiting chat handlers if all tasks cleared."""
        self._ensure_heavy_task_primitives()
        self._ensure_runtime_foundations()
        with self._heavy_task_lock:
            removed = self._heavy_tasks.pop(task_id, None)
            all_clear = len(self._heavy_tasks) == 0
        if removed:
            elapsed = time.time() - removed.get("start_ts", 0)
            try:
                self._task_runtime.complete(
                    task_id,
                    result={"elapsed_sec": round(elapsed, 3)},
                    metadata={"kind": "heavy", "user_id": str(removed.get("user_id") or "")},
                    progress=1.0,
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 356, exc_info=True)
            self._emit_task_lifecycle(
                task_id,
                str(removed.get("label") or ""),
                TaskStatus.COMPLETED.value,
                progress=1.0,
                user_id=str(removed.get("user_id") or ""),
                detail={"elapsed_sec": round(elapsed, 3), "kind": "heavy"},
            )
            logger.info(f"🏋️ Heavy task done: {removed['label']} ({elapsed:.0f}s)")
        if all_clear:
            self._heavy_task_done_event.set()

    def get_active_heavy_tasks(self) -> list[dict]:
        """Return list of currently running heavy tasks."""
        self._ensure_heavy_task_primitives()
        self._ensure_runtime_foundations()
        with self._heavy_task_lock:
            now = time.time()
            # Auto-expire tasks older than 30 minutes (safety net)
            expired = [k for k, v in self._heavy_tasks.items() if now - v.get("start_ts", 0) > 1800]
            for k in expired:
                removed = self._heavy_tasks.pop(k, None)
                if removed:
                    try:
                        self._task_runtime.cancel(
                            k,
                            reason="expired safety net",
                            metadata={"kind": "heavy", "user_id": str(removed.get("user_id") or "")},
                        )
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 377, exc_info=True)
                    self._emit_task_lifecycle(
                        k,
                        str(removed.get("label") or ""),
                        TaskStatus.CANCELLED.value,
                        user_id=str(removed.get("user_id") or ""),
                        detail={"reason": "expired safety net", "kind": "heavy"},
                    )
            return list(self._heavy_tasks.values())
    # ─────────────────────────────────────────────────────────────────

    def _append_route_trace(self, user_id: str, platform: str, stage: str, route: str, detail: Optional[dict] = None) -> None:
        self._ensure_runtime_foundations()
        payload = {
            "ts": time.time(),
            "user_id": str(user_id or ""),
            "platform": str(platform or ""),
            "stage": str(stage or ""),
            "route": str(route or ""),
        }
        # Attach correlation_id from thread-local if available.
        _cid = getattr(_orchestrator_tls, "correlation_id", None)
        if _cid:
            payload["correlation_id"] = _cid
        if isinstance(detail, dict):
            for key, value in detail.items():
                if value is None:
                    continue
                payload[str(key)] = value
        try:
            with self._route_trace_lock:
                with open(self._route_trace_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                # Auto-prune: keep last 50K lines (~5MB) when file exceeds 10MB
                try:
                    if os.path.getsize(self._route_trace_file) > 10 * 1024 * 1024:
                        # Stream line count + tail without loading entire file into memory
                        import collections as _col
                        tail_buf = _col.deque(maxlen=50000)
                        with open(self._route_trace_file, "r", encoding="utf-8") as f:
                            for line in f:
                                tail_buf.append(line)
                        import tempfile as _tf
                        fd, tmp = _tf.mkstemp(dir=self._agent_dir, suffix=".tmp")
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            f.writelines(tail_buf)
                        os.replace(tmp, self._route_trace_file)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 310, exc_info=True)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 312, exc_info=True)
        try:
            route_detail = dict(detail or {})
            confidence = route_detail.get("confidence")
            try:
                confidence_value = float(confidence) if confidence is not None else 0.0
            except Exception:
                confidence_value = 0.0
            self._hook_bus.route_decision(
                str(route or ""),
                confidence=confidence_value,
                reason=str(route_detail.get("reason") or stage or ""),
                message=str(route_detail.get("message") or ""),
                candidates=list(route_detail.get("candidates") or []),
                correlation_id=self._current_correlation_id(),
                metadata={
                    "stage": str(stage or ""),
                    "platform": str(platform or ""),
                    "user_id": str(user_id or ""),
                    **route_detail,
                },
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 334, exc_info=True)

    def _sanitize_incoming_message(self, message: str) -> str:
        return _get_handler("tp").sanitize_incoming_message(message)

    def _read_openclaw_primary_model(self) -> str:
        return _message_router.read_openclaw_primary_model()

    def _handle_gibberish_report(self, user_id, message: str, platform: str = "") -> Optional[str]:
        return _message_router.handle_gibberish_report(self, user_id, message, platform)

    def _quick_fixed_reply(self, message: str, role: str = "user") -> Optional[str]:
        return _message_router.quick_fixed_reply(self, message, role)

    def _brain_runtime_banner(self) -> str:
        return _message_router.brain_runtime_banner()

    def _call_with_timeout(self, fn, timeout_sec: int, fallback_text: str, tag: str) -> str:
        fut = self._timeout_pool.submit(fn)
        try:
            return fut.result(timeout=timeout_sec)
        except FuturesTimeoutError:
            fut.cancel()
            logger.warning(f"⏱️ {tag} timeout ({timeout_sec}s)")
            return fallback_text
        except Exception as e:
            logger.warning(f"{tag} failed: {e}")
            return fallback_text

    def _nl_router_enabled(self) -> bool:
        return _message_router.nl_router_enabled()

    # Pre-compiled keyword sets for _should_try_nl_route (avoid per-message iteration)
    _NL_STOCK_PHRASES = frozenset({
        "追蹤股票", "追蹤清單", "新增追蹤", "增加追蹤", "移除追蹤", "設定追蹤",
        "股市晨報", "股市預測", "股票預測", "技術分析", "台股", "美股",
        "macd", "rsi", "布林通道",
    })
    _NL_STOCK_PHRASES_LOWER = frozenset(k.lower().replace(" ", "") for k in _NL_STOCK_PHRASES)
    _NL_ROUTE_KWS = [
        "自動巡檢", "夜間任務", "檢查閱卷", "閱卷信箱", "可下載判定", "閱卷下載", "同步筆錄", "筆錄下載",
        "掃描案件", "待辦佇列", "日曆同步", "gcal", "法扶未開辦", "未開辦掃描", "laf_pending",
        "開辦", "疑義", "撤回", "訴訟中費用", "二階段", "調解不成立", "結案", "報結", "法扶",
        "加班費", "勞基法", "勞動基準法", "特休假", "資遣費", "一例一休",
        "新增爬蟲", "移除爬蟲", "爬蟲清單", "每日爬蟲",
        "找判決", "判決搜尋", "法規搜尋", "法規向量更新",
        "大腦模式", "big brain", "分散式推理", "開啟大腦", "關閉大腦", "修理大腦", "修理melchior", "校準ngl", "ngl",
        "你現在使用模型", "目前模型", "模型為何", "模型是什麼",
        "除錯", "排查", "診斷", "健康檢查", "自動修復", "穩定度", "成功率", "降級策略", "slo",
        "鐵穹", "iron dome", "iron",
        "skills check", "技能狀態", "系統狀態",
        "請報告你現在的功能", "目前功能與缺失", "還有什麼缺失", "功能審計", "完整稽核",
        "備份資料庫", "資料庫備份", "備份db", "還原資料庫", "資料庫還原", "restore db",
    ]
    _NL_ROUTE_KWS_LOWER = [k.lower() for k in _NL_ROUTE_KWS]

    def _should_try_nl_route(self, message: str) -> bool:
        return _message_router.should_try_nl_route(self, message)

    def _load_market_watch_state(self) -> dict:
        return _market_flow.load_market_watch_state(self)

    @staticmethod
    def _is_stock_like_token(token: str) -> bool:
        return _market_flow.is_stock_like_token(token)

    def _looks_like_market_watchlist_reply(self, message: str) -> bool:
        return _market_flow.looks_like_market_watchlist_reply(message)

    def _try_market_watchlist_quick_set(self, message: str, platform: str = "") -> tuple[bool, str]:
        return _market_flow.try_market_watchlist_quick_set(self, message, platform=platform)

    def _extract_judgment_collect_payload(self, message: str) -> tuple[Optional[dict], str]:
        return _judgment_flow.extract_judgment_collect_payload(message)

    def _format_judgment_collect_result(self, payload: dict) -> str:
        return _judgment_flow.format_judgment_collect_result(payload)

    def _run_judgment_collector_command(self, message: str, notify: bool = False) -> str:
        return _judgment_flow.run_judgment_collector_command(self, message, notify=notify)

    def _run_judgment_trend_command(self, message: str) -> str:
        return _judgment_flow.run_judgment_trend_command(self, message)

    def _strip_intent_prefixes(self, text: str, patterns: list[str]) -> str:
        return _get_handler("tp").strip_intent_prefixes(text, patterns)

    def _run_labor_law_command(self, message: str) -> str:
        return _spec_cmds.run_labor_law_command(self, message)


    def _run_inline_translation_command(self, user_id, message: str) -> str:
        return _spec_cmds.run_inline_translation_command(self, user_id, message)


    def _run_inline_summary_command(self, message: str) -> str:
        return _spec_cmds.run_inline_summary_command(self, message)


    def _run_stock_briefing_command(self, message: str) -> str:
        return _market_flow.run_stock_briefing_command(self, message)

    def _run_court_hearing_command(self, message: str) -> str:
        return _spec_cmds.run_court_hearing_command(self, message)


    def _run_embedding_web_search(self, message: str) -> str:
        return _spec_cmds.run_embedding_web_search(self, message)


    def _summarize_web_results(self, topic: str, result: dict) -> str:
        return _spec_cmds.summarize_web_results(topic, result)

    def _run_transcribe_guidance(self, message: str) -> str:
        return _skill_dispatch.run_transcribe_guidance(message)

    def _looks_like_capability_question(self, message: str) -> bool:
        return _skill_dispatch.looks_like_capability_question(message)


    def _dispatch_safe_semantic_skill(self, user_id, message: str, skill: str, role: str, platform: str) -> tuple[bool, str]:
        return _skill_dispatch.dispatch_safe_semantic_skill(self, user_id, message, skill, role, platform)


    def _generic_skill_dispatch(self, skill: str, message: str) -> tuple[bool, str]:
        return _skill_dispatch.generic_skill_dispatch(self, skill, message)


    def _polish_skill_output(self, skill: str, user_message: str, raw_output: str) -> str:
        return _skill_dispatch.polish_skill_output(skill, user_message, raw_output)

    @staticmethod
    def _output_looks_messy(text: str) -> bool:
        return _skill_dispatch.output_looks_messy(text)


    @staticmethod
    def _basic_cleanup(text: str) -> str:
        return _skill_dispatch.basic_cleanup(text)


    def _try_safe_semantic_skill_route(self, user_id: str, message: str, role: str, platform: str) -> tuple[bool, str]:
        return _skill_dispatch.try_safe_semantic_skill_route(self, user_id, message, role, platform)


    def _run_nl_route(self, user_id: str, message: str, platform: str, role: str) -> tuple[bool, str]:
        return _message_router.run_nl_route(self, user_id, message, platform, role)

    def _redact_secrets(self, text: str) -> str:
        return _get_handler("tp").redact_secrets(text)

    def _apply_long_dialog_guard(self, text: str, platform: str = "") -> str:
        return _get_handler("tp").apply_long_dialog_guard(text, platform)

    def _postprocess_router_reply(self, text: str, platform: str = "") -> str:
        return _get_handler("tp").postprocess_router_reply(text, platform)

    def _output_guard_issues(self, text: str, mode: str = "general") -> list[str]:
        return _get_handler("tp").output_guard_issues(text, mode)

    def _normalize_txt_body(self, text: str) -> str:
        return _get_handler("dh").normalize_txt_body(text)

    def _prepare_document_text_for_llm(self, text: str) -> str:
        return _get_handler("dh").prepare_document_text_for_llm(text)

    def _polish_translated_document_text(self, text: str) -> str:
        return _get_handler("dh").polish_translated_document_text(text)

    def _build_translation_txt(self, translated_text: str, source: str, provider: str, mode: str) -> str:
        return _get_handler("dh").build_translation_txt(translated_text, source, provider, mode)

    def _is_file_protocol_user(self, user_id: str) -> bool:
        return _get_handler("dh").is_file_protocol_user(user_id)

    def _export_translation_txt(self, *, translated_text: str, source: str, provider: str, mode: str, prefix: str, user_id: str) -> Optional[str]:
        return _get_handler("dh").export_translation_txt(translated_text=translated_text, source=source, provider=provider, mode=mode, prefix=prefix, user_id=user_id)

    def _export_translation_docx(self, *, source_text: str, translated_text: str, source_chunks: Optional[list] = None, translated_chunks: Optional[list] = None, term_glossary: str = "", title: str = "", subtitle: str = "", prefix: str = "translate", user_id: str) -> Optional[str]:
        return _get_handler("dh").export_translation_docx(source_text=source_text, translated_text=translated_text, source_chunks=source_chunks, translated_chunks=translated_chunks, term_glossary=term_glossary, title=title, subtitle=subtitle, prefix=prefix, user_id=user_id)

    def _export_plain_txt(self, *, content: str, prefix: str, user_id: str, title: str = "📄 已輸出 TXT 檔案。") -> Optional[str]:
        return _get_handler("dh").export_plain_txt(content=content, prefix=prefix, user_id=user_id, title=title)

    def _export_plain_docx(self, *, segments: list, mode: str = "transcript", title: str = "", case_info: str = "", prefix: str = "export", user_id: str) -> Optional[str]:
        return _get_handler("dh").export_plain_docx(segments=segments, mode=mode, title=title, case_info=case_info, prefix=prefix, user_id=user_id)

    def _export_summary_docx_or_txt(self, summary_text: str, *, prefix: str, title: str, user_id: str, source_path: str = "") -> Optional[str]:
        from api.domains.export_flow import export_summary_docx_or_txt
        return export_summary_docx_or_txt(self, summary_text, prefix=prefix, title=title, user_id=user_id, source_path=source_path)

    @staticmethod
    def estimate_file_processing_time(file_size_bytes: int, filename: str = "", prompt: str = "", file_path: str = "") -> str:
        return _get_handler("dh").estimate_file_processing_time(file_size_bytes, filename, prompt, file_path)

    def _extract_text_from_uploaded_file(self, path: str, filename: str = "") -> dict:
        return _get_handler("dh").extract_text_from_uploaded_file(path, filename)

    def _ingest_uploaded_text(self, *, kind: str, primary: str, title: str, text: str) -> dict:
        return _get_handler("dh").ingest_uploaded_text(kind=kind, primary=primary, title=title, text=text)

    def _ingest_uploaded_text_async(self, *, kind: str, primary: str, title: str, text: str) -> bool:
        body = str(text or "").strip()
        if not body:
            return False

        def _run() -> None:
            try:
                res = self._ingest_uploaded_text(kind=kind, primary=primary, title=title, text=body)
                if res.get("success"):
                    logger.info(
                        "🧠 Async ingest complete: title=%s doc_key=%s chunks=%s",
                        title,
                        str(res.get("doc_key") or "").strip(),
                        int(res.get("chunks_written") or 0),
                    )
                else:
                    logger.warning(
                        "⚠️ Async ingest failed: title=%s error=%s",
                        title,
                        str(res.get("error") or "unknown")[:240],
                    )
            except Exception as e:
                logger.warning(f"⚠️ Async ingest exception for {title}: {e}")

        self._bg_task_pool.submit(_run)
        return True

    def _cap_translation_source_text(self, text: str) -> tuple[str, bool]:
        return _get_handler("dh").cap_translation_source_text(text)

    def _detect_summary_target_pref(self, prompt: str) -> str:
        return _get_handler("dh").detect_summary_target_pref(prompt)

    def _split_translate_chunks(self, text: str) -> list[str]:
        return _get_handler("dh").split_translate_chunks(text)

    def _translate_text_complete(self, text: str, source_lang: str = "auto", target_lang: str = "繁體中文", heavy: bool = False) -> dict:
        task_id = f"translate_{id(text)}_{time.time():.0f}"
        self.register_heavy_task(task_id, "翻譯")
        try:
            if not heavy:
                try:
                    from flask import g as _flask_g
                    heavy = bool(getattr(_flask_g, "heavy_opt_in", False))
                except Exception:
                    heavy = False
            return _get_handler("tr").translate_text_complete(text, source_lang=source_lang, target_lang=target_lang, heavy=heavy)
        finally:
            self.unregister_heavy_task(task_id)

    @staticmethod
    def _detect_summary_length(message: str) -> str:
        return _get_handler("dh").detect_summary_length(message)

    @staticmethod
    def _summary_length_prompt(length: str) -> tuple[str, str]:
        return _get_handler("sh").summary_length_prompt(length)

    def _summarize_text_resilient(self, text: str, summary_length: str = "medium", *, progress_callback=None) -> dict:
        task_id = f"summary_{id(text)}_{time.time():.0f}"
        self.register_heavy_task(task_id, "摘要")
        try:
            return _get_handler("sh").summarize_text_resilient(text, summary_length=summary_length, progress_callback=progress_callback)
        finally:
            self.unregister_heavy_task(task_id)

    @staticmethod
    def __load_json_safe(path: str) -> dict:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                return data if isinstance(data, dict) else {}
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1851, exc_info=True)
        return {}

    def _schedule_state_flush(self) -> None:
        """Schedule a deferred flush (5s) to batch multiple writes."""
        if self._flush_timer and self._flush_timer.is_alive():
            return  # already scheduled
        self._flush_timer = threading.Timer(5.0, self._flush_dirty_state)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _flush_dirty_state(self) -> None:
        """Write all dirty caches to disk atomically."""
        with self._state_cache_lock:
            dirty = set(self._state_dirty)
            self._state_dirty.clear()
        for name in dirty:
            try:
                if name == "memory_pending":
                    self.__save_json_atomic(self._memory_pending_file, self._memory_pending_cache)
                elif name == "skill_interview":
                    self.__save_json_atomic(self._skill_interview_pending_file, self._skill_interview_cache)
                elif name == "recent_attachments":
                    self.__save_json_atomic(self._recent_attachment_file, self._recent_attachments_cache)
            except Exception as e:
                logger.warning("State flush failed for %s: %s", name, e)

    @staticmethod
    def __save_json_atomic(path: str, data: dict) -> None:
        import tempfile as _tf
        d = data if isinstance(data, dict) else {}
        fd, tmp = _tf.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _load_memory_pending(self) -> dict:
        with self._state_cache_lock:
            return dict(self._memory_pending_cache)

    def _save_memory_pending(self, data: dict) -> None:
        with self._state_cache_lock:
            self._memory_pending_cache = data if isinstance(data, dict) else {}
            self._state_dirty.add("memory_pending")
        self._schedule_state_flush()

    def _load_skill_interview_pending(self) -> dict:
        with self._state_cache_lock:
            return dict(self._skill_interview_cache)

    def _save_skill_interview_pending(self, data: dict) -> None:
        with self._state_cache_lock:
            self._skill_interview_cache = data if isinstance(data, dict) else {}
            self._state_dirty.add("skill_interview")
        self._schedule_state_flush()

    def _pending_key(self, user_id: str, platform: str) -> str:
        return f"{str(platform or '').strip()}::{str(user_id or '').strip()}"

    @staticmethod
    def _skill_interview_default_reply(message: str) -> bool:
        return _skill_interview_flow.skill_interview_default_reply(message)


    @staticmethod
    def _skill_interview_cancel_reply(message: str) -> bool:
        return _skill_interview_flow.skill_interview_cancel_reply(message)


    @staticmethod
    def _skill_interview_status_reply(message: str) -> bool:
        return _skill_interview_flow.skill_interview_status_reply(message)


    @staticmethod
    def _skill_interview_split_items(text: str, limit: int = 8) -> list[str]:
        return _skill_interview_flow.skill_interview_split_items(text, limit)


    def _parse_skill_interview_io(self, message: str) -> tuple[list[str], list[str]]:
        return _skill_interview_flow.parse_skill_interview_io(message)

    def _format_skill_interview_progress(self, entry: dict) -> str:
        return _skill_interview_flow.format_skill_interview_progress(entry)


    def _render_skill_interview_question(self, entry: dict) -> str:
        return _skill_interview_flow.render_skill_interview_question(entry)


    def _start_skill_interview(self, user_id: str, platform: str, role: str, initial_request: str, trigger_reason: str = "manual") -> str:
        return _skill_interview_flow.start_skill_interview(self, user_id, platform, role, initial_request, trigger_reason)


    def start_skill_interview(self, user_id: str, platform: str, role: str, initial_request: str, trigger_reason: str = "manual") -> str:
        return self._start_skill_interview(user_id, platform, role, initial_request, trigger_reason=trigger_reason)

    def _finalize_skill_interview(self, user_id: str, platform: str, entry: dict) -> str:
        return _skill_interview_flow.finalize_skill_interview(self, user_id, platform, entry)


    def _handle_skill_interview_if_any(self, user_id: str, platform: str, role: str, message: str) -> tuple[bool, str]:
        return _skill_interview_flow.handle_skill_interview_if_any(self, user_id, platform, role, message)


    def reply_skill_interview(self, user_id: str, platform: str, role: str, message: str) -> tuple[bool, str]:
        return self._handle_skill_interview_if_any(user_id, platform, role, message)

    def get_skill_interview_state(self, user_id: str, platform: str) -> dict:
        return _skill_interview_flow.get_skill_interview_state(self, user_id, platform)


    def _load_recent_attachments(self) -> dict:
        return _attachment_pipeline.load_recent_attachments(self)


    def _save_recent_attachments(self, data: dict) -> None:
        return _attachment_pipeline.save_recent_attachments(self, data)


    def _prune_recent_attachments(self, data: dict) -> dict:
        return _attachment_pipeline.prune_recent_attachments(data)


    def remember_recent_attachment(self, *, user_id: str, platform: str, attachment: dict, source_message: str = "") -> dict:
        return _attachment_pipeline.remember_recent_attachment(self, user_id=user_id, platform=platform, attachment=attachment, source_message=source_message)


    def _get_recent_attachment(self, user_id: str, platform: str) -> dict:
        return _attachment_pipeline.get_recent_attachment(self, user_id, platform)


    def _looks_like_attachment_followup(self, message: str, attachment_type: str = "") -> bool:
        return _attachment_pipeline.looks_like_attachment_followup(message, attachment_type)


    def has_recent_attachment_followup(self, user_id: str, platform: str, message: str) -> bool:
        return _attachment_pipeline.has_recent_attachment_followup(self, user_id, platform, message)


    def _maybe_reuse_recent_attachment(self, user_id: str, platform: str, message: str) -> Optional[dict]:
        return _attachment_pipeline.maybe_reuse_recent_attachment(self, user_id, platform, message)


    def _load_laf_submit_pending(self) -> dict:
        return _laf_flow.load_laf_submit_pending(self)


    def _save_laf_submit_pending(self, data: dict) -> None:
        return _laf_flow.save_laf_submit_pending(self, data)


    def _update_laf_status_after_action(self, *, case_number: str = "", client_name: str = "",
                                           laf_case_no: str = "",
                                           case_reason_hint: str = "",
                                           new_status: str, action_label: str = "") -> bool:
        return _laf_flow.update_laf_status_after_action(self, case_number=case_number, client_name=client_name, laf_case_no=laf_case_no, case_reason_hint=case_reason_hint, new_status=new_status, action_label=action_label)


    def _register_laf_go_live_submit_pending(self, *, platform: str, requester_user_id: str, payload: dict, result_data: dict) -> dict:
        return _laf_flow.register_laf_go_live_submit_pending(self, platform=platform, requester_user_id=requester_user_id, payload=payload, result_data=result_data)


    def _resolve_laf_go_live_pending_token(self, platform: str, message: str) -> tuple[str, dict]:
        return _laf_flow.resolve_laf_go_live_pending_token(self, platform, message)


    def _handle_laf_submit_confirmation_if_any(self, user_id: str, platform: str, role: str, message: str) -> tuple[bool, str]:
        return _laf_flow.handle_laf_submit_confirmation_if_any(self, user_id, platform, role, message)

    # ── Plan C: progress submit pending wrappers ────────────────────────────

    def _register_laf_progress_submit_pending(self, *, platform: str, requester_user_id: str, payload: dict, result_data: dict) -> str:
        """進度回報兩階段確認碼：登記 pending，回傳 6-hex token。"""
        return _laf_flow.register_laf_progress_submit_pending(
            self,
            platform=platform,
            requester_user_id=requester_user_id,
            payload=payload,
            result_data=result_data,
        )

    def _resolve_laf_progress_pending_token(self, token: str):
        """進度回報兩階段確認碼：驗證 token，回傳 entry dict 或 None。"""
        return _laf_flow.resolve_laf_progress_pending_token(self, token)


    def _is_ambiguous_rule(self, text: str) -> bool:
        return _memory_flow.is_ambiguous_rule(text)


    def _handle_memory_confirmation_if_any(self, user_id: str, platform: str, message: str) -> tuple[bool, str]:
        return _memory_flow.handle_memory_confirmation_if_any(self, user_id, platform, message)


    def _maybe_capture_user_rules(self, user_id: str, platform: str, message: str):
        return _memory_flow.maybe_capture_user_rules(self, user_id, platform, message)


    def _maybe_capture_chatlog(self, user_id: str, platform: str, role: str, content: str):
        return _memory_flow.maybe_capture_chatlog(self, user_id, platform, role, content)


    def _is_verified_admin_sender(self, user_id: str, platform: str) -> bool:
        """
        Only treat a sender as admin if explicitly allowlisted.
        This is stronger than trusting upstream role flags.
        """
        uid = str(user_id or "").strip()
        plat = str(platform or "").strip().lower()

        internal = {
            x.strip()
            for x in os.environ.get("MAGI_INTERNAL_ADMIN_IDS", "SYSTEM,SYSTEM_CRON").split(",")
            if x.strip()
        }
        if uid in internal:
            return True

        if plat.startswith("line"):
            allowed = {
                x.strip()
                for x in os.environ.get("MAGI_ADMIN_LINE_IDS", "").split(",")
                if x.strip()
            }
            # Allow file-based allowlist (more resilient than env across restarts).
            try:
                from api.admin_allowlist import get_line_admin_user_ids  # type: ignore
                allowed |= set(get_line_admin_user_ids() or set())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2773, exc_info=True)
            allowed |= self._get_admin_line_ids_from_brain_sqlite()
            return uid in allowed

        if plat.startswith("discord"):
            did = uid.replace("discord_", "").strip()
            allowed = {x.strip() for x in os.environ.get("DISCORD_ADMIN_IDS", "").split(",") if x.strip()}
            # Allow file-based allowlist (more resilient than env across restarts).
            try:
                from api.admin_allowlist import get_discord_admin_ids  # type: ignore
                allowed |= set(get_discord_admin_ids() or set())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2785, exc_info=True)
            return did in allowed

        if plat.startswith("telegram"):
            tid = uid.replace("telegram_", "").strip()
            allowed = {x.strip() for x in os.environ.get("MAGI_ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
            try:
                from api.admin_allowlist import get_telegram_admin_ids  # type: ignore
                allowed |= set(get_telegram_admin_ids() or set())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2795, exc_info=True)
            return tid in allowed

        if plat.upper() == "WEB":
            # Trust server.py authentication
            return True

        return False

    def _get_admin_line_ids_from_brain_sqlite(self) -> set:
        """
        Secondary admin allowlist source:
        - MAGI Brain SQLite: users(line_user_id, role)
        This makes admin verification resilient when env is missing/misaligned.
        """
        try:
            ttl = float(os.environ.get("MAGI_ADMIN_DB_CACHE_TTL_SEC", "30"))
        except Exception:
            ttl = 30.0

        now = time.time()
        cache_ts = float(self._admin_allowlist_cache.get("ts", 0.0) or 0.0)
        if now - cache_ts < ttl and self._admin_allowlist_cache.get("line_admin_ids"):
            return set(self._admin_allowlist_cache["line_admin_ids"])

        db_path = os.environ.get("MAGI_BRAIN_SQLITE_PATH", f"{_MAGI_ROOT}/magi_brain.db")
        ids: set[str] = set()
        try:
            if not os.path.exists(db_path):
                self._admin_allowlist_cache["ts"] = now
                self._admin_allowlist_cache["line_admin_ids"] = set()
                return set()
            conn = sqlite3.connect(db_path, timeout=5)
            try:
                cur = conn.cursor()
                cur.execute("SELECT line_user_id FROM users WHERE role = 'admin' AND line_user_id IS NOT NULL")
                for (line_user_id,) in cur.fetchall() or []:
                    s = str(line_user_id or "").strip()
                    if s:
                        ids.add(s)
            finally:
                conn.close()
        except Exception:
            # Don't block routing if the DB is locked/unavailable.
            ids = set()

        self._admin_allowlist_cache["ts"] = now
        self._admin_allowlist_cache["line_admin_ids"] = set(ids)
        return set(ids)

    def _parse_codex_distributed_features(self, message: str) -> dict:
        return _codex_flow.parse_codex_distributed_features(message)


    def _format_codex_distributed_status(self, report: dict) -> str:
        return _codex_flow.format_codex_distributed_status(report)


    def _handle_codex_distributed_command(self, message: str, role: str):
        return _codex_flow.handle_codex_distributed_command(self, message, role)


    def _explain_routing(self, message: str, role: str = "user") -> dict:
        return _message_router.explain_routing(self, message, role)


    # ════════════════════════════════════════════════════════════════
    def _topic_fast_path(self, topic_key: str, user_id, message: str, role: str, platform: str, attachment=None):
        return _message_router.topic_fast_path(self, topic_key, user_id, message, role, platform, attachment)

    def _try_conversational_intent(self, message: str, msg_lower: str, user_id, role: str, platform: str):
        return _message_router.try_conversational_intent(self, message, msg_lower, user_id, role, platform)


    def _extract_route_probe(self, message: str) -> tuple[bool, str, str]:
        return _message_router.extract_route_probe(message)


    def _format_route_explain(self, info: dict, role: str = "user") -> str:
        return _message_router.format_route_explain(info, role)


    def register_callback(self, callback_func):
        """
        Registers a callback function to send asynchronous notifications.
        Format: callback(user_id, message, platform="LINE")
        """
        self.notification_callback = callback_func
        logger.info("🔔 Notification Callback Registered.")

    # --- Context compression thresholds ---
    _HISTORY_COMPRESS_AT = 30   # trigger compression when deque reaches this size
    _HISTORY_COMPRESS_KEEP = 10 # keep this many recent messages after compression
    _HISTORY_COMPRESS_TIMEOUT = 15  # seconds for summary LLM call
    _HISTORY_TOKEN_BUDGET = 2400   # max estimated tokens for conversation history sent to LLM
    _SUMMARY_MAX_TOKENS = 300      # max tokens for rolling summary

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return _chat_pipeline.estimate_tokens(text)


    def _append_history(self, user_id, role, content):
        return _chat_pipeline.append_history(self, user_id, role, content)


    def _compress_history(self, user_id):
        return _chat_pipeline.compress_history(self, user_id)


    def record_assistant_reply(self, user_id, content):
        return _chat_pipeline.record_assistant_reply(self, user_id, content)


    def _build_conversation_history(self, user_id, limit=12):
        return _chat_pipeline.build_conversation_history(self, user_id, limit)


    def _maybe_capture_profile_fact(self, user_id, message):
        return _chat_pipeline.maybe_capture_profile_fact(self, user_id, message) if hasattr(_chat_pipeline, "maybe_capture_profile_fact") else None


    def process_message(self, user_id, message, platform="LINE", role="user", attachment=None, correlation_id: Optional[str] = None, progress_callback=None, channel_context=None):
        """
        Main Event Loop for processing a single message.

        Args:
            progress_callback: Optional callable(phase, current, total, message) for long tasks.
            channel_context: Optional ChannelContext (or dict) with topic_key, channel_id, thread_id.
        """
        # Store correlation_id in thread-local so _append_route_trace picks it up.
        _orchestrator_tls.correlation_id = correlation_id or None
        self._progress_callback = progress_callback

        # 使用者活動信標 — 夜間任務會據此自動延後
        try:
            from skills.ops.user_activity_beacon import touch as _beacon_touch
            _beacon_touch(user_id, platform)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3928, exc_info=True)

        try:
            return self._process_message_inner(user_id, message, platform, role, attachment, correlation_id, progress_callback, channel_context=channel_context)
        except Exception as _fatal:
            try:
                from skills.management.issue_tracker import log_issue

                log_issue(
                    command=str(message)[:200] if isinstance(message, str) else repr(message)[:200],
                    error_msg=f"{type(_fatal).__name__}: {_fatal}",
                    context=f"user_id={user_id} platform={platform}",
                    severity="High",
                    source="orchestrator.process_message",
                )
            except Exception:
                pass
            logger.error(f"❌ Unhandled exception in process_message: {_fatal}", exc_info=True)
            self._append_route_trace(str(user_id or ""), str(platform or ""), "fatal_error", "unhandled", {"error": str(_fatal)[:200]})
            return "❌ 系統暫時忙碌，請稍後再試。"

    def _process_message_inner(self, user_id, message, platform="LINE", role="user", attachment=None, correlation_id=None, progress_callback=None, channel_context=None):
        from api.pipelines.message_pipeline import process_message_inner
        return process_message_inner(self, user_id, message, platform=platform, role=role, attachment=attachment, correlation_id=correlation_id, progress_callback=progress_callback, channel_context=channel_context)

    def _handle_multimedia(self, user_id, prompt, attachment):
        from api.domains.multimedia_flow import handle_multimedia
        return handle_multimedia(self, user_id, prompt, attachment)


    def process_image(self, user_id, image_path, platform="LINE"):
        """
        Handles incoming images.
        """
        logger.info(f"🖼️ Received Image from {user_id}: {image_path}")
        
        # Call Melchior
        description = analyze_image(image_path)
        
        # We could also store this memory or trigger other flows
        return f"👁️ Melchior sees: {description}"

    def _get_magi_status(self):
        """Get real-time MAGI node status from heartbeat."""
        import json
        import os
        status_file = f"{_MAGI_ROOT}/static/magi_status.json"
        
        try:
            if os.path.exists(status_file):
                with open(status_file, 'r') as f:
                    data = json.load(f)
                
                response = "🖥️ **MAGI 節點即時狀態**\n"
                response += f"更新時間: {data.get('timestamp', 'N/A')}\n\n"
                
                nodes = data.get('nodes', {})
                for key, node in nodes.items():
                    online = bool(node.get('online')) or str(node.get('status', '')).lower() == 'online'
                    status_icon = "🟢" if online else "🔴"
                    response += f"{status_icon} {node.get('name', key)} ({node.get('role', '')})\n"
                
                return response
            else:
                return "⚠️ 狀態檔案不存在。Heartbeat 服務可能未啟動。"
                
        except Exception as e:
            logger.error(f"❌ Status query error: {e}")
            return f"❌ 無法讀取狀態: {e}"

    def _get_collaboration_status(self):
        from api.domains.collab_flow import get_collaboration_status
        return get_collaboration_status(self)

    def _get_schedule(self):
        from api.domains.schedule_flow import get_schedule
        return get_schedule(self)

    def _translate_prompt_to_english(self, prompt: str) -> str:
        """Translate non-ASCII prompt to English for Stable Diffusion."""
        import urllib.parse, urllib.request
        # If prompt is already mostly ASCII, skip translation
        non_ascii = sum(1 for c in prompt if ord(c) > 127)
        if non_ascii < 2:
            return prompt
        try:
            q = urllib.parse.quote(prompt[:500])
            url = (
                "https://translate.googleapis.com/translate_a/single"
                f"?client=gtx&sl=auto&tl=en&dt=t&q={q}"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            data = json.loads(raw)
            parts = []
            if isinstance(data, list) and data and isinstance(data[0], list):
                for row in data[0]:
                    if isinstance(row, list) and row and row[0]:
                        parts.append(str(row[0]))
            en = " ".join(parts).strip()
            if en:
                logger.info(f"🌐 Prompt translated: '{prompt}' → '{en}'")
                return en
        except Exception as e:
            logger.warning(f"⚠️ Prompt translation failed, using original: {e}")
        return prompt

    def _generate_image(self, prompt, user_id=None):
        """Generate image using Melchior."""
        original_mode = get_brain_mode()

        try:
            from skills.bridge.melchior_bridge import generate_image

            # Translate Chinese prompt to English for Stable Diffusion
            sd_prompt = self._translate_prompt_to_english(prompt)

            result = generate_image(sd_prompt)
            
            if result.get("success"):
                path = result.get("path")
                model = result.get("model", "unknown")
                
                # Check if path is absolute or relative
                if not os.path.isabs(path):
                    path = os.path.abspath(path)
                
                # Verify file exists
                if os.path.exists(path):
                    # For LINE/Discord, we need to return a special format or handle it in server
                    # Here we return a text indicator + path
                    return f"🎨 Image Generated (模型: {model}, 提示詞: {prompt})|||IMAGE_PATH|||{path}"
                else:
                    return f"⚠️ Image generation reported success but file not found at: {path}"
            else:
                return f"❌ Image generation failed: {result.get('error')}"
                
        except Exception as e:
            logger.error(f"❌ Image generation error: {e}")
            # Local-first: no mode restore needed
            return f"❌ Error: {e}"

    def _should_attempt_auto_acquire(self, message: str, msg_lower: str) -> bool:
        if not message or len(message.strip()) < 6:
            return False
        blocked = ["/help", "/start", "help", "menu", "status", "狀態"]
        if msg_lower.strip() in blocked:
            return False

        demand_kws = [
            "幫我", "請", "做", "建立", "製作", "寫", "自動化", "處理",
            "build", "create", "implement", "write", "automate", "integrate", "execute",
        ]
        return any(k in msg_lower for k in demand_kws)

    def _looks_like_skill_creation_request(self, message: str) -> bool:
        text = str(message or "").strip()
        msg_lower = text.lower()
        if not msg_lower:
            return False
        skill_kws = [
            "learn to", "build skill", "create skill", "build a skill", "write a skill",
            "學會", "學習", "建立技能", "新增技能", "製作技能", "幫我寫一個技能",
            "打造一個技能", "做一個技能", "做個技能", "寫一個技能",
            "做一個工具", "做個工具",
            "建立一個工具", "建立一個流程", "做一個流程", "做個流程",
        ]
        if any(kw in msg_lower for kw in skill_kws):
            return True
        return bool(
            re.search(
                r"(幫我|請|麻煩|我要|我想要).{0,8}(做|建立|打造|規劃|撰寫|寫).{0,24}(工具|技能|skill|流程|agent|機器人|功能)",
                text,
                re.IGNORECASE,
            )
        )

    def _should_start_skill_interview_from_gap(self, message: str, role: str, intent: str = "", er_result=None) -> bool:
        text = str(message or "").strip()
        if role != "admin":
            return False
        if not text or self._looks_like_capability_question(text):
            return False
        return self._looks_like_skill_creation_request(text)

    _FORGE_MAX_RETRIES = 3
    _FORGE_TIMEOUT_SCHEDULE = (300, 420, 600)  # 5min, 7min, 10min — escalating
    _FORGE_LOCK_TIMEOUT = 600  # Safety: force-release lock after 600s

    def _auto_acquire_and_execute(self, user_id, message, platform: str = "LINE"):
        from api.domains.acquisition_flow import auto_acquire_and_execute
        return auto_acquire_and_execute(self, user_id, message, platform)

    def _laf_report_command_help(self) -> str:
        return _get_handler("laf").laf_report_command_help()

    def _detect_laf_report_action(self, text: str) -> tuple[str, str]:
        return _get_handler("laf").detect_laf_report_action(text)

    def _parse_laf_report_payload(self, raw_text: str):
        return _get_handler("laf").parse_laf_report_payload(raw_text)

    # ── Vision-based smart routing for images ──────────────────────────

    def _vision_classify_and_route_image(self, user_id, image_path: str, prompt):
        from api.domains.multimedia_flow import vision_classify_and_route_image
        return vision_classify_and_route_image(self, user_id, image_path, prompt)

    def _handle_payment_proof_from_channel(self, image_path: str) -> str:
        from api.domains.multimedia_flow import handle_payment_proof_from_channel
        return handle_payment_proof_from_channel(self, image_path)

    def _handle_command(self, user_id, message, role="user", platform="LINE"):
        from api.pipelines.command_dispatch import handle_command
        return handle_command(self, user_id, message, role=role, platform=platform)

    def _list_skills(self):
        from api.pipelines.command_dispatch import list_skills
        return list_skills(self)

    def _handle_query(self, user_id, message, platform_hint="LINE"):
        return _chat_pipeline.handle_query(self, user_id, message, platform_hint)

    def _handle_chat_async(self, user_id, message, platform_hint="LINE"):
        return _chat_pipeline.handle_chat_async(self, user_id, message, platform_hint)
