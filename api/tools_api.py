#!/usr/bin/env python3
"""
MAGI TOOLS API (三哲人系統)
===========================
HTTP API that exposes MAGI tools for external callers (OpenClaw, etc.)
Run on port 5003 to avoid conflicts with main server (5002).

三哲人 (Three Sages):
  - CASPER: Decision & Orchestration (本API)
  - MELCHIOR: Vision & Code (GPU)
  - BALTHASAR: Summarization (Apple Intelligence)

SECURITY (P1 Baseline v2 - 10/10 Security):
  - Unified Authorization Module (api/authz.py)
    * @require_api_key - For API key protected endpoints
    * @require_role("admin"|"operator"|"viewer") - For role-based endpoints
    * Audit logging of all access attempts
  - CSRF Protection (api/csrf_guard.py)
    * Double-submit cookie pattern
    * Exempts: webhook endpoints, API key-authenticated endpoints
  - API Contract (docs/API_CONTRACT.md)
    * Full OpenAPI-style documentation
    * Auth requirements per endpoint

Endpoints:
  POST /search {"query": "AI 2024"}
  POST /research {"topic": "Python best practices"}
  POST /fetch {"url": "https://example.com"}
  POST /vision {"image_path": "/path/to/image.jpg", "prompt": "Describe this"}
  POST /summarize {"text": "Long text..."}
  GET /sages - 三哲人狀態
  GET /skills
"""

import sys
import os
import json
import hmac
import logging
import subprocess
import time
import threading
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from flask import Flask, request, jsonify, send_from_directory, Response

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_STARTUP_TS = time.time()
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)
from api.model_config import TEXT_PRIMARY_MODEL

# Auto-reap zombie children (skill subprocesses, etc.)
import signal as _signal
def _sigchld_handler(_signum, _frame):
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except ChildProcessError:
            break
_signal.signal(_signal.SIGCHLD, _sigchld_handler)

from api.hooks import HookBus
from api.permissions import (
    PermissionEnforcer,
    PermissionMode,
    PermissionPolicy,
    deny_command,
    deny_path,
)
from api.runtime_paths import get_metrics_dir, get_orch_dir


def _to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def _ensure_python_package(import_name: str, pip_name: str) -> bool:
    try:
        __import__(import_name)
        return True
    except ModuleNotFoundError:
        pass
    except Exception:
        return False

    base = [
        sys.executable or "python3",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        pip_name,
    ]
    attempts = [
        base,
        base + ["--break-system-packages"],
        base + ["--user"],
    ]
    for cmd in attempts:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if result.returncode == 0:
                __import__(import_name)
                return True
            last_err = (result.stderr or "").strip()[:300]
            logging.getLogger("tools_api").warning("auto-install attempt failed for %s: %s", pip_name, last_err)
        except Exception as e:
            logging.getLogger("tools_api").warning("auto-install exception for %s: %s", pip_name, e)
    return False


if _ensure_python_package("flask_cors", "flask-cors"):
    from flask_cors import CORS
else:
    def CORS(_app, *args, **kwargs):
        return _app

# Stability-first default: avoid distributed inference unless explicitly enabled.
os.environ.setdefault("MAGI_AVOID_DISTRIBUTED", "1")
from api.mysql_connector_guard import patch_mysql_connector_for_stability

# DB connector stability guard:
# default to pure-python mysql-connector path to avoid C-extension segfaults under threaded load.
os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
if patch_mysql_connector_for_stability():
    logging.getLogger("tools_api").info("mysql connector guard enabled (MAGI_MYSQL_USE_PURE=%s)", os.environ.get("MAGI_MYSQL_USE_PURE", "1"))

from skills.research.web_research import search_web, research_topic, fetch_url_content
from skills.evolution.skill_genesis import list_skills, generate_skill
from skills.bridge.melchior_bridge import analyze_image
from skills.bridge.inference_gateway import InferenceGateway
from skills.bridge.balthasar_bridge import summarize_text, check_health as balthasar_health
from skills.bridge.melchior_manager import sync_skills_to_melchior, melchior_health
try:
    from api.tw_output_guard import normalize_output_text as _normalize_output_text
except Exception:
    _normalize_output_text = None

app = Flask(__name__)
from api.thread_pools import io_pool as _INLINE_EXECUTOR
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
_INFERENCE_EXECUTOR = _ThreadPoolExecutor(
    max_workers=int(os.environ.get("MAGI_INFERENCE_POOL_SIZE", "4")),
    thread_name_prefix="inference",
)

_SKILLS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills"))

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 151, exc_info=True)
_DEFAULT_SKILL_RE = re.compile(r"default:\s*([A-Za-z0-9._-]+)")
_TOOL_ENDPOINT_SKILL_DEPENDENCIES = {
    # Uses run_skill_action("code-laf_automation_v2", ...)
    "/laf/smoke_login": "code-laf_automation_v2",
}

# Serve /static/exports/* so TXT/captcha artifacts are reachable even when a reverse proxy points to Tools API.
EXPORTS_DIR = os.environ.get(
    "MAGI_EXPORTS_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static", "exports")),
)
SUMMARY_METRICS_PATH = os.environ.get(
    "MAGI_SUMMARY_METRICS_PATH",
    str(get_metrics_dir() / "summarize_requests.jsonl"),
)
TRANSCRIBE_METRICS_PATH = os.environ.get(
    "MAGI_TRANSCRIBE_METRICS_PATH",
    str(get_metrics_dir() / "transcribe_requests.jsonl"),
)
EXTERNAL_CHAT_METRICS_PATH = os.path.join(_MAGI_ROOT, "static", "external_chat_metrics.jsonl")

# P0-13: CORS 改為 allowlist，不再允許所有來源。
# 預設僅允許 localhost 開發用來源，正式環境請透過 MAGI_CORS_ORIGINS 設定。
_cors_raw = os.environ.get("MAGI_CORS_ORIGINS", "http://localhost:3000,http://localhost:5002,http://127.0.0.1:3000,http://127.0.0.1:5002")
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
CORS(app, origins=_cors_origins, supports_credentials=True)

# ── Unified Authorization & CSRF Protection (Security Baseline Upgrade) ──
try:
    from api.authz import require_role, require_api_key, is_admin
    from api.csrf_guard import csrf_exempt, middleware_apply_csrf
    middleware_apply_csrf(app)
except ImportError as e:
    logging.getLogger("tools_api").warning(f"Could not load security modules: {e}")
    # Fallback: no-op decorators
    def require_role(role):
        def dec(f):
            return f
        return dec
    def require_api_key(f):
        return f
    def csrf_exempt(f):
        return f
    def is_admin(user=None):
        return False

# static_exports route — must be defined AFTER require_api_key is available
@app.route("/static/exports/<path:filename>", methods=["GET"])
@require_api_key
def static_exports(filename: str):
    # No directory listing; only direct file fetch.  Auth required (P0 security).
    return send_from_directory(EXPORTS_DIR, filename, as_attachment=False)

# P0-13: 補安全標頭 middleware
@app.after_request
def _add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def _discover_runnable_skill_dirs() -> set[str]:
    dirs: set[str] = set()
    try:
        for entry in os.scandir(_SKILLS_ROOT):
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(".") or name == "__pycache__":
                continue
            if os.path.exists(os.path.join(entry.path, "action.py")):
                dirs.add(name)
    except Exception:
        return set()
    return dirs


def _extract_default_skill_from_tool(tool: dict) -> str:
    try:
        params = tool.get("parameters") or {}
        props = params.get("properties") or {}
        skill_prop = props.get("skill") or {}
        default_value = str(skill_prop.get("default") or "").strip()
        if default_value:
            return default_value
        desc = str(skill_prop.get("description") or "")
        m = _DEFAULT_SKILL_RE.search(desc)
        return (m.group(1).strip() if m else "")
    except Exception:
        return ""


def _infer_skill_from_run_tool_name(tool_name: str, available_dirs: set[str]) -> str:
    if not (isinstance(tool_name, str) and tool_name.startswith("run_")):
        return ""
    raw = tool_name[4:]
    candidates = [
        raw,
        raw.replace("_", "-"),
        raw.replace("-", "_"),
    ]
    for c in candidates:
        if c in available_dirs:
            return c
    return ""


def _sanitize_definitions_payload(payload: dict) -> dict:
    """
    Runtime hardening for /definitions:
    - expose only runnable /skills/run tools
    - pin each runnable tool's `skill` parameter to an existing folder
    """
    if not isinstance(payload, dict):
        return payload
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return payload

    available_dirs = _discover_runnable_skill_dirs()
    filtered_tools = []
    dropped_run_tools = 0

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        endpoint = str(tool.get("endpoint") or "").strip()
        name = str(tool.get("name") or "").strip()

        dep_skill = _TOOL_ENDPOINT_SKILL_DEPENDENCIES.get(endpoint)
        if dep_skill and dep_skill not in available_dirs:
            dropped_run_tools += 1
            continue

        if endpoint != "/skills/run":
            filtered_tools.append(tool)
            continue

        default_skill = _extract_default_skill_from_tool(tool)
        if not default_skill:
            default_skill = _infer_skill_from_run_tool_name(name, available_dirs)
        if (not default_skill) or (default_skill not in available_dirs):
            dropped_run_tools += 1
            continue

        t = dict(tool)
        params = dict(t.get("parameters") or {})
        props = dict(params.get("properties") or {})
        skill_prop = dict(props.get("skill") or {})

        skill_desc = str(skill_prop.get("description") or "").strip()
        if not skill_desc:
            skill_desc = f"Skill folder name (default: {default_skill})"
        skill_prop.update(
            {
                "type": "string",
                "default": default_skill,
                "enum": [default_skill],
                "description": skill_desc,
            }
        )
        props["skill"] = skill_prop

        params["type"] = "object"
        params["properties"] = props
        required = params.get("required")
        if not isinstance(required, list):
            required = []
        if "task" not in required:
            required.append("task")
        if "skill" not in required:
            required.append("skill")
        params["required"] = required
        t["parameters"] = params
        filtered_tools.append(t)

    out = dict(payload)
    out["tools"] = filtered_tools
    meta = dict(out.get("_meta") or {})
    meta["runtime_filter"] = {
        "available_skill_dirs": len(available_dirs),
        "tools_total": len(tools),
        "tools_exposed": len(filtered_tools),
        "dropped_unrunnable_run_tools": dropped_run_tools,
    }
    out["_meta"] = meta
    return out


def _append_jsonl(path: str, row: dict) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        # Size-based rotation (10 MB, keep 5)
        try:
            from api.events.sinks import rotate_jsonl
            rotate_jsonl(p)
        except Exception:
            pass
    except Exception:
        return


def _record_summarize_metric(
    started_at_mono: float,
    *,
    success: bool,
    timeout: bool,
    upstream_timeout: bool,
    engine: str,
    route: str,
    degraded: bool,
    apple_tried: bool,
    error: str = "",
) -> None:
    elapsed_ms = int(max(0.0, (time.monotonic() - float(started_at_mono))) * 1000)
    _append_jsonl(
        SUMMARY_METRICS_PATH,
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "latency_ms": elapsed_ms,
            "success": bool(success),
            "timeout": bool(timeout),
            "upstream_timeout": bool(upstream_timeout),
            "engine": str(engine or ""),
            "route": str(route or ""),
            "degraded": bool(degraded),
            "apple_tried": bool(apple_tried),
            "error": str(error or "")[:300],
        },
    )


def _record_transcribe_metric(
    started_at_mono: float,
    *,
    success: bool,
    speaker_count_estimate: int,
    audio_path: str,
    provider: str,
    error: str = "",
) -> None:
    elapsed_ms = int(max(0.0, (time.monotonic() - float(started_at_mono))) * 1000)
    _append_jsonl(
        TRANSCRIBE_METRICS_PATH,
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "latency_ms": elapsed_ms,
            "success": bool(success),
            "speaker_count_estimate": int(max(0, int(speaker_count_estimate or 0))),
            "audio_path": str(audio_path or ""),
            "provider": str(provider or ""),
            "error": str(error or "")[:300],
        },
    )


def _record_external_chat_metric(
    duration_sec,  # type: float
    success,  # type: bool
    degraded,  # type: bool
    cold_start,  # type: bool
    tier,  # type: str
    effective_timeout,  # type: int
):  # type: (...) -> None
    """Append one metric line and keep the jsonl capped at 500 lines."""
    _append_jsonl(
        EXTERNAL_CHAT_METRICS_PATH,
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "success": bool(success),
            "degraded": bool(degraded),
            "cold_start": bool(cold_start),
            "duration_sec": round(float(duration_sec), 2),
            "tier": str(tier or "COMPLEX"),
            "effective_timeout": int(effective_timeout),
        },
    )
    # Truncate to max 500 lines
    try:
        p = Path(EXTERNAL_CHAT_METRICS_PATH)
        if p.exists():
            lines = p.read_text(encoding="utf-8").splitlines()
            if len(lines) > 500:
                p.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def _guard_text(s: str, platform: str = "OPENCLAW") -> str:
    text = str(s or "")
    if not text:
        return text
    try:
        if _normalize_output_text:
            return _normalize_output_text(text, platform=platform)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 415, exc_info=True)
    return text


def _guard_payload_fields(payload):
    """
    Guard only human-facing text fields to avoid altering IDs/URLs.
    """
    text_keys = {"reply", "response", "text", "translation", "summary", "message"}
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if isinstance(v, str) and str(k).lower() in text_keys:
                out[k] = _guard_text(v, platform="OPENCLAW")
            elif isinstance(v, dict):
                out[k] = _guard_payload_fields(v)
            elif isinstance(v, list):
                out[k] = [_guard_payload_fields(x) if isinstance(x, (dict, list)) else x for x in v]
            else:
                out[k] = v
        return out
    if isinstance(payload, list):
        return [_guard_payload_fields(x) if isinstance(x, (dict, list)) else x for x in payload]
    return payload


