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
    # probe 也要失敗才會走到 degraded 路徑（_run_with_timeout 讓 probe 回失敗）
    _orig_rwt = tools_api._run_with_timeout
    def _fail_probe(fn, wait_sec, *args, **kwargs):
        return False, {"success": False, "text": "", "error": "mocked_probe_fail"}
    monkeypatch.setattr(tools_api, "_run_with_timeout", _fail_probe)

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


def test_external_chat_applies_min_timeout_floor(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime
    monkeypatch.setenv("MAGI_EXTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("MAGI_CHAT_TIMEOUT_SEC", "150")
    monkeypatch.setenv("MAGI_EXTERNAL_CHAT_SIMPLE_TIMEOUT_OPT_IN", "0")  # force COMPLEX floor to test 240s default
    monkeypatch.delenv("MAGI_EXTERNAL_CHAT_MIN_TIMEOUT_SEC", raising=False)
    tools_api._EXTERNAL_KEY_CACHE["ts"] = 0.0
    tools_api._EXTERNAL_KEY_CACHE["value"] = ""

    class _FakeOrch:
        def process_message(self, user_id, message, platform, role):
            return f"ok:{user_id}:{platform}:{role}:{message}"

    captured = {}

    def _fake_timeout(fn, wait_sec, *args, **kwargs):
        captured["wait_sec"] = wait_sec
        return True, fn()

    monkeypatch.setattr(tools_api, "_get_osc_orchestrator", lambda: _FakeOrch())
    monkeypatch.setattr(tools_api, "_run_with_timeout", _fake_timeout)

    response = client.post(
        "/osc/external/chat",
        json={
            "user_id": "external_api_user",
            "platform": "WEB",
            "message": "我覺得綠茶滿好喝的，那你呢，你覺得好喝嗎",
            "timeout_sec": 45,
            "async": False,
        },
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert captured["wait_sec"] == 240


def test_external_chat_simple_timeout_opt_in(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime
    monkeypatch.setenv("MAGI_EXTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("MAGI_CHAT_TIMEOUT_SEC", "150")
    monkeypatch.setenv("MAGI_EXTERNAL_CHAT_SIMPLE_TIMEOUT_OPT_IN", "1")
    monkeypatch.setenv("MAGI_EXTERNAL_CHAT_SIMPLE_MIN_TIMEOUT_SEC", "45")
    monkeypatch.delenv("MAGI_EXTERNAL_CHAT_MIN_TIMEOUT_SEC", raising=False)
    tools_api._EXTERNAL_KEY_CACHE["ts"] = 0.0
    tools_api._EXTERNAL_KEY_CACHE["value"] = ""

    class _FakeOrch:
        def process_message(self, user_id, message, platform, role):
            return f"ok:{user_id}:{platform}:{role}:{message}"

    captured = {}

    def _fake_timeout(fn, wait_sec, *args, **kwargs):
        captured["wait_sec"] = wait_sec
        return True, fn()

    monkeypatch.setattr(tools_api, "_get_osc_orchestrator", lambda: _FakeOrch())
    monkeypatch.setattr(tools_api, "_run_with_timeout", _fake_timeout)

    response = client.post(
        "/osc/external/chat",
        json={
            "user_id": "external_api_user",
            "platform": "WEB",
            "message": "我覺得綠茶滿好喝的，那你呢，你覺得好喝嗎",
            "timeout_sec": 20,
            "async": False,
        },
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert captured["wait_sec"] == 45


def test_summarize_circuit_open_uses_resilient_probe(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime
    monkeypatch.setattr(tools_api, "_summarize_cb_allow_upstream", lambda: False)

    from api.handlers import summary_handler as _summary_handler

    def _fake_resilient(text, summary_length="medium", progress_callback=None):
        return {
            "success": True,
            "text": "【重點摘要】\n- 已由 resilient 路徑產生可用摘要。",
            "provider": "resilient_probe",
        }

    monkeypatch.setattr(_summary_handler, "summarize_text_resilient", _fake_resilient)

    response = client.post(
        "/summarize",
        json={"text": "這是一段需要摘要的長文字。" * 20, "summary_length": "medium", "timeout_sec": 45},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["result"]["success"] is True
    assert payload["result"].get("degraded") is not True
    assert "resilient" in payload["note"]


def test_summarize_timeout_uses_extractive_fallback(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime

    from api.handlers import summary_handler as _summary_handler

    calls = {"count": 0, "cb_success": 0}

    def _fake_resilient(text, summary_length="medium", progress_callback=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"success": False, "error": "timeout_exceeded_46s"}
        return {
            "success": True,
            "text": "• 第一重點\n• 第二重點",
            "provider": "extractive_fallback",
        }

    monkeypatch.setattr(_summary_handler, "summarize_text_resilient", _fake_resilient)
    monkeypatch.setattr(tools_api, "_summarize_cb_note_success", lambda: calls.__setitem__("cb_success", calls["cb_success"] + 1))

    response = client.post(
        "/summarize",
        json={"text": "這是一段需要摘要的長文字。" * 20, "summary_length": "medium", "timeout_sec": 45},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["result"]["success"] is True
    assert payload["result"]["provider"] == "extractive_fallback"
    assert payload["result"]["degraded"] is False
    assert calls["cb_success"] == 1
