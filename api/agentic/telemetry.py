"""Public-safe agent status telemetry.

This module intentionally projects an internal agent snapshot into a small,
categorical document suitable for a static public endpoint.  It never forwards
free-form task data, prompts, tool arguments, or arbitrary metadata.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


PUBLIC_AGENT_STATUS_SCHEMA_VERSION = 1
PUBLIC_AGENT_STATUS_FILENAME = "agent_status_public_latest.json"
PUBLIC_AGENT_STATUS_PATH = Path(__file__).resolve().parents[2] / "static" / PUBLIC_AGENT_STATUS_FILENAME
PUBLIC_AGENT_STATUS_PATH_ENV = "MAGI_AGENT_STATUS_PUBLIC_PATH"

PUBLIC_AGENT_STATES = frozenset({"shadow", "ready", "running", "blocked", "degraded", "completed"})
PUBLIC_INTENT_CATEGORIES = frozenset(
    {
        "general",
        "cases",
        "clients",
        "calendar",
        "todos",
        "documents",
        "files",
        "nas",
        "drive",
        "research",
        "legal",
        "legal_statutes",
        "judgments",
        "laf",
        "file_review",
        "transcript",
        "transcription",
        "translation",
        "ocr",
        "drafting",
        "accounting",
        "quotation",
        "memory",
        "obsidian",
        "realtime",
        "web",
        "models",
        "system",
        "backup",
        "notifications",
        "automation",
    }
)
PUBLIC_PLAN_STATUSES = frozenset(
    {
        "draft",
        "awaiting_input",
        "awaiting_confirmation",
        "ready",
        "running",
        "succeeded",
        "failed",
        "blocked",
        "cancelled",
        "completed",
    }
)
PUBLIC_ACTIONS = frozenset(
    {
        "classify",
        "route",
        "check_permissions",
        "retrieve",
        "execute",
        "verify",
        "respond",
        "await_confirmation",
    }
)
PUBLIC_TOOL_CATEGORIES = frozenset(
    {
        "web",
        "search",
        "fetch",
        "database",
        "calendar",
        "drive",
        "files",
        "nas",
        "documents",
        "todos",
        "file_review",
        "transcript",
        "transcription",
        "translation",
        "ocr",
        "laf",
        "legal",
        "accounting",
        "memory",
        "notifications",
        "code",
        "system",
        "none",
    }
)
PUBLIC_SIDE_EFFECTS = frozenset(
    {
        "none",
        "read",
        "write",
        "read_only",
        "local_draft",
        "reversible_write",
        "external_commit",
        "destructive",
    }
)
PUBLIC_VERIFICATION_STATES = frozenset({"not_run", "pending", "passed", "failed", "unavailable"})
PUBLIC_HEALTH_STATES = frozenset({"offline", "unknown", "healthy", "degraded", "unhealthy"})
PUBLIC_ERROR_CATEGORIES = frozenset(
    {
        "none",
        "auth_required",
        "login_failed",
        "path_missing",
        "external_service",
        "validation_failed",
        "unknown",
    }
)
PUBLIC_STEP_COUNT_KEYS = ("total", "pending", "running", "succeeded", "failed", "skipped", "cancelled")

# A public status document must not preserve any private tree, even when a
# producer places it below an otherwise public-looking mapping.
_PRIVATE_KEY_FRAGMENTS = (
    "prompt",
    "message",
    "content",
    "thought",
    "reasoning",
    "user",
    "case",
    "client",
    "path",
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "query",
    "trace",
    "stack",
    "exception",
    "utterance",
)


def build_public_agent_status(snapshot: Mapping[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    """Return the public allowlist projection of an agent snapshot.

    Missing or malformed input returns a deliberately inert ``shadow`` status.
    No free-form source value is emitted: each field has a bounded type and an
    explicit allowlist before it enters the returned document.
    """
    source = _strip_private_fields(snapshot if isinstance(snapshot, Mapping) else {})
    if overrides:
        source.update(_strip_private_fields(overrides))

    payload: dict[str, Any] = {
        "schema_version": PUBLIC_AGENT_STATUS_SCHEMA_VERSION,
        "status": _allowed_value(_first(source, "status", "state"), PUBLIC_AGENT_STATES, default="shadow"),
    }

    intent_category = _intent_category(source)
    if intent_category:
        payload["intent_category"] = intent_category

    confidence = _confidence(_first(source, "confidence", "route_confidence"))
    if confidence is not None:
        payload["confidence"] = confidence
        # Retain the existing public dashboard's name without emitting more data.
        payload["route_confidence"] = confidence

    plan = _mapping(source.get("plan"))
    plan_status = _allowed_value(
        _first(source, "plan_status") if "plan_status" in source else plan.get("status"),
        PUBLIC_PLAN_STATUSES,
    )
    if plan_status:
        payload["plan_status"] = plan_status

    step_counts = _step_counts(source, plan)
    if step_counts:
        payload["step_counts"] = step_counts

    current_action = _allowed_value(
        _first(source, "current_action", "action") if any(key in source for key in ("current_action", "action")) else plan.get("current_action"),
        PUBLIC_ACTIONS,
    )
    if current_action:
        payload["current_action"] = current_action
        step_state = _step_state(plan_status)
        if step_state:
            payload["plan_steps"] = [{"id": current_action, "state": step_state}]

    tool_category = _tool_category(source)
    if tool_category:
        payload["tool_category"] = tool_category

    side_effect = _allowed_value(source.get("side_effect"), PUBLIC_SIDE_EFFECTS)
    if side_effect:
        payload["side_effect"] = side_effect

    confirmation = _confirmation_pending(source)
    if confirmation is not None:
        payload["waiting_confirmation"] = confirmation

    verification = _verification_status(source.get("verification"))
    if verification:
        payload["verification"] = {"status": verification}

    health = _health_status(source.get("health"))
    if health:
        payload["health"] = {"status": health}
    elif payload["status"] == "shadow":
        # No producer activity must be visibly inert, rather than accidentally
        # implying that an internal component is healthy.
        payload["health"] = {"status": "offline"}

    degraded = _degraded(source.get("degraded"), payload["status"])
    if degraded is not None:
        payload["degraded"] = {"active": degraded}

    last_success = _bool_value(source.get("last_success")) if "last_success" in source else None
    if last_success is not None:
        payload["last_success"] = last_success

    error_category = _error_category(source)
    if error_category:
        payload["error_category"] = error_category

    # Defence in depth: the builder itself only creates allowlisted values, but
    # strip once more so future schema additions cannot accidentally expose a
    # nested private field.
    return _strip_private_fields(payload)


def write_public_agent_status(
    snapshot: Mapping[str, Any] | None = None,
    *,
    path: Path | str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Atomically publish an allowlisted status document and return it."""
    payload = build_public_agent_status(snapshot, **overrides)
    # ``published_at_epoch`` lets static clients expire a terminal snapshot.
    # It is generated here rather than accepted from the caller, so an
    # arbitrary producer cannot forge freshness or inject free-form data.
    payload["published_at_epoch"] = int(time.time())
    destination = Path(path) if path is not None else public_agent_status_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise
    return payload


