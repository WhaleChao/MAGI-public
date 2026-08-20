"""User-facing health language without operational identifiers or traces."""

from __future__ import annotations

from typing import Any


_UNSAFE_TOKENS = ("job_id", "returncode", "trace", "traceback", "stack", "exception")


def _safe_text(value: Any) -> str:
    text = str(value or "").strip()
    if any(token in text.lower() for token in _UNSAFE_TOKENS):
        return "系統已記錄技術細節，正在處理。"
    return text[:240]


def present_health(*, status: str, ready: bool, components: dict[str, Any]) -> dict[str, str]:
    """Map raw state to one stable, plain-language user status.

    This projection deliberately never exposes raw scheduler identifiers,
    process exit values, or traceback text.  Callers can retain the raw health
    structure for authenticated diagnostics separately.
    """
    raw = str(status or "").lower()
    reasons = components.get("reasons") if isinstance(components, dict) else []
    reason = _safe_text((reasons or [""])[0] if isinstance(reasons, list) else reasons)
    if raw in {"failed", "error", "unhealthy", "not_ready"}:
        return {"color": "red", "state": "fault", "label": "需要處理", "detail": reason or "必要服務尚未就緒。"}
    if raw in {"deferred", "waiting", "paused"}:
        return {"color": "yellow", "state": "deferred", "label": "安全等待", "detail": reason or "等待資源或下一個安全時段後會續跑。"}
    if raw in {"quality_debt", "partial", "review_needed", "degraded"}:
        return {"color": "orange", "state": "quality_debt", "label": "品質待補", "detail": reason or "資料仍待核對或補強，尚未宣告完成。"}
    if raw in {"initial", "initial_run", "running", "live"}:
        state = "initial_run" if raw in {"initial", "initial_run"} else "running"
        detail = "正在建立首次檢查基準。" if state == "initial_run" else "工作正在安全執行中。"
        return {"color": "blue", "state": state, "label": "正在執行", "detail": detail}
    if not ready:
        return {"color": "red", "state": "fault", "label": "需要處理", "detail": reason or "必要服務尚未就緒。"}
    return {"color": "green", "state": "completed", "label": "已完成", "detail": "已完成並保有可核對的結果。"}
