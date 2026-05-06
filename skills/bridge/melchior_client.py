"""
MELCHIOR CLIENT MODULE
======================
Provides reliable API access to Melchior for code/chat/vision,
with layered fallbacks to direct Ollama endpoints.
"""

import base64
import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

from api.model_config import (
    CODE_MODEL as DEFAULT_CODE_MODEL,
    EMBED_MODEL as DEFAULT_EMBED_MODEL,
    GENERAL_MODEL as DEFAULT_GENERAL_MODEL,
    OCR_MODEL as DEFAULT_OCR_MODEL,
    SUMMARY_MODEL as DEFAULT_SUMMARY_MODEL,
    TEXT_PRIMARY_MODEL,
    TEXT_REVIEW_MODEL,
    VISION_MODEL as DEFAULT_VISION_MODEL,
    resolve_text_model,
)

_logger = logging.getLogger("melchior_client")
logger = _logger  # backward compat: some callsites use 'logger'
_AUDIT_MARKER_20260310 = True  # marker for code-version verification

# =============================================================================
# oMLX Configuration (local Apple Silicon MLX inference)
# =============================================================================
OMLX_ENABLED = os.environ.get("MAGI_OMLX_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
OMLX_CHAT_HOST = os.environ.get("MAGI_OMLX_CHAT_HOST", os.environ.get("MAGI_OMLX_HOST", "127.0.0.1"))
OMLX_CHAT_PORT = int(os.environ.get("MAGI_OMLX_CHAT_PORT", os.environ.get("MAGI_OMLX_PORT", "8080")))
OMLX_CHAT_BASE = (os.environ.get("MAGI_OMLX_CHAT_URL") or f"http://{OMLX_CHAT_HOST}:{OMLX_CHAT_PORT}").rstrip("/")
OMLX_EMBED_HOST = os.environ.get("MAGI_OMLX_EMBED_HOST", OMLX_CHAT_HOST)
OMLX_EMBED_PORT = int(os.environ.get("MAGI_OMLX_EMBED_PORT", "8081"))
OMLX_EMBED_BASE = (os.environ.get("MAGI_OMLX_EMBED_URL") or f"http://{OMLX_EMBED_HOST}:{OMLX_EMBED_PORT}").rstrip("/")
OMLX_VISION_HOST = os.environ.get("MAGI_OMLX_VISION_HOST", OMLX_CHAT_HOST)
# GLM-OCR retired (2026-04-08): macOS Vision OCR is now primary OCR engine.
# Vision/multimodal tasks route to Gemma 4 on main port (8080).
OMLX_VISION_PORT = int(os.environ.get("MAGI_OMLX_VISION_PORT", str(OMLX_CHAT_PORT)))
OMLX_VISION_BASE = (os.environ.get("MAGI_OMLX_VISION_URL") or f"http://{OMLX_VISION_HOST}:{OMLX_VISION_PORT}").rstrip("/")
OMLX_HOST = OMLX_CHAT_HOST
OMLX_PORT = OMLX_CHAT_PORT
OMLX_BASE = OMLX_CHAT_BASE
OMLX_SUMMARY_MODEL = os.environ.get("MAGI_OMLX_SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL)
OMLX_VISION_MODEL = os.environ.get("MAGI_OMLX_VISION_MODEL", DEFAULT_VISION_MODEL)
OMLX_OCR_MODEL = os.environ.get("MAGI_OMLX_OCR_MODEL", DEFAULT_OCR_MODEL)
OMLX_CODE_MODEL = os.environ.get("MAGI_OMLX_CODE_MODEL", DEFAULT_CODE_MODEL)
OMLX_EMBED_MODEL = os.environ.get("MAGI_OMLX_EMBED_MODEL", DEFAULT_EMBED_MODEL)
OMLX_GENERAL_MODEL = os.environ.get("MAGI_OMLX_GENERAL_MODEL", DEFAULT_GENERAL_MODEL)
OMLX_LOCAL_CHAT_MODEL = os.environ.get("MAGI_OMLX_LOCAL_CHAT_MODEL", TEXT_REVIEW_MODEL)
MAGI_RUNTIME_DIR = os.environ.get(
    "MAGI_RUNTIME_DIR",
    os.path.join(os.path.expanduser("~"), "Library", "Application Support", "MAGI"),
)
OMLX_WATCHDOG_STATE_PATH = os.environ.get(
    "MAGI_OMLX_WATCHDOG_STATE_PATH",
    os.path.join(MAGI_RUNTIME_DIR, "omlx_watchdog_state.json"),
)
OMLX_WATCHDOG_CACHE_TTL_SEC = float(os.environ.get("MAGI_OMLX_WATCHDOG_CACHE_TTL_SEC", "2"))
OMLX_WATCHDOG_STALE_SEC = int(os.environ.get("MAGI_OMLX_WATCHDOG_STALE_SEC", "900"))
_OMLX_MODELS_CACHE = {"ts": 0.0, "models": []}
_OMLX_BASE_MODELS_CACHE: dict[str, dict] = {}
_OMLX_WATCHDOG_CACHE = {"ts": 0.0, "data": {}}
_MODEL_CACHE_LOCK = threading.Lock()  # guards _MODEL_CACHE, _OMLX_MODELS_CACHE, _OPENAI_MODELS_CACHE

# Model alias: Ollama names → oMLX names (for seamless migration)
_OMLX_MODEL_ALIAS = {
    "nomic-embed-text": OMLX_EMBED_MODEL,
    "Qwen2.5-Coder-14B-Instruct-4bit": TEXT_PRIMARY_MODEL,
    "Qwen3.5-9B-4bit": TEXT_PRIMARY_MODEL,
    "qwen3.5-9b-4bit": TEXT_PRIMARY_MODEL,
    "Qwen3.5-9B-Uncensored-mxfp4": TEXT_PRIMARY_MODEL,
    "gemma-4": TEXT_PRIMARY_MODEL,
    "gemma4": TEXT_PRIMARY_MODEL,
    "gemma-4-26b": TEXT_PRIMARY_MODEL,
    "gemma-4-26b-a4b": TEXT_PRIMARY_MODEL,
    "gemma-4-26b-a4b-it-4bit": TEXT_PRIMARY_MODEL,
    "gemma-4-e2b-it-local-bf16": TEXT_PRIMARY_MODEL,
    "gemma-4-e4b-it-bf16": TEXT_PRIMARY_MODEL,
    "gemma-4-26b-a4b-it-4bit": TEXT_PRIMARY_MODEL,
    "gemma4:26b": TEXT_PRIMARY_MODEL,
    "gemma-3-12b": "gemma-3-12b-it-4bit",
    "gemma3-12b": "gemma-3-12b-it-4bit",
}

# =============================================================================
# Configuration
# =============================================================================
try:
    from api.routing.node_registry import get_node_ip as _get_node_ip
    MELCHIOR_HOST = os.environ.get("MELCHIOR_HOST") or _get_node_ip("melchior") or "100.116.54.16"
except Exception:
    MELCHIOR_HOST = os.environ.get("MELCHIOR_HOST", "100.116.54.16")
MELCHIOR_PORT = int(os.environ.get("MELCHIOR_PORT", "5002"))
MELCHIOR_OLLAMA_PORT = int(os.environ.get("MELCHIOR_OLLAMA_PORT", "11434"))
MELCHIOR_API_PORT = int(os.environ.get("MELCHIOR_API_PORT", "8080"))

MELCHIOR_BASE_URL = f"http://{MELCHIOR_HOST}:{MELCHIOR_PORT}"
MELCHIOR_OLLAMA_BASE = f"http://{MELCHIOR_HOST}:{MELCHIOR_OLLAMA_PORT}"
MELCHIOR_OPENAI_V1_BASE = f"http://{MELCHIOR_HOST}:{MELCHIOR_API_PORT}/v1"
MELCHIOR_OPENAI_V1_FALLBACK = f"http://{MELCHIOR_HOST}:{MELCHIOR_OLLAMA_PORT}/v1"

ENDPOINTS = {
    "code": f"{MELCHIOR_BASE_URL}/api/code",
    "chat": f"{MELCHIOR_BASE_URL}/api/chat",
    "vision": f"{MELCHIOR_BASE_URL}/api/vision",
    "generate": f"{MELCHIOR_BASE_URL}/api/generate",
    "capabilities": f"{MELCHIOR_BASE_URL}/api/capabilities",
    "warmup": f"{MELCHIOR_BASE_URL}/api/warmup",
}

TIMEOUT = int(os.environ.get("MELCHIOR_TIMEOUT", "900"))
# Reduced default connect timeout from 3s to 1s for near-instant fallback
CONNECT_TIMEOUT_SEC = int(os.environ.get("MELCHIOR_CONNECT_TIMEOUT_SEC", "1"))
SESSION = requests.Session()
MODEL_CACHE_TTL_SEC = int(os.environ.get("MELCHIOR_MODEL_CACHE_TTL_SEC", "45"))
MELCHIOR_NUM_CTX = int(os.environ.get("MELCHIOR_NUM_CTX", "6144"))
MELCHIOR_TEMPERATURE = float(os.environ.get("MELCHIOR_TEMPERATURE", "0.2"))
MELCHIOR_KEEP_ALIVE = os.environ.get("MELCHIOR_KEEP_ALIVE", "10m").strip() or "10m"
PREFERRED_DISTRIBUTED_MODELS = [
    x.strip()
    for x in os.environ.get(
        "MELCHIOR_PREFERRED_MODELS",
        TEXT_PRIMARY_MODEL,
    ).split(",")
    if x.strip()
]
_MODEL_CACHE = {
    "ts": 0.0,
    "models": [],
}
_CAP_CACHE = {"ts": 0.0, "data": {}}
_OPENAI_MODELS_CACHE = {"ts": 0.0, "models": []}

# ── Circuit Breaker: skip remote attempts after repeated failures ──
_CIRCUIT_BREAKER = {
    "consecutive_failures": 0,
    "tripped_at": 0.0,          # monotonic timestamp of last trip
    "last_failure_reason": "",
    "cooldown_level": 0,        # exponential backoff level
}
CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get("MELCHIOR_CB_THRESHOLD", "3"))
CIRCUIT_BREAKER_COOLDOWN_SEC = int(os.environ.get("MELCHIOR_CB_COOLDOWN_SEC", "180"))
_CB_LOCK = threading.Lock()  # guards _CIRCUIT_BREAKER state
_CB_COOLDOWN_BASE_SEC = 30  # exponential backoff: 30s → 90s → 180s (capped)
_OLLAMA_PROBE_TIMEOUT_SEC = float(os.environ.get("MELCHIOR_OLLAMA_PROBE_TIMEOUT_SEC", "2.0"))
CAP_CACHE_TTL_SEC = int(os.environ.get("MELCHIOR_CAP_CACHE_TTL_SEC", "10"))
OPENAI_MODELS_CACHE_TTL_SEC = int(os.environ.get("MELCHIOR_OPENAI_MODELS_CACHE_TTL_SEC", "15"))
MELCHIOR_MAX_TOKENS = int(os.environ.get("MELCHIOR_MAX_TOKENS", "1024"))
MELCHIOR_ROUTE_PREFER_OPENAI = os.environ.get("MELCHIOR_ROUTE_PREFER_OPENAI", "1").strip().lower() in {"1", "true", "yes", "on"}
MELCHIOR_PRIMARY_TRY_TIMEOUT_SEC = int(os.environ.get("MELCHIOR_PRIMARY_TRY_TIMEOUT_SEC", "35"))
MELCHIOR_FALLBACK_TRY_TIMEOUT_SEC = int(os.environ.get("MELCHIOR_FALLBACK_TRY_TIMEOUT_SEC", "28"))
MELCHIOR_OPENAI_PRIMARY_TIMEOUT_SEC = int(os.environ.get("MELCHIOR_OPENAI_PRIMARY_TIMEOUT_SEC", "35"))
MELCHIOR_LOCAL_FIRST_DEFAULT = os.environ.get("MELCHIOR_LOCAL_FIRST_DEFAULT", "1").strip().lower() in {"1", "true", "yes", "on"}
MELCHIOR_LOCAL_FIRST_TIMEOUT_SEC = int(os.environ.get("MELCHIOR_LOCAL_FIRST_TIMEOUT_SEC", "300"))
MELCHIOR_LOCAL_FALLBACK_TIMEOUT_SEC = int(os.environ.get("MELCHIOR_LOCAL_FALLBACK_TIMEOUT_SEC", "3600"))
MELCHIOR_DISTRIBUTED_TIMEOUT_CAP_SEC = int(os.environ.get("MELCHIOR_DISTRIBUTED_TIMEOUT_CAP_SEC", "90"))
MAGI_AVOID_DISTRIBUTED_DEFAULT = os.environ.get("MAGI_AVOID_DISTRIBUTED", "1").strip().lower() in {"1", "true", "yes", "on"}

DEGRADED_FALLBACK_MODELS = [
    x.strip()
    for x in os.environ.get(
        "MELCHIOR_DEGRADED_MODELS",
        TEXT_PRIMARY_MODEL,
    ).split(",")
    if x.strip()
]


def _avoid_distributed() -> bool:
    """
    Runtime switch: keep distributed inference disabled unless explicitly turned off.
    This is checked dynamically so ops can flip env without code changes.
    """
    single = os.environ.get("MAGI_SINGLE_MACHINE")
    if single is not None and str(single).strip().lower() in {"1", "true", "yes", "on"}:
        return True
    raw = os.environ.get("MAGI_AVOID_DISTRIBUTED")
    if raw is None:
        return bool(MAGI_AVOID_DISTRIBUTED_DEFAULT)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _result(ok: bool, response: str = "", error: str = "") -> Dict[str, str]:
    return {
        "success": ok,
        "response": response,
        "error": error,
    }


def _start_deadline(total_timeout_sec: int) -> float:
    return time.monotonic() + max(3, int(total_timeout_sec))


def _remaining(deadline: float, floor: int = 1) -> int:
    return max(int(deadline - time.monotonic()), int(floor))


def _local_fallback_timeout(remaining_sec: int, floor: int = 6) -> int:
    cap = max(12, int(MELCHIOR_LOCAL_FALLBACK_TIMEOUT_SEC))
    return max(int(floor), min(cap, max(int(floor), int(remaining_sec))))


def _post_json(url: str, payload: dict, timeout: int):
    # Use a short connect timeout so unreachable Melchior won't stall the whole workflow.
    ct = max(1, min(int(CONNECT_TIMEOUT_SEC), int(timeout)))
    _CONNECTION_ERRORS = (
        ConnectionError, ConnectionResetError, ConnectionRefusedError,
    )
    max_retries = 1  # 1 retry on connection errors (GPU crash recovery)
    for attempt in range(max_retries + 1):
        try:
            resp = SESSION.post(url, json=payload, timeout=(ct, int(timeout)))
            # --- DEBUG: log role alternation issues for chat/completions 400 ---
            if resp.status_code == 400 and "chat/completions" in url:
                import traceback
                msgs = payload.get("messages") or []
                roles = [m.get("role", "?") for m in msgs if isinstance(m, dict)]
                _logger.warning(
                    "🔴 _post_json 400 on chat/completions: roles=%s model=%s caller=%s",
                    roles, payload.get("model", "?"),
                    "".join(traceback.format_stack()[-4:-1]).strip(),
                )
            # --- END DEBUG ---
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {"raw": data}
        except _CONNECTION_ERRORS as e:
            if attempt < max_retries:
                logger.info("_post_json connection error (retry %d): %s %s", attempt + 1, url[:60], type(e).__name__)
                import time as _time
                _time.sleep(5)  # wait for oMLX launchd restart (~3-5s)
                continue
            logger.warning("_post_json failed after retry: %s %s", url[:80], e)
            return {"error": str(e)[:300], "_failed": True}
        except Exception as e:
            # Non-connection errors (timeout, HTTP errors): don't retry
            logger.warning("_post_json failed: %s %s", url[:80], e)
            return {"error": str(e)[:300], "_failed": True}
    return {"error": "max_retries_exhausted", "_failed": True}

def _get_json(url: str, timeout: int = 3) -> dict:
    ct = max(1, min(int(CONNECT_TIMEOUT_SEC), int(timeout)))
    try:
        resp = SESSION.get(url, timeout=(ct, int(timeout)))
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except Exception as e:
        logger.warning("_get_json failed: %s %s", url[:80], e)
        return {"error": str(e)[:300], "_failed": True}


# =============================================================================
# oMLX Backend Functions
# =============================================================================
_OMLX_CHAT_CIRCUIT = {"failures": 0, "tripped_at": 0.0}
_OMLX_EMBED_CIRCUIT = {"failures": 0, "tripped_at": 0.0}
_OMLX_VISION_CIRCUIT = {"failures": 0, "tripped_at": 0.0}
_OMLX_CHAT_LOCK = threading.Lock()    # guards _OMLX_CHAT_CIRCUIT state
_OMLX_EMBED_LOCK = threading.Lock()   # guards _OMLX_EMBED_CIRCUIT state
_OMLX_VISION_LOCK = threading.Lock()  # guards _OMLX_VISION_CIRCUIT state


def _get_omlx_watchdog_state(force_refresh: bool = False) -> dict:
    path = str(OMLX_WATCHDOG_STATE_PATH or "").strip()
    if not path:
        return {}
    now = time.time()
    if (
        not force_refresh
        and _OMLX_WATCHDOG_CACHE.get("data")
        and (now - float(_OMLX_WATCHDOG_CACHE.get("ts") or 0.0)) < max(0.5, OMLX_WATCHDOG_CACHE_TTL_SEC)
    ):
        return dict(_OMLX_WATCHDOG_CACHE["data"])
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _OMLX_WATCHDOG_CACHE["ts"] = now
            _OMLX_WATCHDOG_CACHE["data"] = dict(data)
            return dict(data)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 244, exc_info=True)
    _OMLX_WATCHDOG_CACHE["ts"] = now
    _OMLX_WATCHDOG_CACHE["data"] = {}
    return {}


