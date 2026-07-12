"""Private machine metadata carried by MAGI-created calendar todo rows."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping


CALENDAR_AGENT_SOURCE_PREFIX = "manual_dispatch:calendar_agent:"


def encode_calendar_source(metadata: Mapping[str, Any]) -> str:
    allowed = {
        "end": str(metadata.get("end") or ""),
        "rrule": str(metadata.get("rrule") or ""),
        "all_day": bool(metadata.get("all_day")),
    }
    raw = json.dumps(allowed, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return CALENDAR_AGENT_SOURCE_PREFIX + token


def decode_calendar_source(value: object) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text.startswith(CALENDAR_AGENT_SOURCE_PREFIX):
        return None
    token = text[len(CALENDAR_AGENT_SOURCE_PREFIX):]
    if not token:
        return None
    try:
        padded = token + ("=" * (-len(token) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "end": str(payload.get("end") or ""),
        "rrule": str(payload.get("rrule") or ""),
        "all_day": bool(payload.get("all_day")),
    }
