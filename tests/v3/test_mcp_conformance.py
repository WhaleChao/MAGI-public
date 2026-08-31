from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from api.agentic.mcp_gateway import MagiStdioMcpServer
from api.agentic.mcp_http import (
    CONFIRM_SCOPE,
    MagiMcpHttpApplication,
    McpHttpSecurityError,
    OAuthResourceConfig,
    PLAN_SCOPE,
    READ_SCOPE,
    protected_resource_metadata_url,
    required_scope,
)
from magi_v3.mcp_catalog import McpCatalogError, McpClientCatalog
from magi_v3.mcp_conformance import (
    CLIENT_CAPABILITIES_META,
    CLIENT_INFO_META,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META,
)


class FakeGateway:
    def call_tool(self, name, arguments):
        return {"success": True, "tool": name, "arguments_seen": sorted(arguments)}


def modern_message(method: str, *, request_id=1, params=None, traceparent=""):
    payload = dict(params or {})
    meta = {
        PROTOCOL_VERSION_META: MODERN_PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META: {},
        CLIENT_INFO_META: {"name": "magi-test", "version": "1.0"},
    }
    if traceparent:
        meta["traceparent"] = traceparent
    payload["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": payload}


def test_modern_discover_is_stateless_and_advertises_only_modern_version(monkeypatch):
    monkeypatch.setenv("MAGI_OTEL_MODE", "disabled")
    server = MagiStdioMcpServer(FakeGateway())
    response = server.handle(modern_message("server/discover"))
    result = response["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == [MODERN_PROTOCOL_VERSION]
    assert result["capabilities"] == {"tools": {}}
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "magi-agent-gateway"
    assert result["_meta"]["traceparent"].startswith("00-")


def test_modern_request_requires_per_request_version_and_capabilities(monkeypatch):
    monkeypatch.setenv("MAGI_OTEL_MODE", "disabled")
    server = MagiStdioMcpServer(FakeGateway())
    missing = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}
    response = server.handle(missing)
    assert response["error"]["code"] == -32602

    wrong = modern_message("tools/list")
    wrong["params"]["_meta"][PROTOCOL_VERSION_META] = "2099-01-01"
    response = server.handle(wrong)
    assert response["error"]["code"] == -32022
    assert response["error"]["data"]["supported"] == [MODERN_PROTOCOL_VERSION]


def test_modern_trace_context_propagates_and_removed_handshake_fails(monkeypatch):
    monkeypatch.setenv("MAGI_OTEL_MODE", "disabled")
    server = MagiStdioMcpServer(FakeGateway())
    parent = "00-0af7651916cd43dd8448eb211c80319c-00f067aa0ba902b7-01"
    response = server.handle(modern_message("tools/list", traceparent=parent))
    child = response["result"]["_meta"]["traceparent"]
    assert child.split("-")[1] == parent.split("-")[1]
    assert child.split("-")[2] != parent.split("-")[2]

    response = server.handle(modern_message("initialize"))
    assert response["error"]["code"] == -32601


def test_legacy_initialize_remains_compatible(monkeypatch):
    monkeypatch.setenv("MAGI_OTEL_MODE", "disabled")
    server = MagiStdioMcpServer(FakeGateway())
    response = server.handle({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}})
    assert response["result"]["protocolVersion"] == "2024-11-05"
    response = server.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}})
    assert len(response["result"]["tools"]) == 13
    assert "resultType" not in response["result"]


async def call_asgi(app, message, *, method="POST", path="/mcp", headers=None):
    sent = []
    body = json.dumps(message).encode("utf-8") if message is not None else b""
    events = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return events.pop(0)

    async def send(event):
        sent.append(event)

    raw_headers = [(str(k).encode("latin-1"), str(v).encode("latin-1")) for k, v in (headers or {}).items()]
    await app(
        {"type": "http", "method": method, "path": path, "headers": raw_headers},
        receive,
        send,
    )
    status = sent[0]["status"]
    response_headers = {k.decode(): v.decode() for k, v in sent[0].get("headers") or []}
    response_body = sent[1].get("body") or b""
    return status, response_headers, json.loads(response_body) if response_body else None