def _omlx_watchdog_blocks_service() -> bool:
    state = _get_omlx_watchdog_state(force_refresh=False)
    if not state:
        return False
    now = time.time()
    updated_at = float(state.get("updated_at") or 0.0)
    if updated_at <= 0 or (now - updated_at) > max(60, OMLX_WATCHDOG_STALE_SEC):
        return False
    suspend_until = float(state.get("suspend_until") or 0.0)
    status = str(state.get("status") or "").strip().lower()
    return suspend_until > now and status in {"restarting", "cooldown", "blocked", "recovering"}

def _omlx_service_available(circuit: dict, lock: threading.Lock = None) -> bool:
    if not OMLX_ENABLED:
        return False
    if _omlx_watchdog_blocks_service():
        return False
    _lk = lock or _OMLX_CHAT_LOCK
    with _lk:
        threshold = 10 if circuit is _OMLX_VISION_CIRCUIT else 6
        if circuit["failures"] >= threshold:
            if time.monotonic() - circuit["tripped_at"] < 120:
                return False
            circuit["failures"] = 0
            circuit["tripped_at"] = 0.0
    return True


def _omlx_available() -> bool:
    """Backward-compatible chat availability check."""
    return _omlx_service_available(_OMLX_CHAT_CIRCUIT, _OMLX_CHAT_LOCK)


