from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .paths import API_ENVELOPE_SCHEMA_PATH
from .schema import load_json, validate_json


LEGACY_SHAPES = frozenset({"json_ok", "json_success", "reply_json", "json_bare", "text_plain", "sse"})


@dataclass(frozen=True)
class LegacyResponse:
    status: int
    content_type: str
    body: Any
    headers: dict[str, str] | None = None


def assert_legacy_shape(response: LegacyResponse, expected_shape: str) -> None:
    if expected_shape not in LEGACY_SHAPES:
        raise ValueError(f"unknown legacy response shape: {expected_shape}")
    content_type = response.content_type.lower()
    if expected_shape == "json_ok":
        if (
            "json" not in content_type
            or not isinstance(response.body, dict)
            or not isinstance(response.body.get("ok"), bool)
        ):
            raise ValueError("json_ok requires a JSON object with boolean 'ok'")
    elif expected_shape == "json_success":
        if (
            "json" not in content_type
            or not isinstance(response.body, dict)
            or not isinstance(response.body.get("success"), bool)
        ):
            raise ValueError("json_success requires a JSON object with boolean 'success'")
    elif expected_shape == "reply_json":
        if "json" not in content_type or not isinstance(response.body, dict) or "reply" not in response.body:
            raise ValueError("reply_json requires a JSON object with 'reply'")
    elif expected_shape == "json_bare":
        if "json" not in content_type or not isinstance(response.body, (dict, list)):
            raise ValueError("json_bare requires a JSON object or array")
    elif expected_shape == "text_plain":
        if "text/plain" not in content_type or not isinstance(response.body, str):
            raise ValueError("text_plain requires a text/plain string body")
    elif expected_shape == "sse":
        if "text/event-stream" not in content_type or not isinstance(response.body, str):
            raise ValueError("sse requires a text/event-stream string body")


def _sse_data(body: str) -> dict[str, Any]:
    events: list[Any] = []
    done = False
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        value = line[5:].strip()
        if value == "[DONE]":
            done = True
            continue
        try:
            events.append(json.loads(value))
        except json.JSONDecodeError:
            events.append(value)
    return {"events": events, "done": done}


def _error_payload(body: Any, status: int) -> dict[str, Any]:
    if isinstance(body, dict):
        raw = body.get("error") or body.get("message") or f"legacy_http_{status}"
        details = {key: value for key, value in body.items() if key not in {"error", "message"}}
    else:
        raw = str(body or f"legacy_http_{status}")
        details = {}
    if isinstance(raw, dict):
        code = str(raw.get("code") or f"legacy_http_{status}")[:128]
        message = str(raw.get("message") or raw)[:4000]
        details = {**details, **{key: value for key, value in raw.items() if key not in {"code", "message"}}}
    else:
        message = str(raw)[:4000]
        candidate = message.strip().lower().replace(" ", "_")
        code = candidate if candidate and len(candidate) <= 128 else f"legacy_http_{status}"
    return {"code": code, "message": message, "retryable": status in {408, 425, 429, 502, 503, 504}, "details": details}


def adapt_legacy_response(
    response: LegacyResponse,
    *,
    request_id: str,
    expected_shape: str,
) -> dict[str, Any]:
    """Executable adapter specification; never performs transport I/O."""

    assert_legacy_shape(response, expected_shape)
    body = response.body
    status_ok = 200 <= response.status < 300
    if expected_shape == "sse":
        data: Any = _sse_data(str(body))
        ok = status_ok
    elif expected_shape == "text_plain":
        text = str(body)
        ok = status_ok and not text.lstrip().lower().startswith("[error]")
        data = {"text": text}
    else:
        data = body
        if isinstance(body, dict) and isinstance(body.get("ok"), bool):
            ok = status_ok and bool(body["ok"])
        elif isinstance(body, dict) and isinstance(body.get("success"), bool):
            ok = status_ok and bool(body["success"])
        else:
            ok = status_ok
    degraded = bool(isinstance(body, dict) and body.get("degraded"))
    error = None if ok else _error_payload(body, response.status)
    meta = {
        "request_id": request_id,
        "compat_version": "v2",
        "route": body.get("route") if isinstance(body, dict) else None,
        "model": body.get("model") if isinstance(body, dict) else None,
        "degraded": degraded,
        "queued": bool(isinstance(body, dict) and body.get("queued")),
        "job_id": (body.get("job_id") or body.get("task_id")) if isinstance(body, dict) else None,
        "legacy_status": response.status,
        "legacy_shape": expected_shape,
    }
    envelope = {"ok": ok, "data": data, "error": error, "meta": meta}
    validate_json(envelope, load_json(API_ENVELOPE_SCHEMA_PATH), label="adapted API envelope")
    return envelope