def public_agent_status_path() -> Path:
    """Resolve the mutable destination at write time.

    Tests and offline tools can redirect telemetry without importing this
    module in a special order. Production keeps the historical static path
    unless an operator explicitly configures the environment variable.
    """

    configured = str(os.environ.get(PUBLIC_AGENT_STATUS_PATH_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    mutable_static = str(os.environ.get("MAGI_MUTABLE_STATIC_DIR") or "").strip()
    if mutable_static:
        return Path(mutable_static).expanduser().resolve() / PUBLIC_AGENT_STATUS_FILENAME
    if str(os.environ.get("MAGI_TEST_MODE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return Path(tempfile.gettempdir()) / f"magi_test_agent_status_{os.getpid()}.json"
    return PUBLIC_AGENT_STATUS_PATH


# Clear aliases for call sites that phrase publishing as a sanitisation step.
sanitize_public_agent_status = build_public_agent_status
publish_public_agent_status = write_public_agent_status


def _strip_private_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_private_fields(item)
            for key, item in value.items()
            if isinstance(key, str) and not _is_private_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_strip_private_fields(item) for item in value]
    return value


def _is_private_key(key: str) -> bool:
    normalized = "".join(character for character in key.lower() if character.isalnum())
    return any(fragment in normalized for fragment in _PRIVATE_KEY_FRAGMENTS)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def _raw_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _allowed_value(value: Any, allowed: frozenset[str], *, default: str = "") -> str:
    normalized = _raw_value(value)
    return normalized if normalized in allowed else default


def _intent_category(source: Mapping[str, Any]) -> str:
    raw = _first(source, "intent_category", "category")
    if raw is None:
        intent = source.get("intent")
        raw = _mapping(intent).get("category") if isinstance(intent, Mapping) else intent
    return _allowed_value(raw, PUBLIC_INTENT_CATEGORIES)


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return confidence


def _step_counts(source: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, int]:
    raw_counts = _mapping(source.get("step_counts")) or _mapping(plan.get("step_counts"))
    counts: dict[str, int] = {}
    for key in PUBLIC_STEP_COUNT_KEYS:
        value = raw_counts.get(key)
        if value is None:
            value = _first(source, f"{key}_steps", f"{key}_count")
        count = _nonnegative_int(value)
        if count is not None:
            counts[key] = count
    return counts


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 99999 else None


def _step_state(plan_status: str) -> str:
    return {
        "draft": "pending",
        "awaiting_input": "pending",
        "awaiting_confirmation": "pending",
        "ready": "pending",
        "running": "running",
        "succeeded": "done",
        "completed": "done",
        "failed": "blocked",
        "blocked": "blocked",
        "cancelled": "skipped",
    }.get(plan_status, "")


def _tool_category(source: Mapping[str, Any]) -> str:
    raw = source.get("tool_category")
    if raw is None:
        tool = source.get("tool")
        raw = _mapping(tool).get("category") if isinstance(tool, Mapping) else tool
    return _allowed_value(raw, PUBLIC_TOOL_CATEGORIES)


def _confirmation_pending(source: Mapping[str, Any]) -> bool | None:
    if "confirmation" not in source and "waiting_confirmation" not in source and "requires_confirmation" not in source:
        return None
    if "waiting_confirmation" in source:
        return _bool_value(source.get("waiting_confirmation"))
    confirmation = source.get("confirmation")
    if isinstance(confirmation, Mapping):
        pending = _bool_value(_first(confirmation, "pending", "waiting"))
        if pending is not None:
            return pending
        required = _bool_value(confirmation.get("required"))
        confirmed = _bool_value(confirmation.get("confirmed"))
        return bool(required) and confirmed is not True
    direct = _bool_value(confirmation)
    return direct if direct is not None else _bool_value(source.get("requires_confirmation"))


def _verification_status(value: Any) -> str:
    if isinstance(value, Mapping):
        status = _allowed_value(_first(value, "status", "state"), PUBLIC_VERIFICATION_STATES)
        if status:
            return status
        ok = _bool_value(value.get("ok"))
        return "passed" if ok is True else "failed" if ok is False else ""
    if isinstance(value, bool):
        return "passed" if value else "failed"
    return _allowed_value(value, PUBLIC_VERIFICATION_STATES)


def _health_status(value: Any) -> str:
    if isinstance(value, Mapping):
        status = _allowed_value(_first(value, "status", "state"), PUBLIC_HEALTH_STATES)
        if status:
            return status
        ok = _bool_value(value.get("ok"))
        return "healthy" if ok is True else "unhealthy" if ok is False else ""
    if isinstance(value, bool):
        return "healthy" if value else "unhealthy"
    return _allowed_value(value, PUBLIC_HEALTH_STATES)


def _degraded(value: Any, status: str) -> bool | None:
    if value is None:
        return True if status == "degraded" else None
    if isinstance(value, Mapping):
        active = _bool_value(_first(value, "active", "enabled"))
        return active if active is not None else bool(value)
    return _bool_value(value)


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return None


def _error_category(source: Mapping[str, Any]) -> str:
    raw = _first(source, "error_category", "last_error_category")
    if raw is None:
        raw = _mapping(source.get("last_error")).get("category")
    return _allowed_value(raw, PUBLIC_ERROR_CATEGORIES)