def _infer_external_platform(raw: str, user_id: str = "") -> str:
    p = (raw or "").strip().upper()
    if p:
        return p
    uid = (user_id or "").strip()
    if uid.isdigit() and len(uid) >= 6:
        # Telegram user IDs are numeric; fallback to TELEGRAM so output guard rules apply.
        return "TELEGRAM"
    return "OPENCLAW"


def _looks_long_task(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    if len(text) >= int(os.environ.get("MAGI_EXTERNAL_CHAT_ASYNC_TRIGGER_CHARS", "700") or "700"):
        return True
    heavy_keywords = [
        "翻譯", "完整翻譯", "不要摘要", "摘要", "總結", "整理",
        "分析", "爬蟲", "法扶", "閱卷", "筆錄", "回報", "開辦", "結案",
        "生成圖片", "音訊", "逐字稿", "長文",
    ]
    low = text.lower()
    return any(k in text or k in low for k in heavy_keywords)


def _openclaw_cfg() -> dict:
    try:
        p = os.path.expanduser("~/.openclaw/openclaw.json")
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _send_telegram_direct(chat_id: str, text: str) -> bool:
    """
    Best-effort direct Telegram push for async completion notices.
    """
    try:
        from skills.bridge.http_pool import get_session as _get_session
    except Exception:
        return False
    cfg = _openclaw_cfg()
    token = str(((cfg.get("channels") or {}).get("telegram") or {}).get("botToken") or "").strip()
    if not token or not str(chat_id or "").strip():
        return False
    try:
        resp = _get_session().post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": str(chat_id), "text": str(text or "")[:3900]},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _normalize_external_result_text(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return "⚠️ 任務完成，但沒有可用輸出。"
    if "|||FILE_PATH|||" in s:
        try:
            msg, path = s.split("|||FILE_PATH|||", 1)
            msg = (msg or "").strip()
            path = (path or "").strip()
            return f"{msg}\n📎 檔案已產生：{path}".strip()
        except Exception:
            return s
    if "|||IMAGE_PATH|||" in s:
        try:
            msg, path = s.split("|||IMAGE_PATH|||", 1)
            msg = (msg or "").strip()
            path = (path or "").strip()
            return f"{msg}\n🖼️ 圖片已產生：{path}".strip()
        except Exception:
            return s
    return s


def _run_with_timeout(fn, wait_sec: int, *args, pool=None, **kwargs):
    _pool = pool or _INLINE_EXECUTOR
    future = _pool.submit(fn, *args, **kwargs)
    try:
        return True, future.result(timeout=max(1, int(wait_sec)))
    except FutureTimeoutError:
        # NOTE: future.cancel() is a no-op for already-running tasks in ThreadPoolExecutor.
        # The running function will continue until it finishes naturally.  This is acceptable
        # because the inference pool (_INLINE_EXECUTOR) has a bounded worker count (4), which
        # caps the maximum number of leaked in-flight tasks.  A cooperative cancellation via
        # threading.Event would require changes to every callable passed here and is not
        # worth the complexity.
        future.cancel()
        return False, {
            "success": False,
            "error": f"timeout_exceeded_{int(wait_sec)}s",
            "error_type": "timeout",
        }
    except Exception as e:
        return False, {
            "success": False,
            "error": str(e),
            "error_type": "exception",
        }


_OSC_ORCHESTRATOR = None
_INFERENCE_GATEWAY = InferenceGateway()
_EXTERNAL_KEY_CACHE = {"ts": 0.0, "value": ""}
_SUMMARIZE_CB_LOCK = threading.Lock()
_SUMMARIZE_CB = {
    "consecutive_upstream_timeout": 0,
    "open_until": 0.0,
    "last_error": "",
    "last_timeout_ts": 0.0,
}


def _get_osc_orchestrator():
    global _OSC_ORCHESTRATOR
    if _OSC_ORCHESTRATOR is None:
        from api.orchestrator import Orchestrator  # Lazy import: reduce startup coupling
        _OSC_ORCHESTRATOR = Orchestrator()
    return _OSC_ORCHESTRATOR


def _summarize_cb_enabled() -> bool:
    return _to_bool(os.environ.get("MAGI_SUMMARIZE_CB_ENABLE", "1"), True)


def _summarize_cb_snapshot() -> dict:
    now = time.time()
    with _SUMMARIZE_CB_LOCK:
        open_until = float(_SUMMARIZE_CB.get("open_until", 0.0) or 0.0)
        remain = max(0.0, open_until - now)
        return {
            "enabled": _summarize_cb_enabled(),
            "consecutive_upstream_timeout": int(_SUMMARIZE_CB.get("consecutive_upstream_timeout", 0) or 0),
            "open": bool(open_until > now),
            "open_until_epoch": open_until,
            "open_remaining_sec": int(remain),
            "last_error": str(_SUMMARIZE_CB.get("last_error", "") or ""),
            "last_timeout_ts": float(_SUMMARIZE_CB.get("last_timeout_ts", 0.0) or 0.0),
        }


def _summarize_cb_allow_upstream() -> bool:
    if not _summarize_cb_enabled():
        return True
    now = time.time()
    with _SUMMARIZE_CB_LOCK:
        open_until = float(_SUMMARIZE_CB.get("open_until", 0.0) or 0.0)
        return not (open_until > now)


def _summarize_cb_note_success() -> None:
    with _SUMMARIZE_CB_LOCK:
        _SUMMARIZE_CB["consecutive_upstream_timeout"] = 0
        _SUMMARIZE_CB["open_until"] = 0.0
        _SUMMARIZE_CB["last_error"] = ""


def _summarize_cb_note_upstream_timeout(error: str) -> None:
    if not _summarize_cb_enabled():
        return
    try:
        threshold = int(os.environ.get("MAGI_SUMMARIZE_CB_TIMEOUTS_TO_OPEN", "2") or "2")
    except Exception:
        threshold = 2
    try:
        cooldown_sec = int(os.environ.get("MAGI_SUMMARIZE_CB_COOLDOWN_SEC", "300") or "300")
    except Exception:
        cooldown_sec = 300
    threshold = max(1, threshold)
    cooldown_sec = max(20, cooldown_sec)
    now = time.time()
    with _SUMMARIZE_CB_LOCK:
        cnt = int(_SUMMARIZE_CB.get("consecutive_upstream_timeout", 0) or 0) + 1
        _SUMMARIZE_CB["consecutive_upstream_timeout"] = cnt
        _SUMMARIZE_CB["last_error"] = str(error or "")[:300]
        _SUMMARIZE_CB["last_timeout_ts"] = now
        if cnt >= threshold:
            _SUMMARIZE_CB["open_until"] = now + float(cooldown_sec)


def _read_openclaw_gateway_password() -> str:
    try:
        p = os.path.expanduser("~/.openclaw/openclaw.json")
        if not os.path.exists(p):
            return ""
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
        return str((((cfg.get("gateway") or {}).get("auth") or {}).get("password") or "").strip())
    except Exception:
        return ""


def _resolve_external_api_key() -> str:
    now = time.time()
    if now - float(_EXTERNAL_KEY_CACHE.get("ts", 0.0) or 0.0) < 30:
        return str(_EXTERNAL_KEY_CACHE.get("value", "") or "")
    key = (
        os.environ.get("MAGI_EXTERNAL_API_KEY")
        or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        or _read_openclaw_gateway_password()
        or ""
    ).strip()
    _EXTERNAL_KEY_CACHE["ts"] = now
    _EXTERNAL_KEY_CACHE["value"] = key
    return key


def _check_external_api_key():
    """
    API-key gate for external OSC/CASPER routes.
    Default policy: required.
    If MAGI_EXTERNAL_API_KEY is set, clients must provide one of:
    - Header: X-API-Key
    - Header: Authorization: Bearer <key>
    You can temporarily disable enforcement by setting:
    - MAGI_EXTERNAL_API_KEY_REQUIRED=0
    """
    expected = _resolve_external_api_key()
    required = (os.environ.get("MAGI_EXTERNAL_API_KEY_REQUIRED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
    if required and not expected:
        return False, "server_misconfigured: MAGI_EXTERNAL_API_KEY is required but not set"
    if (not required) and (not expected):
        return True, ""
    provided = (request.headers.get("X-API-Key") or "").strip()
    auth = (request.headers.get("Authorization") or "").strip()
    if not provided and auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    if provided and hmac.compare_digest(provided, expected):
        return True, ""
    return False, "unauthorized: invalid api key"


def _default_tools_permission_rules() -> list:
    root_dir = str(_MAGI_ROOT)
    agent_dir = str(get_orch_dir())
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
            reason="agent runtime state is not a valid tool target",
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


_TOOLS_EVENTS_PATH = str(get_orch_dir() / "tools_runtime_events.jsonl")
_TOOLS_HOOK_BUS = HookBus(source="magi.tools_api")
_TOOLS_HOOK_BUS.add_jsonl_sink(_TOOLS_EVENTS_PATH)
_TOOLS_PERMISSION_ENFORCER = PermissionEnforcer(
    policy=PermissionPolicy.from_rules(
        _default_tools_permission_rules(),
        mode=PermissionMode.PERMISSIVE,
    )
)


def _tool_correlation_id() -> str:
    return (
        (request.headers.get("X-Request-ID") or "").strip()
        or (request.headers.get("X-Correlation-ID") or "").strip()
    )


def _tool_preview(payload, limit: int = 240):
    text = ""
    try:
        if isinstance(payload, dict):
            text = json.dumps(payload, ensure_ascii=False, default=str)
        else:
            text = str(payload)
    except Exception:
        text = str(payload)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _resolve_skill_action_path(skill_name: str) -> str:
    candidates = [
        str(skill_name or "").strip(),
        str(skill_name or "").replace("_", "-").strip(),
        str(skill_name or "").replace("-", "_").strip(),
        re.sub(r"^run[_-]+", "", str(skill_name or "").replace("_", "-")).strip(),
        re.sub(r"^run[_-]+", "", str(skill_name or "")).strip(),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        action_path = os.path.join(_SKILLS_ROOT, candidate, "action.py")
        if os.path.exists(action_path):
            return action_path
    return ""


def _check_tool_access(tool_name: str, *, command_subject: str = "", path_subject: str = ""):
    command_target = command_subject or f"tool:{tool_name}"
    command_decision = _TOOLS_PERMISSION_ENFORCER.evaluate_command(command_target)
    if not command_decision.allowed:
        return False, command_decision
    if path_subject:
        path_decision = _TOOLS_PERMISSION_ENFORCER.evaluate_path(path_subject)
        if not path_decision.allowed:
            return False, path_decision
    return True, None


def _start_tool_event(tool_name: str, input_data=None, metadata: Optional[dict] = None) -> float:
    _TOOLS_HOOK_BUS.pre_tool(
        tool_name,
        input_data=dict(input_data or {}),
        user_id=str(request.headers.get("X-User-ID") or ""),
        platform=_infer_external_platform(request.headers.get("X-Platform"), user_id=request.headers.get("X-User-ID", "")),
        correlation_id=_tool_correlation_id(),
        metadata=dict(metadata or {}),
    )
    return time.perf_counter()


def _finish_tool_event(
    tool_name: str,
    started_at: float,
    *,
    ok: bool,
    status: str,
    output_data=None,
    error: str = "",
    metadata: Optional[dict] = None,
) -> None:
    _TOOLS_HOOK_BUS.post_tool(
        tool_name,
        output_data=output_data,
        ok=ok,
        status=status,
        duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
        error=str(error or ""),
        correlation_id=_tool_correlation_id(),
        metadata=dict(metadata or {}),
    )


def _tool_denied_response(tool_name: str, started_at: float, decision, metadata: Optional[dict] = None):
    message = f"permission_denied: {decision.reason}"
    _finish_tool_event(
        tool_name,
        started_at,
        ok=False,
        status="denied",
        error=message,
        metadata=metadata,
    )
    return jsonify({"error": message}), 403


def _tool_exception_response(
    tool_name: str,
    started_at: float,
    error,
    *,
    metadata: Optional[dict] = None,
    status_code: int = 500,
):
    message = str(error or "internal_error")
    _finish_tool_event(
        tool_name,
        started_at,
        ok=False,
        status="error",
        error=message,
        metadata=metadata,
    )
    return jsonify({"error": message}), status_code

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "MAGI Tools API (三哲人)"})


@app.route('/osc/external/health', methods=['GET'])
def external_osc_health():
    ok, err = _check_external_api_key()
    if not ok:
        code = 503 if "server_misconfigured" in err else 401
        return jsonify({"success": False, "error": err}), code
    key_present = bool(
        _resolve_external_api_key()
    )
    return jsonify(
        {
            "success": True,
            "service": "OSC/CASPER external gateway",
            "orchestrator_ready": True,
            "api_key_required": key_present,
        }
    )


@app.route('/osc/external/chat', methods=['POST'])
def external_osc_chat():
    ok, err = _check_external_api_key()
    if not ok:
        code = 503 if "server_misconfigured" in err else 401
        return jsonify({"success": False, "error": err}), code
    data = request.get_json() or {}
    message = (data.get("message") or data.get("prompt") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "Missing 'message'"}), 400
    user_id = str(data.get("user_id") or "external_api_user")
    platform = _infer_external_platform(str(data.get("platform") or ""), user_id=user_id)
    role_raw = str(data.get("role") or "user").strip().lower()
    role = "admin" if role_raw == "admin" else "user"

    # Defense in depth: auto-elevate only when sender is explicitly allowlisted.
    # This keeps Telegram/LINE/Discord webhook role handling resilient even if
    # upstream payload forgets to pass role=admin.
    try:
        orch_probe = _get_osc_orchestrator()
        if orch_probe._is_verified_admin_sender(user_id=user_id, platform=platform):  # type: ignore[attr-defined]
            role = "admin"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 713, exc_info=True)

    msg_low = message.lower()
    quick_model_or_mode = any(
        k in msg_low
        for k in [
            "你現在使用模型", "現在使用模型", "目前模型", "模型為何", "模型是什麼", "what model",
            "目前模式", "現在模式", "推理模式", "大腦模式", "分散式推理",
        ]
    )
    if quick_model_or_mode:
        target_main = (os.environ.get("MAGI_MAIN_MODEL") or TEXT_PRIMARY_MODEL).strip() or TEXT_PRIMARY_MODEL
        target_sub = (os.environ.get("CASPER_LOCAL_MODEL") or TEXT_PRIMARY_MODEL).strip() or TEXT_PRIMARY_MODEL
        active = target_main
        mode = "unknown"
        try:
            from skills.brain_manager.action import get_brain_mode
            mode = str(get_brain_mode() or "unknown").strip().lower()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 732, exc_info=True)
        try:
            from skills.bridge.melchior_manager import get_melchior_runtime_status
            rt = get_melchior_runtime_status() or {}
            models = rt.get("models") if isinstance(rt.get("models"), list) else []
            if models:
                active = str(models[0]).strip() or active
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 740, exc_info=True)
        mode_note = "本地 oMLX 推理"
        quick = (
            f"目標主模型：{target_main}\n"
            f"目標副模型：{target_sub}\n"
            f"目前執行模型：{active}\n"
            f"目前模式：{mode_note}"
        )
        return jsonify({"success": True, "reply": _guard_text(quick, platform=platform)}), 200

    requested_timeout_sec = int(data.get("timeout_sec") or os.environ.get("MAGI_EXTERNAL_CHAT_TIMEOUT_SEC", "20"))

    # Stability-first default: external authenticated chat keeps the proven timeout
    # floor unless a future optimization is explicitly opted in.
    try:
        from skills.bridge.grounded_ai import _classify_query_tier
        msg_tier = _classify_query_tier(message)
    except Exception:
        msg_tier = "COMPLEX"

    simple_timeout_opt_in = _to_bool(os.environ.get("MAGI_EXTERNAL_CHAT_SIMPLE_TIMEOUT_OPT_IN", "0"), False)
    if simple_timeout_opt_in and msg_tier == "SIMPLE":
        min_timeout_sec = max(
            int(os.environ.get("MAGI_EXTERNAL_CHAT_SIMPLE_MIN_TIMEOUT_SEC", "45") or "45"),
            10,
        )
    else:
        min_timeout_sec = max(
            int(os.environ.get("MAGI_CHAT_TIMEOUT_SEC", "150") or "150"),
            int(os.environ.get("MAGI_EXTERNAL_CHAT_MIN_TIMEOUT_SEC", "240") or "240"),
        )

    timeout_sec = max(10, min(300, max(requested_timeout_sec, min_timeout_sec)))

    # Expose cold_start / timeout via request context for downstream (Trace metrics later in C)
    from flask import g
    g.chat_tier = msg_tier
    g.timeout_floor_applied = min_timeout_sec
    g.effective_timeout_sec = timeout_sec

    # ── SSE streaming path ──────────────────────────────────────────
    if data.get("stream"):
        def _sse_generate():
            try:
                orch = _get_osc_orchestrator()
                # Pre-processing: intent classification + recall (non-streaming part)
                # Then stream the LLM tokens via melchior_client.chat_stream()
                from skills.bridge.grounded_ai import _generate_local
                gen = _generate_local(message, timeout=timeout_sec, stream=True)
                for chunk in gen:
                    escaped = json.dumps({"choices": [{"delta": {"content": chunk}}]})
                    yield "data: {}\n\n".format(escaped)
                yield "data: [DONE]\n\n"
            except Exception as exc:
                err_payload = json.dumps({"error": str(exc)})
                yield "data: {}\n\n".format(err_payload)
                yield "data: [DONE]\n\n"
        return Response(_sse_generate(), mimetype="text/event-stream")

    async_enabled = _to_bool(data.get("async"), _to_bool(os.environ.get("MAGI_EXTERNAL_CHAT_ASYNC", "1"), True))

    if async_enabled and _looks_long_task(message):
        task_id = f"ext_{int(time.time())}_{abs(hash(f'{user_id}:{message[:80]}')) % 100000}"

        def _run_background():
            try:
                orch = _get_osc_orchestrator()
                result = orch.process_message(user_id=user_id, message=message, platform=platform, role=role)
                final_text = _guard_text(_normalize_external_result_text(str(result or "")), platform=platform)
            except Exception as e:
                final_text = _guard_text(f"❌ 任務執行失敗：{e}", platform=platform)
            if platform == "TELEGRAM" and str(user_id or "").isdigit():
                _send_telegram_direct(str(user_id), final_text)

        threading.Thread(target=_run_background, daemon=True).start()
        ack = (
            "⏳ 已收到長任務，已改為背景執行。"
            "完成後我會主動回覆結果。"
        )
        return jsonify(
            {
                "success": True,
                "queued": True,
                "task_id": task_id,
                "reply": _guard_text(ack, platform=platform),
                "meta": {
                    "tier": msg_tier,
                    "timeout_floor": min_timeout_sec,
                    "effective_timeout": timeout_sec,
                    "duration_sec": 0,
                    "is_fallback": False,
                    "queued": True
                }
            }
        ), 200

    def _process_chat():
        orch = _get_osc_orchestrator()
        return orch.process_message(user_id=user_id, message=message, platform=platform, role=role)
        
    start_time = time.time()
    ok_run, result = _run_with_timeout(_process_chat, timeout_sec)
    duration = round(time.time() - start_time, 2)
    _cold_start = (time.time() - _STARTUP_TS) < 120

    from flask import g
    meta = {
        "tier": getattr(g, "chat_tier", "COMPLEX"),
        "timeout_floor": getattr(g, "timeout_floor_applied", timeout_sec),
        "effective_timeout": getattr(g, "effective_timeout_sec", timeout_sec),
        "duration_sec": duration,
        "is_fallback": not ok_run,
        "cold_start": _cold_start,
    }

    if ok_run:
        _record_external_chat_metric(
            duration_sec=duration, success=True, degraded=False,
            cold_start=_cold_start, tier=msg_tier, effective_timeout=timeout_sec,
        )
        return jsonify({"success": True, "reply": _guard_text(str(result or ""), platform=platform), "meta": meta})

    fallback = (
        "目前系統較忙，已啟用降級回覆。"
        "請稍後重試，或改用較短問題分段詢問。"
    )
    _record_external_chat_metric(
        duration_sec=duration, success=False, degraded=True,
        cold_start=_cold_start, tier=msg_tier, effective_timeout=timeout_sec,
    )
    return jsonify(
        {
            "success": False,
            "degraded": True,
            "error": (result or {}).get("error", "timeout") if isinstance(result, dict) else "timeout",
            "reply": _guard_text(fallback, platform=platform),
            "meta": meta
        }
    ), 200