def _omlx_embed_available() -> bool:
    return _omlx_service_available(_OMLX_EMBED_CIRCUIT, _OMLX_EMBED_LOCK)


def _omlx_vision_available() -> bool:
    return _omlx_service_available(_OMLX_VISION_CIRCUIT, _OMLX_VISION_LOCK)


def _omlx_ok(circuit: dict, lock: threading.Lock = None):
    _lk = lock or _OMLX_CHAT_LOCK
    with _lk:
        circuit["failures"] = 0
        circuit["tripped_at"] = 0.0


def _omlx_fail(circuit: dict, lock: threading.Lock = None):
    _lk = lock or _OMLX_CHAT_LOCK
    # Unified oMLX server: model LRU swaps cause transient 507/503 errors
    # that are NOT real failures. Use higher threshold for all circuits.
    threshold = 10 if circuit is _OMLX_VISION_CIRCUIT else 6
    with _lk:
        circuit["failures"] = circuit.get("failures", 0) + 1
        if circuit["failures"] >= threshold:
            circuit["tripped_at"] = time.monotonic()


def list_omlx_models(force_refresh: bool = False) -> List[str]:
    """List models available on the local oMLX server."""
    if not OMLX_ENABLED:
        return []
    now = time.time()
    with _MODEL_CACHE_LOCK:
        if not force_refresh and _OMLX_MODELS_CACHE["models"] and (now - _OMLX_MODELS_CACHE["ts"]) < 30:
            return list(_OMLX_MODELS_CACHE["models"])
    try:
        data = _get_json(f"{OMLX_CHAT_BASE}/v1/models", timeout=3)
        models = []
        for it in (data or {}).get("data", []):
            if isinstance(it, dict) and it.get("id"):
                models.append(str(it["id"]).strip())
        with _MODEL_CACHE_LOCK:
            _OMLX_MODELS_CACHE["ts"] = now
            _OMLX_MODELS_CACHE["models"] = sorted(set(models))
            return list(_OMLX_MODELS_CACHE["models"])
    except Exception:
        return []


def list_omlx_models_for_base(base_url: str, force_refresh: bool = False) -> List[str]:
    """List models available on a specific oMLX base URL."""
    if not OMLX_ENABLED:
        return []
    base = (base_url or OMLX_CHAT_BASE).rstrip("/")
    now = time.time()
    with _MODEL_CACHE_LOCK:
        cached = _OMLX_BASE_MODELS_CACHE.get(base) or {}
        if not force_refresh and cached.get("models") and (now - float(cached.get("ts") or 0)) < 30:
            return list(cached.get("models") or [])
    try:
        data = _get_json(f"{base}/v1/models", timeout=3)
        models = []
        for it in (data or {}).get("data", []):
            if isinstance(it, dict) and it.get("id"):
                models.append(str(it["id"]).strip())
        models = sorted(set(models))
        with _MODEL_CACHE_LOCK:
            _OMLX_BASE_MODELS_CACHE[base] = {"ts": now, "models": models}
        return models
    except Exception:
        return []


def _resolve_omlx_chat_model(raw_model: str, *, available_models: Optional[List[str]] = None) -> str:
    """Resolve requested chat model to an actually available local oMLX model."""
    requested = _OMLX_MODEL_ALIAS.get((raw_model or "").strip(), (raw_model or "").strip())
    requested = resolve_text_model(requested, available=available_models)
    if not requested:
        requested = OMLX_GENERAL_MODEL

    models = list(available_models or list_omlx_models())
    if not models:
        return requested
    if requested in models:
        return requested

    requested_lower = requested.lower()
    for model_name in models:
        lower = model_name.lower()
        if requested_lower and (requested_lower == lower or requested_lower in lower or lower.startswith(requested_lower)):
            return model_name

    # When the configured chat model is absent, use the best available local chat model
    # instead of triggering a guaranteed 404 and falling straight into fallback mode.
    if "gemma" in requested_lower:
        for model_name in models:
            lower = model_name.lower()
            if "gemma-4" in lower:
                return model_name
        for model_name in models:
            lower = model_name.lower()
            if any(token in lower for token in ("qwen", "gemma", "coder")):
                return model_name

    return models[0]


def _ensure_alternating_roles(messages: list) -> list:
    """Ensure messages follow user/assistant alternation required by strict chat templates.

    Some models (e.g. Gemma family) enforce strict role alternation:
    - First non-system message must be 'user'
    - Roles must alternate user/assistant/user/assistant/...
    - Consecutive same-role messages are merged

    This helper silently fixes violations instead of sending a 400-triggering request.
    """
    if not messages:
        return messages

    # Separate optional system prefix and merge multiple system messages into one
    system_msgs = []
    body = list(messages)
    while body and body[0].get("role") == "system":
        system_msgs.append(body.pop(0))

    # Merge consecutive system messages into a single one (prevents 400 from strict templates)
    if len(system_msgs) > 1:
        combined = "\n".join(
            str(m.get("content", "")) for m in system_msgs if m.get("content")
        )
        system_msgs = [{"role": "system", "content": combined}] if combined else []

    if not body:
        return messages  # nothing to fix

    # Merge consecutive same-role messages
    merged: list[dict] = []
    for m in body:
        role = m.get("role", "user")
        content = m.get("content", "")
        if merged and merged[-1].get("role") == role:
            # Merge content
            prev = merged[-1].get("content", "")
            if isinstance(prev, str) and isinstance(content, str):
                merged[-1]["content"] = prev + "\n" + content
            else:
                merged[-1]["content"] = content  # keep last for non-string
        else:
            merged.append(dict(m))

    # Ensure first message is 'user' (strict Gemma template requirement)
    if merged and merged[0].get("role") != "user":
        merged.insert(0, {"role": "user", "content": "(context follows)"})

    return system_msgs + merged


def _chat_omlx(
    prompt: str,
    model: str = "",
    timeout: int = 120,
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    system_prompt: str = "",
    images: List[str] = None,
    base_url: str = "",
    circuit: dict = None,
    lock: threading.Lock = None,
) -> dict:
    """
    Chat via local oMLX server (OpenAI-compatible /v1/chat/completions).
    Supports text and vision (base64 images in messages).
    Use base_url/circuit/lock to route to a specific oMLX instance (e.g. vision server).
    """
    _circuit = circuit or _OMLX_CHAT_CIRCUIT
    _lock = lock or _OMLX_CHAT_LOCK
    _base = (base_url or OMLX_CHAT_BASE).rstrip("/")

    if not _omlx_service_available(_circuit, _lock):
        return _result(False, "", "omlx_chat_disabled_or_circuit_open")

    raw_model = (model or OMLX_GENERAL_MODEL).strip()
    # When routing to a non-chat oMLX server (e.g. vision on port 8082),
    # skip model resolution — it queries port 8080's model list, causing
    # the resolved model to be "gemma-4-..." which triggers 404 on the
    # vision server that only serves GLM-OCR.
    if base_url and base_url.rstrip("/") != OMLX_CHAT_BASE:
        use_model = raw_model
        available_on_base = list_omlx_models_for_base(_base)
        if available_on_base and use_model not in available_on_base:
            return _result(
                False,
                "",
                f"omlx_model_unavailable:{use_model}; available={','.join(available_on_base)}",
            )
    else:
        use_model = _resolve_omlx_chat_model(raw_model)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    if images:
        content_parts = [{"type": "text", "text": prompt}]
        for img_b64 in images:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            })
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": prompt})

    # Ensure strict role alternation for Gemma family (which enforces role alternation)
    messages = _ensure_alternating_roles(messages)

    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "top_p": 0.88,
        "stream": False,
    }
    # repetition_penalty: oMLX supports it, Ollama /v1/ does not
    if OMLX_CHAT_PORT != 11434:
        payload["repetition_penalty"] = 1.1
    # Qwen3.5 needs thinking mode disabled
    if "qwen" in use_model.lower() and "3.5" in use_model:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    try:
        data = _post_json(f"{_base}/v1/chat/completions", payload, timeout=max(10, int(timeout)))
        choices = data.get("choices") or []
        text = ""
        if choices and isinstance(choices, list):
            c0 = choices[0] if isinstance(choices[0], dict) else {}
            msg = (c0.get("message") or {}) if isinstance(c0.get("message"), dict) else {}
            text = (msg.get("content") or "").strip()
        if not text:
            _omlx_fail(_circuit, _lock)
            return _result(False, "", "empty_omlx_response")
        _omlx_ok(_circuit, _lock)
        ok = _result(True, text, "")
        ok["model"] = use_model
        ok["route"] = "omlx"
        return ok
    except Exception as e:
        _omlx_fail(_circuit, _lock)
        return _result(False, "", f"omlx_failed: {e}")


