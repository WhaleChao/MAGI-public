from __future__ import annotations

import json
from typing import Any


RUNTIME_ERROR_CATEGORIES = {
    "nas_path_timeout",
    "google_token_expired",
    "drive_auth_denied",
    "file_stage_failed",
    "python_ssl_malloc_crash",
    "model_unavailable",
    "unknown",
}

MODEL_HEALTH_CLASSIFICATIONS = {
    "ok",
    "overload",
    "unavailable",
    "unknown",
}


def _normalize_error_text(payload: Any) -> str:
    if isinstance(payload, Exception):
        return f"{payload.__class__.__name__}: {payload!s}"
    if isinstance(payload, dict):
        values: list[str] = []
        for key in ("error", "detail", "message", "status", "reason", "model", "payload", "description"):
            value = payload.get(key)
            if value is not None:
                values.append(f"{value}")
        if "error" in payload and not values:
            values.append(str(payload["error"]))
        try:
            values.append(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass
        return " ".join(values)
    return str(payload or "")


def classify_runtime_error(payload: Any) -> str:
    text = _normalize_error_text(payload).lower()
    if "system copy staging failed" in text or "file stage failed" in text or ("staging" in text and "failed" in text):
        return "file_stage_failed"
    if any(token in text for token in ("stat timeout", "path is on nas", "/.magi_mounts", "nfs", "/volumes/")):
        return "nas_path_timeout"
    if any(token in text for token in ("token expired", "expired", "google token", "google.oauth", "invalid_grant", "refresh token")):
        return "google_token_expired"
    if any(token in text for token in ("drive", "permission denied", "drive api", "auth", "unauthorized", "access denied", "401", "403")):
        return "drive_auth_denied"
    if any(token in text for token in ("python ssl", "ssl", "ssl malloc", "malloc", "memory allocation", "out of memory")):
        return "python_ssl_malloc_crash"
    if any(token in text for token in ("model unavailable", "model not found", "model_is_unavailable", "unavailable model")):
        return "model_unavailable"
    return "unknown"


def classify_model_health(payload: Any) -> str:
    """Classify model health payload into ui-ready states."""
    if isinstance(payload, Exception):
        text = _normalize_error_text(payload).lower()
        if "overload" in text or "429" in text:
            return "overload"
        if "unavailable" in text or "not available" in text:
            return "unavailable"
        return "unknown"

    if not isinstance(payload, dict):
        return "unknown"

    if isinstance(payload.get("ok"), bool) and not payload["ok"]:
        return "unavailable"

    status = str(payload.get("status", "")).strip().lower()
    if status in {"overload", "overloaded", "busy", "throttled"}:
        return "overload"
    if status in {"unavailable", "off", "down", "error", "failed", "offline"}:
        return "unavailable"
    if "load" in status and "high" in status:
        return "overload"

    if payload.get("available") is False:
        return "unavailable"
    if payload.get("queue_depth", 0) and int(payload["queue_depth"]) >= 20:
        return "overload"
    if payload.get("rps", 0) and float(payload["rps"]) < 0:
        return "overload"

    detail = _normalize_error_text(payload).lower()
    if "overload" in detail:
        return "overload"
    if "unavailable" in detail:
        return "unavailable"

    if status in {"ok", "ready", "running", "healthy", "online"}:
        return "ok"
    return "unknown"
