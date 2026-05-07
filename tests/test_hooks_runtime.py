from __future__ import annotations

from api.hooks.bus import HookBus


def test_hook_bus_emits_hooks_in_call_order():
    bus = HookBus(source="test-suite")
    seen: list[str] = []

    bus.subscribe("*", lambda event: seen.append(event.event_type))

    bus.pre_tool("summarize")
    bus.post_tool("summarize")
    bus.route_decision("summary")
    bus.fallback("summary-fallback")
    bus.memory_write("chatlog")

    assert seen == [
        "hook.tool.pre",
        "hook.tool.post",
        "hook.route.decision",
        "hook.fallback",
        "hook.memory.write",
    ]


def test_hook_bus_respects_subscription_order_with_same_event():
    bus = HookBus(source="test-suite")
    seen: list[str] = []

    bus.subscribe("hook.tool.pre", lambda event: seen.append("first"))
    bus.subscribe("hook.tool.pre", lambda event: seen.append("second"))

    event = bus.pre_tool("fetch", input_data={"url": "https://example.com"})

    assert event.tool_name == "fetch"
    assert seen == ["first", "second"]

