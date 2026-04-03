from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from api.events.emitter import EventEmitter, Subscription
from api.events.models import (
    FallbackEvent,
    MemoryWriteEvent,
    PostToolHookEvent,
    PreToolHookEvent,
    RouteDecisionEvent,
)

HookCallback = Callable[[object], Any]


@dataclass(slots=True)
class HookBus:
    """Typed hook bus for runtime lifecycle events."""

    emitter: EventEmitter = field(default_factory=EventEmitter)
    source: str = "magi"

    def subscribe(self, event_type: str | type, callback: HookCallback) -> Subscription:
        return self.emitter.subscribe(event_type, callback)

    def add_jsonl_sink(self, path: str | Path, event_type: str | type = "*") -> Subscription:
        return self.emitter.add_jsonl_sink(path, event_type=event_type)

    def publish(self, event):
        self.emitter.emit(event)
        return event

    def pre_tool(
        self,
        tool_name: str,
        *,
        input_data: dict[str, Any] | None = None,
        user_id: str = "",
        platform: str = "",
        correlation_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> PreToolHookEvent:
        event = PreToolHookEvent(
            tool_name=tool_name,
            input_data=dict(input_data or {}),
            user_id=user_id,
            platform=platform,
            source=self.source,
            correlation_id=correlation_id,
            metadata=dict(metadata or {}),
        )
        return self.publish(event)

    def post_tool(
        self,
        tool_name: str,
        *,
        output_data: Any = None,
        ok: bool = True,
        status: str = "ok",
        duration_ms: float | None = None,
        error: str = "",
        correlation_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> PostToolHookEvent:
        event = PostToolHookEvent(
            tool_name=tool_name,
            output_data=output_data,
            ok=ok,
            status=status or ("ok" if ok else "error"),
            duration_ms=duration_ms,
            error=error,
            source=self.source,
            correlation_id=correlation_id,
            metadata=dict(metadata or {}),
        )
        return self.publish(event)

    def route_decision(
        self,
        route_name: str,
        *,
        confidence: float = 0.0,
        reason: str = "",
        message: str = "",
        candidates: list[str] | None = None,
        correlation_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> RouteDecisionEvent:
        event = RouteDecisionEvent(
            route_name=route_name,
            confidence=confidence,
            reason=reason,
            message=message,
            candidates=list(candidates or []),
            source=self.source,
            correlation_id=correlation_id,
            metadata=dict(metadata or {}),
        )
        return self.publish(event)

    def fallback(
        self,
        fallback_name: str,
        *,
        stage: str = "",
        reason: str = "",
        detail: dict[str, Any] | None = None,
        correlation_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> FallbackEvent:
        event = FallbackEvent(
            fallback_name=fallback_name,
            stage=stage,
            reason=reason,
            detail=dict(detail or {}),
            source=self.source,
            correlation_id=correlation_id,
            metadata=dict(metadata or {}),
        )
        return self.publish(event)

    def memory_write(
        self,
        memory_kind: str,
        *,
        content: Any = None,
        accepted: bool = True,
        user_id: str = "",
        platform: str = "",
        source_signature: str = "",
        memory_key: str = "",
        correlation_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> MemoryWriteEvent:
        event = MemoryWriteEvent(
            memory_kind=memory_kind,
            content=content,
            accepted=accepted,
            user_id=user_id,
            platform=platform,
            source_signature=source_signature,
            memory_key=memory_key,
            source=self.source,
            correlation_id=correlation_id,
            metadata=dict(metadata or {}),
        )
        return self.publish(event)

