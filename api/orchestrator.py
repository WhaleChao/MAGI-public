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
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
import threading as _threading

from api.model_config import TEXT_PRIMARY_MODEL
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



# Configure Logging
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
file_handler = _RotatingFileHandler(f'{_MAGI_ROOT}/casper.log', maxBytes=5*1024*1024, backupCount=3)
file_handler.setFormatter(log_formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
logger = logging.getLogger("Orchestrator")

from api.command_registry import CommandRegistry, CommandContext
# Global command registry — commands registered below after class definition
_cmd_registry = CommandRegistry()

class Orchestrator:
    def __init__(self):
        self.classifier = IntentionClassifier()
        self._inference_gw = InferenceGateway()  # shared instance — avoids per-call re-init
        self._cmd_registry = _cmd_registry
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
            _omlx_url = os.environ.get("OMLX_BASE_URL", "http://localhost:8080") + "/v1/models"
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
    def _default_notification_callback(user_id: str, text: str, platform: str):
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
        progress: float | None = None,
        user_id: str = "",
        detail: dict | None = None,
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

    def _append_route_trace(self, user_id: str, platform: str, stage: str, route: str, detail: dict | None = None) -> None:
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
                        with open(self._route_trace_file, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                        import tempfile as _tf
                        fd, tmp = _tf.mkstemp(dir=self._agent_dir, suffix=".tmp")
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            f.writelines(lines[-50000:])
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
        try:
            p = os.path.join(os.path.expanduser("~"), ".openclaw", "openclaw.json")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                m = (((cfg or {}).get("agents") or {}).get("defaults") or {}).get("model") or {}
                primary = str(m.get("primary") or "").strip()
                if primary:
                    return primary
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 328, exc_info=True)
        return "未設定"

    # ── 亂碼回報 ──────────────────────────────────────────────────
    _GIBBERISH_REPORT_RE = re.compile(
        r"^(亂碼|這是亂碼|你回的是亂碼|剛才是亂碼|那是亂碼|gibberish|亂碼回報)$",
        re.IGNORECASE,
    )
    _GIBBERISH_LOG_PATH = Path(os.environ.get(
        "MAGI_GIBBERISH_LOG",
        str(Path(__file__).resolve().parent.parent / "static" / "gibberish_samples.jsonl"),
    ))

    def _handle_gibberish_report(self, user_id, message: str, platform: str = "") -> str | None:
        """使用者回報「亂碼」→ 取上一則 assistant 回覆存入 JSONL，供偵測模組學習。"""
        if not self._GIBBERISH_REPORT_RE.search((message or "").strip()):
            return None

        # 從歷史中找最後一筆 assistant 回覆
        hist = list(self.user_history.get(user_id, []))
        last_assistant = None
        for entry in reversed(hist):
            if entry.get("role") == "assistant":
                last_assistant = entry.get("content", "")
                break

        if not last_assistant or len(last_assistant) < 5:
            return "⚠️ 找不到上一則回覆，無法記錄。"

        # 寫入 JSONL
        try:
            self._GIBBERISH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            record = {
                "ts": _dt.now(_tz.utc).isoformat(),
                "user_id": str(user_id or ""),
                "platform": str(platform or ""),
                "text": last_assistant,
            }
            with self._GIBBERISH_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"亂碼回報寫入失敗: {e}")
            return "⚠️ 記錄失敗，請稍後重試。"

        # 自動將樣本餵給 tw_output_guard 的 _GIBBERISH_KEYWORDS（runtime 熱更新）
        try:
            from api.tw_output_guard import _GIBBERISH_KEYWORDS
            # 從亂碼文本中提取 3-gram 作為候選關鍵字（取出現頻率最高的）
            text = last_assistant.strip()
            if len(text) >= 6:
                from collections import Counter as _Counter
                trigrams = [text[i:i+3] for i in range(len(text) - 2)]
                # 過濾掉常見的正常中文 trigram（標點、空白起始等）
                trigrams = [t for t in trigrams if not re.match(r"^[\s，。、；：！？「」『』（）\n]", t)]
                common = _Counter(trigrams).most_common(3)
                for gram, count in common:
                    if count >= 2 and gram not in _GIBBERISH_KEYWORDS:
                        _GIBBERISH_KEYWORDS.append(gram)
                        logger.info(f"[亂碼學習] 新增關鍵字: {gram}")
        except Exception:
            pass  # best-effort

        return f"✅ 已記錄該亂碼回覆，偵測模組會自動學習。感謝回報！"

    def _quick_fixed_reply(self, message: str, role: str = "user") -> str | None:
        """
        Deterministic quick-replies for frequent operational questions.
        """
        t = str(message or "").strip().lower()
        if not t:
            return None

        if re.search(r"(下一步|接下來|下一個步驟|後續怎麼做|next step)", t):
            return "下一步建議：1) 先確認 LINE/DC/TG 通道都正常 2) 跑一次自我測試 3) 針對失敗項目自動修復。"

        if re.search(r"(為什麼.*繁體中文|為何.*繁體中文|不是繁體中文|請用繁體中文|traditional chinese)", t):
            return "收到，後續我會固定使用繁體中文（臺灣用語）回覆。"

        if re.search(r"(help|有.*功能|可以.*什麼|做什麼|功能列表|技能清單|有.*skill|指令|幫助|說明|^功能$)", t):
            if role == "admin":
                return (
                    "🛠️ **MAGI 指令總表 (管理員)**\n\n"
                    "━━ 系統 ━━\n"
                    "• 系統狀態 ⚡ ｜ 自動巡檢 🔄 ｜ 夜間任務 🔄\n"
                    "• 自我測試 ⚡ ｜ 技能狀態 ⚡ ｜ 目前模型 ⚡\n\n"
                    "━━ 除錯 ━━\n"
                    "• 健康檢查 ⚡ ｜ 全面排查 ⚡ ｜ 自動修復 ⚡\n"
                    "• 穩定度 ⚡ ｜ 套用降級 ⚡ ｜ 清除降級 ⚡\n\n"
                    "━━ 法扶 ━━\n"
                    "• 幫[姓名]做開辦草稿 ⚡ ｜ 法扶監控 🔄\n"
                    "• 疑義/撤回/費用/二階段/結案草稿 ⚡\n"
                    "• 正式送出開辦/結案 ⚡（需確認）\n"
                    "• 自動報結掃描 🔄 ｜ 二階段批次 🔄\n\n"
                    "━━ 閱卷 ━━\n"
                    "• 檢查閱卷信箱 🔄 ｜ 閱卷信件預覽 ⚡\n"
                    "• 可下載案件 ⚡ ｜ 下載閱卷 [案號] 🔄\n"
                    "• 閱卷重授權 ⚡\n\n"
                    "━━ 筆錄 ━━\n"
                    "• 同步筆錄 🔄 ｜ 重命名筆錄 🔄\n"
                    "• 下載全部筆錄 🔄 ｜ 下載筆錄 [案號] 🔄\n\n"
                    "━━ 案件 (OSC) ━━\n"
                    "• 掃描案件待辦 🔄 ｜ 待辦佇列狀態 ⚡\n"
                    "• 待辦佇列入庫 🔄 ｜ 日曆同步 🔄\n\n"
                    "━━ PDF ━━\n"
                    "• 單檔命名 [路徑] ⚡ ｜ 批次命名 🔄\n\n"
                    "━━ 爬蟲／判決／法規 ━━\n"
                    "• 爬蟲清單 ⚡ ｜ 新增爬蟲 [url] 🔄 ｜ 移除爬蟲 ⚡\n"
                    "• 每日爬蟲 🔄 ｜ 找判決 [關鍵字] 🔄\n"
                    "• 法規搜尋 [查詢] ⚡ ｜ 法規向量更新 🔄\n\n"
                    "━━ 翻譯／文件 ━━\n"
                    "• 完整翻譯 [文字] 🔄 ｜ 翻譯檔案 [路徑] 🔄\n"
                    "• 去AI味 [文字] ⚡ ｜ 逐字稿 [音檔] 🔄\n\n"
                    "━━ 大腦 ━━\n"
                    "• 開大腦 ⚡ ｜ 關大腦 ⚡\n"
                    "• 修理大腦 🔄 ｜ 校準大腦 🔄\n\n"
                    "━━ 資料庫 (管理員) ━━\n"
                    "• 備份資料庫 🔄 ｜ 備份清單 ⚡ ｜ 還原資料庫 🔄\n\n"
                    "━━ 其他 ━━\n"
                    "• 記住 [內容] ⚡ ｜ 鐵穹 ⚡ ｜ 草擬信 ⚡\n"
                    "• 亂碼 ⚡ — 回報上一則回覆是亂碼（系統自動學習）\n\n"
                    "⚡ = 即時回覆　🔄 = 背景執行\n"
                    "💡 也可直接用自然語言下達，如「找關於詐欺的判決」"
                )
            else:
                return (
                    "🛠️ **MAGI 指令總表**\n\n"
                    "━━ 系統 ━━\n"
                    "• 系統狀態 ⚡ ｜ 目前模型 ⚡ ｜ 技能狀態 ⚡\n"
                    "• 健康檢查 ⚡\n\n"
                    "━━ 法扶 ━━\n"
                    "• 幫[姓名]做開辦草稿 ⚡ ｜ 法扶監控 🔄\n"
                    "• 疑義/撤回/費用/二階段/結案草稿 ⚡\n"
                    "• 正式送出開辦/結案 ⚡（需確認）\n"
                    "• 法扶回報指令 ⚡ ｜ 自動報結掃描 🔄\n\n"
                    "━━ 閱卷 ━━\n"
                    "• 檢查閱卷信箱 🔄 ｜ 閱卷信件預覽 ⚡\n"
                    "• 可下載案件 ⚡ ｜ 下載閱卷 [案號] 🔄\n"
                    "• 閱卷查核 [法院] [案號] ⚡ ｜ 閱卷聲請 [法院] [案號] 🔄\n\n"
                    "━━ 筆錄 ━━\n"
                    "• 同步筆錄 🔄 ｜ 重命名筆錄 🔄\n"
                    "• 下載全部筆錄 🔄 ｜ 下載筆錄 [案號] 🔄\n\n"
                    "━━ 摘要 ━━\n"
                    "• 摘要 [文字/網址] 🔄 ｜ 精簡摘要 / 詳細摘要\n"
                    "• 上傳 PDF 自動摘要 🔄\n\n"
                    "━━ 逐字稿 ━━\n"
                    "• 逐字稿 [音檔] 🔄 ｜ 上傳音檔自動轉寫 🔄\n\n"
                    "━━ 翻譯 ━━\n"
                    "• 完整翻譯 [文字] 🔄 ｜ 翻譯檔案 [路徑] 🔄\n"
                    "• 去AI味 [文字] ⚡\n\n"
                    "━━ 文件產生 ━━\n"
                    "• 委任狀 ⚡ ｜ 契約書 ⚡ ｜ 收據 ⚡ ｜ 存證信函 ⚡\n\n"
                    "━━ 案件 (OSC) ━━\n"
                    "• 掃描案件待辦 🔄 ｜ 待辦佇列狀態 ⚡\n"
                    "• 日曆同步 🔄 ｜ [姓名]已繳費 ⚡\n\n"
                    "━━ PDF ━━\n"
                    "• 單檔命名 [路徑] ⚡ ｜ 批次命名 🔄\n\n"
                    "━━ 爬蟲／判決／法規 ━━\n"
                    "• 爬蟲清單 ⚡ ｜ 新增爬蟲 [url] 🔄 ｜ 移除爬蟲 ⚡\n"
                    "• 找判決 [關鍵字] 🔄 ｜ 判決趨勢 [案由] 🔄\n"
                    "• 法規搜尋 [查詢] ⚡ ｜ 加班費 ⚡\n\n"
                    "━━ 搜尋 ━━\n"
                    "• /搜尋 [關鍵字] 🔄 ｜ /抓取 [網址] 🔄\n\n"
                    "━━ 助理 ━━\n"
                    "• 記住 [內容] ⚡ ｜ 深度思考 [問題] 🔄\n"
                    "• 行程 ⚡ ｜ 庭期 ⚡ ｜ 股市晨報 🔄\n"
                    "• 備份資料庫 🔄 ｜ 備份清單 ⚡\n\n"
                    "⚡ = 即時回覆　🔄 = 背景執行\n"
                    "💡 直接用自然語言下達即可，如「找關於詐欺的判決」\n"
                    "🔒 大腦管理、鐵穹、技能進化等需管理員權限"
                )

        if re.search(r"(你現在使用模型|現在使用模型|目前模型|模型為何|模型是什麼|使用什麼模型|what model)", t):
            primary = self._read_openclaw_primary_model()
            target_main = (os.environ.get("MAGI_MAIN_MODEL") or TEXT_PRIMARY_MODEL).strip() or TEXT_PRIMARY_MODEL
            # Query oMLX for active models
            omlx_models = []
            try:
                import requests as _req
                _r = _req.get("http://127.0.0.1:8080/v1/models", timeout=3)
                if _r.status_code == 200:
                    omlx_models = [m.get("id", "") for m in _r.json().get("data", [])]
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 419, exc_info=True)
            active = ", ".join(omlx_models[:4]) if omlx_models else "oMLX 離線"
            return (
                f"推理引擎：oMLX (port 8080)\n"
                f"可用模型：{active}\n"
                f"主要模型：{target_main}\n"
                f"模式：本地推理 oMLX（Ollama 已退役）\n"
                f"OpenClaw 預設：{primary}"
            )

        return None

    def _brain_runtime_banner(self) -> str:
        """
        Runtime banner — now returns empty to avoid cluttering every reply.
        Brain mode info is still available via 系統狀態 / status commands.
        """
        return ""

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
        v = str(os.environ.get("MAGI_ENABLE_NL_COMMAND_ROUTER", "1")).strip().lower()
        return v in {"1", "true", "yes", "on"}

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
        text = (message or "").strip()
        if not text:
            return False
        if text.startswith("/") or text.startswith("!"):
            return False
        if len(text) > 120000:
            return False
        # Keep attachment/document translation on the dedicated file pipeline.
        ambiguous_short_phrases = {
            "翻譯", "全文翻譯", "完整翻譯", "不要摘要",
            "整篇全文", "整篇翻譯", "摘要", "總結",
        }
        if len(text) <= 16 and text.replace(" ", "") in ambiguous_short_phrases:
            return False
        low_compact = text.lower().replace(" ", "")
        for kw_lower in self._NL_STOCK_PHRASES_LOWER:
            if kw_lower in low_compact:
                return False
        low = text.lower()
        for i, kw in enumerate(self._NL_ROUTE_KWS):
            if kw in text or self._NL_ROUTE_KWS_LOWER[i] in low:
                return True
        return False

    def _load_market_watch_state(self) -> dict:
        path = os.path.join(self._agent_dir, "market_watchlist.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 512, exc_info=True)
        return {}

    @staticmethod
    def _is_stock_like_token(token: str) -> bool:
        t = str(token or "").strip()
        if not t:
            return False
        up = t.upper()
        if re.fullmatch(r"\d{4}(?:\.(?:TW|TWO))?", up):
            return True
        if re.fullmatch(r"[A-Z]{1,6}", up):
            return True
        if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", t):
            banned = {
                "追蹤", "股票", "清單", "設定", "新增", "移除", "刪除",
                "今天", "明天", "可以", "幫我", "請問", "謝謝", "收到",
            }
            return t not in banned
        return False

    def _looks_like_market_watchlist_reply(self, message: str) -> bool:
        raw = str(message or "").strip()
        if not raw or len(raw) > 160:
            return False
        # Remove common command wrappers; users often reply in this style.
        raw = re.sub(
            r"^(?:追蹤股票|追蹤清單|我要追蹤|設定追蹤|更新追蹤|新增追蹤|增加追蹤)\s*[:：]?\s*",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip()
        for sep in ["和", "與", "及", " plus ", " PLUS ", "+", "＋"]:
            raw = raw.replace(sep, " ")
        parts = [
            p.strip(" \t\r\n,，、;；|/()[]{}\"'`")
            for p in re.split(r"[\s,，、;；|/]+", raw)
            if p.strip(" \t\r\n,，、;；|/()[]{}\"'`")
        ]
        if not parts or len(parts) > 12:
            return False
        filler = {
            "請", "麻煩", "幫我", "謝謝", "感謝", "收到",
            "追蹤", "股票", "清單", "設定", "新增", "移除", "刪除",
            "THANKS", "THX", "PLEASE",
        }
        good = 0
        bad = 0
        for p in parts:
            if p.upper() in filler or p in filler:
                continue
            if self._is_stock_like_token(p):
                good += 1
            else:
                bad += 1
        return good > 0 and bad == 0

    def _try_market_watchlist_quick_set(self, message: str, platform: str = "") -> tuple[bool, str]:
        """
        Fallback for Telegram/LINE quick replies after the first stock prompt:
        if watchlist is empty and user replies with plain symbols/names, treat it as market_set.
        """
        text = str(message or "").strip()
        if not text or text.startswith("/") or text.startswith("!"):
            return False, ""

        st = self._load_market_watch_state()
        watch = st.get("watchlist") if isinstance(st.get("watchlist"), list) else []
        first_prompt_date = str(st.get("first_prompt_date") or "").strip()

        # Only auto-capture in the "first prompt pending" stage to avoid false positives.
        if watch or not first_prompt_date:
            return False, ""
        if not self._looks_like_market_watchlist_reply(text):
            return False, ""

        py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
        if not py or not os.path.exists(py):
            py = sys.executable or "python3"
        skill_script = f"{_MAGI_ROOT}/skills/market-briefing/action.py"
        if not os.path.exists(skill_script):
            return False, ""

        try:
            proc = subprocess.run(
                [py, skill_script, "--task", "set", "--text", text],
                capture_output=True,
                text=True,
                timeout=90,
                cwd=_MAGI_ROOT,
                env=os.environ.copy(),
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                logger.warning(f"market quick-set failed rc={proc.returncode}: {(err or out)[:240]}")
                return False, ""
            if not out or "⚠️ 我沒解析到股票代號" in out:
                return False, ""
            logger.info("✅ market quick-set captured plain watchlist reply")
            return True, self._postprocess_router_reply(out, platform)
        except Exception as e:
            logger.warning(f"market quick-set exception: {e}")
            return False, ""

    def _extract_judgment_collect_payload(self, message: str) -> tuple[dict | None, str]:
        text = str(message or "").strip()
        if not text:
            return None, "🔎 請提供案由或案號，例如：`查判決 傷害`、`查判決 113年度上訴字第12號`"

        raw = re.sub(r"^@MAGI\s*", "", text, flags=re.IGNORECASE).strip()
        for _ in range(3):
            prev = raw
            raw = re.sub(r"^(?:幫我|請|麻煩|幫忙|可以幫我|協助我)\s*", "", raw).strip()
            raw = re.sub(
                r"^(?:查判決|找判決|判決搜尋|搜尋判決|收集判決|判決搜集|搜尋最高法院判決)\s*",
                "",
                raw,
            ).strip()
            raw = re.sub(r"^(?:查一下|找一下|搜尋一下|搜一下)\s*", "", raw).strip()
            if raw == prev:
                break
        raw = raw.strip(" ：:，,。；;")

        case_match = re.search(
            r"(\d{4}-\d{4}|\d{2,3}年度[^\s]{1,12}字第?\d+號?)",
            raw,
        )
        if case_match:
            return {"case_number": case_match.group(1).strip()}, ""

        reason = re.sub(r"^(?:最近的?|最新的?|最高法院的?|法院的?)", "", raw).strip()
        reason = re.sub(r"(?:的)?(?:法院)?判決$", "", reason).strip(" ：:，,。；;")
        reason = re.sub(r"\s+", " ", reason).strip()

        generic_only = {
            "最近", "最新", "法院", "判決", "最近判決", "最新判決",
            "法院判決", "最近法院判決", "最近的法院判決",
            "最新法院判決", "最新的法院判決", "最高法院判決",
        }
        if not reason or len(reason) < 2 or reason in generic_only:
            return None, "🔎 請提供案由或案號，例如：`查判決 傷害`、`查判決 113年度上訴字第12號`"
        return {"case_reason": reason}, ""

    def _format_judgment_collect_result(self, payload: dict) -> str:
        if not isinstance(payload, dict):
            return "❌ 判決搜尋失敗：回傳格式異常"
        if not payload.get("success"):
            err = str(payload.get("error") or "unknown").strip()
            return f"❌ 判決搜尋失敗：{err}"

        reason = str(payload.get("case_reason") or payload.get("case_number") or "").strip()
        lines = [f"📚 判決搜尋完成：{reason or '案件'}"]
        court_level = str(payload.get("court_level") or "").strip()
        if court_level:
            lines.append(f"法院：{court_level}")
        count = payload.get("count")
        if count is not None:
            lines.append(f"收集筆數：{count}")
        # 不顯示本機路徑（LINE 外部看不到），報告內容直接列在下方

        # LINE 訊息長度限制 ~5000 字元，預留 header 空間
        LINE_MSG_BUDGET = 4500
        header_len = len("\n".join(lines)) + 2
        remaining = LINE_MSG_BUDGET - header_len

        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for row in items:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            # 優先顯示摘要全文，次選摘要預覽
            summary = str(row.get("summary_full") or row.get("summary_preview") or "").strip()
            is_degraded = row.get("is_degraded", False)

            entry_lines = [f"\n{'=' * 30}", f"【{title[:80]}】"]
            if row.get("url"):
                entry_lines.append(str(row["url"]))
            if summary and not is_degraded:
                # 截斷過長摘要，保留結構完整性
                if len(summary) > 600:
                    summary = summary[:600] + "…（完整內容見報告）"
                entry_lines.append(summary)
            elif is_degraded and summary:
                entry_lines.append(f"[摘要品質不佳，待重試]\n{summary[:200]}…")
            else:
                entry_lines.append("[尚無摘要]")

            entry_text = "\n".join(entry_lines)
            if len(entry_text) > remaining:
                # 預算不夠，加上省略提示後停止
                lines.append(f"\n…其餘 {len(items) - len([l for l in lines if l.startswith('【')])} 筆請見報告檔案")
                break
            lines.append(entry_text)
            remaining -= len(entry_text)

        retry_queued_count = payload.get("retry_queued_count")
        if retry_queued_count:
            lines.append(f"\n摘要重試佇列：+{retry_queued_count}")
        return "\n".join(lines)

    def _run_judgment_collector_command(self, message: str, notify: bool = False) -> str:
        payload, err = self._extract_judgment_collect_payload(message)
        if not payload:
            return err

        py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
        if not py or not os.path.exists(py):
            py = sys.executable or "python3"
        skill_script = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
        if not os.path.exists(skill_script):
            return "❌ 找不到判決搜尋 skill。"

        payload = {
            **payload,
            "max_results": int(os.environ.get("MAGI_JUDGMENT_CHAT_MAX_RESULTS", "12") or "12"),
            "headless": True,
            "save_to_db": True,
            "notify": bool(notify),
        }
        task = "collect " + json.dumps(payload, ensure_ascii=False)
        try:
            proc = subprocess.run(
                [py, skill_script, "--task", task],
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("MAGI_JUDGMENT_CHAT_TIMEOUT_SEC", "180") or "180"),
                cwd=_MAGI_ROOT,
                env=os.environ.copy(),
            )
        except Exception as e:
            return f"❌ 判決搜尋錯誤：{e}"

        out = (proc.stdout or "").strip()
        err_text = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return f"❌ 判決搜尋失敗：{(err_text or out or 'unknown')[:280]}"
        if not out:
            return "❌ 判決搜尋失敗：沒有收到輸出"
        try:
            data = json.loads(out)
        except Exception:
            return out[:1500]
        return self._format_judgment_collect_result(data)

    def _run_judgment_trend_command(self, message: str) -> str:
        """執行判決趨勢分析。"""
        py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
        if not py or not os.path.exists(py):
            py = sys.executable or "python3"
        skill_script = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
        if not os.path.exists(skill_script):
            return "❌ 找不到判決搜尋 skill。"
        # 嘗試提取案由
        case_reason = ""
        for prefix in ["判決趨勢", "趨勢分析", "案由分析", "判決分析"]:
            if prefix in message:
                case_reason = message.split(prefix)[-1].strip()
                break
        payload = {}
        if case_reason:
            payload["case_reason"] = case_reason
        task = "trend_analysis " + json.dumps(payload, ensure_ascii=False) if payload else "trend_analysis"
        try:
            proc = subprocess.run(
                [py, skill_script, "--task", task],
                capture_output=True, text=True, timeout=30,
                cwd=_MAGI_ROOT, env=os.environ.copy(),
            )
            return (proc.stdout or "").strip()[:2000] or "❌ 趨勢分析無輸出"
        except Exception as e:
            return f"❌ 趨勢分析錯誤：{e}"

    def _strip_intent_prefixes(self, text: str, patterns: list[str]) -> str:
        return _get_handler("tp").strip_intent_prefixes(text, patterns)

    def _run_labor_law_command(self, message: str) -> str:
        try:
            skill_script = f"{_MAGI_ROOT}/skills/labor-law-calculator/action.py"
            if not os.path.exists(skill_script):
                return "❌ 找不到勞基法計算器 skill。"
            task = self._strip_intent_prefixes(
                message,
                [
                    r"^(?:勞基法計算|勞動基準法計算|加班費計算)\s*",
                    r"^(?:幫我|請|麻煩|協助我|可以幫我)?\s*",
                ],
            )
            if not task:
                return "❓ 請提供計算條件，例如：`月薪 50000，休息日加班 3 小時`"
            file_paths = re.findall(
                r"(?:/[^\s,，；;]+\.(?:xlsx|xls|pdf)|[A-Za-z]:[^\s,，；;]+\.(?:xlsx|xls|pdf))",
                task,
                re.IGNORECASE,
            )
            if file_paths:
                task_clean = re.sub(
                    r"(?:/[^\s,，；;]+\.(?:xlsx|xls|pdf)|[A-Za-z]:[^\s,，；;]+\.(?:xlsx|xls|pdf))",
                    "",
                    task,
                    flags=re.IGNORECASE,
                ).strip()
                cmd = [sys.executable, skill_script, "--task", task_clean, "--file"] + file_paths
                timeout_sec = 120
            else:
                cmd = [sys.executable, skill_script, "--task", task]
                timeout_sec = 30
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=_MAGI_ROOT,
                env=os.environ.copy(),
            )
            out = (proc.stdout or "").strip()
            if proc.returncode != 0 or not out:
                err = (_proc_err if (_proc_err := (proc.stderr or out or "unknown").strip()) else "unknown")[:300]
                return f"❌ 勞基法計算失敗：{err}"
            return out
        except Exception as e:
            return f"❌ 勞基法計算器錯誤：{e}"

    def _run_inline_translation_command(self, user_id, message: str) -> str:
        text = self._strip_intent_prefixes(
            message,
            [
                r"^(?:幫我|請|麻煩|協助我|可以幫我)?\s*",
                r"^(?:翻譯|translate)\s*",
            ],
        )
        if not text:
            return "❓ 請提供要翻譯的文字。"
        if len(text) <= 800 and len(text.splitlines()) <= 4:
            try:
                from skills.bridge.tri_sage_collab import translate_text as _translate_text

                result = _translate_text(text, target_lang="繁體中文", source_lang="auto", mode="full")
            except Exception:
                result = self._translate_text_complete(text, source_lang="auto", target_lang="繁體中文")
        else:
            result = self._translate_text_complete(text, source_lang="auto", target_lang="繁體中文")
        if not result.get("success"):
            err = str(result.get("error") or "unknown").strip()
            if err.startswith("translation_off_topic:"):
                return "❌ 翻譯結果偏題，已阻擋送出。請稍後重試。"
            return f"❌ 翻譯失敗: {err}"
        translated_text = str(result.get("text") or "").strip()
        msg_lower = str(message or "").lower()
        disable_txt = any(k in msg_lower for k in ["不要txt", "不需要txt", "no txt", "inline", "直接貼上"])
        explicit_txt = any(k in msg_lower for k in ["txt", "文字檔", "檔案"])
        is_url = bool(re.search(r"https?://", text, flags=re.IGNORECASE))
        try:
            long_threshold = int(os.environ.get("MAGI_TRANSLATE_TXT_MIN_CHARS", "1200") or "1200")
        except Exception:
            long_threshold = 1200
        is_long = len(text) >= max(400, long_threshold)
        want_export = (not disable_txt) and (explicit_txt or is_url or is_long)
        if want_export:
            # Prefer DOCX bilingual table, fallback to TXT
            exported_reply = self._export_translation_docx(
                source_text=text,
                translated_text=translated_text,
                title="",
                subtitle="",
                prefix="full_translation",
                user_id=str(user_id or ""),
            )
            if not exported_reply:
                exported_reply = self._export_translation_txt(
                    translated_text=translated_text,
                    source=(text[:240] + "…") if len(text) > 240 else text,
                    provider=str(result.get("provider") or "tri-sage"),
                    mode="full_translation",
                    prefix="full_translation",
                    user_id=str(user_id or ""),
                )
            if exported_reply:
                return exported_reply
        return f"🌐 翻譯結果（{result.get('provider','tri-sage')}）:\n{translated_text}"

    def _run_inline_summary_command(self, message: str) -> str:
        summary_length = self._detect_summary_length(message)
        text = self._strip_intent_prefixes(
            message,
            [
                r"^(?:幫我|請|麻煩|協助我|可以幫我)?\s*",
                r"^(?:短摘要?|詳細摘要?|簡短摘要?|完整摘要?|長摘要?|精簡摘要?)\s*",
                r"^(?:摘要|總結|重點整理|summarize|summarise|summary)\s*",
            ],
        )
        if not text:
            return (
                "❓ 請提供要摘要的內容。\n\n"
                "💡 可指定摘要等級：\n"
                "• `精簡摘要 ...` 或 `短摘要 ...` → 3-5 點，每點一句話\n"
                "• `摘要 ...` → 5-8 點，每點 1-2 句（預設）\n"
                "• `詳細摘要 ...` 或 `長摘要 ...` → 12-15 點，每點 2-3 句（含背景與數據）"
            )
        result = self._summarize_text_resilient(text, summary_length=summary_length)
        if not result.get("success"):
            return f"❌ 摘要失敗：{str(result.get('error') or 'unknown')}"
        summary_text = str(result.get("text") or result.get("summary") or "").strip()
        if not summary_text:
            return "❌ 摘要失敗：沒有可用結果"
        length_label = {"short": "精簡", "medium": "標準", "long": "詳細"}.get(summary_length, "")
        return f"📝 {length_label}摘要結果（{result.get('provider', 'summary')}）:\n{summary_text}"

    def _run_stock_briefing_command(self, message: str) -> str:
        skill_script = f"{_MAGI_ROOT}/skills/market-briefing/action.py"
        if not os.path.exists(skill_script):
            return "❌ 找不到股市追蹤 skill。"
        text = str(message or "").strip()
        if self._looks_like_capability_question(text):
            return (
                "✅ **我可以幫您追蹤股票與產生晨報！**\n\n"
                "• 設定：`追蹤股票 台積電 AAPL`\n"
                "• 清單：`追蹤清單`\n"
                "• 晨報：`股市晨報`"
            )
        msg_lower = text.lower()
        if any(k in text for k in ["目前追蹤", "追蹤清單"]) or "watchlist" in msg_lower:
            task = "list"
            payload = ""
        elif any(k in text for k in ["移除追蹤", "刪除追蹤", "取消追蹤"]) or "remove" in msg_lower:
            task = "remove"
            payload = self._strip_intent_prefixes(
                text,
                [r"^(?:幫我|請|麻煩|協助我|可以幫我)?\s*", r"^(?:移除追蹤|刪除追蹤|取消追蹤)\s*"],
            )
        elif any(k in text for k in ["追蹤以下股票", "追蹤股票", "設定追蹤", "新增追蹤", "增加追蹤"]) or any(k in msg_lower for k in ["track ", "watch "]):
            task = "set" if any(k in text for k in ["追蹤以下股票", "追蹤股票", "設定追蹤"]) else "add"
            # "增加追蹤" should add, not set

            payload = self._strip_intent_prefixes(
                text,
                [r"^(?:幫我|請|麻煩|協助我|可以幫我)?\s*", r"^(?:追蹤以下股票|追蹤股票|設定追蹤|新增追蹤|增加追蹤)\s*"],
            )
        else:
            task = "briefing"
            payload = ""
        if any(k in text for k in ["快速模式", "簡報"]) or "quick" in msg_lower:
            mode = "quick"
        elif any(k in text for k in ["技術分析", "MACD", "RSI", "布林通道"]) or any(k in msg_lower for k in ["technical", "macd", "rsi"]):
            mode = "technical"
        else:
            mode = "deep"
        cmd = [sys.executable, skill_script, "--task", task, "--mode", mode]
        if payload:
            cmd.extend(["--text", payload])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=_MAGI_ROOT,
                env=os.environ.copy(),
            )
            out = (proc.stdout or "").strip()
            if proc.returncode != 0 or not out:
                err = ((proc.stderr or out or "unknown").strip())[:300]
                return f"❌ 股市追蹤失敗：{err}"
            return out
        except Exception as e:
            return f"❌ 股市追蹤錯誤：{e}"

    def _run_court_hearing_command(self, message: str) -> str:
        py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
        if not py or not os.path.exists(py):
            py = sys.executable or "python3"
        skill_script = f"{_MAGI_ROOT}/skills/court-hearing-reminder/action.py"
        if not os.path.exists(skill_script):
            return "❌ 找不到開庭提醒 skill。"
        text = str(message or "").strip()
        if self._looks_like_capability_question(text):
            return (
                "✅ **我可以幫您查排程！**\n\n"
                "• 查看排程：`最近有什麼庭`（含開庭/補正/繳費）\n"
                "• 庭前準備：`準備 XXX 案的開庭資料`\n"
                "• 準備清單：`XXX案的準備清單`\n"
                "• 案件總覽：`案件時程總覽`\n"
                "• 標記完成：`張國賢繳了` / `補字第54號交了`"
            )

        # 判斷任務類型
        import re as _re
        # 「繳了/交了/補正了/已繳/完成了」→ done
        done_match = _re.search(
            r"(.+?)(?:的)?(?:繳了|交了|繳費了|補正了|完成了|已繳|已補正|已交|已完成)$",
            text,
        )
        if not done_match:
            # 也支持「關掉XX的提醒」
            done_match2 = _re.search(r"(?:關掉|取消|關閉)(.+?)(?:的)?(?:提醒|警報|通知)?$", text)
            if done_match2:
                done_match = done_match2

        if done_match:
            task = "done"
            cmd = [py, skill_script, "--task", task, "--text", text]
        elif any(k in text for k in ["pattern", "對造", "歷史案件", "同一對造", "跨案件", "案件分析"]):
            query = text
            for prefix in ["pattern", "跨案件分析", "案件分析", "歷史案件", "查"]:
                query = query.replace(prefix, "")
            query = query.strip()
            task = "patterns"
            cmd = [py, skill_script, "--task", task, "--text", query]
        elif any(k in text for k in ["checklist", "清單", "應備文件", "準備清單"]):
            case_no = text
            for prefix in ["checklist", "準備清單", "應備文件", "開庭清單", "案的", "案"]:
                case_no = case_no.replace(prefix, "")
            case_no = case_no.strip()
            task = "checklist"
            cmd = [py, skill_script, "--task", task, "--text", case_no]
        elif any(k in text for k in ["dashboard", "總覽", "時程總覽", "全部排程", "所有案件"]):
            task = "dashboard"
            cmd = [py, skill_script, "--task", task]
        elif any(k in text for k in ["準備", "庭前", "摘要"]):
            case_no = text
            for prefix in ["準備", "庭前準備", "開庭資料", "案的", "案"]:
                case_no = case_no.replace(prefix, "")
            case_no = case_no.strip()
            task = "prep"
            cmd = [py, skill_script, "--task", task, "--case-number", case_no]
        else:
            task = "list"
            cmd = [py, skill_script, "--task", task]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=_MAGI_ROOT,
                env=os.environ.copy(),
            )
            out = (proc.stdout or "").strip()
            if proc.returncode != 0 or not out:
                err = ((proc.stderr or out or "unknown").strip())[:300]
                return f"❌ 查詢失敗：{err}"
            return out
        except Exception as e:
            return f"❌ 查詢錯誤：{e}"

    def _run_embedding_web_search(self, message: str) -> str:
        """Web search dispatch for EmbeddingRouter (extracts topic from natural language)."""
        topic = str(message or "").strip()
        # Strip common command prefixes
        for kw in ["搜尋", "search", "research", "/search", "查一下", "找一下", "搜一下",
                    "google", "幫我搜", "幫我查一下", "執行網路研究", "進行網路研究",
                    "網路研究", "網路搜尋", "幫我查詢", "請幫我查詢", "幫我查", "幫我找",
                    "請", "幫我", "能不能", "可以", "一下", "幫忙", "@MAGI", "@magi"]:
            topic = re.sub(re.escape(kw), "", topic, flags=re.IGNORECASE).strip()
        topic = re.sub(r"^[:：]\s*", "", topic).strip()
        if len(topic) < 2:
            return "🔍 請告訴我要搜尋什麼主題。例如：'搜尋 AI agent 2024'"
        logger.info(f"🌐 EmbeddingRouter Web Search: {topic}")
        result = research_topic(topic, depth=3)
        if result.get("sources"):
            return self._summarize_web_results(topic, result)
        return f"🔍 找不到關於「{topic}」的資訊。"

    def _summarize_web_results(self, topic: str, result: dict) -> str:
        """Pass raw web search results through LLM to generate a human-readable summary."""
        # Build raw context from sources
        raw_parts = []
        for i, src in enumerate(result.get("sources", []), 1):
            title = src.get("title", "")[:80]
            url = src.get("url", "")
            preview = src.get("content_preview", "")[:500]
            raw_parts.append(f"[{i}] {title}\nURL: {url}\n{preview}")
        raw_context = "\n\n".join(raw_parts)
        # Also use combined_content if available (usually cleaner)
        combined = result.get("combined_content", "")[:3000]
        if combined:
            raw_context = combined

        prompt = f"""你是 MAGI 搜尋助理。根據以下網路搜尋結果，用繁體中文撰寫一份簡潔易讀的摘要回覆。

[搜尋主題]
{topic}

[搜尋結果原始資料]
{raw_context}

[回覆規則]
1. 直接回答使用者的問題，用 3-8 句話。
2. 只使用搜尋結果中的資訊，不要編造。
3. 如果搜尋結果包含數字、日期、溫度等具體數據，務必列出。
4. 最後附上 1-3 個最相關的參考來源連結。
5. 格式要簡潔清楚，適合在手機聊天軟體閱讀。
6. 不要使用 HTML 標籤、Markdown 語法（如 **粗體**、`程式碼`、### 標題）。純文字即可。
"""
        try:
            from skills.bridge.grounded_ai import _generate
            summary = _generate(prompt, temperature=0.2, timeout=120, num_ctx=4096)
            if summary and len(summary) > 10:
                return f"🔍 **{topic}**\n\n{summary}"
        except Exception as e:
            logger.warning(f"Web search LLM summarization failed: {e}")

        # Fallback: clean formatting without LLM
        response = f"🔍 **網路研究報告: {topic}**\n\n"
        for i, src in enumerate(result.get("sources", []), 1):
            title = src.get("title", "")[:50]
            url = src.get("url", "")
            response += f"{i}. {title}\n   {url}\n\n"
        return response

    def _run_transcribe_guidance(self, message: str) -> str:
        return "🎙️ 請上傳音訊檔，或在訊息中附上可讀取的音訊檔路徑後再要求逐字稿。"

    def _looks_like_capability_question(self, message: str) -> bool:
        text = str(message or "").strip()
        if not text:
            return False
        if not re.search(r"[嗎嘛呢？\?]$", text):
            return False
        if not re.search(r"(可以|可不可以|能不能|會不會|會|如何|怎麼|有沒有辦法|能否|可否)", text, re.IGNORECASE):
            return False
        has_payload = bool(
            re.search(r"https?://", text, re.IGNORECASE)
            or re.search(r"[A-Za-z]{4,}", text)
            or re.search(r"\d{4,}", text)
            or re.search(r"[。；;，,]", text)
        )
        return len(text) <= 36 or not has_payload

    def _dispatch_safe_semantic_skill(self, user_id, message: str, skill: str, role: str, platform: str) -> tuple[bool, str]:
        # Set context for lambda-based handlers in skill_loader
        self._last_dispatch_message = message
        self._last_dispatch_user_id = user_id

        # ── Capability guide (registry-based) ─────────────────────
        if self._skill_registry and self._looks_like_capability_question(message):
            guide = self._skill_registry.get_capability_guide(skill)
            if guide:
                return True, guide

        # ── Dispatch via SkillRegistry (plugin → direct → subprocess)
        if self._skill_registry:
            handled, reply = self._skill_registry.dispatch(
                skill, message,
                user_id=user_id, role=role,
                platform=platform, orchestrator=self,
            )
            if handled and reply:
                return True, reply
            if handled:
                return False, ""

        # ── Final subprocess fallback (if registry unavailable) ───
        return self._generic_skill_dispatch(skill, message)

    def _generic_skill_dispatch(self, skill: str, message: str) -> tuple[bool, str]:
        """
        Generic skill dispatcher: runs any skill that has an action.py via run_skill_action().
        Maps definitions.json skill names (e.g. 'web_search') to skill folder names (e.g. 'web-search').
        """
        try:
            from skills.evolution.skill_genesis import run_skill_action
        except ImportError:
            return False, ""

        folder_candidates = []
        try:
            definitions_path = os.path.join(os.path.dirname(__file__), "..", "skills", "definitions.json")
            if os.path.exists(definitions_path):
                with open(definitions_path, "r", encoding="utf-8") as f:
                    payload = json.load(f) or {}
                for tool in payload.get("tools") or []:
                    if not isinstance(tool, dict) or str(tool.get("name") or "").strip() != str(skill or "").strip():
                        continue
                    skill_prop = (((tool.get("parameters") or {}).get("properties") or {}).get("skill") or {})
                    default_folder = str(skill_prop.get("default") or "").strip()
                    if default_folder:
                        folder_candidates.append(default_folder)
                    break
        except Exception as def_err:
            logger.debug(f"generic dispatch: definition lookup failed for {skill}: {def_err}")

        folder_candidates.extend([
            skill.replace("_", "-"),
            skill,
            re.sub(r"^run[_-]+", "", skill.replace("_", "-")),
            re.sub(r"^run[_-]+", "", skill),
            f"{skill.replace('_', '-')}-tw",  # e.g. screenshot-sorter → screenshot-sorter-tw
        ])
        seen_folders = set()
        deduped_folders = []
        for item in folder_candidates:
            folder_name = str(item or "").strip()
            if not folder_name or folder_name in seen_folders:
                continue
            seen_folders.add(folder_name)
            deduped_folders.append(folder_name)

        skill_dirs = [
            os.path.join(os.path.dirname(__file__), "..", "skills", folder_name)
            for folder_name in deduped_folders
        ]
        found_dir = None
        for d in skill_dirs:
            if os.path.isdir(d) and os.path.exists(os.path.join(d, "action.py")):
                found_dir = os.path.basename(os.path.normpath(d))
                break

        if not found_dir:
            logger.debug(f"generic dispatch: no action.py for skill '{skill}'")
            return False, ""

        logger.info(f"🔧 Generic skill dispatch: {skill} → {found_dir}")
        self._ensure_runtime_foundations()
        started = time.perf_counter()
        action_path = os.path.join(os.path.dirname(__file__), "..", "skills", found_dir, "action.py")
        self._hook_bus.pre_tool(
            f"skill:{skill}",
            input_data={"message_preview": (message or "")[:200]},
            correlation_id=self._current_correlation_id(),
            metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
        )
        command_decision = self._permission_enforcer.evaluate_command(f"skill:{skill}")
        if not command_decision.allowed:
            blocked = f"⚠️ 權限策略已阻擋技能執行：{command_decision.reason}"
            self._hook_bus.post_tool(
                f"skill:{skill}",
                ok=False,
                status="denied",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error=blocked,
                correlation_id=self._current_correlation_id(),
                metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
            )
            return True, blocked
        path_decision = self._permission_enforcer.evaluate_path(action_path)
        if not path_decision.allowed:
            blocked = f"⚠️ 權限策略已阻擋技能執行：{path_decision.reason}"
            self._hook_bus.post_tool(
                f"skill:{skill}",
                ok=False,
                status="denied",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error=blocked,
                correlation_id=self._current_correlation_id(),
                metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
            )
            return True, blocked
        try:
            result = run_skill_action(
                found_dir,
                message,
                timeout_sec=60,
                auto_repair=False,
                auto_install_deps=True,
            )
            if result.get("success"):
                output = result.get("output", "").strip()
                if not output:
                    self._hook_bus.post_tool(
                        f"skill:{skill}",
                        output_data="✅ 技能執行完成。",
                        ok=True,
                        status="handled",
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        correlation_id=self._current_correlation_id(),
                        metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
                    )
                    return True, "✅ 技能執行完成。"
                polished = self._polish_skill_output(skill, message, output)
                self._hook_bus.post_tool(
                    f"skill:{skill}",
                    output_data=polished,
                    ok=True,
                    status="handled",
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    correlation_id=self._current_correlation_id(),
                    metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
                )
                return True, polished
            else:
                err = result.get("error", "unknown")
                logger.warning(f"generic dispatch failed for {skill}: {err}")
                self._hook_bus.post_tool(
                    f"skill:{skill}",
                    ok=False,
                    status="not_handled",
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    error=str(err),
                    correlation_id=self._current_correlation_id(),
                    metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
                )
                return False, ""
        except Exception as e:
            logger.warning(f"generic dispatch error for {skill}: {e}")
            self._hook_bus.post_tool(
                f"skill:{skill}",
                ok=False,
                status="error",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                error=str(e),
                correlation_id=self._current_correlation_id(),
                metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
            )
            return False, ""

    def _polish_skill_output(self, skill: str, user_message: str, raw_output: str) -> str:
        """
        Universal LLM polish layer for skill outputs.
        If the output is already clean and short, return as-is.
        Otherwise, pass through LLM to produce a human-readable summary.
        """
        # Skip polish for already-clean outputs
        if len(raw_output) < 200 and not self._output_looks_messy(raw_output):
            return raw_output

        # Truncate excessively long output before sending to LLM
        truncated = raw_output[:3000]

        prompt = f"""你是 MAGI 助理。以下是技能「{skill}」的原始執行結果。
請將它整理成簡潔、易讀的繁體中文回覆，適合在手機 LINE 上閱讀。

[使用者原始訊息]
{user_message}

[技能原始輸出]
{truncated}

[整理規則]
1. 保留所有重要資訊（數字、日期、名稱、結果）。
2. 移除 HTML 標籤、亂碼、debug 訊息、重複內容。
3. 用簡短段落或條列式呈現，不超過 10 行。
4. 如果原始輸出已經很乾淨，直接保留原文即可，不要畫蛇添足。
5. 不要加上「以下是整理後的結果」之類的前綴，直接給內容。
6. 不要使用 Markdown 語法（如 **粗體**、`程式碼`、### 標題）。純文字即可。
"""
        try:
            from skills.bridge.grounded_ai import _generate
            polished = _generate(prompt, temperature=0.15, timeout=90, num_ctx=4096)
            if polished and len(polished) > 10:
                return polished
        except Exception as e:
            logger.warning(f"Polish LLM failed for {skill}: {e}")

        # Fallback: return raw but with basic cleanup
        return self._basic_cleanup(raw_output)

    @staticmethod
    def _output_looks_messy(text: str) -> bool:
        """Check if output contains HTML, excessive whitespace, or garbled content."""
        if re.search(r"<[a-zA-Z][^>]*>", text):
            return True
        if re.search(r"\\n{3,}", text):
            return True
        if text.count("\n") > 15:
            return True
        # High ratio of non-CJK-non-ASCII noise
        noise = sum(1 for c in text if ord(c) > 127 and not ('\u4e00' <= c <= '\u9fff')
                     and not ('\u3000' <= c <= '\u303f') and not ('\uff00' <= c <= '\uffef'))
        if len(text) > 50 and noise / len(text) > 0.3:
            return True
        return False

    @staticmethod
    def _basic_cleanup(text: str) -> str:
        """Minimal cleanup without LLM: strip HTML tags, collapse whitespace."""
        cleaned = re.sub(r"<[^>]+>", "", text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = re.sub(r"[ \t]{3,}", " ", cleaned)
        return cleaned.strip()[:2000]

    def _try_safe_semantic_skill_route(self, user_id: str, message: str, role: str, platform: str) -> tuple[bool, str]:
        text = str(message or "").strip()
        if not text or text.startswith("/") or text.startswith("!") or text.startswith("@MAGI"):
            return False, ""
        if len(text) > 600:
            return False, ""

        safe_skills = {
            "web_search": "command",
            "translate_document": "command",
            "pdf_summarize": "command",
            "audio_transcribe": "command",
            "image_generate": "command",
            "judgment_search": "command",
            "run_judgment_collector": "command",
            "rss_subscribe": "command",
            "memory_search": "command",
            "transcript_query": "command",
            "pdf_annotate": "command",
            "stock_briefing": "command",
            "court_hearing": "command",
            "judgment_trend": "command",
            "labor_law_calc": "command",
            "tri_sage_translate": "command",
            "summarize_text": "command",
            "tri_sage_transcribe": "command",
        }
        min_conf = {
            "phrase": 0.30,
            "semantic": 0.36,
            "llm": 0.46,
        }

        try:
            from skills.bridge.semantic_router import route as _semantic_route, suggest_trigger
        except Exception:
            return False, ""

        try:
            sr = _semantic_route(text)
        except Exception as e:
            logger.debug(f"safe semantic route skipped: {e}")
            return False, ""
        if not sr:
            return False, ""

        skill = str(sr.get("skill") or "").strip()
        method = str(sr.get("method") or "semantic").strip()
        confidence = float(sr.get("confidence") or 0.0)
        if skill not in safe_skills:
            return False, ""
        if confidence < float(min_conf.get(method, 0.38)):
            return False, ""

        synthetic = suggest_trigger(skill, text)
        route_mode = safe_skills[skill]
        self._append_route_trace(
            str(user_id or ""),
            str(platform or ""),
            "semantic_primary",
            skill,
            {
                "confidence": confidence,
                "method": method,
                "route_mode": route_mode,
                "reason": str(sr.get("reason") or ""),
                "candidates": list(sr.get("candidates") or []),
            },
        )
        handled, direct_reply = self._dispatch_safe_semantic_skill(user_id, text, skill, role, platform)
        if handled:
            self._append_route_trace(
                str(user_id or ""),
                str(platform or ""),
                "semantic_primary_dispatch",
                skill,
                {"dispatch": "direct"},
            )
            return True, direct_reply or ""
        if route_mode == "query":
            reply = self._handle_query(user_id, text, platform_hint=platform)
            return (bool(reply), reply or "")

        reply = self._handle_command(user_id, synthetic, role=role, platform=platform)
        return (bool(reply), reply or "")

    def _run_nl_route(self, user_id: str, message: str, platform: str, role: str) -> tuple[bool, str]:
        """
        Route natural language to magi-office-ops commands.
        Returns (handled, reply_text).
        """
        if not self._nl_router_enabled():
            return False, ""
        if not self._should_try_nl_route(message):
            return False, ""

        code_dir = str(get_orch_dir())
        magi_dir = str(get_magi_root_dir())
        py = str(get_skill_python())
        if not py or not os.path.exists(py):
            py = sys.executable or "python3"

        router_script = os.environ.get(
            "MAGI_NL_ROUTER_SCRIPT",
            os.path.join(os.path.expanduser("~"), ".openclaw", "skills", "magi-office-ops", "intent_router.py"),
        ).strip()
        run_script = os.environ.get(
            "MAGI_NL_RUN_SCRIPT",
            os.path.join(os.path.expanduser("~"), ".openclaw", "skills", "magi-office-ops", "run.sh"),
        ).strip()

        if not (router_script and os.path.exists(router_script) and run_script and os.path.exists(run_script)):
            return False, ""

        try:
            r = subprocess.run(
                [py, router_script],
                input=message,
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("MAGI_NL_ROUTE_PARSE_TIMEOUT_SEC", "8") or "8"),
                cwd=code_dir,
            )
        except Exception as e:
            logger.warning(f"NL route parse skipped: {e}")
            return False, ""

        raw = (r.stdout or "").strip()
        if not raw:
            return False, ""
        try:
            route = json.loads(raw)
        except Exception:
            return False, ""

        if not isinstance(route, dict) or not route.get("ok"):
            return False, ""

        intent = str(route.get("intent") or "").strip()
        argv = route.get("argv") if isinstance(route.get("argv"), list) else []
        argv = [str(x) for x in argv if str(x).strip()]
        if not argv:
            return False, ""

        # Keep strict security posture on chat channels.
        user_safe_intents = {
            "system_status",
            "skills_check",
            "brain_status_model",
            "translate_full",
            "translate_summary",
            "translate_file",
            "quick_model_info",
            "quick_language",
            "quick_next_step",
            # --- OpenClaw external skills wiring ---
            "humanizer_apply",
            "whisper_transcribe",
            "automation_workflow_plan",
            "proactive_agent_guide",
            "self_improving_guide",
            # --- 法扶 (2026-03-01 開放一般使用者) ---
            "laf_monitor",
            "laf_closing",
            "laf_condition_draft",
            "laf_backfill",
            # --- 閱卷 ---
            "file_review_check",
            "file_review_preview",
            "file_review_check_downloadable",
            "file_review_downloadable",
            "file_review_download",
            "file_review_download_case",
            # --- 筆錄 ---
            "transcript_sync",
            "transcript_rename",
            "transcript_download_all",
            "transcript_download_case",
            "transcript_download_all_fallback",
            # --- 掃描/案件 ---
            "osc_scan_cases",
            "osc_queue_flush",
            "pdf_scan",
            # --- 股票晨報 ---
            "market_prompt",
            "market_set",
            "market_add",
            "market_remove",
            "market_list",
            "market_briefing",
            # --- 勞基法計算 ---
            "labor_law_calc",
            "labor_law_overtime",
            "labor_law_annual_leave",
            "labor_law_severance",
            # --- 法扶業務（完整開放）---
            "laf_go_live",
            "laf_fee",
            "laf_inquiry",
            "laf_withdrawal",
            # --- 判決/見解查詢 ---
            "judgment_search",
            "judgment_collect",
            "judgment_daily_crawl",
            # --- 其他業務 ---
            "db_backup",
            "calendar_sync",
            "autopilot_tick",
            "autopilot_nightly",
            "autopilot_self_test",
        }
        # 大腦操作：一般使用者可觸發但先通知管理員 (2026-03-01)
        brain_notify_intents = {"brain_repair", "brain_calibrate_ngl"}
        if role != "admin" and intent in brain_notify_intents:
            try:
                from skills.ops.red_phone import alert_admin
                alert_admin(
                    f"⚠️ 使用者 {user_id} ({platform}) 正在要求執行大腦操作：{intent}\n"
                    f"原始訊息：{message[:200]}",
                    severity="warning",
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1512, exc_info=True)
            # 通知後繼續執行，不阻擋
        elif role != "admin" and intent not in user_safe_intents:
            return True, self._postprocess_router_reply("⛔ 這個自然語句命令涉及系統流程，僅管理員可執行。", platform)

        env = os.environ.copy()
        env["MAGI_CODE_DIR"] = code_dir
        env["MAGI_ROOT_DIR"] = magi_dir
        env["MAGI_NO_DELETE"] = env.get("MAGI_NO_DELETE", "1") or "1"
        env["MAGI_PREFER_LOCAL_DB"] = env.get("MAGI_PREFER_LOCAL_DB", "0") or "0"

        timeout_sec = int(os.environ.get("MAGI_NL_ROUTE_EXEC_TIMEOUT_SEC", "300") or "300")
        async_timeout_sec = int(os.environ.get("MAGI_NL_ROUTE_ASYNC_TIMEOUT_SEC", "2400") or "2400")
        async_enabled = str(os.environ.get("MAGI_NL_ROUTE_ASYNC", "1")).strip().lower() in {"1", "true", "yes", "on"}
        async_intents = {
            "autopilot_tick",
            "autopilot_nightly",
            "autopilot_self_test",
            "laf_monitor",
            "laf_closing",
            "laf_condition_draft",
            "laf_backfill",
            "file_review_check",
            "file_review_preview",
            "file_review_check_downloadable",
            "file_review_downloadable",
            "file_review_download",
            "file_review_download_case",
            "transcript_sync",
            "transcript_rename",
            "transcript_download_all",
            "transcript_download_case",
            "transcript_download_all_fallback",
            "osc_scan_cases",
            "osc_queue_flush",
            "pdf_scan",
            "brain_repair",
            "brain_calibrate_ngl",
            "translate_file",
            "db_backup",
            "db_backup_restore",
        }

        if async_enabled and intent in async_intents:
            import threading

            def _run_background():
                try:
                    proc = subprocess.run(
                        [run_script] + argv,
                        capture_output=True,
                        text=True,
                        timeout=async_timeout_sec,
                        cwd=code_dir,
                        env=env,
                    )
                    out = (proc.stdout or "").strip()
                    err = (proc.stderr or "").strip()
                    if proc.returncode != 0:
                        tail = (err or out or "unknown error").strip()[-1200:]
                        msg = f"❌ 命令失敗：`{intent or 'unknown'}`\n{tail}"
                    elif out:
                        if len(out) > 1800:
                            msg = f"✅ 已完成：`{intent or 'command'}`\n（輸出較長，以下為尾段）\n{out[-1600:]}"
                        else:
                            msg = out
                    else:
                        msg = f"✅ 已完成：`{intent or 'command'}`"
                except subprocess.TimeoutExpired:
                    msg = f"⚠️ 自然語句命令逾時（>{async_timeout_sec}s）：`{intent or 'unknown'}`"
                except Exception as e:
                    msg = f"❌ 自然語句命令執行失敗：{e}"

                msg = self._postprocess_router_reply(msg, platform)
                try:
                    cb = getattr(self, "notification_callback", None)
                    if cb:
                        cb(str(user_id or ""), msg, str(platform or ""))
                except Exception as notify_err:
                    logger.warning(f"NL route async callback failed: {notify_err}")

            threading.Thread(target=_run_background, daemon=True).start()
            return True, self._postprocess_router_reply(
                f"⏳ 已開始執行：`{intent or 'command'}`。完成後我會主動回報結果。",
                platform,
            )

        try:
            proc = subprocess.run(
                [run_script] + argv,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=code_dir,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return True, self._postprocess_router_reply(f"⚠️ 自然語句命令逾時（>{timeout_sec}s）：`{intent or 'unknown'}`", platform)
        except Exception as e:
            return True, self._postprocess_router_reply(f"❌ 自然語句命令執行失敗：{e}", platform)

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if proc.returncode != 0:
            tail = (err or out or "unknown error").strip()
            tail = tail[-1200:]
            return True, self._postprocess_router_reply(f"❌ 命令失敗：`{intent or 'unknown'}`\n{tail}", platform)

        if out:
            # Keep output concise on chat channels; server/discord layer will chunk/export if needed.
            if len(out) > 1800:
                return True, self._postprocess_router_reply(
                    f"✅ 已執行：`{intent or 'command'}`\n（輸出較長，以下為尾段）\n{out[-1600:]}",
                    platform,
                )
            return True, self._postprocess_router_reply(out, platform)

        return True, self._postprocess_router_reply(f"✅ 已執行：`{intent or 'command'}`", platform)

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

    def _export_translation_txt(self, *, translated_text: str, source: str, provider: str, mode: str, prefix: str, user_id: str) -> str | None:
        return _get_handler("dh").export_translation_txt(translated_text=translated_text, source=source, provider=provider, mode=mode, prefix=prefix, user_id=user_id)

    def _export_translation_docx(self, *, source_text: str, translated_text: str, source_chunks: list | None = None, translated_chunks: list | None = None, title: str = "", subtitle: str = "", prefix: str = "translate", user_id: str) -> str | None:
        return _get_handler("dh").export_translation_docx(source_text=source_text, translated_text=translated_text, source_chunks=source_chunks, translated_chunks=translated_chunks, title=title, subtitle=subtitle, prefix=prefix, user_id=user_id)

    def _export_plain_txt(self, *, content: str, prefix: str, user_id: str, title: str = "📄 已輸出 TXT 檔案。") -> str | None:
        return _get_handler("dh").export_plain_txt(content=content, prefix=prefix, user_id=user_id, title=title)

    def _export_plain_docx(self, *, segments: list, mode: str = "transcript", title: str = "", case_info: str = "", prefix: str = "export", user_id: str) -> str | None:
        return _get_handler("dh").export_plain_docx(segments=segments, mode=mode, title=title, case_info=case_info, prefix=prefix, user_id=user_id)

    def _export_summary_docx_or_txt(self, summary_text: str, *, prefix: str, title: str, user_id: str, source_path: str = "") -> str | None:
        """摘要輸出：優先 DOCX 原文／摘要對照表格，fallback TXT。"""
        import re as _re
        # 嘗試提取原文，與摘要做對照表格（bilingual 模式，左原文右摘要）
        src_text = ""
        if source_path:
            try:
                extracted = self._extract_text_from_uploaded_file(source_path)
                if extracted.get("success"):
                    src_text = str(extracted.get("text") or "").strip()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1681, exc_info=True)
        if src_text and summary_text:
            try:
                from skills.ops.export_docx import export_bilingual_docx
                from api.handlers.document_handler import is_file_protocol_user
                # 原文：依頁面標記 (--- 第 X 頁 ---) 或大段落分段
                _page_pattern = r"---\s*第\s*\d+\s*頁\s*---"
                if _re.search(_page_pattern, src_text):
                    # 有頁面標記 → 以頁為單位
                    _raw_pages = _re.split(_page_pattern, src_text)
                    src_chunks = [p.strip() for p in _raw_pages if p.strip()]
                else:
                    # 無頁面標記 → 依雙換行分段，再合併短段落（每段至少 800 字）
                    _raw = [p.strip() for p in _re.split(r"\n{2,}", src_text) if p.strip()]
                    src_chunks = []
                    _buf = []
                    _buf_len = 0
                    for p in _raw:
                        _buf.append(p)
                        _buf_len += len(p)
                        if _buf_len >= 800:
                            src_chunks.append("\n\n".join(_buf))
                            _buf, _buf_len = [], 0
                    if _buf:
                        src_chunks.append("\n\n".join(_buf))
                # 摘要：依 markdown 標題 / 數字編號 / 項目符號 / 雙換行分段
                sum_chunks = [p.strip() for p in _re.split(
                    r"\n(?=#{1,3}\s|(?:\d+[\.\、])|(?:[-\*]\s))|(?:\n{2,})",
                    summary_text.strip(),
                ) if p.strip()]
                # 去掉摘要開頭的純標題行（如 "📄 **PDF 摘要**"）
                while sum_chunks and _re.match(r"^[📄📚🌐\*#\s]+$", sum_chunks[0].strip().replace("*", "")):
                    sum_chunks.pop(0)
                # 配對：較長的一方決定行數
                max_rows = max(len(src_chunks), len(sum_chunks), 1)
                while len(src_chunks) < max_rows:
                    src_chunks.append("")
                while len(sum_chunks) < max_rows:
                    sum_chunks.append("")
                pages = [
                    {"page": i + 1, "source": s, "target": t}
                    for i, (s, t) in enumerate(zip(src_chunks, sum_chunks))
                    if s.strip() or t.strip()
                ]
                if pages:
                    ex = export_bilingual_docx(
                        pages, title=title, header_text=title,
                        prefix=prefix,
                        col_labels={"col1": "段落", "col2": "原文", "col3": "摘要"},
                    )
                    if isinstance(ex, dict) and ex.get("success"):
                        path = str(ex.get("path") or "").strip()
                        url = str(ex.get("url") or "").strip()
                        head = "📄 已輸出原文／摘要對照 DOCX 表格檔案。"
                        if url:
                            head = f"{head}\n{url}"
                        if is_file_protocol_user(user_id) and path:
                            return f"{head}|||FILE_PATH|||{path}"
                        return f"{head}\n{path}".strip()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1741, exc_info=True)
        # Fallback: summary-only DOCX table
        sections = []
        parts = _re.split(r"\n(?=#{1,3}\s|(?:\d+[\.\、]))", summary_text.strip())
        for i, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            heading_match = _re.match(r"^#{1,3}\s*(.+?)$", part, _re.MULTILINE)
            num_match = _re.match(r"^(\d+[\.\、])\s*(.+?)$", part, _re.MULTILINE)
            if heading_match:
                heading = heading_match.group(1).strip()
                body = part[heading_match.end():].strip()
            elif num_match:
                heading = num_match.group(0).split("\n")[0].strip()
                body = "\n".join(part.split("\n")[1:]).strip() or part.strip()
            else:
                heading = f"段落 {i + 1}" if len(parts) > 1 else ""
                body = part
            sections.append({"heading": heading, "summary": body, "excerpt": ""})
        if sections:
            exported = self._export_plain_docx(
                segments=sections, mode="summary",
                title=title, prefix=prefix, user_id=user_id,
            )
            if exported:
                return exported
        return self._export_plain_txt(
            content=summary_text, prefix=prefix,
            user_id=user_id, title=f"📄 已輸出{title}摘要 TXT 檔案。",
        )

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

    def _translate_text_complete(self, text: str, source_lang: str = "auto", target_lang: str = "繁體中文") -> dict:
        task_id = f"translate_{id(text)}_{time.time():.0f}"
        self.register_heavy_task(task_id, "翻譯")
        try:
            return _get_handler("tr").translate_text_complete(text, source_lang=source_lang, target_lang=target_lang)
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
        low = str(message or "").strip().lower()
        return low in {
            "就這樣", "照預設", "照預設即可", "預設", "ok", "okay", "yes", "y",
            "auto", "好", "可以", "都可以", "你決定", "照鐵穹預設", "略過",
        }

    @staticmethod
    def _skill_interview_cancel_reply(message: str) -> bool:
        low = str(message or "").strip().lower()
        return low in {"取消", "先不要", "停止", "stop", "cancel", "算了"}

    @staticmethod
    def _skill_interview_status_reply(message: str) -> bool:
        low = str(message or "").strip().lower()
        return low in {"目前進度", "進度", "skill 狀態", "技能狀態", "訪談狀態", "status"}

    @staticmethod
    def _skill_interview_split_items(text: str, limit: int = 8) -> list[str]:
        raw = str(text or "").strip()
        if not raw:
            return []
        parts = re.split(r"[\n,，、;；/|]+", raw)
        out = []
        seen = set()
        for part in parts:
            item = str(part or "").strip(" -•\t")
            if not item:
                continue
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(item[:120])
            if len(out) >= limit:
                break
        return out

    def _parse_skill_interview_io(self, message: str) -> tuple[list[str], list[str]]:
        text = str(message or "").strip()
        if not text:
            return [], []
        inputs = []
        outputs = []
        match_in = re.search(r"輸入\s*[:：]\s*(.*?)(?=\s*輸出\s*[:：]|$)", text, re.IGNORECASE | re.DOTALL)
        match_out = re.search(r"輸出\s*[:：]\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
        if match_in:
            inputs = self._skill_interview_split_items(match_in.group(1))
        if match_out:
            outputs = self._skill_interview_split_items(match_out.group(1))
        if not inputs and not outputs and "->" in text:
            left, right = text.split("->", 1)
            inputs = self._skill_interview_split_items(left)
            outputs = self._skill_interview_split_items(right)
        if not inputs and not outputs:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if len(lines) >= 2:
                inputs = self._skill_interview_split_items(lines[0])
                outputs = self._skill_interview_split_items(" ".join(lines[1:]))
        return inputs, outputs

    def _format_skill_interview_progress(self, entry: dict) -> str:
        draft = entry.get("draft") if isinstance(entry.get("draft"), dict) else {}
        step = int(entry.get("step") or 0)
        total = 5
        lines = [f"🧩 SKILL 訪談進行中（{min(step + 1, total)}/{total}）"]
        if draft.get("purpose"):
            lines.append(f"目標：{str(draft.get('purpose'))[:120]}")
        if draft.get("trigger_examples"):
            lines.append("觸發詞：" + "、".join([str(x) for x in (draft.get("trigger_examples") or [])[:4]]))
        if draft.get("inputs"):
            lines.append("輸入：" + "、".join([str(x) for x in (draft.get("inputs") or [])[:3]]))
        if draft.get("outputs"):
            lines.append("輸出：" + "、".join([str(x) for x in (draft.get("outputs") or [])[:3]]))
        return "\n".join(lines)

    def _render_skill_interview_question(self, entry: dict) -> str:
        draft = entry.get("draft") if isinstance(entry.get("draft"), dict) else {}
        step = int(entry.get("step") or 0)
        total = 5
        reason = str(entry.get("trigger_reason") or "manual")
        intro = (
            "🧩 我判斷這個需求目前沒有現成 SKILL 可穩定接手，先用 5 題問答幫你補一個新 SKILL。"
            if reason == "gap"
            else "🧩 我們先用 5 題問答把這個新 SKILL 定義清楚，完成後我會直接寫進 MAGI。"
        )
        footer = "\n回覆 `取消` 可終止，回覆 `目前進度` 可查看草稿。"
        if step == 0:
            return (
                f"{intro}\n\n"
                f"Q1/{total} 目標確認\n"
                f"我先抓成：\n{draft.get('purpose')}\n\n"
                "如果正確回 `就這樣`，要修改就直接改寫成你要的目標。"
                f"{footer}"
            )
        if step == 1:
            triggers = "、".join([str(x) for x in (draft.get("trigger_examples") or [])[:3]])
            return (
                f"Q2/{total} 觸發方式\n"
                f"你通常會怎麼叫這類任務？請給我 2-5 個觸發詞或例句。\n"
                f"預設我會先用：{triggers}\n\n"
                "不知道可回 `照預設`。"
                f"{footer}"
            )
        if step == 2:
            return (
                f"Q3/{total} 輸入與輸出\n"
                "這個 SKILL 通常會收到哪些輸入、要回哪些輸出？\n"
                "可直接用：`輸入：... / 輸出：...`。\n\n"
                "不知道可回 `照預設`。"
                f"{footer}"
            )
        if step == 3:
            guards = "、".join([str(x) for x in (draft.get("guardrails") or [])[:3]])
            return (
                f"Q4/{total} 邊界與禁區\n"
                "有沒有特殊邊界？例如只能先草稿、不能自動送出、不能碰外網、要先問你確認。\n"
                f"目前預設：{guards}\n\n"
                "沒有就回 `照鐵穹預設`。"
                f"{footer}"
            )
        return (
            f"Q5/{total} 技能名稱\n"
            f"最後，這個 SKILL 想叫什麼名字？目前暫名：{draft.get('display_name')}\n\n"
            "你可以直接給中文或英文名稱；不知道就回 `照預設`。"
            f"{footer}"
        )

    def _start_skill_interview(self, user_id: str, platform: str, role: str, initial_request: str, trigger_reason: str = "manual") -> str:
        if role != "admin":
            return "⛔ 這個需求看起來需要新增 SKILL，但目前只有管理員可以正式寫入 MAGI。"
        from skills.management.skill_interview import infer_skill_defaults

        pending = self._load_skill_interview_pending()
        key = self._pending_key(user_id, platform)
        draft = infer_skill_defaults(initial_request)
        pending[key] = {
            "kind": "skill_interview",
            "user_id": str(user_id or "").strip(),
            "platform": str(platform or "").strip(),
            "role": str(role or "user").strip(),
            "trigger_reason": str(trigger_reason or "manual").strip(),
            "initial_request": str(initial_request or "").strip()[:2000],
            "draft": draft,
            "step": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "expires_at": time.time() + float(os.environ.get("MAGI_SKILL_INTERVIEW_TTL_SEC", "5400")),
        }
        self._save_skill_interview_pending(pending)
        self._append_route_trace(
            str(user_id or ""),
            str(platform or ""),
            "skill_interview",
            "started",
            {"trigger_reason": str(trigger_reason or "manual"), "preview": str(initial_request or "")[:80]},
        )
        return self._render_skill_interview_question(pending[key])

    def start_skill_interview(self, user_id: str, platform: str, role: str, initial_request: str, trigger_reason: str = "manual") -> str:
        return self._start_skill_interview(user_id, platform, role, initial_request, trigger_reason=trigger_reason)

    def _finalize_skill_interview(self, user_id: str, platform: str, entry: dict) -> str:
        from skills.management.skill_interview import create_skill_from_interview

        result = create_skill_from_interview(
            str(entry.get("initial_request") or ""),
            entry.get("draft") if isinstance(entry.get("draft"), dict) else {},
            requested_by=f"{platform}:{user_id}",
        )
        if not result.get("success"):
            violations = result.get("violations") or []
            if violations:
                return "🛡️ 這次 SKILL 生成被 Iron Dome 擋下，原因：\n- " + "\n- ".join([str(v) for v in violations[:4]])
            return f"❌ 新 SKILL 生成失敗：{result.get('error', 'unknown')}"

        ci = result.get("ci") if isinstance(result.get("ci"), dict) else {}
        ci_ok = bool(ci.get("success"))
        definition = result.get("definition") if isinstance(result.get("definition"), dict) else {}
        profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
        snapshot = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else {}
        triggers = "、".join([str(x) for x in (profile.get("trigger_examples") or [])[:3]])
        lines = [
            "🧬 新 SKILL 已建立並啟用",
            f"名稱：{result.get('display_name')}",
            f"資料夾：`{result.get('skill_name')}`",
            f"路徑：`{result.get('skill_path')}`",
            f"觸發詞：{triggers or '已依描述建立'}",
            f"版本快照：`{snapshot.get('version_id') or 'n/a'}`",
            f"註冊：{'✅' if definition.get('success') else '⚠️'} definitions.json",
            f"驗證：{'✅' if ci_ok else '⚠️'} smoke / CI",
            "檔案：SKILL.md、action.py、skill_profile.json",
            "",
            "之後你可以直接用這類句子測它：",
            f"- {str((profile.get('trigger_examples') or ['直接描述你的任務'])[0])}",
        ]
        if not ci_ok:
            lines.append(f"CI 補充：{str(ci.get('checks') or '')[:240]}")
        self._append_route_trace(
            str(user_id or ""),
            str(platform or ""),
            "skill_interview",
            "finalized",
            {"skill_name": str(result.get("skill_name") or ""), "ci_ok": ci_ok},
        )
        return "\n".join(lines).strip()

    def _handle_skill_interview_if_any(self, user_id: str, platform: str, role: str, message: str) -> tuple[bool, str]:
        msg = str(message or "").strip()
        if not msg:
            return False, ""
        pending = self._load_skill_interview_pending()
        key = self._pending_key(user_id, platform)
        entry = pending.get(key) if isinstance(pending, dict) else None
        if not isinstance(entry, dict):
            return False, ""

        now = time.time()
        exp = float(entry.get("expires_at", 0.0) or 0.0)
        if exp and now > exp:
            pending.pop(key, None)
            self._save_skill_interview_pending(pending)
            return True, "⏱️ 剛剛那筆 SKILL 訪談已過期。你可以再描述一次需求，我會重新開始。"

        if self._skill_interview_cancel_reply(msg):
            pending.pop(key, None)
            self._save_skill_interview_pending(pending)
            return True, "🛑 已取消這次 SKILL 訪談，不會寫入新技能。"

        if self._skill_interview_status_reply(msg):
            return True, self._format_skill_interview_progress(entry) + "\n\n" + self._render_skill_interview_question(entry)

        if role != "admin":
            pending.pop(key, None)
            self._save_skill_interview_pending(pending)
            return True, "⛔ 這筆 SKILL 訪談需要管理員權限才能完成。"

        draft = entry.get("draft") if isinstance(entry.get("draft"), dict) else {}
        step = int(entry.get("step") or 0)
        use_default = self._skill_interview_default_reply(msg)

        if step == 0 and not use_default:
            draft["purpose"] = msg[:500]
        elif step == 1 and not use_default:
            triggers = self._skill_interview_split_items(msg)
            if triggers:
                draft["trigger_examples"] = triggers
        elif step == 2 and not use_default:
            inputs, outputs = self._parse_skill_interview_io(msg)
            if inputs:
                draft["inputs"] = inputs
            if outputs:
                draft["outputs"] = outputs
            if not inputs and not outputs:
                draft["outputs"] = self._skill_interview_split_items(msg)
        elif step == 3 and not use_default:
            guardrails = self._skill_interview_split_items(msg)
            if guardrails:
                draft["guardrails"] = guardrails
        elif step == 4 and not use_default:
            draft["display_name"] = msg[:60]

        entry["draft"] = draft
        entry["step"] = step + 1
        entry["updated_at"] = now
        entry["expires_at"] = now + float(os.environ.get("MAGI_SKILL_INTERVIEW_TTL_SEC", "5400"))

        if entry["step"] >= 5:
            pending.pop(key, None)
            self._save_skill_interview_pending(pending)
            return True, self._finalize_skill_interview(user_id, platform, entry)

        pending[key] = entry
        self._save_skill_interview_pending(pending)
        return True, self._render_skill_interview_question(entry)

    def reply_skill_interview(self, user_id: str, platform: str, role: str, message: str) -> tuple[bool, str]:
        return self._handle_skill_interview_if_any(user_id, platform, role, message)

    def get_skill_interview_state(self, user_id: str, platform: str) -> dict:
        pending = self._load_skill_interview_pending()
        key = self._pending_key(user_id, platform)
        entry = pending.get(key) if isinstance(pending, dict) else None
        if not isinstance(entry, dict):
            return {"active": False, "step": 0, "total_steps": 5, "prompt": "", "draft": {}}

        now = time.time()
        exp = float(entry.get("expires_at", 0.0) or 0.0)
        if exp and now > exp:
            pending.pop(key, None)
            self._save_skill_interview_pending(pending)
            return {"active": False, "step": 0, "total_steps": 5, "prompt": "", "draft": {}}

        step = int(entry.get("step") or 0)
        return {
            "active": True,
            "step": min(step + 1, 5),
            "total_steps": 5,
            "trigger_reason": str(entry.get("trigger_reason") or "manual"),
            "initial_request": str(entry.get("initial_request") or ""),
            "draft": entry.get("draft") if isinstance(entry.get("draft"), dict) else {},
            "prompt": self._render_skill_interview_question(entry),
            "updated_at": float(entry.get("updated_at") or 0.0),
        }

    def _load_recent_attachments(self) -> dict:
        with self._state_cache_lock:
            return dict(self._recent_attachments_cache)

    def _save_recent_attachments(self, data: dict) -> None:
        with self._state_cache_lock:
            self._recent_attachments_cache = data if isinstance(data, dict) else {}
            self._state_dirty.add("recent_attachments")
        self._schedule_state_flush()

    def _prune_recent_attachments(self, data: dict) -> dict:
        ttl_sec = int(os.environ.get("MAGI_RECENT_ATTACHMENT_TTL_SEC", "21600") or "21600")
        now = time.time()
        out = {}
        for key, entry in (data or {}).items():
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip()
            if not path or not os.path.exists(path):
                continue
            try:
                ts = float(entry.get("timestamp") or 0.0)
            except Exception:
                ts = 0.0
            if ts and (now - ts > max(600, ttl_sec)):
                continue
            out[str(key)] = entry
        return out

    def remember_recent_attachment(self, *, user_id: str, platform: str, attachment: dict, source_message: str = "") -> dict:
        kind = str((attachment or {}).get("type") or "").strip().lower()
        path = str((attachment or {}).get("path") or "").strip()
        if kind not in {"file", "audio", "image"} or not path or not os.path.exists(path):
            return {}
        data = self._prune_recent_attachments(self._load_recent_attachments())
        key = self._pending_key(user_id, platform)
        entry = {
            "user_id": str(user_id or "").strip(),
            "platform": str(platform or "").strip(),
            "type": kind,
            "path": path,
            "filename": str((attachment or {}).get("filename") or os.path.basename(path) or "").strip(),
            "timestamp": float((attachment or {}).get("timestamp") or time.time()),
            "source_message": str(source_message or "").strip()[:2000],
        }
        data[key] = entry
        self._save_recent_attachments(data)
        return entry

    def _get_recent_attachment(self, user_id: str, platform: str) -> dict:
        data = self._prune_recent_attachments(self._load_recent_attachments())
        self._save_recent_attachments(data)
        return data.get(self._pending_key(user_id, platform)) or {}

    def _looks_like_attachment_followup(self, message: str, attachment_type: str = "") -> bool:
        s = str(message or "").strip().lower()
        if not s:
            return False
        direct_hits = [
            "翻譯", "translate", "翻成", "全文", "整篇", "整份", "整個檔案", "完整翻譯", "全文翻譯",
            "不要摘要", "摘要", "總結", "重點", "關鍵段落", "逐字稿", "時間戳", "附件", "檔案", "文件",
            "pdf", "docx", "txt", "epub", "回到剛剛", "剛剛那份", "那份文件", "那個檔案",
        ]
        if any(hit in s for hit in direct_hits):
            return True
        if attachment_type == "file":
            return s in {"要整篇全文", "整篇全文", "要全文", "全文", "全部", "完整的", "整份"}
        if attachment_type == "audio":
            return s in {"逐字稿", "要逐字稿", "全文逐字稿", "要全文", "全文", "加時間戳", "要時間戳"}
        return False

    def has_recent_attachment_followup(self, user_id: str, platform: str, message: str) -> bool:
        recent = self._get_recent_attachment(str(user_id or ""), str(platform or ""))
        if not recent:
            return False
        return self._looks_like_attachment_followup(message, str(recent.get("type") or ""))

    def _maybe_reuse_recent_attachment(self, user_id: str, platform: str, message: str) -> dict | None:
        recent = self._get_recent_attachment(str(user_id or ""), str(platform or ""))
        if not recent:
            return None
        kind = str(recent.get("type") or "").strip().lower()
        if not self._looks_like_attachment_followup(message, kind):
            return None
        path = str(recent.get("path") or "").strip()
        if not path or not os.path.exists(path):
            return None
        logger.info(
            "♻️ Reusing recent attachment for follow-up: user=%s platform=%s type=%s file=%s",
            user_id,
            platform,
            kind,
            os.path.basename(path),
        )
        return {
            "type": kind,
            "path": path,
            "filename": str(recent.get("filename") or os.path.basename(path) or "").strip(),
            "timestamp": float(recent.get("timestamp") or time.time()),
            "reused_recent": True,
        }

    def _load_laf_submit_pending(self) -> dict:
        try:
            if os.path.exists(self._laf_submit_pending_file):
                with open(self._laf_submit_pending_file, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    def _save_laf_submit_pending(self, data: dict) -> None:
        try:
            tmp = self._laf_submit_pending_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data if isinstance(data, dict) else {}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._laf_submit_pending_file)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2335, exc_info=True)

    def _update_laf_status_after_action(self, *, case_number: str = "", client_name: str = "",
                                           laf_case_no: str = "",
                                           case_reason_hint: str = "",
                                           new_status: str, action_label: str = "") -> bool:
        """法扶操作成功後回寫 DB legal_aid_status。"""
        try:
            from api.runtime_paths import get_config_path
            from osc import DatabaseManager
            config_path = get_config_path("config.json")
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
            db = None
            for profile in config.get("mariadb_profiles", []):
                try:
                    db = DatabaseManager(profile["config"])
                    break
                except Exception:
                    continue
            if not db:
                return False
            # 用 case_number、laf_case_no 或 client_name 找到案件
            rows = []
            if case_number:
                rows = db.fetch_all(
                    "SELECT id, case_number, client_name, legal_aid_status, case_reason FROM cases WHERE case_number = %s",
                    (case_number,), as_dict=True,
                ) or []
            if not rows and laf_case_no:
                rows = db.fetch_all(
                    "SELECT id, case_number, client_name, legal_aid_status, case_reason FROM cases "
                    "WHERE (legal_aid_number = %s OR laf_case_no = %s OR application_no = %s) LIMIT 1",
                    (laf_case_no, laf_case_no, laf_case_no), as_dict=True,
                ) or []
            if not rows and client_name:
                rows = db.fetch_all(
                    "SELECT id, case_number, client_name, legal_aid_status, legal_aid_number, case_reason FROM cases "
                    "WHERE client_name LIKE %s AND (case_category='法律扶助案件' OR case_reason LIKE '%%法扶%%') "
                    "ORDER BY case_number DESC",
                    (f"%{client_name}%",), as_dict=True,
                ) or []
            if not rows:
                return False
            # 多筆同名案件 + 有案由提示 → 用案由篩選
            if len(rows) > 1 and case_reason_hint:
                from api.handlers.laf_handler import _expand_reason_keywords
                keywords = _expand_reason_keywords(case_reason_hint)
                if keywords:
                    filtered = [
                        r for r in rows
                        if any(kw in (r.get("case_reason") or "") for kw in keywords)
                    ]
                    if len(filtered) == 1:
                        rows = filtered
                    elif filtered:
                        rows = filtered  # 縮小範圍，下面再判斷是否仍有歧義
            # 多筆同名案件：自動操作用最新的，手動操作回傳歧義提示
            if len(rows) > 1 and action_label.startswith("手動"):
                lines = [f"⚠️ 找到 {len(rows)} 件「{client_name}」的法扶案件，請指定案號或加上案由："]
                for r in rows:
                    laf = r.get("legal_aid_number") or ""
                    reason = r.get("case_reason") or ""
                    lines.append(f"  • {r['case_number']} {r['client_name']} {reason} ({laf}) — {r.get('legal_aid_status') or '(空)'}")
                status_word = new_status.replace("進行中", "開辦").replace("已報結", "報結")
                lines.append(f"\n範例：`{rows[0]['case_number']} 已{status_word}` 或 `{client_name} {(rows[0].get('case_reason') or '案由')[:6]} 已{status_word}`")
                self._ambiguous_laf_status_hint = "\n".join(lines)
                return False
            row = rows[0]
            old = row.get("legal_aid_status") or "(空)"
            db.execute_write(
                "UPDATE cases SET legal_aid_status = %s WHERE id = %s",
                (new_status, row["id"]),
            )
            logger.info("📝 %s → DB legal_aid_status「%s」→「%s」（%s %s）",
                        action_label, old, new_status, row.get("case_number"), row.get("client_name"))
            return True
        except Exception as e:
            logger.warning("_update_laf_status_after_action failed: %s", e)
            return False

    def _register_laf_go_live_submit_pending(self, *, platform: str, requester_user_id: str, payload: dict, result_data: dict) -> dict:
        pending = self._load_laf_submit_pending()
        token = secrets.token_hex(3).upper()
        expires_sec = int(os.environ.get("MAGI_LAF_GO_LIVE_CONFIRM_TTL_SEC", "1800") or "1800")
        now = time.time()
        entry = {
            "kind": "laf_go_live_submit",
            "token": token,
            "platform": str(platform or "").strip(),
            "requester_user_id": str(requester_user_id or "").strip(),
            "created_at": now,
            "expires_at": now + float(expires_sec),
            "status": "pending",
            "payload": payload or {},
            "result_data": result_data or {},
        }
        pending[token] = entry
        self._save_laf_submit_pending(pending)
        return entry

    def _resolve_laf_go_live_pending_token(self, platform: str, message: str) -> tuple[str, dict]:
        pending = self._load_laf_submit_pending()
        msg = (message or "").strip()
        platform_norm = str(platform or "").strip().lower()

        # remove expired first
        now = time.time()
        removed = []
        for tk, e in list(pending.items()):
            if not isinstance(e, dict):
                removed.append(tk)
                continue
            exp = float(e.get("expires_at", 0.0) or 0.0)
            if exp and now > exp:
                removed.append(tk)
        if removed:
            for tk in removed:
                pending.pop(tk, None)
            self._save_laf_submit_pending(pending)

        # token in message
        m = re.search(r"\b([A-F0-9]{6,12})\b", msg.upper())
        if m:
            tk = m.group(1)
            e = pending.get(tk)
            if isinstance(e, dict):
                if str(e.get("kind")) != "laf_go_live_submit":
                    return "", {}
                if str(e.get("status")) != "pending":
                    return "", {}
                if str(e.get("platform", "")).strip().lower() != platform_norm:
                    return "", {}
                return tk, e

        # fallback: only one pending on same platform
        cands = []
        for tk, e in pending.items():
            if not isinstance(e, dict):
                continue
            if str(e.get("kind")) != "laf_go_live_submit":
                continue
            if str(e.get("status")) != "pending":
                continue
            if str(e.get("platform", "")).strip().lower() != platform_norm:
                continue
            cands.append((tk, e))
        if len(cands) == 1:
            return cands[0]

        return "", {}

    def _handle_laf_submit_confirmation_if_any(self, user_id: str, platform: str, role: str, message: str) -> tuple[bool, str]:
        msg = (message or "").strip()
        if not msg:
            return False, ""

        low = msg.lower()
        has_confirm_kw = any(k in low for k in ["正確", "確認", "ok", "可以送出", "送出"])
        has_cancel_kw = any(k in low for k in ["取消", "不要送出", "先不要", "暫停送出"])
        has_laf_kw = any(k in msg for k in ["開辦", "法扶", "回報"])
        has_token = bool(re.search(r"\b([A-F0-9]{6,12})\b", msg.upper()))

        # not a confirmation-like message
        if not (has_confirm_kw or has_cancel_kw or has_token):
            return False, ""
        if not (has_laf_kw or has_token):
            tk_probe, _e_probe = self._resolve_laf_go_live_pending_token(platform, "")
            if not tk_probe:
                return False, ""

        token, entry = self._resolve_laf_go_live_pending_token(platform, msg)
        if not token or not isinstance(entry, dict):
            # If user provided an explicit token, return deterministic status.
            m = re.search(r"\b([A-F0-9]{6,12})\b", msg.upper())
            if m:
                tk = m.group(1)
                pending0 = self._load_laf_submit_pending()
                e0 = pending0.get(tk) if isinstance(pending0, dict) else None
                if isinstance(e0, dict) and str(e0.get("kind")) == "laf_go_live_submit":
                    platform_ok = str(e0.get("platform", "")).strip().lower() == str(platform or "").strip().lower()
                    if not platform_ok:
                        return True, f"⚠️ 確認碼 {tk} 屬於其他通訊平台，請在原平台回覆。"
                    return True, f"⚠️ 這筆開辦送出確認目前狀態為「{e0.get('status')}」，不能再次送出。"
            return False, ""

        pending = self._load_laf_submit_pending()
        ent = pending.get(token) if isinstance(pending, dict) else None
        if not isinstance(ent, dict):
            return True, "⚠️ 這筆開辦送出確認已不存在或已過期，請重新執行開辦填寫流程。"
        if str(ent.get("status")) != "pending":
            return True, f"⚠️ 這筆開辦送出確認目前狀態為「{ent.get('status')}」，不能重複送出。"

        allow_colleague = str(os.environ.get("MAGI_LAF_ALLOW_COLLEAGUE_CONFIRM", "1")).strip().lower() in {"1", "true", "yes", "on"}
        if role != "admin" and not allow_colleague:
            return True, "⛔ 目前只允許管理員確認送出。"

        if has_cancel_kw:
            ent["status"] = "cancelled"
            ent["cancelled_by"] = str(user_id or "")
            ent["cancelled_at"] = time.time()
            pending[token] = ent
            self._save_laf_submit_pending(pending)
            return True, f"🛑 已取消開辦送出（確認碼 {token}）。"

        # confirm -> submit in background
        ent["status"] = "submitting"
        ent["confirmed_by"] = str(user_id or "")
        ent["confirmed_at"] = time.time()
        pending[token] = ent
        self._save_laf_submit_pending(pending)

        import subprocess
        import threading

        skill_python = (os.environ.get("MAGI_SKILL_PYTHON") or "").strip()
        if not skill_python:
            skill_python = f"{_MAGI_ROOT}/venv/bin/python"
        if not os.path.exists(skill_python):
            skill_python = sys.executable or "python3"

        laf_script = str(get_laf_script())
        payload = ent.get("payload") if isinstance(ent.get("payload"), dict) else {}
        result_data = ent.get("result_data") if isinstance(ent.get("result_data"), dict) else {}

        def _run_submit(uid: str, platform_name: str, token_id: str, payload_obj: dict, result_obj: dict):
            cmd = [skill_python, laf_script, "--mode", "portal-submit", "--action", "go_live"]
            if payload_obj.get("laf_case_no"):
                cmd.extend(["--laf-case-no", str(payload_obj.get("laf_case_no"))])
            if payload_obj.get("case_number"):
                cmd.extend(["--case", str(payload_obj.get("case_number"))])
            if payload_obj.get("client_name"):
                cmd.extend(["--client", str(payload_obj.get("client_name"))])
            fields = payload_obj.get("fields") if isinstance(payload_obj.get("fields"), dict) else {}
            if fields:
                cmd.extend(["--fields-json", json.dumps(fields, ensure_ascii=False)])

            env = os.environ.copy()
            env["MAGI_LAF_ALLOW_GO_LIVE_SUBMIT"] = "1"
            timeout_sec = int(os.environ.get("MAGI_LAF_REPORT_TIMEOUT_SEC", "2400") or "2400")
            text = ""
            success = False
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, env=env)
                stdout_text = (proc.stdout or "").strip()
                stderr_text = (proc.stderr or "").strip()
                if proc.returncode != 0:
                    text = f"❌ 開辦送出失敗（確認碼 {token_id}，code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                else:
                    data = None
                    if stdout_text:
                        try:
                            data = json.loads(stdout_text)
                        except Exception:
                            m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                            if m2:
                                try:
                                    data = json.loads(m2.group(1))
                                except Exception:
                                    data = None
                    if isinstance(data, dict) and data.get("ok"):
                        identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
                        cname = str(identity.get("client_name") or payload_obj.get("client_name") or "").strip()
                        laf_no = str(identity.get("laf_case_number") or payload_obj.get("laf_case_no") or "").strip()
                        osc_no = str(identity.get("case_number") or payload_obj.get("case_number") or "").strip()
                        parts = [x for x in [cname, laf_no, osc_no] if x]
                        preview = data.get("preview") if isinstance(data.get("preview"), dict) else {}
                        shot_url = ""
                        if isinstance(preview.get("png_export"), dict):
                            shot_url = str(preview.get("png_export", {}).get("url") or "").strip()
                        shot_path = str(preview.get("png") or "").strip()
                        lines = [f"✅ 開辦回報已送出（確認碼 {token_id}）"]
                        if parts:
                            lines.append("目標：" + "｜".join(parts))
                        if shot_url:
                            lines.append(f"送出後畫面：{shot_url}")
                        elif shot_path:
                            lines.append(f"送出後截圖：{shot_path}")
                        text = "\n".join(lines)
                        success = True
                        # 回寫 DB：開辦成功 → legal_aid_status = "進行中"
                        try:
                            _update_case_no = osc_no or str(payload_obj.get("case_number") or "").strip()
                            _update_client = cname or str(payload_obj.get("client_name") or "").strip()
                            if _update_case_no or _update_client:
                                self._update_laf_status_after_action(
                                    case_number=_update_case_no,
                                    client_name=_update_client,
                                    new_status="進行中",
                                    action_label="開辦送出",
                                )
                        except Exception as _db_err:
                            logger.warning("go_live DB status update failed: %s", _db_err)
                    else:
                        err = ""
                        if isinstance(data, dict):
                            err = str(data.get("error") or "").strip()
                        text = f"❌ 開辦送出失敗（確認碼 {token_id}）：{err or (stdout_text[:500] if stdout_text else 'unknown')}"
            except subprocess.TimeoutExpired:
                text = f"⏳ 開辦送出逾時（確認碼 {token_id}），請稍後檢查平台結果。"
            except Exception as e:
                text = f"❌ 開辦送出流程異常（確認碼 {token_id}）：{e}"

            try:
                pending2 = self._load_laf_submit_pending()
                e2 = pending2.get(token_id) if isinstance(pending2, dict) else None
                if isinstance(e2, dict):
                    e2["status"] = "submitted" if success else "failed"
                    e2["finished_at"] = time.time()
                    pending2[token_id] = e2
                    self._save_laf_submit_pending(pending2)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2555, exc_info=True)

            try:
                if getattr(self, "notification_callback", None):
                    self.notification_callback(uid, text, platform_name, topic_key="laf_go_live")
            except Exception as notify_err:
                logger.warning(f"LAF submit callback failed: {notify_err}")

        threading.Thread(
            target=_run_submit,
            args=(str(user_id or ""), str(platform or ""), token, payload, result_data),
            daemon=True,
        ).start()

        return True, f"⏳ 已收到確認，開始送出開辦回報（確認碼 {token}）。完成後我會主動回報。"

    def _is_ambiguous_rule(self, text: str) -> bool:
        """
        Heuristic: treat 'rule-like' text as ambiguous if it looks like a question/hypothetical,
        or includes multiple alternatives, or is too short to be stable.
        """
        s = (text or "").strip()
        if not s:
            return False
        low = s.lower()
        if len(s) < 12:
            return True
        if any(x in s for x in ["？", "?", "嗎", "呢", "要不要", "可不可以", "行不行"]):
            return True
        if low.startswith(("如果", "假如", "也許", "可能", "萬一", "看情況")):
            return True
        if any(x in s for x in ["或", "還是", "任一", "其中一個", "二選一"]):
            return True
        if any(x in s for x in ["舉例", "例如", "比如", "ex:", "e.g."]):
            return True
        return False

    def _handle_memory_confirmation_if_any(self, user_id: str, platform: str, message: str) -> tuple[bool, str]:
        """
        Handle user confirmation for pending rule-memory capture.
        """
        msg = (message or "").strip()
        if not msg:
            return False, ""
        pending = self._load_memory_pending()
        key = self._pending_key(user_id, platform)
        entry = pending.get(key) if isinstance(pending, dict) else None
        if not isinstance(entry, dict):
            return False, ""

        # Expire old requests
        now = time.time()
        exp = float(entry.get("expires_at", 0.0) or 0.0)
        if exp and now > exp:
            pending.pop(key, None)
            self._save_memory_pending(pending)
            return True, "⏱️ 剛剛那筆「是否要記成規則」已過期。你可以再貼一次，我再幫你確認。"

        low = msg.lower()
        accept = low in {"要", "好", "是", "對", "ok", "yes", "y", "記住"} or msg.startswith(("要，", "要:", "要：", "好，", "好:", "好："))
        reject = low in {"不要", "不用", "不必", "取消", "no", "n"} or msg.startswith(("不要", "不用", "取消"))
        edit_prefixes = ("改成：", "改成:", "修正：", "修正:", "更正：", "更正:", "補充：", "補充:")

        if reject:
            pending.pop(key, None)
            self._save_memory_pending(pending)
            return True, "好，我不會把那句話記成規則。"

        if msg.startswith(edit_prefixes):
            new_text = msg.split(":", 1)[-1].strip() if ":" in msg else msg.split("：", 1)[-1].strip()
            new_text = (new_text or "").strip()
            if not new_text:
                return True, "❓ 你想改成什麼版本？請用 `改成：...` 把完整句子貼上。"
            entry["content"] = self._redact_secrets(new_text)[:800]
            entry["updated_at"] = now
            pending[key] = entry
            self._save_memory_pending(pending)
            return True, f"收到，我先改成這句：\n「{entry['content']}」\n\n要把它記成規則嗎？回我：`要` / `不要`"

        if accept:
            # Write rule memory now.
            try:
                from skills.memory.mem_bridge import remember
                from skills.evolution.skill_genesis import validate_skill_safety

                content = self._redact_secrets(str(entry.get("content") or ""))[:800]
                ok, _violations = validate_skill_safety(content)
                if not ok:
                    pending.pop(key, None)
                    self._save_memory_pending(pending)
                    return True, "🛡️ 這句話觸發鐵穹限制，我不會記成規則。"
                src = f"user_rule|platform={platform}|user={user_id}|ts={datetime.now(timezone.utc).isoformat()}"
                remember(content, source=src)
            except Exception as e:
                return True, f"❌ 記憶寫入失敗：{e}"
            pending.pop(key, None)
            self._save_memory_pending(pending)
            return True, "✅ 好，我已把這句話記成你的規則。"

        # Not a confirmation response; let normal processing continue.
        return False, ""

    def _maybe_capture_user_rules(self, user_id: str, platform: str, message: str):
        """
        Persist user-provided rules/preferences into long-term memory (for ALL users),
        while keeping system mutation commands admin-only.
        """
        if os.environ.get("MAGI_CAPTURE_USER_RULES", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        msg = (message or "").strip()
        if not msg:
            return
        low = msg.lower()
        # Heuristic: only capture "rules" style statements to avoid polluting memory.
        rule_markers = [
            "規則", "以後", "請你", "務必", "一定", "不要", "不允許", "禁止", "永遠", "一律",
            "rule", "always", "never", "must", "do not", "don't",
        ]
        if not any(m in msg or m in low for m in rule_markers):
            return
        now = time.time()
        key = (str(user_id or ""), str(platform or ""),)
        with self._rule_last_write_lock:
            last = float(self._rule_last_write.get(key, 0.0) or 0.0)
            if now - last < float(os.environ.get("MAGI_RULE_MEMORY_MIN_INTERVAL_SEC", "45")):
                return
            self._rule_last_write[key] = now
        try:
            from skills.memory.mem_bridge import remember
            from skills.evolution.skill_genesis import validate_skill_safety

            content = self._redact_secrets(msg)[:800]
            ok, _violations = validate_skill_safety(content)
            if not ok:
                return
            # If the phrasing is ambiguous, ask the user to confirm before remembering as a "rule".
            if self._is_ambiguous_rule(content):
                pending = self._load_memory_pending()
                key = self._pending_key(user_id, platform)
                pending[key] = {
                    "kind": "user_rule",
                    "content": content,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "expires_at": time.time() + float(os.environ.get("MAGI_MEMORY_CONFIRM_TTL_SEC", "600")),
                }
                self._save_memory_pending(pending)
                # Don't store yet; we will ask for confirmation in the main flow.
                return "ASK_CONFIRM"
            src = f"user_rule|platform={platform}|user={user_id}|ts={datetime.now(timezone.utc).isoformat()}"
            remember(
                content,
                source=src,
                metadata={
                    "verified": True,
                    "confidence": 0.98,
                    "source_type": "user_rule",
                    "role": "user",
                },
            )
        except Exception as e:
            logger.warning(f"Rule memory capture skipped: {e}")
        return

    def _maybe_capture_chatlog(self, user_id: str, platform: str, role: str, content: str):
        """
        Persist chat turns for ALL users.
        Stored with a 'chatlog|' source marker so retrieval can be gated later.
        """
        self._ensure_runtime_foundations()
        if os.environ.get("MAGI_CAPTURE_CHATLOG", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        role_name = str(role or "").strip().lower()
        if role_name and role_name != "user":
            capture_assistant = os.environ.get("MAGI_CAPTURE_ASSISTANT_CHATLOG", "0").strip().lower()
            if capture_assistant not in {"1", "true", "yes", "on"}:
                return
        text = (content or "").strip()
        if not text:
            return
        now = time.time()
        key = (str(user_id or ""), str(platform or ""), role_name)
        with self._chatlog_last_write_lock:
            last = float(self._chatlog_last_write.get(key, 0.0) or 0.0)
            if now - last < float(os.environ.get("MAGI_CHATLOG_MIN_INTERVAL_SEC", "25")):
                return
            self._chatlog_last_write[key] = now
            # Prune stale entries to prevent unbounded growth
            if len(self._chatlog_last_write) > self._chatlog_last_write_maxsize:
                sorted_keys = sorted(self._chatlog_last_write, key=self._chatlog_last_write.get)
                for k in sorted_keys[:len(sorted_keys) // 5]:
                    self._chatlog_last_write.pop(k, None)
        try:
            from skills.memory.mem_bridge import remember
            from skills.evolution.skill_genesis import validate_skill_safety

            safe = self._redact_secrets(text)
            safe = safe[:1200]
            ok, _violations = validate_skill_safety(safe)
            if not ok:
                self._hook_bus.memory_write(
                    "chatlog",
                    content=safe,
                    accepted=False,
                    user_id=str(user_id or ""),
                    platform=str(platform or ""),
                    source_signature="chatlog|rejected",
                    correlation_id=self._current_correlation_id(),
                    metadata={"reason": "validate_skill_safety_failed", "role": role_name},
                )
                return
            src = f"chatlog|platform={platform}|user={user_id}|role={role}|ts={datetime.now(timezone.utc).isoformat()}"
            remember(
                safe,
                source=src,
                metadata={
                    "verified": role_name == "user",
                    "confidence": 0.82 if role_name == "user" else 0.18,
                    "source_type": "chatlog",
                    "role": role_name,
                    "derived_from": "" if role_name == "user" else "assistant_reply",
                },
            )
            self._hook_bus.memory_write(
                "chatlog",
                content=safe,
                accepted=True,
                user_id=str(user_id or ""),
                platform=str(platform or ""),
                source_signature=src,
                memory_key="chatlog",
                correlation_id=self._current_correlation_id(),
                metadata={"role": role_name},
            )
        except Exception as e:
            try:
                self._hook_bus.memory_write(
                    "chatlog",
                    content=(content or "").strip()[:1200],
                    accepted=False,
                    user_id=str(user_id or ""),
                    platform=str(platform or ""),
                    source_signature="chatlog|error",
                    correlation_id=self._current_correlation_id(),
                    metadata={"error": str(e)[:200], "role": role_name},
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2974, exc_info=True)
            logger.warning(f"Chatlog memory capture skipped: {e}")

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
            conn = sqlite3.connect(db_path)
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
        aliases = {
            "summary": "summary",
            "summarize": "summary",
            "摘要": "summary",
            "總結": "summary",
            "translate": "translate",
            "translation": "translate",
            "翻譯": "translate",
            "vision": "vision",
            "ocr": "vision",
            "image": "vision",
            "圖像": "vision",
            "影像": "vision",
            "視覺": "vision",
            "辨識": "vision",
            "intent": "intent",
            "route": "intent",
            "router": "intent",
            "routing": "intent",
            "意圖": "intent",
            "路由": "intent",
            "transcript": "transcript",
            "transcribe": "transcript",
            "stt": "transcript",
            "audio": "transcript",
            "逐字稿": "transcript",
            "逐字": "transcript",
            "聽打": "transcript",
            "轉錄": "transcript",
        }
        found = {}
        for token in re.findall(r"[\w\u4e00-\u9fff-]+", str(message or "").lower()):
            name = aliases.get(token)
            if name:
                found[name] = True
        return found

    def _format_codex_distributed_status(self, report: dict) -> str:
        labels = {
            "summary": "摘要",
            "translate": "翻譯",
            "vision": "視覺/OCR",
            "intent": "意圖路由",
            "transcript": "逐字稿",
        }
        features = report.get("features") if isinstance(report.get("features"), dict) else {}
        enabled_list = [labels.get(name, name) for name, on in features.items() if on]
        disabled_list = [labels.get(name, name) for name, on in features.items() if not on]
        runtime_line = "ready"
        if not report.get("runtime_ready"):
            runtime_line = f"cooldown {int(report.get('runtime_cooldown_remaining_sec') or 0)}s"
        lines = [
            "🧠 Codex Sidecar 狀態",
            f"- 模式：{report.get('mode_label') or '-'} ({report.get('mode_code') or '-'})",
            f"- 全域開關：{'開啟' if report.get('enabled') else '關閉'}",
            f"- 功能：{', '.join(enabled_list) if enabled_list else '無'}",
        ]
        if disabled_list:
            lines.append(f"- 已停用：{', '.join(disabled_list)}")
        lines.append(f"- Runtime：{runtime_line}")
        lines.append(f"- OAuth：{'可用' if report.get('oauth_ready') else '不可用'}")
        if report.get("last_success_at"):
            lines.append(f"- 最近成功：{report.get('last_success_at')}")
        if report.get("last_feature"):
            lines.append(f"- 最近功能：{labels.get(str(report.get('last_feature')), str(report.get('last_feature')))}")
        if report.get("last_error"):
            lines.append(f"- 最近錯誤：{str(report.get('last_error'))[:180]}")
        if report.get("cooldown_reason"):
            lines.append(f"- 冷卻原因：{str(report.get('cooldown_reason'))[:180]}")
        lines.append("")
        lines.append("可用指令：codex 狀態 / codex 開啟 / codex 關閉 / codex 開啟 摘要 翻譯")
        return "\n".join(lines)

    def _handle_codex_distributed_command(self, message: str, role: str):
        msg = str(message or "").strip()
        msg_lower = msg.lower()
        if "codex" not in msg_lower and "sidecar" not in msg_lower and "分散式" not in msg:
            return False, None

        command = None
        if any(kw in msg_lower for kw in [" status", "codex status", "狀態", "mode", "模式", "health", "查看"]):
            command = "status"
        if any(kw in msg_lower for kw in ["開啟", "啟用", "打開", "全開", "on", "enable", "啟動"]):
            command = "on"
        if any(kw in msg_lower for kw in ["關閉", "停用", "關掉", "off", "disable", "本地", "切回本地", "退回本地"]):
            command = "off"
        if any(kw in msg_lower for kw in ["help", "幫助", "怎麼用", "指令"]) and "codex" in msg_lower:
            command = "help"

        if not command:
            return False, None
        if role != "admin":
            return True, "⛔ 抱歉，只有管理員可以切換 Codex sidecar。"
        if command == "help":
            report = public_status_report(can_toggle=True)
            return True, self._format_codex_distributed_status(report)

        features = self._parse_codex_distributed_features(msg)
        try:
            apply_manual_command(command, features=features or None)
            report = public_status_report(can_toggle=True)
            return True, self._format_codex_distributed_status(report)
        except Exception as e:
            logger.warning(f"Codex sidecar command failed: {e}")
            return True, f"❌ Codex sidecar 切換失敗：{e}"

    def _explain_routing(self, message: str, role: str = "user") -> dict:
        """
        Explain which internal handler would be invoked for a given text message.
        IMPORTANT: This must not execute side effects. It's used for transparency/debugging.
        The returned dict may include admin-only details; callers must redact for non-admin.
        """
        from api.routing import build_route_decision

        msg = (message or "").strip()
        msg_lower = msg.lower()

        def _res(
            action: str,
            matched: str,
            requires_admin: bool = False,
            handler: str = "",
            *,
            confidence: float = 1.0,
            reason: str = "",
            candidates: list[dict] | None = None,
            intent: str = "",
        ) -> dict:
            return build_route_decision(
                action=action,
                matched=matched,
                requires_admin=requires_admin,
                handler=handler,
                confidence=confidence,
                reason=reason or matched,
                candidates=candidates,
                intent=intent,
            )

        # Mirror the high-priority routing checks (subset) without executing.
        if ("codex" in msg_lower or "sidecar" in msg_lower or "分散式" in msg) and any(
            kw in msg_lower for kw in ["開啟", "啟用", "打開", "全開", "on", "enable", "關閉", "停用", "關掉", "off", "disable", "狀態", "status", "模式", "help", "幫助"]
        ):
            return _res(
                action="codex_distributed_control",
                matched="codex_sidecar_keywords",
                requires_admin=True,
                handler="api/orchestrator.py:_handle_codex_distributed_command",
            )

        if msg_lower in ["/help", "help", "指令", "說明", "功能", "menu", "helps", "/start"]:
            return _res(
                action="help_menu",
                matched="universal_help",
                requires_admin=True,
                handler="api/orchestrator.py:_handle_command('/help')",
            )

        if any(kw in msg_lower for kw in ["狀態", "status", "運作狀態", "節點狀態", "機器狀態", "大腦", "brain"]) or (
            ("模型" in msg) and any(kw in msg_lower for kw in ["目前", "現在", "使用", "模式", "為何", "是什麼"])
        ):
            return _res(
                action="status_report",
                matched="status_keywords",
                requires_admin=False,
                handler="api/orchestrator.py:process_message(status fast-path)",
            )

        if (
            msg_lower.strip() in {"今天", "明天"}
            or any(kw in msg_lower for kw in ["行程", "schedule", "日曆", "會議", "meeting", "本週", "這週"])
        ):
            return _res(
                action="schedule_query",
                matched="schedule_keywords",
                requires_admin=False,
                handler="api/orchestrator.py:_get_schedule",
            )

        if any(kw in msg_lower for kw in ["更新openclaw", "openclaw update", "openclaw 更新", "update openclaw"]):
            return _res(
                action="openclaw_update",
                matched="openclaw_update_keywords",
                requires_admin=True,
                handler="api/orchestrator.py:openclaw_updater.update_openclaw",
            )

        if any(msg_lower.startswith(k) for k in ["記住", "remember", "save memory", "memorize", "@magi 記住", "@magi learn"]):
            return _res(
                action="memory_write",
                matched="memory_write_keywords",
                requires_admin=True,
                handler="skills/memory/mem_bridge.py:remember",
            )

        if msg.startswith("翻譯 ") or msg_lower.startswith("translate "):
            return _res(
                action="translate",
                matched="translate_prefix",
                requires_admin=False,
                handler="skills/bridge/tri_sage_collab.py:translate_text",
            )

        if msg.startswith("製作音樂 ") or msg.startswith("生成音樂 ") or msg_lower.startswith("make music "):
            return _res(
                action="music_generate",
                matched="music_prefix",
                requires_admin=False,
                handler="skills/bridge/tri_sage_collab.py:generate_music",
            )

        if any(kw in msg_lower for kw in ["analyze code", "讀取程式碼", "code folder", "code資料夾", "連動模式", "改善建議", "read code"]):
            return _res(
                action="code_analysis_async",
                matched="code_analysis_keywords",
                requires_admin=True,
                handler="skills/bridge/code_analysis.py:analyze_code (background thread)",
            )

        if any(kw in msg_lower for kw in ["系統狀態", "system status", "cpu", "ram", "記憶體", "磁碟", "系統監控", "健康檢查", "service health"]):
            return _res(
                action="system_monitor",
                matched="system_monitor_keywords",
                requires_admin=True,
                handler="skills/ops/system_monitor.py",
            )

        # Fall back to classifier-based routing.
        try:
            detail: dict | str | None
            classify_detailed = getattr(self.classifier, "classify_detailed", None)
            if callable(classify_detailed):
                detail = classify_detailed(msg)
            else:
                detail = None

            if isinstance(detail, dict):
                intent = str(detail.get("intent") or "UNKNOWN")
            else:
                legacy_intent = getattr(self.classifier, "classify", lambda _msg: "UNKNOWN")(msg)
                intent = str(legacy_intent or "UNKNOWN")
                detail = {
                    "intent": intent,
                    "confidence": 0.0,
                    "reason": "legacy_classifier_fallback",
                    "candidates": [],
                }
        except Exception:
            detail = {"intent": "UNKNOWN", "confidence": 0.0, "reason": "classifier_exception", "candidates": []}
            intent = "UNKNOWN"
        return _res(
            action=(
                "command_handler" if intent == "CMD" else
                "query_handler" if intent == "QUERY" else
                "chat_handler" if intent == "CHAT" else
                "danger_handler" if intent == "DANGER" else
                "unknown"
            ),
            matched="intent_classifier",
            requires_admin=False,
            handler=(
                "api/orchestrator.py:_handle_command" if intent == "CMD" else
                "api/orchestrator.py:_handle_query" if intent == "QUERY" else
                "api/orchestrator.py:_handle_chat_async" if intent == "CHAT" else
                "api/orchestrator.py:(danger path)" if intent == "DANGER" else
                ""
            ),
            confidence=float(detail.get("confidence") or 0.0),
            reason=str(detail.get("reason") or "intent_classifier"),
            candidates=list(detail.get("candidates") or []),
            intent=intent,
        )

    # ════════════════════════════════════════════════════════════════
    # Topic Fast Path — specialized channel handlers
    # ════════════════════════════════════════════════════════════════
    def _topic_fast_path(self, topic_key: str, user_id, message: str, role: str, platform: str, attachment=None):
        """
        Specialized channel handler.  Returns a reply string if the topic
        handler consumed the message, or None to fall through to general logic.

        This lets messages in the 'laf' topic skip the 41-gate waterfall and go
        directly to the LAF handler, etc.
        """
        handler = self._TOPIC_HANDLERS.get(topic_key)
        if handler is None:
            return None
        try:
            return handler(self, user_id, message, role, platform, attachment)
        except Exception as e:
            logger.error(f"❌ Topic fast path '{topic_key}' error: {e}", exc_info=True)
            return None  # fall through to general logic

    @staticmethod
    def _topic_handler_laf(self, user_id, message, role, platform, attachment):
        """法扶頻道：優先嘗試 LAF 指令解析"""
        # 1) Try LAF report payload (開辦/報結/疑義 etc.)
        try:
            payload = self._parse_laf_report_payload(message)
            if payload:
                return self._handle_command(user_id, message, role=role, platform=platform)
        except Exception as e:
            logger.warning(f"LAF fast path parse error: {e}")

        # 2) Try LAF-related keywords
        msg_lower = message.lower()
        laf_kws = ["法扶", "開辦", "報結", "疑義", "撤回", "結案", "派案", "酬金", "回報"]
        if any(k in msg_lower for k in laf_kws):
            return self._handle_command(user_id, message, role=role, platform=platform)

        return None  # not a LAF command → fall through to general chat

    @staticmethod
    def _topic_handler_filereview(self, user_id, message, role, platform, attachment):
        """閱卷頻道：優先嘗試閱卷指令"""
        msg_lower = message.lower()
        fr_kws = ["閱卷", "卷宗", "下載", "上傳", "聲請", "繳費", "筆錄", "歸檔", "同步"]
        if any(k in msg_lower for k in fr_kws):
            return self._handle_command(user_id, message, role=role, platform=platform)
        return None

    @staticmethod
    def _topic_handler_judgment(self, user_id, message, role, platform, attachment):
        """判決頻道：優先嘗試判決搜尋"""
        msg_lower = message.lower()
        j_kws = ["判決", "案號", "法院", "搜尋", "收集", "趨勢", "案由", "見解"]
        if any(k in msg_lower for k in j_kws):
            return self._handle_command(user_id, message, role=role, platform=platform)
        return None

    @staticmethod
    def _topic_handler_transcript(self, user_id, message, role, platform, attachment):
        """筆錄頻道"""
        msg_lower = message.lower()
        if any(k in msg_lower for k in ["筆錄", "逐字稿", "語音", "錄音", "轉文字"]):
            return self._handle_command(user_id, message, role=role, platform=platform)
        return None

    @staticmethod
    def _topic_handler_translation(self, user_id, message, role, platform, attachment):
        """翻譯頻道"""
        # If attachment present, fall through to general logic (file processing path)
        if attachment:
            return None
        msg_lower = message.lower()
        if any(k in msg_lower for k in ["翻譯", "翻成", "translate", "中翻英", "英翻中"]):
            return self._handle_command(user_id, message, role=role, platform=platform)
        # In translation channel, assume any text is a translation request
        if len(message.strip()) > 10:
            return self._run_inline_translation_command(user_id, f"翻譯 {message}")
        return None

    @staticmethod
    def _topic_handler_summary(self, user_id, message, role, platform, attachment):
        """摘要頻道"""
        # If attachment present, fall through to general logic (file processing path)
        if attachment:
            return None
        msg_lower = message.lower()
        if any(k in msg_lower for k in ["摘要", "總結", "重點", "summarize", "summary"]):
            return self._run_inline_summary_command(message)
        # In summary channel, assume any long text is a summary request
        if len(message.strip()) > 100:
            return self._run_inline_summary_command(f"摘要 {message}")
        return None

    @staticmethod
    def _topic_handler_market(self, user_id, message, role, platform, attachment):
        """股市頻道"""
        msg_lower = message.lower()
        if any(k in msg_lower for k in ["股票", "追蹤", "晨報", "預測", "分析", "macd", "rsi", "布林"]):
            return self._run_stock_briefing_command(message)
        return None

    _TOPIC_HANDLERS = {
        "laf": _topic_handler_laf,
        "filereview": _topic_handler_filereview,
        "judgment": _topic_handler_judgment,
        "transcript": _topic_handler_transcript,
        "translation": _topic_handler_translation,
        "summary": _topic_handler_summary,
        "market": _topic_handler_market,
    }

    def _try_conversational_intent(self, message: str, msg_lower: str, user_id, role: str, platform: str):
        """
        Comprehensive natural-language intent dispatcher.
        Catches ALL conversational phrasing patterns and maps them to the
        appropriate MAGI skill — either executing directly or returning a
        friendly usage guide.

        Returns a reply string if a match is found, or None to fall through.
        """
        import re
        # Normalize away spaces for Chinese matching
        compact = message.replace(" ", "")
        low_compact = compact.lower()

        # ════════════════════════════════════════════════════════════
        # GATE: Detect if this message is "conversational" at all.
        # We cast a VERY wide net here so almost any non-command
        # phrasing that mentions a skill keyword gets caught.
        # ════════════════════════════════════════════════════════════

        # 1) Question endings
        is_question = bool(re.search(r"[嗎嘛呢阿啊？\?]$", compact))

        # 2) Ability / request / how-to / implicit-need signals (very broad)
        has_conv_signal = bool(re.search(
            r"(?:"
            r"可以|能不能|會不會|能否|可否|你會|你能|能幫|幫我|幫忙|"
            r"可不可以|有沒有辦法|是否能|是否可以|有辦法|"
            r"怎麼|如何|要怎麼|該怎麼|怎樣|要怎樣|"  # how-to
            r"有沒有|是不是|會不會|能不能|"              # yes/no
            r"想要|想用|想看|想問|想知道|想了解|"        # I want to...
            r"我想|我要|我需要|我希望|"                  # I want/need
            r"請|麻煩|拜託|勞駕|"                       # polite request
            r"教我|告訴我|跟我說|讓我|給我|"             # teach me, tell me
            r"有什麼|什麼是|這是什麼|那是什麼|"          # what is
            r"哪裡|在哪|去哪|"                          # where
            r"為什麼|為何|幹嘛|"                        # why
            r"看不懂|聽不懂|不會用|不知道怎|找不到|"    # implicit need: "I can't..."
            r"不懂|搞不懂|弄不懂|沒辦法|做不到|"        # frustration = implicit need
            r"太長|太多|太難|看不完|"                   # overwhelm = need summary/help
            r"can you|could you|do you|are you able|how to|how do i|"
            r"please|i want|i need|i'd like|show me|tell me|help me"
            r")",
            low_compact, re.IGNORECASE
        ))

        # 3) Soft intent starters — short prefixes that imply a request
        is_soft = bool(re.search(
            r"^(?:我想|我要|幫我|請幫我|麻煩|請你|我需要|我希望|"
            r"可以|能不能|你可以|你能|你會|能幫|"
            r"怎麼|如何|要怎麼|該怎麼|"
            r"教我|告訴我|幫忙|拜託|"
            r"幫|給我|讓我|替我|"                       # short action prefix
            r"這篇|這段|這個|那個|那篇|那段|"           # demonstrative + context
            r"i want|i need|can you|how|please|help|show)",
            low_compact, re.IGNORECASE
        ))

        if not (is_question or has_conv_signal or is_soft):
            return None  # Not conversational; fall through to rigid matchers.

        # ════════════════════════════════════════════════════════════
        # SKILL PATTERN TABLE
        # Each: (regex, action_key, guide_msg_or_None, direct_execute?)
        # Patterns are intentionally broad — the conversational gate
        # above already filters out non-conversational messages.
        # ════════════════════════════════════════════════════════════
        patterns = [
            # ── Translation ──
            (r"(?:翻譯|翻成|翻一下|幫翻|翻個|翻書|翻文|翻這|翻那|翻英|翻中|翻日|翻韓|"
             r"translate|translation|翻一篇|翻成中文|翻成英文|翻成日文|"
             r"中翻英|英翻中|日翻中|中翻日|韓翻中|多國語|多語|"
             r"看不懂.{0,6}(?:英文|日文|韓文|外文|這篇)|"
             r"這篇.{0,4}(?:外文|英文|日文)|"
             r"pdf.{0,6}翻|翻.{0,6}pdf)", "translate",
             "✅ **我可以幫您翻譯！**\n\n"
             "• 翻譯文字：直接輸入 `翻譯 [文字/網址]`\n"
             "• 翻譯檔案：上傳 PDF/TXT/DOCX 後在留言打 `翻譯`\n"
             "• 支援中英日韓等多語系，透過 TAIDE 引擎處理！", True),

            # ── Image Generation ──
            (r"(?:畫圖|畫畫|畫一|畫個|畫張|畫幅|生成圖|產生圖|做圖|出圖|弄圖|"
             r"generate\s*image|draw|make.*(?:image|picture|art)|create.*(?:image|art)|"
             r"作圖|圖片|插圖|海報|頭像|桌布|logo|illustration|"
             r"設計圖|設計一個|設計一張|做設計|弄設計|"
             r"幫我畫|幫畫|給我一張|弄一張|產一張|"
             r"ai.{0,4}(?:畫|圖|art)|人工智慧.{0,4}畫)", "image",
             "✅ **我可以幫您畫圖！**\n\n"
             "直接輸入描述就好，例如：\n"
             "• `畫一隻可愛的貓咪`\n"
             "• `draw a sunset over mountains`\n"
             "• `幫我畫一張海報`"),

            # ── Music ──
            (r"(?:做音樂|作曲|製作音樂|生成音樂|配樂|編曲|bgm|"
             r"make\s*music|compose|produce.*music|create.*(?:song|music|melody)|"
             r"幫我作曲|弄音樂|弄一首|寫歌|寫一首|"
             r"背景音樂|片頭曲|ringtone|音效)", "music",
             "✅ **我可以幫您製作音樂！**\n\n"
             "請輸入：`製作音樂 [風格描述]`\n"
             "例如：\n"
             "• `製作音樂 溫暖鋼琴、30秒`\n"
             "• `生成音樂 cyberpunk EDM 60s`"),

            # ── System Status / Node Status ──
            (r"(?:系統狀態|系統健康|伺服器狀態|server\s*status|system\s*status|"
             r"cpu|ram|記憶體|磁碟|硬碟|disk|"
             r"機器怎樣|電腦怎樣|機器還好嗎|系統還好嗎|"
             r"跑得動嗎|有沒有問題|有沒有異常|"
             r"系統正常嗎|是否正常|系統負載|load|"
             r"node\s*status|magi.*狀態|casper.*狀態|melchior.*狀態|"
             r"目前狀態|現在狀態|各節點|節點狀態|"
             r"大腦|brain|運作狀態|看一下狀態)", "status",
             None, True),

            # ── Schedule / Calendar ──
            (r"(?:行程|日曆|會議|開會|schedule|calendar|meeting|"
             r"今天有什麼|明天有什麼|這週|本週|下週|"
             r"待辦|to.?do|agenda|接下來|有什麼事|"
             r"今天的安排|明天的安排|今天要幹嘛|"
             r"有沒有會|幾點開會|什麼時候開會|"
             r"my\s*schedule|upcoming|what.*today|what.*tomorrow)", "schedule",
             None, True),

            # ── Memory ──
            (r"(?:記住|記東西|記事|記一下|筆記|memorize|remember|"
             r"幫我記|幫記|存記憶|寫筆記|做筆記|"
             r"把這個記|把這段記|以後記得|記錄一下|"
             r"save.*(?:note|memory)|take.*note|jot.*down|"
             r"我怕忘|怕我忘|不要忘記|別忘了)", "memory",
             "✅ **我可以幫您記住事情！**\n\n"
             "請輸入：`記住 [要記的內容]`\n"
             "例如：\n"
             "• `記住 我的車牌是 ABC-1234`\n"
             "• `記住 下次開會要帶合約`"),

            # ── Obsidian Notebook ──
            (r"(?:obsidian|筆記本|vault|知識庫|知識筆記|"
             r"obsidian\s*(?:search|read|ingest|ask|status|設定|搜尋|讀取|問)|"
             r"用.*(?:筆記|notes?).*(?:回答|查|找|搜)|"
             r"查.*(?:筆記|notes?)|搜.*(?:筆記|notes?)|"
             r"(?:筆記|notes?).*(?:搜尋|查詢|查找|search)|"
             r"notebook\s*(?:qa|q&a|query)|"
             r"用obsidian|開obsidian|連obsidian)", "obsidian",
             "✅ **我可以幫您管理 Obsidian 筆記！**\n\n"
             "• 查看狀態：`obsidian status`\n"
             "• 搜尋筆記：`obsidian search <關鍵字>`\n"
             "• 讀取筆記：`obsidian read <筆記路徑>`\n"
             "• 匯入記憶：`obsidian ingest [資料夾]`\n"
             "• 來源匯入：`obsidian ingest_source --source 案件 [--subpath X] [--limit N]`\n"
             "• 筆記問答：`obsidian ask <問題> [--scope source:案件|case:2025-0014]`\n"
             "• 設定 Vault：`obsidian set_vault <路徑>`"),

            # ── Code Analysis ──
            (r"(?:分析程式|檢查程式|程式碼|code|analyze\s*code|"
             r"code\s*review|讀code|看code|改code|修code|"
             r"review\s*code|debug|除錯|檢查bug|找bug|"
             r"幫我看程式|看一下程式|程式有問題|code有問題|"
             r"lint|syntax\s*check|程式檢查|原始碼|source\s*code|"
             r"改善.{0,4}程式|優化.{0,4}程式|重構|refactor)", "code_analysis",
             "✅ **我可以幫您分析程式碼！**\n\n"
             "• 全面掃描：`讀取程式碼` 或 `analyze code`\n"
             "• 自動修復：`自動修復code`\n"
             "• 我會深度掃描並產生改善建議報告。"),

            # ── Browser ──
            (r"(?:開網頁|開網站|開啟網頁|瀏覽器|browse|open\s*url|"
             r"截圖|screenshot|幫我開|打開.{0,6}網|"
             r"上網|查網頁|查網站|看網站|看網頁|"
             r"訪問.{0,4}網|連到.{0,4}網|去.{0,4}網站|"
             r"navigate|visit\s*(?:url|site|page)|"
             r"幫我查.{0,6}(?:網|site)|capture|screen\s*cap)", "browser",
             "✅ **我可以幫您開啟網頁或截圖！**\n\n"
             "• 開網頁：`打開 https://google.com`\n"
             "• 截圖：`截圖 https://example.com`\n"
             "• 也可以直接貼上網址請我開"),

            # ── File Manager ──
            (r"(?:找檔案|搜尋檔|列出檔|檔案管理|search\s*file|list\s*file|find\s*file|"
             r"看檔案|查檔案|有沒有這個檔|檔案在哪|"
             r"ls|dir|folder|資料夾|目錄|"
             r"幫我找.{0,6}檔|某個檔案)", "file_manager",
             "✅ **我可以幫您搜尋或列出檔案！**\n\n"
             "• 搜尋：`搜尋檔案 [關鍵字]`\n"
             "• 列出目錄：`列出檔案`"),

            # ── RSS / News ──
            (r"(?:看新聞|訂閱新聞|rss|news|讀新聞|最新消息|"
             r"今日新聞|有什麼新聞|國際新聞|科技新聞|"
             r"幫我看新聞|有沒有新消息|最新資訊|最新動態|"
             r"feed|headline|今天.{0,4}新聞|現在.{0,4}新聞)", "rss",
             "✅ **我可以幫您讀取新聞！**\n\n"
             "• 閱讀最新：`讀新聞` 或 `read news`\n"
             "• 新增訂閱：`訂閱 [RSS 網址]`"),

            # ── GitHub ──
            (r"(?:github|git\s*hub|搜尋\s*repo|找\s*repo|"
             r"github\s*趨勢|trending|open\s*source|開源|"
             r"找.{0,4}(?:套件|package|library|框架|framework)|"
             r"有沒有.{0,6}(?:repo|專案|project)|"
             r"star|fork|github上)", "github",
             "✅ **我可以幫您搜尋 GitHub！**\n\n"
             "• 趨勢：`github 趨勢`\n"
             "• 搜尋：`github 搜尋 [關鍵字]`"),

            # ── Summary ──
            (r"(?:短摘要?|詳細摘要?|簡短摘要?|完整摘要?|長摘要?|精簡摘要?|"
             r"摘要|summarize|summary|整理重點|幫我整理|"
             r"懶人包|太長.{0,4}(?:了|不想看|看不完)|tl;?dr|"
             r"簡單說|簡單講|長話短說|精簡|濃縮|"
             r"幫我看.{0,6}(?:重點|大意)|這篇.{0,4}(?:重點|大意|在講|在說)|"
             r"抓重點|歸納|總結|統整|overview|brief|detailed\s*summary|"
             r"key\s*point|main\s*point|abstract)", "summary",
             "✅ **我可以幫您做摘要！**\n\n"
             "• 網頁摘要：`摘要 [網址]`\n"
             "• 文字摘要：`摘要 [一段文字]`\n"
             "• 也可以上傳檔案請我整理重點\n\n"
             "💡 可指定摘要等級：\n"
             "• `精簡摘要` / `短摘要` → 3-5 點，每點一句話\n"
             "• `摘要` → 5-8 點，每點 1-2 句（預設）\n"
             "• `詳細摘要` / `長摘要` → 12-15 點，每點 2-3 句（含背景與數據）", True),

            # ── Legal Attest ──
            (r"(?:存證信函|寫存證|草擬存證|法律信函|legal\s*attest|"
             r"律師函|警告函|催告|催告書|催告函|"
             r"正式信函|法律文件|法律文書|存證|"
             r"寄存證|發存證|怎麼寫.{0,4}存證|"
             r"怎麼.{0,4}(?:寄|發).{0,4}(?:存證|信函))", "legal_attest",
             "✅ **我可以幫您寫存證信函！**\n\n"
             "請直接說：`幫我寫存證信函`\n"
             "我就會一步步引導您填寫寄件人、收件人及內文，最後產生標準 PDF。"),

            # ── POA / 委任狀 / 委託書 ──
            (r"(?:委任狀|委託書|委任状|委托书|power\s*of\s*attorney|poa|"
             r"做委任|寫委任|開委任|製作委任|產生委任|草擬委任|"
             r"做委託|寫委託|開委託|製作委託|產生委託|草擬委託|"
             r"怎麼.{0,4}(?:做|寫|開).{0,4}(?:委任|委託))", "poa",
             "✅ **我可以幫您製作委任狀/委託書！**\n\n"
             "請直接說：`幫我做委任狀`\n"
             "我會一步步引導您填寫案件類型、當事人、案號等欄位，最後產生 DOCX 檔案。\n\n"
             "💡 也可以一次提供資訊：\n"
             "• `幫張三做民事委任狀`\n"
             "• `製作刑事辯護人委任狀 114年度訴字第123號`"),

            # ── 委任契約書 ──
            (r"(?:委任契約|委任合約|engagement\s*agreement|"
             r"做契約|寫契約|製作契約|產生契約|草擬契約|開契約|"
             r"怎麼.{0,4}(?:做|寫|開).{0,4}契約)", "contract",
             "✅ **我可以幫您製作委任契約書！**\n\n"
             "請直接說：`幫我做委任契約書`\n"
             "我會一步步引導您填寫當事人、案由、費用等欄位，最後產生 DOCX 檔案。"),

            # ── 收據 ──
            (r"(?:收據|收执|收執|receipt|"
             r"做收據|寫收據|開收據|製作收據|產生收據|"
             r"怎麼.{0,4}(?:做|寫|開).{0,4}收據)", "receipt",
             "✅ **我可以幫您開收據！**\n\n"
             "請直接說：`幫我開收據`\n"
             "我會一步步引導您填寫委任人、案由、金額等欄位，最後產生 DOCX 檔案。"),

            # ── Skill List / Capabilities ──
            (r"(?:有什麼功能|你會什麼|你能做什麼|你有什麼能力|功能列表|"
             r"skill\s*list|what\s*can\s*you\s*do|"
             r"你是誰|你是什麼|自我介紹|介紹.*自己|"
             r"all\s*skills|所有功能|全部功能|功能清單|"
             r"能力清單|能力表|技能表|技能列表|你做得到什麼|"
             r"capabilities|features|what\s*(?:are|is)\s*(?:you|your)|"
             r"你做了什麼|你可以做什麼|有哪些功能|有哪些技能|"
             r"命令列表|指令列表|指令清單)", "skill_list",
             None, True),

            # ── Deep Think ──
            (r"(?:深度思考|deep\s*think|仔細想|認真想|好好想|"
             r"深度分析|深入分析|詳細分析|深入思考|"
             r"用大腦|用melchior|"
             r"think.*(?:hard|deep|careful)|analyze.*(?:deep|thorough)|"
             r"仔細.{0,4}(?:分析|想|看)|認真.{0,4}(?:分析|想|看)|"
             r"幫我.{0,4}深度|用比較強的)", "deep_think",
             "✅ **我可以用深度思考模式！**\n\n"
             "請輸入：`@MAGI 深度思考 [您的問題]`\n"
             "我會使用深度思考模式為您深度分析。"),

            # ── Crawler ──
            (r"(?:爬蟲|crawler|爬網|爬取|scrape|抓資料|"
             r"幫我爬|爬一下|爬個|spider|"
             r"抓.{0,4}(?:網頁|網站|資料|data|頁面)|"
             r"定時抓|自動抓|自動爬|排程爬|"
             r"每日.{0,4}(?:爬|抓)|daily\s*crawl)", "crawler",
             "✅ **我可以幫您管理爬蟲！**\n\n"
             "• 新增目標：`新增爬蟲目標 [網址]`\n"
             "• 列出目標：`列出爬蟲目標`\n"
             "• 執行爬取：`爬蟲目標 立即執行`"),

            # ── System Monitor / Health Check ──
            (r"(?:健康檢查|檢查系統|check\s*health|health\s*check|服務狀態|"
             r"service.*(?:check|health|alive|ok)|"
             r"服務.{0,4}(?:正常|活|掛|死|down)|"
             r"有沒有.{0,4}(?:掛|當|crash)|"
             r"系統.{0,4}(?:掛|當|crash|down)|"
             r"ping|heartbeat|uptime|是否.{0,4}正常)", "sys_monitor",
             None, True),

            # ── Update OpenClaw ──
            (r"(?:更新openclaw|openclaw.{0,4}更新|update\s*openclaw|"
             r"openclaw.*(?:update|upgrade|版本)|"
             r"升級.{0,4}openclaw)", "openclaw_update",
             "✅ **我可以幫您更新 OpenClaw！**\n\n"
             "請輸入：`更新openclaw`\n"
             "（僅限管理員操作）"),

            # ── Audio / Transcription ──
            (r"(?:語音|錄音|聽寫|逐字稿|transcript|speech|"
             r"audio|voice|whisper|stt|語音辨識|"
             r"幫我聽|幫我轉文字|轉成文字|"
             r"voice.*text|speech.*text|"
             r"錄音檔|音檔|mp3|wav|m4a)", "audio",
             "✅ **我可以幫您處理語音！**\n\n"
             "直接上傳錄音檔（MP3/WAV/M4A），我就會自動產生逐字稿。\n"
             "• 加上 `翻譯` → 翻譯逐字稿\n"
             "• 加上 `摘要` → 摘要逐字稿"),

            # ── Image Analysis ──
            (r"(?:看圖|看照片|分析圖|分析照片|圖片辨識|"
             r"這張圖|這個圖|這張照片|辨識圖|"
             r"image.*(?:analy|recogni)|photo.*(?:analy|recogni)|"
             r"ocr|文字辨識|辨識文字|"
             r"幫我看.{0,4}(?:圖|照片|這張)|"
             r"圖片裡|照片裡|圖上)", "image_analysis",
             "✅ **我可以幫您分析圖片！**\n\n"
             "直接上傳圖片，我就會用 Melchior 視覺模型幫您分析。\n"
             "• 也支援 OCR 文字辨識"),

            # ── 勞動基準法 ──
            (r"(?:加班費|勞基法|勞動基準法|特休假|特別休假|資遣費|"
             r"一例一休|例假日加班|休息日加班|平日延長|延長工時|"
             r"overtime.*pay|severance\s*pay|annual\s*leave.*taiwan|"
             r"算加班|算特休|算資遣|幾天特休|幾個月資遣|"
             r"休息日.{0,4}加班|例假日.{0,4}出勤)", "labor_law",
             "✅ **我可以幫您計算勞基法相關金額！**\n\n"
             "**加班費**：`月薪 50000，休息日加班 3 小時`\n"
             "**特休假**：`到職日 2020-03-01，我有幾天特休`\n"
             "**資遣費**：`月薪 45000，到職 2018-01-01，現在資遣費多少`\n"
             "**試算表代算**：貼上 Google Sheets 公開連結\n\n"
             "假別：平日 / 休息日 / 例假日 / 國定假日", True),

            # ── Judgment Search ──
            (r"(?:查判決|找判決|判決搜尋|搜尋判決|收集判決|判決搜集|"
             r"搜尋最高法院判決|最近.{0,4}判決|法院判決|court\s*judgment)", "judgment_search",
             "✅ **我可以幫您查判決！**\n\n"
             "• 直接輸入：`查判決 傷害`\n"
             "• 也可提供案號：`查判決 113年度上訴字第12號`", True),

            # ── Court Hearing Reminder ──
            (r"(?:開庭排程|庭期|最近.{0,4}(?:什麼庭|有庭|開庭)|"
             r"明天開庭|今天.{0,4}庭|下.{0,2}開庭|"
             r"庭前準備|準備.{0,6}開庭資料|準備清單|應備文件|"
             r"案件時程|時程總覽|全部排程|所有案件.{0,3}排程|"
             r"補正期限|繳費期限|補正提醒|繳費提醒|"
             r"什麼時候.{0,3}(?:補正|繳費)|"
             r".{1,8}(?:繳了|交了|繳費了|補正了|已繳|已補正|已交)(?:嗎|呢|沒|了沒|[？?])|"
             r"關掉.{1,8}(?:提醒|警報|通知)|"
             r"開庭提醒|hearing)", "court_hearing",
             "✅ **我可以幫您查排程！**\n\n"
             "• 查看排程：`最近有什麼庭`（含開庭/補正/繳費）\n"
             "• 庭前準備：`準備 XXX 案的開庭資料`\n"
             "• 標記完成：`張國賢繳了` / `補字第54號交了`", True),

            # ── Judgment Trend ──
            (r"(?:判決趨勢|趨勢分析|案由分析|判決分析|案由統計|"
             r"判決統計|見解趨勢|裁判趨勢)", "judgment_trend",
             "✅ **我可以分析判決趨勢！**\n\n"
             "• 總覽：`判決趨勢`\n"
             "• 特定案由：`判決趨勢 詐欺`", True),

            # ── Stock Briefing ──
            (r"(?:追蹤股票|追蹤清單|新增追蹤|增加追蹤|設定追蹤|移除追蹤|"
             r"股市晨報|股市預測|技術分析|macd|rsi|布林通道|watchlist|track\s+stock)", "stock_briefing",
             "✅ **我可以幫您追蹤股票與產生晨報！**\n\n"
             "• 設定：`追蹤股票 台積電 AAPL`\n"
             "• 清單：`追蹤清單`\n"
             "• 晨報：`股市晨報`", True),

            # ── Help / Menu ──
            (r"(?:怎麼用|怎麼使用|使用方法|使用教學|新手|"
             r"tutorial|guide|manual|beginner|"
             r"操作說明|操作方式|使用說明|入門)", "help",
             "✅ **歡迎使用 MAGI 系統！**\n\n"
             "輸入 `/help` 或 `指令` 可以看到完整的功能清單。\n"
             "也可以直接用白話問我，例如：\n"
             "• 「幫我翻譯這段英文」\n"
             "• 「我想看今天的行程」\n"
             "• 「幫我畫一張圖」"),
        ]

        for pattern, action, guide, *rest in patterns:
            direct = rest[0] if rest else False
            if not re.search(pattern, low_compact, re.IGNORECASE):
                continue

            # === Direct execution shortcuts ===
            if direct and action == "status":
                node_status = self._get_magi_status()
                brain_status = get_brain_status()
                collab_status = self._get_collaboration_status()
                return f"{node_status}\n\n{brain_status}\n\n{collab_status}"

            if direct and action == "schedule":
                return self._get_schedule()

            if direct and action == "skill_list":
                return self._list_skills()

            if direct and action == "sys_monitor":
                try:
                    from skills.ops.system_monitor import get_system_status, check_service_health
                    if any(kw in msg_lower for kw in ["服務", "service", "健康"]):
                        return check_service_health()
                    return get_system_status()
                except Exception as e:
                    return f"❌ 系統監控失敗: {e}"

            if direct and action == "translate":
                if self._looks_like_capability_question(message):
                    return guide
                return self._run_inline_translation_command(user_id, message)

            if direct and action == "summary":
                if self._looks_like_capability_question(message):
                    return guide
                return self._run_inline_summary_command(message)

            if direct and action == "labor_law":
                if self._looks_like_capability_question(message):
                    return guide
                return self._run_labor_law_command(message)

            if direct and action == "judgment_search":
                if self._looks_like_capability_question(message):
                    return guide
                return self._run_judgment_collector_command(message, notify=False)

            if direct and action == "stock_briefing":
                if self._looks_like_capability_question(message):
                    return guide
                return self._run_stock_briefing_command(message)

            if direct and action == "court_hearing":
                if self._looks_like_capability_question(message):
                    return "✅ **我可以幫您查開庭排程！**\n\n• 查看排程：`最近有什麼庭`\n• 庭前準備：`準備 XXX 案的開庭資料`"
                return self._run_court_hearing_command(message)

            # === Guided responses ===
            if guide:
                return guide

        return None

    def _extract_route_probe(self, message: str) -> tuple[bool, str, str]:
        """
        Parse a natural-language "route explain" request.
        Returns (is_request, probe_text, error_message).
        """
        msg = (message or "").strip()
        if not msg:
            return False, "", ""

        low = msg.lower()

        # Explicit command-style prefixes (preferred; unambiguous).
        explicit_prefixes = [
            "查詢路由",
            "看路由",
            "路由判斷",
            "路由查詢",
            "路由",
            "route",
            "routing",
        ]
        for p in explicit_prefixes:
            if msg.startswith(p) or low.startswith(p + " "):
                rest = msg[len(p) :].strip()
                rest = rest.lstrip(" :：\n\t")
                rest = rest.strip("「」\"'")
                if not rest:
                    return True, "", "❓ 你想查哪一句會走哪個功能？例如：`查詢路由 翻譯 https://...`"
                return True, rest, ""

        # Natural Taiwan-style phrasing; require a clear separator to avoid accidental triggers.
        natural_starts = ["幫我看", "麻煩看", "請幫我看", "請你看", "幫我判斷", "麻煩你判斷", "這句話", "這句"]
        natural_markers = ["會怎麼處理", "會走哪個", "會走什麼流程", "會跑哪個流程", "會觸發什麼", "會觸發哪個"]
        if any(msg.startswith(s) for s in natural_starts) and any(m in msg for m in natural_markers):
            # Prefer separator right after the marker phrase (avoid catching URL scheme like https://).
            marker_hit = next((m for m in natural_markers if m in msg), "")
            marker_end = (msg.find(marker_hit) + len(marker_hit)) if marker_hit else 0
            tail = msg[marker_end:] if marker_end > 0 else msg
            tail = tail.lstrip()

            # Accept a separator within a short window (e.g., "會走哪個功能：...").
            window = int(os.environ.get("MAGI_ROUTE_EXPLAIN_SEP_WINDOW", "12"))
            sep_full = tail.find("：")
            sep_ascii = tail.find(":")
            if 0 <= sep_ascii <= window:
                # Don't treat URL scheme colon (http://, https://) as a separator.
                left = tail[:sep_ascii].strip().lower()
                if left.endswith("http") or left.endswith("https"):
                    sep_ascii = -1
            if 0 <= sep_full <= window:
                rest = tail[sep_full + 1 :].strip()
            elif 0 <= sep_ascii <= window:
                rest = tail[sep_ascii + 1 :].strip()
            else:
                return True, "", "❓ 麻煩用 `：` 接上要判斷的句子，例如：`幫我看這句會怎麼處理：翻譯 https://...`"
            rest = rest.strip("「」\"'").strip()
            if not rest:
                return True, "", "❓ 麻煩在 `：` 後面貼上要判斷的句子。"
            return True, rest, ""

        return False, "", ""

    def _format_route_explain(self, info: dict, role: str = "user") -> str:
        """
        Render route explanation for the requester.
        Non-admin view must not leak system mutation commands/handlers.
        """
        if not isinstance(info, dict) or not info.get("success"):
            return "❌ 無法判斷路由。"

        is_admin = (role == "admin")
        requires_admin = bool(info.get("requires_admin"))
        action = str(info.get("action") or "")
        matched = str(info.get("matched") or "")
        intent = str(info.get("intent") or "")
        handler = str(info.get("handler") or "")

        if not is_admin and requires_admin:
            # Do not expose handler/action details for system mutation commands.
            return (
                "🔎 路由判定（一般使用者）\n"
                f"- 類型: 系統指令（僅管理員可用）\n"
                "說明：此操作屬於系統改動/管理類，已隱藏內部命令碼細節。"
            )

        if not is_admin:
            # Safe, user-facing action names only.
            public_action_map = {
                "translate": "翻譯/摘要（網頁/文字）",
                "music_generate": "製作音樂",
                "schedule_query": "行程查詢",
                "status_report": "狀態查詢",
                "chat_handler": "一般對話",
                "query_handler": "一般查詢",
            }
            public_action = public_action_map.get(action, action or (intent or "unknown"))
            return (
                "🔎 路由判定（一般使用者）\n"
                f"- 會走功能: {public_action}\n"
                f"- 判斷依據: {matched or 'n/a'}"
            )

        # Admin view: include handler details.
        lines = [
            "🔎 路由判定（管理員）",
            f"- 功能(action): {action or 'n/a'}",
            f"- 意圖(intent): {intent or info.get('intent','') or 'n/a'}",
            f"- 判斷依據(matched): {matched or 'n/a'}",
            f"- 管理員限定: {requires_admin}",
        ]
        if handler:
            lines.append(f"- 內部處理器(handler): {handler}")
        return "\n".join(lines)

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
        """
        Estimate token count for mixed CJK / Latin text.
        CJK characters ≈ 1-2 tokens each; Latin ≈ 1 token per ~4 chars.
        Simple heuristic — no tokenizer dependency.
        """
        if not text:
            return 0
        cjk = 0
        latin_chars = 0
        for ch in text:
            cp = ord(ch)
            # CJK Unified Ideographs + common CJK ranges
            if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
                    or 0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F
                    or 0xFF00 <= cp <= 0xFFEF):
                cjk += 1
            else:
                latin_chars += 1
        # CJK: ~1.5 tokens per char on average; Latin: ~1 token per 4 chars
        return int(cjk * 1.5) + (latin_chars // 4) + 1

    def _append_history(self, user_id, role, content):
        self._ensure_runtime_foundations()
        text = (content or "").strip()
        if not text:
            return
        if len(text) > 800:
            text = text[:800] + "...(truncated)"
        user_hist = self.user_history[user_id]
        if user_hist:
            last = user_hist[-1]
            if last.get("role") == role and last.get("content") == text:
                return
        ts = datetime.now(timezone.utc).isoformat()
        user_hist.append({"role": role, "content": text, "ts": ts})
        try:
            self._session_store.append_message(
                str(user_id or ""),
                str(role or ""),
                text,
                source="raw_history",
                metadata={"ts": ts},
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4249, exc_info=True)
        # Auto-compress: by message count OR by token budget overflow
        if len(user_hist) >= self._HISTORY_COMPRESS_AT:
            self._compress_history(user_id)
        elif len(user_hist) >= self._HISTORY_COMPRESS_KEEP + 4:
            # Check token budget even before count threshold
            total_tokens = sum(
                self._estimate_tokens(m.get("content", "")) for m in user_hist
            )
            if total_tokens >= self._HISTORY_TOKEN_BUDGET:
                self._compress_history(user_id)

    def _compress_history(self, user_id):
        """
        Summarize the oldest messages to prevent context overflow.
        Keeps the most recent _HISTORY_COMPRESS_KEEP messages intact;
        everything older is summarised by LLM and stored in
        self._history_summaries[user_id] with <summary> tags.

        Two triggers:
          1. Message count ≥ _HISTORY_COMPRESS_AT  (handled by caller)
          2. Estimated tokens ≥ _HISTORY_TOKEN_BUDGET (checked here)
        """
        user_hist = self.user_history[user_id]
        all_msgs = list(user_hist)

        # Also trigger compression by token budget (even if count < threshold)
        total_tokens = sum(self._estimate_tokens(m.get("content", "")) for m in all_msgs)
        if len(all_msgs) < self._HISTORY_COMPRESS_KEEP + 2 and total_tokens < self._HISTORY_TOKEN_BUDGET:
            return

        to_compress = all_msgs[: -self._HISTORY_COMPRESS_KEEP]
        keep_msgs = all_msgs[-self._HISTORY_COMPRESS_KEEP :]
        if not to_compress:
            return

        # Build raw text for summarisation (token-aware truncation)
        raw_lines = []
        token_count = 0
        for m in to_compress:
            line = f"{m['role']}: {m['content']}"
            line_tokens = self._estimate_tokens(line)
            if token_count + line_tokens > 1500:  # cap input to LLM summary
                raw_lines.append(f"...（略 {len(to_compress) - len(raw_lines)} 則）")
                break
            raw_lines.append(line)
            token_count += line_tokens
        raw_text = "\n".join(raw_lines)

        with self._history_summaries_lock:
            prev_summary = self._history_summaries.get(user_id, "")

        summary_prompt = (
            "你是對話背景摘要引擎。請將以下對話壓縮為結構化背景摘要（繁體中文，非原文、僅供延續上下文），"
            "不得補寫原文沒有的細節，也不要把推論寫成事實。"
            "格式如下：\n"
            "<summary provenance=\"derived\">\n"
            "【主題】一句話描述對話主題\n"
            "【關鍵決策】條列重要的指令、決策或結論\n"
            "【待辦/未完成】如有未完成事項請列出\n"
            "</summary>\n\n"
            "規則：忽略客套話和重複內容，只保留有意義的資訊；"
            "如果資訊不確定，請明確保留不確定性。"
            f"{f'（先前對話摘要：{prev_summary[:400]}）' if prev_summary else ''}"
            f"\n\n對話內容：\n{raw_text}"
        )

        new_summary = ""
        try:
            from skills.bridge.melchior_bridge import generate_text
            resp = generate_text(summary_prompt)
            if resp:
                new_summary = resp.strip()
                # Enforce token budget for summary
                if self._estimate_tokens(new_summary) > self._SUMMARY_MAX_TOKENS:
                    # Trim to budget while keeping structure
                    chars_budget = self._SUMMARY_MAX_TOKENS * 2  # rough chars for CJK
                    new_summary = new_summary[:chars_budget] + "..."
        except Exception:
            logging.getLogger(__name__).debug(
                "compress_history LLM failed for %s", user_id, exc_info=True
            )

        if not new_summary:
            # Fallback: structured extractive summary
            topics = []
            for m in to_compress:
                content = m.get("content", "").strip()
                if m.get("role") == "user" and content:
                    topics.append(content[:60])
            topic_str = "；".join(topics[:5]) if topics else "（多輪對話）"
            new_summary = (
                "<summary provenance=\"derived\">\n"
                "【注意】此為非原文背景摘要，僅供延續上下文，不可視為逐字紀錄。\n"
                f"【主題】{topic_str}\n"
                f"【訊息數】已壓縮 {len(to_compress)} 則對話\n"
                f"{'【先前摘要】' + prev_summary[:200] if prev_summary else ''}\n"
                "</summary>"
            )

        with self._history_summaries_lock:
            self._history_summaries[user_id] = new_summary
            # Prune oldest summaries to prevent unbounded growth
            if len(self._history_summaries) > self._history_summaries_maxsize:
                oldest_keys = list(self._history_summaries.keys())[:len(self._history_summaries) // 5]
                for k in oldest_keys:
                    self._history_summaries.pop(k, None)
        try:
            self._ensure_runtime_foundations()
            self._session_store.add_summary(
                str(user_id or ""),
                new_summary,
                source="history_compression",
                authoritative=False,
                metadata={"compressed_count": len(to_compress)},
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4362, exc_info=True)

        # Rebuild the deque with only the recent messages
        user_hist.clear()
        for m in keep_msgs:
            user_hist.append(m)

    def record_assistant_reply(self, user_id, content):
        """
        Public hook for server/bot layers to record replies from all return paths.
        """
        if not content:
            return
        normalized = str(content).replace("|||IMAGE_PATH|||", " [IMAGE] ")
        self._append_history(user_id, "assistant", normalized)
        # Assistant replies stay in short-term history by default and are not
        # promoted into long-term chatlog memory unless explicitly enabled.
        try:
            capture_assistant = os.environ.get("MAGI_CAPTURE_ASSISTANT_CHATLOG", "0").strip().lower()
            if capture_assistant in {"1", "true", "yes", "on"}:
                self._maybe_capture_chatlog(str(user_id or ""), "unknown", "assistant", normalized)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3858, exc_info=True)

    def _build_conversation_history(self, user_id, limit=12):
        """
        Build conversation history string for LLM, with token budget control.
        Includes continuation message when prior context was compressed.
        """
        history = list(self.user_history.get(user_id, []))
        with self._history_summaries_lock:
            summary = self._history_summaries.get(user_id, "")
        if not history and not summary:
            return ""

        parts = []
        token_used = 0

        # 1. Continuation message + summary (if compressed history exists)
        if summary:
            marked_summary = summary
            if _mark_non_authoritative_context:
                marked_summary = _mark_non_authoritative_context(
                    summary,
                    label="歷史摘要",
                    source="模型壓縮",
                )
            continuation = (
                "以下內容是延續用的背景摘要，不是逐字原文；"
                "它的權重低於最近的原文訊息，若與原文衝突，以原文為準。\n"
                f"{marked_summary}"
            )
            parts.append(continuation)
            token_used += self._estimate_tokens(continuation)

        # 2. Fill recent messages from newest to oldest, respecting token budget
        budget = self._HISTORY_TOKEN_BUDGET - token_used
        selected = []
        for msg in reversed(history[-limit:]):
            line = f"{msg['role']}: {msg['content']}"
            line_tokens = self._estimate_tokens(line)
            if token_used + line_tokens > self._HISTORY_TOKEN_BUDGET:
                break
            selected.append(line)
            token_used += line_tokens
        selected.reverse()
        parts += selected

        return "\n".join(parts)

    def _maybe_capture_profile_fact(self, user_id, message):
        """
        Capture explicit personal facts into long-term memory.
        """
        text = (message or "").strip()
        if not text or len(text) > 220:
            return

        lowered = text.lower()
        if lowered.startswith("/") or lowered.startswith("!"):
            return

        fact_patterns = [
            r"^(我是|我叫|我的名字是)",
            r"^(我喜歡|我偏好|我不喜歡)",
            r"^(my name is|i am|i prefer|my preference is)",
            r"(我的(生日|電話|信箱|地址))",
        ]
        if not any(re.search(pattern, text, re.IGNORECASE) for pattern in fact_patterns):
            return

        fingerprint = f"{user_id}:{text.lower()}"
        if fingerprint in self.profile_fact_cache:
            return

        try:
            from skills.memory.mem_bridge import remember

            remember(
                text,
                source=f"user_profile_{user_id}",
                metadata={
                    "verified": True,
                    "confidence": 0.96,
                    "source_type": "user_profile",
                    "role": "user",
                },
            )
            self.profile_fact_cache.add(fingerprint)
            if len(self.profile_fact_cache) > self._profile_fact_cache_maxsize:
                # Evict ~20% oldest entries (set is unordered, so random eviction)
                evict_n = self._profile_fact_cache_maxsize // 5
                for _ in range(evict_n):
                    self.profile_fact_cache.pop()
            logger.info(f"🧠 Captured profile fact for {user_id}")
        except Exception as e:
            logger.warning(f"Profile fact capture skipped: {e}")

    def process_message(self, user_id, message, platform="LINE", role="user", attachment=None, correlation_id: str | None = None, progress_callback=None, channel_context=None):
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
            logger.error(f"❌ Unhandled exception in process_message: {_fatal}", exc_info=True)
            self._append_route_trace(str(user_id or ""), str(platform or ""), "fatal_error", "unhandled", {"error": str(_fatal)[:200]})
            return "❌ 系統暫時忙碌，請稍後再試。"

    def _process_message_inner(self, user_id, message, platform="LINE", role="user", attachment=None, correlation_id=None, progress_callback=None, channel_context=None):
        message = self._sanitize_incoming_message((message or "").strip())
        quick_reply = self._quick_fixed_reply(message, role)
        if quick_reply:
            self._append_history(user_id, "user", message)
            self._append_history(user_id, "assistant", quick_reply)
            return quick_reply
        if not message and not attachment:
            return "✍️ 請輸入文字內容，或上傳檔案後告訴我要做的事。"
        self._append_history(user_id, "user", message)

        # ── 亂碼回報快捷指令 ──
        gibberish_reply = self._handle_gibberish_report(user_id, message, platform)
        if gibberish_reply:
            self._append_history(user_id, "assistant", gibberish_reply)
            return gibberish_reply

        # Defense in depth: never trust upstream "role=admin" unless the sender is allowlisted.
        # This prevents accidental privilege escalation (e.g., Discord guild admin, misrouted requests).
        try:
            if role == "admin" and not self._is_verified_admin_sender(user_id, platform):
                logger.warning(f"⚠️ Admin role downgraded (unverified): {platform}:{user_id}")
                role = "user"
        except Exception:
            if role == "admin":
                role = "user"

        try:
            if attachment:
                self.remember_recent_attachment(
                    user_id=str(user_id or ""),
                    platform=str(platform or ""),
                    attachment=attachment,
                    source_message=message,
                )
            else:
                recent_attachment = self._maybe_reuse_recent_attachment(
                    str(user_id or ""),
                    str(platform or ""),
                    message,
                )
                if recent_attachment:
                    attachment = recent_attachment
                    self._append_route_trace(
                        str(user_id or ""),
                        str(platform or ""),
                        "pre_route",
                        "recent_attachment_reuse",
                        {"attachment_type": str(recent_attachment.get("type") or "")},
                    )
        except Exception as recent_err:
            logger.warning(f"Recent attachment context skipped: {recent_err}")

        # ════════════════════════════════════════════════════════════════
        # CHANNEL-AWARE ROUTING — topic fast path + general channel logic
        # ════════════════════════════════════════════════════════════════
        _topic_key = ""
        if channel_context:
            _topic_key = (channel_context.get("topic_key", "") if isinstance(channel_context, dict)
                          else getattr(channel_context, "topic_key", ""))
        if _topic_key:
            self._append_route_trace(
                str(user_id or ""), str(platform or ""),
                "channel_context", _topic_key,
                {"channel_id": str((channel_context or {}).get("channel_id", ""))},
            )

        # ── Topic Fast Path: specialized channels get priority routing ──
        if _topic_key and _topic_key not in ("general", ""):
            _fast_result = self._topic_fast_path(
                _topic_key, user_id, message, role, platform, attachment,
            )
            if _fast_result is not None:
                self._append_history(user_id, "assistant", _fast_result)
                self._append_route_trace(
                    str(user_id or ""), str(platform or ""),
                    "topic_fast_path", _topic_key, {},
                )
                return _fast_result
            # fast path returned None → fall through to general logic

        # If the user is responding to a "should I remember this rule?" prompt, handle it first.
        try:
            handled, reply = self._handle_memory_confirmation_if_any(str(user_id or ""), str(platform or ""), message)
            if handled:
                return reply
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4018, exc_info=True)

        try:
            handled, reply = self._handle_skill_interview_if_any(str(user_id or ""), str(platform or ""), role, message)
            if handled:
                self._append_history(user_id, "assistant", reply)
                return reply
        except Exception as skill_interview_err:
            logger.debug(f"Skill interview intercept skipped: {skill_interview_err}")

        if not attachment and self._looks_like_skill_creation_request(message) and not self._looks_like_capability_question(message):
            reply = self._start_skill_interview(
                str(user_id or ""),
                str(platform or ""),
                role,
                message,
                trigger_reason="manual",
            )
            self._append_history(user_id, "assistant", reply)
            return reply

        # --- Legal Attest Generator Intercept ---
        try:
            import json
            import os
            legal_attest_state_file = f"{_MAGI_ROOT}/.agent/legal_attest_state.json"
            in_legal_flow = False
            if os.path.exists(legal_attest_state_file):
                with open(legal_attest_state_file, 'r', encoding='utf-8') as f:
                    legal_st = json.load(f)
                if str(user_id) in legal_st:
                    in_legal_flow = True
            
            msg_l = message.lower()
            trigger_start = any(kw in msg_l for kw in ["存證信函", "寫存證信"]) and any(kw in msg_l for kw in ["寫", "產生", "生成", "幫我", "草擬", "製作"])
            # Don't trigger on question-style phrasing (e.g. "你會寫存證信函嗎？")
            # Those should fall through to _try_conversational_intent for a guide.
            if trigger_start and re.search(r"[嗎嘛呢？\?]$", message.strip()):
                trigger_start = False
            
            if in_legal_flow or trigger_start:
                if in_legal_flow and any(kw in msg_l for kw in ["取消", "算了", "不要寫", "不寫了", "退出"]):
                    with open(legal_attest_state_file, 'r', encoding='utf-8') as f:
                        legal_st = json.load(f)
                    if str(user_id) in legal_st:
                        del legal_st[str(user_id)]
                        with open(legal_attest_state_file, 'w', encoding='utf-8') as f:
                            json.dump(legal_st, f)
                    return "✅ 已為您取消存證信函流程。"
                
                from skills.legal_attest.action import handle_chat
                cmd = "init" if (trigger_start and not in_legal_flow) else message
                return handle_chat(str(user_id), cmd)
        except Exception as e:
            logger.error(f"Legal attest flow check failed: {e}")

        # --- 文件產生 Intercept (委任狀/委託書/委任契約書/收據) ---
        try:
            import json as _json_poa
            import os as _os_poa
            poa_state_file = f"{_MAGI_ROOT}/.agent/poa_chat_state.json"
            in_poa_flow = False
            if _os_poa.path.exists(poa_state_file):
                with open(poa_state_file, 'r', encoding='utf-8') as f:
                    poa_st = _json_poa.load(f)
                if str(user_id) in poa_st:
                    in_poa_flow = True

            msg_l_poa = message.lower() if message else ""
            _action_kws = [
                "做", "製作", "產生", "生成", "幫我", "草擬", "建立", "開", "寫",
                "make", "generate", "create",
            ]
            poa_trigger = (
                any(kw in msg_l_poa for kw in ["委任狀", "委託書", "委任状", "委托书"])
                and any(kw in msg_l_poa for kw in _action_kws)
            )
            contract_trigger = (
                any(kw in msg_l_poa for kw in ["委任契約", "契約書", "委任合約"])
                and any(kw in msg_l_poa for kw in _action_kws)
            )
            receipt_trigger = (
                any(kw in msg_l_poa for kw in ["收據", "收执", "收執"])
                and any(kw in msg_l_poa for kw in _action_kws)
            )
            # 優先級消歧：契約 > 委任狀 > 收據
            if poa_trigger and contract_trigger:
                poa_trigger = "契約" not in msg_l_poa
                contract_trigger = not poa_trigger

            # 不攔截詢問式
            if (poa_trigger or contract_trigger or receipt_trigger) and re.search(r"[嗎嘛呢？\?]$", message.strip()):
                poa_trigger = contract_trigger = receipt_trigger = False

            if in_poa_flow or poa_trigger or contract_trigger or receipt_trigger:
                if in_poa_flow and any(kw in msg_l_poa for kw in ["取消", "算了", "不要", "不做了", "退出"]):
                    with open(poa_state_file, 'r', encoding='utf-8') as f:
                        poa_st = _json_poa.load(f)
                    if str(user_id) in poa_st:
                        del poa_st[str(user_id)]
                        with open(poa_state_file, 'w', encoding='utf-8') as f:
                            _json_poa.dump(poa_st, f)
                    return "✅ 已為您取消製作流程。"

                from api.poa_chat_handler import handle_chat as poa_handle_chat
                if (poa_trigger or contract_trigger or receipt_trigger) and not in_poa_flow:
                    poa_st = {}
                    if _os_poa.path.exists(poa_state_file):
                        with open(poa_state_file, 'r', encoding='utf-8') as f:
                            poa_st = _json_poa.load(f)
                    if receipt_trigger:
                        doc_type = "receipt"
                    elif contract_trigger:
                        doc_type = "contract"
                    else:
                        doc_type = "poa"
                    poa_st[str(user_id)] = {
                        "step": "start" if doc_type == "poa" else "ask_client",
                        "doc_type": doc_type,
                        "_raw_message": message,
                    }
                    with open(poa_state_file, 'w', encoding='utf-8') as f:
                        _json_poa.dump(poa_st, f, ensure_ascii=False)
                    return poa_handle_chat(str(user_id), "smart_init")
                else:
                    return poa_handle_chat(str(user_id), message)
        except Exception as e:
            logger.error(f"Document gen chat flow check failed: {e}")

        # LAF 開辦送出確認（你或同事回覆「正確送出 <確認碼>」才會真正送出）
        try:
            handled, reply = self._handle_laf_submit_confirmation_if_any(
                str(user_id or ""),
                str(platform or ""),
                str(role or "user"),
                message,
            )
            if handled:
                return reply
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4158, exc_info=True)

        # Route explain (safe): allow both user/admin to ask what would be executed.
        ok_route, probe, route_err = self._extract_route_probe(message)
        if ok_route:
            if route_err:
                return route_err
            info = self._explain_routing(probe, role=role)
            return self._format_route_explain(info, role=role)

        # Persist chat + user rules for ALL users (but keep system mutation commands admin-only).
        try:
            self._maybe_capture_chatlog(str(user_id or ""), str(platform or ""), "user", message)
            rule_flag = self._maybe_capture_user_rules(str(user_id or ""), str(platform or ""), message)
        except Exception:
            rule_flag = None

        log_msg = f"📥 Received from {user_id} ({platform}) [Role:{role}]: {message}"
        if attachment:
            log_msg += f" [Attachment: {attachment['type']}]"
        logger.info(log_msg)

        # 1. Safety Check (Iron Dome)
        if "rm -rf" in message or "drop table" in message.lower():
            try:
                from skills.evolution.skill_genesis import auto_harden_iron_dome_scope

                auto_harden_iron_dome_scope(
                    message,
                    source=f"{platform}:{user_id}",
                    max_new=2,
                )
            except Exception as e:
                logger.warning(f"Iron Dome auto-harden skipped: {e}")
            if role != 'admin':
                logger.warning(f"🛡️ Iron Dome Triggered by {user_id} (Unauthorized)")
                return "⛔ I cannot do that. You do not have permission."
            else:
                logger.warning(f"⚠️ Admin {user_id} is executing a dangerous command.")
                alert_iron_dome_violation("Dangerous Command (Admin)", "Destructive Keywords", message)

        # 2. Multimedia Processing (High Priority)
        # NOTE: keep attachment routing ahead of NL/text intent routing so file tasks
        # (e.g., "請完整翻譯…") are not hijacked into plain-text flows.
        if attachment:
            self._append_route_trace(
                str(user_id or ""),
                str(platform or ""),
                "top_level",
                "multimedia",
                {
                    "attachment_type": str(attachment.get("type") or ""),
                    "filename": str(attachment.get("filename") or "")[:120],
                },
            )
            return self._handle_multimedia(user_id, message, attachment)

        try:
            handled, codex_reply = self._handle_codex_distributed_command(message, str(role or "user"))
            if handled:
                self._append_route_trace(
                    str(user_id or ""),
                    str(platform or ""),
                    "top_level",
                    "codex_distributed_command",
                    {"role": str(role or "user")},
                )
                return codex_reply
        except Exception as e:
            logger.warning(f"Codex sidecar command routing skipped: {e}")

        # 1.5 Natural-language command router (shared across LINE/Discord/Telegram/web callers)
        # This maps colloquial zh-TW phrases to vetted magi-office-ops commands.
        # ⚠️ LAF report commands (開辦回報/報結/疑義 etc.) have a dedicated parser
        #    in _handle_command → parse_laf_report_payload.  Skip NL route for these
        #    to prevent the external intent_router from mis-parsing client names.
        #
        # 2026-03-29 Channel-aware routing: NL Router keyword interception is now
        # DISABLED in general/LINE channels to prevent conversational messages from
        # being hijacked.  Skills are instead reached via slash commands,
        # EmbeddingRouter (≥0.85), or topic fast path in specialized channels.
        # NL Router is ONLY active in specialized topic channels as a secondary route.
        _nl_router_enabled = bool(_topic_key and _topic_key not in ("general", ""))
        _skip_nl_for_laf = False
        if _nl_router_enabled:
            try:
                _skip_nl_for_laf = self._parse_laf_report_payload(message) is not None
            except Exception as _laf_parse_err:
                logger.error("LAF parser failed, will NOT skip NL router: %s", _laf_parse_err, exc_info=True)
                _skip_nl_for_laf = False
            if _skip_nl_for_laf:
                logger.info("📋 LAF report detected — skipping NL router (dedicated handler)")
        else:
            logger.debug("🔇 NL Router disabled (general/LINE channel, topic=%s)", _topic_key or "none")
        if _nl_router_enabled and not _skip_nl_for_laf:
            try:
                handled, routed_reply = self._run_nl_route(
                    str(user_id or ""),
                    message,
                    str(platform or ""),
                    str(role or "user"),
                )
                if handled:
                    self._append_route_trace(
                        str(user_id or ""),
                        str(platform or ""),
                        "top_level",
                        "nl_router",
                        {"role": str(role or "user")},
                    )
                    return routed_reply
            except Exception as e:
                logger.warning(f"NL router skipped due to error: {e}")

        # 1.6 Stock watchlist fallback:
        # when first prompt is pending, accept plain symbol/name replies
        # like "台積電 AAPL" even without explicit "追蹤股票：" prefix.
        try:
            handled, quick_set_reply = self._try_market_watchlist_quick_set(
                message,
                str(platform or ""),
            )
            if handled:
                self._append_route_trace(
                    str(user_id or ""),
                    str(platform or ""),
                    "top_level",
                    "market_quick_set",
                    None,
                )
                return quick_set_reply
        except Exception as e:
            logger.warning(f"Market quick-set fallback skipped: {e}")

        # 2.5. Universal Help/Menu Command (High Priority)
        # Check this before LLM classification to ensure menu always accessible
        msg_lower = message.lower()
        # Capture explicit personal facts into long-term memory for all users.
        self._maybe_capture_profile_fact(user_id, message)
        if msg_lower in ["/help", "help", "指令", "說明", "功能", "menu", "helps", "/start"]:
             return self._handle_command(user_id, "/help", role=role, platform=platform) # Force route to command handler

        # 2.6. Status Command (High Priority) - Check before LLM
        if any(kw in msg_lower for kw in ["狀態", "status", "運作狀態", "節點狀態", "機器狀態", "大腦", "brain"]) or (
            ("模型" in message) and any(kw in msg_lower for kw in ["目前", "現在", "使用", "模式", "為何", "是什麼"])
        ):
            # Combine Node Status (Heartbeat) + Brain Status (Manager)
            node_status = self._get_magi_status()
            brain_status = get_brain_status()
            collab_status = self._get_collaboration_status()
            return f"{node_status}\n\n{brain_status}\n\n{collab_status}"

        # 2.7. Schedule/Meeting Query (High Priority) - Check before LLM
        if (
            msg_lower.strip() in {"今天", "明天"}
            or any(kw in msg_lower for kw in ["行程", "schedule", "日曆", "會議", "meeting", "本週", "這週"])
        ):
            return self._get_schedule()

        # 2.7.0a Council Core Approval Commands (High Priority — must run before
        #         intent_forge / conversational_intent / semantic router to avoid
        #         being intercepted).
        if any(kw in msg_lower for kw in ["核心變更待審", "core approvals", "pending core changes"]):
            try:
                from skills.magi.council_approval import format_pending_summary
                return format_pending_summary(limit=20)
            except Exception as e:
                return f"❌ 讀取核心待審清單失敗: {e}"

        _ccr_match = re.search(r"(ccr-\d{14})", message)
        if any(kw in msg_lower for kw in ["批准核心變更", "approve core"]) or (
            _ccr_match and any(kw in msg_lower for kw in ["批准", "approve", "ok", "通過"])
        ):
            try:
                from skills.magi.council_approval import resolve_core_change
                # Extract ccr- ID from anywhere in the message
                _ccr_id_m = re.search(r"(ccr-\d{14})", message)
                if not _ccr_id_m:
                    return "❓ 請提供待審 ID，例如：`批准 ccr-20260213094500`"
                approval_id = _ccr_id_m.group(1)
                # Extract optional note (everything after the ccr- ID)
                note = message[_ccr_id_m.end():].strip()
                result = resolve_core_change(approval_id, "approved", approver=user_id, note=note)
                if result.get("success"):
                    item = result.get("item", {})
                    exec_info = item.get("execution", {})
                    if exec_info.get("success"):
                        files = ", ".join(exec_info.get("patches_applied", []))
                        return (
                            f"✅ 核心變更已核准並自動執行：`{approval_id}`\n"
                            f"修改檔案：{files}\n"
                            f"備份：{exec_info.get('details', {}).get('backup_dir', '?')}"
                        )
                    elif exec_info.get("error"):
                        return (
                            f"✅ 核心變更已核准：`{approval_id}`\n"
                            f"⚠️ 自動執行失敗：{exec_info.get('error', '?')[:200]}\n"
                            f"已自動回滾，需要手動處理。"
                        )
                    return f"✅ 核心變更已核准：`{approval_id}`"
                return f"❌ 核准失敗：{result.get('error')}"
            except Exception as e:
                return f"❌ 核准流程錯誤：{e}"

        if any(kw in msg_lower for kw in ["拒絕核心變更", "reject core"]) or (
            _ccr_match and any(kw in msg_lower for kw in ["拒絕", "reject", "不要", "駁回"])
        ):
            try:
                from skills.magi.council_approval import resolve_core_change
                _ccr_id_m = re.search(r"(ccr-\d{14})", message)
                if not _ccr_id_m:
                    return "❓ 請提供待審 ID，例如：`拒絕 ccr-20260213094500 原因`"
                approval_id = _ccr_id_m.group(1)
                note = message[_ccr_id_m.end():].strip()
                result = resolve_core_change(approval_id, "rejected", approver=user_id, note=note)
                if result.get("success"):
                    return f"🛑 核心變更已拒絕：`{approval_id}`"
                return f"❌ 拒絕失敗：{result.get('error')}"
            except Exception as e:
                return f"❌ 拒絕流程錯誤：{e}"

        # 2.7.6 User crawler targets (chat-callable, persisted into nightly run list)
        if any(kw in msg_lower for kw in ["爬蟲目標", "crawl target", "新增爬蟲", "移除爬蟲", "列出爬蟲", "run_daily"]):
            # 爬蟲管理開放給所有使用者 (2026-03-01)
            try:
                skill_script = f"{_MAGI_ROOT}/skills/crawler-targets/action.py"
                if not os.path.exists(skill_script):
                    return "❌ 找不到 crawler-targets skill。"

                url_match = re.search(r"(https?://\\S+)", message)
                url = (url_match.group(1).strip() if url_match else "").rstrip(").,")

                if any(k in msg_lower for k in ["列出", "list", "查看"]):
                    task_value = "list"
                elif any(k in msg_lower for k in ["移除", "刪除", "remove"]):
                    if not url:
                        return "⚠️ 請提供要移除的網址，例如：移除爬蟲目標 https://example.com"
                    task_value = "remove " + json.dumps({"url": url}, ensure_ascii=False)
                elif any(k in msg_lower for k in ["run_daily", "立即執行", "立刻執行", "現在執行"]):
                    task_value = "run_daily {}"
                else:
                    if not url:
                        return "⚠️ 請提供要新增的網址，例如：新增爬蟲目標 https://example.com"
                    note = ""
                    try:
                        tail = message.split(url, 1)[1].strip()
                        if tail:
                            note = tail[:120]
                    except Exception:
                        note = ""
                    task_value = "add " + json.dumps({"url": url, "note": note}, ensure_ascii=False)

                proc = subprocess.run(
                    [sys.executable, skill_script, "--task", task_value],
                    capture_output=True,
                    text=True,
                    timeout=int(os.environ.get("MAGI_CRAWLER_TARGETS_TIMEOUT_SEC", "90") or "90"),
                )
                out = (proc.stdout or "").strip()
                data = {}
                try:
                    data = json.loads(out) if out else {}
                except Exception:
                    m = re.search(r"(\\{[\\s\\S]*\\})\\s*$", out or "")
                    if m:
                        try:
                            data = json.loads(m.group(1))
                        except Exception:
                            data = {}

                if proc.returncode != 0 or (isinstance(data, dict) and not data.get("success", False)):
                    err = ""
                    if isinstance(data, dict):
                        err = str(data.get("error") or "").strip()
                    if not err:
                        err = (proc.stderr or out or "unknown error").strip()[:240]
                    return f"❌ 爬蟲目標操作失敗：{err}"

                if task_value == "list":
                    targets = data.get("targets") if isinstance(data, dict) else []
                    if not isinstance(targets, list):
                        targets = []
                    if not targets:
                        return "📭 目前沒有自訂爬蟲目標。"
                    lines = ["🕸️ 自訂爬蟲目標："]
                    for idx, t in enumerate(targets[:20], 1):
                        u = str((t or {}).get("url") or "").strip()
                        n = str((t or {}).get("note") or "").strip()
                        lines.append(f"{idx}. {u}" + (f"（{n}）" if n else ""))
                    if len(targets) > 20:
                        lines.append(f"...其餘 {len(targets) - 20} 筆")
                    return "\n".join(lines)

                if task_value.startswith("add "):
                    return f"✅ 已加入每日爬蟲目標：{url}"
                if task_value.startswith("remove "):
                    return f"✅ 已移除爬蟲目標：{url}"
                return "✅ 已執行自訂爬蟲目標每日流程。"
            except Exception as e:
                return f"❌ 爬蟲目標指令失敗：{e}"

        # 2.7.6.5 勞動基準法計算
        _labor_kws = ["加班費", "勞基法", "勞動基準法", "特休假", "特別休假", "資遣費",
                      "一例一休", "例假日加班", "休息日加班", "平日加班", "overtime計算",
                      "severance pay", "加班計算", "特休天數"]
        if any(kw in message for kw in _labor_kws):
            if self._looks_like_capability_question(message):
                return (
                    "✅ **我可以幫您計算勞基法相關金額！**\n\n"
                    "**加班費**：`月薪 50000，休息日加班 3 小時`\n"
                    "**特休假**：`到職日 2020-03-01，我有幾天特休`\n"
                    "**資遣費**：`月薪 45000，到職 2018-01-01，現在資遣費多少`"
                )
            return self._run_labor_law_command(message)

        # 2.7.7 OpenClaw Auto Update (Admin Only)
        if any(kw in msg_lower for kw in ["更新openclaw", "openclaw update", "openclaw 更新", "update openclaw"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以更新 OpenClaw（系統改動指令）。"
            try:
                from skills.ops.openclaw_updater import update_openclaw
                r = update_openclaw(auto=False)  # manual trigger
                if r.get("skipped"):
                    return "ℹ️ OpenClaw 更新已跳過（auto update disabled）。"
                if not r.get("success"):
                    return f"❌ OpenClaw 更新失敗: {r.get('error','unknown')}"
                if r.get("updated"):
                    return f"🚀 OpenClaw 已更新：{r.get('from','?')} -> {r.get('to','?')} (restart={r.get('restart','')})"
                return f"✅ OpenClaw 已是最新版本：{r.get('current','?')}"
            except Exception as e:
                return f"❌ OpenClaw 更新流程錯誤: {e}"

        # 2.7.75 Judgment Collector / Search
        if any(k in msg_lower for k in ["查判決", "找判決", "判決搜尋", "搜尋判決"]):
            if self._looks_like_capability_question(message):
                return (
                    "✅ **我可以幫您查判決！**\n\n"
                    "• 直接輸入：`查判決 傷害`\n"
                    "• 也可提供案號：`查判決 113年度上訴字第12號`"
                )
            return self._run_judgment_collector_command(message, notify=False)

        # 2.7.8 Memory Commands (High Priority) - Avoid LLM classification
        if any(msg_lower.startswith(k) for k in ["記住", "remember", "save memory", "memorize", "@magi 記住", "@magi learn"]):
            # 記憶寫入開放給所有使用者 (2026-03-01)
            try:
                content = message
                for kw in ["@MAGI 記住", "@MAGI learn", "remember", "記住", "save memory", "memorize", "請記住", "幫我記住"]:
                    content = content.replace(kw, "").strip()
                if len(content) < 2:
                    return "🧠 請告訴我要記住什麼？例如：`記住我的車牌是 ABC-1234`"
                from skills.memory.mem_bridge import remember
                remember(
                    content,
                    source=f"user_chat_{user_id}",
                    metadata={
                        "verified": True,
                        "confidence": 0.94,
                        "source_type": "user_confirmed",
                        "role": "user",
                    },
                )
                return "🧠 已記住。"
            except Exception as e:
                return f"❌ 記憶寫入失敗: {e}"

        if any(msg_lower.startswith(k) for k in ["忘記", "forget", "刪除記憶", "delete memory"]):
            try:
                content = message
                for kw in ["forget", "刪除記憶", "忘記", "delete memory", "把這段記憶刪掉", "請把這段記憶刪掉", "這是錯的"]:
                    content = content.replace(kw, "").strip()
                if len(content) < 2:
                    return "🧠 請告訴我要刪除哪段記憶？例如：`忘記我之前說的地址`"
                # 非管理員：通知管理員等待授權 (2026-03-01)
                if role != "admin":
                    try:
                        from skills.ops.red_phone import alert_admin
                        alert_admin(
                            f"🧠 使用者 {user_id} ({platform}) 要求刪除記憶：\n"
                            f"{content[:300]}\n\n"
                            "請管理員回覆「刪除記憶 <內容>」來確認執行。",
                            severity="warning",
                        )
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4468, exc_info=True)
                    return "🧠 已將刪除記憶的請求通知管理員，請等待授權後才會執行。"
                from skills.memory.mem_bridge import forget
                success, result_msg = forget(content)
                return f"{'🗑️ 已刪除記憶' if success else '⚠️ 刪除失敗'}\n{result_msg}"
            except Exception as e:
                return f"❌ 記憶刪除失敗: {e}"

        # 2.7.9 Obsidian Commands
        if msg_lower.startswith("obsidian ") or msg_lower.startswith("obsidian\n"):
            import subprocess as _sp
            _obs_parts = message.strip().split(None, 2)
            _obs_cmd = _obs_parts[1].lower() if len(_obs_parts) > 1 else "status"
            _obs_arg = _obs_parts[2] if len(_obs_parts) > 2 else ""
            _obs_py = os.path.join(_MAGI_ROOT if '_MAGI_ROOT' not in dir() else os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "obsidian", "action.py")
            _obs_venv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "venv", "bin", "python3")
            try:
                _obs_argv = [_obs_venv, _obs_py]
                if _obs_cmd in ("search", "ask"):
                    _obs_argv += ["--task", _obs_cmd, "--query", _obs_arg]
                elif _obs_cmd == "read":
                    _obs_argv += ["--task", "read", "--note", _obs_arg]
                elif _obs_cmd in ("set_vault", "set-vault"):
                    _obs_argv += ["--task", "set_vault", "--vault-path", _obs_arg]
                elif _obs_cmd in ("ingest_source", "ingest-source"):
                    _obs_argv += ["--task", "ingest_source"]
                    if _obs_arg:
                        import shlex as _shlex2
                        try:
                            _is_tokens = _shlex2.split(_obs_arg)
                        except ValueError:
                            _is_tokens = _obs_arg.split()
                        _is_i = 0
                        while _is_i < len(_is_tokens):
                            _tok = _is_tokens[_is_i]
                            if _tok == "--source" and _is_i + 1 < len(_is_tokens):
                                _obs_argv += ["--source", _is_tokens[_is_i + 1]]
                                _is_i += 2
                            elif _tok == "--subpath" and _is_i + 1 < len(_is_tokens):
                                _obs_argv += ["--subpath", _is_tokens[_is_i + 1]]
                                _is_i += 2
                            elif _tok == "--limit" and _is_i + 1 < len(_is_tokens):
                                _obs_argv += ["--limit", _is_tokens[_is_i + 1]]
                                _is_i += 2
                            elif _tok == "--force":
                                _obs_argv += ["--force"]
                                _is_i += 1
                            elif not _tok.startswith("--"):
                                _obs_argv += ["--source", _tok]
                                _is_i += 1
                            else:
                                _is_i += 1
                elif _obs_cmd == "ingest":
                    _obs_argv += ["--task", "ingest"]
                    if _obs_arg:
                        # Parse --tags, --since, --force, --folder flags
                        import shlex as _shlex
                        try:
                            _ingest_tokens = _shlex.split(_obs_arg)
                        except ValueError:
                            _ingest_tokens = _obs_arg.split()
                        _ingest_i = 0
                        _ingest_folder = ""
                        while _ingest_i < len(_ingest_tokens):
                            _tok = _ingest_tokens[_ingest_i]
                            if _tok == "--tags" and _ingest_i + 1 < len(_ingest_tokens):
                                _obs_argv += ["--tags", _ingest_tokens[_ingest_i + 1]]
                                _ingest_i += 2
                            elif _tok == "--since" and _ingest_i + 1 < len(_ingest_tokens):
                                _obs_argv += ["--since", _ingest_tokens[_ingest_i + 1]]
                                _ingest_i += 2
                            elif _tok == "--force":
                                _obs_argv += ["--force"]
                                _ingest_i += 1
                            elif _tok == "--folder" and _ingest_i + 1 < len(_ingest_tokens):
                                _ingest_folder = _ingest_tokens[_ingest_i + 1]
                                _ingest_i += 2
                            elif not _tok.startswith("--") and not _ingest_folder:
                                _ingest_folder = _tok
                                _ingest_i += 1
                            else:
                                _ingest_i += 1
                        if _ingest_folder:
                            _obs_argv += ["--folder", _ingest_folder]
                elif _obs_cmd == "status":
                    _obs_argv += ["--task", "status"]
                elif _obs_cmd == "list_vaults":
                    _obs_argv += ["--task", "list_vaults"]
                elif _obs_cmd == "help":
                    _obs_argv += ["--task", "help"]
                else:
                    _obs_argv += ["--task", _obs_cmd]
                    if _obs_arg:
                        _obs_argv += ["--query", _obs_arg]
                _obs_r = _sp.run(_obs_argv, capture_output=True, text=True, timeout=120, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                _obs_out = _obs_r.stdout.strip() or _obs_r.stderr.strip() or "No output"
                try:
                    _obs_j = json.loads(_obs_out)
                    if _obs_j.get("success") is False:
                        return f"⚠️ Obsidian: {_obs_j.get('error', 'unknown error')}"
                    return f"📓 **Obsidian**\n```json\n{json.dumps(_obs_j, ensure_ascii=False, indent=2)}\n```"
                except (json.JSONDecodeError, ValueError):
                    return f"📓 Obsidian:\n{_obs_out[:2000]}"
            except _sp.TimeoutExpired:
                return "⏱️ Obsidian 操作超時（120秒）"
            except Exception as e:
                return f"❌ Obsidian 錯誤: {e}"

        # 2.7.5. Intent Forge Debug Continuation (High Priority)
        # If CASPER previously asked a blocker question, treat the next message as feedback unless user issues another command.
        if any(kw in msg_lower for kw in ["清除除錯", "clear feedback", "取消除錯", "取消", "算了", "放棄"]):
            try:
                from skills.evolution.intent_forge import clear_pending_issue

                clear_pending_issue(str(user_id))
                return "🧹 已清除待補充除錯流程。"
            except Exception as e:
                return f"❌ 清除待補充除錯失敗: {e}"

        if any(kw in msg_lower for kw in ["補充除錯", "debug feedback", "繼續修復", "continue debug"]):
            try:
                from skills.evolution.intent_forge import forge_continue_with_user_feedback

                feedback = (
                    message.replace("補充除錯", "")
                    .replace("debug feedback", "")
                    .replace("繼續修復", "")
                    .replace("continue debug", "")
                    .strip()
                )
                result = forge_continue_with_user_feedback(str(user_id), feedback)
                return result.get("reply", "ℹ️ 已收到補充，正在續跑。")
            except Exception as e:
                return f"❌ 續跑除錯失敗: {e}"

        try:
            from skills.evolution.intent_forge import get_pending_issue, forge_continue_with_user_feedback

            pending = get_pending_issue(str(user_id))
            if pending and message and not message.startswith("/") and not message.startswith("@MAGI"):
                result = forge_continue_with_user_feedback(str(user_id), message)
                reply = result.get("reply")
                if reply:
                    return reply
        except Exception as e:
            logger.warning(f"Pending intent-forge continuation skipped: {e}")

        # ── 2.7.99 Comprehensive Natural Language Intent Dispatcher ──
        # Catches conversational phrasing for ALL major skills so the user
        # never gets "no specific skill matched" when asking something MAGI can do.
        nl_reply = self._try_conversational_intent(message, msg_lower, user_id, role, platform)
        if nl_reply is not None:
            self._append_history(user_id, "assistant", nl_reply)
            return nl_reply

        try:
            handled, semantic_reply = self._try_safe_semantic_skill_route(
                str(user_id or ""),
                message,
                str(role or "user"),
                str(platform or ""),
            )
            if handled:
                self._append_history(user_id, "assistant", semantic_reply)
                return semantic_reply
        except Exception as e:
            logger.warning(f"Primary semantic route skipped: {e}")

        # 2.8. Image Generation (High Priority) - Check before LLM
        # Matches: "/draw xxx", "draw a cat", "幫我畫一隻貓", "請畫圖", "生成圖片: sunset"
        draw_pattern = re.compile(r"(?:/draw\b|畫|draw|generate image|產生圖片|绘|画圖|畫一|画一)", re.IGNORECASE)

        if draw_pattern.search(msg_lower):
            # Extract prompt by removing common command words
            prompt = message
            for kw in ["/draw", "幫我", "請", "畫圖", "一張", "一個", "draw", "generate image", "產生圖片", "畫", "画", "a picture of", "an image of"]:
                prompt = re.sub(re.escape(kw), "", prompt, flags=re.IGNORECASE).strip()

            # If prompt became empty but message was long enough, use original message minus strict command
            if len(prompt) < 2:
                 return "🎨 請描述您想要的圖片內容。例如：'畫一隻可愛的貓咪'"

            return self._generate_image(prompt, user_id)

        # 2.8.5. Code Auto-Fix (High Priority)
        if any(kw in msg_lower for kw in ["自動修復code", "修復code資料夾", "autofix code", "auto fix code", "修復程式碼"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以執行 Code Auto-Fix（系統改動指令）。"
            try:
                from skills.management.code_autofix import autofix_codebase
                target = "magi" if "magi" in msg_lower else "code"
                dry_run = any(k in msg_lower for k in ["dry run", "preview", "只分析", "僅檢查"])
                include_tests = any(k in msg_lower for k in ["含測試", "include tests", "含 tests"])
                internalize = any(k in msg_lower for k in ["內化", "internalize", "技能化"])

                result = autofix_codebase(
                    target=target,
                    max_files=80,
                    max_rounds=2,
                    dry_run=dry_run,
                    include_tests=include_tests,
                    task_hint=message,
                    internalize_skill=internalize,
                    internalize_name="casper-autofix-knowledge",
                )
                if not result.get("success") and result.get("error"):
                    return f"❌ 自動修復啟動失敗: {result.get('error')}"

                verify = result.get("verify", {})
                verify_errors = verify.get("errors", [])
                lines = [
                    f"🛠️ **Code Auto-Fix 完成** (`{result.get('target', target)}`)",
                    f"- 掃描檔案: {result.get('scanned_files', 0)}",
                    f"- 發現語法問題: {result.get('syntax_issue_files', 0)}",
                    f"- 修復成功: {result.get('fixed_files', 0)}",
                    f"- 修復失敗: {result.get('failed_files', 0)}",
                    f"- Dry Run: {result.get('dry_run', False)}",
                ]
                if result.get("fixes"):
                    first_fix = result["fixes"][0]
                    lines.append(f"- 範例修復: `{first_fix.get('file','')}` (rounds={first_fix.get('rounds', 0)})")
                if verify_errors:
                    err = verify_errors[0]
                    lines.append(f"⚠️ 驗證仍有錯誤: `{err.get('file','')}` -> {err.get('error','')}")
                if result.get("internalized", {}).get("success"):
                    lines.append(f"🧬 已內化技能: `{result['internalized'].get('skill_folder')}`")
                return "\n".join(lines)
            except Exception as e:
                return f"❌ 自動修復流程失敗: {e}"

        # 2.8.6. CODE -> SKILL Internalization (High Priority)
        if any(kw in msg_lower for kw in ["內化code", "code技能化", "內化 code", "skillize code", "code internalize"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以執行 CODE 內化（系統改動指令）。"
            try:
                from skills.management.auto_skill import AutoSkill

                autoskill = AutoSkill()
                source_dir = str(get_magi_root_dir())
                if "legacy" in msg_lower or "archive" in msg_lower:
                    source_dir = str(get_legacy_code_root())
                force = any(k in msg_lower for k in ["force", "重建", "重新內化"])
                result = autoskill.internalize_codebase_as_skills(
                    source_dir=source_dir,
                    max_files=60,
                    force=force,
                    auto_activate=True,
                    enable_release=True,
                    canary_percent=20,
                    promote_min_runs=12,
                    promote_max_failure_rate=0.2,
                )
                if not result.get("success"):
                    return f"❌ CODE 內化失敗: {result.get('message', result.get('error', 'unknown'))}"
                canary_started = 0
                stable_set = 0
                for item in result.get("items", []):
                    rel = item.get("release", {}) or {}
                    if isinstance(rel.get("canary"), dict) and rel.get("canary", {}).get("success"):
                        canary_started += 1
                    if isinstance(rel.get("stable"), dict) and rel.get("stable", {}).get("success"):
                        stable_set += 1
                return (
                    "🧬 CODE 內化完成\n"
                    f"- Source: `{result.get('source_dir')}`\n"
                    f"- 掃描檔案: {result.get('scanned_files', 0)}\n"
                    f"- 新增/更新技能: {result.get('created_skills', 0)}\n"
                    f"- 略過: {result.get('skipped_files', 0)}\n"
                    f"- 新增知識: {result.get('learned_tips', 0)}\n"
                    f"- Canary 啟動: {canary_started}\n"
                    f"- Stable 設定: {stable_set}"
                )
            except Exception as e:
                return f"❌ CODE 內化流程失敗: {e}"

        if any(kw in msg_lower for kw in ["導入auto-skill", "import auto-skill", "toolsai auto-skill"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以導入 auto-skill（系統改動指令）。"
            try:
                from skills.management.auto_skill import AutoSkill

                autoskill = AutoSkill()
                result = autoskill.import_toolsai_auto_skill(notify_dc=True)
                if result.get("success"):
                    dc = result.get("dc_notify", {}) if isinstance(result.get("dc_notify"), dict) else {}
                    return (
                        "📥 Toolsai auto-skill 導入完成\n"
                        f"- 新增知識: {result.get('learned', 0)}\n"
                        f"- 檔案數: {len(result.get('imported_files', []))}\n"
                        f"- DC通知: line={dc.get('line')} discord={dc.get('discord')}"
                    )
                return f"❌ 導入失敗: {result.get('message', result.get('error', 'unknown'))}"
            except Exception as e:
                return f"❌ 導入 auto-skill 流程失敗: {e}"

        if any(kw in msg_lower for kw in ["code cycle", "自動巡檢", "工作流程自動化", "流程自動化"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以執行自動巡檢（系統改動指令）。"
            try:
                from scripts.code_skill_cycle import run_cycle

                result = run_cycle()
                if not result.get("success"):
                    return "❌ 自動巡檢流程失敗。請查看 `logs` 與 `skill events`。"
                af = result.get("autofix", {})
                ci = result.get("code_internalization", {})
                return (
                    "⚙️ 自動巡檢完成\n"
                    f"- AutoFix: fixed={af.get('fixed_files',0)} failed={af.get('failed_files',0)}\n"
                    f"- Code->Skill: created={ci.get('created_skills',0)} skipped={ci.get('skipped_files',0)}"
                )
            except Exception as e:
                return f"❌ 自動巡檢執行失敗: {e}"

        # 2.8.7. Translation (High Priority)
        # (Conversational translation queries now handled by _try_conversational_intent above)

        if message.startswith("翻譯 ") or message.lower().startswith("translate "):
            try:
                from skills.bridge.tri_sage_collab import translate_text

                text = message.replace("翻譯 ", "", 1).replace("translate ", "", 1).strip()
                if not text:
                    return "❓ 請提供要翻譯的文字。"
                result = translate_text(text, target_lang="繁體中文", source_lang="auto", mode="full")
                if result.get("success"):
                    translated_text = str(result.get("text") or "").strip()
                    disable_txt = any(k in msg_lower for k in ["不要txt", "不需要txt", "no txt", "inline", "直接貼上"])
                    explicit_txt = any(k in msg_lower for k in ["txt", "文字檔", "檔案"])
                    is_url = bool(re.search(r"https?://", text, flags=re.IGNORECASE))
                    try:
                        long_threshold = int(os.environ.get("MAGI_TRANSLATE_TXT_MIN_CHARS", "1200") or "1200")
                    except Exception:
                        long_threshold = 1200
                    is_long = len(text) >= max(400, long_threshold)
                    want_export = (not disable_txt) and (explicit_txt or is_url or is_long)
                    if want_export:
                        exported_reply = self._export_translation_docx(
                            source_text=text,
                            translated_text=translated_text,
                            prefix="full_translation",
                            user_id=str(user_id or ""),
                        )
                        if not exported_reply:
                            exported_reply = self._export_translation_txt(
                                translated_text=translated_text,
                                source=(text[:240] + "…") if len(text) > 240 else text,
                                provider=str(result.get("provider") or "tri-sage"),
                                mode="full_translation",
                                prefix="full_translation",
                                user_id=str(user_id or ""),
                            )
                        if exported_reply:
                            return exported_reply
                    return f"🌐 翻譯結果（{result.get('provider','tri-sage')}）:\n{translated_text}"
                return f"❌ 翻譯失敗: {result.get('error')}"
            except Exception as e:
                return f"❌ 翻譯流程失敗: {e}"

        # 2.8.8. Music Generation (High Priority)
        if message.startswith("製作音樂 ") or message.startswith("生成音樂 ") or message.lower().startswith("make music "):
            try:
                from skills.bridge.tri_sage_collab import generate_music

                prompt = (
                    message.replace("製作音樂 ", "", 1)
                    .replace("生成音樂 ", "", 1)
                    .replace("make music ", "", 1)
                    .strip()
                )
                if not prompt:
                    return "❓ 請提供音樂風格或需求，例如：`製作音樂 溫暖鋼琴、30秒`"
                result = generate_music(prompt, duration_sec=30)
                if result.get("success"):
                    return f"🎵 音樂已產生：`{result.get('path','')}`（{result.get('provider','tri-sage')}）"
                return f"❌ 音樂生成失敗: {result.get('error')}"
            except Exception as e:
                return f"❌ 音樂生成流程失敗: {e}"
            
        # 2.9. Code Analysis (High Priority)
        # Matches: "analyze code", "讀取程式碼", "code folder", "改善建議"
        if any(kw in msg_lower for kw in ["analyze code", "讀取程式碼", "code folder", "code資料夾", "連動模式", "改善建議", "read code"]):
            # Extract basic params
            target = "code"
            if "magi" in msg_lower:
                target = "magi"
            
            # Async Code Analysis
            from skills.bridge.code_analysis import estimate_effort
            
            # 1. Estimate Effort
            est = estimate_effort(target)
            if est["success"]:
                 wait_msg = f"🧐 **收到請求**\n已識別 {est['file_count']} 個關鍵檔案 (總計 {est['total_files']} 個)。\n**預估分析時間: {est['estimated_minutes']} 分鐘**\n\n正在進行深度分析，請稍候... (背景執行中)"
            else:
                 wait_msg = f"🧐 **收到請求**\n正在讀取 `{target}` 資料夾並進行深度分析...\n這個過程可能需要幾分鐘。 (背景執行中)"
            
            def run_analysis(uid, target_kw, instructions):
                try:
                    from skills.bridge.code_analysis import analyze_code
                    logger.info(f"🧵 Starting background analysis for {uid}...")
                    report = analyze_code(target_kw, instructions)
                    
                    if hasattr(self, 'notification_callback') and self.notification_callback:
                        header = f"🧐 **程式碼分析報告 (完成)**\n\n"
                        self.notification_callback(uid, header + report, "Discord")
                    else:
                        logger.warning("⚠️ Analysis done but no callback registered to notify user.")
                        
                except Exception as e:
                    logger.error(f"❌ Background Analysis Failed: {e}")
                    if hasattr(self, 'notification_callback') and self.notification_callback:
                        try:
                            self.notification_callback(uid, "❌ 分析過程中發生錯誤，請再試一次。", "Discord")
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4883, exc_info=True)

            # Start background thread
            thread = threading.Thread(target=run_analysis, args=(user_id, target, message))
            thread.daemon = True
            thread.start()
            
            return wait_msg

        # 2.10. List Skills (High Priority)
        # Matches: any message containing "skill" or "技能" or "功能" combined with listing/query words
        skill_kws = ["skill", "技能", "功能列表"]
        if any(kw in msg_lower for kw in skill_kws) and any(w in msg_lower for w in ["表", "列", "list", "哪些", "什麼", "告訴", "功能", "show", "help"]):
            return self._list_skills()

        # 2.11. System Monitor (系統監控)
        if any(kw in msg_lower for kw in ["系統狀態", "system status", "cpu", "ram", "記憶體", "磁碟", "系統監控", "健康檢查", "service health"]):
            try:
                from skills.ops.system_monitor import get_system_status, check_service_health
                if any(kw in msg_lower for kw in ["服務", "service", "健康"]):
                    return check_service_health()
                return get_system_status()
            except Exception as e:
                from skills.management.issue_tracker import log_issue
                log_issue(message, str(e), "System Monitor Skill")
                return f"❌ 系統監控失敗，已加入夜議檢討: {e}"

        # 2.11.5 Process Guardian (程序守護者)
        if any(kw in msg_lower for kw in ["check duplicates", "檢查分身", "kill duplicates", "刪除分身", "process check", "檢查重複"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以清理重複程序（系統改動指令）。"
            try:
                from skills.ops.process_guardian import check_and_clean_duplicates
                # Check Discord Bot by default, maybe check others too if requested?
                # For now focus on the main culprit: discord_bot.py
                report = check_and_clean_duplicates("api/discord_bot.py")
                return report
            except Exception as e:
                return f"❌ Process Guardian Error: {e}"

        # 2.11.7.1 Zombie Patrol (殭屍巡邏)
        if any(kw in msg_lower for kw in ["殭屍巡邏", "zombie patrol", "巡邏殭屍", "殭屍清除", "zombie clean"]):
            try:
                from daemon import reap_orphan_workers, get_reap_report
                dry = "模擬" in message or "dry" in msg_lower
                reap_orphan_workers(force=True, dry_run=dry)
                report = get_reap_report()
                if not report:
                    return "✅ 系統乾淨。"
                return report
            except Exception as e:
                return f"❌ 殭屍巡邏失敗: {e}"

        # 2.11.8 Raw URL Reader (網頁閱讀) - High Priority
        # Catch messages that are just a URL or start with a URL
        url_only_match = re.match(r'^(https?://[^\s]+)', message.strip())
        if url_only_match:
            try:
                url = url_only_match.group(1)
                
                # Check for image extensions - if image, let multimedia handler or Melchior Vision handle it?
                # Actually, orchestrator flows linearly. Multimedia check is at #2 (lines 80).
                # But message text might be a URL to an image.
                if any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                    # Let it fall through or handle as image analysis? 
                    # For now, let's treat as webpage unless we add specific image URL handling.
                    pass

                logger.info(f"🌐 Detected Raw URL: {url} -> Fetching via Web Research")
                
                # Fetch
                fetch_result = fetch_url_content(url, max_length=6000, exempt_iron_dome=True)
                
                if fetch_result['success']:
                    content = fetch_result['content']
                    prompt = f"User sent this URL: {url}\n\nPlease summarize the content in Traditional Chinese (繁體中文). Focus on the key points.\n\nTitle: {fetch_result['title']}\n\nContent:\n{content}"
                    
                    # Summarize via InferenceGateway (oMLX → remote → local fallback)
                    _gw = self._inference_gw
                    resp = _gw.chat(prompt, task_type="summary", timeout=120)
                    summary = resp.get("response", "無法產生摘要。")
                    
                    if "error" in resp and resp["error"]:
                        summary += f"\n(Error: {resp['error']})"

                    return f"🌐 **{fetch_result['title']}**\n(來源: {url})\n\n{summary}"
                else:
                    return f"❌ 無法讀取網頁: {fetch_result['error']}"
            except Exception as e:
                logger.error(f"Web Fetch Error: {e}")
                return f"❌ 網頁讀取發生錯誤: {e}"

        # 2.11.9 Webpage Translate/Summarize (網頁翻譯/摘要) - High Priority
        # If user asks to translate/summarize a webpage, prefer HTML section extraction over Playwright visible-text scraping.
        if re.search(r"https?://", msg_lower) and any(kw in msg_lower for kw in ["翻譯", "translate", "摘要", "總結", "整理"]):
            try:
                # Decide mode:
                # - If user explicitly asks for 摘要/總結/整理 => summary mode.
                # - If user just says 翻譯 (or says 不要摘要) => full-translation mode (no summarization).
                wants_translate = any(kw in msg_lower for kw in ["翻譯", "translate"])
                wants_summary = any(kw in msg_lower for kw in ["摘要", "總結", "整理"])
                no_summary = any(kw in msg_lower for kw in ["不要摘要", "不用摘要", "不需要摘要", "不要總結", "不用總結", "不需要總結"])
                disable_txt = any(kw in msg_lower for kw in ["不要txt", "不需要txt", "no txt", "inline", "直接貼上"])
                
                # For web translation, default to exporting formatted TXT unless explicitly disabled.
                force_txt = wants_translate and (not wants_summary) and (not disable_txt)
                if "full translation without summary" in msg_lower or "完整翻譯不摘要" in msg_lower:
                    wants_translate = True
                    wants_summary = False
                    force_txt = not disable_txt
                elif no_summary:
                    wants_summary = False

                url_match = re.search(r"https?://[^\s]+", message)
                if url_match:
                    url = url_match.group().strip()
                    logger.info(f"🌐 Webpage translate/summarize requested: {url}")

                    # For full translation we need more raw content; for summary we can keep it tighter.
                    if wants_translate and (not wants_summary):
                        sec = fetch_url_sections(url, max_length=160000, max_sections=12, exempt_iron_dome=True)
                    else:
                        sec = fetch_url_sections(url, max_length=60000, max_sections=8, exempt_iron_dome=True)
                    if not sec.get("success"):
                        return f"❌ 無法讀取網頁分頁內容: {sec.get('error')}"

                    title = (sec.get("title") or "").strip() or "Web Page"
                    sections = sec.get("sections") or []
                    if not sections:
                        return f"❌ 找不到可用的分頁內容（來源: {url}）"

                    # Push a progress note early (LINE will receive via server-registered callback).
                    try:
                        if getattr(self, "notification_callback", None):
                            tab_names = [((s.get("title") or s.get("id") or "分頁").strip()) for s in sections]
                            self.notification_callback(
                                user_id,
                                "🧾 我已抓到這個網頁的分頁，正在整理翻譯與摘要：\n- " + "\n- ".join(tab_names[:8]),
                                platform,
                            )
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5024, exc_info=True)

                    def _truncate(txt: str, n: int) -> str:
                        t = (txt or "").strip()
                        if len(t) <= n:
                            return t
                        return t[:n] + "\n...（內容過長已截斷）"

                    def _chunk_by_paragraph(txt: str, limit_chars: int = 3800) -> list[str]:
                        """
                        Split by blank lines first, then pack into chunks to keep prompt sizes stable.
                        """
                        s = (txt or "").strip()
                        if not s:
                            return []
                        parts = re.split(r"\n\s*\n", s)
                        chunks = []
                        buf = ""
                        for p in parts:
                            p = (p or "").strip()
                            if not p:
                                continue
                            candidate = (buf + "\n\n" + p).strip() if buf else p
                            if len(candidate) <= limit_chars:
                                buf = candidate
                                continue
                            if buf:
                                chunks.append(buf)
                            # If a single paragraph is huge, hard-split.
                            if len(p) > limit_chars:
                                for i in range(0, len(p), limit_chars):
                                    chunks.append(p[i : i + limit_chars])
                                buf = ""
                            else:
                                buf = p
                        if buf:
                            chunks.append(buf)
                        return chunks

                    blocks = []
                    for s in sections:
                        sid = (s.get("id") or "").strip()
                        stitle = (s.get("title") or "").strip() or (sid or "分頁")
                        serr = (s.get("error") or "").strip()
                        content = (s.get("content") or "").strip()
                        if serr and not content:
                            blocks.append(f"### {stitle}\n⚠️ 讀取失敗或被鐵穹擋下：{serr}")
                            continue
                        if not content:
                            continue
                        # Summary path uses truncated blocks; full-translation uses raw blocks but chunked later.
                        if wants_translate and (not wants_summary):
                            blocks.append(f"### {stitle}\n{content}")
                        else:
                            blocks.append(f"### {stitle}\n{_truncate(content, 6500)}")

                    if not blocks:
                        return f"❌ 分頁內容皆為空或被擋下（來源: {url}）"

                    model = (os.environ.get("MAGI_MAIN_MODEL") or os.environ.get("MAGI_MAIN_LLM") or TEXT_PRIMARY_MODEL).strip()

                    if wants_translate and (not wants_summary):
                        # Full translation mode: preserve structure, do NOT summarize.
                        out_parts = [
                            f"🌐 **{title}**",
                            f"來源: {url}",
                            "",
                            "（完整翻譯，不摘要。若內容太長會改用 TXT 連結傳送。）",
                            "",
                        ]
                        total_tabs = len(sections)
                        done_tabs = 0

                        for s in sections:
                            sid = (s.get("id") or "").strip()
                            stitle = (s.get("title") or "").strip() or (sid or "分頁")
                            serr = (s.get("error") or "").strip()
                            content = (s.get("content") or "").strip()

                            if serr and not content:
                                out_parts.append(f"## {stitle}\n⚠️ 讀取失敗或被鐵穹擋下：{serr}\n")
                                done_tabs += 1
                                continue
                            if not content:
                                done_tabs += 1
                                continue

                            # Progress ping per tab.
                            try:
                                if getattr(self, "notification_callback", None):
                                    self.notification_callback(
                                        user_id,
                                        f"📄 正在完整翻譯分頁：{stitle}（{done_tabs + 1}/{total_tabs}）",
                                        platform,
                                    )
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5120, exc_info=True)

                            chunks = _chunk_by_paragraph(content, limit_chars=3800)

                            def _translate_tab_chunk(idx, ch, total):
                                tprompt = f"""
請把下列英文內容「完整翻譯」成繁體中文（臺灣用語）。

規則：
1. 不要摘要、不省略。
2. 盡量保留原本段落、清單、標點與引用格式。
3. 專有名詞（人名、機構、案件名）保留原文為主（例如 Dickson, United Kingdom, European Court of Human Rights）。
4. 條文請寫 Article 8 或 第8條，不要寫第八章。
5. 請直接輸出翻譯結果，不要加入任何註解或修稿痕跡。

[段落 {idx}/{total}]
{ch}
""".strip()
                                _gw = self._inference_gw
                                r = _gw.chat(tprompt, task_type="translate", timeout=240)
                                t = (r.get("response") or "").strip()
                                if not (r.get("success") and t):
                                    err = (r.get("error") or "unknown").strip()
                                    return idx, f"（⚠️ 此段翻譯失敗：{err}）\n{_truncate(ch, 1200)}"
                                return idx, t

                            from concurrent.futures import ThreadPoolExecutor, as_completed
                            translated_buf = [None] * len(chunks)
                            with ThreadPoolExecutor(max_workers=3) as tab_executor:
                                tab_futs = {tab_executor.submit(_translate_tab_chunk, i+1, ch, len(chunks)): i for i, ch in enumerate(chunks)}
                                for f in as_completed(tab_futs):
                                    fi = tab_futs[f]
                                    try:
                                        _, txt = f.result()
                                        translated_buf[fi] = txt
                                    except Exception as e:
                                        translated_buf[fi] = f"（⚠️ 此段翻譯發生系統錯誤：{e}）"
                            translated_chunks = [t for t in translated_buf if t is not None]

                            out_parts.append(f"## {stitle}\n" + "\n\n".join(translated_chunks) + "\n")
                            done_tabs += 1

                        text = "\n".join(out_parts).strip()
                        
                        # TXT export is handled after cleanup/normalization so saved file is the final output.
                    else:
                        # Summary mode (fast): Single-pass summarization/translation for all tabs.
                        combined = "\n\n".join(blocks)
                        prompt = f"""
你是 CASPER。以下是一個網頁（同頁含多個分頁/章節）的英文內容摘錄。請用繁體中文（臺灣用語）輸出「翻譯式摘要」（不是逐字翻譯）。

要求：
0. **必須使用繁體字**（不要出現簡體字）。
1. 先給「整體重點」(8-14 點條列)。
2. 再給「各分頁重點」：每個分頁 4-8 點條列 + 2-4 句白話說明。
3. 最後給「我建議先看哪幾個分頁」(最多 4 個) + 原因。
4. 禁止編造；只能依內容推導。
5. **法規/條約/法院名稱請以內容原文為準**：不要自行改成別的條約或法規；若不確定正式中文名稱，直接保留英文。
6. 任何數字（金額、年份、條文編號、判決結果）若內容沒明講，就不要寫「具體數字」。
7. 不要夾雜英文單字（除非是原文專有名詞，且你不確定正式中文譯名）。
8. 不要引用外部資料或你自己的知識；只用下方內容。
9. 人名/地名/機構名請以原文為主（例如 Dickson, United Kingdom），不要自行翻成其他語言或不常見譯名。
10. 請直接輸出「最終版本」，不要出現任何修稿痕跡或註解，例如「修改成：」、「更正：」、「草稿：」、「思考：」。
11. 條文請寫「Article 8」或「第8條」（不要寫「第八章」）。

[專有名詞固定寫法（請務必遵守）]
- Dickson：一律寫 Dickson（不要自行翻譯成中文名）
- The United Kingdom：可寫「英國」或「United Kingdom」（擇一即可）
- European Court of Human Rights：可寫「歐洲人權法院」

[網頁標題]
{title}

[來源]
{url}

[分頁內容]
{combined}
""".strip()

                        _gw = self._inference_gw
                        resp = _gw.chat(prompt, task_type="translate", timeout=240)
                        text = (resp.get("response") or "").strip()
                        if not (resp.get("success") and text):
                            err = (resp.get("error") or "unknown").strip()
                            return f"❌ 網頁翻譯/摘要失敗：{err}"

                    # Force Traditional Chinese (Taiwan) output even if the model slips into Simplified.
                    try:
                        from opencc import OpenCC
                        text = OpenCC("s2twp").convert(text)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5212, exc_info=True)

                    # If the model leaks Japanese/English, do a quick cleanup pass.
                    try:
                        import re as _re

                        def _needs_cleanup(t: str) -> bool:
                            s = (t or "").strip()
                            if not s:
                                return False
                            # Hiragana/Katakana
                            if _re.search(r"[\u3040-\u30ff]", s):
                                return True
                            # Cyrillic / Hangul (model occasionally leaks non-Chinese scripts)
                            if _re.search(r"[\u0400-\u04ff\uac00-\ud7af]", s):
                                return True
                            # Too many latin words (ignore URLs)
                            no_urls = _re.sub(r"https?://\\S+", "", s)
                            if len(_re.findall(r"[A-Za-z]{4,}", no_urls)) >= 8:
                                return True
                            return False

                        if _needs_cleanup(text):
                            cleanup_prompt = f"""
請把下列內容「改寫成全篇繁體中文（臺灣用語）」版本，並遵守：
1. 不要出現日文（平假名/片假名/日文漢字用法）或英文單字（除非是原文專有名詞且無合適中文譯名；但也請盡量翻成中文）。
2. 保留原本的章節結構、清單與順序。
3. 不要新增任何新資訊；只做語言與用詞修正。
4. 人名/地名/機構名以原文為主（例如 Dickson, United Kingdom），不要自行翻成別的語言或不常見譯名。

[原內容]
{text}
""".strip()
                            _gw_clean = self._inference_gw
                            resp2 = _gw_clean.chat(cleanup_prompt, task_type="tc_review", timeout=120)
                            fixed = (resp2.get("response") or "").strip()
                            if resp2.get("success") and fixed:
                                text = fixed
                                try:
                                    from opencc import OpenCC
                                    text = OpenCC("s2twp").convert(text)
                                except Exception:
                                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5254, exc_info=True)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5256, exc_info=True)

                    # Hard-sanitize any non-Chinese scripts that can slip through (Cyrillic/Kana/Hangul).
                    try:
                        import re as _re
                        text = _re.sub(r"[\u3040-\u30ff\u0400-\u04ff\uac00-\ud7af]", "", text)
                        # Common bad translations / normalization.
                        text = text.replace("應徵者", "申請人")
                        text = _re.sub(r"文章\\s*8", "第8條", text)
                        text = _re.sub(r"[ \t]{2,}", " ", text).strip()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5267, exc_info=True)

                    # In full-translation mode text already includes header.
                    if wants_translate and (not wants_summary):
                        # Export final cleaned translation to TXT by default for web translation.
                        try:
                            export_long = os.environ.get("MAGI_EXPORT_LONG_TEXT", "1").strip().lower() in {"1", "true", "yes", "on"}
                            threshold = int(os.environ.get("MAGI_EXPORT_TEXT_THRESHOLD", "9000") or "9000")
                        except Exception:
                            export_long, threshold = True, 9000
                        if force_txt or (export_long and len(text) >= threshold):
                            # Prefer DOCX bilingual table
                            exported_reply = self._export_translation_docx(
                                source_text=locals().get("combined", ""),
                                translated_text=text,
                                title=title or "",
                                prefix="web_translate",
                                user_id=str(user_id or ""),
                            )
                            if not exported_reply:
                                exported_reply = self._export_translation_txt(
                                    translated_text=text,
                                    source=url,
                                    provider=f"melchior:{model}",
                                    mode="web_full_translation",
                                    prefix="web_translate",
                                    user_id=str(user_id or ""),
                                )
                            if exported_reply:
                                return exported_reply
                            if force_txt:
                                return "⚠️ DOCX/TXT 匯出失敗，先提供內文結果（可稍後再輸出）。\n\n" + text
                        return text
                    return f"🌐 **{title}**\n來源: {url}\n\n{text}"
            except Exception as e:
                logger.error(f"Webpage translate/summarize error: {e}")
                return f"❌ 網頁翻譯/摘要發生錯誤: {e}"

        # 2.12. Browser Automation (瀏覽器)
        if any(kw in msg_lower for kw in ["打開", "瀏覽", "browse", "open url", "截圖", "screenshot", "網頁"]):
            try:
                from skills.browser.browser_control import browse_url, take_screenshot
                # Extract URL
                url_match = re.search(r'https?://[^\s]+', message)
                if url_match:
                    url = url_match.group()
                    if "截圖" in msg_lower or "screenshot" in msg_lower:
                        return take_screenshot(url)
                    return browse_url(url)
                # Check for domain-like text
                domain_match = re.search(r'(?:打開|瀏覽|open|browse)\s+([a-zA-Z0-9][\w\-]*\.[a-zA-Z]{2,}(?:/\S*)?)', message, re.IGNORECASE)
                if domain_match:
                    url = f"https://{domain_match.group(1)}"
                    return browse_url(url)
                return "🌐 請提供要開啟的 URL，例如: `打開 https://google.com`"
            except Exception as e:
                from skills.management.issue_tracker import log_issue
                log_issue(message, str(e), "Browser Skill")
                return f"❌ 瀏覽器操作失敗，已加入夜議檢討: {e}"

        # 2.13. File Manager (檔案管理)
        if any(kw in msg_lower for kw in ["檔案", "file", "搜尋檔", "列出", "目錄"]) and any(w in msg_lower for w in ["列", "搜", "找", "list", "search", "info"]):
            try:
                from skills.ops.file_manager import list_directory, search_files
                if any(kw in msg_lower for kw in ["搜尋", "search", "找"]):
                    # Extract search term
                    return search_files(_MAGI_ROOT, message.split("搜尋")[-1].split("search")[-1].strip()[:30])
                return list_directory(_MAGI_ROOT)
            except Exception as e:
                from skills.management.issue_tracker import log_issue
                log_issue(message, str(e), "File Manager Skill")
                return f"❌ 檔案管理失敗，已加入夜議檢討: {e}"

        # 2.14. RSS Reader (RSS 閱讀器)
        if any(kw in msg_lower for kw in ["rss", "訂閱", "subscribe", "news", "新聞"]) and any(w in msg_lower for w in ["讀", "read", "訂", "sub", "add"]):
            try:
                from skills.research.rss_reader import RSSReader
                reader = RSSReader()
                
                result = ""
                # Subscribe logic
                if "訂閱" in message or "subscribe" in msg_lower or "add" in msg_lower:
                    if role != "admin":
                        return "⛔ 抱歉，只有管理員可以新增 RSS 訂閱（系統改動指令）。"
                    url_match = re.search(r'https?://[^\s]+', message)
                    if url_match:
                        result = reader.add_feed(url_match.group())
                    else:
                        result = "❌ 請提供 RSS URL，例如: `@MAGI 訂閱 https://news.google.com/rss`"
                else:
                    # List/Read logic
                    result = reader.read_latest()
                
                if result.startswith("❌"):
                    from skills.management.issue_tracker import log_issue
                    log_issue(message, result, "RSS Skill")
                    return f"{result}\n(已加入夜議檢討)"
                return result

            except Exception as e:
                from skills.management.issue_tracker import log_issue
                log_issue(message, str(e), "RSS Skill")
                return f"❌ RSS 操作失敗，已加入夜議檢討: {e}"

        # 2.15. GitHub Monitor (GitHub 監控)
        if "github" in msg_lower and any(w in msg_lower for w in ["趨勢", "trend", "search", "搜尋", "找"]):
            try:
                from skills.research.github_monitor import search_repos, get_trending
                
                result = ""
                if "趨勢" in message or "trend" in msg_lower:
                    result = get_trending()
                else:
                    # Search
                    query = message.split("搜尋")[-1].split("search")[-1].split("github")[-1].strip()
                    if not query: query = "AI Agent"
                    result = search_repos(query)
                
                if result.startswith("❌"):
                    from skills.management.issue_tracker import log_issue
                    log_issue(message, result, "GitHub Monitor Skill")
                    return f"{result}\n(已加入夜議檢討)"
                return result

            except Exception as e:
                from skills.management.issue_tracker import log_issue
                log_issue(message, str(e), "GitHub Monitor Skill")
                return f"❌ GitHub 操作失敗，已加入夜議檢討: {e}"

        # 2.16. Judgment Summary Retry Queue (實務見解摘要補跑)
        if "重試摘要佇列自動" in message or "retry_summary_queue_auto" in msg_lower:
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以執行摘要補跑（系統改動指令）。"
            try:
                import json as _json
                import subprocess as _subprocess
                py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
                if not py or not os.path.exists(py):
                    py = sys.executable or "python3"
                jc = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
                cp = _subprocess.run(
                    [py, jc, "--task", "retry_summary_queue_auto {\"notify\": false}"],
                    capture_output=True,
                    text=True,
                    timeout=420,
                )
                out = (cp.stdout or "").strip()
                if cp.returncode != 0:
                    return f"❌ 摘要補跑失敗（exit={cp.returncode}）: {(cp.stderr or out)[:220]}"
                data = {}
                try:
                    data = _json.loads(out or "{}")
                except Exception:
                    data = {}
                return (
                    "📚 摘要補跑完成\n"
                    f"- 處理: {data.get('processed', 0)}\n"
                    f"- 改善: {data.get('improved', 0)}\n"
                    f"- 剩餘: {data.get('remaining', 0)}\n"
                    f"- 模式: {data.get('mode', 'tiered')}"
                )
            except Exception as e:
                return f"❌ 摘要補跑流程失敗: {e}"

        # 2.17. Smart Summary (智能摘要)
        if any(kw in msg_lower for kw in ["摘要", "summarize", "summary", "重點"]):
            try:
                from skills.ops.smart_summary import summarize_url, extract_key_points
                url_match = re.search(r'https?://[^\s]+', message)
                if url_match:
                    return summarize_url(url_match.group())
                # Summarize the message itself
                return extract_key_points(message)
            except Exception as e:
                from skills.management.issue_tracker import log_issue
                log_issue(message, str(e), "Smart Summary Skill")
                return f"❌ 摘要失敗，已加入夜議檢討: {e}"

        # 2.18. Cortex Integration (皮質整合)
        if any(kw in msg_lower for kw in ["爬蟲", "crawler", "sync", "同步"]) and any(w in msg_lower for w in ["run", "exec", "執行", "start", "force"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以執行爬蟲/同步（系統改動指令）。"
            try:
                if "爬蟲" in message or "crawler" in msg_lower:
                    from skills.law_firm.legal_crawler_wrapper import run_crawler
                    result = run_crawler()
                elif "同步" in message or "sync" in msg_lower:
                    from skills.memory.cortex_sync import CortexSync
                    result = CortexSync().run_sync()
                else:
                    return "❓ 請指定操作: `執行爬蟲` 或 `執行同步`"

                if result.startswith("❌"):
                    from skills.management.issue_tracker import log_issue
                    log_issue(message, result, "Cortex Integration")
                    return f"{result}\n(已加入夜議檢討)"
                return result

            except Exception as e:
                from skills.management.issue_tracker import log_issue
                log_issue(message, str(e), "Cortex Integration")
                return f"❌ Cortex 操作失敗，已加入夜議檢討: {e}"

        # 2.19. Crawler Architect (爬蟲建築師)
        if (
            "修改爬蟲" in message
            or "modify crawler" in msg_lower
            or ("修改" in message and ("爬蟲" in message or "crawler" in msg_lower))
        ):
            # Only trigger if specifically asking to modify crawler
            if "爬蟲" in message or "crawler" in msg_lower:
                if role != "admin":
                    return "⛔ 抱歉，只有管理員可以修改爬蟲（系統改動指令）。"
                try:
                    requirement = message.replace("@MAGI", "").replace("修改爬蟲", "").replace("修改", "").strip()
                    if not requirement:
                        return "❓ 請說明需求，例如: `@MAGI 修改爬蟲 幫我爬 PTT Stock 版`"
                    
                    from skills.law_firm.crawler_architect import CrawlerArchitect
                    architect = CrawlerArchitect()
                    return architect.execute_modification(requirement)
                except Exception as e:
                    return f"❌ 建築師執行失敗: {e}"

        # 2.19. Auto-Skill Learning / Teaching / Internalization
        if (
            message.startswith("@MAGI 教學檔案")
            or message.startswith("@MAGI teach file")
            or message.startswith("教學檔案")
            or message.startswith("teach file")
        ):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以教學/內化檔案（系統改動指令）。"
            try:
                tip_file = (
                    message.replace("@MAGI 教學檔案", "")
                    .replace("@MAGI teach file", "")
                    .replace("教學檔案", "")
                    .replace("teach file", "")
                    .strip()
                )
                if not tip_file:
                    return "❓ 請提供教學檔案路徑，例如：`教學檔案 /path/to/notes.txt`"
                from skills.management.auto_skill import AutoSkill

                autoskill = AutoSkill()
                result = autoskill.learn_from_file(tip_file)
                return result.get("message", "📘 教學檔案已處理。")
            except Exception as e:
                return f"❌ 教學檔案處理失敗: {e}"

        if (
            message.startswith("@MAGI 教學")
            or message.startswith("@MAGI teach")
            or message.startswith("教學 ")
            or message.startswith("teach ")
        ):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以教學（系統改動指令）。"
            try:
                lesson = (
                    message.replace("@MAGI 教學", "")
                    .replace("@MAGI teach", "")
                    .replace("教學 ", "")
                    .replace("teach ", "")
                    .strip()
                )
                if not lesson:
                    return "❓ 請告訴我要學的內容，例如：`教學 遇到 timeout 要先檢查網路與服務健康`"
                from skills.management.auto_skill import AutoSkill

                autoskill = AutoSkill()
                result = autoskill.teach(lesson, context="user-teach", source=f"{platform}:{user_id}")
                return result.get("message", "🧠 教學完成。")
            except Exception as e:
                return f"❌ 教學失敗: {e}"

        # 2.19b. ClaWHub skill search / acquire with Iron Dome review (admin only)
        _clawhub_search_kws = ["搜尋skill", "搜尋 skill", "clawhub search", "skill search", "找skill", "找 skill"]
        _clawhub_install_kws = ["安裝skill", "安裝 skill", "acquire skill", "install skill", "clawhub install"]
        if role == "admin" and any(kw in msg_lower for kw in _clawhub_search_kws):
            try:
                query = re.sub(r"@magi\s+", "", msg_lower)
                for kw in _clawhub_search_kws:
                    query = query.replace(kw, "").strip()
                if not query:
                    return "❓ 請提供搜尋關鍵字，例如：`@MAGI 搜尋skill pdf converter`"
                from skills.magi.skill_acquire import search_clawhub, format_search_result
                result = search_clawhub(query)
                return format_search_result(result)
            except Exception as e:
                return f"❌ ClaWHub 搜尋失敗: {e}"

        if role == "admin" and any(kw in msg_lower for kw in _clawhub_install_kws):
            try:
                slug = re.sub(r"@magi\s+", "", message.strip())
                for kw in _clawhub_install_kws + ["@MAGI", "@magi"]:
                    slug = re.sub(re.escape(kw), "", slug, flags=re.IGNORECASE).strip()
                if not slug:
                    return "❓ 請提供 slug，例如：`@MAGI 安裝skill pdf-tools`"
                from skills.magi.skill_acquire import acquire_skill
                result = acquire_skill(slug)
                return result.get("message") or (
                    f"技能 '{slug}' 安裝成功。" if result.get("ok")
                    else f"❌ 安裝失敗：{result.get('error', '未知錯誤')}\n"
                         + ("\n".join(result.get("violations", []))[:800] if result.get("violations") else "")
                )
            except Exception as e:
                return f"❌ 技能安裝失敗: {e}"

        if (
            message.startswith("@MAGI 內化技能")
            or message.startswith("@MAGI internalize skill")
            or message.startswith("內化技能")
            or message.startswith("internalize skill")
        ):
            try:
                name = (
                    message.replace("@MAGI 內化技能", "")
                    .replace("@MAGI internalize skill", "")
                    .replace("內化技能", "")
                    .replace("internalize skill", "")
                    .strip()
                )
                from skills.management.auto_skill import AutoSkill

                autoskill = AutoSkill()
                result = autoskill.internalize_as_skill(
                    skill_name=name or "casper-learned-skill",
                    description="Internalized user-taught CASPER knowledge.",
                    auto_activate=True,
                )
                if result.get("success"):
                    return (
                        f"{result.get('message')}\n"
                        f"路徑: `{result.get('skill_path')}`"
                    )
                return f"❌ 內化技能失敗: {result.get('message')}"
            except Exception as e:
                return f"❌ 內化技能失敗: {e}"

        if message.startswith("@MAGI 記住") or message.startswith("@MAGI learn"):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以寫入長期經驗（系統改動指令）。"
            try:
                tip = message.replace("@MAGI 記住", "").replace("@MAGI learn", "").strip()
                if not tip:
                    return "❓ 請告訴我需要記住什麼經驗。"
                
                # Fingerprint from tip itself or context? Ideally context.
                # For now, just save it under extracted keywords from the tip.
                from skills.management.auto_skill import AutoSkill
                autoskill = AutoSkill()
                # Simple keyword extraction from the tip content
                keywords = [w for w in re.split(r'\s+', tip) if len(w) > 1]
                
                result = autoskill.learn(keywords, tip, context="user-manual")
                return result.get("message", "🧠 已記住。")
            except Exception as e:
                return f"❌ 記憶失敗: {e}"

        # 2.20. Council Core Approval Commands (High Priority)
        if any(kw in msg_lower for kw in ["核心變更待審", "core approvals", "pending core changes"]):
            try:
                from skills.magi.council_approval import format_pending_summary

                return format_pending_summary(limit=20)
            except Exception as e:
                return f"❌ 讀取核心待審清單失敗: {e}"

        if any(kw in msg_lower for kw in ["批准核心變更", "approve core"]):
            try:
                from skills.magi.council_approval import resolve_core_change

                text = (
                    message.replace("批准核心變更", "")
                    .replace("approve core", "")
                    .strip()
                )
                if not text:
                    return "❓ 請提供待審 ID，例如：`批准核心變更 ccr-20260213094500`"
                parts = text.split(maxsplit=1)
                approval_id = parts[0]
                note = parts[1] if len(parts) > 1 else ""
                result = resolve_core_change(approval_id, "approved", approver=user_id, note=note)
                if result.get("success"):
                    return f"✅ 核心變更已核准：`{approval_id}`"
                return f"❌ 核准失敗：{result.get('error')}"
            except Exception as e:
                return f"❌ 核准流程錯誤：{e}"

        if any(kw in msg_lower for kw in ["拒絕核心變更", "reject core"]):
            try:
                from skills.magi.council_approval import resolve_core_change

                text = (
                    message.replace("拒絕核心變更", "")
                    .replace("reject core", "")
                    .strip()
                )
                if not text:
                    return "❓ 請提供待審 ID，例如：`拒絕核心變更 ccr-20260213094500 缺少回滾方案`"
                parts = text.split(maxsplit=1)
                approval_id = parts[0]
                note = parts[1] if len(parts) > 1 else ""
                result = resolve_core_change(approval_id, "rejected", approver=user_id, note=note)
                if result.get("success"):
                    return f"🛑 核心變更已拒絕：`{approval_id}`"
                return f"❌ 拒絕失敗：{result.get('error')}"
            except Exception as e:
                return f"❌ 拒絕流程錯誤：{e}"

        # 3. [Auto-Skill] Proactive Recall
        try:
            from skills.management.auto_skill import AutoSkill
            autoskill = AutoSkill()
            tips = autoskill.recall(message)
            if tips:
                # 過濾掉佔位符和無實質內容的 tip（防止 LLM 幻覺）
                _PLACEHOLDER_MARKERS = ["此分類記錄", "經驗條目會自動添加", "最佳實踐。"]
                tips = [t for t in tips if not any(m in t for m in _PLACEHOLDER_MARKERS)]
                if tips:
                    tips_str = "\n".join(tips)
                    logger.info(f"💡 Auto-Skill Recalled: {tips_str[:120]}")
                    message += f"\n\n[Auto-Skill 經驗提示]:\n{tips_str}"
        except Exception as e:
            logger.error(f"Auto-Skill Recall Error: {e}")

        # 4. Routing via Hybrid Mode (Deep Thinking)
        if "@MAGI 深度思考" in message or "@MAGI deep" in message or "deep think" in msg_lower:
            from skills.bridge.melchior_bridge import generate_text
            
            # Remove trigger
            clean_prompt = message.replace("@MAGI 深度思考", "").replace("@MAGI deep", "").strip()
            if not clean_prompt:
                return "❓ 請輸入深度思考的內容。"

            logger.info("🚀 Routing to deep think (%s)...", TEXT_PRIMARY_MODEL)
            response = generate_text(clean_prompt)
            
            if response:
                reply = f"🧠 [Deep Think]:\n{response}"
                self._append_history(user_id, "assistant", reply)
                return reply

            fallback = self._handle_chat_async(user_id, clean_prompt, platform_hint=platform)
            reply = f"⚠️ Melchior 無回應，轉由本地 Casper 回答：\n{fallback}"
            self._append_history(user_id, "assistant", reply)
            return reply

        # 5. Intent Classification
        # Hard override: LAF report commands should always enter CMD path.
        forced_cmd = False
        if any(k in msg_lower for k in ["法扶回報指令", "法扶指令", "回報指令", "開辦回報", "開辦"]):
            forced_cmd = True
        elif self._parse_laf_report_payload(message):
            forced_cmd = True

        intent = "CMD" if forced_cmd else self.classifier.classify(message)
        logger.info(f"🧠 Detected Intent: {intent}")
        self._append_route_trace(
            str(user_id or ""),
            str(platform or ""),
            "classifier",
            str(intent or ""),
            {"role": str(role or "user")},
        )

        # 6. Routing — Embedding Router (primary) → legacy if/elif → SemanticRouter (fallback)
        response = ""

        # 6.0 Embedding-based skill dispatch (fast, runs before legacy handlers)
        # Route ONCE here; reuse result for CHAT override below (avoid duplicate embed call)
        _embed_dispatched = False
        _er_cached_result = None
        try:
            from skills.bridge.embedding_router import get_router as _get_embed_router
            _er = _get_embed_router()
            _er_cached_result = _er.route(message) if _er.is_ready else None
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5744, exc_info=True)

        if intent in ("CMD", "QUERY"):
            try:
                _er_result = _er_cached_result
                # LAF 回報指令已有專屬 handler，不讓 EmbeddingRouter 攔截
                if _er_result and not forced_cmd:
                    _er_skill, _er_score, _er_tier = _er_result
                    self._append_route_trace(
                        str(user_id or ""), str(platform or ""),
                        "embedding_router", str(_er_skill),
                        {"score": round(_er_score, 3), "tier": _er_tier, "intent": intent},
                    )
                    if _er_tier == "DIRECT":
                        _handled, _reply = self._dispatch_safe_semantic_skill(
                            user_id, message, _er_skill, role, platform
                        )
                        if _handled and _reply:
                            logger.info(f"🧭 EmbeddingRouter DIRECT dispatch: {_er_skill} ({_er_score:.3f})")
                            response = _reply
                            _embed_dispatched = True
                    elif _er_tier == "GUIDED" and intent == "QUERY":
                        # For QUERY with a GUIDED match, try the skill before falling to generic query
                        _handled, _reply = self._dispatch_safe_semantic_skill(
                            user_id, message, _er_skill, role, platform
                        )
                        if _handled and _reply:
                            logger.info(f"🧭 EmbeddingRouter GUIDED dispatch (QUERY): {_er_skill} ({_er_score:.3f})")
                            response = _reply
                            _embed_dispatched = True
                else:
                    # Fix #8: trace when embedding router returns no match
                    if _er.is_ready:
                        self._append_route_trace(
                            str(user_id or ""), str(platform or ""),
                            "embedding_router", "no_match",
                            {"intent": intent, "reason": "cooldown_or_api_error" if _er._last_embed_error else "low_score"},
                        )
            except Exception as _er_err:
                logger.debug(f"EmbeddingRouter error: {_er_err}")

        if not _embed_dispatched and intent == "CMD":
            try:
                response = self._handle_command(user_id, message, role=role, platform=platform)
            except Exception as _cmd_err:
                logger.error(f"❌ _handle_command crashed: {_cmd_err}", exc_info=True)
                response = f"❌ 指令處理失敗：{type(_cmd_err).__name__}: {str(_cmd_err)[:200]}"
            # 6a. Semantic fallback: if CMD handler returned nothing, try semantic router
            if not response:
                try:
                    from skills.bridge.semantic_router import route as _semantic_route, suggest_trigger
                    sr = _semantic_route(message)
                    if sr and sr.get("confidence", 0) >= 0.20:
                        synthetic = suggest_trigger(sr["skill"], message)
                        logger.info(f"SemanticRouter fallback: {sr['skill']} ({sr['confidence']:.2f}) → '{synthetic[:60]}'")
                        self._append_route_trace(
                            str(user_id or ""),
                            str(platform or ""),
                            "semantic_fallback",
                            str(sr["skill"]),
                            {"confidence": float(sr.get("confidence") or 0.0), "method": str(sr.get("method") or "")},
                        )
                        response = self._handle_command(user_id, synthetic, role=role, platform=platform)
                        if not response:
                            response = self._handle_query(user_id, message, platform_hint=platform)
                except Exception as _sr_err:
                    logger.debug(f"SemanticRouter error: {_sr_err}")
                # Fix #1: Log when CMD falls through all routers
                if not response:
                    logger.warning(f"⚠️ CMD fell through all routers: '{message[:80]}' → defaulting to LLM chat")
                    self._append_route_trace(
                        str(user_id or ""), str(platform or ""),
                        "cmd_fallthrough", "llm_chat",
                        {"message_preview": message[:60]},
                    )
        elif not _embed_dispatched and intent == "QUERY":
            # 6b. Before pure QUERY, check if semantic router suggests a concrete skill action
            _sr_fired = False
            try:
                from skills.bridge.semantic_router import route as _semantic_route, suggest_trigger
                sr = _semantic_route(message)
                if sr and sr.get("confidence", 0) >= 0.28 and sr.get("method") in {"semantic", "llm"}:
                    synthetic = suggest_trigger(sr["skill"], message)
                    logger.info(f"SemanticRouter QUERY override: {sr['skill']} ({sr['confidence']:.2f})")
                    self._append_route_trace(
                        str(user_id or ""),
                        str(platform or ""),
                        "semantic_override",
                        str(sr["skill"]),
                        {"confidence": float(sr.get("confidence") or 0.0), "method": str(sr.get("method") or "")},
                    )
                    _candidate = self._handle_command(user_id, synthetic, role=role, platform=platform)
                    if _candidate:
                        response = _candidate
                        _sr_fired = True
            except Exception as _sr_err:
                logger.debug(f"SemanticRouter QUERY check error: {_sr_err}")
            if not _sr_fired and self._should_start_skill_interview_from_gap(message, role, intent="QUERY", er_result=_er_cached_result):
                response = self._start_skill_interview(
                    str(user_id or ""),
                    str(platform or ""),
                    role,
                    message,
                    trigger_reason="gap",
                )
            if not response:
                response = self._handle_query(user_id, message, platform_hint=platform)
        elif intent == "CHAT":
            # 6c. Even for CHAT, check if embedding router has a DIRECT match
            # (catches cases where IntentionClassifier misses actionable messages)
            # Reuse _er_cached_result from above — no duplicate embedding call
            # 2026-03-29: In general/LINE channels (no topic), raise threshold to
            # 0.85 to prevent casual mentions from hijacking conversations.
            if not _embed_dispatched:
                try:
                    _er_result = _er_cached_result
                    if _er_result:
                        _er_skill, _er_score, _er_tier = _er_result
                        from skills.bridge.embedding_router import _DIRECT_THRESH
                        _chat_er_thresh = 0.85 if not _topic_key or _topic_key == "general" else _DIRECT_THRESH
                        if _er_tier == "DIRECT" and _er_score >= _chat_er_thresh:
                            _handled, _reply = self._dispatch_safe_semantic_skill(
                                user_id, message, _er_skill, role, platform
                            )
                            if _handled and _reply:
                                logger.info(f"🧭 EmbeddingRouter CHAT override: {_er_skill} ({_er_score:.3f})")
                                self._append_route_trace(
                                    str(user_id or ""), str(platform or ""),
                                    "embedding_chat_override", str(_er_skill),
                                    {"score": round(_er_score, 3)},
                                )
                                response = _reply
                                _embed_dispatched = True
                except Exception as _er_err:
                    logger.debug(f"EmbeddingRouter CHAT check error: {_er_err}")
            if not _embed_dispatched and self._should_start_skill_interview_from_gap(message, role, intent="CHAT", er_result=_er_cached_result):
                response = self._start_skill_interview(
                    str(user_id or ""),
                    str(platform or ""),
                    role,
                    message,
                    trigger_reason="gap",
                )
                _embed_dispatched = True
            if not _embed_dispatched:
                response = self._handle_chat_async(user_id, message, platform_hint=platform)
        elif intent == "DANGER":
            # Second-pass guard against false positives:
            # only trigger Iron Dome "danger" flow when deterministic destructive
            # command patterns are actually present in the message.
            danger_hit = re.search(
                r"(rm\s+-rf|drop\s+table|delete\s+from|truncate\s+table|shutdown\s+-h|reboot\s+now)",
                message or "",
                re.IGNORECASE,
            )
            if not danger_hit:
                logger.warning(
                    "⚠️ Intent was DANGER but no deterministic destructive token matched; downgraded to CHAT. user=%s platform=%s",
                    user_id,
                    platform,
                )
                response = self._handle_chat_async(user_id, message, platform_hint=platform)
            else:
                try:
                    alert_iron_dome_violation("Dangerous Command", f"{platform}:{user_id}", message)
                    try:
                        from skills.evolution.skill_genesis import auto_harden_iron_dome_scope

                        auto_harden_iron_dome_scope(message, source=f"{platform}:{user_id}", max_new=2)
                    except Exception as e:
                        logger.warning(f"Iron Dome auto-harden (intent danger) skipped: {e}")
                except Exception as e:
                    logger.warning(f"Iron Dome alert failed: {e}")
                response = "🛡️ 已偵測高風險指令，已啟動防護並記錄事件。請改用安全且可審核的操作。"
        else:
            response = self._handle_chat_async(user_id, message, platform_hint=platform)

        # If we flagged an ambiguous rule, append a quick confirmation question.
        if rule_flag == "ASK_CONFIRM":
            response = (response or "").rstrip() + (
                "\n\n我有點不確定你這句話要不要當成「規則」記起來。\n"
                "要記的話回我：`要`；不記回我：`不要`；要改寫回我：`改成：...`"
            )

        self._append_history(user_id, "assistant", response)
        return response

    def _handle_multimedia(self, user_id, prompt, attachment):
        """
        Routes file attachments to appropriate skills.
        """
        msg_type = attachment['type']
        path = attachment['path']
        
        if msg_type == "image":
            # --- Payment proof intercept: detect 繳費 keywords in prompt ---
            prompt_lower = (prompt or "").lower()
            _payment_kw = ["繳費", "繳款", "繳費憑證", "繳費單", "繳費截圖", "payment proof",
                           "上傳繳費", "銷帳", "入帳"]
            if any(kw in prompt_lower for kw in _payment_kw):
                logger.info(f"💰 Payment proof detected via channel image: {path}")
                try:
                    return self._handle_payment_proof_from_channel(path)
                except Exception as pay_err:
                    logger.error(f"Payment proof upload from channel failed: {pay_err}")
                    return f"❌ 繳費憑證上傳失敗：{str(pay_err)[:200]}"

            logger.info(f"👁️ Routing Image to Melchior: {path}")
            # Use Melchior Bridge
            description = analyze_image(path, prompt=prompt)
            return f"👁️ Melchior: {description}"
            
        elif msg_type == "audio":
            logger.info(f"🎙️ Routing Audio to unified transcription pipeline: {path}")
            _transcribe_task_id = f"transcribe_{id(path)}_{time.time():.0f}"
            self.register_heavy_task(_transcribe_task_id, "逐字稿")
            try:
                prompt_lower = (prompt or "").lower()
                wants_translate = any(k in prompt_lower for k in ["translate", "翻譯", "翻成"])
                wants_summary = any(k in prompt_lower for k in ["summary", "摘要", "重點"])
                no_summary = any(k in prompt_lower for k in ["不要摘要", "不用摘要", "不需要摘要", "不要總結", "不用總結", "不需要總結"])
                if no_summary:
                    wants_summary = False
                summary_length = self._detect_summary_length(prompt or "")
                summary_pref = self._detect_summary_target_pref(prompt_lower)
                disable_txt = any(k in prompt_lower for k in ["不要txt", "不需要txt", "no txt", "no file"])
                disable_timestamps = any(k in prompt_lower for k in ["不要時間戳", "不要時間碼", "no timestamp", "without timestamp", "純文字"])
                # Audio transcription defaults to timestamp + TXT, unless user explicitly disables.
                wants_txt = not disable_txt
                wants_timestamps = not disable_timestamps
                taigi_hint = any(k in prompt_lower for k in ["台語", "臺語", "閩南語", "hokkien", "taigi", "tai-gi"])
                force_non_zh = any(k in prompt_lower for k in [" english", "英文", "en-us", "en-uk", "日文", "japanese", "日本語"])
                has_cjk_prompt = bool(re.search(r"[\u4e00-\u9fff]", prompt or ""))
                language_hint = None if force_non_zh else ("zh" if (taigi_hint or has_cjk_prompt or not prompt_lower.strip()) else None)
                initial_prompt_hint = ""
                if language_hint == "zh":
                    initial_prompt_hint = (
                        "這段音訊可能包含華語與臺灣口語，請盡量以繁體中文準確轉寫，必要時保留台語詞彙。"
                        "常見用語：原告、被告、聲請人、相對人、法院、法官、檢察官、律師、"
                        "委任狀、起訴狀、答辯狀、準備書狀、調解、和解、判決、裁定、"
                        "民事、刑事、行政訴訟、強制執行、假扣押、假處分、"
                        "勞動基準法、民法、刑法、公司法、著作權法、"
                        "當事人、證人、鑑定人、書記官、庭期、開庭、筆錄、"
                        "損害賠償、違約金、利息、遲延利息、訴訟費用。"
                    )
                if taigi_hint:
                    initial_prompt_hint = (
                        "這段音訊可能包含台語（臺灣閩南語）與華語，請盡量以繁體中文準確轉寫。"
                        "常見用語：原告、被告、法院、律師、判決、調解、和解。"
                    )

                from skills.bridge.balthasar_bridge import transcribe as transcribe_audio
                tr = transcribe_audio(
                    path,
                    language=language_hint,
                    initial_prompt=initial_prompt_hint or None,
                    taigi_hint=taigi_hint,
                )
                transcript = str((tr or {}).get("text") or "").strip()
                if not transcript:
                    err = str((tr or {}).get("error") or "transcription_failed").strip()[:300]
                    logger.warning(f"Audio transcription failed: {err}")
                    return "⚠️ 語音已接收，但目前無法完成轉錄。請稍後再試，或在訊息加上「台語」再重試。"
                force_txt = (
                    "full translation without summary" in prompt_lower
                    or "完整翻譯不摘要" in prompt_lower
                    or wants_txt
                )

                segments = tr.get("segments") if isinstance(tr, dict) else []
                timestamp_text = str((tr or {}).get("timestamp_text") or "").strip()
                if (not timestamp_text) and isinstance(segments, list) and segments:
                    def _normalize_ts_sec(v: float) -> float:
                        try:
                            x = float(v)
                        except Exception:
                            return 0.0
                        if x >= 20000.0:
                            x = x / 1000.0
                        return max(0.0, x)

                    def _fmt_hhmmss(sec: float) -> str:
                        try:
                            total = int(_normalize_ts_sec(sec))
                        except Exception:
                            total = 0
                        hh = total // 3600
                        mm = (total % 3600) // 60
                        ss = total % 60
                        return f"{hh:02d}:{mm:02d}:{ss:02d}"
                    lines = []
                    for seg in segments:
                        if not isinstance(seg, dict):
                            continue
                        st = _normalize_ts_sec(seg.get("start", 0.0))
                        txt = str(seg.get("text") or "").strip()
                        if txt:
                            lines.append(f"[{_fmt_hhmmss(st)}] {txt}")
                    timestamp_text = "\n".join(lines).strip()

                # --- Post-process: punctuation correction for Chinese transcripts ---
                if language_hint == "zh" and len(transcript) > 30:
                    try:
                        from skills.bridge import melchior_client as _pp_mc
                        _pp_prompt = (
                            "你是中文標點修正工具。請修正以下逐字稿的標點符號與斷句，"
                            "只修標點和段落分隔，不要更改任何用詞或內容。"
                            "直接輸出修正後的全文，不要加任何說明。\n\n"
                            f"{transcript}"
                        )
                        _pp_ctx = min(16384, max(4096, len(transcript) * 2))
                        _pp = _pp_mc.quick_local_chat(
                            _pp_prompt, timeout=30, model_hint=TEXT_PRIMARY_MODEL,
                            num_ctx=_pp_ctx, num_predict=min(4096, max(1024, len(transcript) + 200)),
                        )
                        if _pp.get("success") and _pp.get("response"):
                            _pp_out = str(_pp["response"]).strip()
                            # Only use if output is reasonably similar length (not hallucinated)
                            if 0.7 < len(_pp_out) / max(1, len(transcript)) < 1.4:
                                transcript = _pp_out
                                logger.info("Transcript punctuation corrected by taide-12b")
                    except Exception as _pp_err:
                        logger.debug("Transcript punctuation correction skipped: %s", _pp_err)

                if len(transcript) > 30:
                    try:
                        from skills.bridge.openclaw_codex_bridge import feature_enabled as _codex_feature_enabled, polish_transcript_with_codex

                        codex_max_chars = int(os.environ.get("MAGI_CODEX_TRANSCRIPT_MAX_CHARS", "14000") or "14000")
                        if _codex_feature_enabled("transcript") and len(transcript) <= max(1200, codex_max_chars):
                            codex_res = polish_transcript_with_codex(
                                transcript,
                                timeout_sec=int(os.environ.get("MAGI_CODEX_TRANSCRIPT_TIMEOUT_SEC", "240") or "240"),
                            )
                            codex_text = str(codex_res.get("text") or "").strip()
                            if codex_res.get("success") and codex_text:
                                ratio = len(codex_text) / max(1, len(transcript))
                                if 0.7 < ratio < 1.6:
                                    transcript = codex_text
                                    logger.info("Transcript polished by Codex")
                            elif codex_res.get("error"):
                                logger.warning("Transcript Codex polish failed: %s", codex_res.get("error"))
                    except Exception as codex_err:
                        logger.debug("Transcript Codex polish skipped: %s", codex_err)

                final_text = transcript
                title = "🎙️ 語音逐字稿"

                # --- Parallel execution: run translation & summary concurrently when possible ---
                # When both translate and summary are requested and the summary does not
                # depend on the translated output, run them in parallel to halve wait time.
                _audio_can_parallel = wants_translate and wants_summary and summary_pref != "translated"

                if _audio_can_parallel:
                    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="audio_ts") as _ats_pool:
                        _tr_future = _ats_pool.submit(
                            self._translate_text_complete,
                            transcript,
                            source_lang="auto",
                            target_lang="繁體中文",
                        )
                        _sm_future = _ats_pool.submit(
                            self._summarize_text_resilient,
                            transcript,
                            summary_length=summary_length,
                            progress_callback=getattr(self, "_progress_callback", None),
                        )

                    # Collect translation result.
                    try:
                        rr = _tr_future.result()
                        if isinstance(rr, dict) and rr.get("success"):
                            t = str(rr.get("text") or "").strip()
                            if t:
                                final_text = t
                                title = "🌐 語音翻譯結果"
                    except Exception as translate_err:
                        logger.warning(f"Audio translation skipped due to error: {translate_err}")

                    # Collect summary result (ran on transcript / source).
                    summary_text = ""
                    summary_source_label = "逐字稿原文"
                    try:
                        summary_res = _sm_future.result()
                        if isinstance(summary_res, dict) and summary_res.get("success"):
                            summary_text = str(summary_res.get("text") or summary_res.get("summary") or "").strip()
                    except Exception as summarize_err:
                        logger.warning(f"Audio summary fallback due to error: {summarize_err}")
                else:
                    # Sequential path (original behaviour).
                    if wants_translate:
                        try:
                            rr = self._translate_text_complete(
                                transcript,
                                source_lang="auto",
                                target_lang="繁體中文",
                            )
                            if isinstance(rr, dict) and rr.get("success"):
                                t = str(rr.get("text") or "").strip()
                                if t:
                                    final_text = t
                                    title = "🌐 語音翻譯結果"
                        except Exception as translate_err:
                            logger.warning(f"Audio translation skipped due to error: {translate_err}")

                    summary_text = ""
                    summary_source_label = ""
                    if wants_summary:
                        try:
                            summary_target_text = final_text
                            if summary_pref == "source":
                                summary_target_text = transcript
                                summary_source_label = "逐字稿原文"
                            elif summary_pref == "translated":
                                summary_target_text = final_text
                                summary_source_label = "翻譯結果" if wants_translate else "逐字稿原文"
                            else:
                                summary_source_label = "翻譯結果" if wants_translate else "逐字稿原文"
                            summary_res = self._summarize_text_resilient(
                                summary_target_text,
                                summary_length=summary_length,
                                progress_callback=getattr(self, "_progress_callback", None),
                            )
                            if summary_res.get("success"):
                                summary_text = str(summary_res.get("text") or summary_res.get("summary") or "").strip()
                        except Exception as summarize_err:
                            logger.warning(f"Audio summary fallback due to error: {summarize_err}")

                export_text = final_text
                if wants_timestamps and timestamp_text:
                    export_text = f"【時間戳記】\n{timestamp_text}\n\n【全文】\n{final_text}".strip()

                if force_txt or len(export_text) > 2500:
                    try:
                        from skills.ops.export_text import export_txt
                        exported = export_txt(export_text, prefix="audio_transcription")
                        if exported.get("success"):
                            path_out = str(exported.get("path") or "").strip()
                            url_out = str(exported.get("url") or "").strip()
                            head = "📄 已輸出逐字稿 TXT 檔案。"
                            if wants_timestamps:
                                head = "📄 已輸出含時間戳記的逐字稿 TXT 檔案。"
                            if url_out:
                                head = f"{head}\n{url_out}"
                            if summary_text:
                                head = f"📝 語音重點摘要（來源：{summary_source_label}）：\n{summary_text}\n\n{head}"
                            if self._is_file_protocol_user(str(user_id or "")) and path_out:
                                return f"{head}|||FILE_PATH|||{path_out}"
                            return f"{head}\n{path_out}".strip()
                    except Exception as e:
                        logger.error(f"TXT Export error in orchestrator audio: {e}")
                if summary_text:
                    return f"📝 語音重點摘要（來源：{summary_source_label}）：\n{summary_text}\n\n{title}：\n{final_text[:1200]}"

                if wants_timestamps and timestamp_text:
                    preview_lines = timestamp_text.splitlines()
                    preview = "\n".join(preview_lines[:24]).strip()
                    if len(preview_lines) > 24:
                        preview += "\n…（其餘內容可加上「請給我TXT」取得完整檔案）"
                    return f"{title}（含時間戳記）：\n{preview}"

                return f"{title}：\n{final_text}"
            except Exception as e:
                logger.error(f"❌ Audio routing error: {e}")
                return "❌ 語音處理失敗：音訊模組執行異常（已記錄）。請稍後再試。"
            finally:
                self.unregister_heavy_task(_transcribe_task_id)

        elif msg_type == "file":
            filename = attachment.get('filename', '')
            logger.info(f"📄 Routing File: {filename}")
            prompt_lower = (prompt or "").lower()
            wants_translate = any(k in prompt_lower for k in ["翻譯", "translate", "翻成"])
            wants_summary = any(k in prompt_lower for k in ["摘要", "總結", "重點", "summary", "summarize"])
            no_summary = any(k in prompt_lower for k in ["不要摘要", "不用摘要", "不需要摘要", "不要總結", "不用總結", "不需要總結"])
            if no_summary:
                wants_summary = False
            summary_length = self._detect_summary_length(prompt or "")
            summary_pref = self._detect_summary_target_pref(prompt_lower)
            disable_txt = any(k in prompt_lower for k in ["不要txt", "不需要txt", "no txt", "inline", "直接貼上"])
            explicit_txt = any(k in prompt_lower for k in ["txt", "文字檔", "檔案", "download", "下載"])
            try:
                summary_txt_default = os.environ.get("MAGI_FILE_SUMMARY_EXPORT_TXT_DEFAULT", "1").strip().lower() in {"1", "true", "yes", "on"}
            except Exception:
                summary_txt_default = True
            summary_force_txt = (not disable_txt) and (explicit_txt or summary_txt_default)

            if wants_translate:
                extracted = self._extract_text_from_uploaded_file(path, filename=filename)
                if not extracted.get("success"):
                    return (
                        f"📄 檔案 `{filename or os.path.basename(path)}` 已接收，但目前無法做全文翻譯：{extracted.get('error')}\n"
                        "已支援：PDF、EPUB、TXT、MD、LOG、CSV、JSON、DOCX。"
                    )

                src_text = self._prepare_document_text_for_llm(str(extracted.get("text") or ""))
                src_text, was_capped = self._cap_translation_source_text(src_text)
                if not src_text:
                    return "⚠️ 檔案內容為空，無法翻譯。"

                try:
                    auto_ingest = os.environ.get("MAGI_DOC_AUTO_INGEST", "1").strip().lower() in {"1", "true", "yes", "on"}
                except Exception:
                    auto_ingest = True
                ingest_queued = False
                if auto_ingest:
                    ingest_queued = self._ingest_uploaded_text_async(
                        kind=str(extracted.get("kind") or "file"),
                        primary=path,
                        title=str(extracted.get("title") or filename or os.path.basename(path)),
                        text=src_text,
                    )

                # --- Parallel execution: run translation & summary concurrently when possible ---
                # When summary_pref is "translated", summary depends on translation output,
                # so they must stay sequential. Otherwise (source / auto), summary can use
                # src_text directly and run in parallel with translation.
                _can_parallel_summary = wants_summary and summary_pref != "translated"

                if _can_parallel_summary:
                    # Run translation and summary in parallel on independent inputs.
                    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="file_ts") as _ts_pool:
                        _translate_future = _ts_pool.submit(
                            self._translate_text_complete, src_text,
                            source_lang="auto", target_lang="繁體中文",
                        )
                        _summary_future = _ts_pool.submit(
                            self._summarize_text_resilient, src_text,
                            summary_length, progress_callback=getattr(self, '_progress_callback', None),
                        )
                    try:
                        rr = _translate_future.result()
                    except Exception as e:
                        rr = {"success": False, "error": str(e)}
                    try:
                        sr = _summary_future.result()
                    except Exception as e:
                        sr = {"success": False, "error": str(e)}
                else:
                    # Sequential: translate first (summary may depend on result).
                    try:
                        rr = self._translate_text_complete(src_text, source_lang="auto", target_lang="繁體中文")
                    except Exception as e:
                        rr = {"success": False, "error": str(e)}
                    sr = None  # will be computed below if needed

                if not rr.get("success"):
                    err = str(rr.get("error") or "translate_failed").strip()[:260]
                    if err.startswith("translation_off_topic:"):
                        err = "偵測到翻譯結果偏題，已中止回傳以避免送出錯誤內容"
                    base = f"❌ 檔案翻譯失敗：{err}"
                    if ingest_queued:
                        base += "\n🧠 文件內容已排入背景吸收。"
                    return base

                # Use plain translated text (not markdown table) for exports
                _plain_translated = str(rr.get("translated_text") or rr.get("text") or "").strip()
                translated_text = self._polish_translated_document_text(_plain_translated)
                if not translated_text:
                    return "⚠️ 檔案翻譯結果為空。請稍後再試。"
                # Chunk-level source/target pairs for DOCX bilingual table
                _src_chunks = rr.get("source_chunks") or []
                _tgt_chunks = rr.get("translated_chunks") or []
                summary_text = ""
                summary_note = ""
                summary_source_label = "翻譯結果"
                if wants_summary:
                    if _can_parallel_summary:
                        # Summary already computed in parallel on src_text.
                        summary_source_label = "原文"
                        if sr.get("success"):
                            summary_text = str(sr.get("text") or "").strip()
                        else:
                            summary_note = f"⚠️ 摘要產生失敗：{str(sr.get('error') or 'summary_failed')[:120]}"
                    else:
                        # Sequential path: summary_pref == "translated", use translated_text.
                        summary_target_text = translated_text
                        summary_source_label = "翻譯結果"
                        sr = self._summarize_text_resilient(summary_target_text, summary_length=summary_length, progress_callback=getattr(self, '_progress_callback', None))
                        if sr.get("success"):
                            summary_text = str(sr.get("text") or "").strip()
                        else:
                            summary_note = f"⚠️ 摘要產生失敗：{str(sr.get('error') or 'summary_failed')[:120]}"

                ingest_note = ""
                if ingest_queued:
                    ingest_note = "🧠 文件內容已排入背景吸收。"
                fail_cnt = int(rr.get("chunks_failed") or 0)
                fail_note = ""
                if fail_cnt > 0:
                    fail_note = f"⚠️ 有 {fail_cnt} 個段落翻譯失敗，已先保留原文，稍後可針對該段重跑。"
                export_body = translated_text
                if summary_text:
                    _sl_label = {"short": "精簡", "long": "詳細"}.get(summary_length, "")
                    _sl_tag = f"{_sl_label}摘要" if _sl_label else "摘要"
                    export_body = f"【{_sl_tag}（來源：{summary_source_label}）】\n{summary_text}\n\n【全文翻譯】\n{translated_text}".strip()

                if not disable_txt:
                    # Prefer DOCX bilingual table, fallback to TXT
                    exported_reply = self._export_translation_docx(
                        source_text=locals().get("src_text", ""),
                        translated_text=translated_text,
                        source_chunks=_src_chunks,
                        translated_chunks=_tgt_chunks,
                        title=(filename or os.path.basename(path)),
                        prefix="file_translate",
                        user_id=str(user_id or ""),
                    )
                    if not exported_reply:
                        exported_reply = self._export_translation_txt(
                            translated_text=export_body,
                            source=(filename or os.path.basename(path)),
                            provider=str(rr.get("provider") or "tri-sage"),
                            mode="file_translate_with_summary" if wants_summary else "file_full_translation",
                            prefix="file_translate",
                            user_id=str(user_id or ""),
                        )
                    if exported_reply:
                        extra_notes = "\n".join([n for n in [summary_note, fail_note, ingest_note] if n]).strip()
                        if "|||FILE_PATH|||" in exported_reply:
                            if extra_notes:
                                head, tail = exported_reply.split("|||FILE_PATH|||", 1)
                                return f"{head}\n{extra_notes}|||FILE_PATH|||{tail}"
                            return exported_reply
                        if extra_notes:
                            return f"{exported_reply}\n{extra_notes}"
                        return exported_reply

                prefix = "🌐 檔案翻譯結果：\n"
                if was_capped:
                    prefix = "🌐 檔案翻譯結果（內容過長，已截斷後翻譯）：\n"
                out = prefix + export_body
                if summary_note:
                    out += f"\n\n{summary_note}"
                if fail_note:
                    out += f"\n\n{fail_note}"
                if ingest_note:
                    out += f"\n\n{ingest_note}"
                return out
            
            # PDF Processing
            if filename.lower().endswith('.pdf'):
                logger.info(f"📄 Processing PDF: {path}")
                # Go directly to pdf_bridge.summarize_pdf which has map-reduce
                # for large docs. Avoids wasting 120s on _summarize_text_resilient
                # single-shot attempt that always times out on large PDFs.
                from skills.documents.pdf_bridge import summarize_pdf
                out = str(
                    summarize_pdf(
                        path,
                        progress_callback=getattr(self, '_progress_callback', None),
                        summary_length=summary_length,
                    )
                    or ""
                ).strip()
                if summary_force_txt and out:
                    exported_reply = self._export_summary_docx_or_txt(
                        out, prefix="pdf_summary", title=(filename or "PDF 摘要"),
                        user_id=str(user_id or ""), source_path=path,
                    )
                    if exported_reply:
                        return exported_reply
                return out

            # EPUB Processing
            elif filename.lower().endswith('.epub'):
                logger.info(f"📚 Processing EPUB: {path}")
                from skills.documents.epub_bridge import summarize_epub
                out = str(summarize_epub(path) or "").strip()
                if summary_force_txt and out:
                    exported_reply = self._export_summary_docx_or_txt(
                        out, prefix="epub_summary", title=(filename or "EPUB 摘要"),
                        user_id=str(user_id or ""), source_path=path,
                    )
                    if exported_reply:
                        return exported_reply
                return out

            # Common text/office docs
            elif any(filename.lower().endswith(ext) for ext in [".txt", ".md", ".log", ".csv", ".json", ".docx"]):
                from skills.documents.file_bridge import summarize_file
                out = str(summarize_file(path, filename=filename) or "").strip()
                if summary_force_txt and out:
                    exported_reply = self._export_summary_docx_or_txt(
                        out, prefix="doc_summary", title=(filename or "檔案摘要"),
                        user_id=str(user_id or ""), source_path=path,
                    )
                    if exported_reply:
                        return exported_reply
                return out
            
            # Other files
            else:
                return (
                    f"📄 檔案 '{filename}' 已接收，但目前不支援此格式摘要。\n"
                    "已支援：PDF、EPUB、TXT、MD、LOG、CSV、JSON、DOCX。"
                )
            
        return (
            "⚠️ 不支援此附件類型。\n"
            "目前支援的格式：PDF、EPUB、TXT、MD、LOG、CSV、JSON、DOCX。\n"
            "圖片（PNG/JPG）請直接傳送，不要以檔案方式上傳。"
        )

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
        """
        Cross-node collaboration health summary (Melchior / Balthasar / Watcher).
        """
        lines = ["🤝 **協作鏈路健康度**"]

        # Melchior
        try:
            from skills.bridge.melchior_client import check_health as melchior_health
            mh = melchior_health()
            if mh.get("online"):
                models = mh.get("models") or []
                has_main20 = any(TEXT_PRIMARY_MODEL.lower() in str(m).lower() for m in models)
                lines.append(
                    f"🟢 Melchior: {mh.get('mode', 'unknown')} / v{mh.get('ollama_version', 'n/a')} / "
                    f"Main20B={'yes' if has_main20 else 'no'}"
                )
            else:
                lines.append("🔴 Melchior: offline")
        except Exception as e:
            lines.append(f"🟡 Melchior: status unavailable ({e})")

        # Balthasar
        try:
            from skills.bridge.balthasar_bridge import check_health as balthasar_health
            ok, msg = balthasar_health()
            if ok:
                lines.append(f"🟢 Balthasar: {msg}")
            else:
                # In normal operations, Balthasar is council-only; Casper provides proxy capabilities.
                if "council-only" in str(msg).lower():
                    lines.append("🟣 Balthasar: council-only (proxy on Casper for summarize/transcribe)")
                else:
                    lines.append(f"🔴 Balthasar: {msg}")
        except Exception as e:
            lines.append(f"🟡 Balthasar: status unavailable ({e})")

        # Watcher
        try:
            from skills.bridge.watcher_bridge import check_health as watcher_health
            ok, msg = watcher_health()
            lines.append(f"{'🟢' if ok else '🔴'} Watcher: {msg}")
        except Exception as e:
            lines.append(f"🟡 Watcher: status unavailable ({e})")

        return "\n".join(lines)

    def _get_schedule(self):
        """Get upcoming meetings from law_firm_data database."""
        try:
            from skills.law_firm.manage_meetings import list_meetings
            from datetime import datetime, timedelta
            
            result = list_meetings()
            
            db_items = []
            if result.get("success") and result.get("data"):
                meetings = result["data"]
                for m in meetings[:7]:  # Limit to 7 entries
                    dt_str = m.get('datetime', '')
                    if dt_str:
                        try:
                            dt = datetime.fromisoformat(dt_str)
                            date_fmt = dt.strftime("%m/%d %H:%M")
                        except Exception:
                            date_fmt = dt_str[:16]
                    else:
                        date_fmt = "待定"

                    meeting_type = m.get('type', '會議')
                    client = m.get('client_name', '')
                    location = m.get('location', '')

                    line = f"• **{date_fmt}** - {meeting_type}"
                    if client:
                        line += f" ({client})"
                    if location:
                        line += f" @ {location}"
                    db_items.append(line)

            # ── Google Calendar 查詢 ──
            gcal_items = []
            try:
                import importlib, importlib.util
                from api.runtime_paths import get_config_path
                credentials_path = str(get_config_path("credentials.json"))
                token_path = str(get_config_path("google_calendar_token.json"))
                # osc-orchestrator uses hyphen — must use importlib
                spec = importlib.util.spec_from_file_location(
                    "osc_orchestrator_action",
                    os.path.join(os.environ.get("MAGI_ROOT_DIR", ""), "skills", "osc-orchestrator", "action.py"),
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                svc = mod._build_google_calendar_service(credentials_path, token_path, interactive=False)
                if svc.get("ok") and svc.get("service"):
                    service = svc["service"]
                    from datetime import timezone
                    tz = timezone(timedelta(hours=8))
                    now = datetime.now(tz)
                    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                    today_end = (now + timedelta(days=7)).replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
                    events_result = service.events().list(
                        calendarId='primary',
                        timeMin=today_start,
                        timeMax=today_end,
                        singleEvents=True,
                        orderBy='startTime',
                        maxResults=20,
                    ).execute()
                    for ev in events_result.get('items', []):
                        start_raw = ev['start'].get('dateTime', ev['start'].get('date', ''))
                        summary = ev.get('summary', '(無標題)')
                        ev_location = ev.get('location', '')
                        try:
                            dt_ev = datetime.fromisoformat(start_raw)
                            date_fmt = dt_ev.strftime("%m/%d %H:%M")
                        except Exception:
                            date_fmt = start_raw[:16] if start_raw else "待定"
                        line = f"• **{date_fmt}** - {summary}"
                        if ev_location:
                            line += f" @ {ev_location}"
                        gcal_items.append(line)
            except Exception as e:
                logger.warning(f"Google Calendar query failed: {e}")

            # ── 合併結果 ──
            all_items = db_items + gcal_items
            if all_items:
                response = "📅 **近期行程**\n\n"
                response += "\n".join(all_items) + "\n"
                if gcal_items:
                    response += f"\n_(含 {len(gcal_items)} 筆 Google 日曆行程)_"
                return response
            else:
                return "📅 目前沒有排定的行程。"

        except Exception as e:
            logger.error(f"❌ Schedule query error: {e}")
            return f"⚠️ 無法讀取行程: {e}"

    @staticmethod
    def _translate_prompt_to_english(prompt: str) -> str:
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

    def _auto_acquire_and_execute(self, user_id, message, platform: str = "LINE"):
        """
        Autonomous capability upgrade with auto-retry:
        acquire skill -> validate/activate -> optionally execute action.py.
        ★ 背景執行緒跑（不卡 reply_token），超時自動重試（最多 3 次），每次通知使用者。
        """
        # Concurrency guard: prevent duplicate forge for same user
        uid = str(user_id)
        lock = self._forge_locks.setdefault(uid, threading.Lock())
        if not lock.acquire(blocking=False):
            return "⏳ 技能生成已在進行中，請稍候上一個完成…"

        def _notify(text: str):
            try:
                cb = getattr(self, "notification_callback", None)
                if cb:
                    cb(str(user_id), text, platform)
                else:
                    logger.warning("No notification_callback set, forge notification lost")
            except Exception as e:
                logger.warning(f"Forge notification callback failed: {e}")

        def _rebuild_embed_cache():
            try:
                from skills.bridge.embedding_router import get_router as _get_embed_router
                _er = _get_embed_router()
                if _er.is_ready:
                    _er.rebuild_cache()
                    logger.info("🔄 Embedding router cache rebuilt after skill genesis")
            except Exception as e:
                logger.debug(f"Embedding router rebuild after genesis: {e}")

        def _run_forge_with_retry():
            import concurrent.futures
            from skills.evolution.intent_forge import forge_execute

            max_retries = self._FORGE_MAX_RETRIES
            timeouts = self._FORGE_TIMEOUT_SCHEDULE

            for attempt in range(1, max_retries + 1):
                timeout = timeouts[min(attempt - 1, len(timeouts) - 1)]
                logger.info(f"🧬 Forge attempt {attempt}/{max_retries}, timeout={timeout}s")

                try:
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix=f"forge-r{attempt}"
                    ) as pool:
                        future = pool.submit(
                            forge_execute, str(user_id), message, "", "orchestrator_auto"
                        )
                        reply = future.result(timeout=timeout)

                    # Success
                    msg = reply.get("reply", "ℹ️ 自主演化流程完成。") if isinstance(reply, dict) else str(reply)
                    success = reply.get("success", False) if isinstance(reply, dict) else bool(msg)

                    if success or attempt == max_retries:
                        _notify(msg)
                        _rebuild_embed_cache()
                        return

                    # forge_execute returned but reported failure — retry
                    logger.warning(f"Forge attempt {attempt} failed (non-success): {msg[:200]}")
                    if attempt < max_retries:
                        _notify(
                            f"⏳ 技能生成第 {attempt} 次未成功，MAGI 正在自動重試"
                            f"（第 {attempt + 1}/{max_retries} 次）…"
                        )

                except concurrent.futures.TimeoutError:
                    logger.warning(f"Forge attempt {attempt} timed out after {timeout}s")
                    if attempt < max_retries:
                        _notify(
                            f"⏳ 技能生成第 {attempt} 次超時（{timeout}s），MAGI 正在自動接續"
                            f"（第 {attempt + 1}/{max_retries} 次，上限 {timeouts[min(attempt, len(timeouts) - 1)]}s）…"
                        )
                    else:
                        _notify(
                            f"❌ 技能生成經過 {max_retries} 次嘗試仍未完成。\n"
                            f"累計等待約 {sum(timeouts[:max_retries]) // 60} 分鐘。\n"
                            "建議：簡化指令再試一次，或手動建立技能。"
                        )
                        return

                except Exception as e:
                    logger.error(f"Forge attempt {attempt} error: {e}")
                    if attempt < max_retries:
                        _notify(
                            f"⚠️ 技能生成第 {attempt} 次遇到錯誤：{str(e)[:100]}\n"
                            f"MAGI 正在自動重試（第 {attempt + 1}/{max_retries} 次）…"
                        )
                    else:
                        _notify(f"❌ 技能生成失敗（{max_retries} 次嘗試）：{str(e)[:200]}")
                        return

        def _run_forge_with_lock():
            try:
                _run_forge_with_retry()
            finally:
                lock.release()

        import threading
        threading.Thread(target=_run_forge_with_lock, daemon=True, name="forge-bg").start()
        return "🧬 正在自動生成新技能中，請稍候（約 1-5 分鐘）。完成後我會主動回報，若超時會自動重試。"

    def _laf_report_command_help(self) -> str:
        return _get_handler("laf").laf_report_command_help()

    def _detect_laf_report_action(self, text: str) -> tuple[str, str]:
        return _get_handler("laf").detect_laf_report_action(text)

    def _parse_laf_report_payload(self, raw_text: str):
        return _get_handler("laf").parse_laf_report_payload(raw_text)

    # ── Payment proof upload from channel images (LINE/DC/TG) ──────────

    def _handle_payment_proof_from_channel(self, image_path: str) -> str:
        """
        接收從 LINE/Discord/Telegram 傳來的繳費截圖，
        自動解析案號並上傳至 OLA。
        使用 subprocess 呼叫 action.py，避免重型 import 阻塞主進程。
        """
        action_script = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "skills",
            "file-review-orchestrator", "action.py",
        ))
        if not os.path.exists(action_script):
            return "❌ 找不到閱卷模組 action.py"

        py = os.environ.get("MAGI_SKILL_PYTHON", "").strip()
        if not py or not os.path.exists(py):
            py = sys.executable or "python3"

        # 呼叫 action.py 的 cmd_upload_payment_proof_from_image
        cmd_json = json.dumps({"cmd": "upload_payment_proof_from_image", "image_path": image_path})
        logger.info("💰 Calling action.py for payment proof: %s", image_path)

        try:
            proc = subprocess.run(
                [py, action_script, "--json-cmd"],
                input=cmd_json,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=os.path.dirname(action_script),
            )
        except subprocess.TimeoutExpired:
            return "❌ 繳費憑證上傳逾時（超過 3 分鐘）"

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        # 嘗試解析 JSON 結果
        try:
            result = json.loads(stdout)
            return result.get("message") or str(result)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6917, exc_info=True)

        # 如果不是 JSON，直接回傳 stdout
        if stdout:
            return stdout
        if proc.returncode != 0:
            err = stderr[:200] if stderr else f"exit code {proc.returncode}"
            return f"❌ 繳費憑證上傳失敗：{err}"
        return "⚠️ 繳費憑證上傳完成但無回傳結果"

    def _handle_command(self, user_id, message, role="user", platform="LINE"):
        """
        Routes commands to Melchior or System Skills.
        Uses CommandRegistry for extensible dispatch, falls back to legacy if-elif.
        """
        msg_lower = message.lower()

        # Try registry-based dispatch first
        ctx = CommandContext(
            user_id=user_id,
            message=message,
            msg_lower=msg_lower,
            role=role,
            platform=platform,
            orchestrator=self,
        )
        registry = getattr(self, "_cmd_registry", None) or _cmd_registry
        if registry is not None:
            registry_result = registry.dispatch(ctx)
            if registry_result is not None:
                return registry_result
        
        # Help Command — role-aware
        if msg_lower in ["/help", "help", "指令", "說明", "功能", "menu", "helps", "/start"]:
            if role == "admin":
                return (
"🤖 **MAGI (Casper) 功能總覽 (管理員)**\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📝 **文件產生**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/委任狀` 或 `幫我做委任狀` — 民事／刑事／行政委任狀\n"
"• `/契約書` 或 `幫我做委任契約書` — 委任契約書\n"
"• `/收據` 或 `幫我開收據` — 律師費收據\n"
"• `/存證信函` 或 `幫我寫存證信函` — 存證信函 PDF\n"
"• `審閱契約 [上傳檔案]` — 合約風險審查\n"
"• `證據能力 [案號]` — 卷證索引證據能力自動分類\n"
"• `截圖排序 [上傳截圖]` — 對話截圖自動排序\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"⚖️ **法扶作業**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `法扶回報指令` — 顯示回報指令集\n"
"• `幫我做[姓名]開辦回報` — 自然語言回報\n"
"• `正式送出開辦/結案` — 送出（需確認）\n"
"• `法扶監控` — 法扶案件狀態\n"
"• `自動報結掃描` / `二階段批次` — 報結作業\n"
"• `/閱卷查核 <法院> <案號>` — 查核卷宗狀態\n"
"• `/閱卷聲請 <法院> <案號>` — 聲請閱卷\n"
"• `/下載閱卷 [案號]` — 下載卷宗\n"
"• `/下載筆錄 <案號>` — 下載筆錄並歸檔\n"
"• `同步筆錄` / `重命名筆錄` — 筆錄管理\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"🖼️ **視覺 & 搜尋**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/draw [描述]` — 生成圖片\n"
"• 上傳圖片 — 自動分析內容\n"
"• `/搜尋 [關鍵字]` — 聯網搜尋\n"
"• `/抓取 [網址]` — 讀取網頁\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"⚖️ **法扶作業 / 法律工具**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/查判決 [關鍵字]` — 搜尋判決\n"
"• `/法規搜尋 [查詢]` — 查詢法規\n"
"• `/加班費` — 勞基法試算\n"
"• `/庭期` — 開庭排程與提醒\n"
"• `/判決趨勢 [案由]` — 判決趨勢分析\n"
"• `/司法工具` — 規費/折舊/刑度試算\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📅 **助理 & 記憶**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/行程` — 查詢本週會議\n"
"• `/狀態` — MAGI 節點狀態\n"
"• `/翻譯 [文字/網址]` — 本地翻譯\n"
"• `/摘要 [文字/網址]` — 文件摘要（精簡/普通/詳細三級）\n"
"  ↳ `精簡摘要` 3-5點 ∣ `摘要` 5-8點 ∣ `詳細摘要` 12-15點\n"
"• 上傳音檔 — 自動產生逐字稿\n"
"• `去AI味 [文字]` — 去除 AI 痕跡\n"
"• `/記住 [內容]` — 存入長期記憶\n"
"• `/忘記 [內容]` — 刪除記憶\n"
"• `/深度思考 [問題]` — 深度分析模式\n"
"• `/obsidian [指令]` — 筆記管理\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📊 **追蹤 & 監控**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/股市晨報` — 股票追蹤與分析\n"
"• `/爬蟲 [指令]` — 爬蟲目標管理\n"
"• `/RSS` — 新聞訂閱\n"
"• `掃描案件待辦` — 案件待辦管理\n"
"• `單檔命名` / `批次命名` — PDF 自動命名\n"
"• `[姓名]已繳費` — 標記繳費完成\n"
"• `日曆同步` — 庭期同步 Google Calendar\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"🧬 **技能進化 (管理員)**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• 自動生成／驗證／上線新技能\n"
"• `技能CI [skill]` — 安全檢查\n"
"• `技能事件` — 執行統計\n"
"• `內化CODE` — 自動技能化\n"
"• `自動巡檢` — 修復＋內化循環\n"
"• `核心變更待審` — 核心改動審批\n"
"\n"
"💡 在一般頻道用 `/指令` 確保觸發，或在專屬頻道直接用自然語言\n"
"💡 可透過 Telegram / Discord / LINE / 網頁入口使用"
)
            else:
                return (
"🤖 **MAGI 功能總覽**\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"⚖️ **法扶作業**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `法扶回報指令` — 顯示回報指令集\n"
"• `幫我做[姓名]開辦回報` — 自然語言回報\n"
"• `正式送出開辦/結案` — 送出（需確認）\n"
"• `法扶監控` — 法扶案件狀態\n"
"• `自動報結掃描` / `二階段批次` — 報結作業\n"
"• `/閱卷查核 <法院> <案號>` — 查核卷宗狀態\n"
"• `/閱卷聲請 <法院> <案號>` — 聲請閱卷\n"
"• `/下載閱卷 [案號]` — 下載卷宗\n"
"• `/下載筆錄 <案號>` — 下載筆錄並歸檔\n"
"• `同步筆錄` / `重命名筆錄` — 筆錄管理\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📝 **文件產生 / 處理**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/翻譯 [文字/網址]` — 翻譯文件或網頁\n"
"• `/摘要 [文字/網址]` — 產生文件摘要（精簡/普通/詳細三級）\n"
"  ↳ `精簡摘要` 3-5點 ∣ `摘要` 5-8點 ∣ `詳細摘要` 12-15點\n"
"• 上傳音檔 — 自動產生逐字稿\n"
"• `去AI味 [文字]` — 去除 AI 痕跡\n"
"• `/委任狀` — 製作委任狀\n"
"• `/契約書` — 製作委任契約書\n"
"• `/收據` — 開收據\n"
"• `/存證信函` — 草擬存證信函\n"
"• `審閱契約` — 合約風險審查（上傳檔案）\n"
"• `證據能力 [案號]` — 卷證索引證據能力分類\n"
"• `截圖排序` — 對話截圖自動排序\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"⚖️ **法律工具**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/查判決 [關鍵字]` — 搜尋判決\n"
"• `/判決趨勢 [案由]` — 判決趨勢分析\n"
"• `/法規搜尋 [查詢]` — 查詢法規\n"
"• `/加班費` — 勞基法試算\n"
"• `/庭期` — 開庭排程與提醒\n"
"• `/司法工具` — 規費/折舊/刑度試算\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"🖼️ **視覺 & 搜尋**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/draw [描述]` — 生成圖片\n"
"• 上傳圖片 — 自動分析內容\n"
"• `/搜尋 [關鍵字]` — 聯網搜尋\n"
"• `/抓取 [網址]` — 讀取網頁\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📊 **案件 & PDF**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `掃描案件待辦` / `待辦佇列狀態` — 案件待辦管理\n"
"• `日曆同步` — 庭期同步 Google Calendar\n"
"• `單檔命名 [路徑]` / `批次命名` — PDF 自動命名\n"
"• `[姓名]已繳費` — 標記繳費完成\n"
"\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"📅 **助理 & 記憶**\n"
"━━━━━━━━━━━━━━━━━━━━\n"
"• `/行程` — 查詢本週會議\n"
"• `/狀態` — 系統狀態\n"
"• `/股市晨報` — 股票追蹤與分析\n"
"• `/爬蟲 [指令]` — 爬蟲目標管理\n"
"• `/記住 [內容]` — 存入長期記憶\n"
"• `/深度思考 [問題]` — 深度分析模式\n"
"• `備份資料庫` / `備份清單` — 資料庫備份\n"
"• 直接對話 — 一般問答\n"
"\n"
"💡 用 `/指令` 確保觸發功能，或在專屬頻道直接用自然語言\n"
"🔒 大腦管理、鐵穹、技能進化等需管理員權限"
)
        
        # Image Generation (Enhanced Natural Language)
        # Matches: "/draw xxx", "draw a cat", "幫我畫一隻貓", "請畫圖", "生成圖片: sunset"
        import re
        draw_pattern = re.compile(r"(?:/draw\b|畫|draw|generate image|產生圖片|绘|画圖|畫一|画一)", re.IGNORECASE)

        if draw_pattern.search(msg_lower) and len(message) > 2:
            # Extract prompt by removing common command words
            prompt = message
            for kw in ["/draw", "幫我", "請", "畫圖", "一張", "一個", "draw", "generate image", "產生圖片", "畫", "画", "a picture of", "an image of"]:
                prompt = re.sub(re.escape(kw), "", prompt, flags=re.IGNORECASE).strip()

            # If prompt became empty but message was long enough, use original message minus strict command
            if len(prompt) < 2:
                 return "🎨 請描述您想要的圖片內容。例如：'畫一隻可愛的貓咪'"

            return self._generate_image(prompt, user_id)

        # Web Research (Below help command content)
        help_extra = """
**🌐 網路研究 (Web Research)**
- `搜尋 [主題]` : 強制聯網搜尋 (e.g., "搜尋 2025 AI 趨勢")
- `抓取 [網址]` : 讀取特定網頁內容 (e.g., "抓取 https://example.com")
- *聊天時自動搜尋* : 若問題涉及新資訊，我會自動上網查。

**🧬 技能進化 (Skill Genesis)**
- `學會 [能力]` : 請求 Melchior 撰寫新工具 (e.g., "學會畫圖", "製作幣安查價技能")
- *Iron Dome 保護中* : 所有生成程式碼皆經過安全掃描。

**🔧 其他工具**
- `court` : 查詢法院庭期 (Paperclip)
- `laf` : 法扶信件監控 (Laf Monitor)
"""
        
        # MAGI Status Command - Real status from heartbeat
        if any(kw in msg_lower for kw in ["狀態", "status", "運作狀態", "節點狀態", "機器狀態"]) or (
            ("模型" in message) and any(kw in msg_lower for kw in ["目前", "現在", "使用", "模式", "為何", "是什麼"])
        ):
            node_status = self._get_magi_status()
            brain_status = get_brain_status()
            collab_status = self._get_collaboration_status()
            rt = get_melchior_runtime_status()
            model_line = "（目前抓不到模型資訊）"
            models = rt.get("models") if isinstance(rt.get("models"), list) else []
            if models:
                model_line = f"目前主要模型：`{models[0]}`"
            gpu_line = ""
            if rt.get("gpu_used_mb") is not None and rt.get("gpu_total_mb") is not None:
                gpu_line = f"\nMelchior GPU：{float(rt['gpu_used_mb'])/1024.0:.2f}/{float(rt['gpu_total_mb'])/1024.0:.2f} GB"
            return f"{node_status}\n\n{brain_status}\n\n{collab_status}\n\n🧩 模型資訊：{model_line}{gpu_line}"

        # Code Auto-Fix Command
        if any(kw in msg_lower for kw in ["自動修復code", "修復code資料夾", "autofix code", "auto fix code", "修復程式碼"]):
            try:
                from skills.management.code_autofix import autofix_codebase
                target = "magi" if "magi" in msg_lower else "code"
                dry_run = any(k in msg_lower for k in ["dry run", "preview", "只分析", "僅檢查"])
                include_tests = any(k in msg_lower for k in ["含測試", "include tests", "含 tests"])
                internalize = any(k in msg_lower for k in ["內化", "internalize", "技能化"])
                result = autofix_codebase(
                    target=target,
                    max_files=80,
                    max_rounds=2,
                    dry_run=dry_run,
                    include_tests=include_tests,
                    task_hint=message,
                    internalize_skill=internalize,
                    internalize_name="casper-autofix-knowledge",
                )
                if not result.get("success") and result.get("error"):
                    return f"❌ 自動修復啟動失敗: {result.get('error')}"
                lines = [
                    f"🛠️ **Code Auto-Fix 完成** (`{result.get('target', target)}`)",
                    f"- 掃描檔案: {result.get('scanned_files', 0)}",
                    f"- 發現語法問題: {result.get('syntax_issue_files', 0)}",
                    f"- 修復成功: {result.get('fixed_files', 0)}",
                    f"- 修復失敗: {result.get('failed_files', 0)}",
                ]
                verify_errors = result.get("verify", {}).get("errors", [])
                if verify_errors:
                    lines.append(f"⚠️ 驗證錯誤數: {len(verify_errors)}")
                if result.get("internalized", {}).get("success"):
                    lines.append(f"🧬 已內化技能: `{result['internalized'].get('skill_folder')}`")
                return "\n".join(lines)
            except Exception as e:
                return f"❌ 自動修復流程失敗: {e}"

        if any(kw in msg_lower for kw in ["內化code", "code技能化", "內化 code", "skillize code", "code internalize"]):
            try:
                from skills.management.auto_skill import AutoSkill

                autoskill = AutoSkill()
                source_dir = str(get_magi_root_dir())
                if "legacy" in msg_lower or "archive" in msg_lower:
                    source_dir = str(get_legacy_code_root())
                force = any(k in msg_lower for k in ["force", "重建", "重新內化"])
                result = autoskill.internalize_codebase_as_skills(
                    source_dir=source_dir,
                    max_files=60,
                    force=force,
                    auto_activate=True,
                    enable_release=True,
                    canary_percent=20,
                    promote_min_runs=12,
                    promote_max_failure_rate=0.2,
                )
                if result.get("success"):
                    canary_started = 0
                    stable_set = 0
                    for item in result.get("items", []):
                        rel = item.get("release", {}) or {}
                        if isinstance(rel.get("canary"), dict) and rel.get("canary", {}).get("success"):
                            canary_started += 1
                        if isinstance(rel.get("stable"), dict) and rel.get("stable", {}).get("success"):
                            stable_set += 1
                    return (
                        "🧬 CODE 內化完成\n"
                        f"- Source: `{result.get('source_dir')}`\n"
                        f"- 掃描: {result.get('scanned_files', 0)}\n"
                        f"- 技能新增/更新: {result.get('created_skills', 0)}\n"
                        f"- 略過: {result.get('skipped_files', 0)}\n"
                        f"- Canary 啟動: {canary_started}\n"
                        f"- Stable 設定: {stable_set}"
                    )
                return f"❌ CODE 內化失敗: {result.get('message', result.get('error', 'unknown'))}"
            except Exception as e:
                return f"❌ CODE 內化流程失敗: {e}"

        if any(kw in msg_lower for kw in ["導入auto-skill", "import auto-skill", "toolsai auto-skill"]):
            try:
                from skills.management.auto_skill import AutoSkill

                autoskill = AutoSkill()
                result = autoskill.import_toolsai_auto_skill(notify_dc=True)
                if result.get("success"):
                    dc = result.get("dc_notify", {}) if isinstance(result.get("dc_notify"), dict) else {}
                    return (
                        "📥 Toolsai auto-skill 導入完成\n"
                        f"- 新增知識: {result.get('learned', 0)}\n"
                        f"- 檔案數: {len(result.get('imported_files', []))}\n"
                        f"- DC通知: line={dc.get('line')} discord={dc.get('discord')}"
                    )
                return f"❌ 導入失敗: {result.get('message', result.get('error', 'unknown'))}"
            except Exception as e:
                return f"❌ 導入 auto-skill 流程失敗: {e}"

        if any(kw in msg_lower for kw in ["code cycle", "自動巡檢", "工作流程自動化", "流程自動化"]):
            try:
                from scripts.code_skill_cycle import run_cycle

                result = run_cycle()
                if not result.get("success"):
                    return "❌ 自動巡檢流程失敗。"
                af = result.get("autofix", {})
                ci = result.get("code_internalization", {})
                return (
                    "⚙️ 自動巡檢完成\n"
                    f"- AutoFix: fixed={af.get('fixed_files',0)} failed={af.get('failed_files',0)}\n"
                    f"- Code->Skill: created={ci.get('created_skills',0)} skipped={ci.get('skipped_files',0)}"
                )
            except Exception as e:
                return f"❌ 自動巡檢執行失敗: {e}"

        if "重試摘要佇列自動" in message or "retry_summary_queue_auto" in msg_lower:
            try:
                import json as _json
                import subprocess as _subprocess
                py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
                if not py or not os.path.exists(py):
                    py = sys.executable or "python3"
                jc = f"{_MAGI_ROOT}/skills/judgment-collector/action.py"
                cp = _subprocess.run(
                    [py, jc, "--task", "retry_summary_queue_auto {\"notify\": false}"],
                    capture_output=True,
                    text=True,
                    timeout=420,
                )
                out = (cp.stdout or "").strip()
                if cp.returncode != 0:
                    return f"❌ 摘要補跑失敗（exit={cp.returncode}）: {(cp.stderr or out)[:220]}"
                data = {}
                try:
                    data = _json.loads(out or "{}")
                except Exception:
                    data = {}
                return (
                    "📚 摘要補跑完成\n"
                    f"- 處理: {data.get('processed', 0)}\n"
                    f"- 改善: {data.get('improved', 0)}\n"
                    f"- 剩餘: {data.get('remaining', 0)}\n"
                    f"- 模式: {data.get('mode', 'tiered')}"
                )
            except Exception as e:
                return f"❌ 摘要補跑流程失敗: {e}"

        if any(k in message for k in ["查判決", "找判決", "判決搜尋", "搜尋判決", "收集判決", "判決搜集", "搜尋最高法院判決"]):
            if self._looks_like_capability_question(message):
                return (
                    "✅ **我可以幫您查判決！**\n\n"
                    "• 直接輸入：`查判決 傷害`\n"
                    "• 也可提供案號：`查判決 113年度上訴字第12號`"
                )
            return self._run_judgment_collector_command(message, notify=False)

        if message.startswith("翻譯 ") or message.lower().startswith("translate "):
            return self._run_inline_translation_command(user_id, message)

        if message.startswith("製作音樂 ") or message.startswith("生成音樂 ") or message.lower().startswith("make music "):
            try:
                from skills.bridge.tri_sage_collab import generate_music

                prompt = (
                    message.replace("製作音樂 ", "", 1)
                    .replace("生成音樂 ", "", 1)
                    .replace("make music ", "", 1)
                    .strip()
                )
                if not prompt:
                    return "❓ 請提供音樂描述。"
                result = generate_music(prompt, duration_sec=30)
                if result.get("success"):
                    return f"🎵 音樂已產生：`{result.get('path','')}`（{result.get('provider','tri-sage')}）"
                return f"❌ 音樂生成失敗: {result.get('error')}"
            except Exception as e:
                return f"❌ 音樂生成流程失敗: {e}"

        # Teach / Internalize Commands
        if any(message.startswith(prefix) for prefix in ["教學檔案", "@MAGI 教學檔案", "teach file", "@MAGI teach file"]):
            try:
                from skills.management.auto_skill import AutoSkill
                autoskill = AutoSkill()
                tip_file = (
                    message.replace("@MAGI 教學檔案", "")
                    .replace("@MAGI teach file", "")
                    .replace("教學檔案", "")
                    .replace("teach file", "")
                    .strip()
                )
                if not tip_file:
                    return "❓ 請提供教學檔案路徑。"
                result = autoskill.learn_from_file(tip_file)
                return result.get("message", "📘 教學檔案已處理。")
            except Exception as e:
                return f"❌ 教學檔案處理失敗: {e}"

        if any(message.startswith(prefix) for prefix in ["教學 ", "@MAGI 教學", "teach ", "@MAGI teach"]):
            try:
                from skills.management.auto_skill import AutoSkill
                autoskill = AutoSkill()
                lesson = (
                    message.replace("@MAGI 教學", "")
                    .replace("@MAGI teach", "")
                    .replace("教學 ", "")
                    .replace("teach ", "")
                    .strip()
                )
                if not lesson:
                    return "❓ 請提供教學內容。"
                result = autoskill.teach(lesson, context="user-teach", source=f"{role}:{user_id}")
                return result.get("message", "🧠 教學完成。")
            except Exception as e:
                return f"❌ 教學失敗: {e}"

        if any(message.startswith(prefix) for prefix in ["內化技能", "@MAGI 內化技能", "internalize skill", "@MAGI internalize skill"]):
            try:
                from skills.management.auto_skill import AutoSkill
                autoskill = AutoSkill()
                name = (
                    message.replace("@MAGI 內化技能", "")
                    .replace("@MAGI internalize skill", "")
                    .replace("內化技能", "")
                    .replace("internalize skill", "")
                    .strip()
                )
                result = autoskill.internalize_as_skill(
                    skill_name=name or "casper-learned-skill",
                    description="Internalized user-taught CASPER knowledge.",
                    auto_activate=True,
                )
                if result.get("success"):
                    return f"{result.get('message')}\n路徑: `{result.get('skill_path')}`"
                return f"❌ 內化技能失敗: {result.get('message')}"
            except Exception as e:
                return f"❌ 內化技能失敗: {e}"
        
        # Web Research Commands — only trigger on explicit search intent
        _web_search_explicit = re.search(
            r"^(?:搜尋|search|research|/search|查一下|找一下|搜一下|google|幫我搜|幫我查一下|執行網路研究|進行網路研究|網路研究|網路搜尋|幫我查詢|請幫我查詢)\s*[:：]?\s*",
            msg_lower,
        )
        if _web_search_explicit:
            # Extract the topic (remove command keywords)
            topic = message
            for kw in ["research", "搜尋", "search", "/search", "查一下", "找一下", "搜一下",
                        "google", "幫我搜", "幫我查一下", "執行網路研究", "進行網路研究",
                        "網路研究", "網路搜尋", "幫我查詢", "請幫我查詢", "@MAGI", "@magi"]:
                topic = re.sub(re.escape(kw), "", topic, flags=re.IGNORECASE).strip()
            # Strip colon separators
            topic = re.sub(r"^[:：]\s*", "", topic).strip()
            # Also strip filler words
            topic = re.sub(r"^(?:請|幫我|能不能|可以|一下|幫忙)\s*", "", topic).strip()

            if len(topic) < 2:
                return "🔍 請告訴我要搜尋什麼主題。例如：'搜尋 AI agent 2024'"

            logger.info(f"🌐 Web Research requested: {topic}")
            result = research_topic(topic, depth=3)

            if result.get("sources"):
                return self._summarize_web_results(topic, result)
            else:
                return f"🔍 找不到關於「{topic}」的資訊。"
        
        # URL Fetch Command
        if any(kw in msg_lower for kw in ["fetch", "抓取", "讀取網頁"]):
            import re
            urls = re.findall(r'https?://[^\s]+', message)
            if urls:
                result = fetch_url_content(urls[0])
                if result["success"]:
                    return f"📄 **{result['title']}**\n\n{result.get('content', '')[:2000]}..."
                else:
                    return f"❌ 無法抓取網頁: {result['error']}"
            return "🔗 請提供要抓取的網址。"
        
        # Memory Command (Remember)
        if any(kw in msg_lower for kw in ["remember", "記住", "save memory", "memorize"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以寫入記憶（系統改動指令）。"
            content = message
            for kw in ["remember", "記住", "save memory", "memorize", "請記住", "幫我記住"]:
                content = content.replace(kw, "").strip()
            
            if len(content) < 2:
                return "🧠 請告訴我要記住什麼？例如：'記住我的車牌是 ABC-1234'"
                
            from skills.memory.mem_bridge import remember
            remember(
                content,
                source=f"user_chat_{user_id}",
                metadata={
                    "verified": True,
                    "confidence": 0.94,
                    "source_type": "user_confirmed",
                    "role": "user",
                },
            )
            return f"🧠 **已存入記憶庫**\n內容: {content}"

        # Memory Command (Forget)
        if any(kw in msg_lower for kw in ["forget", "刪除記憶", "忘記", "delete memory"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以刪除記憶（系統改動指令）。"
            content = message
            for kw in ["forget", "刪除記憶", "忘記", "delete memory", "把這段記憶刪掉", "請把這段記憶刪掉", "這是錯的"]:
                content = content.replace(kw, "").strip()
            
            if len(content) < 2:
                # User might just say "delete this", imply context?
                # For now require content.
                return "🧠 請告訴我要刪除哪段記憶？例如：'刪除關於夏油傑的記憶'"
            
            from skills.memory.mem_bridge import forget
            success, result_msg = forget(content)
            
            if success:
                return f"🗑️ **已刪除記憶**\n{result_msg}"
            else:
                return f"⚠️ **刪除失敗**: {result_msg}"

        # Image Generation Command
        # Triggered by: "/draw", "畫", "draw", "產生圖片", "generate image"
        if any(kw in msg_lower for kw in ["/draw", "畫", "draw", "產生圖片", "generate image", "畫圖", "畫一"]):
            # Extract the prompt
            prompt = message
            for kw in ["/draw", "畫", "draw", "產生圖片", "generate image", "幫我", "請", "畫圖", "一張", "一個"]:
                prompt = prompt.replace(kw, "").strip()
            
            if len(prompt) < 2:
                return "🎨 請描述您想要的圖片內容。例如：'畫一隻可愛的貓咪'"
            
            logger.info(f"🎨 Image Generation requested: {prompt}")
            
            from skills.bridge.melchior_bridge import generate_image
            result = generate_image(prompt)
            
            if result.get("success"):
                return f"🎨 **圖片生成成功！ (By 3rd-Child Melchior)**\n提示詞: {prompt}\n{result.get('message', '工程部門 (Melchior) 已完成繪圖。')}"
            else:
                return f"❌ **Melchior 回報錯誤**: {result.get('error', 'Unknown error')}"

        # Brain Switching Commands
        # Triggered by: "switch to", "big brain", "local mode", "切換", "本地"
        # Note: distributed mode disabled — all inference is local-first
        if any(kw in msg_lower for kw in ["switch to", "big brain", "distributed", "分散式", "最強模式", "activate big brain"]):
             if role != "admin":
                 return "⛔ 抱歉，只有管理員可以切換推理模式（系統改動指令）。"
             return "ℹ️ 目前使用本地 oMLX 推理（TAIDE-12b 摘要/通用/視覺辨識）。"
        
        if any(kw in msg_lower for kw in ["local mode", "go local", "independent", "本地模式", "切回本地", "release engineer"]):
             if role != "admin":
                 return "⛔ 抱歉，只有管理員可以切換推理模式（系統改動指令）。"
             return switch_brain_mode("local")

        # Big Brain Repair
        if any(kw in msg_lower for kw in ["修理大腦", "修復大腦", "修理melchior", "修復melchior", "repair big brain", "repair melchior"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以修復推理叢集（系統改動指令）。"
            try:
                timeout = 300
                m = re.search(r"(\d{2,4})\s*(?:秒|sec|s)", msg_lower)
                if m:
                    timeout = max(60, min(int(m.group(1)), 900))
                repaired = repair_big_brain(timeout_sec=timeout, force_cycle=True)
                ok = bool(repaired.get("success"))
                mode_after = str(repaired.get("mode_after") or "unknown")
                remote_h = repaired.get("remote_health") if isinstance(repaired.get("remote_health"), dict) else {}
                remote_msg = str(remote_h.get("message") or "")
                if ok:
                    return f"✅ 大腦模式修復完成\n- 目前模式：`{mode_after}`\n- 遠端健康：{remote_msg or 'OK'}"
                return f"⚠️ 修復已執行，但遠端仍未恢復\n- 目前模式：`{mode_after}`\n- 診斷：{remote_msg or 'unknown'}"
            except Exception as e:
                return f"❌ 修復大腦模式失敗：{e}"

        # NGL auto-calibration
        if any(kw in msg_lower for kw in ["校準ngl", "自動校準ngl", "ngl calibrate", "校準大腦"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以校準 NGL（系統改動指令）。"
            try:
                target = 8.0
                tol = 0.5
                m_target = re.search(r"(\d+(?:\.\d+)?)\s*gb", msg_lower)
                if m_target:
                    target = max(2.0, min(float(m_target.group(1)), 24.0))
                m_tol = re.search(r"[±\+\-]\s*(\d+(?:\.\d+)?)\s*gb", msg_lower)
                if m_tol:
                    tol = max(0.1, min(float(m_tol.group(1)), 4.0))
                cal = calibrate_distributed_ngl(target_gb=target, tolerance_gb=tol, max_rounds=4, min_ngl=8, max_ngl=80)
                rec = cal.get("recommended_ngl")
                note = str(cal.get("note") or "")
                best = cal.get("best_delta_gb")
                if cal.get("success"):
                    return f"✅ NGL 校準完成\n- 目標：{target:.2f}GB ± {tol:.2f}GB\n- 建議 NGL：`{rec}`\n- 結果：達標"
                return (
                    f"⚠️ NGL 校準已完成（最佳努力）\n"
                    f"- 目標：{target:.2f}GB ± {tol:.2f}GB\n"
                    f"- 建議 NGL：`{rec}`\n"
                    f"- 最佳偏差：{best if best is not None else 'unknown'} GB\n"
                    f"- 說明：{note or 'not_reached'}"
                )
            except Exception as e:
                return f"❌ NGL 校準失敗：{e}"

        # Night Talk Trigger (夜議模式)
        # Disconnects Melchior to allow daily tasks or independent processing
        if any(kw in msg_lower for kw in ["夜議", "night talk", "night meeting", "yiyi", "意議", "開始夜議", "start night talk"]):
             if role != "admin":
                 return "⛔ 抱歉，只有管理員可以啟動夜議（系統改動指令）。"
             logger.info("🌙 Night Talk Initiated...")
             
             # Run in background via thread to avoid blocking
             def run_night_talk(uid):
                 try:
                     from skills.magi.night_talk import start_night_talk
                     result = start_night_talk()
                     
                     # Notify User
                     if hasattr(self, 'notification_callback') and self.notification_callback:
                         self.notification_callback(uid, f"🌙 **夜議 (Yi Yi) 會議記錄**\n\n{result[:1500]}...\n(完整記錄已封存)", "Discord")
                 except Exception as e:
                     logger.error(f"Night Talk Error: {e}")
                     if hasattr(self, 'notification_callback'):
                         self.notification_callback(uid, "❌ 夜議執行失敗，請查看日誌。", "Discord")

             self._bg_task_pool.submit(run_night_talk, user_id)
             
             return "🌙 **夜議模式已啟動**\n正在切換至獨立模式 (Local Mode)...\nCasper 與 Melchior 即將開始審視今日錯誤 (請稍候)..."

        # Skill Genesis (Self-Evolution)
        # Triggered by: "learn to...", "build a skill...", "學會..."
        if any(kw in msg_lower for kw in ["learn to", "build skill", "create skill", "學會", "學習", "製作技能", "幫我寫一個", "build a skill to", "寫工具"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以觸發技能進化（系統改動指令）。"
            # Extract topic
            topic = message
            for kw in ["learn to", "build skill", "create skill", "學會", "學習", "製作技能", "幫我寫一個", "build a skill to", "可以幫我寫一個", "寫工具"]:
                topic = topic.replace(kw, "").strip()
            
            if len(topic) < 2:
                return "🔧 請告訴我您想讓我學會有什麼功能？例如：'學會畫圖' 或 '製作一個幣安查價技能'"
            
            logger.info(f"🧬 Skill Genesis Triggered: {topic}")
            return self._start_skill_interview(
                str(user_id or ""),
                str(platform or ""),
                role,
                topic,
                trigger_reason="manual",
            )

        # Skill Version Listing / Rollback
        if any(kw in msg_lower for kw in ["技能版本", "skill versions", "list versions"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以查詢技能版本（系統改動指令）。"
            try:
                from skills.evolution.skill_genesis import list_skill_versions
                skill_name = (
                    message.replace("技能版本", "")
                    .replace("skill versions", "")
                    .replace("list versions", "")
                    .strip()
                )
                if not skill_name:
                    return "🗂️ 請提供技能資料夾名稱，例如：`技能版本 generated-my-skill`"
                result = list_skill_versions(skill_name)
                if not result.get("success"):
                    return f"❌ 讀取版本失敗: {result.get('error')}"
                versions = result.get("versions", [])[:8]
                if not versions:
                    return "ℹ️ 此技能目前沒有可用版本快照。"
                lines = [f"🗂️ **{skill_name} 版本快照**"]
                for v in versions:
                    lines.append(f"- {v.get('version_id')} ({v.get('reason', 'snapshot')})")
                return "\n".join(lines)
            except Exception as e:
                return f"❌ 技能版本查詢失敗: {e}"

        if any(kw in msg_lower for kw in ["回滾技能", "rollback skill"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以回滾技能（系統改動指令）。"
            try:
                from skills.evolution.skill_genesis import rollback_skill_version
                text = message.replace("回滾技能", "").replace("rollback skill", "").strip()
                parts = text.split()
                if not parts:
                    return "♻️ 請提供技能名稱，例如：`回滾技能 generated-my-skill`"
                skill_name = parts[0]
                version_id = parts[1] if len(parts) > 1 else ""
                result = rollback_skill_version(skill_name, version_id=version_id)
                if result.get("success"):
                    return (
                        f"♻️ 已回滾 `{skill_name}` 到版本 `{result.get('restored_version')}`。\n"
                        f"檔案: {', '.join(result.get('restored_files', []))}"
                    )
                return f"❌ 回滾失敗: {result.get('error')}"
            except Exception as e:
                return f"❌ 回滾執行失敗: {e}"

        if any(kw in msg_lower for kw in ["技能ci", "skill ci", "技能健康檢查"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以執行技能 CI（系統改動指令）。"
            try:
                from skills.evolution.skill_genesis import run_skill_ci
                text = (
                    message.replace("技能CI", "")
                    .replace("技能ci", "")
                    .replace("skill ci", "")
                    .replace("技能健康檢查", "")
                    .strip()
                )
                if not text:
                    return "🧪 請提供技能名稱，例如：`技能CI generated-my-skill`"
                result = run_skill_ci(text, task="health check", attempt_repair=True)
                if result.get("success"):
                    return f"✅ 技能 CI 通過：`{text}`"
                checks = result.get("checks", [])
                failed = [c for c in checks if not c.get("ok")]
                detail = failed[0].get("detail", "unknown") if failed else result.get("error", "unknown")
                return f"❌ 技能 CI 未通過：`{text}`\n原因: {detail}"
            except Exception as e:
                return f"❌ 技能 CI 執行失敗: {e}"

        if any(kw in msg_lower for kw in ["技能事件", "skill events", "技能健康總覽"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以查詢技能事件（系統改動指令）。"
            try:
                from skills.evolution.skill_genesis import get_skill_runtime_stats
                stats = get_skill_runtime_stats(limit=200)
                if not stats.get("success", True):
                    return f"❌ 讀取技能事件失敗: {stats.get('error')}"
                total = stats.get("total", 0)
                by_event = stats.get("by_event", {})
                by_status = stats.get("by_status", {})
                return (
                    "📊 **技能執行健康總覽**\n"
                    f"- 事件總數: {total}\n"
                    f"- 事件分布: {by_event}\n"
                    f"- 狀態分布: {by_status}"
                )
            except Exception as e:
                return f"❌ 技能事件查詢失敗: {e}"

        if any(kw in msg_lower for kw in ["標記穩定版", "set stable"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以標記穩定版（系統改動指令）。"
            try:
                from skills.evolution.skill_genesis import set_stable_skill_version
                text = message.replace("標記穩定版", "").replace("set stable", "").strip()
                parts = text.split()
                if not parts:
                    return "🏷️ 請提供技能名稱，例如：`標記穩定版 generated-my-skill`"
                skill_name = parts[0]
                version_id = parts[1] if len(parts) > 1 else ""
                result = set_stable_skill_version(skill_name, version_id=version_id, enforce=True)
                if result.get("success"):
                    return f"🏷️ 已標記穩定版：`{skill_name}` -> `{result.get('stable_version')}`"
                return f"❌ 標記穩定版失敗: {result.get('error')}"
            except Exception as e:
                return f"❌ 標記穩定版執行失敗: {e}"

        if any(kw in msg_lower for kw in ["開始canary", "start canary"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以啟動 canary（系統改動指令）。"
            try:
                from skills.evolution.skill_genesis import start_canary_release
                text = message.replace("開始canary", "").replace("start canary", "").strip()
                parts = text.split()
                if len(parts) < 2:
                    return "🧪 請提供技能與版本，例如：`開始canary generated-my-skill 20260213010101000000 20 12 0.15`"
                skill_name = parts[0]
                version_id = parts[1]
                canary_percent = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 10
                promote_min_runs = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
                try:
                    promote_max_failure_rate = float(parts[4]) if len(parts) > 4 else None
                except Exception:
                    promote_max_failure_rate = None
                result = start_canary_release(
                    skill_name,
                    version_id,
                    canary_percent=canary_percent,
                    min_runs=10,
                    fail_threshold=3,
                    max_failure_rate=0.5,
                    auto_promote=True,
                    promote_min_runs=promote_min_runs,
                    promote_max_failure_rate=promote_max_failure_rate,
                )
                if result.get("success"):
                    st = result.get("state", {})
                    return (
                        f"🧪 Canary 已啟動：`{skill_name}` 版本 `{version_id}`，流量 {canary_percent}%\n"
                        f"Auto-Promote: runs>={st.get('promote_min_runs')} 且 failure_rate<={st.get('promote_max_failure_rate')}"
                    )
                return f"❌ Canary 啟動失敗: {result.get('error')}"
            except Exception as e:
                return f"❌ Canary 啟動錯誤: {e}"

        if any(kw in msg_lower for kw in ["停止canary", "stop canary"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以停止 canary（系統改動指令）。"
            try:
                from skills.evolution.skill_genesis import stop_canary_release
                text = message.replace("停止canary", "").replace("stop canary", "").strip()
                if not text:
                    return "🧪 請提供技能名稱，例如：`停止canary generated-my-skill`"
                result = stop_canary_release(text, reason="manual_stop")
                if result.get("success"):
                    return f"🛑 Canary 已停止：`{text}`"
                return f"❌ 停止 Canary 失敗: {result.get('error')}"
            except Exception as e:
                return f"❌ 停止 Canary 錯誤: {e}"

        if any(kw in msg_lower for kw in ["同步技能到melchior", "sync skills to melchior", "melchior skills sync"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以同步技能到 Melchior（系統改動指令）。"
            try:
                from skills.bridge.melchior_manager import sync_skills_to_melchior

                text = (
                    message.replace("同步技能到melchior", "")
                    .replace("sync skills to melchior", "")
                    .replace("melchior skills sync", "")
                    .strip()
                )
                tokens = [t for t in text.split() if t]
                mode = ""
                force = False
                smoke = True
                for t in tokens:
                    low = t.lower()
                    if low in {"auto", "delta", "full"}:
                        mode = low
                    elif low in {"force", "強制"}:
                        force = True
                    elif low in {"nosmoke", "no-smoke"}:
                        smoke = False

                result = sync_skills_to_melchior(f"{_MAGI_ROOT}/skills", mode=mode, force=force, smoke_test=smoke)
                if result.get("success"):
                    action = result.get("action", "ok")
                    if action.startswith("skipped"):
                        return f"📦 Melchior 同步略過：{action}"
                    ms = ""
                    smoke_res = result.get("smoke") or {}
                    if isinstance(smoke_res, dict) and smoke_res.get("checks"):
                        ok = smoke_res.get("ok")
                        ms = f"；smoke={'ok' if ok else 'fail'}"
                    return f"📦 已同步技能到 Melchior（mode={result.get('mode','')}, files={result.get('zip_files',0)}){ms}"
                return f"❌ 同步到 Melchior 失敗: {result.get('error', 'unknown')}"
            except Exception as e:
                return f"❌ 同步到 Melchior 發生錯誤: {e}"

        if any(kw in msg_lower for kw in ["melchior狀態", "melchior status"]):
            try:
                from skills.bridge.melchior_manager import melchior_health

                h = melchior_health()
                if h.get("online"):
                    models = h.get("models") or []
                    return f"🟢 Melchior online ({h.get('mode','remote')}) models={models[:5]}"
                return f"🔴 Melchior offline: {h.get('error') or h.get('mode')}"
            except Exception as e:
                return f"❌ Melchior 狀態查詢失敗: {e}"

        if any(kw in msg_lower for kw in ["發布狀態", "release status"]):
            try:
                from skills.evolution.skill_genesis import get_skill_release_state
                text = message.replace("發布狀態", "").replace("release status", "").strip()
                if not text:
                    return "📦 請提供技能名稱，例如：`發布狀態 generated-my-skill`"
                result = get_skill_release_state(text)
                if not result.get("success"):
                    return f"❌ 讀取發布狀態失敗: {result.get('error')}"
                state = result.get("state", {})
                stats = state.get("stats", {})
                return (
                    f"📦 **{text} 發布狀態**\n"
                    f"- stable: {state.get('stable_version') or '未設定'}\n"
                    f"- canary_active: {state.get('canary_active')}\n"
                    f"- canary_version: {state.get('canary_version') or 'n/a'}\n"
                    f"- canary_percent: {state.get('canary_percent', 0)}%\n"
                    f"- auto_promote: {state.get('auto_promote', True)} (runs>={state.get('promote_min_runs', 10)}, failure_rate<={state.get('promote_max_failure_rate', 0.2)})\n"
                    f"- last_promoted: {state.get('last_promoted_version') or 'n/a'}\n"
                    f"- stats: runs={stats.get('runs',0)}, success={stats.get('success',0)}, fail={stats.get('fail',0)}"
                )
            except Exception as e:
                return f"❌ 發布狀態查詢失敗: {e}"

        # Iron Dome Dynamic Rules (Admin Only)
        if any(kw in msg_lower for kw in ["鐵穹規則", "iron dome rules", "iron_dome rules"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以查看鐵穹規則。"
            try:
                from skills.evolution.skill_genesis import list_iron_dome_patterns

                result = list_iron_dome_patterns(include_static=False, include_disabled=False, limit=40)
                if not result.get("success"):
                    return f"❌ 讀取鐵穹規則失敗: {result.get('error','unknown')}"
                dynamic = result.get("dynamic", [])
                lines = [
                    "🛡️ **鐵穹動態規則**",
                    f"- dynamic_count: {result.get('dynamic_count', 0)}",
                    f"- updated_at: {result.get('updated_at') or 'n/a'}",
                ]
                if dynamic:
                    lines.append("最近規則:")
                    for item in dynamic[:10]:
                        rid = item.get("id", "")
                        pat = item.get("pattern", "")[:80]
                        hits = item.get("hits", 0)
                        lines.append(f"- {rid} hits={hits} `{pat}`")
                else:
                    lines.append("（目前沒有動態規則）")
                return "\n".join(lines)
            except Exception as e:
                return f"❌ 鐵穹規則查詢失敗: {e}"

        if any(kw in msg_lower for kw in ["加入鐵穹規則", "add iron dome rule"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以修改鐵穹規則。"
            try:
                from skills.evolution.skill_genesis import add_iron_dome_pattern

                pat = (
                    message.replace("加入鐵穹規則", "")
                    .replace("add iron dome rule", "")
                    .strip()
                )
                if not pat:
                    return "❓ 請提供 regex，例如：`加入鐵穹規則 rm\\s+-rf`"
                result = add_iron_dome_pattern(pat, reason="admin_add", source=f"{role}:{user_id}", enabled=True)
                if result.get("success"):
                    return f"✅ 已加入鐵穹規則：`{result.get('id','')}`"
                return f"❌ 加入規則失敗: {result.get('error','unknown')}"
            except Exception as e:
                return f"❌ 加入規則流程失敗: {e}"

        if any(kw in msg_lower for kw in ["自動加固鐵穹", "auto harden iron dome"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以執行鐵穹加固。"
            try:
                from skills.evolution.skill_genesis import auto_harden_iron_dome_scope

                incident = (
                    message.replace("自動加固鐵穹", "")
                    .replace("auto harden iron dome", "")
                    .strip()
                )
                if not incident:
                    return "❓ 請貼上要用來加固的 incident 內容（錯誤訊息/攻擊樣本/日誌片段）。"
                result = auto_harden_iron_dome_scope(incident, source=f"{role}:{user_id}", max_new=3)
                added = result.get("added", [])
                return f"🛡️ 鐵穹加固完成：新增 {len(added)} 條規則。"
            except Exception as e:
                return f"❌ 鐵穹加固失敗: {e}"

        if any(kw in msg_lower for kw in ["供應鏈掃描", "supply chain scan", "supply chain audit", "npm audit", "鐵穹掃描套件"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以執行供應鏈掃描。"
            try:
                from skills.iron_dome import core as _id_core

                result = _id_core.audit_supply_chain()
                findings = result.get("findings", [])
                if result.get("ok") and not findings:
                    return "🛡️ **供應鏈掃描完成：安全** ✅\n未發現已知惡意套件或可疑依賴。"
                lines = [f"🛡️ **供應鏈掃描完成：發現 {len(findings)} 項問題**"]
                for f in findings[:15]:
                    sev = f.get("severity", "?")
                    icon = "🚨" if sev == "CRITICAL" else "⚠️"
                    lines.append(f"{icon} [{sev}] {f.get('package', '?')}@{f.get('version', '?')} — {f.get('detail', '')}")
                    lines.append(f"   📁 {f.get('file', '')}")
                if len(findings) > 15:
                    lines.append(f"...（還有 {len(findings) - 15} 項）")
                if not result.get("ok"):
                    lines.append("\n⚠️ 建議立即移除 CRITICAL 級別的套件！")
                return "\n".join(lines)
            except Exception as e:
                return f"❌ 供應鏈掃描失敗: {e}"

        if any(kw in msg_lower for kw in ["核心變更待審", "core approvals", "pending core changes"]):
            try:
                from skills.magi.council_approval import format_pending_summary

                return format_pending_summary(limit=20)
            except Exception as e:
                return f"❌ 讀取核心待審清單失敗: {e}"

        if any(kw in msg_lower for kw in ["批准核心變更", "approve core"]):
            try:
                from skills.magi.council_approval import resolve_core_change

                text = (
                    message.replace("批准核心變更", "")
                    .replace("approve core", "")
                    .strip()
                )
                if not text:
                    return "❓ 請提供待審 ID，例如：`批准核心變更 ccr-20260213094500`"
                parts = text.split(maxsplit=1)
                approval_id = parts[0]
                note = parts[1] if len(parts) > 1 else ""
                result = resolve_core_change(approval_id, "approved", approver=user_id, note=note)
                if result.get("success"):
                    return f"✅ 核心變更已核准：`{approval_id}`"
                return f"❌ 核准失敗：{result.get('error')}"
            except Exception as e:
                return f"❌ 核准流程錯誤：{e}"

        if any(kw in msg_lower for kw in ["拒絕核心變更", "reject core"]):
            try:
                from skills.magi.council_approval import resolve_core_change

                text = (
                    message.replace("拒絕核心變更", "")
                    .replace("reject core", "")
                    .strip()
                )
                if not text:
                    return "❓ 請提供待審 ID，例如：`拒絕核心變更 ccr-20260213094500 缺少回滾方案`"
                parts = text.split(maxsplit=1)
                approval_id = parts[0]
                note = parts[1] if len(parts) > 1 else ""
                result = resolve_core_change(approval_id, "rejected", approver=user_id, note=note)
                if result.get("success"):
                    return f"🛑 核心變更已拒絕：`{approval_id}`"
                return f"❌ 拒絕失敗：{result.get('error')}"
            except Exception as e:
                return f"❌ 拒絕流程錯誤：{e}"

        skill_python = (os.environ.get("MAGI_SKILL_PYTHON") or "").strip()
        if not skill_python:
            skill_python = f"{_MAGI_ROOT}/venv/bin/python"
        if not os.path.exists(skill_python):
            skill_python = sys.executable or "python3"

        if any(k in msg_lower for k in ["法扶回報指令", "法扶指令", "回報指令"]):
            return self._laf_report_command_help()

        # 法扶狀態手動更新：「[當事人E] 已開辦」「[當事人N] 已報結」
        try:
            from api.handlers.laf_handler import parse_laf_status_update
            _status_upd = parse_laf_status_update(message)
            if _status_upd and role == "admin":
                _ok = self._update_laf_status_after_action(
                    case_number=_status_upd.get("case_number", ""),
                    client_name=_status_upd.get("client_name", ""),
                    laf_case_no=_status_upd.get("laf_case_no", ""),
                    case_reason_hint=_status_upd.get("case_reason_hint", ""),
                    new_status=_status_upd["new_status"],
                    action_label=f"手動更新（{_status_upd['status_label']}）",
                )
                if _ok:
                    _target = _status_upd.get("client_name") or _status_upd.get("case_number") or _status_upd.get("laf_case_no")
                    return f"✅ 已更新 {_target} 的法扶狀態為「{_status_upd['new_status']}」"
                else:
                    # 多筆同名案件 → 提示使用者指定案號
                    _hint = getattr(self, "_ambiguous_laf_status_hint", "")
                    if _hint:
                        self._ambiguous_laf_status_hint = ""
                        return _hint
                    _target = _status_upd.get("client_name") or _status_upd.get("case_number") or _status_upd.get("laf_case_no")
                    return f"❌ 找不到 {_target} 的案件，無法更新狀態。請確認姓名或案號是否正確。"
        except Exception as _su_err:
            logger.debug("LAF status update parse skipped: %s", _su_err)

        laf_payload = self._parse_laf_report_payload(message)
        if laf_payload:
            logger.info("📋 LAF report payload: %s (from message: %r)", laf_payload, message[:80])

            if not any([laf_payload.get("laf_case_no"), laf_payload.get("case_number"), laf_payload.get("client_name")]):
                return (
                    "❓ 我知道你要做法扶回報，但缺少目標。\n"
                    "請補：姓名、法扶案號（1140728-K-002）或案件系統編號（2026-0013）之一。"
                )

            laf_script = str(get_laf_script())
            if not os.path.exists(laf_script):
                return f"❌ 找不到法扶 orchestrator：{laf_script}"

            platform_hint = "Discord" if str(user_id).startswith("discord_") else ("Telegram" if str(user_id).startswith("telegram_") else "LINE")
            timeout_sec = int(os.environ.get("MAGI_LAF_REPORT_TIMEOUT_SEC", "2400"))

            def run_laf_report(uid: str, payload_obj: dict, platform_name: str):
                action = str(payload_obj.get("action") or "").strip()
                cmd = [skill_python, laf_script, "--mode", "portal-draft", "--action", action]
                if payload_obj.get("laf_case_no"):
                    cmd.extend(["--laf-case-no", str(payload_obj.get("laf_case_no"))])
                if payload_obj.get("case_number"):
                    cmd.extend(["--case", str(payload_obj.get("case_number"))])
                if payload_obj.get("client_name"):
                    cmd.extend(["--client", str(payload_obj.get("client_name"))])
                if payload_obj.get("reason"):
                    cmd.extend(["--reason", str(payload_obj.get("reason"))])
                fields = payload_obj.get("fields") if isinstance(payload_obj.get("fields"), dict) else {}
                if fields:
                    cmd.extend(["--fields-json", json.dumps(fields, ensure_ascii=False)])
                if str(os.environ.get("MAGI_LAF_CHAT_DRY_RUN", "0")).strip().lower() in {"1", "true", "yes", "on"}:
                    cmd.append("--dry-run")

                logger.info("📋 LAF subprocess cmd: %s", cmd)
                _screenshot_sent = False
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                    stdout_text = (proc.stdout or "").strip()
                    stderr_text = (proc.stderr or "").strip()

                    if proc.returncode != 0:
                        result_text = f"❌ 法扶{payload_obj.get('action_label','回報')}流程失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                    else:
                        data = None
                        if stdout_text:
                            try:
                                data = json.loads(stdout_text)
                            except Exception:
                                m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                                if m2:
                                    try:
                                        data = json.loads(m2.group(1))
                                    except Exception:
                                        data = None
                        if isinstance(data, dict):
                            if data.get("ok"):
                                identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
                                cname = str(identity.get("client_name") or payload_obj.get("client_name") or "").strip()
                                laf_no = str(identity.get("laf_case_number") or payload_obj.get("laf_case_no") or "").strip()
                                osc_no = str(identity.get("case_number") or payload_obj.get("case_number") or "").strip()
                                preview = data.get("preview") if isinstance(data.get("preview"), dict) else {}
                                shot_url = ""
                                shot_path = ""
                                html_url = ""
                                if isinstance(preview.get("png_export"), dict):
                                    shot_url = str(preview.get("png_export", {}).get("url") or "").strip()
                                shot_path = str(preview.get("png") or "").strip()
                                if isinstance(preview.get("html_export"), dict):
                                    html_url = str(preview.get("html_export", {}).get("url") or "").strip()
                                if action == "go_live":
                                    lines = [f"✅ 法扶{payload_obj.get('action_label','回報')}已完成填寫（尚未送出）"]
                                else:
                                    lines = [f"✅ 法扶{payload_obj.get('action_label','回報')}已完成存檔（未送出）"]
                                target_parts = [x for x in [cname, laf_no, osc_no] if x]
                                if target_parts:
                                    lines.append("目標：" + "｜".join(target_parts))
                                if payload_obj.get("reason"):
                                    lines.append(f"說明：{payload_obj.get('reason')}")
                                if action != "go_live":
                                    if shot_url:
                                        lines.append(f"畫面預覽：{shot_url}")
                                    elif shot_path:
                                        lines.append(f"畫面截圖：{shot_path}")
                                    if html_url:
                                        lines.append(f"頁面 HTML：{html_url}")
                                if action == "go_live":
                                    dates = data.get("dates") if isinstance(data.get("dates"), dict) else {}
                                    od = str(dates.get("opening_date") or "").strip()
                                    pd = str(dates.get("poa_submit_date") or "").strip()
                                    if od:
                                        lines.append(f"開辦通知日期：{od}")
                                    if pd:
                                        lines.append(f"委任狀遞出日期：{pd}")
                                    if shot_url:
                                        lines.append(f"畫面預覽：{shot_url}")
                                    elif shot_path:
                                        lines.append(f"畫面截圖：{shot_path}")
                                    if html_url:
                                        lines.append(f"頁面 HTML：{html_url}")
                                    token = ""
                                    try:
                                        e = self._register_laf_go_live_submit_pending(
                                            platform=platform_name,
                                            requester_user_id=uid,
                                            payload=payload_obj,
                                            result_data=data,
                                        )
                                        token = str(e.get("token") or "").strip()
                                    except Exception as reg_err:
                                        logger.warning(f"Register go_live submit pending failed: {reg_err}")
                                        token = ""
                                    if token:
                                        lines.append("請確認以上畫面與資料是否正確（你或同事皆可確認）。")
                                        lines.append(f"回覆：`正確送出 {token}`")
                                        lines.append(f"取消：`取消送出 {token}`")
                                if action in {"fee", "condition"}:
                                    docs = data.get("docs") if isinstance(data.get("docs"), dict) else {}
                                    if action == "fee" and docs.get("pink_receipt"):
                                        lines.append(f"收據：{os.path.basename(str(docs.get('pink_receipt')))}")
                                    if action == "condition" and docs.get("mediation_failure"):
                                        lines.append(f"證明：{os.path.basename(str(docs.get('mediation_failure')))}")
                                if action == "closing":
                                    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
                                    if counts:
                                        # 統計摘要
                                        _stats = []
                                        for _key, _label in [
                                            ("meeting_count", "開會"), ("contact_count", "聯繫"),
                                            ("inq_count", "律見"), ("court_count", "開庭"),
                                            ("review_count", "閱卷"), ("document_count", "書狀"),
                                        ]:
                                            if _key in counts:
                                                _stats.append(f"{_label} {int(counts[_key] or 0)}")
                                        if _stats:
                                            lines.append(f"統計：{'／'.join(_stats)}")

                                        # 案號
                                        _court_name = str(counts.get("court_name") or "").strip()
                                        _case_year = str(counts.get("court_case_year") or "").strip()
                                        _case_code = str(counts.get("court_case_code") or "").strip()
                                        _case_no = str(counts.get("court_case_no") or "").strip()
                                        if _court_name and _case_year:
                                            lines.append(f"案號：{_court_name}{_case_year}年度{_case_code}字第{_case_no}號")

                                        # 結果
                                        _result = str(counts.get("closing_result") or "").strip()
                                        if _result:
                                            lines.append(f"結果：{_result[:80]}")

                                        # 裁判效力
                                        _doc_type = str(counts.get("closing_doc_type") or "").strip()
                                        _judg_eff = str(counts.get("judg_eff") or "").strip()
                                        if _doc_type or _judg_eff:
                                            lines.append(f"裁判：{_doc_type}{'，' + _judg_eff if _judg_eff else ''}")

                                        # 零值警告
                                        _label_map = {"meeting_count": "開會", "contact_count": "聯繫", "court_count": "開庭", "review_count": "閱卷", "document_count": "書狀"}
                                        _zeros = [_label_map[k] for k in _label_map if int(counts.get(k, 0) or 0) == 0]
                                        if _zeros:
                                            lines.append(f"⚠️ 以下為 0：{'、'.join(_zeros)}，請確認「扶助律師特別說明」是否需要修改")

                                    # 上傳檔案數
                                    _upload_bundle = data.get("upload_bundle") if isinstance(data.get("upload_bundle"), dict) else {}
                                    _upload_files = _upload_bundle.get("pdf_files") or []
                                    if _upload_files:
                                        lines.append(f"上傳：{len(_upload_files)} 份")

                                    # 零值理由
                                    _zero_reasons = data.get("zero_reasons") if isinstance(data.get("zero_reasons"), dict) else {}
                                    if _zero_reasons:
                                        _zr_label_map = {"disc_times": "討論次數", "review_count": "閱卷", "court_count": "開庭", "document_count": "書狀"}
                                        lines.append("理由：")
                                        for _zk, _zv in _zero_reasons.items():
                                            lines.append(f"- {_zr_label_map.get(_zk, _zk)}：{_zv}")

                                    # 安全政策
                                    if os.environ.get("MAGI_LAF_DRAFT_ONLY", "1") == "1":
                                        lines.append("🔒 安全政策：目前僅暫存，不會代為送出。")
                                    else:
                                        lines.append("可回覆「送出」由 CASPER 代為送出（請先確認平台畫面）。")
                                if action == "withdrawal":
                                    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
                                    if counts:
                                        lines.append(
                                            "辦理情形：開會{meeting_count}／聯繫{contact_count}／開庭{court_count}／書狀{document_count}／閱卷{review_count}".format(
                                                meeting_count=int(counts.get("meeting_count", 0) or 0),
                                                contact_count=int(counts.get("contact_count", 0) or 0),
                                                court_count=int(counts.get("court_count", 0) or 0),
                                                document_count=int(counts.get("document_count", 0) or 0),
                                                review_count=int(counts.get("review_count", 0) or 0),
                                            )
                                        )
                                result_text = "\n".join(lines)
                                # 傳送截圖圖片（go_live + closing 都需要）
                                _screenshot_sent = False
                                if action in ("go_live", "closing") and shot_path and os.path.isfile(shot_path):
                                    try:
                                        from skills.ops.red_phone import send_file_admin, send_discord_bot_file
                                        _caption = result_text[:800]
                                        _laf_topic = "laf_go_live" if action == "go_live" else ("laf_closing" if action == "closing" else "laf")
                                        _plat = str(platform_name or "").strip().lower()
                                        if _plat == "telegram":
                                            send_file_admin(file_path=shot_path, caption=_caption, topic_key=_laf_topic)
                                        elif _plat == "discord":
                                            send_discord_bot_file(file_path=shot_path, caption=_caption, topic_key=_laf_topic, source=_laf_topic)
                                        else:
                                            send_file_admin(file_path=shot_path, caption=_caption, topic_key=_laf_topic)
                                            send_discord_bot_file(file_path=shot_path, caption=_caption, topic_key=_laf_topic, source=_laf_topic)
                                        _screenshot_sent = True  # 避免 notification_callback 重複發送
                                    except Exception as _img_err:
                                        logger.warning("LAF screenshot send failed: %s", _img_err)
                                # 回寫 DB：closing 成功 → legal_aid_status = "已報結"
                                #          withdrawal 成功 → "已報結"
                                if action in ("closing", "withdrawal") and data.get("server_verified"):
                                    try:
                                        _upd_osc = osc_no or str(payload_obj.get("case_number") or "").strip()
                                        _upd_cli = cname or str(payload_obj.get("client_name") or "").strip()
                                        if _upd_osc or _upd_cli:
                                            self._update_laf_status_after_action(
                                                case_number=_upd_osc,
                                                client_name=_upd_cli,
                                                new_status="已報結",
                                                action_label=f"報結（{action}）",
                                            )
                                    except Exception as _db_err2:
                                        logger.warning("closing DB status update failed: %s", _db_err2)
                            else:
                                err = str(data.get("error") or "unknown").strip()
                                if err == "missing_target":
                                    result_text = (
                                        f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：缺少目標。\n"
                                        "請補上姓名、法扶案號或案件系統編號。"
                                    )
                                elif err == "missing_case_folder":
                                    result_text = (
                                        f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：找不到案件資料夾。\n"
                                        "請先確認該案已建立資料夾並可由 DB 對應。"
                                    )
                                elif err == "missing_reason":
                                    result_text = (
                                        f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：缺少『疑義原因』。\n"
                                        "請重送：`... 疑義回報 原因 <你的原因>`"
                                    )
                                elif err == "missing_required_docs":
                                    missing = data.get("missing") if isinstance(data.get("missing"), list) else []
                                    miss_txt = "、".join(str(x) for x in missing) if missing else "必要文件"
                                    result_text = (
                                        f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：缺少文件：{miss_txt}\n"
                                        "請先把文件放入對應案件資料夾後再重試。"
                                    )
                                elif err == "missing_required_dates":
                                    missing = data.get("missing") if isinstance(data.get("missing"), list) else []
                                    miss_txt = "、".join(str(x) for x in missing) if missing else "必要日期"
                                    result_text = (
                                        f"❌ 法扶{payload_obj.get('action_label','回報')}失敗：視覺判讀日期不足（{miss_txt}）。\n"
                                        "請確認開辦通知書/委任狀內容清晰。"
                                    )
                                elif err == "need_reason_for_low_counts":
                                    label_map = {
                                        "meeting_count": "開會",
                                        "contact_count": "聯繫",
                                        "court_count": "開庭",
                                        "document_count": "書狀",
                                        "review_count": "閱卷",
                                    }
                                    lows = data.get("low_fields") if isinstance(data.get("low_fields"), list) else []
                                    low_txt = "、".join(label_map.get(str(x), str(x)) for x in lows) if lows else "低值欄位"
                                    result_text = (
                                        "⚠️ 結案回報暫停：以下統計 <= 0，需要你提供原因後才能存檔。\n"
                                        f"欄位：{low_txt}\n"
                                        "請回覆：`<當事人/案號> 結案回報 原因 <理由>`"
                                    )
                                elif err == "portal_draft_failed":
                                    result_text = (
                                        f"❌ 法扶{payload_obj.get('action_label','回報')}表單填寫失敗。\n"
                                        "可能原因：法扶網站登入逾時、頁面載入異常或按鈕找不到。\n"
                                        "請稍後重試，或手動在法扶系統確認。"
                                    )
                                elif err == "identity_needs_manual_confirmation":
                                    _identity = data.get("identity") or {}
                                    _reason = _identity.get("manual_reason", "")
                                    _conflicts = _identity.get("conflicts", [])
                                    _hint_lines = [f"⚠️ 法扶{payload_obj.get('action_label','回報')}需要補充資訊："]
                                    if _reason == "missing_case_or_laf_signal":
                                        _hint_lines.append("系統無法辨識案件，請補上以下任一資訊：")
                                        _hint_lines.append("• 法扶案號（如 1141223-E-021）")
                                        _hint_lines.append("• 案件系統編號（如 2025-0087）")
                                        _hint_lines.append("• 當事人姓名 + 案由")
                                        _hint_lines.append("")
                                        _hint_lines.append("範例：`1141223-E-021 結案` 或 `[當事人L] 更生 結案`")
                                    elif _reason == "identity_signal_conflict":
                                        _hint_lines.append("找到的案件資訊有衝突，無法自動確認：")
                                        for _c in _conflicts[:3]:
                                            _hint_lines.append(f"• {_c.get('client_name','')} ({_c.get('laf_case_number','')}) — {_c.get('reason','')}")
                                        _hint_lines.append("")
                                        _hint_lines.append("請用更精確的法扶案號重試。")
                                    elif "conflict" in _reason:
                                        _hint_lines.append(f"案件比對有衝突（{_reason}），請確認後用法扶案號重試。")
                                    else:
                                        _hint_lines.append(f"原因：{_reason}")
                                        _hint_lines.append("請補上法扶案號或案件系統編號後重試。")
                                    result_text = "\n".join(_hint_lines)
                                else:
                                    result_text = f"❌ 法扶{payload_obj.get('action_label','回報')}存檔失敗：{err}"
                        else:
                            result_text = f"✅ 法扶{payload_obj.get('action_label','回報')}流程完成（未送出）。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"
                except subprocess.TimeoutExpired:
                    result_text = f"⏳ 法扶{payload_obj.get('action_label','回報')}流程逾時（>{timeout_sec} 秒），請稍後重試。"
                except Exception as e:
                    result_text = f"❌ 法扶{payload_obj.get('action_label','回報')}背景流程異常：{e}"

                try:
                    if getattr(self, "notification_callback", None) and not _screenshot_sent:
                        # 截圖已帶 caption 送出時不再重複發送文字（避免同頻道收到兩次）
                        _cb_topic = {"go_live": "laf_go_live", "closing": "laf_closing"}.get(action, "laf_dispatch")
                        self.notification_callback(uid, result_text, platform_name, topic_key=_cb_topic)
                except Exception as notify_err:
                    logger.warning(f"LAF report callback failed: {notify_err}")

            # 2026-03-29: removed local import threading (use module-level import)
            thread = threading.Thread(
                target=run_laf_report,
                args=(str(user_id), laf_payload, platform_hint),
                daemon=True,
            )
            thread.start()

            target_hint = laf_payload.get("client_name") or laf_payload.get("laf_case_no") or laf_payload.get("case_number") or "（未指定）"
            if str(laf_payload.get("action") or "") == "go_live":
                launch_line = f"⏳ 已啟動法扶{laf_payload.get('action_label','回報')}流程（先填寫並截圖，待確認後才送出）。"
            else:
                launch_line = f"⏳ 已啟動法扶{laf_payload.get('action_label','回報')}流程（只存檔不送出）。"
            return f"{launch_line}\n目標：{target_hint}\n完成後我會主動回報。"

        # ── 繳費通知手動標記已繳費 / 跳過 ──
        _dismiss_payment_kw = ""
        _dismiss_m = re.search(r"^(.+?)\s*(?:已繳費|已經繳費|繳費完畢|繳費了)\s*$", message.strip())
        if _dismiss_m:
            _dismiss_payment_kw = _dismiss_m.group(1).strip()
        else:
            for _dtrig in ("已繳費", "跳過繳費", "繳費跳過"):
                if message.strip().startswith(_dtrig):
                    _dismiss_payment_kw = message.strip()[len(_dtrig):].strip()
                    break
        if _dismiss_payment_kw:
            try:
                _action_script = f"{_MAGI_ROOT}/skills/file-review-orchestrator/action.py"
                _py = os.environ.get("MAGI_SKILL_PYTHON", "").strip()
                if not _py or not os.path.exists(_py):
                    _py = sys.executable or "python3"
                _task_str = 'dismiss_payment ' + json.dumps({"case_keyword": _dismiss_payment_kw}, ensure_ascii=False)
                _proc = subprocess.run(
                    [_py, _action_script, "--task", _task_str],
                    capture_output=True, text=True, timeout=30,
                )
                _out = (_proc.stdout or "").strip()
                try:
                    _result = json.loads(_out)
                    _data = _result.get("data", {}) if isinstance(_result, dict) else {}
                    _new = _data.get("new_dismissals", 0)
                    _already = _data.get("already_dismissed", 0)
                    if _new:
                        return f"✅ 已標記「{_dismiss_payment_kw}」為已繳費，後續不再通知。"
                    elif _already:
                        return f"ℹ️ 「{_dismiss_payment_kw}」先前已標記為已繳費。"
                    else:
                        return f"✅ 已記錄「{_dismiss_payment_kw}」為已繳費。"
                except Exception:
                    return f"✅ 已標記「{_dismiss_payment_kw}」為已繳費。"
            except Exception as _e:
                logger.warning("dismiss_payment failed: %s", _e)
                return f"❌ 標記繳費狀態失敗：{type(_e).__name__}"

        # File Review Probe (chat-callable formal skill command)
        probe_aliases = ["閱卷查核", "查核閱卷", "卷宗查核", "查核卷宗", "卷宗檢核", "檢核卷宗"]
        if any(msg_lower.startswith(alias) for alias in probe_aliases):

            def _parse_probe_payload(raw_text: str):
                raw = (raw_text or "").strip()
                alias_hit = next((alias for alias in probe_aliases if raw.lower().startswith(alias)), "")
                remainder = raw[len(alias_hit):].strip() if alias_hit else raw
                if not remainder:
                    return None

                # JSON payload mode.
                if remainder.startswith("{"):
                    try:
                        payload = json.loads(remainder)
                        if isinstance(payload, dict):
                            return payload
                    except Exception:
                        return None

                # Natural phrase mode: <法院> <案號>
                parts = remainder.split()
                if len(parts) < 2:
                    return None
                court = parts[0].strip()
                case_text = parts[1].strip()
                m = re.match(r"(\d{2,3})\s*(?:年度)?\s*([^\d\s]+)\s*(?:字)?\s*(?:第)?\s*(\d+)\s*(?:號)?", case_text)
                if not m:
                    return None
                case_type = re.sub(r"(字第|字|第)", "", (m.group(2) or "")).strip()
                return {
                    "court_code": court,
                    "year": m.group(1),
                    "case_type": case_type,
                    "case_number": m.group(3),
                }

            payload = _parse_probe_payload(message)
            if not payload:
                return (
                    "❓ 指令格式：`閱卷查核 <法院> <案號>`\n"
                    "例如：`閱卷查核 基隆 114訴1`\n"
                    "或：`閱卷查核 {\"court_code\":\"KLD\",\"year\":\"114\",\"case_type\":\"訴\",\"case_number\":\"1\"}`"
                )

            action_script = f"{_MAGI_ROOT}/skills/file-review-orchestrator/action.py"
            if not os.path.exists(action_script):
                return f"❌ 找不到 skill 腳本：{action_script}"

            task_payload = {
                "court_code": str(payload.get("court_code", "")).strip(),
                "year": str(payload.get("year", "")).strip(),
                "case_type": str(payload.get("case_type", "")).strip(),
                "case_number": str(payload.get("case_number", "")).strip(),
                "client_name": str(payload.get("client_name", "")).strip(),
            }
            if not all([task_payload["court_code"], task_payload["year"], task_payload["case_type"], task_payload["case_number"]]):
                return "❌ 缺少必要欄位：court_code/year/case_type/case_number"

            platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"
            timeout_sec = int(os.environ.get("MAGI_FILE_REVIEW_PROBE_TIMEOUT_SEC", "1800"))

            def run_probe(uid: str, payload_obj: dict, platform_name: str):
                def _sanitize_filereview_text(raw_text: str) -> str:
                    t = str(raw_text or "").strip()
                    if not t:
                        return ""
                    tl = t.lower()
                    looks_web_notice = (
                        ("尊敬的客戶" in t and "閱卷服務" in t)
                        or ("若您已完成閱卷" in t and "可下載狀態" in t)
                        or ("登入正確帳戶" in t and "雲端儲存空間" in t)
                        or ("<html" in tl and "</html>" in tl)
                        or ("<!doctype html" in tl)
                    )
                    if looks_web_notice:
                        return "⚠️ 偵測到網站提示頁文案（非系統通知文字），目前判定為暫無可下載檔案。"
                    return t if len(t) <= 700 else (t[:700] + "…")

                task_text = f"probe {json.dumps(payload_obj, ensure_ascii=False)}"
                cmd = [skill_python, action_script, "--task", task_text]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                    stdout_text = (proc.stdout or "").strip()
                    stderr_text = (proc.stderr or "").strip()

                    if proc.returncode != 0:
                        result_text = f"❌ 閱卷查核失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                    else:
                        data = None
                        if stdout_text:
                            try:
                                data = json.loads(stdout_text)
                            except Exception:
                                m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                                if m2:
                                    try:
                                        data = json.loads(m2.group(1))
                                    except Exception:
                                        data = None

                        if isinstance(data, dict):
                            if data.get("success"):
                                case_label = str(data.get("case", "")).strip()
                                status = str(data.get("result", "")).strip()
                                summary = _sanitize_filereview_text(str(data.get("message", "")))
                                if status == "Ready":
                                    head = "✅ 閱卷查核完成：卷宗已可下載"
                                elif status == "Applied":
                                    head = "📋 閱卷查核完成：目前為已聲請/處理中"
                                else:
                                    head = f"ℹ️ 閱卷查核完成：{status or 'unknown'}"
                                result_text = "\n".join(x for x in [head, case_label, summary] if x)
                            else:
                                result_text = f"❌ 閱卷查核失敗：{str(data.get('error', 'unknown')).strip()}"
                        else:
                            result_text = f"✅ 閱卷查核流程完成。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"

                except subprocess.TimeoutExpired:
                    result_text = f"⏳ 閱卷查核逾時（>{timeout_sec} 秒），請稍後重試。"
                except Exception as e:
                    result_text = f"❌ 閱卷查核背景流程異常：{e}"

                try:
                    if getattr(self, "notification_callback", None):
                        self.notification_callback(uid, result_text, platform_name)
                except Exception as notify_err:
                    logger.warning(f"File-review probe callback failed: {notify_err}")

            thread = threading.Thread(
                target=run_probe,
                args=(str(user_id), task_payload, platform_hint),
                daemon=True,
            )
            thread.start()

            return (
                "⏳ 已啟動閱卷查核（只查核、不送出）。\n"
                f"目標：{task_payload['court_code']} {task_payload['year']}年{task_payload['case_type']}字第{task_payload['case_number']}號\n"
                "完成後會主動回報。"
            )

        # File Review Apply — 閱卷聲請 (chat-callable formal skill command)
        apply_aliases = ["閱卷聲請", "聲請閱卷", "申請閱卷", "聲請閱覽"]
        if any(msg_lower.startswith(alias) for alias in apply_aliases):

            def _parse_apply_payload(raw_text: str):
                raw = (raw_text or "").strip()
                alias_hit = next((alias for alias in apply_aliases if raw.lower().startswith(alias)), "")
                remainder = raw[len(alias_hit):].strip() if alias_hit else raw
                if not remainder:
                    return None

                # JSON payload mode.
                if remainder.startswith("{"):
                    try:
                        payload = json.loads(remainder)
                        if isinstance(payload, dict):
                            return payload
                    except Exception:
                        return None

                # Natural phrase mode: <法院> <案號> [當事人]
                parts = remainder.split()
                if len(parts) < 2:
                    return None
                court = parts[0].strip()
                case_text = parts[1].strip()
                m = re.match(r"(\d{2,3})\s*(?:年度)?\s*([^\d\s]+)\s*(?:字)?\s*(?:第)?\s*(\d+)\s*(?:號)?", case_text)
                if not m:
                    return None
                case_type = re.sub(r"(字第|字|第)", "", (m.group(2) or "")).strip()
                result = {
                    "court_code": court,
                    "year": m.group(1),
                    "case_type": case_type,
                    "case_number": m.group(3),
                }
                # Optional: client_name or case category after case number
                if len(parts) >= 3:
                    extra = parts[2].strip()
                    if extra in ("刑事", "民事", "行政"):
                        pass  # category hint, already embedded in case_type
                    else:
                        result["client_name"] = extra
                if len(parts) >= 4 and "client_name" not in result:
                    result["client_name"] = parts[3].strip()
                return result

            payload = _parse_apply_payload(message)
            if not payload:
                return (
                    "❓ 指令格式：`閱卷聲請 <法院> <案號> <當事人>`\n"
                    "例如：`閱卷聲請 花蓮 115原侵訴1 王小明`\n"
                    "或：`閱卷聲請 台北 114訴123 張三`\n"
                    "（當事人未填時會嘗試從案件 DB 自動帶入）\n"
                    "或：`閱卷聲請 {\"court_code\":\"HLD\",\"year\":\"115\",\"case_type\":\"原侵訴\",\"case_number\":\"1\",\"client_name\":\"王小明\"}`"
                )

            action_script = f"{_MAGI_ROOT}/skills/file-review-orchestrator/action.py"
            if not os.path.exists(action_script):
                return f"❌ 找不到 skill 腳本：{action_script}"

            task_payload = {
                "court_code": str(payload.get("court_code", "")).strip(),
                "year": str(payload.get("year", "")).strip(),
                "case_type": str(payload.get("case_type", "")).strip(),
                "case_number": str(payload.get("case_number", "")).strip(),
                "client_name": str(payload.get("client_name", "")).strip(),
            }
            if not all([task_payload["court_code"], task_payload["year"], task_payload["case_type"], task_payload["case_number"]]):
                return "❌ 缺少必要欄位：court_code/year/case_type/case_number"

            platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"
            timeout_sec = int(os.environ.get("MAGI_FILE_REVIEW_APPLY_TIMEOUT_SEC", "1800"))

            # 2026-03-29: removed local import threading (use module-level import)

            def run_apply(uid: str, payload_obj: dict, platform_name: str):
                task_text = f"apply {json.dumps(payload_obj, ensure_ascii=False)}"
                cmd = [skill_python, action_script, "--task", task_text]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                    stdout_text = (proc.stdout or "").strip()
                    stderr_text = (proc.stderr or "").strip()

                    if proc.returncode != 0:
                        result_text = f"❌ 閱卷聲請失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                    else:
                        data = None
                        if stdout_text:
                            try:
                                data = json.loads(stdout_text)
                            except Exception:
                                m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                                if m2:
                                    try:
                                        data = json.loads(m2.group(1))
                                    except Exception:
                                        data = None

                        if isinstance(data, dict):
                            if data.get("success"):
                                case_label = str(data.get("case", "")).strip()
                                msg = str(data.get("message", "")).strip()
                                result_text = f"📋 閱卷聲請已送出\n{case_label}\n{msg}".strip()
                            else:
                                result_text = f"❌ 閱卷聲請失敗：{str(data.get('error', 'unknown')).strip()}"
                        else:
                            result_text = f"📋 閱卷聲請流程完成。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"

                except subprocess.TimeoutExpired:
                    result_text = f"⏳ 閱卷聲請逾時（>{timeout_sec} 秒），請稍後重試。"
                except Exception as e:
                    result_text = f"❌ 閱卷聲請背景流程異常：{e}"

                try:
                    if getattr(self, "notification_callback", None):
                        self.notification_callback(uid, result_text, platform_name)
                except Exception as notify_err:
                    logger.warning(f"File-review apply callback failed: {notify_err}")

            thread = threading.Thread(
                target=run_apply,
                args=(str(user_id), task_payload, platform_hint),
                daemon=True,
            )
            thread.start()

            label = f"{task_payload['court_code']} {task_payload['year']}年{task_payload['case_type']}字第{task_payload['case_number']}號"
            client_hint = f"\n當事人：{task_payload['client_name']}" if task_payload.get("client_name") else ""
            return (
                f"⏳ 已啟動閱卷聲請。\n"
                f"目標：{label}{client_hint}\n"
                "完成後會主動回報。"
            )

        # Transcript downloader (chat-callable formal skill command)
        transcript_aliases = ["下載筆錄", "筆錄下載", "調閱筆錄", "筆錄調閱", "筆錄同步", "同步筆錄", "筆錄全同步", "筆錄更名", "更名筆錄"]
        if any(msg_lower.startswith(alias) for alias in transcript_aliases):

            transcript_script = f"{_MAGI_ROOT}/skills/transcript-downloader/action.py"
            if not os.path.exists(transcript_script):
                return f"❌ 找不到 skill 腳本：{transcript_script}"

            if any(msg_lower.startswith(x) for x in ["下載筆錄", "筆錄下載", "調閱筆錄", "筆錄調閱"]):
                # Require case number for direct download command.
                parts = message.strip().split(maxsplit=1)
                if len(parts) < 2:
                    return "❓ 指令格式：`下載筆錄 <案號>`，例如：`下載筆錄 114年度訴字第123號`"

            task_text = message.strip()
            platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"
            timeout_sec = int(os.environ.get("MAGI_TRANSCRIPT_TASK_TIMEOUT_SEC", "2400"))

            def run_transcript(uid: str, platform_name: str, task_value: str):
                def _basename(path_text: str) -> str:
                    try:
                        s = str(path_text or "").strip()
                        return os.path.basename(s) if s else ""
                    except Exception:
                        return ""

                def _format_transcript_details(payload: dict) -> list[str]:
                    lines: list[str] = []
                    cases = payload.get("cases")
                    if isinstance(cases, list) and cases:
                        shown = 0
                        lines.append("案件明細：")
                        for row in cases:
                            if not isinstance(row, dict):
                                continue
                            if shown >= 6:
                                break
                            case_no = str(row.get("case_number") or "").strip()
                            court_case_no = str(row.get("court_case_number") or "").strip()
                            party = str(row.get("client_name") or "").strip()
                            label_parts = [x for x in [party, court_case_no or case_no] if x]
                            label = "｜".join(label_parts) if label_parts else (court_case_no or case_no or "未判斷案件")
                            files = row.get("files")
                            file_list = files if isinstance(files, list) else []
                            lines.append(f"{shown + 1}. {label}（{len(file_list)} 份）")
                            for fp in file_list[:2]:
                                bn = _basename(fp) or str(fp).strip()
                                if bn:
                                    lines.append(f"- {bn}")
                            shown += 1
                        remaining = len([r for r in cases if isinstance(r, dict)]) - shown
                        if remaining > 0:
                            lines.append(f"...其餘 {remaining} 案略")
                    elif isinstance(payload.get("files"), list) and payload.get("files"):
                        lines.append("檔案：")
                        for fp in payload.get("files", [])[:5]:
                            bn = _basename(fp) or str(fp).strip()
                            if bn:
                                lines.append(f"- {bn}")
                    return lines

                cmd = [skill_python, transcript_script, "--task", task_value]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                    stdout_text = (proc.stdout or "").strip()
                    stderr_text = (proc.stderr or "").strip()

                    if proc.returncode != 0:
                        result_text = f"❌ 筆錄流程失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                    else:
                        data = None
                        if stdout_text:
                            try:
                                data = json.loads(stdout_text)
                            except Exception:
                                m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                                if m2:
                                    try:
                                        data = json.loads(m2.group(1))
                                    except Exception:
                                        data = None

                        if isinstance(data, dict):
                            if data.get("success"):
                                lines = ["✅ 筆錄流程完成"]
                                if data.get("message"):
                                    lines.append(str(data.get("message")))
                                if "downloaded_count" in data:
                                    lines.append(f"下載數量：{data.get('downloaded_count', 0)}")
                                lines.extend(_format_transcript_details(data))
                                result_text = "\n".join(lines)
                            else:
                                result_text = f"❌ 筆錄流程失敗：{str(data.get('error', 'unknown')).strip()}"
                        else:
                            result_text = f"✅ 筆錄流程完成。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"
                except subprocess.TimeoutExpired:
                    result_text = f"⏳ 筆錄流程逾時（>{timeout_sec} 秒），請稍後重試。"
                except Exception as e:
                    result_text = f"❌ 筆錄流程背景執行異常：{e}"

                try:
                    if getattr(self, "notification_callback", None):
                        self.notification_callback(uid, result_text, platform_name)
                except Exception as notify_err:
                    logger.warning(f"Transcript callback failed: {notify_err}")

            thread = threading.Thread(
                target=run_transcript,
                args=(str(user_id), platform_hint, task_text),
                daemon=True,
            )
            thread.start()

            return "⏳ 已啟動筆錄流程，完成後會主動回報。"

        # Mock skill test (可從 TG/DC 呼叫，用模擬站驗證所有技能)
        mock_test_aliases = [
            "模擬測試", "mock test", "mock_test", "模擬站測試",
            "閱卷模擬測試", "法扶模擬測試", "模擬測試閱卷", "模擬測試法扶",
        ]
        if any(msg_lower.startswith(a) for a in mock_test_aliases):
            import subprocess as _sp, threading as _thr

            mock_skill_script = f"{_MAGI_ROOT}/skills/mock-test/action.py"
            skills_arg = "all"
            for alias in ("閱卷", "file_review", "file-review"):
                if alias in msg_lower:
                    skills_arg = "file_review"
                    break
            for alias in ("法扶", "laf"):
                if alias in msg_lower:
                    skills_arg = "laf"
                    break

            _pname = "Discord" if str(user_id).startswith("discord_") else "LINE"

            def _run_mock_test(uid, skills, pname):
                try:
                    r = _sp.run(
                        [str(get_skill_python()),
                         mock_skill_script, "--task", skills],
                        capture_output=True, text=True, timeout=600,
                    )
                    out = r.stdout.strip()
                    # Find summary line
                    summary = ""
                    for line in out.splitlines():
                        if "PASS" in line and "FAIL" in line and "共" in line:
                            summary = line.strip()
                    reply = summary or out[-300:]
                    self.notification_callback(uid, f"✅ 模擬測試完成\n{reply}", pname)
                except Exception as e:
                    self.notification_callback(uid, f"❌ 模擬測試失敗: {e}", pname)

            t = _thr.Thread(target=_run_mock_test, args=(user_id, skills_arg, _pname), daemon=True)
            t.start()
            scope = {"all": "全套", "file_review": "閱卷", "laf": "法扶"}.get(skills_arg, "全套")
            return f"⏳ 正在執行{scope}模擬測試，完成後會主動回報結果…"

        # File-review download/check commands (chat-callable formal skill command)
        review_dl_aliases = ["下載閱卷", "閱卷下載", "檢查閱卷信箱", "閱卷到期檢查", "閱卷到期", "閱卷期限"]
        if any(msg_lower.startswith(alias) for alias in review_dl_aliases):

            review_script = f"{_MAGI_ROOT}/skills/file-review-orchestrator/action.py"
            if not os.path.exists(review_script):
                return f"❌ 找不到 skill 腳本：{review_script}"

            task_text = message.strip()
            platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"
            timeout_sec = int(os.environ.get("MAGI_FILE_REVIEW_TASK_TIMEOUT_SEC", "2400"))

            def run_file_review(uid: str, platform_name: str, task_value: str):
                def _sanitize_filereview_text(raw_text: str) -> str:
                    t = str(raw_text or "").strip()
                    if not t:
                        return ""
                    tl = t.lower()
                    looks_web_notice = (
                        ("尊敬的客戶" in t and "閱卷服務" in t)
                        or ("若您已完成閱卷" in t and "可下載狀態" in t)
                        or ("登入正確帳戶" in t and "雲端儲存空間" in t)
                        or ("<html" in tl and "</html>" in tl)
                        or ("<!doctype html" in tl)
                    )
                    if looks_web_notice:
                        return "⚠️ 偵測到網站提示頁文案（非系統通知文字），目前判定為暫無可下載檔案。"
                    return t if len(t) <= 700 else (t[:700] + "…")

                def _basename(path_text: str) -> str:
                    try:
                        s = str(path_text or "").strip()
                        return os.path.basename(s) if s else ""
                    except Exception:
                        return ""

                def _format_filereview_details(payload: dict) -> list[str]:
                    lines: list[str] = []
                    items = payload.get("items")
                    if not isinstance(items, list):
                        items = []
                    if items:
                        groups = {}
                        for it in items:
                            if not isinstance(it, dict):
                                continue
                            party = str(it.get("party") or "").strip()
                            court_case_no = str(it.get("court_case_no") or "").strip()
                            folder = str(it.get("folder") or "").strip()
                            key = (party, court_case_no, folder)
                            groups.setdefault(key, []).append(it)

                        if groups:
                            lines.append("案件明細：")
                            idx = 0
                            for (party, court_case_no, folder), grouped_items in groups.items():
                                if idx >= 6:
                                    break
                                label_parts = [x for x in [party, court_case_no] if x]
                                if not label_parts and folder:
                                    label_parts.append(os.path.basename(folder))
                                label = "｜".join(label_parts) if label_parts else "未判斷案件"
                                lines.append(f"{idx + 1}. {label}（{len(grouped_items)} 份）")
                                for it in grouped_items[:2]:
                                    fn = str(it.get("file") or "").strip()
                                    dst = str(it.get("dst") or "").strip()
                                    if fn:
                                        lines.append(f"- {fn}")
                                    elif dst:
                                        lines.append(f"- {_basename(dst) or dst}")
                                idx += 1
                            remaining = len(groups) - idx
                            if remaining > 0:
                                lines.append(f"...其餘 {remaining} 案略")
                    elif isinstance(payload.get("files"), list) and payload.get("files"):
                        lines.append("檔案：")
                        for fp in payload.get("files", [])[:5]:
                            bn = _basename(fp) or str(fp).strip()
                            if bn:
                                lines.append(f"- {bn}")

                    archive_summary = payload.get("archive_summary")
                    if isinstance(archive_summary, dict):
                        unresolved = int(archive_summary.get("unresolved_count") or 0)
                        if unresolved > 0:
                            lines.append(f"⚠️ 待歸檔：{unresolved} 份")
                    return lines

                cmd = [skill_python, review_script, "--task", task_value]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                    stdout_text = (proc.stdout or "").strip()
                    stderr_text = (proc.stderr or "").strip()

                    if proc.returncode != 0:
                        result_text = f"❌ 閱卷流程失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
                    else:
                        data = None
                        if stdout_text:
                            try:
                                data = json.loads(stdout_text)
                            except Exception:
                                m2 = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
                                if m2:
                                    try:
                                        data = json.loads(m2.group(1))
                                    except Exception:
                                        data = None

                        if isinstance(data, dict):
                            if data.get("success"):
                                lines = ["✅ 閱卷流程完成"]
                                if data.get("message"):
                                    lines.append(_sanitize_filereview_text(str(data.get("message"))))
                                if "downloaded_count" in data:
                                    lines.append(f"下載數量：{data.get('downloaded_count', 0)}")
                                lines.extend(_format_filereview_details(data))
                                result_text = "\n".join(lines)
                            else:
                                result_text = f"❌ 閱卷流程失敗：{str(data.get('error', 'unknown')).strip()}"
                        else:
                            result_text = f"✅ 閱卷流程完成。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"
                except subprocess.TimeoutExpired:
                    result_text = f"⏳ 閱卷流程逾時（>{timeout_sec} 秒），請稍後重試。"
                except Exception as e:
                    result_text = f"❌ 閱卷流程背景執行異常：{e}"

                try:
                    if getattr(self, "notification_callback", None):
                        self.notification_callback(uid, result_text, platform_name)
                except Exception as notify_err:
                    logger.warning(f"File-review callback failed: {notify_err}")

            thread = threading.Thread(
                target=run_file_review,
                args=(str(user_id), platform_hint, task_text),
                daemon=True,
            )
            thread.start()

            return "⏳ 已啟動閱卷流程，完成後會主動回報。"
        
        # Existing commands
        if "court" in msg_lower or "schedule" in msg_lower:
             return execute_skill("paperclip-control", [message])
        elif "laf" in msg_lower:
             return execute_skill("laf-monitor", [message])
        elif "meeting" in msg_lower:
             return execute_skill("meetings", ["list"])
        elif "summarize" in msg_lower or "summary" in msg_lower or "balthasar" in msg_lower:
             try:
                 summary_result = summarize_text(message)
                 if summary_result and summary_result.get("success", True):
                     text = summary_result.get("text") or summary_result.get("summary") or ""
                     if text:
                         return f"🍏 Balthasar: {text}"
                 return "⚠️ Balthasar 摘要服務無可用結果，請稍後再試。"
             except Exception as e:
                 logger.warning(f"Balthasar summary fallback due to error: {e}")
                 from skills.bridge.grounded_ai import chat_casper
                 return f"🍏 Balthasar 暫時不可用，改由 Casper 摘要：\n{chat_casper('請用繁體中文摘要：' + message)}"
        elif "melchior" in msg_lower and "vision" in msg_lower:
             return "👁️ Please send me an image for Melchior to analyze."
        
        # Code Analysis Command
        # Triggered by: "analyze code", "讀取程式碼", "code folder", "code 資料夾"
        if any(kw in msg_lower for kw in ["analyze code", "讀取程式碼", "code folder", "code資料夾", "連動模式", "改善建議", "read code"]):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以執行程式碼分析（避免洩漏系統內部指令/結構）。"

            # Extract basic params
            target = "code"
            if "magi" in msg_lower:
                target = "magi"
            
            logger.info(f"🧐 Parsing Codebase ({target})...")
            
            from skills.bridge.code_analysis import analyze_code
            # 2026-03-29: removed local import threading (use module-level import)

            platform_hint = "Discord" if str(user_id).startswith("discord_") else "LINE"

            def run_code_analysis(uid: str, platform_name: str, tgt: str, msg: str):
                try:
                    report = analyze_code(tgt, msg)
                    result_text = f"🧐 **程式碼分析報告**\n\n{report}"
                except Exception as e:
                    result_text = f"❌ 程式碼分析失敗：{e}"

                try:
                    if getattr(self, "notification_callback", None):
                        self.notification_callback(uid, result_text, platform_name)
                except Exception as notify_err:
                    logger.warning(f"Code analysis callback failed: {notify_err}")

            self._bg_task_pool.submit(run_code_analysis, str(user_id), platform_hint, target, message)

            return (
                "⏳ 已啟動程式碼分析，完成後會主動回報。\n"
                f"目標：{target}\n"
                "（此流程可能需要 1-3 分鐘，視資料夾大小而定）"
            )

        # No specific command matched. Check if auto skill genesis should trigger.
        # Trigger conditions:
        #   1. Explicit skill-related keywords (original behavior)
        #   2. EmbeddingRouter returned LOW tier but message looks actionable (new)
        _skill_genesis_kws = ["建立技能", "建立skill", "create skill", "自動化", "automate",
                              "寫一個", "寫個", "implement", "build a", "製作工具"]
        _explicit_skill_req = any(k in msg_lower for k in _skill_genesis_kws)

        _embed_low_but_actionable = False
        if not _explicit_skill_req and self._should_attempt_auto_acquire(message, msg_lower):
            try:
                from skills.bridge.embedding_router import get_router as _get_embed_router
                _er = _get_embed_router()
                _er_result = _er.route(message) if _er.is_ready else None
                if _er_result:
                    _er_skill, _er_score, _er_tier = _er_result
                    if _er_tier == "LOW" and _er_score < 0.50:
                        _embed_low_but_actionable = True
                        logger.info(f"🧬 EmbeddingRouter LOW ({_er_score:.3f}), may trigger auto-acquire")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8825, exc_info=True)

        if (
            self._should_attempt_auto_acquire(message, msg_lower)
            and not self._looks_like_capability_question(message)
            and (_explicit_skill_req or _embed_low_but_actionable)
        ):
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以啟動自主演化/自動上線技能（系統改動指令）。"
            logger.info("🧩 Skill request or embedding gap detected, starting interview-driven skill creation...")
            return self._start_skill_interview(
                str(user_id or ""),
                str(platform or ""),
                role,
                message,
                trigger_reason="gap",
            )

        # Everything else: let LLM handle it conversationally.
        logger.info("💬 No command matched, routing to LLM chat")
        return self._handle_chat_async(user_id, message, platform_hint=platform)

    def _list_skills(self):
        """
        Dynamically lists available skills by parsing SKILL.md frontmatter.
        """
        import os
        from skills.catalog import iter_top_level_skill_dirs

        skill_roots = [
            (f"{_MAGI_ROOT}/skills", "magi"),
            (os.path.join(os.path.expanduser("~"), ".openclaw", "skills"), "openclaw"),
        ]
        skills_found = []

        # Scan for all SKILL.md files
        try:
            for skills_dir, source in skill_roots:
                if not os.path.isdir(skills_dir):
                    continue
                for entry in iter_top_level_skill_dirs(skills_dir):
                    skill_path = os.path.join(entry.path, "SKILL.md")
                    try:
                        with open(skill_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        # Simple frontmatter parsing (no yaml dependency)
                        name = entry.name
                        desc = "No description"
                        if content.startswith("---"):
                            parts = content.split("---", 2)
                            if len(parts) >= 3:
                                for line in parts[1].strip().split("\n"):
                                    line = line.strip()
                                    if line.startswith("name:"):
                                        name = line.split(":", 1)[1].strip().strip("'\"")
                                    elif line.startswith("description:"):
                                        desc = line.split(":", 1)[1].strip().strip("'\"")
                        # Truncate long descriptions
                        if len(desc) > 80:
                            desc = desc[:77] + "..."
                        skills_found.append({"name": name, "desc": desc, "source": source})
                    except Exception:
                        skills_found.append({"name": entry.name, "desc": "(Unable to parse)", "source": source})
        except Exception as e:
            logger.error(f"Error scanning skills: {e}")
            return "❌ 無法讀取技能列表。"
            
        # Format Output
        response = f"🧩 **MAGI 技能列表 (Skill Matrix)**\n"
        response += f"📦 已安裝 **{len(skills_found)}** 個技能模組\n\n"
        
        # Emoji map
        emoji_map = {
            "bridge": "🌉", "memory": "🧠", "research": "🌐",
            "law-firm": "⚖️", "browser": "🖥️", "identity": "🪪",
            "evolution": "🧬", "apple": "🍎", "ops": "⚙️",
            "maintenance": "🔧", "source_control": "📂", "synology": "💾",
            "brain_manager": "🧠"
        }
        
        for skill in sorted(skills_found, key=lambda s: s["name"]):
            emoji = emoji_map.get(skill["name"], "📌")
            src = str(skill.get("source") or "magi")
            response += f"{emoji} **{skill['name']}** [{src}]\n"
            response += f"  _{skill['desc']}_\n\n"
            
        response += "💡 *您可以直接對我下達相關指令，例如「查詢行程」、「分析程式碼」等。*"
        return response

    def _handle_query(self, user_id, message, platform_hint="LINE"):
        """
        Routes queries to RAG (Keeper) or Search.
        """
        logger.info(f"🔍 Routing Query from {user_id} to Grounded AI...")
        history = self._build_conversation_history(user_id, limit=8)
        force_research = any(k in message.lower() for k in [
            "最新", "today", "news", "2026", "價格",
            "天氣", "氣溫", "weather", "上網", "查一下", "現在",
            "幫我查", "搜尋",
        ])
        timeout_sec = int(os.environ.get("MAGI_QUERY_TIMEOUT_SEC", "120") or "120")
        async_enabled = str(os.environ.get("MAGI_QUERY_ASYNC", "1")).strip().lower() in {"1", "true", "yes", "on"}
        async_trigger_chars = int(os.environ.get("MAGI_QUERY_ASYNC_TRIGGER_CHARS", "500") or "500")
        async_timeout_sec = int(os.environ.get("MAGI_QUERY_ASYNC_TIMEOUT_SEC", "900") or "900")

        mode_banner = self._brain_runtime_banner()
        if async_enabled and len(message or "") >= max(400, async_trigger_chars) and getattr(self, "notification_callback", None):
            uid = str(user_id or "")
            platform_name = str(platform_hint or "LINE")

            def _run_query_background():
                try:
                    reply = self._call_with_timeout(
                        lambda: ask_casper(message, conversation_history=history, force_research=force_research),
                        async_timeout_sec,
                        f"⚠️ 查詢逾時（>{async_timeout_sec}s），目前沒有可驗證結果。",
                        "query-async",
                    )
                    final_text = str(reply or "").strip() or "⚠️ 查詢完成，但沒有可用輸出。"
                    if "查詢逾時（>" in final_text:
                        self._ensure_runtime_foundations()
                        self._hook_bus.fallback(
                            "query-timeout",
                            stage="query_async",
                            reason=f"查詢逾時（>{async_timeout_sec}s）",
                            detail={"user_id": uid, "platform": platform_name},
                            correlation_id=self._current_correlation_id(),
                        )
                        safe_reply = "目前沒有可驗證結果，請稍後重試，或把問題縮小成更具體的一個事實點。"
                        if _mark_unverified_reply:
                            final_text = _mark_unverified_reply(
                                safe_reply,
                                reason=f"查詢逾時（>{async_timeout_sec}s）",
                            )
                        else:
                            final_text = f"⚠️ 查詢逾時（>{async_timeout_sec}s）\n{safe_reply}"
                    final_text = f"{mode_banner}\n{final_text}"
                except Exception as e:
                    self._ensure_runtime_foundations()
                    self._hook_bus.fallback(
                        "query-exception",
                        stage="query_async",
                        reason=str(e)[:200],
                        detail={"user_id": uid, "platform": platform_name},
                        correlation_id=self._current_correlation_id(),
                    )
                    final_text = f"{mode_banner}\n❌ 查詢失敗：{e}"
                try:
                    self.notification_callback(uid, final_text, platform_name)
                except Exception as notify_err:
                    logger.warning(f"Query async callback failed: {notify_err}")

            self._bg_task_pool.submit(_run_query_background)
            return f"{mode_banner}\n⏳ 內容較長，我已改成背景查詢。完成後會主動回覆結果。"

        reply = self._call_with_timeout(
            lambda: ask_casper(message, conversation_history=history, force_research=force_research),
            timeout_sec,
            f"⚠️ 查詢逾時（>{timeout_sec}s），目前沒有可驗證結果。",
            "query",
        )
        reply = str(reply or "").strip() or "⚠️ 查詢完成，但目前沒有可用輸出。"
        if "查詢逾時（>" in reply:
            self._ensure_runtime_foundations()
            self._hook_bus.fallback(
                "query-timeout",
                stage="query",
                reason=f"查詢逾時（>{timeout_sec}s）",
                detail={"user_id": str(user_id or ""), "platform": str(platform_hint or "LINE")},
                correlation_id=self._current_correlation_id(),
            )
            safe_reply = "目前沒有可驗證結果，請稍後重試，或把問題縮小成更具體的一個事實點。"
            if _mark_unverified_reply:
                reply = _mark_unverified_reply(
                    safe_reply,
                    reason=f"查詢逾時（>{timeout_sec}s）",
                )
            else:
                reply = f"⚠️ 查詢逾時（>{timeout_sec}s）\n{safe_reply}"
        return f"{mode_banner}\n{reply}"

    def _handle_chat_async(self, user_id, message, platform_hint="LINE"):
        """
        Routes chat to LLM (Casper/Ollama) for generation.
        """
        logger.info(f"💬 Chatting with {user_id}...")

        # ── Heavy task awareness: if oMLX is busy with heavy work, queue chat ──
        heavy_tasks = self.get_active_heavy_tasks()
        if heavy_tasks:
            labels = "、".join(t["label"] for t in heavy_tasks[:3])
            elapsed = max(int(time.time() - min(t["start_ts"] for t in heavy_tasks)), 0)
            logger.info(f"🏋️ Chat deferred: oMLX busy with {labels} ({elapsed}s)")
            # Queue this chat to be retried after heavy tasks complete
            _uid = str(user_id or "")
            _platform = str(platform_hint or "LINE")
            _msg = message

            def _deferred_chat():
                # Wait for heavy tasks to clear via Event (max 3 min, no polling)
                self._heavy_task_done_event.clear()
                self._heavy_task_done_event.wait(timeout=180)
                # Now run the chat
                try:
                    history = self._build_conversation_history(_uid, limit=8)
                    from skills.bridge.grounded_ai import chat_casper
                    reply = chat_casper(_msg, conversation_history=history)
                    reply = str(reply or "").strip() or "抱歉讓你久等了，但目前沒有可用輸出。"
                    banner = self._brain_runtime_banner()
                    self.notification_callback(_uid, f"{banner}\n{reply}", _platform)
                except Exception as e:
                    logger.warning(f"Deferred chat failed: {e}")
                    self.notification_callback(_uid, "⚠️ 延遲回覆失敗，請再試一次。", _platform)

            if getattr(self, "notification_callback", None):
                threading.Thread(target=_deferred_chat, daemon=True).start()
                return f"⏳ 我目前正在處理 **{labels}**（已進行 {elapsed} 秒），完成後會立刻回覆你的訊息。"
        # ─────────────────────────────────────────────────────────────────

        history = self._build_conversation_history(user_id, limit=8)
        from skills.bridge.grounded_ai import chat_casper
        timeout_sec = int(os.environ.get("MAGI_CHAT_TIMEOUT_SEC", "150") or "150")
        async_enabled = str(os.environ.get("MAGI_CHAT_ASYNC", "1")).strip().lower() in {"1", "true", "yes", "on"}
        async_trigger_chars = int(os.environ.get("MAGI_CHAT_ASYNC_TRIGGER_CHARS", "500") or "500")
        async_timeout_sec = int(os.environ.get("MAGI_CHAT_ASYNC_TIMEOUT_SEC", "900") or "900")

        mode_banner = self._brain_runtime_banner()
        if async_enabled and len(message or "") >= max(400, async_trigger_chars) and getattr(self, "notification_callback", None):
            uid = str(user_id or "")
            platform_name = str(platform_hint or "LINE")

            def _run_chat_background():
                try:
                    reply = self._call_with_timeout(
                        lambda: chat_casper(message, conversation_history=history),
                        async_timeout_sec,
                        f"⚠️ 長訊息處理逾時（>{async_timeout_sec}s）。",
                        "chat-async",
                    )
                    final_text = str(reply or "").strip() or "⚠️ 長訊息處理完成，但沒有可用輸出。"
                    final_text = f"{mode_banner}\n{final_text}"
                except Exception as e:
                    logger.warning(f"Chat async background failed: {e}")
                    final_text = f"{mode_banner}\n❌ 長訊息處理失敗，請再試一次。"
                try:
                    self.notification_callback(uid, final_text, platform_name)
                except Exception as notify_err:
                    logger.warning(f"Chat async callback failed: {notify_err}")

            threading.Thread(target=_run_chat_background, daemon=True).start()
            return f"{mode_banner}\n⏳ 問題內容較長，我已改成背景處理。完成後會主動回覆結果。"

        reply = self._call_with_timeout(
            lambda: chat_casper(message, conversation_history=history),
            timeout_sec,
            f"⚠️ 我這邊回覆逾時（>{timeout_sec}s），請再試一次，或改問「狀態」讓我先做健康檢查。",
            "chat",
        )
        reply = str(reply or "").strip() or "⚠️ 回覆完成，但目前沒有可用輸出。"
        if "回覆逾時（>" in reply:
            try:
                _gw = self._inference_gw
                quick = _gw.chat(
                    f"請用繁體中文直接回答下列訊息，簡潔但具體：\n\n{message}",
                    task_type="general",
                    timeout=max(8, min(14, timeout_sec // 5)),
                )
                qtxt = str((quick or {}).get("response") or "").strip()
                _degraded_markers = ("系統降級回覆", "本機模型逾時", "請稍後重試")
                if quick.get("success") and qtxt and not any(m in qtxt for m in _degraded_markers):
                    reply = f"⚠️ 回覆逾時，先提供快速回覆：\n{qtxt}"
                else:
                    # Both main and quick paths failed — give a clean message without degraded text
                    reply = "⚠️ 目前模型忙碌中，請稍後再試一次。"
            except Exception as quick_err:
                logger.warning(f"Chat timeout quick fallback failed: {quick_err}")
                reply = "⚠️ 目前模型忙碌中，請稍後再試一次。"
        return f"{mode_banner}\n{reply}"

# Simple CLI Test for the Orchestrator
# ── Registered Commands (migrated from _handle_command) ────────────────
# New commands should be registered here using @_cmd_registry.command(...)
# instead of adding to the legacy if-elif chain.

@_cmd_registry.command(
    name="brain_status",
    keywords=["大腦狀態", "brain status", "大腦模式", "目前模式", "模型為何", "模型是什麼"],
    priority=50,
)
def _cmd_brain_status(ctx: CommandContext) -> str | None:
    # Only trigger if it's a status query, not a switch command
    if any(kw in ctx.msg_lower for kw in ["switch", "切換", "切回", "修理", "修復", "校準", "activate"]):
        return None
    orch = ctx.orchestrator
    try:
        status = get_brain_status()
        mode = str(status.get("mode") or "local")
        model = str(status.get("primary_model") or "unknown")
        return f"🧠 目前大腦模式：`{mode}`\n模型：`{model}`"
    except Exception as e:
        return f"⚠️ 無法取得大腦狀態：{e}"


def _handle_tier_command(message: str):
    """攔截模型切換指令（不限管理員）。回傳 str 或 None。"""
    import re as _re
    msg = (message or "").strip()
    msg_lower = msg.lower()

    if _re.search(r"(?:切換|switch\s*(?:to\s*)?)(?:26[bB]|重型)", msg):
        from skills.bridge.tier_router import set_mode
        return set_mode("26b")

    if _re.search(r"(?:切換|switch\s*(?:to\s*)?)(?:[eE]4[bB]|輕型)", msg):
        from skills.bridge.tier_router import set_mode
        return set_mode("e4b")

    if _re.search(r"自動模式|auto\s*mode", msg_lower):
        from skills.bridge.tier_router import set_mode
        return set_mode("auto")

    m = _re.search(r"/model\s+(26b|e4b|auto|status)", msg_lower)
    if m:
        from skills.bridge.tier_router import set_mode, format_status
        arg = m.group(1)
        if arg == "status":
            return format_status()
        return set_mode(arg)

    if _re.search(r"模型狀態|model\s*status|推理狀態", msg_lower):
        from skills.bridge.tier_router import format_status
        return format_status()

    return None


@_cmd_registry.command(
    name="zombie_patrol",
    keywords=["殭屍巡邏", "zombie patrol", "巡邏殭屍", "殭屍清除", "zombie clean"],
    admin_only=True,
    priority=60,
)
def _cmd_zombie_patrol(ctx: CommandContext) -> str | None:
    try:
        from daemon import reap_orphan_workers, get_reap_report
        dry = "模擬" in (ctx.message or "") or "dry" in (ctx.message or "").lower()
        reap_orphan_workers(force=True, dry_run=dry)
        report = get_reap_report()
        return f"🧟 殭屍巡邏結果：\n{report or '巡邏完成（無殭屍）'}"
    except Exception as e:
        return f"❌ 殭屍巡邏失敗：{e}"


if __name__ == "__main__":
    conductor = Orchestrator()
    
    print("\n--- CASPER ORCHESTRATOR CLI ---")
    while True:
        try:
            user_input = input("You > ")
            if user_input.lower() in ["exit", "quit"]:
                break
                
            response = conductor.process_message("CLI_USER", user_input, "CLI")
            if response:
                print(f"Casper > {response}")
            else:
                print("Casper > (Thinking/Chatting...)")
                
        except KeyboardInterrupt:
            print("\nExiting...")
            break
