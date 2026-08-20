"""NVIDIA NIM 重型兜底 helper.

- 統一入口 run_nim_chat()
- 律所資料去識別強制前置；使用者在當次訊息明確 @heavy 授權時可保留原文
- 模型白名單守門（禁用中國模型）
- Circuit breaker（連續 3 次 429 → 60s 冷卻）
- Daily budget guard（超量自動禁用當日）
- Usage log（.runtime/nvidia_nim_usage.jsonl）
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import threading
import time
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import requests

from skills.engine.pii_scrubber import (
    PRIVACY_POLICY_VERSION,
    build_scrubber_from_magi_db,
)

logger = logging.getLogger("NvidiaNimHeavy")

MAGI_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_DIR = Path(os.environ.get("MAGI_RUNTIME_DIR", "").strip() or MAGI_ROOT / ".runtime").expanduser()
USAGE_LOG_PATH = _RUNTIME_DIR / "nvidia_nim_usage.jsonl"
STATE_PATH = _RUNTIME_DIR / "nvidia_nim_state.json"
NIM_EOL_MODELS = {
    "meta/llama-3.1-405b-instruct",
}
NIM_HEAVY_TARGET_MODEL = "nvidia/nemotron-3-super-120b-a12b"
NIM_TRANSLATION_MODEL = "nvidia/nemotron-3-super-120b-a12b"
# Backward-compatible constant name used by older tests/imports.
NIM_405B_TARGET_MODEL = NIM_HEAVY_TARGET_MODEL
NIM_LARGE_FALLBACK_MODEL = "nvidia/nemotron-3-super-120b-a12b"
NIM_FINAL_FALLBACK_MODEL = "meta/llama-3.3-70b-instruct"
NIM_ALLOWED_MODELS = frozenset(
    {
        "meta/llama-3.1-405b-instruct",
        "meta/llama-3.3-70b-instruct",
        "meta/llama-3.1-70b-instruct",
        "meta/llama-3.1-8b-instruct",
        "nvidia/nemotron-3-super-120b-a12b",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "nvidia/llama-3.3-nemotron-super-49b-v1",
        "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "nvidia/llama-3.1-nemotron-70b-instruct",
        "nvidia/llama-3.1-nemotron-51b-instruct",
        "mistralai/mistral-large-3-675b-instruct-2512",
        "mistralai/mistral-medium-3.5-128b",
        "mistralai/mistral-large-2-instruct",
        "mistralai/mixtral-8x22b-instruct-v0.1",
        "google/gemma-3-27b-it",
        "google/gemma-2-27b-it",
        "microsoft/phi-4-multimodal-instruct",
    }
)
NIM_BLOCKED_MODEL_KEYWORDS = frozenset(
    {
        "deepseek",
        "qwen",
        "kimi",
        "minimax",
        "yi-",
        "baichuan",
        "glm-",
        "moonshot",
        "internlm",
        "chatglm",
        "sensetime",
    }
)
NIM_BACKGROUND_TASK_TYPES = frozenset(
    {
        "judgment_summary",
        "repair_insight_summary",
        "weekend_resummary",
        "knowledge_distillation",
        "batch_summary",
        "exam_tutor_trend_analysis",
        "exam_tutor_trend_statutory_audit",
    }
)

# A user may explicitly authorize personal data for one Heavy request, but
# credentials are never user content that MAGI may relay to a model provider.
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password|passwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:sk|ghp|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S+"),
    re.compile(r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]*(?:session|token|auth)[^\r\n]*="),
    re.compile(r"(?i)\b(?:session(?:id|[_ -]?token)?|oauth[_ -]?(?:token|secret))\s*[:=]\s*\S+"),
)


def _contains_credentials(*texts: str) -> bool:
    return any(pattern.search(str(text or "")) for text in texts for pattern in _CREDENTIAL_PATTERNS)


def background_heavy_authorization(job_id: str) -> dict[str, Any] | None:
    """Return one configured background authorization, never derived from chat.

    ``NVIDIA_NIM_BACKGROUND_AUTHORIZATIONS`` is a JSON object keyed by the
    scheduler job id.  The caller still passes the returned object to
    :func:`run_nim_chat`, which verifies every binding immediately before the
    provider call.  Missing/malformed configuration intentionally returns
    ``None`` and therefore fails closed.
    """
    raw = os.environ.get("NVIDIA_NIM_BACKGROUND_AUTHORIZATIONS", "").strip()
    if not raw or not str(job_id or "").strip():
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    item = payload.get(str(job_id)) if isinstance(payload, dict) else None
    return dict(item) if isinstance(item, dict) else None


def _validate_background_heavy_authorization(
    authorization: Mapping[str, Any] | None,
    *,
    task_type: str,
    source_class: str,
    model: str,
    daily_count: int,
) -> str:
    """Verify the complete, job-bound contract for a scheduled Heavy call."""
    if not isinstance(authorization, Mapping):
        return "background_heavy_authorization_missing"
    required = ("job_id", "task_type", "source_class", "provider", "model", "daily_budget", "expires_at")
    if any(not str(authorization.get(key) or "").strip() for key in required):
        return "background_heavy_authorization_incomplete"
    if str(authorization["task_type"]).strip().lower() != task_type:
        return "background_heavy_authorization_task_mismatch"
    if str(authorization["source_class"]).strip().lower() != source_class:
        return "background_heavy_authorization_source_mismatch"
    if str(authorization["provider"]).strip().lower() != "nvidia_nim":
        return "background_heavy_authorization_provider_mismatch"
    if str(authorization["model"]).strip() != model:
        return "background_heavy_authorization_model_mismatch"
    try:
        budget = int(authorization["daily_budget"])
    except (TypeError, ValueError):
        return "background_heavy_authorization_budget_invalid"
    if budget < 1 or daily_count >= budget:
        return "background_heavy_authorization_budget_exhausted"
    try:
        expires_at = datetime.datetime.fromisoformat(str(authorization["expires_at"]).replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            return "background_heavy_authorization_expiry_invalid"
        if datetime.datetime.now(datetime.timezone.utc) >= expires_at.astimezone(datetime.timezone.utc):
            return "background_heavy_authorization_expired"
    except (TypeError, ValueError):
        return "background_heavy_authorization_expiry_invalid"
    return ""

# ── 執行期狀態（單進程內）────────────────────────────────────
_state_lock = threading.Lock()
_cb_lock = threading.Lock()
_daily_count_lock = threading.Lock()
_nim_semaphore = threading.BoundedSemaphore(
    int(os.environ.get("NVIDIA_NIM_MAX_CONCURRENT", "3") or "3")
)
_cb_state: Dict[str, Any] = {
    "consecutive_429": 0,
    "cooldown_until_ts": 0,
    "last_error": "",
}

# ── 2026-04-24：動態壅塞監測 — 供 handler 決定是否跳過 NIM 直接走 GTX ──
# 維護最近 N 次呼叫結果的 sliding window，判定 NIM 目前是否在 congestion。
# 當 3/最近 5 次失敗或 p50 延遲 > 300s → 建議 prefer_gtx（下一 chunk 跳過 NIM 省時間）。
_congestion_lock = threading.Lock()
_congestion_window: list = []  # list of (ts, success, duration_ms) tuples
_CONGESTION_MAX = 5


def _model_allowed(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    return bool(
        normalized
        and normalized in NIM_ALLOWED_MODELS
        and not any(term in normalized for term in NIM_BLOCKED_MODEL_KEYWORDS)
    )


def record_nim_outcome(success: bool, duration_ms: int, error: str = "") -> None:
    """Track recent NIM call outcome for adaptive policy decisions."""
    with _congestion_lock:
        _congestion_window.append((time.time(), bool(success), int(duration_ms)))
        if len(_congestion_window) > _CONGESTION_MAX:
            _congestion_window.pop(0)


def recommend_nim_policy() -> Dict[str, Any]:
    """Return adaptive policy recommendation based on recent NIM health.

    Returns dict with keys:
      - policy: 'healthy' | 'fail_fast' | 'prefer_gtx'
      - reason: human-readable explanation
      - recent_success_rate: float (0.0-1.0)
      - recent_avg_duration_ms: int
    """
    with _congestion_lock:
        if len(_congestion_window) < 3:
            return {"policy": "healthy", "reason": "warmup:not_enough_samples",
                    "recent_success_rate": 1.0, "recent_avg_duration_ms": 0}
        successes = [x for x in _congestion_window if x[1]]
        failures = [x for x in _congestion_window if not x[1]]
        rate = len(successes) / len(_congestion_window)
        avg_ms = int(sum(x[2] for x in _congestion_window) / len(_congestion_window))
        if rate < 0.4:
            return {"policy": "prefer_gtx", "reason": f"failure_rate={rate:.0%}",
                    "recent_success_rate": rate, "recent_avg_duration_ms": avg_ms}
        if avg_ms > 600_000:  # 10 min avg
            return {"policy": "prefer_gtx", "reason": f"avg_duration={avg_ms//1000}s",
                    "recent_success_rate": rate, "recent_avg_duration_ms": avg_ms}
        if rate < 0.6 or avg_ms > 300_000:
            return {"policy": "fail_fast", "reason": f"rate={rate:.0%} avg={avg_ms//1000}s",
                    "recent_success_rate": rate, "recent_avg_duration_ms": avg_ms}
        return {"policy": "healthy", "reason": f"rate={rate:.0%} avg={avg_ms//1000}s",
                "recent_success_rate": rate, "recent_avg_duration_ms": avg_ms}


def reset_congestion_window() -> None:
    """For tests / manual reset."""
    with _congestion_lock:
        _congestion_window.clear()


def _today_key() -> str:
    return datetime.date.today().isoformat()


def _load_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("NIM state save failed: %s", e)


def _get_daily_count() -> int:
    with _state_lock:
        state = _load_state()
        if state.get("date") != _today_key():
            return 0
        return int(state.get("count") or 0)


def _incr_daily_count() -> int:
    with _daily_count_lock:
        state = _load_state()
        today = _today_key()
        if state.get("date") != today:
            state = {"date": today, "count": 0}
        state["count"] = int(state.get("count") or 0) + 1
        _save_state(state)
        return state["count"]


def _daily_budget() -> int:
    return int(os.environ.get("NVIDIA_NIM_DAILY_BUDGET", "500") or "500")


def _interactive_reserve() -> int:
    budget = max(1, _daily_budget())
    configured = int(os.environ.get("NVIDIA_NIM_INTERACTIVE_RESERVE", "25") or "25")
    return max(0, min(configured, budget - 1))


def _log_usage(payload: Dict[str, Any]) -> None:
    try:
        USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        try:
            from api.events.sinks import rotate_jsonl
            rotate_jsonl(str(USAGE_LOG_PATH))
        except Exception:
            pass
    except Exception as e:
        logger.debug("NIM usage log failed: %s", e)


def _pick_model(task_type: str, heavy: bool = False) -> str:
    task_key = (task_type or "").strip().lower()
    translate_model = os.environ.get("NVIDIA_NIM_TRANSLATE_MODEL", NIM_TRANSLATION_MODEL).strip()
    heavy_model = os.environ.get("NVIDIA_NIM_MODEL", NIM_HEAVY_TARGET_MODEL).strip()
    large_fallback = os.environ.get("NVIDIA_NIM_MODEL_LARGE_FALLBACK", NIM_LARGE_FALLBACK_MODEL).strip()
    fast_model = os.environ.get("NVIDIA_NIM_MODEL_FAST", NIM_FINAL_FALLBACK_MODEL).strip()
    # heavy flag 或長 prompt（另由 caller 判斷）→ heavy model；否則 fast model。
    # 2026-06: public NVIDIA API may return 404 for the literal 405B id on
    # some accounts, so legacy heavy configs are mapped to Super 120B.
    if heavy and task_key in {"translate", "translation", "file_translate", "legal_translation"}:
        chosen = translate_model or heavy_model
    else:
        chosen = heavy_model if heavy else fast_model
    if chosen in NIM_EOL_MODELS:
        logger.warning("NIM model %s is unavailable on this account; using large fallback %s", chosen, large_fallback)
        chosen = large_fallback
    if not _model_allowed(chosen):
        logger.error("NIM model %s not in allow list, falling back to %s", chosen, fast_model)
        chosen = fast_model
    return chosen


def _model_chain(preferred: str, *, heavy: bool) -> list[str]:
    """Return a de-duplicated NVIDIA model chain for one request."""
    heavy_model = os.environ.get("NVIDIA_NIM_MODEL", NIM_HEAVY_TARGET_MODEL).strip()
    large_fallback = os.environ.get("NVIDIA_NIM_MODEL_LARGE_FALLBACK", NIM_LARGE_FALLBACK_MODEL).strip()
    fast_model = os.environ.get("NVIDIA_NIM_MODEL_FAST", NIM_FINAL_FALLBACK_MODEL).strip()
    raw = [preferred]
    if heavy:
        raw.extend([heavy_model, large_fallback, fast_model])
    else:
        raw.extend([fast_model, large_fallback])
    out: list[str] = []
    for model in raw:
        model = (model or "").strip()
        if not model:
            continue
        if model in NIM_EOL_MODELS:
            mapped = large_fallback if heavy else fast_model
            logger.warning("NIM model %s is unavailable on this account; trying %s", model, mapped)
            model = mapped
        if not _model_allowed(model):
            logger.error("NIM model %s not in allow list; skipped", model)
            continue
        if model not in out:
            out.append(model)
    if not out:
        out.append(NIM_FINAL_FALLBACK_MODEL)
    return out


def _cb_can_call():
    # --- NEW: RemoteHealthGate opt-in path ---
    if os.environ.get("MAGI_USE_REMOTE_HEALTH_GATE", "0").strip().lower() in {"1", "true", "on", "yes"}:
        try:
            from api.platforms.remote_health_gate import get_gate, PeerConfig
            gate = get_gate()
            gate.register(PeerConfig(
                name="nvidia_nim",
                probe_url=None,  # NIM 沒有 health endpoint；純 mark_failure/mark_success
                fail_threshold=3,
                cooldown_seconds=(60, 120, 300),
            ))
            ok, _ = gate.is_reachable("nvidia_nim")
            return ok, ""
        except Exception:
            pass
    # legacy code unchanged below
    with _cb_lock:
        now = time.time()
        if _cb_state["cooldown_until_ts"] > now:
            remaining = int(_cb_state["cooldown_until_ts"] - now)
            return False, f"circuit_cooldown:{remaining}s:{_cb_state['last_error'][:80]}"
    return True, ""


def _cb_record_429(err: str) -> None:
    with _cb_lock:
        _cb_state["consecutive_429"] += 1
        _cb_state["last_error"] = err
        if _cb_state["consecutive_429"] >= 3:
            _cb_state["cooldown_until_ts"] = time.time() + 60
            logger.warning("NIM circuit breaker tripped: %s", err)


def _cb_record_success() -> None:
    with _cb_lock:
        _cb_state["consecutive_429"] = 0
        _cb_state["cooldown_until_ts"] = 0
        _cb_state["last_error"] = ""


def run_nim_chat(
    *,
    prompt: str,
    timeout_sec: int = 120,
    task_type: str = "general",
    require_pii_scrub: bool = True,
    system_prompt: Optional[str] = None,
    heavy: Optional[bool] = None,
    model: Optional[str] = None,
    data_classification: str = "office_confidential",
    privacy_profile: Optional[str] = None,
    restore_pii: bool = True,
    allow_model_fallback: bool = True,
    max_tokens: int = 4096,
    reasoning_effort: Optional[str] = None,
    reasoning_budget: Optional[int] = None,
    user_heavy_authorized: bool = False,
    background_heavy_authorized: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """發送 prompt 到 NVIDIA NIM 並回傳標準化結果。

    Returns:
        {
            "success": bool,
            "response": str,          # 已還原 PII 的文字
            "response_raw": str,       # 雲端原始回覆（含佔位符）
            "model": str,
            "error": str,
            "pii_scrubbed": bool,
            "pii_counts": dict,
            "duration_ms": int,
        }
    """
    t0 = time.monotonic()
    started_iso = datetime.datetime.now().isoformat()

    classification = str(data_classification or "office_confidential").strip().lower()
    allowed_classifications = {
        "office_confidential",
        "public_judgment",
        "public_source",
        "synthetic",
        "exam_practice_content",
    }
    if classification not in allowed_classifications:
        return _fail("privacy_classification_invalid")
    effective_profile = str(privacy_profile or classification).strip().lower()
    if effective_profile not in allowed_classifications:
        return _fail("privacy_profile_invalid")
    task_key = str(task_type or "").strip().lower()
    verbatim_exam_content = (
        not require_pii_scrub
        and task_key == "exam_tutor_grading"
        and classification == "exam_practice_content"
        and effective_profile == "exam_practice_content"
    )
    verbatim_public_source = (
        not require_pii_scrub
        and task_key in {"exam_tutor_trend_analysis", "exam_tutor_trend_statutory_audit"}
        and classification == "public_source"
        and effective_profile == "public_source"
    )
    # This flag is deliberately separate from ``heavy``.  ``heavy`` chooses a
    # model; only a message-level @heavy authorization permits unsanitized
    # office content.  Internal/background calls retain the privacy gate.
    verbatim_user_authorized = bool(user_heavy_authorized and heavy)
    verbatim_content = verbatim_exam_content or verbatim_public_source or verbatim_user_authorized
    content_handling = (
        "verbatim_exam_content"
        if verbatim_exam_content
        else "verbatim_public_source"
        if verbatim_public_source
        else "verbatim_user_heavy_authorized"
        if verbatim_user_authorized
        else "pii_scrubbed"
    )
    if (
        classification == "exam_practice_content"
        or effective_profile == "exam_practice_content"
    ) and not verbatim_exam_content:
        return _fail("privacy_exam_profile_scope_invalid")
    # Privacy remains mandatory for every other hosted-model call, including
    # synthetic probes.  The narrowly scoped exceptions are exam-practice
    # bodies and the trend job's already-public source excerpts: their names,
    # dockets, dates and official addresses are part of the legal context.
    if not require_pii_scrub and not verbatim_content:
        logger.warning("require_pii_scrub=False ignored; hosted-model privacy remains mandatory")
    require_pii_scrub = not verbatim_content

    # This is intentionally before any network call and applies even to an
    # explicit Heavy request.  It protects API keys, OAuth/JWT tokens, cookies
    # and passwords without attempting to redact user-provided personal data.
    if _contains_credentials(prompt, system_prompt or ""):
        return _fail("credential_blocked")

    # 1) Feature flag
    if not _env_bool("NVIDIA_NIM_ENABLE", False):
        return _fail("nim_disabled")

    # 2) API key
    api_key = (os.environ.get("NVIDIA_NIM_API_KEY") or "").strip()
    if not api_key or api_key.startswith("<<"):
        return _fail("nim_api_key_missing_or_placeholder")

    # 3) Daily budget.  Background knowledge maintenance must never consume
    # the entire allowance and starve an operator-triggered draft/translation.
    count = _get_daily_count()
    budget = _daily_budget()
    reserve = _interactive_reserve()
    if task_key in NIM_BACKGROUND_TASK_TYPES and count >= max(0, budget - reserve):
        return _fail(
            f"nim_background_budget_reserved:{count}/{max(0, budget - reserve)};daily={budget}"
        )
    if count >= budget:
        return _fail(f"nim_daily_budget_exceeded:{count}/{budget}")

    # 4) Circuit breaker
    can_call, cb_reason = _cb_can_call()
    if not can_call:
        return _fail(cb_reason)

    # 5) Model
    auto_heavy = heavy if heavy is not None else (
        len(prompt or "") >= int(os.environ.get("NVIDIA_NIM_HEAVY_THRESHOLD_CHARS", "20000") or "20000")
    )
    chosen_model = model or _pick_model(task_type, heavy=auto_heavy)
    candidate_models = (
        _model_chain(chosen_model, heavy=bool(auto_heavy))
        if allow_model_fallback
        else ([chosen_model] if _model_allowed(chosen_model) else [])
    )
    if not candidate_models:
        return _fail(f"nim_model_not_allowed:{chosen_model}")

    # Scheduled work never inherits a conversational @heavy marker.  Every
    # permitted background type must bring its own complete, expiring contract.
    if task_key in NIM_BACKGROUND_TASK_TYPES:
        authorization_error = _validate_background_heavy_authorization(
            background_heavy_authorized,
            task_type=task_key,
            source_class=classification,
            model=chosen_model,
            daily_count=count,
        )
        if authorization_error:
            return _fail(authorization_error)

    # 6) PII scrub
    scrubbed_text = str(prompt or "")
    scrubbed_system_prompt = str(system_prompt or "")
    pii_counts: Dict[str, int] = {}
    restore_fn = None
    privacy_certificate: Dict[str, Any] = {
        "policy_version": PRIVACY_POLICY_VERSION,
        "profile": effective_profile,
        "classification": classification,
        "safe_to_send": verbatim_content,
        "counts": {},
        "residual_categories": [],
        "warnings": [content_handling] if verbatim_content else [],
        "content_handling": content_handling,
    }
    if verbatim_content:
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "system_prompt": scrubbed_system_prompt,
                    "prompt": scrubbed_text,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        privacy_certificate["original_sha256"] = request_fingerprint
        privacy_certificate["scrubbed_sha256"] = request_fingerprint
    scrubber = None
    if require_pii_scrub:
        scrubber = build_scrubber_from_magi_db()
        boundary = "\u241eMAGI_PRIVACY_BOUNDARY_V2\u241e"
        if boundary in scrubbed_text or boundary in scrubbed_system_prompt:
            return _fail("privacy_boundary_collision")
        combined = f"{scrubbed_system_prompt}{boundary}{scrubbed_text}"
        scrub_result = scrubber.scrub(
            combined,
            profile=effective_profile,
            require_known_names=(effective_profile == "office_confidential"),
        )
        privacy_certificate = scrub_result.certificate()
        privacy_certificate["classification"] = classification
        if not scrub_result.safe_to_send:
            categories = ",".join(scrub_result.residual_categories) or "none"
            warnings = ",".join(scrub_result.warnings) or "none"
            return _fail(
                f"privacy_gate_blocked:residual={categories}:warning={warnings}",
                privacy_certificate=privacy_certificate,
            )
        parts = scrub_result.scrubbed_text.split(boundary, 1)
        if len(parts) != 2:
            return _fail("privacy_boundary_split_failed", privacy_certificate=privacy_certificate)
        scrubbed_system_prompt, scrubbed_text = parts
        pii_counts = scrub_result.counts
        restore_fn = scrub_result.restore

    # 7) Call NIM
    base_url = os.environ.get("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    messages = []
    if scrubbed_system_prompt:
        messages.append({"role": "system", "content": scrubbed_system_prompt})
    messages.append({"role": "user", "content": scrubbed_text})

    acquired = _nim_semaphore.acquire(blocking=True, timeout=max(5, int(timeout_sec)))
    if not acquired:
        return _fail("nim_semaphore_timeout")

    response = None
    final_error = ""
    try:
        _incr_daily_count()
        for idx, candidate_model in enumerate(candidate_models):
            payload = {
                "model": candidate_model,
                "messages": messages,
                "temperature": 0.2,
                "top_p": 0.9,
                "max_tokens": max(1, min(32768, int(max_tokens))),
                "stream": False,
            }
            if reasoning_effort in {"none", "medium", "high"}:
                payload["reasoning_effort"] = reasoning_effort
            if reasoning_budget is not None:
                payload["reasoning_budget"] = max(-1, min(32768, int(reasoning_budget)))
            r = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=(5.0, float(timeout_sec)),
            )
            err_body = (r.text or "")[:300]
            err_body_sha256 = hashlib.sha256(err_body.encode("utf-8")).hexdigest() if err_body else ""
            if r.status_code == 404 and idx + 1 < len(candidate_models):
                final_error = f"nim_http_404:{candidate_model}"
                record_nim_outcome(False, int((time.monotonic() - t0) * 1000), "http_404_retry")
                _log_usage({"ts": started_iso, "model": candidate_model, "ok": False,
                            "error": "http_404_retry", "error_body_sha256": err_body_sha256,
                            "prompt_chars": len(prompt or ""), "task": task_type})
                logger.warning("NIM model %s returned 404; trying next candidate", candidate_model)
                continue
            response = (candidate_model, r)
            break
    except requests.Timeout:
        _cb_record_429("timeout")
        record_nim_outcome(False, int((time.monotonic() - t0) * 1000), "timeout")
        _log_usage({"ts": started_iso, "model": chosen_model, "ok": False, "error": "timeout", "task": task_type})
        return _fail("nim_http_timeout")
    except Exception as e:
        record_nim_outcome(False, int((time.monotonic() - t0) * 1000), str(e)[:100])
        _log_usage({"ts": started_iso, "model": chosen_model, "ok": False, "error": str(e)[:200], "task": task_type})
        return _fail(f"nim_http_exception:{e}")
    finally:
        _nim_semaphore.release()

    if response is None:
        return _fail(final_error or "nim_no_model_response")
    chosen_model, r = response

    # Provider body may echo source text.  Persist only its SHA-256 fingerprint.
    err_body = (r.text or "")[:300]
    err_body_sha256 = hashlib.sha256(err_body.encode("utf-8")).hexdigest() if err_body else ""
    if r.status_code == 429:
        _cb_record_429("http_429")
        record_nim_outcome(False, int((time.monotonic() - t0) * 1000), "http_429")
        _log_usage({"ts": started_iso, "model": chosen_model, "ok": False,
                    "error": "http_429", "error_body_sha256": err_body_sha256,
                    "prompt_chars": len(prompt or ""), "task": task_type})
        return _fail("nim_rate_limit_429")
    if r.status_code != 200:
        record_nim_outcome(False, int((time.monotonic() - t0) * 1000), f"http_{r.status_code}")
        _log_usage({"ts": started_iso, "model": chosen_model, "ok": False,
                    "error": f"http_{r.status_code}", "error_body_sha256": err_body_sha256,
                    "prompt_chars": len(prompt or ""), "task": task_type})
        return _fail(f"nim_http_{r.status_code}", privacy_certificate=privacy_certificate)

    try:
        data = r.json()
    except Exception as e:
        return _fail(f"nim_json_decode:{e}")

    choices = (data or {}).get("choices") or []
    if not choices:
        return _fail("nim_no_choices")
    msg = (choices[0] or {}).get("message") or {}
    text_raw = str(msg.get("content") or "").strip()
    if not text_raw:
        return _fail("nim_empty_response")

    # Hosted output is untrusted too.  Block newly generated identifiers before
    # any local restoration or downstream persistence.
    response_residuals = scrubber.detect_residuals(text_raw, profile=effective_profile) if scrubber else []
    privacy_certificate["response_sha256"] = hashlib.sha256(text_raw.encode("utf-8")).hexdigest()
    privacy_certificate["response_residual_categories"] = list(response_residuals)
    if response_residuals:
        return _fail(
            "privacy_response_blocked:" + ",".join(response_residuals),
            privacy_certificate=privacy_certificate,
        )

    # 8) Restore PII
    text_final = restore_fn(text_raw) if (restore_fn and restore_pii) else text_raw

    _cb_record_success()
    duration_ms = int((time.monotonic() - t0) * 1000)
    record_nim_outcome(True, duration_ms, "")
    usage = (data or {}).get("usage") or {}
    _log_usage({
        "ts": started_iso,
        "model": chosen_model,
        "ok": True,
        "task": task_type,
        "duration_ms": duration_ms,
        "prompt_chars": len(prompt or ""),
        "response_chars": len(text_final),
        "usage": usage,
        "pii_counts": pii_counts,
    })

    return {
        "success": True,
        "response": text_final,
        "response_raw": text_raw,
        "model": chosen_model,
        "error": "",
        "pii_scrubbed": bool(pii_counts) and any(pii_counts.values()),
        "pii_counts": pii_counts,
        "content_handling": content_handling,
        "privacy_policy_version": PRIVACY_POLICY_VERSION,
        "privacy_certificate": privacy_certificate,
        "duration_ms": duration_ms,
        "usage": usage,
    }


def _fail(err: str, *, privacy_certificate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "success": False,
        "response": "",
        "response_raw": "",
        "model": "",
        "error": err,
        "pii_scrubbed": False,
        "pii_counts": {},
        "privacy_policy_version": PRIVACY_POLICY_VERSION,
        "privacy_certificate": dict(privacy_certificate or {}),
        "duration_ms": 0,
    }


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def get_usage_report(*, days: int = 1) -> Dict[str, Any]:
    """統計最近 N 天的用量（給 MAGI menubar / DC 報告用）"""
    if not USAGE_LOG_PATH.exists():
        return {"total": 0, "ok": 0, "fail": 0, "days": days}
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    total = ok = fail = 0
    models: Dict[str, int] = {}
    try:
        with open(USAGE_LOG_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                try:
                    ts = datetime.datetime.fromisoformat(row.get("ts") or "")
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                total += 1
                if row.get("ok"):
                    ok += 1
                else:
                    fail += 1
                m = str(row.get("model") or "")
                models[m] = models.get(m, 0) + 1
    except Exception as e:
        logger.warning("usage report read failed: %s", e)
    return {
        "total": total, "ok": ok, "fail": fail,
        "models": models, "days": days,
        "daily_count_today": _get_daily_count(),
        "daily_budget": _daily_budget(),
    }
