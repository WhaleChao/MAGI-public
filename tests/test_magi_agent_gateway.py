from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import sys

import pytest

from api.agentic.mcp_gateway import (
    AgentGatewayError,
    MagiAgentGatewayClient,
    MagiAgentGatewayConfig,
    MagiStdioMcpServer,
    mcp_tool_definitions,
)


def _config(**overrides):
    values = {
        "base_url": "http://127.0.0.1:5003",
        "api_key": "test-key",
        "user_id": "test-user",
        "platform": "GOOSE",
    }
    values.update(overrides)
    return MagiAgentGatewayConfig(**values)


class _Response:
    status = 200

    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_gateway_config_rejects_embedded_credentials_and_missing_key():
    with pytest.raises(ValueError, match="api_key is required"):
        MagiAgentGatewayConfig(base_url="http://127.0.0.1:5003", api_key="", user_id="u", platform="MCP")
    with pytest.raises(ValueError, match="embedded credentials"):
        MagiAgentGatewayConfig(
            base_url="https://user:password@example.test/mcp",
            api_key="key",
            user_id="u",
            platform="MCP",
        )


def test_published_tools_are_fixed_and_only_confirm_can_write():
    tools = mcp_tool_definitions()
    names = [item["name"] for item in tools]
    assert names == [
        "magi_health",
        "magi_capabilities",
        "magi_read",
        "magi_case_status",
        "magi_search",
        "magi_research",
        "magi_fetch",
        "magi_summarize",
        "magi_prepare_action",
        "magi_list_plans",
        "magi_get_plan",
        "magi_confirm_plan",
        "magi_cancel_plan",
    ]
    assert [item["name"] for item in tools if item.get("sideEffect") == "write_or_external"] == [
        "magi_confirm_plan"
    ]
    assert all("additionalProperties" in item["inputSchema"] for item in tools)
    assert all("shell" not in json.dumps(item).lower() for item in tools)


def test_client_uses_headers_not_query_secrets_and_validates_list_query():
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["key"] = request.get_header("X-api-key")
        seen["user"] = request.get_header("X-magi-agent-user-id")
        seen["platform"] = request.get_header("X-magi-agent-platform")
        seen["traceparent"] = request.get_header("Traceparent")
        return _Response({"success": True, "plans": []})

    client = MagiAgentGatewayClient(_config(), opener=opener, request_id_factory=lambda: "req-fixed")
    result = client.list_plans(limit=7)

    assert result["plans"] == []
    assert seen["url"] == "http://127.0.0.1:5003/agent/v1/plans?limit=7"
    assert "test-key" not in seen["url"]
    assert seen["key"] == "test-key"
    assert seen["user"] == "test-user"
    assert seen["platform"] == "GOOSE"
    assert seen["traceparent"].startswith("00-")
    assert len(seen["traceparent"]) == 55
    assert seen["timeout"] == 60


def test_client_maps_http_errors_without_leaking_credentials():
    def opener(request, timeout):
        from urllib.error import HTTPError

        raise HTTPError(request.full_url, 403, "forbidden", {}, StringIO('{"error":"denied"}'))

    client = MagiAgentGatewayClient(_config(), opener=opener)
    with pytest.raises(AgentGatewayError, match="denied") as caught:
        client.health()
    assert caught.value.status == 403
    assert "test-key" not in str(caught.value)


def test_stdio_server_implements_initialize_list_and_tool_call():
    def opener(request, timeout):
        return _Response({"success": True, "gateway": {"ok": True}})

    client = MagiAgentGatewayClient(_config(), opener=opener)
    server = MagiStdioMcpServer(client)
    input_text = "\n".join(
        [
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            '{"jsonrpc":"2.0","id":2,"method":"notifications/initialized"}',
            '{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}',
            '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"magi_health","arguments":{}}}',
        ]
    ) + "\n"

    output = StringIO()
    server.serve(stdin=StringIO(input_text), stdout=output)
    rows = [json.loads(line) for line in output.getvalue().splitlines()]

    assert [row["id"] for row in rows] == [1, 3, 4]
    assert rows[0]["result"]["serverInfo"]["name"] == "magi-agent-gateway"
    assert len(rows[1]["result"]["tools"]) == 13
    assert rows[2]["result"]["structuredContent"]["gateway"]["ok"] is True


