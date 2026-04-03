from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.hooks import HookBus
from api.permissions import (
    PermissionEnforcer,
    PermissionMode,
    PermissionPolicy,
    deny_command,
)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def tools_api_runtime(monkeypatch, tmp_path):
    import api.tools_api as tools_api

    events_path = tmp_path / "tools_runtime_events.jsonl"
    metrics_path = tmp_path / "summarize_metrics.jsonl"
    hook_bus = HookBus(source="test.tools_api")
    hook_bus.add_jsonl_sink(events_path)

    monkeypatch.setattr(tools_api, "_TOOLS_EVENTS_PATH", str(events_path))
    monkeypatch.setattr(tools_api, "_TOOLS_HOOK_BUS", hook_bus)
    monkeypatch.setattr(tools_api, "SUMMARY_METRICS_PATH", str(metrics_path))
    monkeypatch.setattr(
        tools_api,
        "_TOOLS_PERMISSION_ENFORCER",
        PermissionEnforcer(
            policy=PermissionPolicy.from_rules([], mode=PermissionMode.PERMISSIVE)
        ),
    )

    return tools_api, tools_api.app.test_client(), events_path


def test_search_emits_pre_and_post_events(monkeypatch, tools_api_runtime):
    tools_api, client, events_path = tools_api_runtime
    monkeypatch.setattr(
        tools_api,
        "search_web",
        lambda query, num_results: {
            "query": query,
            "num_results": num_results,
            "results": [{"title": "ok"}],
        },
    )

    response = client.post(
        "/search",
        json={"query": "MAGI", "num_results": 3},
        headers={"X-Request-ID": "req-search", "X-User-ID": "u1", "X-Platform": "LINE"},
    )

    assert response.status_code == 200
    events = _read_jsonl(events_path)
    assert [event["event_type"] for event in events] == ["hook.tool.pre", "hook.tool.post"]
    assert events[0]["tool_name"] == "search"
    assert events[0]["correlation_id"] == "req-search"
    assert events[1]["tool_name"] == "search"
    assert events[1]["status"] == "handled"
    assert events[1]["ok"] is True


def test_search_denial_emits_denied_post_event(monkeypatch, tools_api_runtime):
    tools_api, client, events_path = tools_api_runtime
    monkeypatch.setattr(
        tools_api,
        "_TOOLS_PERMISSION_ENFORCER",
        PermissionEnforcer(
            policy=PermissionPolicy.from_rules(
                [
                    deny_command(
                        name="deny-search",
                        commands=("tool:search",),
                        reason="blocked for test",
                        priority=1,
                    )
                ],
                mode=PermissionMode.PERMISSIVE,
            )
        ),
    )

    response = client.post("/search", json={"query": "MAGI"})

    assert response.status_code == 403
    payload = response.get_json()
    assert "permission_denied" in payload["error"]

    events = _read_jsonl(events_path)
    assert [event["event_type"] for event in events] == ["hook.tool.pre", "hook.tool.post"]
    assert events[1]["status"] == "denied"
    assert events[1]["ok"] is False
    assert "permission_denied" in events[1]["error"]


def test_search_exception_emits_error_post_event(monkeypatch, tools_api_runtime):
    tools_api, client, events_path = tools_api_runtime

    def _boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(tools_api, "search_web", _boom)

    response = client.post("/search", json={"query": "MAGI"})

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"] == "search_exception: network down"

    events = _read_jsonl(events_path)
    assert [event["event_type"] for event in events] == ["hook.tool.pre", "hook.tool.post"]
    assert events[1]["status"] == "error"
    assert events[1]["ok"] is False
    assert events[1]["error"] == "search_exception: network down"


def test_summarize_circuit_breaker_degraded_path_emits_post_event(monkeypatch, tools_api_runtime):
    tools_api, client, events_path = tools_api_runtime
    monkeypatch.setattr(tools_api, "_summarize_cb_allow_upstream", lambda: False)

    response = client.post("/summarize", json={"text": "這是一段需要摘要的長文字。" * 5})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["result"]["degraded"] is True
    assert payload["result"]["provider"] == "circuit_open_degraded"

    events = _read_jsonl(events_path)
    assert [event["event_type"] for event in events] == ["hook.tool.pre", "hook.tool.post"]
    assert events[1]["tool_name"] == "summarize"
    assert events[1]["status"] == "degraded"
    assert events[1]["ok"] is True
    assert events[1]["error"] == "circuit_open"