# ── Embedding TTL cache ──────────────────────────────────────────────
_EMBED_CACHE = {}       # type: dict  # {text_hash: (vector, timestamp)}
_EMBED_CACHE_TTL = 3600  # 1 hour
_EMBED_CACHE_MAX = 500
_EMBED_CACHE_STATS = {"hits": 0, "misses": 0}
_EMBED_CACHE_LOCK = threading.Lock()


def embed_omlx(text: str, model: str = "", retries: int = 2) -> list:
    """
    Get embedding vector from oMLX /v1/embeddings endpoint.
    Returns list of floats, or empty list on failure.
    Retries on timeout (oMLX max_num_seqs=1 may queue requests).
    Results are cached with a 1-hour TTL (max 500 entries).
    """
    # --- TTL cache lookup ---
    cache_key = hash(text)
    now = time.time()
    with _EMBED_CACHE_LOCK:
        cached = _EMBED_CACHE.get(cache_key)
        if cached is not None:
            vec, ts = cached
            if now - ts < _EMBED_CACHE_TTL:
                _EMBED_CACHE_STATS["hits"] += 1
                return list(vec)
            else:
                del _EMBED_CACHE[cache_key]

    if not _omlx_embed_available():
        return []
    use_model = (model or OMLX_EMBED_MODEL).strip()
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            data = _post_json(
                f"{OMLX_EMBED_BASE}/v1/embeddings",
                {"model": use_model, "input": text},
                timeout=60,
            )
            emb_data = (data or {}).get("data", [])
            if emb_data and isinstance(emb_data, list):
                _omlx_ok(_OMLX_EMBED_CIRCUIT, _OMLX_EMBED_LOCK)
                result_vec = emb_data[0].get("embedding", [])
                # --- Store in cache ---
                with _EMBED_CACHE_LOCK:
                    _EMBED_CACHE_STATS["misses"] += 1
                    _EMBED_CACHE[cache_key] = (result_vec, time.time())
                    # Evict oldest 100 if cache exceeds max
                    if len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
                        sorted_keys = sorted(
                            _EMBED_CACHE.keys(),
                            key=lambda k: _EMBED_CACHE[k][1],
                        )
                        for old_key in sorted_keys[:100]:
                            del _EMBED_CACHE[old_key]
                return result_vec
            return []
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.0 * attempt)
    _omlx_fail(_OMLX_EMBED_CIRCUIT, _OMLX_EMBED_LOCK)
    _logger.warning("omlx embed failed after %d attempts: %s", retries, last_err)
    return []


def embed_omlx_batch(texts: List[str], model: str = "", retries: int = 2) -> List[list]:
    """
    Batch embedding via oMLX /v1/embeddings.
    Returns list of embedding vectors (same order as input).
    Retries on timeout (oMLX max_num_seqs=1 may queue requests).
    """
    if not texts:
        return []
    if not _omlx_embed_available():
        return [[] for _ in texts]
    use_model = (model or OMLX_EMBED_MODEL).strip()
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            data = _post_json(
                f"{OMLX_EMBED_BASE}/v1/embeddings",
                {"model": use_model, "input": texts},
                timeout=90,
            )
            emb_data = (data or {}).get("data", [])
            if emb_data and isinstance(emb_data, list) and len(emb_data) == len(texts):
                _omlx_ok(_OMLX_EMBED_CIRCUIT, _OMLX_EMBED_LOCK)
                return [item.get("embedding", []) for item in emb_data]
            return [[] for _ in texts]
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * attempt)
    _omlx_fail(_OMLX_EMBED_CIRCUIT, _OMLX_EMBED_LOCK)
    _logger.warning("omlx batch embed failed after %d attempts: %s", retries, last_err)
    return [[] for _ in texts]


def _cb_trip(reason: str = "") -> None:
    """Trip the circuit breaker after a remote failure."""
    import random
    with _CB_LOCK:
        _CIRCUIT_BREAKER["consecutive_failures"] = _CIRCUIT_BREAKER.get("consecutive_failures", 0) + 1
        if _CIRCUIT_BREAKER["consecutive_failures"] >= CIRCUIT_BREAKER_THRESHOLD:
            level = _CIRCUIT_BREAKER.get("cooldown_level", 0)
            # Exponential backoff with jitter: 30s → 90s → 180s (capped)
            raw_cooldown = min(_CB_COOLDOWN_BASE_SEC * (3 ** level), CIRCUIT_BREAKER_COOLDOWN_SEC)
            jitter = random.uniform(0.8, 1.2)
            _CIRCUIT_BREAKER["tripped_at"] = time.monotonic()
            _CIRCUIT_BREAKER["effective_cooldown"] = raw_cooldown * jitter
            _CIRCUIT_BREAKER["cooldown_level"] = level + 1
            _CIRCUIT_BREAKER["last_failure_reason"] = (reason or "unknown")[:200]


def _cb_reset() -> None:
    """Reset circuit breaker after a successful remote call."""
    with _CB_LOCK:
        _CIRCUIT_BREAKER["consecutive_failures"] = 0
        _CIRCUIT_BREAKER["tripped_at"] = 0.0
        _CIRCUIT_BREAKER["last_failure_reason"] = ""
        _CIRCUIT_BREAKER["cooldown_level"] = 0
        _CIRCUIT_BREAKER.pop("effective_cooldown", None)


def _cb_is_open_unlocked() -> bool:
    """Check if circuit breaker is open. Caller MUST hold _CB_LOCK."""
    if _CIRCUIT_BREAKER.get("consecutive_failures", 0) < CIRCUIT_BREAKER_THRESHOLD:
        return False
    tripped = _CIRCUIT_BREAKER.get("tripped_at", 0.0)
    if tripped <= 0:
        return False
    cooldown = _CIRCUIT_BREAKER.get("effective_cooldown", CIRCUIT_BREAKER_COOLDOWN_SEC)
    elapsed = time.monotonic() - tripped
    if elapsed > cooldown:
        # Cooldown expired — allow one probe attempt
        return False
    return True


def _cb_is_open() -> bool:
    """Check if circuit breaker is currently open (should skip remote)."""
    with _CB_LOCK:
        return _cb_is_open_unlocked()


def get_circuit_breaker_status() -> dict:
    """Public API to check circuit breaker state (for monitoring/patrol)."""
    with _CB_LOCK:
        is_open = _cb_is_open_unlocked()
        return {
            "open": is_open,
            "consecutive_failures": _CIRCUIT_BREAKER.get("consecutive_failures", 0),
            "threshold": CIRCUIT_BREAKER_THRESHOLD,
            "cooldown_sec": CIRCUIT_BREAKER_COOLDOWN_SEC,
            "last_failure_reason": _CIRCUIT_BREAKER.get("last_failure_reason", ""),
            "status": "OPEN (degraded to local)" if is_open else "CLOSED (remote OK)",
        }