@pytest.fixture
def tools_api_client(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MAGI_DISABLE_SERVER_STARTUP_HOOKS", "1")
    monkeypatch.setenv("MAGI_API_KEY", "test-key")
    monkeypatch.setenv("MAGI_EXTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("MAGI_CONTROLLED_AUTONOMY_DB", str(tmp_path / "plans.sqlite3"))
    sys.modules.pop("api.authz", None)
    sys.modules.pop("api.tools_api", None)
    import api.tools_api as tools_api

    tools_api._EXTERNAL_KEY_CACHE.update(ts=0.0, value="")

    class _PublicTestOrchestrator:
        def process_message(self, **_kwargs):
            # Public-source tests intentionally do not ship or import private
            # OSC connectors.  Leaving the plan unchanged models a rejected
            # confirmation without creating any external side effect.
            return "confirmation rejected"

    monkeypatch.setattr(
        tools_api,
        "_get_osc_orchestrator",
        lambda: _PublicTestOrchestrator(),
    )
    try:
        yield tools_api, tools_api.app.test_client()
    finally:
        tools_api._INFERENCE_EXECUTOR.shutdown(wait=True, cancel_futures=True)
        sys.modules.pop("api.tools_api", None)


def _agent_headers():
    return {
        "X-API-Key": "test-key",
        "X-MAGI-Agent-User-ID": "test-user",
        "X-MAGI-Agent-Platform": "GOOSE",
        "X-User-ID": "test-user",
        "X-Platform": "GOOSE",
        "X-Request-ID": "agent-test-1",
    }


def test_agent_gateway_requires_auth_and_identity(tools_api_client):
    _tools_api, client = tools_api_client
    assert client.get("/agent/v1/capabilities").status_code == 401
    assert client.get("/agent/v1/capabilities", headers={"X-API-Key": "test-key"}).status_code == 400

    response = client.get("/agent/v1/capabilities", headers=_agent_headers())
    assert response.status_code == 200
    assert response.headers["traceparent"].startswith("00-")
    payload = response.get_json()
    assert payload["success"] is True
    assert len(payload["tools"]) == 13
    assert payload["security"]["raw_shell_and_raw_database_access"] is False


def test_agent_read_rejects_mutation_before_orchestrator(tools_api_client, monkeypatch):
    tools_api, client = tools_api_client
    monkeypatch.setattr(
        tools_api,
        "_get_osc_orchestrator",
        lambda: (_ for _ in ()).throw(AssertionError("mutable read must not invoke orchestrator")),
    )
    response = client.post(
        "/agent/v1/read",
        json={"message": "請幫我建立一個行事曆事件，明天下午三點開會"},
        headers=_agent_headers(),
    )
    assert response.status_code == 409
    payload = response.get_json()
    assert payload["safe"] is False
    assert payload["requires_confirmation"] is True
    assert payload["next_tool"] == "magi_prepare_action"


def test_agent_prepare_list_get_and_cancel_never_return_token(tools_api_client):
    _tools_api, client = tools_api_client
    headers = _agent_headers()
    prepared = client.post(
        "/agent/v1/plans",
        json={"message": "請幫我建立一個行事曆事件，明天下午三點開會", "ttl_minutes": 15},
        headers=headers,
    )
    assert prepared.status_code == 201
    proposal = prepared.get_json()
    assert proposal["requires_confirmation"] is True
    assert len(proposal["confirmation_token"]) == 12
    plan_id = proposal["plan"]["plan_id"]

    listed = client.get("/agent/v1/plans?limit=5", headers=headers)
    assert listed.status_code == 200
    assert "confirmation_token" not in listed.get_data(as_text=True)

    fetched = client.get(f"/agent/v1/plans/{plan_id}", headers=headers)
    assert fetched.status_code == 200
    assert "confirmation_token" not in fetched.get_data(as_text=True)
    assert fetched.get_json()["plan"]["plan_id"] == plan_id

    wrong_confirmation = client.post(
        f"/agent/v1/plans/{plan_id}/confirm",
        json={"confirmation_token": "0" * 12},
        headers=headers,
    )
    assert wrong_confirmation.status_code == 403
    assert wrong_confirmation.get_json()["error"] == "confirmation_not_accepted"

    cancelled = client.post(f"/agent/v1/plans/{plan_id}/cancel", headers=headers, json={})
    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"
