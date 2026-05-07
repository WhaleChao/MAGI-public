from __future__ import annotations

import json

from api.events.emitter import EventEmitter
from api.events.models import PreToolHookEvent, RouteDecisionEvent


def test_event_emitter_preserves_subscription_order_and_unsubscribe(tmp_path):
    emitter = EventEmitter()
    seen: list[tuple[str, str]] = []

    first = emitter.subscribe(
        "hook.route.decision",
        lambda event: seen.append(("first", event.route_name)),
    )
    second = emitter.subscribe(
        RouteDecisionEvent,
        lambda event: seen.append(("second", event.route_name)),
    )

    event = RouteDecisionEvent(route_name="osc", message="route", confidence=0.9)
    results = emitter.emit(event)

    assert seen == [("first", "osc"), ("second", "osc")]
    assert results == [None, None]

    assert first.unsubscribe() is True
    assert second.unsubscribe() is True
    seen.clear()

    emitter.emit(RouteDecisionEvent(route_name="intel"))
    assert seen == []


def test_jsonl_sink_serializes_event_models(tmp_path):
    emitter = EventEmitter()
    sink_path = tmp_path / "events.jsonl"
    emitter.add_jsonl_sink(sink_path)

    emitter.emit(
        PreToolHookEvent(
            tool_name="summarize",
            input_data={"text": "hello"},
            user_id="u1",
            platform="LINE",
            metadata={"case": "A1"},
        )
    )
    emitter.emit(
        RouteDecisionEvent(
            route_name="summary",
            confidence=0.75,
            reason="keyword match",
            candidates=["summary", "chat"],
        )
    )

    lines = sink_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    second = json.loads(lines[1])

    assert first["event_type"] == "hook.tool.pre"
    assert first["tool_name"] == "summarize"
    assert first["input_data"] == {"text": "hello"}
    assert first["metadata"] == {"case": "A1"}
    assert "T" in first["occurred_at"]

    assert second["event_type"] == "hook.route.decision"
    assert second["route_name"] == "summary"
    assert second["candidates"] == ["summary", "chat"]

