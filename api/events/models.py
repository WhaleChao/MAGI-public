from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass()
class EventModel:
    """Base event payload for the MAGI runtime."""

    event_type: ClassVar[str] = "event.base"
    event_id: str = field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = field(default_factory=_utcnow)
    source: str = "magi"
    correlation_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["event_type"] = self.event_type
        data["occurred_at"] = self.occurred_at.isoformat()
        return data

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, default=str, separators=(",", ":"))


@dataclass()
class PreToolHookEvent(EventModel):
    event_type: ClassVar[str] = "hook.tool.pre"

    tool_name: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    user_id: str = ""
    platform: str = ""


@dataclass()
class PostToolHookEvent(EventModel):
    event_type: ClassVar[str] = "hook.tool.post"

    tool_name: str = ""
    output_data: Any = None
    ok: bool = True
    status: str = "ok"
    duration_ms: Optional[float] = None
    error: str = ""


@dataclass()
class RouteDecisionEvent(EventModel):
    event_type: ClassVar[str] = "hook.route.decision"

    route_name: str = ""
    confidence: float = 0.0
    reason: str = ""
    message: str = ""
    candidates: list[str] = field(default_factory=list)


@dataclass()
class FallbackEvent(EventModel):
    event_type: ClassVar[str] = "hook.fallback"

    fallback_name: str = ""
    stage: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass()
class MemoryWriteEvent(EventModel):
    event_type: ClassVar[str] = "hook.memory.write"

    memory_kind: str = ""
    content: Any = None
    accepted: bool = True
    user_id: str = ""
    platform: str = ""
    source_signature: str = ""
    memory_key: str = ""


@dataclass()
class TaskLifecycleEvent(EventModel):
    event_type: ClassVar[str] = "task.lifecycle"

    task_id: str = ""
    task_name: str = ""
    status: str = ""
    progress: Optional[float] = None
    user_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