def _remote_online_quick() -> bool:
    """
    Fast reachability probe for Melchior host.
    Checks:
      1. MELCHIOR_FORCE_LOCAL env override
      2. Circuit breaker (skip remote if recently failed)
      3. Flask /health endpoint (is the server process alive?)
      4. Ollama /api/tags (is the LLM engine responsive?)
    Returns False if any check fails → caller will use local Ollama.
    """
    if _avoid_distributed():
        return False

    # --- Retired remote Melchior path ---
    # Kept only for explicit legacy opt-in during migration tests.
    if os.environ.get("MAGI_USE_REMOTE_HEALTH_GATE", "0").strip().lower() in {"1", "true", "on", "yes"}:
        try:
            from api.platforms.remote_health_gate import get_gate, PeerConfig
            gate = get_gate()
            gate.register(PeerConfig(
                name="melchior",
                probe_url=f"{MELCHIOR_BASE_URL.rstrip('/')}/health" if MELCHIOR_BASE_URL else None,
                fail_threshold=int(os.environ.get("MELCHIOR_CB_THRESHOLD", "3")),
                cooldown_seconds=(
                    int(os.environ.get("MELCHIOR_CB_COOLDOWN_SEC", "180")),
                    int(os.environ.get("MELCHIOR_CB_COOLDOWN_SEC", "180")) * 2,
                    int(os.environ.get("MELCHIOR_CB_COOLDOWN_SEC", "180")) * 3,
                ),
            ))
            ok, _ = gate.is_reachable("melchior")
            return ok
        except Exception:
            pass
        # legacy code unchanged below

    if (os.environ.get("MELCHIOR_FORCE_LOCAL", "") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False

    # Circuit breaker: skip remote if recently tripped
    if _cb_is_open():
        return False

    try:
        # 1) Flask health — is the server process alive?
        r = SESSION.get(
            f"{MELCHIOR_BASE_URL}/health",
            timeout=(max(0.5, CONNECT_TIMEOUT_SEC), max(1.0, CONNECT_TIMEOUT_SEC)),
        )
        if r.status_code != 200:
            return False
    except Exception:
        return False

    try:
        # 2) Ollama /api/tags — is the LLM engine actually responsive?
        #    This catches the case where Flask is alive but Ollama is hung/crashed.
        r2 = SESSION.get(
            f"{MELCHIOR_OLLAMA_BASE}/api/tags",
            timeout=(max(0.5, CONNECT_TIMEOUT_SEC), _OLLAMA_PROBE_TIMEOUT_SEC),
        )
        if r2.status_code != 200:
            _cb_trip("ollama_tags_non200")
            return False
    except Exception:
        _cb_trip("ollama_tags_unreachable")
        return False

    return True


def _list_ollama_models(host: str = "localhost", port: int = 11434, force_refresh: bool = False) -> List[str]:
    now = time.time()
    with _MODEL_CACHE_LOCK:
        if (
            host == MELCHIOR_HOST
            and (not force_refresh)
            and _MODEL_CACHE["models"]
            and (now - float(_MODEL_CACHE["ts"])) < MODEL_CACHE_TTL_SEC
        ):
            return list(_MODEL_CACHE["models"])

    try:
        resp = SESSION.get(f"http://{host}:{port}/api/tags", timeout=5)
        if resp.status_code != 200:
            return []
        models = []
        for item in resp.json().get("models", []):
            name = (item or {}).get("name", "").strip()
            if name:
                models.append(name)
                # Also add short name without :latest so callers can match either form
                if name.endswith(":latest"):
                    short = name[: -len(":latest")]
                    models.append(short)
        models = sorted(set(models))
        if host == MELCHIOR_HOST:
            with _MODEL_CACHE_LOCK:
                _MODEL_CACHE["ts"] = now
                _MODEL_CACHE["models"] = list(models)
        return models
    except Exception:
        return []

def list_openai_v1_models(force_refresh: bool = False) -> List[str]:
    """
    List models from Melchior's OpenAI-compatible /v1 server (usually available in distributed mode).
    """
    if _cb_is_open():
        return []
    
    now = time.time()
    with _MODEL_CACHE_LOCK:
        if (not force_refresh) and _OPENAI_MODELS_CACHE.get("models") and (now - float(_OPENAI_MODELS_CACHE.get("ts") or 0.0)) < OPENAI_MODELS_CACHE_TTL_SEC:
            return list(_OPENAI_MODELS_CACHE["models"])
    def _parse(data: dict) -> List[str]:
        items = (data or {}).get("data") or []
        models = []
        for it in items:
            if isinstance(it, dict) and it.get("id"):
                models.append(str(it["id"]).strip())
        return sorted(set(models))

    # Try primary (llama-server /v1), then fallback (Ollama /v1).
    for base in [MELCHIOR_OPENAI_V1_BASE, MELCHIOR_OPENAI_V1_FALLBACK]:
        try:
            data = _get_json(f"{base}/models", timeout=4)
            models = _parse(data)
            if models:
                with _MODEL_CACHE_LOCK:
                    _OPENAI_MODELS_CACHE["ts"] = now
                    _OPENAI_MODELS_CACHE["models"] = list(models)
                    # Store last-good base for chat calls.
                    _OPENAI_MODELS_CACHE["base"] = base
                return models
        except Exception:
            continue
    return []


def get_capabilities(force_refresh: bool = False) -> dict:
    """
    Fetch Melchior capability probe (best-effort).
    Falls back to local inference by returning minimal capabilities if the endpoint is not present.
    """
    now = time.time()
    if (not force_refresh) and _CAP_CACHE.get("data") and (now - float(_CAP_CACHE.get("ts") or 0.0)) < CAP_CACHE_TTL_SEC:
        return dict(_CAP_CACHE["data"])

    cap = {}
    try:
        cap = _get_json(ENDPOINTS["capabilities"], timeout=4)
        if isinstance(cap, dict) and cap.get("ok"):
            _CAP_CACHE["ts"] = now
            _CAP_CACHE["data"] = dict(cap)
            return dict(cap)
    except Exception:
        cap = {}

    # Fallback: infer from health + tags.
    h = check_health()
    v1 = list_openai_v1_models(force_refresh=False)
    mode = "distributed" if v1 else ("engineer" if h.get("online") else "offline")
    fallback = {
        "ok": bool(h.get("online")),
        "service": "melchior",
        "mode": mode,
        "ollama": {"reachable": bool(h.get("models")), "models": h.get("models") or []},
        "openai_v1": {"reachable": bool(v1), "models": v1},
        "ts": int(now),
    }
    _CAP_CACHE["ts"] = now
    _CAP_CACHE["data"] = dict(fallback)
    return fallback


def warmup(model: str = "", timeout: int = 60) -> dict:
    """
    Ask Melchior agent to warm up an Ollama model (best-effort).
    """
    use_model = (model or "").strip() or (PREFERRED_DISTRIBUTED_MODELS[0] if PREFERRED_DISTRIBUTED_MODELS else TEXT_PRIMARY_MODEL)
    try:
        data = _post_json(ENDPOINTS["warmup"], {"model": use_model, "timeout": int(timeout)}, timeout=max(5, int(timeout) + 5))
        return {"success": bool(data.get("success")), "model": use_model, "ms": data.get("ms"), "error": data.get("error", "")}
    except Exception as e:
        return {"success": False, "model": use_model, "error": str(e)}


def _pick_openai_model(requested_model: str, available: List[str]) -> str:
    req = (requested_model or "").strip()
    avail = [m for m in (available or []) if isinstance(m, str) and m.strip()]
    if not avail:
        return req or ""
    if req and req in avail:
        return req
    if req:
        pref = req.split(":")[0].lower()
        hit = next((a for a in avail if a.lower().split(":")[0] == pref), "")
        if hit:
            return hit
    # Prefer local Gemma models available on this machine
    for c in ["gemma"]:
        hit = next((a for a in avail if a.lower().startswith(c)), "")
        if hit:
            return hit
    return avail[0]


def _openai_v1_chat(prompt: str, model: str, timeout: int, temperature: float = MELCHIOR_TEMPERATURE, max_tokens: int = MELCHIOR_MAX_TOKENS) -> dict:
    """
    OpenAI-compatible chat via Melchior's llama-server (/v1/chat/completions).
    """
    if _cb_is_open():
        return _result(False, "", "circuit breaker open (skipping remote openai_v1)")

    use_model = (model or "").strip()
    if not use_model:
        return _result(False, "", "missing model for openai v1")
    payload = {
        "model": use_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    base = str(_OPENAI_MODELS_CACHE.get("base") or "").strip() or MELCHIOR_OPENAI_V1_BASE
    try:
        data = _post_json(f"{base}/chat/completions", payload, timeout=max(10, int(timeout)))
        choices = data.get("choices") or []
        text = ""
        if choices and isinstance(choices, list):
            c0 = choices[0] if isinstance(choices[0], dict) else {}
            msg = (c0.get("message") or {}) if isinstance(c0.get("message"), dict) else {}
            text = (msg.get("content") or "").strip() or str(c0.get("text") or "").strip()
        if not text:
            return _result(False, "", "Empty openai v1 response")
        ok = _result(True, text, "")
        ok["model"] = use_model
        ok["route"] = "melchior_openai_v1"
        ok["base"] = base
        _cb_reset()
        return ok
    except Exception as e:
        _cb_trip(f"openai_v1_failed: {e}")
        return _result(False, "", f"openai v1 failed: {e}")


def _local_openai_v1_models(timeout: int = 3) -> List[str]:
    if _avoid_distributed():
        return []
    try:
        data = _get_json("http://localhost:8080/v1/models", timeout=max(2, int(timeout)))
        items = (data or {}).get("data") or []
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("id"):
                out.append(str(it["id"]).strip())
        return sorted(set(out))
    except Exception:
        return []


def _local_openai_v1_chat(prompt: str, model: str = "", timeout: int = 20) -> dict:
    models = _local_openai_v1_models(timeout=min(4, max(2, int(timeout // 4) or 2)))
    use = _pick_openai_model(model or os.environ.get("LOCAL_MAIN_MODEL", TEXT_PRIMARY_MODEL), models) if models else ""
    if not use:
        return _result(False, "", "local_openai_v1_model_unavailable")
    payload = {
        "model": use,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": min(512, MELCHIOR_MAX_TOKENS),
        "stream": False,
    }
    try:
        data = _post_json("http://localhost:8080/v1/chat/completions", payload, timeout=max(8, int(timeout)))
        choices = data.get("choices") or []
        text = ""
        if choices and isinstance(choices, list):
            c0 = choices[0] if isinstance(choices[0], dict) else {}
            msg = (c0.get("message") or {}) if isinstance(c0.get("message"), dict) else {}
            text = (msg.get("content") or "").strip() or str(c0.get("text") or "").strip()
        if not text:
            return _result(False, "", "empty_local_openai_v1_response")
        ok = _result(True, text, "")
        ok["route"] = "local_openai_v1"
        ok["model"] = use
        ok["degraded"] = True
        return ok
    except Exception as e:
        return _result(False, "", f"local_openai_v1_failed: {e}")


def _resolve_remote_model(requested_model: str) -> str:
    requested = (requested_model or "").strip()
    available = _list_ollama_models(MELCHIOR_HOST, MELCHIOR_OLLAMA_PORT)
    if not available:
        return requested or (PREFERRED_DISTRIBUTED_MODELS[0] if PREFERRED_DISTRIBUTED_MODELS else TEXT_PRIMARY_MODEL)

    if requested and requested in available:
        return requested
    if requested:
        req_prefix = requested.split(":")[0].lower()
        for m in available:
            if m.lower().split(":")[0] == req_prefix:
                return m

    for candidate in PREFERRED_DISTRIBUTED_MODELS:
        if candidate in available:
            return candidate
        c_prefix = candidate.split(":")[0].lower()
        for m in available:
            if m.lower().split(":")[0] == c_prefix:
                return m
    return available[0]

def _unique(seq: List[str]) -> List[str]:
    out = []
    seen = set()
    for x in seq:
        v = (x or "").strip()
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def _fallback_remote_models(primary: str, available: List[str]) -> List[str]:
    """
    When the primary model is overloaded (timeout), try smaller remote models to keep tasks running.
    """
    avail = [m for m in (available or []) if isinstance(m, str)]
    candidates = []
    for m in DEGRADED_FALLBACK_MODELS:
        if m in avail:
            candidates.append(m)
            continue
        # Prefix match (e.g., gemma-4 -> gemma-4-26b-a4b-it-4bit)
        pref = m.split(":")[0].lower()
        hit = next((a for a in avail if a.lower().split(":")[0] == pref), "")
        if hit:
            candidates.append(hit)
    # If nothing matched, try any small-ish models that exist.
    if not candidates:
        for a in avail:
            low = a.lower()
            if any(k in low for k in ["tinyllama", "mistral", "7b", "8b"]):
                candidates.append(a)
    # Never re-try the primary here.
    candidates = [c for c in candidates if c and c != primary]
    return _unique(candidates)[:3]


def _chat_ollama(
    prompt: str,
    model: str,
    timeout: int,
    host: str = "localhost",
    port: int = 11434,
    *,
    num_ctx_override: int = 0,
    num_predict: int = 0,
    temperature_override: float = None,
) -> dict:
    """
    Direct Ollama API access (remote or local fallback).
    """
    # Ollama on localhost:11434 serves as fallback when oMLX crashes.
    # Uses lightweight model (gemma4:e4b) to avoid GPU contention with oMLX.
    try:
        if host == "localhost":
            local_models = _list_ollama_models("localhost", port)
            requested = (model or "").strip()
            if requested and requested in local_models:
                model = requested
            elif requested:
                # Caller explicitly requested a model — trust it even if not
                # in cached list (model may need cold-start loading by Ollama).
                model = requested
            else:
                # No specific model requested — pick a lightweight local model.
                picked = ""
                for cand in [TEXT_PRIMARY_MODEL, "gemma-3-12b-it-4bit"]:
                    if cand in local_models:
                        picked = cand
                        break
                if not picked and local_models:
                    picked = local_models[0]
                if picked:
                    model = picked

        base_url = f"http://{host}:{port}"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,  # Disable thinking for models like gpt-oss that support it
            "keep_alive": MELCHIOR_KEEP_ALIVE,
            "options": {
                # Lower default ctx improves throughput/latency for local models in most admin tasks.
                "temperature": float(MELCHIOR_TEMPERATURE if temperature_override is None else temperature_override),
                "num_ctx": int(num_ctx_override or MELCHIOR_NUM_CTX),
            },
        }
        if int(num_predict or 0) > 0:
            payload["options"]["num_predict"] = int(num_predict)

        data = _post_json(f"{base_url}/api/generate", payload, timeout)
        if host != "localhost":
            _cb_reset()
        return _result(True, data.get("response", ""), "")
    except Exception as e:
        if host != "localhost":
            _cb_trip(f"ollama_{host}_failed: {e}")
        return _result(False, "", f"Ollama ({host}) failed: {e}")


def quick_local_chat(prompt: str, timeout: int = 14, model_hint: str = TEXT_PRIMARY_MODEL,
                     num_ctx: int = 0, num_predict: int = 0) -> dict:
    """
    Fast degraded-response path on local Ollama.
    Returns a short but real model answer when distributed path is slow/unavailable.

    num_ctx / num_predict: optional overrides for heavy workloads (e.g. summarization).
        When 0 (default), uses the original conservative defaults (4096 / 520-768).
    """
    try:
        requested_timeout = int(timeout)
    except Exception:
        requested_timeout = 14
    # Keep quick path bounded, but allow enough time for local model warmup
    # and summary-type requests that need longer generation windows.
    use_timeout = max(6, min(requested_timeout, 300))
    try:
        quick_num_predict = int(os.environ.get("MAGI_QUICK_LOCAL_NUM_PREDICT", "520") or "520")
    except Exception:
        quick_num_predict = 520
    # For longer prompts (e.g. summaries), allow more output tokens so the
    # model can produce a complete structured response.
    prompt_len = len(prompt) if prompt else 0
    if prompt_len > 600 and quick_num_predict < 768:
        quick_num_predict = 768
    # Caller-specified overrides for heavy workloads (summarization / translation).
    if num_predict and num_predict > 0:
        quick_num_predict = int(num_predict)
    use_num_ctx = 4096
    if num_ctx and num_ctx > 0:
        use_num_ctx = int(num_ctx)

    # ── Primary: oMLX (replaces Ollama for all local inference) ──
    if _omlx_available():
        omlx_model = _OMLX_MODEL_ALIAS.get(model_hint, model_hint)
        # If the hint is not available, fall back to configured primary text model.
        if omlx_model == model_hint and model_hint not in list_omlx_models():
            omlx_model = TEXT_PRIMARY_MODEL
        r = _chat_omlx(
            prompt=prompt,
            model=omlx_model,
            timeout=use_timeout,
            temperature=0.3,
            max_tokens=max(quick_num_predict, 1024),
        )
        if r.get("success"):
            r["route"] = "omlx_quick"
            return r

    # Ollama is retired — no fallback beyond oMLX.
    _logger.warning("quick_local_chat: oMLX unavailable (timeout=%ss, model_hint=%s)", use_timeout, model_hint)
    return _result(False, "", "quick_local_chat_failed")


def generate_code(prompt: str, language: str = "python") -> dict:
    """
    Request code generation. Prefers oMLX/Coder-14B, falls back to Ollama.
    Returns: {"success": bool, "code": str, "error": str}
    """
    # Try oMLX Coder first (best quality + speed on Apple Silicon)
    if _omlx_available():
        r = _chat_omlx(
            prompt=f"Write code in {language}:\n{prompt}",
            model=OMLX_CODE_MODEL,
            timeout=180,
            temperature=0.2,
            max_tokens=4096,
            system_prompt=f"You are an expert {language} programmer. Write clean, correct code. Output ONLY code, no explanations.",
        )
        if r.get("success"):
            return {"success": True, "code": r["response"], "error": "", "model": OMLX_CODE_MODEL, "route": "omlx"}

    try:
        data = _post_json(
            ENDPOINTS["code"],
            {
                "prompt": f"[{language}] {prompt}",
                "model": "GLM-4.7:latest",
            },
            TIMEOUT,
        )
        code = data.get("response", "")
        if not code or "error" in code.lower():
            return {"success": False, "code": "", "error": code or "Empty response"}
        return {"success": True, "code": code, "error": ""}
    except Exception:
        # Fallback: remote ollama, then local ollama
        remote_model = _resolve_remote_model("GLM-4.7:latest")
        remote = _chat_ollama(
            f"Write code in {language}: {prompt}",
            remote_model,
            TIMEOUT,
            host=MELCHIOR_HOST,
            port=MELCHIOR_OLLAMA_PORT,
        )
        if remote["success"]:
            return {"success": True, "code": remote["response"], "error": "", "model": remote_model, "route": "melchior_ollama"}

        local = _chat_ollama(
            f"Write code in {language}: {prompt}",
            TEXT_PRIMARY_MODEL,
            min(max(12, MELCHIOR_LOCAL_FALLBACK_TIMEOUT_SEC), TIMEOUT),
            host="localhost",
            port=11434,
        )
        return {
            "success": local["success"],
            "code": local.get("response", ""),
            "error": local.get("error", remote.get("error", "fallback failed")),
            "route": "local_ollama",
        }


def chat_stream(prompt, model=TEXT_PRIMARY_MODEL, timeout=TIMEOUT):
    # type: (str, str, int) -> ...
    """Streaming version of chat(). Yields text chunks as they arrive.

    Usage:
        for chunk in chat_stream("你好"):
            print(chunk, end="", flush=True)
    """
    budget_sec = max(8, int(timeout))

    # Resolve model alias (same logic as chat())
    omlx_model = _OMLX_MODEL_ALIAS.get(model, model)
    if omlx_model == model and model not in list_omlx_models():
        omlx_model = OMLX_LOCAL_CHAT_MODEL

    messages = [{"role": "user", "content": prompt}]
    messages = _ensure_alternating_roles(messages)

    payload = {
        "model": omlx_model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 2048,
        "top_p": 0.88,
        "stream": True,
    }
    if OMLX_CHAT_PORT != 11434:
        payload["repetition_penalty"] = 1.1

    url = "{}/v1/chat/completions".format(OMLX_CHAT_BASE)

    try:
        sess = requests.Session()
        resp = sess.post(url, json=payload, stream=True, timeout=budget_sec)
        resp.raise_for_status()

        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            if line == "data: [DONE]":
                break
            if not line.startswith("data: "):
                continue
            json_str = line[len("data: "):]
            try:
                chunk_data = json.loads(json_str)
            except (json.JSONDecodeError, ValueError):
                continue
            choices = chunk_data.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                yield content
        return
    except Exception as e:
        _logger.warning("chat_stream failed (%s), falling back to non-streaming chat()", e)

    # Fallback: non-streaming chat(), yield full result at once
    result = chat(prompt, model=model, timeout=timeout)
    if result.get("success") and result.get("response"):
        yield result["response"]


def chat(prompt: str, model: str = TEXT_PRIMARY_MODEL, timeout: int = TIMEOUT) -> dict:
    """
    Send chat prompt to Melchior agent with layered fallback.
    Returns: {"success": bool, "response": str, "error": str}
    """
    if "gpt-oss" in (model or "").lower():
        logger.warning("🚨 [MODEL-MIGRATION] melchior_client.chat() called with gpt-oss model: %s — should be %s", model, TEXT_PRIMARY_MODEL)
    budget_sec = max(8, int(timeout))
    deadline = _start_deadline(budget_sec)
    errors = []

    # Default policy: local-first via oMLX. Ollama/remote are fallback only.
    if MELCHIOR_LOCAL_FIRST_DEFAULT:
        local_try_timeout = max(8, min(int(MELCHIOR_LOCAL_FIRST_TIMEOUT_SEC), max(10, int(budget_sec * 0.6))))

        # Step 1: oMLX primary (direct _chat_omlx with model alias resolution)
        _step_timeout = min(local_try_timeout, _remaining(deadline, floor=4))
        if _step_timeout >= 4 and _omlx_available():
            omlx_model = _OMLX_MODEL_ALIAS.get(model, model)
            # If the requested model isn't known to oMLX, use local chat model as default
            if omlx_model == model and model not in list_omlx_models():
                omlx_model = OMLX_LOCAL_CHAT_MODEL
            omlx_r = _chat_omlx(prompt=prompt, model=omlx_model, timeout=_step_timeout)
            if omlx_r.get("success"):
                omlx_r["route"] = "omlx_primary"
                omlx_r["timeout_budget_sec"] = budget_sec
                return omlx_r
            errors.append(f"omlx_primary={omlx_r.get('error','')}")

        # Step 2: oMLX via OpenAI-compatible endpoint (port 8080)
        # Skip if Step 1 oMLX already failed with connection error (GPU crash → port 8080 also down)
        _omlx_conn_failed = any("Connection" in e or "Refused" in e or "Disconnect" in e for e in errors)
        _step_timeout = min(local_try_timeout, _remaining(deadline, floor=4))
        if _step_timeout >= 4 and not _omlx_conn_failed:
            local_v1_primary = _local_openai_v1_chat(
                prompt,
                model=os.environ.get("LOCAL_MAIN_MODEL", model or TEXT_PRIMARY_MODEL),
                timeout=_step_timeout,
            )
            if local_v1_primary.get("success"):
                local_v1_primary["route"] = "local_openai_v1_primary"
                local_v1_primary["degraded"] = True
                local_v1_primary["timeout_budget_sec"] = budget_sec
                return local_v1_primary
            errors.append(f"local_v1_primary={local_v1_primary.get('error','')}")

        # Step 3: Ollama fallback (lightweight model only — avoid loading 26B and competing with oMLX for GPU)
        _step_timeout = min(local_try_timeout, _remaining(deadline, floor=4))
        _ollama_fallback_model = os.environ.get("MAGI_OLLAMA_FALLBACK_MODEL", "gemma4:e4b").strip() or "gemma4:e4b"
        if _step_timeout >= 4:
            local_primary = _chat_ollama(
                prompt,
                _ollama_fallback_model,
                _step_timeout,
                host="localhost",
                port=11434,
            )
            if local_primary.get("success"):
                local_primary["route"] = "local_ollama_fallback"
                local_primary["degraded"] = True
                local_primary["timeout_budget_sec"] = budget_sec
                return local_primary
            errors.append(f"local_ollama={local_primary.get('error','')}")

    # If Melchior is not reachable, skip remote attempts and go straight to local Ollama.
    # This prevents long stalls when the network path (Tailscale/VPN) is down.
    if not _remote_online_quick():
        local = _chat_ollama(
            prompt,
            model,
            _local_fallback_timeout(_remaining(deadline, floor=8), floor=8),
            host="localhost",
            port=11434,
        )
        if local.get("success"):
            local["route"] = "local_ollama"
            local["degraded"] = True
            local["tried_models"] = []
            local["timeout_budget_sec"] = budget_sec
            return local
        quick = quick_local_chat(
            prompt=prompt,
            timeout=max(10, min(20, _remaining(deadline, floor=10))),
            model_hint=(os.environ.get("MELCHIOR_SHORT_TIMEOUT_MODEL", TEXT_PRIMARY_MODEL) or TEXT_PRIMARY_MODEL),
        )
        if quick.get("success"):
            quick["timeout_budget_sec"] = budget_sec
            return quick
        err = f"melchior_offline_and_local_failed: {local.get('error','')}"
        err += f" ; quick={quick.get('error','')}"
        if errors:
            err = " ; ".join(errors + [err])
        return _result(False, "", err[:1200])

    available = _list_ollama_models(MELCHIOR_HOST, MELCHIOR_OLLAMA_PORT)
    chosen_remote_model = _resolve_remote_model(model)

    tried = []
    # Primary attempt first, then degrade to smaller remote models to keep the task running.
    remote_models = _unique([chosen_remote_model] + _fallback_remote_models(chosen_remote_model, available))

    for idx, remote_model in enumerate(remote_models):
        if _remaining(deadline, floor=0) <= 0:
            errors.append("overall_timeout_exceeded_before_remote_attempt")
            break
        tried.append(remote_model)
        remaining_now = _remaining(deadline, floor=6)
        # Keep some budget for degraded fallbacks so one heavy model won't consume everything.
        remaining_models = max(0, len(remote_models) - idx - 1)
        reserve_for_after = 18 + (remaining_models * 8)
        if idx == 0:
            cap = max(12, MELCHIOR_PRIMARY_TRY_TIMEOUT_SEC)
        else:
            cap = max(8, MELCHIOR_FALLBACK_TRY_TIMEOUT_SEC)
        per_try_timeout = max(8, min(int(timeout), int(cap), max(8, remaining_now - reserve_for_after)))
        try:
            data = _post_json(
                ENDPOINTS["chat"],
                {
                    "prompt": prompt,
                    "model": remote_model,
                    "timeout": per_try_timeout,
                    "keep_alive": MELCHIOR_KEEP_ALIVE,
                    "options": {
                        "temperature": MELCHIOR_TEMPERATURE,
                        "num_ctx": MELCHIOR_NUM_CTX,
                    },
                },
                per_try_timeout,
            )
            text = data.get("response", "")
            if not text:
                raise RuntimeError("Empty response")
            if "error" in text.lower() and len(text) < 300:
                raise RuntimeError(text)
            ok = _result(True, text, "")
            ok["model"] = remote_model
            ok["route"] = "melchior_agent"
            ok["degraded"] = (remote_model != chosen_remote_model)
            ok["tried_models"] = tried
            ok["timeout_budget_sec"] = budget_sec
            _cb_reset()
            return ok
        except Exception as e:
            _cb_trip(f"agent({remote_model})={e}")
            errors.append(f"agent({remote_model})={e}")

        if _remaining(deadline, floor=0) <= 0:
            errors.append("overall_timeout_exceeded_before_ollama_attempt")
            break
        remaining_now = _remaining(deadline, floor=6)
        remaining_models = max(0, len(remote_models) - idx - 1)
        reserve_for_after = 18 + (remaining_models * 8)
        if idx == 0:
            cap = max(12, MELCHIOR_PRIMARY_TRY_TIMEOUT_SEC)
        else:
            cap = max(8, MELCHIOR_FALLBACK_TRY_TIMEOUT_SEC)
        per_try_timeout = max(8, min(int(timeout), int(cap), max(8, remaining_now - reserve_for_after)))
        remote = _chat_ollama(
            prompt,
            remote_model,
            per_try_timeout,
            host=MELCHIOR_HOST,
            port=MELCHIOR_OLLAMA_PORT,
        )
        if remote.get("success"):
            remote["model"] = remote_model
            remote["route"] = "melchior_ollama"
            remote["degraded"] = (remote_model != chosen_remote_model)
            remote["tried_models"] = tried
            remote["timeout_budget_sec"] = budget_sec
            return remote
        errors.append(f"ollama({remote_model})={remote.get('error','')}")

    # Last resort: local Ollama (if present). If it's not running, we still return a clear error.
    if _remaining(deadline, floor=0) <= 0:
        return _result(
            False,
            "",
            f"overall_timeout_exceeded (budget={budget_sec}s); tried={','.join(tried) or 'none'}",
        )

    local = _chat_ollama(
        prompt,
        model,
        _local_fallback_timeout(_remaining(deadline, floor=8), floor=8),
        host="localhost",
        port=11434,
    )
    if local.get("success"):
        local["route"] = "local_ollama"
        local["degraded"] = True
        local["tried_models"] = tried
        local["timeout_budget_sec"] = budget_sec
        return local
    # Fallback 2: local llama-server OpenAI /v1 stack if available.
    local_v1 = _local_openai_v1_chat(
        prompt,
        model=os.environ.get("LOCAL_MAIN_MODEL", TEXT_PRIMARY_MODEL),
        timeout=_local_fallback_timeout(_remaining(deadline, floor=10), floor=10),
    )
    if local_v1.get("success"):
        local_v1["tried_models"] = tried
        local_v1["timeout_budget_sec"] = budget_sec
        return local_v1

    errors.append(f"local={local.get('error','')}")
    errors.append(f"local_v1={local_v1.get('error','')}")
    return _result(False, "", " ; ".join(errors)[:1200])


def distributed_chat(prompt: str, timeout: int = TIMEOUT) -> dict:
    """
    Chat helper that routes to the configured local primary text model.
    Distributed inference is disabled; uses local-first routing.
    Returns route/model metadata for observability.
    """
    preferred = PREFERRED_DISTRIBUTED_MODELS[0] if PREFERRED_DISTRIBUTED_MODELS else TEXT_PRIMARY_MODEL
    timeout_i = max(8, int(timeout))
    timeout_cap = max(20, int(MELCHIOR_DISTRIBUTED_TIMEOUT_CAP_SEC))
    timeout_i = min(timeout_i, timeout_cap)
    if _avoid_distributed():
        out = chat(prompt=prompt, model=preferred, timeout=timeout_i)
        if isinstance(out, dict):
            out["avoid_distributed"] = True
            out["route"] = str(out.get("route") or "local_only")
        return out
    return smart_chat(prompt=prompt, model_hint=preferred, timeout=timeout_i, quality="high")


def smart_chat(prompt: str, model_hint: str = "", timeout: int = TIMEOUT, quality: str = "auto") -> dict:
    """
    Dynamic routing:
    - If Melchior OpenAI /v1 is reachable and quality is high, prefer /v1 (distributed mode).
    - Otherwise fall back to Melchior agent /api/chat then remote Ollama then local.
    """
    q = (quality or "auto").strip().lower()
    timeout_i = max(8, int(timeout))
    want_high = q in {"high", "reasoning", "quality", "best", "auto"}
    requested = (model_hint or "").strip() or (PREFERRED_DISTRIBUTED_MODELS[0] if PREFERRED_DISTRIBUTED_MODELS else TEXT_PRIMARY_MODEL)
    if _avoid_distributed():
        out = chat(prompt=prompt, model=requested, timeout=timeout_i)
        if isinstance(out, dict):
            out["avoid_distributed"] = True
        return out
    if MELCHIOR_LOCAL_FIRST_DEFAULT:
        return chat(prompt=prompt, model=requested, timeout=timeout_i)
    # For short request budgets, prefer a smaller model to avoid "no reply" experiences.
    # Explicit high/reasoning quality keeps large-model preference.
    if q == "auto" and timeout_i <= 90:
        requested = (os.environ.get("MELCHIOR_SHORT_TIMEOUT_MODEL", TEXT_PRIMARY_MODEL) or TEXT_PRIMARY_MODEL).strip()
        want_high = False

    deadline = _start_deadline(timeout_i)

    if MELCHIOR_ROUTE_PREFER_OPENAI and want_high:
        avail = list_openai_v1_models(force_refresh=False)
        if avail:
            use = _pick_openai_model(requested, avail)
            openai_cap = max(10, int(int(timeout) * 0.35))
            openai_timeout = max(
                8,
                min(int(MELCHIOR_OPENAI_PRIMARY_TIMEOUT_SEC), int(openai_cap), _remaining(deadline, floor=8)),
            )
            r = _openai_v1_chat(prompt, use, timeout=openai_timeout, temperature=MELCHIOR_TEMPERATURE, max_tokens=MELCHIOR_MAX_TOKENS)
            if r.get("success"):
                return r

    # Default path (existing robust fallback chain)
    return chat(prompt=prompt, model=requested, timeout=max(8, _remaining(deadline, floor=8)))


def reason(prompt, use_wfgy=True, timeout=300):
    """
    Executes a reasoning request, optionally using the WFGY protocol.
    """
    final_prompt = prompt
    if use_wfgy:
        try:
            from skills.reasoning.wfgy import apply_wfgy_logic

            final_prompt = apply_wfgy_logic(prompt)
        except ImportError:
            logger.warning("⚠️ WFGY reasoning module not available — using standard prompt")

    return chat(final_prompt, timeout=timeout)


def update_agent(api_script_path: str) -> dict:
    """
    Uploads a new agent script to Melchior for self-update.
    """
    update_url = f"{MELCHIOR_BASE_URL}/api/update_agent"
    try:
        if not os.path.exists(api_script_path):
            return {"success": False, "error": "File not found"}

        with open(api_script_path, "rb") as f:
            files = {"file": f}
            response = SESSION.post(update_url, files=files, timeout=30)
            response.raise_for_status()

        return response.json()
    except Exception as e:
        return {"success": False, "error": str(e)}


def check_health() -> dict:
    """
    Check if Melchior is online and responsive.
    Returns:
        {"online": bool, "ollama_version": str, "code_engine": bool, "mode": "remote"|"local"|"offline"}
    """
    result = {
        "online": False,
        "ollama_version": "",
        "code_engine": False,
        "agent_online": False,
        "mode": "offline",
        "models": [],
        "openai_v1": {"reachable": False, "models": []},
    }

    # 0) Agent API health
    try:
        agent_resp = SESSION.get(f"{MELCHIOR_BASE_URL}/health", timeout=3)
        if agent_resp.status_code == 200:
            result["agent_online"] = True
            result["online"] = True
            result["mode"] = "remote"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1402, exc_info=True)

    # 1) Remote Ollama
    try:
        resp = SESSION.get(f"{MELCHIOR_OLLAMA_BASE}/api/version", timeout=3)
        if resp.status_code == 200:
            result["online"] = True
            result["ollama_version"] = resp.json().get("version", "unknown")
            result["mode"] = "remote"
            result["models"] = _list_ollama_models(MELCHIOR_HOST, MELCHIOR_OLLAMA_PORT)
            v1 = list_openai_v1_models(force_refresh=False)
            result["openai_v1"] = {"reachable": bool(v1), "models": v1}
            try:
                probe = SESSION.post(ENDPOINTS["code"], json={"prompt": "ping"}, timeout=5)
                if probe.status_code == 200:
                    result["code_engine"] = True
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1419, exc_info=True)
            return result
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1422, exc_info=True)

    if result["agent_online"]:
        return result

    # 2) Local fallback oMLX
    try:
        try:
            from api.routing.service_registry import get_service_url as _gsurl
            _omlx_base = _gsurl("omlx_inference")
        except Exception:
            _omlx_base = "http://127.0.0.1:8080"
        resp = SESSION.get(f"{_omlx_base}/v1/models", timeout=3)
        if resp.status_code == 200:
            result["online"] = True
            result["mode"] = "local_omlx"
            models_data = resp.json().get("data") or []
            result["models"] = [m.get("id", "?") for m in models_data]
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1436, exc_info=True)

    return result


def analyze_image(image_path: str, prompt: str = "Describe this image in detail") -> dict:
    """
    Send image for vision analysis. Prefers oMLX/Gemma-3 (multimodal), falls back to Melchior.
    Returns: {"success": bool, "analysis": str, "error": str}
    """
    if not os.path.exists(image_path):
        return {"success": False, "analysis": "", "error": f"File not found: {image_path}"}

    try:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        return {"success": False, "analysis": "", "error": f"Failed to read image: {e}"}

    # Try oMLX vision server (GLM-OCR on port 8082)
    if _omlx_vision_available():
        r = _chat_omlx(
            prompt=prompt,
            model=OMLX_VISION_MODEL,
            timeout=120,
            temperature=0.3,
            max_tokens=2048,
            images=[image_data],
            base_url=OMLX_VISION_BASE,
            circuit=_OMLX_VISION_CIRCUIT,
            lock=_OMLX_VISION_LOCK,
        )
        if r.get("success"):
            return {"success": True, "analysis": r["response"], "error": "", "route": "omlx_vision", "model": OMLX_VISION_MODEL}

    # Fallback to Melchior remote vision
    try:
        data = _post_json(
            ENDPOINTS["vision"],
            {
                "prompt": prompt,
                "image": image_data,
            },
            TIMEOUT,
        )

        text = data.get("response", "")
        if not text:
            return {"success": False, "analysis": "", "error": "Empty vision response"}
        if "error" in text.lower() and len(text) < 300:
            return {"success": False, "analysis": "", "error": text}

        return {"success": True, "analysis": text, "error": ""}
    except requests.exceptions.Timeout:
        return {"success": False, "analysis": "", "error": "Melchior vision timeout"}
    except Exception as e:
        return {"success": False, "analysis": "", "error": str(e)}


if __name__ == "__main__":
    print("MELCHIOR CLIENT TEST")
    print("=" * 40)
    print(check_health())