@app.route('/osc/external/ui', methods=['GET'])
def external_osc_ui():
    """
    Minimal web chat UI for OSC external interface.
    Requires API key and uses /osc/external/chat.
    """
    html = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CASPER OSC 外部對話介面</title>
  <style>
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:920px;margin:20px auto;padding:0 14px;}
    .row{display:flex;gap:8px;align-items:center;margin:8px 0;}
    input,textarea,button{font-size:14px;padding:8px;}
    input,textarea{width:100%;}
    textarea{height:110px;}
    pre{background:#111;color:#f7f7f7;padding:12px;white-space:pre-wrap;border-radius:8px;min-height:120px;}
  </style>
</head>
<body>
  <h2>CASPER OSC 外部對話介面</h2>
  <p>此頁僅提供安全測試用途；需輸入 API Key。</p>
  <div class="row"><label style="width:120px;">API Key</label><input id="key" placeholder="X-API-Key" /></div>
  <div class="row"><label style="width:120px;">User ID</label><input id="uid" value="external_ui_user" /></div>
  <div class="row"><label style="width:120px;">Platform</label><input id="plat" value="WEB" /></div>
  <div class="row"><label style="width:120px;">Message</label><textarea id="msg" placeholder="輸入要詢問 CASPER 的內容"></textarea></div>
  <div class="row"><button onclick="sendMsg()">送出</button></div>
  <pre id="out"></pre>
<script>
async function sendMsg(){
  const key=document.getElementById('key').value.trim();
  const uid=document.getElementById('uid').value.trim()||'external_ui_user';
  const plat=document.getElementById('plat').value.trim()||'WEB';
  const msg=document.getElementById('msg').value.trim();
  const out=document.getElementById('out');
  out.textContent='處理中...';
  try{
    const res=await fetch('/osc/external/chat',{method:'POST',headers:{'Content-Type':'application/json','X-API-Key':key},body:JSON.stringify({user_id:uid,platform:plat,message:msg})});
    const j=await res.json();
    out.textContent=JSON.stringify(j,null,2);
  }catch(e){
    out.textContent=String(e);
  }
}
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route('/osc/external/case_status', methods=['POST'])
def external_osc_case_status():
    ok, err = _check_external_api_key()
    if not ok:
        code = 503 if "server_misconfigured" in err else 401
        return jsonify({"success": False, "error": err}), code
    from skills.evolution.skill_genesis import run_skill_action

    data = request.get_json() or {}
    timeout_sec = min(180, max(10, int(data.get("timeout_sec", 180))))  # cap 10-180s
    query = (data.get("query") or "").strip()
    payload = {
        "query": query or str(data.get("case_query") or data.get("case_number") or "").strip(),
        "max_cases": int(data.get("max_cases", 6)),
        "max_files_per_case": int(data.get("max_files_per_case", 20)),
        "full_scan": bool(data.get("full_scan", False)),
        "summary_only": bool(data.get("summary_only", True)),
    }
    task = "status " + json.dumps(payload, ensure_ascii=False)
    tool_name = "skill:osc-flow-case-status"
    started = _start_tool_event(tool_name, {"task": task}, {"route": "osc_external_case_status"})
    allowed, decision = _check_tool_access(
        tool_name,
        command_subject="skill:osc-flow-case-status",
        path_subject=_resolve_skill_action_path("osc-flow-case-status"),
    )
    if not allowed:
        return _tool_denied_response(tool_name, started, decision, {"route": "osc_external_case_status"})
    try:
        result = run_skill_action(
            "osc-flow-case-status",
            task,
            timeout_sec=timeout_sec,
            auto_repair=True,
            rollback_on_fail=True,
            auto_install_deps=True,
            route_key="osc:external:case_status",
        )
    except Exception as exc:
        return _tool_exception_response(
            tool_name,
            started,
            f"osc_external_case_status_exception: {exc}",
            metadata={"route": "osc_external_case_status"},
        )
    _finish_tool_event(
        tool_name,
        started,
        ok=bool(result.get("success")),
        status="handled" if result.get("success") else "error",
        output_data=_tool_preview(result),
        error=str(result.get("error") or ""),
        metadata={"route": "osc_external_case_status"},
    )
    return jsonify(_guard_payload_fields(result)), (200 if result.get("success") else 400)

@app.route('/connections', methods=['GET'])
def connections_status():
    """
    Non-sensitive connectivity/status snapshot.
    Returns booleans and safe metadata only (never returns tokens).
    """
    project_root = str(_MAGI_ROOT)
    agent_dir = os.path.join(project_root, ".agent")
    line_last_sender_file = os.environ.get("MAGI_LINE_LAST_SENDER_FILE", os.path.join(agent_dir, "line_last_sender.json"))
    discord_last_channel_file = os.environ.get("MAGI_DISCORD_LAST_CHANNEL_FILE", os.path.join(agent_dir, "discord_last_channel.json"))

    def _safe_tail(s: str, n: int = 6) -> str:
        s = (s or "").strip()
        return ("…" + s[-n:]) if len(s) > n else s

    def _read_json(path: str) -> dict:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    line_access_token_set = bool((os.environ.get("MAGI_LINE_CHANNEL_ACCESS_TOKEN") or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or "").strip())
    line_secret_set = bool((os.environ.get("MAGI_LINE_CHANNEL_SECRET") or os.environ.get("LINE_CHANNEL_SECRET") or "").strip())
    line_admin_ids = [x.strip() for x in (os.environ.get("MAGI_ADMIN_LINE_IDS") or "").split(",") if x.strip()]
    discord_admin_ids = [x.strip() for x in (os.environ.get("DISCORD_ADMIN_IDS") or "").split(",") if x.strip()]
    try:
        from api.admin_allowlist import get_line_admin_user_ids, get_discord_admin_ids  # type: ignore
        line_admin_ids = sorted(set(line_admin_ids) | set(get_line_admin_user_ids() or set()))
        discord_admin_ids = sorted(set(discord_admin_ids) | set(get_discord_admin_ids() or set()))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 917, exc_info=True)
    last_sender = _read_json(line_last_sender_file)
    last_sender_uid = (last_sender.get("user_id") or "").strip()

    discord_bot_token_set = bool((os.environ.get("DISCORD_BOT_TOKEN") or "").strip())
    discord_webhook_set = bool((os.environ.get("MAGI_DISCORD_WEBHOOK") or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip())
    discord_channel_id = (os.environ.get("DISCORD_CHANNEL_ID") or "").strip()
    last_channel = _read_json(discord_last_channel_file)
    last_channel_id = (last_channel.get("channel_id") or "").strip()

    internet_allowed = os.environ.get("MAGI_ALLOW_INTERNET", "0").strip().lower() in {"1", "true", "yes", "on"}
    cloud_models_allowed = os.environ.get("MAGI_ALLOW_CLOUD_MODELS", "0").strip().lower() in {"1", "true", "yes", "on"}

    return jsonify(
        {
            "policy": {
                "internet_allowed": internet_allowed,
                "cloud_models_allowed": cloud_models_allowed,
            },
            "line": {
                "enabled": bool(line_access_token_set and line_secret_set),
                "access_token_set": line_access_token_set,
                "channel_secret_set": line_secret_set,
                "admin_ids_configured": bool(line_admin_ids),
                "admin_ids_count": len(line_admin_ids),
                "last_sender_file_present": os.path.exists(line_last_sender_file),
                "last_sender_user_id_tail": _safe_tail(last_sender_uid),
                "last_sender_updated_at": last_sender.get("updated_at"),
                "auto_admin_last_sender": _to_bool(os.environ.get("MAGI_LINE_AUTO_ADMIN_LAST_SENDER", "0"), False),
            },
            "discord": {
                "bot_token_set": discord_bot_token_set,
                "webhook_set": discord_webhook_set,
                "admin_ids_configured": bool(discord_admin_ids),
                "admin_ids_count": len(discord_admin_ids),
                "channel_id_configured": bool(discord_channel_id),
                "channel_id": discord_channel_id or None,
                "last_channel_file_present": os.path.exists(discord_last_channel_file),
                "last_channel_id": last_channel_id or None,
                "can_proactive_notify": bool(discord_webhook_set or (discord_bot_token_set and (discord_channel_id or last_channel_id))),
                "last_channel_updated_at": last_channel.get("updated_at"),
            },
            "time": {"epoch": int(time.time())},
        }
    )

# ============== 三哲人狀態 ==============
@app.route('/sages', methods=['GET'])
def sages_status():
    """Returns status of all MAGI sages."""
    from skills.bridge.http_pool import get_session as _get_session

    # Check Melchior (via /v1/models API)
    try:
        from api.routing.node_registry import get_node_ip
        melchior_host = os.environ.get("MELCHIOR_HOST") or get_node_ip("melchior") or "100.116.54.16"
    except Exception:
        melchior_host = os.environ.get("MELCHIOR_HOST", "100.116.54.16")
    melchior_api_port = os.environ.get("MELCHIOR_API_PORT", "8080")
    try:
        r = _get_session().get(f"http://{melchior_host}:{melchior_api_port}/v1/models", timeout=3)
        if r.status_code == 200:
            data = r.json().get("data", [])
            model_names = [m.get("id", "") for m in data[:3]]
            melchior = {"online": True, "role": "Scientist (Vision/Code)", "gpu": "RTX 3060", "models": model_names}
        else:
            melchior = {"online": False, "role": "Scientist (Vision/Code)", "gpu": "RTX 3060"}
    except Exception:
        melchior = {"online": False, "role": "Scientist (Vision/Code)", "gpu": "RTX 3060"}
    
    # Balthasar: council-only node; daily service is proxied by Casper.
    b_status, b_msg = balthasar_health()
    balthasar = {
        "online": b_status,
        "role": "Council (Review Only)",
        "council_only": True,
        "remote_enabled": _to_bool(os.environ.get("BALTHASAR_REMOTE_ENABLED", "0"), False),
        "message": b_msg,
        "proxy_on_casper": {"summarize": True, "transcribe": True},
    }
    
    return jsonify({
        "casper": {"online": True, "role": "Decision & Governor"},
        "melchior": melchior,
        "balthasar": balthasar
    })

# ============== CASPER (搜尋/研究) ==============
@app.route('/search', methods=['POST'])
def api_search():
    """Web search endpoint."""
    data = request.get_json() or {}
    query = data.get('query', '')
    num_results = data.get('num_results', 5)
    
    if not query:
        return jsonify({"error": "Missing 'query' parameter"}), 400

    started = _start_tool_event("search", {"query": query, "num_results": num_results})
    allowed, decision = _check_tool_access("search", command_subject="tool:search")
    if not allowed:
        return _tool_denied_response("search", started, decision)
    try:
        ok, result = _run_with_timeout(search_web, 30, query, num_results)
        if not ok:
            prefix = "search_timeout" if result.get("error_type") == "timeout" else "search_exception"
            return _tool_exception_response("search", started, f"{prefix}: {result.get('error', 'unknown_error')}")
    except Exception as exc:
        return _tool_exception_response("search", started, f"search_exception: {exc}")
    _finish_tool_event("search", started, ok=True, status="handled", output_data=_tool_preview(result))
    return jsonify(result)

@app.route('/research', methods=['POST'])
def api_research():
    """Deep research endpoint."""
    data = request.get_json() or {}
    topic = data.get('topic', '')
    depth = data.get('depth', 3)
    
    if not topic:
        return jsonify({"error": "Missing 'topic' parameter"}), 400

    started = _start_tool_event("research", {"topic": topic, "depth": depth})
    allowed, decision = _check_tool_access("research", command_subject="tool:research")
    if not allowed:
        return _tool_denied_response("research", started, decision)
    try:
        ok, result = _run_with_timeout(research_topic, 60, topic, depth)
        if not ok:
            prefix = "research_timeout" if result.get("error_type") == "timeout" else "research_exception"
            return _tool_exception_response("research", started, f"{prefix}: {result.get('error', 'unknown_error')}")
    except Exception as exc:
        return _tool_exception_response("research", started, f"research_exception: {exc}")
    payload = {
        "topic": result["topic"],
        "sources": [{"title": s["title"], "url": s["url"]} for s in result.get("sources", [])],
        "content_preview": result.get("combined_content", "")[:5000]
    }
    _finish_tool_event("research", started, ok=True, status="handled", output_data=_tool_preview(payload))
    return jsonify({
        "topic": result["topic"],
        "sources": [{"title": s["title"], "url": s["url"]} for s in result.get("sources", [])],
        "content_preview": result.get("combined_content", "")[:5000]
    })

@app.route('/fetch', methods=['POST'])
def api_fetch():
    """URL fetch endpoint."""
    data = request.get_json() or {}
    url = data.get('url', '')
    
    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400

    started = _start_tool_event("fetch", {"url": url})
    allowed, decision = _check_tool_access("fetch", command_subject="tool:fetch")
    if not allowed:
        return _tool_denied_response("fetch", started, decision)
    try:
        ok, result = _run_with_timeout(fetch_url_content, 30, url)
        if not ok:
            prefix = "fetch_timeout" if result.get("error_type") == "timeout" else "fetch_exception"
            return _tool_exception_response("fetch", started, f"{prefix}: {result.get('error', 'unknown_error')}")
    except Exception as exc:
        return _tool_exception_response("fetch", started, f"fetch_exception: {exc}")
    _finish_tool_event("fetch", started, ok=True, status="handled", output_data=_tool_preview(result))
    return jsonify(result)

# ============== MELCHIOR (視覺分析) ==============
@app.route('/vision', methods=['POST'])
def api_vision():
    """Melchior vision analysis endpoint."""
    data = request.get_json() or {}
    image_path = data.get('image_path', '')
    prompt = data.get('prompt', 'Describe this image in detail')
    task_type = str(data.get("task_type") or "vision").strip().lower()
    
    if not image_path:
        return jsonify({"error": "Missing 'image_path' parameter"}), 400

    if not os.path.exists(image_path):
        return jsonify({"error": f"Image not found: {image_path}"}), 404

    started = _start_tool_event(
        "vision",
        {"image_path": image_path, "prompt": prompt, "task_type": task_type},
    )
    allowed, decision = _check_tool_access(
        "vision",
        command_subject="tool:vision",
        path_subject=image_path,
    )
    if not allowed:
        return _tool_denied_response("vision", started, decision)

    prompt_l = str(prompt or "").lower()
    is_ocr = (
        task_type in {"ocr", "text", "transcribe", "scan"}
        or any(k in prompt_l for k in ("ocr", "辨識", "文字", "讀取", "transcribe"))
    )
    is_captcha = (
        task_type in {"captcha"}
        or any(k in prompt_l for k in ("captcha", "驗證碼", "digits", "characters"))
    )
    effective_task = "captcha" if is_captcha else "vision"
    model_hint = (
        str(data.get("model") or "").strip()
        or (os.environ.get("MAGI_VISION_OCR_MODEL", os.environ.get("MAGI_OMLX_OCR_MODEL", "")) if is_ocr else os.environ.get("MAGI_VISION_MODEL", os.environ.get("MAGI_OMLX_OCR_MODEL", "")))
    )
    force_local = _to_bool(data.get("force_local"), _to_bool(os.environ.get("MAGI_VISION_FORCE_LOCAL", "1"), True))
    timeout_sec = int(data.get("timeout_sec") or os.environ.get("MAGI_VISION_TIMEOUT_SEC", "90") or "90")

    try:
        result = _INFERENCE_GATEWAY.vision(
            image_path=image_path,
            prompt=str(prompt or "").strip() or "Describe this image in detail",
            timeout=max(8, int(timeout_sec)),
            task_type=effective_task,
            force_local=force_local,
            model=model_hint,
        )
    except Exception as e:
        result = {"success": False, "error": f"vision_gateway_exception: {e}"}

    if not result.get("success"):
        # KEEP: Last-resort vision fallback via melchior_bridge.analyze_image().
        # Not a separate endpoint -- inline resilience when vision_gateway fails.
        # Provides degraded-mode results rather than returning an error to callers.
        fb_ok, fb_val = _run_with_timeout(analyze_image, 60, image_path, prompt)
        description = fb_val if fb_ok else None
        result = {
            "success": bool(fb_ok and description and not str(description).lower().startswith("error")),
            "analysis": str(description or "") if fb_ok else "",
            "route": "compat_melchior_bridge",
            "degraded": True,
            "model": "",
            "error": str(result.get("error") or "") if fb_ok else f"vision_fallback_timeout: {fb_val.get('error', 'timeout')}",
        }

    description = str(result.get("analysis") or result.get("response") or "").strip()
    _finish_tool_event(
        "vision",
        started,
        ok=bool(result.get("success")),
        status="handled" if result.get("success") else "error",
        output_data=_tool_preview(description),
        error=str(result.get("error") or ""),
        metadata={"route": str(result.get("route") or ""), "task_type": effective_task},
    )
    return jsonify({
        "success": bool(result.get("success")),
        "sage": "vision_gateway",
        "image": image_path,
        "description": description,
        "route": str(result.get("route") or ""),
        "model": str(result.get("model") or ""),
        "degraded": bool(result.get("degraded", False)),
        "force_local": bool(force_local),
        "task_type": effective_task,
        "error": str(result.get("error") or ""),
    })

# ============== MELCHIOR (Remote Enhancement) ==============
@app.route('/melchior/health', methods=['GET'])
def api_melchior_health():
    """Melchior health summary."""
    return jsonify(melchior_health()), 200


@app.route('/melchior/skills/sync', methods=['POST'])
def api_melchior_sync_skills():
    """Push current skills bundle to Melchior (/api/skills/sync on Melchior)."""
    data = request.get_json() or {}
    skills_dir = data.get("skills_dir", f"{_MAGI_ROOT}/skills")
    mode = (data.get("mode") or "").strip().lower()  # auto|delta|full
    force = _to_bool(data.get("force", False), False)
    smoke_test = _to_bool(data.get("smoke_test", True), True)
    result = sync_skills_to_melchior(skills_dir, mode=mode, force=force, smoke_test=smoke_test)
    return jsonify(result), (200 if result.get("success") else 500)

# ============== BALTHASAR (摘要) ==============
@app.route('/summarize', methods=['POST'])
def api_summarize():
    """Summarization endpoint (Apple Intelligence first when available, then fallback)."""
    t0 = time.monotonic()
    data = request.get_json() or {}
    text = data.get('text', '')
    metric_engine = str((data.get("engine") or os.environ.get("MAGI_SUMMARIZE_ENGINE") or "balthasar")).strip().lower()
    metric_route = ""
    metric_degraded = False
    metric_timeout = False
    metric_apple_tried = False
    metric_error = ""

    if not text:
        return jsonify({"error": "Missing 'text' parameter"}), 400

    started = _start_tool_event(
        "summarize",
        {"text_preview": _tool_preview(text), "engine": metric_engine},
    )
    allowed, decision = _check_tool_access("summarize", command_subject="tool:summarize")
    if not allowed:
        return _tool_denied_response("summarize", started, decision)

    default_engine = metric_engine or "balthasar"
    auto_prefers_apple = _to_bool(os.environ.get("MAGI_SUMMARIZE_AUTO_APPLE", "0"), False)
    allow_apple = _to_bool(data.get("allow_apple"), auto_prefers_apple)
    summary_length = str(data.get("summary_length") or "medium").strip().lower() or "medium"
    timeout_sec = max(10, min(int(data.get("timeout_sec", 75)), int(os.environ.get("MAGI_SUMMARIZE_MAX_TIMEOUT_SEC", "90"))))
    # Reliability-first default: keep the primary summary budget large enough for
    # medium legal documents instead of degrading too aggressively at ~28s.
    primary_timeout_sec = max(8, min(timeout_sec, int(os.environ.get("MAGI_SUMMARIZE_PRIMARY_TIMEOUT_SEC", "45") or "45")))
    engine = (data.get("engine") or default_engine).strip().lower()
    if engine == "auto":
        engine = "apple" if allow_apple else "balthasar"
    apple_tried = False
    apple_result = None

    def _extractive_summary_quick(s: str) -> str:
        body = str(s or "").strip()
        if not body:
            return ""
        compact = re.sub(r"\s+", " ", body.replace("\n", " ")).strip()
        if not compact:
            return ""
        sentences = [
            seg.strip(" \t\r\n-•")
            for seg in re.split(r"(?<=[。！？!?；;\.])\s+", compact)
            if seg.strip()
        ]
        picks = []
        seen = set()
        for seg in sentences:
            if len(seg) < 18:
                continue
            norm = re.sub(r"\W+", "", seg).lower()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            picks.append(seg[:180])
            if len(picks) >= 4:
                break
        if not picks:
            short = compact[:240] + ("…" if len(compact) > 240 else "")
            return f"重點摘要：{short}"
        return "\n".join(f"• {item}" for item in picks)

    # Circuit breaker: when upstream keeps timing out, return immediate degraded summary.
    from api.handlers.summary_handler import summarize_text_resilient as _resilient_summarize

    if not _summarize_cb_allow_upstream():
        probe_budget = max(12, min(timeout_sec, int(os.environ.get("MAGI_SUMMARIZE_CB_PROBE_TIMEOUT_SEC", "20") or "20")))
        probe_ok, probe_result = _run_with_timeout(_resilient_summarize, probe_budget + 1, text, summary_length, pool=_INFERENCE_EXECUTOR)
        if probe_ok and isinstance(probe_result, dict) and probe_result.get("success") and (probe_result.get("text") or "").strip():
            metric_route = str(probe_result.get("provider") or probe_result.get("route") or "resilient_probe")
            _summarize_cb_note_success()
            _record_summarize_metric(
                t0,
                success=True,
                timeout=False,
                upstream_timeout=False,
                engine="balthasar",
                route=metric_route,
                degraded=False,
                apple_tried=metric_apple_tried,
                error="",
            )
            response = jsonify({
                "sage": "balthasar",
                "served_by": "casper",
                "engine": "balthasar",
                "apple_tried": bool(apple_tried),
                "apple_error": (apple_result.get("error") if isinstance(apple_result, dict) else ""),
                "note": "摘要上游曾短暫繁忙，已由 resilient 本地路徑接手。",
                "result": probe_result,
            })
            _finish_tool_event("summarize", started, ok=True, status="handled", output_data=_tool_preview(response.get_json()))
            return response
        metric_degraded = True
        metric_route = "circuit_open_degraded"
        _record_summarize_metric(
            t0,
            success=True,
            timeout=False,
            upstream_timeout=False,
            engine="balthasar",
            route=metric_route,
            degraded=True,
            apple_tried=metric_apple_tried,
            error="circuit_open",
        )
        response = jsonify({
            "sage": "balthasar",
            "served_by": "casper",
            "engine": "balthasar",
            "apple_tried": bool(apple_tried),
            "apple_error": (apple_result.get("error") if isinstance(apple_result, dict) else ""),
            "note": "摘要上游暫時繁忙，已啟用快速降級。",
            "result": {
                "success": True,
                "degraded": True,
                "provider": metric_route,
                "error": "circuit_open",
                "text": _extractive_summary_quick(text),
            }
        })
        _finish_tool_event(
            "summarize",
            started,
            ok=True,
            status="degraded",
            output_data=_tool_preview(response.get_json()),
            error="circuit_open",
        )
        return response

    # 1) Apple Intelligence via Shortcuts (requires user-created shortcut "MAGI 摘要")
    if allow_apple and engine in {"apple", "apple_intelligence", "shortcuts"}:
        try:
            from skills.apple.apple_intelligence import summarize_text_apple_intelligence

            apple_tried = True
            metric_apple_tried = True
            # Best-effort: Shortcuts input length may be limited; chunk if huge.
            s = (text or "").strip()
            if len(s) > 12000:
                chunks = [s[i : i + 6000] for i in range(0, min(len(s), 36000), 6000)]
                outs = []
                for c in chunks:
                    r = summarize_text_apple_intelligence(c, timeout_sec=min(60, timeout_sec))
                    if not r.get("success"):
                        apple_result = r
                        raise RuntimeError(r.get("error") or "apple summarize failed")
                    outs.append((r.get("text") or "").strip())
                apple_result = {"success": True, "text": "\n\n".join([o for o in outs if o]), "engine": "shortcuts"}
            else:
                apple_result = summarize_text_apple_intelligence(s, timeout_sec=min(60, timeout_sec))
        except Exception as e:
            apple_result = apple_result or {"success": False, "error": str(e)[:200], "engine": "shortcuts"}
            metric_error = str(e)[:200]

        if isinstance(apple_result, dict) and apple_result.get("success") and (apple_result.get("text") or "").strip():
            metric_route = "apple_shortcuts"
            _record_summarize_metric(
                t0,
                success=True,
                timeout=False,
                upstream_timeout=False,
                engine=engine,
                route=metric_route,
                degraded=False,
                apple_tried=metric_apple_tried,
                error="",
            )
            response = jsonify({
                "sage": "apple_intelligence",
                "served_by": "casper",
                "engine": "shortcuts",
                "result": (apple_result.get("text") or "").strip(),
            })
            _finish_tool_event("summarize", started, ok=True, status="handled", output_data=_tool_preview(response.get_json()))
            return response
        # If user explicitly forced apple, do not block on LLM fallback.
        if engine in {"apple", "apple_intelligence", "shortcuts"}:
            metric_route = "apple_shortcuts_forced"
            metric_error = (apple_result.get("error") if isinstance(apple_result, dict) else "apple summarize failed")
            _record_summarize_metric(
                t0,
                success=False,
                timeout=False,
                upstream_timeout=False,
                engine=engine,
                route=metric_route,
                degraded=False,
                apple_tried=metric_apple_tried,
                error=metric_error,
            )
            response = jsonify({
                "sage": "apple_intelligence",
                "served_by": "casper",
                "engine": "shortcuts",
                "success": False,
                "error": (apple_result.get("error") if isinstance(apple_result, dict) else "apple summarize failed"),
                "note": "請先在捷徑 App 建立捷徑「MAGI 摘要」（Apple Intelligence）。建立後再重試。",
            })
            _finish_tool_event(
                "summarize",
                started,
                ok=False,
                status="error",
                output_data=_tool_preview(response.get_json()),
                error=(apple_result.get("error") if isinstance(apple_result, dict) else "apple summarize failed"),
            )
            return response, 400

    # 2) Fallback: Balthasar summarization
    ok, result = _run_with_timeout(_resilient_summarize, primary_timeout_sec + 1, text, summary_length, pool=_INFERENCE_EXECUTOR)
    if (not ok) and isinstance(result, dict):
        if "timeout_exceeded" in str(result.get("error", "")).lower():
            metric_timeout = True
    if ok and isinstance(result, dict) and result.get("success"):
        metric_route = str(result.get("provider") or result.get("route") or "balthasar")
        _record_summarize_metric(
            t0,
            success=True,
            timeout=False,
            upstream_timeout=False,
            engine="balthasar",
            route=metric_route,
            degraded=False,
            apple_tried=metric_apple_tried,
            error="",
        )
        _summarize_cb_note_success()
        response = jsonify({
            "sage": "balthasar",
            "served_by": "casper",
            "engine": "balthasar",
            "apple_tried": bool(apple_tried),
            "apple_error": (apple_result.get("error") if isinstance(apple_result, dict) else ""),
            "note": "若要啟用 Apple Intelligence 摘要，需先在捷徑 App 建立捷徑「MAGI 摘要」。",
            "result": result
        })
        _finish_tool_event("summarize", started, ok=True, status="handled", output_data=_tool_preview(response.get_json()))
        return response

    if not ok:
        result = {"success": False, "error": result.get("error", "summarize_timeout")}
    metric_error = str((result or {}).get("error") or "")
    if metric_timeout:
        _summarize_cb_note_upstream_timeout(metric_error)

    # 3) Fast degraded fallback: keep response under tight budget even when upstream stalls.
    fallback_budget = max(8, min(timeout_sec, int(os.environ.get("MAGI_SUMMARIZE_EXTRACTIVE_TIMEOUT_SEC", "16") or "16")))
    fallback_ok, fallback_result = _run_with_timeout(_resilient_summarize, fallback_budget + 1, text, "short", pool=_INFERENCE_EXECUTOR)
    if fallback_ok and isinstance(fallback_result, dict) and fallback_result.get("success") and (fallback_result.get("text") or "").strip():
        fallback_result = dict(fallback_result)
        fallback_result.setdefault("degraded", False)
        metric_route = str(fallback_result.get("provider") or fallback_result.get("route") or "extractive_fallback")
        metric_degraded = False
        _summarize_cb_note_success()
        _record_summarize_metric(
            t0,
            success=True,
            timeout=False,
            upstream_timeout=metric_timeout,
            engine="balthasar",
            route=metric_route,
            degraded=False,
            apple_tried=metric_apple_tried,
            error="",
        )
        response = jsonify({
            "sage": "balthasar",
            "served_by": "casper",
            "engine": "balthasar",
            "apple_tried": bool(apple_tried),
            "apple_error": (apple_result.get("error") if isinstance(apple_result, dict) else ""),
            "note": "摘要主路徑逾時，已改用穩定摘要回覆。",
            "result": fallback_result,
        })
        _finish_tool_event("summarize", started, ok=True, status="handled", output_data=_tool_preview(response.get_json()))
        return response

    fallback_text = _extractive_summary_quick(text)
    metric_degraded = True
    metric_route = "extractive_fallback" if metric_timeout else "extractive_error_fallback"
    _record_summarize_metric(
        t0,
        success=True,
        timeout=False,
        upstream_timeout=metric_timeout,
        engine="balthasar",
        route=metric_route,
        degraded=metric_degraded,
        apple_tried=metric_apple_tried,
        error=metric_error,
    )
    response = jsonify({
        "sage": "balthasar",
        "served_by": "casper",
        "engine": "balthasar",
        "apple_tried": bool(apple_tried),
        "apple_error": (apple_result.get("error") if isinstance(apple_result, dict) else ""),
        "note": "若要啟用 Apple Intelligence 摘要，需先在捷徑 App 建立捷徑「MAGI 摘要」。",
        "result": {
            "success": True,
            "degraded": True,
            "provider": metric_route,
            "error": metric_error[:240],
            "text": fallback_text,
        }
    })
    _finish_tool_event(
        "summarize",
        started,
        ok=True,
        status="degraded",
        output_data=_tool_preview(response.get_json()),
        error=metric_error[:240],
    )
    return response


@app.route('/summarize/health', methods=['GET'])
def api_summarize_health():
    """
    Summarize SLO health / circuit breaker state.
    """
    return jsonify({
        "success": True,
        "summary_metrics_path": SUMMARY_METRICS_PATH,
        "circuit_breaker": _summarize_cb_snapshot(),
    }), 200


# ============== Apple Shortcut thin wrappers ==============
# Apple Shortcuts' "Get Contents of URL" works best with raw octet-stream request
# bodies and text/plain responses.  These wrappers accept a single payload per
# endpoint and return plain text, so the Shortcut can use "Stop and Output"
# directly without JSON parsing.

_SHORTCUT_OCR_MAX_BYTES = int(os.environ.get("MAGI_SHORTCUT_OCR_MAX_BYTES", str(20 * 1024 * 1024)))
_SHORTCUT_PDF_MAX_BYTES = int(os.environ.get("MAGI_SHORTCUT_PDF_MAX_BYTES", str(50 * 1024 * 1024)))
_SHORTCUT_AUDIO_MAX_BYTES = int(os.environ.get("MAGI_SHORTCUT_AUDIO_MAX_BYTES", str(100 * 1024 * 1024)))
_SHORTCUT_TEXT_MAX_BYTES = int(os.environ.get("MAGI_SHORTCUT_TEXT_MAX_BYTES", str(500 * 1024)))


def _shortcut_text_response(body: str, status: int = 200):
    resp = Response(str(body or ""), status=status, mimetype="text/plain; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _shortcut_write_temp(data: bytes, suffix: str) -> str:
    import tempfile
    fd, path = tempfile.mkstemp(prefix="shortcut_", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise
    return path


def _shortcut_payload_bytes(max_bytes: int):
    """Read raw request body or first multipart file.  Returns (bytes, suffix_hint, err)."""
    if request.files:
        try:
            key = next(iter(request.files))
        except Exception:
            key = None
        if key:
            f = request.files[key]
            raw = f.read() or b""
            suffix = os.path.splitext(f.filename or "")[1].lower() or ""
            if len(raw) > max_bytes:
                return b"", "", f"payload_too_large: {len(raw)} > {max_bytes}"
            return raw, suffix, ""
    raw = request.get_data(cache=False) or b""
    if not raw:
        return b"", "", "empty_body"
    if len(raw) > max_bytes:
        return b"", "", f"payload_too_large: {len(raw)} > {max_bytes}"
    return raw, "", ""


@app.route('/shortcut/ocr', methods=['POST'])
@require_api_key
def api_shortcut_ocr():
    """Thin wrapper: raw image bytes → plain-text OCR output.

    Accepts either ``application/octet-stream`` raw body or multipart ``file=``.
    Returns ``text/plain`` with extracted text.  Uses the same vision gateway as
    ``/vision`` with ``task_type=ocr``.
    """
    raw, suffix, err = _shortcut_payload_bytes(_SHORTCUT_OCR_MAX_BYTES)
    if err:
        return _shortcut_text_response(f"[error] {err}", status=400)

    if suffix not in {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp"}:
        # Sniff from first bytes if no suffix
        if raw[:3] == b"\xff\xd8\xff":
            suffix = ".jpg"
        elif raw[:8] == b"\x89PNG\r\n\x1a\n":
            suffix = ".png"
        elif raw[4:8] == b"ftyp":
            suffix = ".heic"
        else:
            suffix = ".jpg"

    tmp_path = _shortcut_write_temp(raw, suffix)
    try:
        timeout_sec = int(request.args.get("timeout_sec") or os.environ.get("MAGI_VISION_TIMEOUT_SEC", "90") or "90")
        try:
            result = _INFERENCE_GATEWAY.vision(
                image_path=tmp_path,
                prompt="請將影像中的文字完整辨識出來，保留原始排版。",
                timeout=max(8, int(timeout_sec)),
                task_type="ocr",
                force_local=True,
            )
        except Exception as e:
            result = {"success": False, "error": f"vision_gateway_exception: {e}"}

        if not result.get("success"):
            fb_ok, fb_val = _run_with_timeout(analyze_image, 60, tmp_path, "OCR this image; return extracted text only.")
            if fb_ok:
                text = str(fb_val or "").strip()
                if text and not text.lower().startswith("error"):
                    return _shortcut_text_response(text)
            return _shortcut_text_response(f"[error] ocr_failed: {result.get('error', 'unknown')}", status=502)

        text = str(result.get("analysis") or result.get("response") or "").strip()
        return _shortcut_text_response(text)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.route('/shortcut/pdf_text', methods=['POST'])
@require_api_key
def api_shortcut_pdf_text():
    """Thin wrapper: raw PDF bytes → plain-text extraction (with OCR fallback)."""
    raw, _suffix, err = _shortcut_payload_bytes(_SHORTCUT_PDF_MAX_BYTES)
    if err:
        return _shortcut_text_response(f"[error] {err}", status=400)
    if raw[:4] != b"%PDF":
        return _shortcut_text_response("[error] not_a_pdf", status=400)

    tmp_path = _shortcut_write_temp(raw, ".pdf")
    try:
        from skills.engine.document_reader import read_document
        r = read_document(tmp_path, mode="auto", ocr_fallback=True, max_chars=500_000)
        if not r.success:
            return _shortcut_text_response(f"[error] pdf_extract_failed: {r.error}", status=502)
        text = (r.text or "").strip()
        if not text:
            return _shortcut_text_response("[error] empty_output", status=502)
        return _shortcut_text_response(text)
    except Exception as e:
        return _shortcut_text_response(f"[error] pdf_exception: {e}", status=500)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.route('/shortcut/summarize', methods=['POST'])
@require_api_key
def api_shortcut_summarize():
    """Thin wrapper: raw text body → plain-text summary."""
    raw = request.get_data(cache=False) or b""
    if not raw:
        return _shortcut_text_response("[error] empty_body", status=400)
    if len(raw) > _SHORTCUT_TEXT_MAX_BYTES:
        return _shortcut_text_response(
            f"[error] payload_too_large: {len(raw)} > {_SHORTCUT_TEXT_MAX_BYTES}", status=400
        )
    try:
        text = raw.decode("utf-8", errors="replace").strip()
    except Exception as e:
        return _shortcut_text_response(f"[error] decode_failed: {e}", status=400)
    if not text:
        return _shortcut_text_response("[error] empty_text", status=400)

    timeout_sec = int(request.args.get("timeout_sec") or "75")
    try:
        ok, result = _run_with_timeout(
            summarize_text, max(15, timeout_sec), text, pool=_INFERENCE_EXECUTOR
        )
    except Exception as e:
        return _shortcut_text_response(f"[error] summarize_exception: {e}", status=500)
    if not ok:
        return _shortcut_text_response(f"[error] summarize_timeout_{timeout_sec}s", status=504)
    if isinstance(result, dict):
        summary = (result.get("summary") or result.get("text") or "").strip()
        if not summary and not result.get("success", False):
            return _shortcut_text_response(f"[error] summarize_failed: {result.get('error', 'unknown')}", status=502)
    else:
        summary = str(result or "").strip()
    if not summary:
        return _shortcut_text_response("[error] empty_summary", status=502)
    return _shortcut_text_response(summary)


@app.route('/shortcut/transcribe', methods=['POST'])
@require_api_key
def api_shortcut_transcribe():
    """Thin wrapper: raw audio bytes → plain-text transcription."""
    raw, suffix, err = _shortcut_payload_bytes(_SHORTCUT_AUDIO_MAX_BYTES)
    if err:
        return _shortcut_text_response(f"[error] {err}", status=400)

    if suffix not in {".wav", ".mp3", ".m4a", ".aiff", ".aif", ".caf", ".flac", ".ogg", ".mp4"}:
        suffix = ".m4a"

    tmp_path = _shortcut_write_temp(raw, suffix)
    try:
        from skills.bridge.tri_sage_collab import transcribe_audio
        timeout_sec = int(
            request.args.get("timeout_sec")
            or os.environ.get("MAGI_TRANSCRIBE_TIMEOUT_SEC", "180")
            or "180"
        )
        ok, result = _run_with_timeout(
            transcribe_audio, max(30, timeout_sec), tmp_path, pool=_INFERENCE_EXECUTOR
        )
        if not ok:
            return _shortcut_text_response(f"[error] transcribe_timeout_{timeout_sec}s", status=504)
        if not result.get("success"):
            return _shortcut_text_response(
                f"[error] transcribe_failed: {result.get('error', 'unknown')}", status=502
            )
        text = str(result.get("text") or result.get("transcript") or "").strip()
        if not text:
            return _shortcut_text_response("[error] empty_transcript", status=502)
        return _shortcut_text_response(text)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ============== Skills ==============
@app.route('/skills', methods=['GET'])
def api_list_skills():
    """List installed skills."""
    return jsonify({"skills": list_skills()})

@app.route('/skills', methods=['POST'])
@require_api_key
def api_create_skill():
    """Create a new skill."""
    data = request.get_json() or {}
    name = data.get('name', '')
    description = data.get('description', '')
    instructions = data.get('instructions', '')
    
    if not all([name, description, instructions]):
        return jsonify({"error": "Missing required parameters: name, description, instructions"}), 400
    
    result = generate_skill(name, description, instructions, author="API")
    return jsonify(result)

@app.route('/skills/discover', methods=['POST'])
@require_api_key
def api_discover_skills():
    """Discover skills from GitHub matching a need."""
    from skills.evolution.skill_genesis import auto_discover_and_suggest
    data = request.get_json() or {}
    need = data.get('need', '')
    if not need:
        return jsonify({"error": "Missing 'need' parameter"}), 400
    result = auto_discover_and_suggest(need)
    return jsonify(result)

@app.route('/skills/install', methods=['POST'])
@require_api_key
def api_install_skill():
    """Install skill from GitHub URL or name."""
    from skills.evolution.skill_genesis import auto_install_skill
    data = request.get_json() or {}
    source = data.get('url') or data.get('name', '')
    if not source:
        return jsonify({"error": "Missing 'url' or 'name' parameter"}), 400
    result = auto_install_skill(source)
    return jsonify(result)

@app.route('/skills/acquire', methods=['POST'])
@require_api_key
def api_acquire_skill():
    """Complete skill acquisition: search → analyze → install → generate if not found."""
    from skills.evolution.skill_genesis import acquire_skill
    data = request.get_json() or {}
    need = data.get('need', '')
    auto_generate = data.get('auto_generate', True)
    auto_activate = data.get('auto_activate', True)
    if not need:
        return jsonify({"error": "Missing 'need' parameter"}), 400
    result = acquire_skill(need, auto_generate, auto_activate=auto_activate)
    return jsonify(result)


@app.route('/skills/run', methods=['POST'])
@require_api_key
def api_run_skill():
    """Run generated/imported skill action.py."""
    from skills.evolution.skill_genesis import run_skill_action
    data = request.get_json() or {}
    skill = data.get('skill', '')
    task = data.get('task', '')
    timeout_sec = min(180, max(5, int(data.get('timeout_sec', 30))))  # cap 5-180s
    auto_repair = _to_bool(data.get('auto_repair', True), True)
    rollback_on_fail = _to_bool(data.get('rollback_on_fail', True), True)
    auto_install_deps = _to_bool(data.get('auto_install_deps', True), True)
    route_key = data.get('route_key', '')
    if not task:
        return jsonify({"error": "Missing 'task' parameter"}), 400
    if not skill:
        return jsonify({"error": "Missing 'skill' parameter"}), 400
    tool_name = f"skill:{skill}"
    started = _start_tool_event(tool_name, {"task": task}, {"route": "skills_run"})
    allowed, decision = _check_tool_access(
        tool_name,
        command_subject=tool_name,
        path_subject=_resolve_skill_action_path(skill),
    )
    if not allowed:
        return _tool_denied_response(tool_name, started, decision, {"route": "skills_run"})
    try:
        result = run_skill_action(
            skill,
            task,
            timeout_sec=timeout_sec,
            auto_repair=auto_repair,
            rollback_on_fail=rollback_on_fail,
            auto_install_deps=auto_install_deps,
            route_key=route_key,
        )
    except Exception as exc:
        return _tool_exception_response(
            tool_name,
            started,
            f"skills_run_exception: {exc}",
            metadata={"route": "skills_run"},
        )
    _finish_tool_event(
        tool_name,
        started,
        ok=bool(result.get("success")),
        status="handled" if result.get("success") else "error",
        output_data=_tool_preview(result),
        error=str(result.get("error") or ""),
        metadata={"route": "skills_run"},
    )
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/versions', methods=['POST'])
def api_skill_versions():
    """List available snapshots for a skill."""
    from skills.evolution.skill_genesis import list_skill_versions
    data = request.get_json() or {}
    skill = data.get('skill', '')
    if not skill:
        return jsonify({"error": "Missing 'skill' parameter"}), 400
    result = list_skill_versions(skill)
    return jsonify(result), (200 if result.get("success") else 404)


@app.route('/skills/rollback', methods=['POST'])
def api_skill_rollback():
    """Rollback skill files to a previous snapshot."""
    from skills.evolution.skill_genesis import rollback_skill_version
    data = request.get_json() or {}
    skill = data.get('skill', '')
    version_id = data.get('version_id', '')
    if not skill:
        return jsonify({"error": "Missing 'skill' parameter"}), 400
    result = rollback_skill_version(skill, version_id=version_id)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/release', methods=['GET'])
def api_skill_release_state():
    """Get stable/canary release state."""
    from skills.evolution.skill_genesis import get_skill_release_state
    skill = request.args.get("skill", "").strip()
    if not skill:
        return jsonify({"error": "Missing 'skill' query parameter"}), 400
    result = get_skill_release_state(skill)
    return jsonify(result), (200 if result.get("success") else 404)


@app.route('/skills/stable', methods=['POST'])
def api_skill_set_stable():
    """Mark a stable version for a skill."""
    from skills.evolution.skill_genesis import set_stable_skill_version
    data = request.get_json() or {}
    skill = data.get('skill', '')
    version_id = data.get('version_id', '')
    enforce = _to_bool(data.get('enforce', True), True)
    if not skill:
        return jsonify({"error": "Missing 'skill' parameter"}), 400
    result = set_stable_skill_version(skill, version_id=version_id, enforce=enforce)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/canary/start', methods=['POST'])
def api_skill_canary_start():
    """Start canary release for a specific version."""
    from skills.evolution.skill_genesis import start_canary_release
    data = request.get_json() or {}
    skill = data.get('skill', '')
    version_id = data.get('version_id', '')
    canary_percent = int(data.get('canary_percent', 10))
    min_runs = int(data.get('min_runs', 10))
    fail_threshold = int(data.get('fail_threshold', 3))
    max_failure_rate = float(data.get('max_failure_rate', 0.5))
    auto_promote = _to_bool(data.get('auto_promote', True), True)
    raw_promote_min_runs = data.get('promote_min_runs', None)
    raw_promote_max_failure_rate = data.get('promote_max_failure_rate', None)
    promote_min_runs = int(raw_promote_min_runs) if raw_promote_min_runs not in (None, "", "null") else None
    promote_max_failure_rate = float(raw_promote_max_failure_rate) if raw_promote_max_failure_rate not in (None, "", "null") else None
    if not skill or not version_id:
        return jsonify({"error": "Missing 'skill' or 'version_id' parameter"}), 400
    result = start_canary_release(
        skill,
        version_id,
        canary_percent=canary_percent,
        min_runs=min_runs,
        fail_threshold=fail_threshold,
        max_failure_rate=max_failure_rate,
        auto_promote=auto_promote,
        promote_min_runs=promote_min_runs,
        promote_max_failure_rate=promote_max_failure_rate,
    )
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/canary/stop', methods=['POST'])
def api_skill_canary_stop():
    """Stop canary release for a skill."""
    from skills.evolution.skill_genesis import stop_canary_release
    data = request.get_json() or {}
    skill = data.get('skill', '')
    reason = data.get('reason', 'manual_stop')
    if not skill:
        return jsonify({"error": "Missing 'skill' parameter"}), 400
    result = stop_canary_release(skill, reason=reason)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/ci', methods=['POST'])
def api_skill_ci():
    """Run skill CI checks (safety/compile/smoke)."""
    from skills.evolution.skill_genesis import run_skill_ci
    data = request.get_json() or {}
    skill = data.get('skill', '')
    task = data.get('task', 'self test')
    attempt_repair = _to_bool(data.get('attempt_repair', False), False)
    if not skill:
        return jsonify({"error": "Missing 'skill' parameter"}), 400
    result = run_skill_ci(skill, task=task, attempt_repair=attempt_repair)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/events', methods=['GET'])
def api_skill_events():
    """Get skill runtime event summary."""
    from skills.evolution.skill_genesis import get_skill_runtime_stats
    limit = int(request.args.get("limit", 200))
    result = get_skill_runtime_stats(limit=limit)
    return jsonify(result), (200 if result.get("success", True) else 500)


@app.route('/skills/teach', methods=['POST'])
def api_skill_teach():
    """Teach CASPER a new tip/lesson."""
    from skills.management.auto_skill import AutoSkill
    data = request.get_json() or {}
    lesson = data.get('lesson') or data.get('tip') or ''
    keywords = data.get('keywords') or []
    context = data.get('context', 'api-teach')
    source = data.get('source', 'openclaw')
    if not lesson:
        return jsonify({"error": "Missing 'lesson' (or 'tip') parameter"}), 400
    autoskill = AutoSkill()
    if keywords:
        result = autoskill.learn(keywords, lesson, context=context, source=source)
    else:
        result = autoskill.teach(lesson, context=context, source=source)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/teach/file', methods=['POST'])
def api_skill_teach_file():
    """Teach CASPER from a text/code file."""
    from skills.management.auto_skill import AutoSkill
    data = request.get_json() or {}
    file_path = data.get('file_path', '')
    context = data.get('context', 'api-file-teach')
    source = data.get('source', 'openclaw')
    max_lines = int(data.get('max_lines', 200))
    if not file_path:
        return jsonify({"error": "Missing 'file_path' parameter"}), 400
    autoskill = AutoSkill()
    result = autoskill.learn_from_file(file_path, context=context, source=source, max_lines=max_lines)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/internalize', methods=['POST'])
def api_skill_internalize():
    """Internalize learned knowledge as a runnable skill."""
    from skills.management.auto_skill import AutoSkill
    data = request.get_json() or {}
    skill_name = data.get('skill_name', '')
    description = data.get('description', '')
    keywords = data.get('keywords') or []
    max_tips = int(data.get('max_tips', 40))
    auto_activate = _to_bool(data.get('auto_activate', True), True)
    autoskill = AutoSkill()
    result = autoskill.internalize_as_skill(
        skill_name=skill_name,
        description=description,
        keywords=keywords,
        max_tips=max_tips,
        auto_activate=auto_activate,
    )
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/internalize/codebase', methods=['POST'])
def api_skill_internalize_codebase():
    """Convert codebase Python modules into wrapper skills with incremental index."""
    from skills.management.auto_skill import AutoSkill
    data = request.get_json() or {}
    source_dir = data.get("source_dir", str(get_orch_dir()))
    max_files = int(data.get("max_files", 50))
    force = _to_bool(data.get("force", False), False)
    auto_activate = _to_bool(data.get("auto_activate", True), True)
    enable_release = _to_bool(data.get("enable_release", True), True)
    canary_percent = int(data.get("canary_percent", 20))
    promote_min_runs = int(data.get("promote_min_runs", 12))
    promote_max_failure_rate = float(data.get("promote_max_failure_rate", 0.2))
    autoskill = AutoSkill()
    result = autoskill.internalize_codebase_as_skills(
        source_dir=source_dir,
        max_files=max_files,
        force=force,
        auto_activate=auto_activate,
        enable_release=enable_release,
        canary_percent=canary_percent,
        promote_min_runs=promote_min_runs,
        promote_max_failure_rate=promote_max_failure_rate,
    )
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/skills/import/toolsai-auto-skill', methods=['POST'])
def api_import_toolsai_auto_skill():
    """Import knowledge and experience from Toolsai/auto-skill repository."""
    from skills.management.auto_skill import AutoSkill
    data = request.get_json() or {}
    repo_url = data.get("repo_url", "https://github.com/Toolsai/auto-skill.git")
    local_path = data.get("local_path", "")
    notify_dc = _to_bool(data.get("notify_dc", False), False)
    autoskill = AutoSkill()
    result = autoskill.import_toolsai_auto_skill(
        repo_url=repo_url,
        local_path=local_path,
        cleanup=True,
        notify_dc=notify_dc,
    )
    return jsonify(result), (200 if result.get("success") else 400)

# ============== Iron Dome Dynamic Rules ==============
@app.route('/iron-dome/patterns', methods=['GET'])
def api_iron_dome_patterns_list():
    """List Iron Dome dynamic patterns."""
    from skills.evolution.skill_genesis import list_iron_dome_patterns
    include_static = _to_bool(request.args.get("include_static", "0"), False)
    include_disabled = _to_bool(request.args.get("include_disabled", "0"), False)
    limit = int(request.args.get("limit", 200))
    result = list_iron_dome_patterns(
        include_static=include_static,
        include_disabled=include_disabled,
        limit=limit,
    )
    return jsonify(result), (200 if result.get("success") else 500)


@app.route('/iron-dome/patterns', methods=['POST'])
def api_iron_dome_patterns_add():
    """Add or update an Iron Dome dynamic pattern."""
    from skills.evolution.skill_genesis import add_iron_dome_pattern
    data = request.get_json() or {}
    pattern = data.get("pattern", "")
    reason = data.get("reason", "")
    source = data.get("source", "tools_api")
    enabled = _to_bool(data.get("enabled", True), True)
    if not pattern:
        return jsonify({"error": "Missing 'pattern'"}), 400
    result = add_iron_dome_pattern(pattern, reason=reason, source=source, enabled=enabled)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/iron-dome/auto-harden', methods=['POST'])
def api_iron_dome_auto_harden():
    """Auto-harden Iron Dome scope from an incident text."""
    from skills.evolution.skill_genesis import auto_harden_iron_dome_scope
    data = request.get_json() or {}
    incident = data.get("incident", "") or data.get("text", "") or ""
    source = data.get("source", "tools_api")
    max_new = int(data.get("max_new", 3))
    if not incident:
        return jsonify({"error": "Missing 'incident' (or 'text')"}), 400
    result = auto_harden_iron_dome_scope(incident, source=source, max_new=max_new)
    return jsonify(result), 200


@app.route('/skills/knowledge/stats', methods=['GET'])
def api_skill_knowledge_stats():
    """Get AutoSkill knowledge base stats."""
    from skills.management.auto_skill import AutoSkill
    autoskill = AutoSkill()
    return jsonify(autoskill.stats()), 200


@app.route('/code/autofix', methods=['POST'])
def api_code_autofix():
    """Run autonomous code auto-fix loop for allowed paths."""
    from skills.management.code_autofix import autofix_codebase
    data = request.get_json() or {}
    target = data.get('target', 'code')
    max_files = int(data.get('max_files', 80))
    max_rounds = int(data.get('max_rounds', 2))
    dry_run = _to_bool(data.get('dry_run', False), False)
    include_tests = _to_bool(data.get('include_tests', False), False)
    task_hint = data.get('task_hint', '')
    internalize_skill = _to_bool(data.get('internalize_skill', False), False)
    internalize_name = data.get('internalize_name', 'casper-autofix-knowledge')
    result = autofix_codebase(
        target=target,
        max_files=max_files,
        max_rounds=max_rounds,
        dry_run=dry_run,
        include_tests=include_tests,
        task_hint=task_hint,
        internalize_skill=internalize_skill,
        internalize_name=internalize_name,
    )
    return jsonify(result), (200 if result.get("success", False) else 400)


@app.route('/code/skill-cycle', methods=['POST'])
def api_code_skill_cycle():
    """Run full automation cycle: code auto-fix + code-to-skill internalization."""
    from scripts.code_skill_cycle import run_cycle

    result = run_cycle()
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/collab/translate', methods=['POST'])
def api_collab_translate():
    """Tri-sage translation route (local-first with resilient fallbacks)."""
    from skills.bridge.tri_sage_collab import translate_text
    data = request.get_json() or {}
    text = data.get("text", "")
    target_lang = data.get("target_lang", "繁體中文")
    source_lang = data.get("source_lang", "auto")
    mode = data.get("mode", "auto")
    if not text:
        return jsonify({"error": "Missing 'text'"}), 400
    if len(text) > 500_000:
        return jsonify({"success": False, "error": "payload_too_large"}), 413
    result = translate_text(text, target_lang=target_lang, source_lang=source_lang, mode=mode)
    result = _guard_payload_fields(result)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/collab/music', methods=['POST'])
def api_collab_music():
    """Tri-sage music generation route (Melchior first, local fallback)."""
    from skills.bridge.tri_sage_collab import generate_music
    data = request.get_json() or {}
    prompt = data.get("prompt", "")
    duration_sec = int(data.get("duration_sec", 30))
    if not prompt:
        return jsonify({"error": "Missing 'prompt'"}), 400
    result = generate_music(prompt, duration_sec=duration_sec)
    result = _guard_payload_fields(result)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/collab/chat', methods=['POST'])
def api_collab_chat():
    """Tri-sage general chat/generation route (local-first, no hard remote dependency)."""
    import time as _time
    from skills.bridge import melchior_client
    data = request.get_json() or {}
    prompt = (data.get("prompt") or data.get("text") or "").strip()
    max_timeout = int(os.environ.get("MAGI_COLLAB_CHAT_MAX_TIMEOUT_SEC", "90"))
    timeout_sec = max(8, min(int(data.get("timeout_sec", 45)), max_timeout))
    total_budget_sec = timeout_sec + 2
    _t0 = _time.monotonic()
    allow_fallback = _to_bool(data.get("allow_fallback", True), True)
    allow_template_fallback = _to_bool(data.get("allow_template_fallback", True), True)
    if not prompt:
        return jsonify({"error": "Missing 'prompt'"}), 400
    primary_model = (data.get("model") or os.environ.get("MAGI_COLLAB_CHAT_MODEL") or TEXT_PRIMARY_MODEL).strip() or TEXT_PRIMARY_MODEL
    # Use InferenceGateway — handles oMLX/Ollama/remote fallback internally
    try:
        from skills.bridge.inference_gateway import InferenceGateway
        _gw = InferenceGateway()
        result = _gw.chat(prompt, task_type="general", timeout=timeout_sec, model=primary_model)
    except Exception as _gw_err:
        result = {"success": False, "error": f"gateway_error: {_gw_err}", "route": "gateway_failed"}
    if (not result.get("success")) and allow_template_fallback:
        preview = " ".join(prompt.split())[:180]
        result = {
            "success": True,
            "response": (
                "（系統降級回覆）目前分散式模型忙碌或逾時，"
                "已先保留你的請求。請稍後再試，或改用較短指令。\n\n"
                f"請求片段：{preview}"
            ),
            "route": "template_fallback",
            "degraded": True,
            "upstream_error": result.get("error", ""),
            "fallback_error": result.get("fallback_error", ""),
        }
    result = _guard_payload_fields(result)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/collab/transcribe', methods=['POST'])
def api_collab_transcribe():
    """Tri-sage transcription route."""
    t0 = time.monotonic()
    from skills.bridge.tri_sage_collab import transcribe_audio
    data = request.get_json() or {}
    audio_path = data.get("audio_path", "")
    if not audio_path:
        return jsonify({"error": "Missing 'audio_path'"}), 400
    _transcribe_timeout = int(os.environ.get("MAGI_TRANSCRIBE_TIMEOUT_SEC", "180") or "180")
    _t_ok, result = _run_with_timeout(transcribe_audio, _transcribe_timeout, audio_path, pool=_INFERENCE_EXECUTOR)
    if not _t_ok:
        result = {"success": False, "error": f"transcribe_timeout_{_transcribe_timeout}s", "error_type": "timeout"}
    try:
        _record_transcribe_metric(
            t0,
            success=bool(result.get("success")),
            speaker_count_estimate=int(result.get("speaker_count_estimate", 0) or 0),
            audio_path=str(audio_path or ""),
            provider=str(result.get("provider") or ""),
            error=str(result.get("error") or ""),
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1829, exc_info=True)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/council/core/pending', methods=['GET'])
def api_council_core_pending():
    """List pending core-change approvals raised by nightly council."""
    from skills.magi.council_approval import list_pending_core_changes

    limit = int(request.args.get("limit", 20))
    result = list_pending_core_changes(limit=limit)
    return jsonify(result), (200 if result.get("success") else 500)


@app.route('/council/core/approve', methods=['POST'])
def api_council_core_approve():
    """Approve a pending core change by approval id."""
    from skills.magi.council_approval import resolve_core_change

    data = request.get_json() or {}
    approval_id = data.get("approval_id", "").strip()
    approver = data.get("approver", "api").strip() or "api"
    note = data.get("note", "")
    if not approval_id:
        return jsonify({"error": "Missing 'approval_id'"}), 400
    result = resolve_core_change(approval_id, "approved", approver=approver, note=note)
    return jsonify(result), (200 if result.get("success") else 400)


@app.route('/council/core/reject', methods=['POST'])
def api_council_core_reject():
    """Reject a pending core change by approval id."""
    from skills.magi.council_approval import resolve_core_change

    data = request.get_json() or {}
    approval_id = data.get("approval_id", "").strip()
    approver = data.get("approver", "api").strip() or "api"
    note = data.get("note", "")
    if not approval_id:
        return jsonify({"error": "Missing 'approval_id'"}), 400
    result = resolve_core_change(approval_id, "rejected", approver=approver, note=note)
    return jsonify(result), (200 if result.get("success") else 400)


# ============== 記憶系統 (Memory) ==============
@app.route('/remember', methods=['POST'])
def api_remember():
    """Save content to vector memory database."""
    from skills.memory.mem_bridge import remember
    data = request.get_json() or {}
    content = data.get('content', '')
    source = data.get('source', 'openclaw')
    
    if not content:
        return jsonify({"error": "Missing 'content' parameter"}), 400
    
    try:
        remember(content, source)
        return jsonify({"success": True, "message": "Memory saved!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/recall', methods=['POST'])
def api_recall():
    """Recall relevant memories using vector search."""
    from skills.memory.mem_bridge import recall
    data = request.get_json() or {}
    query = data.get('query', '')
    top_k = data.get('top_k', 3)
    
    if not query:
        return jsonify({"error": "Missing 'query' parameter"}), 400
    
    try:
        results = recall(query, top_k)
        return jsonify({"memories": results or []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============== 律師事務所 (Law Firm) ==============
@app.route('/clients', methods=['GET'])
def api_query_clients():
    """Query clients by keyword."""
    from skills.law_firm.manage_clients import query_clients
    keyword = request.args.get('q', '')
    if not keyword:
        return jsonify({"error": "Missing 'q' parameter"}), 400
    return jsonify(query_clients(keyword))

@app.route('/clients', methods=['POST'])
def api_add_client():
    """Add a new client."""
    from skills.law_firm.manage_clients import add_client
    data = request.get_json() or {}
    code = data.get('code', '')
    name = data.get('name', '')
    contact = data.get('contact', '')
    phone = data.get('phone', '')
    address = data.get('address', '')
    if not all([code, name]):
        return jsonify({"error": "Missing 'code' or 'name'"}), 400
    return jsonify(add_client(code, name, contact, phone, address))

@app.route('/meetings', methods=['GET'])
def api_list_meetings():
    """List upcoming meetings."""
    from skills.law_firm.manage_meetings import list_meetings
    date_str = request.args.get('date', None)
    return jsonify(list_meetings(date_str))

@app.route('/meetings', methods=['POST'])
def api_book_meeting():
    """Book a new meeting."""
    from skills.law_firm.manage_meetings import book_meeting
    data = request.get_json() or {}
    title = data.get('title', '')
    start = data.get('start', '')
    duration = data.get('duration', 60)
    client = data.get('client', None)
    location = data.get('location', '事務所')
    if not all([title, start]):
        return jsonify({"error": "Missing 'title' or 'start'"}), 400
    return jsonify(book_meeting(title, start, duration, client, location))

# ============== 法律橋接 (Legal Bridge) ==============
@app.route('/legal/<skill_name>', methods=['POST'])
def api_legal_skill(skill_name):
    """Execute legacy legal automation scripts."""
    from skills.bridge.legal_bridge import execute_skill
    data = request.get_json() or {}
    args = data.get('args', [])
    tool_name = f"legal:{skill_name}"
    started = _start_tool_event(tool_name, {"args": args}, {"route": "legal_skill"})
    allowed, decision = _check_tool_access(tool_name, command_subject=tool_name)
    if not allowed:
        return _tool_denied_response(tool_name, started, decision, {"route": "legal_skill"})
    try:
        result = execute_skill(skill_name, args)
    except Exception as exc:
        return _tool_exception_response(
            tool_name,
            started,
            f"legal_skill_exception: {exc}",
            metadata={"route": "legal_skill"},
        )
    _finish_tool_event(tool_name, started, ok=True, status="handled", output_data=_tool_preview(result), metadata={"route": "legal_skill"})
    return jsonify({"result": result})

@app.route('/legal', methods=['GET'])
def api_legal_skills_list():
    """List available legal automation skills."""
    from skills.bridge.legal_bridge import SCRIPTS
    return jsonify({"skills": list(SCRIPTS.keys())})

# ============== 緊急通知 (Red Phone) ==============
@app.route('/alert', methods=['POST'])
def api_alert():
    """Send alert via LINE and Discord."""
    from skills.ops.red_phone import alert_admin
    data = request.get_json() or {}
    message = data.get('message', '')
    severity = data.get('severity', 'warning')
    if not message:
        return jsonify({"error": "Missing 'message'"}), 400
    topic_key = data.get('topic_key', '')
    result = alert_admin(message, severity, topic_key=topic_key)
    return jsonify(result)

# ============== Skill Definitions (OpenClaw Integration) ==============
@app.route('/definitions', methods=['GET'])
def api_definitions():
    """Return skill definitions for OpenClaw tool selection."""
    definitions_path = os.path.join(os.path.dirname(__file__), '..', 'skills', 'definitions.json')
    try:
        with open(definitions_path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        return jsonify(_sanitize_definitions_payload(payload))
    except FileNotFoundError:
        return jsonify({"error": "definitions.json not found"}), 404

# ============== 法扶冒煙測試 (只登入、不送出) ==============
@app.route('/laf/smoke_login', methods=['POST'])
def api_laf_smoke_login():
    """
    Formal-site smoke login test:
    - Only logs in (no report/submit actions)
    - Uses CODE wrapper skill `code-laf_automation_v2` (function call mode)
    """
    from skills.evolution.skill_genesis import run_skill_action
    data = request.get_json() or {}
    headless = _to_bool(data.get("headless", True), True)
    mock_mode = _to_bool(data.get("mock_mode", False), False)
    base_url = (data.get("base_url") or "").strip()
    timeout_sec = min(180, max(10, int(data.get("timeout_sec", 180))))  # cap 10-180s

    payload = {
        "headless": bool(headless),
        "mock_mode": bool(mock_mode),
        "base_url": base_url,
        "timeout_sec": int(data.get("fn_timeout_sec", 90) or 90),
    }
    task = "call smoke_login " + json.dumps(payload, ensure_ascii=False)
    tool_name = "skill:code-laf_automation_v2"
    started = _start_tool_event(tool_name, {"task": task}, {"route": "laf_smoke_login"})
    allowed, decision = _check_tool_access(
        tool_name,
        command_subject=tool_name,
        path_subject=_resolve_skill_action_path("code-laf_automation_v2"),
    )
    if not allowed:
        return _tool_denied_response(tool_name, started, decision, {"route": "laf_smoke_login"})
    try:
        result = run_skill_action(
            "code-laf_automation_v2",
            task,
            timeout_sec=timeout_sec,
            auto_repair=True,
            rollback_on_fail=True,
            auto_install_deps=True,
            route_key="laf:smoke_login",
        )
    except Exception as exc:
        return _tool_exception_response(
            tool_name,
            started,
            f"laf_smoke_login_exception: {exc}",
            metadata={"route": "laf_smoke_login"},
        )
    _finish_tool_event(
        tool_name,
        started,
        ok=bool(result.get("success")),
        status="handled" if result.get("success") else "error",
        output_data=_tool_preview(result),
        error=str(result.get("error") or ""),
        metadata={"route": "laf_smoke_login"},
    )
    return jsonify(result), (200 if result.get("success") else 400)

# ============== Audit Log (Iron Dome) ==============
@app.route('/api/audit_log', methods=['GET'])
def api_list_audit_log():
    """List recent audit log entries for one-click restore UI."""
    from datetime import datetime, timedelta
    from api.db_helper import get_cursor

    limit = request.args.get('limit', 50, type=int)
    days = request.args.get('days', 7, type=int)

    try:
        _audit_db_cfg = {
            "host": os.environ.get("DB_HOST", "127.0.0.1"),
            "user": os.environ.get("DB_USER", "casper_service"),
            "password": os.environ.get("DB_PASSWORD", ""),
            "database": "magi_brain",
            "use_pure": True,
        }
        with get_cursor(config=_audit_db_cfg, dictionary=True) as (_conn, cursor):
            since_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            cursor.execute("""
                SELECT id, agent_name, target_db, table_name, record_id,
                       operation, old_value, new_value, reason, executed_at
                FROM audit_log
                WHERE executed_at >= %s
                ORDER BY executed_at DESC
                LIMIT %s
            """, (since_date, limit))

            entries = cursor.fetchall()
            # Convert datetime to ISO format for JSON
            for entry in entries:
                if entry.get('executed_at'):
                    entry['executed_at'] = entry['executed_at'].isoformat()
                # Parse JSON strings if needed
                if entry.get('old_value') and isinstance(entry['old_value'], str):
                    try:
                        entry['old_value'] = json.loads(entry['old_value'])
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2067, exc_info=True)
                if entry.get('new_value') and isinstance(entry['new_value'], str):
                    try:
                        entry['new_value'] = json.loads(entry['new_value'])
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2072, exc_info=True)

        return jsonify({
            "entries": entries,
            "count": len(entries),
            "filters": {"limit": limit, "days": days}
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/audit_log/restore/<int:log_id>', methods=['POST'])
def api_restore_from_audit(log_id):
    """Restore data from audit log snapshot (one-click restore)."""
    from api.db_helper import get_cursor

    _restore_db_cfg = {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "user": os.environ.get("DB_USER", "casper_service"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": "magi_brain",
        "use_pure": True,
    }
    try:
        with get_cursor(config=_restore_db_cfg, dictionary=True) as (conn, cursor):
            # Get the audit log entry
            cursor.execute("SELECT * FROM audit_log WHERE id = %s", (log_id,))
            entry = cursor.fetchone()

            if not entry:
                return jsonify({"error": f"Audit log entry {log_id} not found"}), 404

            if not entry.get('old_value'):
                return jsonify({"error": "No old_value snapshot available for restore"}), 400

            old_value = entry['old_value']
            if isinstance(old_value, str):
                old_value = json.loads(old_value)

            target_db = entry.get('target_db', 'law_firm_data')
            table_name = entry['table_name']
            record_id = entry['record_id']

            # Build UPDATE query from old_value
            if not old_value:
                return jsonify({"error": "Empty old_value snapshot"}), 400

            # Whitelist validation to prevent SQL injection
            ALLOWED_DBS = {'law_firm_data', 'magi_brain'}
            import re as _re_sql
            if target_db not in ALLOWED_DBS:
                return jsonify({"error": f"Database '{target_db}' not in allowlist"}), 403
            if not _re_sql.match(r'^[A-Za-z_][A-Za-z0-9_]*$', table_name):
                return jsonify({"error": f"Invalid table name '{table_name}'"}), 403
            for key in old_value.keys():
                if not _re_sql.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key):
                    return jsonify({"error": f"Invalid column name '{key}'"}), 403

            # Switch to target database
            cursor.execute(f"USE `{target_db}`")

            # Build SET clause from old_value
            set_clauses = []
            values = []
            for key, value in old_value.items():
                if key != 'id':  # Don't update the primary key
                    set_clauses.append(f"`{key}` = %s")
                    values.append(value)

            if not set_clauses:
                return jsonify({"error": "No fields to restore"}), 400

            values.append(record_id)
            update_sql = f"UPDATE `{table_name}` SET {', '.join(set_clauses)} WHERE id = %s"

            cursor.execute(update_sql, values)
            conn.commit()

            # Log the restore operation
            cursor.execute("USE magi_brain")
            cursor.execute("""
                INSERT INTO audit_log (agent_name, target_db, table_name, record_id, operation, old_value, new_value, reason)
                VALUES (%s, %s, %s, %s, 'UPDATE', %s, %s, %s)
            """, (
                'RESTORE_UI',
                target_db,
                table_name,
                record_id,
                json.dumps(entry.get('new_value')),  # Current state becomes old
                json.dumps(old_value),  # Restoring to this
                f"One-click restore from audit_log entry #{log_id}"
            ))
            conn.commit()

            return jsonify({
                "success": True,
                "message": f"Restored {table_name}#{record_id} to snapshot from audit_log #{log_id}",
                "restored_data": old_value
            })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _tool_registry_search(query: str = "", num_results: int = 5, **_) -> dict:
    return search_web(query, num_results)


def _tool_registry_research(topic: str = "", depth: int = 3, **_) -> dict:
    return research_topic(topic, depth)


def _tool_registry_fetch(url: str = "", **_) -> dict:
    return fetch_url_content(url)


def _tool_registry_summarize(text: str = "", **kwargs) -> dict:
    return summarize_text(text, **kwargs)


def _tool_registry_vision(image_path: str = "", prompt: str = "Describe this image in detail", **kwargs) -> dict:
    return analyze_image(image_path, prompt)


def _bootstrap_tool_registry() -> None:
    try:
        from api.tools import get_global_tool_registry

        registry = get_global_tool_registry()
        globals()["TOOL_REGISTRY"] = registry
        if not registry.get("search"):
            registry.register_callable(
                "search",
                _tool_registry_search,
                description="Web search",
                permission_tag="tool:search",
                timeout_sec=30,
                metadata={"route": "/search"},
            )
        if not registry.get("research"):
            registry.register_callable(
                "research",
                _tool_registry_research,
                description="Deep web research",
                permission_tag="tool:research",
                timeout_sec=60,
                metadata={"route": "/research"},
            )
        if not registry.get("fetch"):
            registry.register_callable(
                "fetch",
                _tool_registry_fetch,
                description="Fetch URL content",
                permission_tag="tool:fetch",
                timeout_sec=30,
                metadata={"route": "/fetch"},
            )
        if not registry.get("summarize"):
            registry.register_callable(
                "summarize",
                _tool_registry_summarize,
                description="Summarize text",
                permission_tag="tool:summarize",
                timeout_sec=90,
                metadata={"route": "/summarize"},
            )
        if not registry.get("vision"):
            registry.register_callable(
                "vision",
                _tool_registry_vision,
                description="Vision analysis",
                permission_tag="tool:vision",
                timeout_sec=90,
                metadata={"route": "/vision"},
            )
    except Exception:
        logging.getLogger("tools_api").debug("tool registry bootstrap skipped", exc_info=True)


_bootstrap_tool_registry()

def _warmup_background():
    import time
    time.sleep(3)  # wait for server to begin listening
    try:
        from skills.bridge.grounded_ai import _classify_query_tier
        logger = logging.getLogger("tools_api")
        logger.info("🔧 System Background Warmup: Preloading embedding anchors (Phase D)...")
        # Preload SIMPLE / COMPLEX routes
        _classify_query_tier("早安你好")
        _classify_query_tier("幫我查一個最高法院113年度的判決與存證信函")
        logger.info("✅ System Background Warmup Complete: models loaded in RAM.")
    except Exception as e:
        logging.getLogger("tools_api").debug(f"Warmup skipped: {e}")

import threading
threading.Thread(target=_warmup_background, daemon=True).start()

if __name__ == '__main__':
    logging.getLogger("tools_api").info("MAGI Tools API starting on http://localhost:5003")
    app.run(host='127.0.0.1', port=5003, debug=False, threaded=True)
