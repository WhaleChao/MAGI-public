from __future__ import annotations

import json
import sys
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
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root))
    sys.modules.pop("api.tools_api", None)
    sys.modules.pop("api", None)
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


def test_skill_runtime_flags_default_to_non_mutating(monkeypatch, tools_api_runtime):
    tools_api, _client, _events_path = tools_api_runtime
    monkeypatch.delenv("MAGI_DEV_SKILL_RUNTIME_MUTATIONS", raising=False)
    monkeypatch.delenv("MAGI_SKILL_AUTO_REPAIR_DEFAULT", raising=False)
    monkeypatch.delenv("MAGI_SKILL_AUTO_INSTALL_DEPS_DEFAULT", raising=False)
    monkeypatch.delenv("MAGI_SKILL_ROLLBACK_ON_FAIL_DEFAULT", raising=False)

    flags = tools_api._resolve_skill_runtime_flags({})

    assert flags["auto_repair"] is False
    assert flags["auto_install_deps"] is False
    assert flags["rollback_on_fail"] is False
    assert flags["mutating_runtime_requested"] is False
    assert flags["dev_env_opt_in"] is False


def test_skill_runtime_flags_require_explicit_dev_opt_in(monkeypatch, tools_api_runtime):
    tools_api, _client, _events_path = tools_api_runtime
    monkeypatch.setenv("MAGI_DEV_SKILL_RUNTIME_MUTATIONS", "1")
    monkeypatch.delenv("MAGI_SKILL_AUTO_REPAIR_DEFAULT", raising=False)
    monkeypatch.delenv("MAGI_SKILL_AUTO_INSTALL_DEPS_DEFAULT", raising=False)
    monkeypatch.delenv("MAGI_SKILL_ROLLBACK_ON_FAIL_DEFAULT", raising=False)

    flags = tools_api._resolve_skill_runtime_flags({})

    assert flags["auto_repair"] is True
    assert flags["auto_install_deps"] is True
    assert flags["rollback_on_fail"] is True
    assert flags["mutating_runtime_requested"] is True
    assert flags["dev_env_opt_in"] is True


def test_skill_runtime_flags_payload_can_request_single_mutation(monkeypatch, tools_api_runtime):
    tools_api, _client, _events_path = tools_api_runtime
    monkeypatch.delenv("MAGI_DEV_SKILL_RUNTIME_MUTATIONS", raising=False)

    flags = tools_api._resolve_skill_runtime_flags({"auto_install_deps": True})

    assert flags["auto_repair"] is False
    assert flags["auto_install_deps"] is True
    assert flags["rollback_on_fail"] is False
    assert flags["mutating_runtime_requested"] is True


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


def test_external_chat_current_main_model_shortcut(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime
    monkeypatch.setenv("MAGI_EXTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("MAGI_MAIN_MODEL", "gemma-4-e4b-it-4bit")
    monkeypatch.setenv("CASPER_LOCAL_MODEL", "gemma-4-e4b-it-4bit")
    tools_api._EXTERNAL_KEY_CACHE["ts"] = 0.0
    tools_api._EXTERNAL_KEY_CACHE["value"] = ""

    class _FakeOrch:
        def _is_verified_admin_sender(self, user_id, platform):
            return False

        def process_message(self, *args, **kwargs):
            raise AssertionError("model status shortcut should not hit general chat")

    monkeypatch.setattr(tools_api, "_get_osc_orchestrator", lambda: _FakeOrch())

    response = client.post(
        "/osc/external/chat",
        json={
            "user_id": "external_api_user",
            "platform": "WEB",
            "message": "目前主模型是哪一個？",
            "timeout_sec": 20,
            "async": False,
        },
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert "目標主模型" in payload["reply"]
    assert "gemma-4-e4b-it-4bit" in payload["reply"]


def test_collab_chat_semantic_preflight_uses_orchestrator_before_gateway(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime

    class _FakeOrch:
        def __init__(self):
            self.calls = []

        def process_message(self, user_id, message, platform="COLLAB", role="user"):
            self.calls.append((user_id, message, platform, role))
            return "ORCH_SEMANTIC_REPLY"

    fake_orch = _FakeOrch()
    monkeypatch.setattr(tools_api, "_get_osc_orchestrator", lambda: fake_orch)

    response = client.post("/collab/chat", json={"prompt": "你可以查天氣嗎？"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["response"] == "ORCH_SEMANTIC_REPLY"
    assert payload["route"] == "orchestrator_semantic_preflight"
    assert payload["intent_kind"] == "tool_capability"
    assert fake_orch.calls == [("collab-chat", "你可以查天氣嗎？", "COLLAB", "user")]


def test_collab_chat_agentic_prompt_uses_orchestrator(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime

    class _FakeOrch:
        def __init__(self):
            self.calls = []

        def process_message(self, user_id, message, platform="COLLAB", role="user"):
            self.calls.append((user_id, message, platform, role))
            return "ORCH_AGENTIC_REPLY"

    fake_orch = _FakeOrch()
    monkeypatch.setattr(tools_api, "_get_osc_orchestrator", lambda: fake_orch)

    response = client.post("/collab/chat", json={"prompt": "請幫我比較民法184條與相關判決見解"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["response"] == "ORCH_AGENTIC_REPLY"
    assert payload["route"] == "orchestrator_semantic_preflight"
    assert payload["intent_kind"] == "agent_task"
    assert fake_orch.calls == [("collab-chat", "請幫我比較民法184條與相關判決見解", "COLLAB", "user")]


def test_collab_chat_explicit_task_uses_orchestrator(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime

    class _FakeOrch:
        def process_message(self, user_id, message, platform="COLLAB", role="user"):
            return f"ORCH_EXPLICIT:{message}"

    monkeypatch.setattr(tools_api, "_get_osc_orchestrator", lambda: _FakeOrch())

    response = client.post("/collab/chat", json={"prompt": "查案件 2025-0134"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["route"] == "orchestrator_semantic_preflight"
    assert payload["intent_kind"] == "explicit_task"
    assert payload["response"] == "ORCH_EXPLICIT:查案件 2025-0134"


def test_external_ui_enforces_api_key(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime
    monkeypatch.setenv("MAGI_EXTERNAL_API_KEY", "secret-key")

    response = client.get("/osc/external/ui")
    assert response.status_code == 401
    payload = response.get_json()
    assert payload["success"] is False
    assert payload["error"] == "unauthorized: invalid api key"


def test_external_ui_loads_with_valid_api_key(monkeypatch, tools_api_runtime):
    tools_api, client, _events_path = tools_api_runtime
    monkeypatch.setenv("MAGI_EXTERNAL_API_KEY", "secret-key")
    tools_api._EXTERNAL_KEY_CACHE["ts"] = 0.0
    tools_api._EXTERNAL_KEY_CACHE["value"] = ""

    response = client.get("/osc/external/ui", headers={"X-API-Key": "secret-key"})
    assert response.status_code == 200
    assert "CASPER OSC 外部對話介面" in response.get_data(as_text=True)


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
