from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class SessionMessage:
    role: str
    content: str
    created_at: datetime = field(default_factory=utcnow)
    source: str = "raw"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionSummary:
    text: str
    created_at: datetime = field(default_factory=utcnow)
    source: str = "derived"
    authoritative: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionPendingState:
    values: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class SessionContext:
    session_id: str
    raw_history: list[SessionMessage]
    summaries: list[SessionSummary]
    pending_state: dict[str, Any]
    assembled_messages: list[dict[str, Any]]
    rendered_text: str