def test_stateless_http_requires_modern_header_and_rejects_session(monkeypatch):
    monkeypatch.setenv("MAGI_OTEL_MODE", "disabled")
    app = MagiMcpHttpApplication(MagiStdioMcpServer(FakeGateway()))
    message = modern_message("tools/list")
    status, headers, payload = asyncio.run(
        call_asgi(app, message, headers={"mcp-protocol-version": MODERN_PROTOCOL_VERSION})
    )
    assert status == 200
    assert headers["mcp-protocol-version"] == MODERN_PROTOCOL_VERSION
    assert payload["result"]["resultType"] == "complete"

    status, _headers, payload = asyncio.run(call_asgi(app, message))
    assert status == 400
    assert payload["error"]["code"] == -32602

    status, _headers, payload = asyncio.run(
        call_asgi(
            app,
            message,
            headers={
                "mcp-protocol-version": MODERN_PROTOCOL_VERSION,
                "mcp-session-id": "forbidden",
            },
        )
    )
    assert status == 400
    assert payload["error"] == "protocol_sessions_are_not_supported"


def test_stateless_http_keeps_explicit_legacy_protocol_compatible(monkeypatch):
    monkeypatch.setenv("MAGI_OTEL_MODE", "disabled")
    app = MagiMcpHttpApplication(MagiStdioMcpServer(FakeGateway()))
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    }
    status, _headers, payload = asyncio.run(
        call_asgi(app, initialize, headers={"mcp-protocol-version": "2025-11-25"})
    )
    assert status == 200
    assert payload["result"]["protocolVersion"] == "2025-11-25"

    status, _headers, payload = asyncio.run(
        call_asgi(
            app,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={"mcp-protocol-version": "2025-11-25"},
        )
    )
    assert status == 200
    assert len(payload["result"]["tools"]) == 13


def test_scope_mapping_keeps_confirmation_separate():
    assert required_scope({"method": "tools/list"}) == READ_SCOPE
    assert required_scope({"method": "tools/call", "params": {"name": "magi_prepare_action"}}) == PLAN_SCOPE
    assert required_scope({"method": "tools/call", "params": {"name": "magi_confirm_plan"}}) == CONFIRM_SCOPE


def test_oauth_resource_config_is_fail_closed(tmp_path: Path):
    key = tmp_path / "public.pem"
    key.write_text("not-used-in-this-configuration-test", encoding="utf-8")
    with pytest.raises(McpHttpSecurityError, match="HTTPS"):
        OAuthResourceConfig(
            required=True,
            resource_url="http://example.test/mcp",
            issuer_url="https://issuer.example.test",
            audience="magi",
            public_key_file=key,
        )


def test_oauth_challenge_uses_rfc9728_path_aware_metadata_url(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MAGI_OTEL_MODE", "disabled")
    key = tmp_path / "public.pem"
    key.write_text("test-public-key", encoding="utf-8")
    oauth = OAuthResourceConfig(
        required=True,
        resource_url="https://magi.example.test/mcp",
        issuer_url="https://issuer.example.test",
        audience="magi",
        public_key_file=key,
    )
    assert protected_resource_metadata_url(oauth.resource_url) == (
        "https://magi.example.test/.well-known/oauth-protected-resource/mcp"
    )
    app = MagiMcpHttpApplication(MagiStdioMcpServer(FakeGateway()), oauth=oauth, verifier=object())
    status, headers, payload = asyncio.run(
        call_asgi(
            app,
            modern_message("tools/list"),
            headers={"mcp-protocol-version": MODERN_PROTOCOL_VERSION},
        )
    )
    assert status == 401
    assert payload == {"error": "invalid_token"}
    assert "resource_metadata=\"https://magi.example.test/.well-known/oauth-protected-resource/mcp\"" in headers[
        "www-authenticate"
    ]
    with pytest.raises(McpHttpSecurityError, match="exactly one"):
        OAuthResourceConfig(
            required=True,
            resource_url="https://magi.example.test/mcp",
            issuer_url="https://issuer.example.test",
            audience="magi",
        )


def test_client_catalog_defaults_to_deny_and_checks_digest(tmp_path: Path):
    catalog_path = Path("config/mcp/approved_servers.json")
    catalog = McpClientCatalog.load(catalog_path)
    with pytest.raises(McpCatalogError, match="not present"):
        catalog.resolve("unapproved")

    executable = tmp_path / "server"
    executable.write_text("fixed", encoding="utf-8")
    payload = {
        "schema": "magi.mcp-client-catalog/v1",
        "servers": [
            {
                "server_id": "fixed-server",
                "enabled": True,
                "transport": "stdio",
                "executable": str(executable),
                "executable_sha256": hashlib.sha256(b"fixed").hexdigest(),
                "source": "https://github.com/example/fixed-server",
                "source_commit": "a" * 40,
                "arguments": [],
            }
        ],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = McpClientCatalog.load(path)
    assert loaded.resolve("fixed-server").executable == executable
    executable.write_text("changed", encoding="utf-8")
    with pytest.raises(McpCatalogError, match="digest changed"):
        loaded.resolve("fixed-server")
