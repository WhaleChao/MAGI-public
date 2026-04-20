"""NVIDIA NIM 重型兜底 helper.

- 統一入口 run_nim_chat()
- PII scrubber 強制前置（NVIDIA_NIM_REQUIRE_PII_SCRUB=1）
- 模型白名單守門（禁用中國模型）
- Circuit breaker（連續 3 次 429 → 60s 冷卻）
- Daily budget guard（超量自動禁用當日）
- Usage log（.runtime/nvidia_nim_usage.jsonl）
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from providers.nvidia_nim import NvidiaNimProvider
from skills.engine.pii_scrubber import build_scrubber_from_magi_db

logger = logging.getLogger("NvidiaNimHeavy")

MAGI_ROOT = Path(__file__).resolve().parents[2]
USAGE_LOG_PATH = MAGI_ROOT / ".runtime" / "nvidia_nim_usage.jsonl"
STATE_PATH = MAGI_ROOT / ".runtime" / "nvidia_nim_state.json"

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
    heavy_model = os.environ.get("NVIDIA_NIM_MODEL", "meta/llama-3.1-405b-instruct").strip()
    fast_model = os.environ.get("NVIDIA_NIM_MODEL_FAST", "meta/llama-3.3-70b-instruct").strip()
    # heavy flag 或長 prompt（另由 caller 判斷）→ 405B；否則 70B
    chosen = heavy_model if heavy else fast_model
    if not NvidiaNimProvider.is_model_allowed(chosen):
        logger.error("NIM model %s not in allow list, falling back to 70b", chosen)
        chosen = "meta/llama-3.3-70b-instruct"
    return chosen


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

    # 1) Feature flag
    if not _env_bool("NVIDIA_NIM_ENABLE", False):
        return _fail("nim_disabled")

    # 2) API key
    api_key = (os.environ.get("NVIDIA_NIM_API_KEY") or "").strip()
    if not api_key or api_key.startswith("<<"):
        return _fail("nim_api_key_missing_or_placeholder")

    # 3) Daily budget
    count = _get_daily_count()
    if count >= _daily_budget():
        return _fail(f"nim_daily_budget_exceeded:{count}/{_daily_budget()}")

    # 4) Circuit breaker
    can_call, cb_reason = _cb_can_call()
    if not can_call:
        return _fail(cb_reason)

    # 5) Model
    auto_heavy = heavy if heavy is not None else (
        len(prompt or "") >= int(os.environ.get("NVIDIA_NIM_HEAVY_THRESHOLD_CHARS", "20000") or "20000")
    )
    chosen_model = model or _pick_model(task_type, heavy=auto_heavy)
    if not NvidiaNimProvider.is_model_allowed(chosen_model):
        return _fail(f"nim_model_not_allowed:{chosen_model}")

    # 6) PII scrub
    scrubbed_text = prompt
    pii_counts: Dict[str, int] = {}
    restore_fn = None
    if require_pii_scrub:
        scrubber = build_scrubber_from_magi_db()
        scrub_result = scrubber.scrub(prompt)
        scrubbed_text = scrub_result.scrubbed_text
        pii_counts = scrub_result.counts
        restore_fn = scrub_result.restore

    # 7) Call NIM
    base_url = os.environ.get("NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": scrubbed_text})

    payload = {
        "model": chosen_model,
        "messages": messages,
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": 4096,
        "stream": False,
    }

    acquired = _nim_semaphore.acquire(blocking=True, timeout=max(5, int(timeout_sec)))
    if not acquired:
        return _fail("nim_semaphore_timeout")

    try:
        _incr_daily_count()
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
    except requests.Timeout:
        _cb_record_429("timeout")
        _log_usage({"ts": started_iso, "model": chosen_model, "ok": False, "error": "timeout", "task": task_type})
        return _fail("nim_http_timeout")
    except Exception as e:
        _log_usage({"ts": started_iso, "model": chosen_model, "ok": False, "error": str(e)[:200], "task": task_type})
        return _fail(f"nim_http_exception:{e}")
    finally:
        _nim_semaphore.release()

    if r.status_code == 429:
        _cb_record_429(f"http_429:{(r.text or '')[:100]}")
        _log_usage({"ts": started_iso, "model": chosen_model, "ok": False, "error": "http_429", "task": task_type})
        return _fail("nim_rate_limit_429")
    if r.status_code != 200:
        _log_usage({"ts": started_iso, "model": chosen_model, "ok": False, "error": f"http_{r.status_code}", "task": task_type})
        return _fail(f"nim_http_{r.status_code}:{(r.text or '')[:200]}")

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

    # 8) Restore PII
    text_final = restore_fn(text_raw) if restore_fn else text_raw

    _cb_record_success()
    duration_ms = int((time.monotonic() - t0) * 1000)
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
        "duration_ms": duration_ms,
        "usage": usage,
    }


def _fail(err: str) -> Dict[str, Any]:
    return {
        "success": False,
        "response": "",
        "response_raw": "",
        "model": "",
        "error": err,
        "pii_scrubbed": False,
        "pii_counts": {},
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
