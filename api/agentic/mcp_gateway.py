"""Safe MCP-facing client and stdio adapter for MAGI's Agent Gateway.

The gateway deliberately exposes a small, typed surface instead of forwarding
MAGI's entire legacy Tools API.  A client process is bound to one identity via
environment variables and talks to ``/agent/v1`` with an API key.  Read-only
requests and mutable plans therefore share MAGI's existing authorization,
controlled-autonomy store, receipts, and domain handlers.

The optional ``mcp`` dependency is only needed for the Streamable HTTP/SSE
server.  The stdio adapter is dependency-free so Goose, Cline, and other MCP
clients can use a minimal local bridge in a fresh MAGI environment.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from magi_v3.mcp_conformance import (
    MODERN_PROTOCOL_VERSION,
    McpProtocolError,
    discover_result,
    legacy_initialize_version,
    request_context,
    response_meta,
)
from magi_v3.telemetry import Tracer


GATEWAY_SCHEMA = "magi.agent-gateway/v1"
SERVER_NAME = "magi-agent-gateway"
SERVER_VERSION = "2.0.0"
DEFAULT_BASE_URL = "http://127.0.0.1:5003"
_PLAN_ID_RE = re.compile(r"\Aca-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\Z")
_TOKEN_RE = re.compile(r"\A[a-f0-9]{12}\Z")
_SAFE_ID_RE = re.compile(r"\A[^\x00-\x1f\x7f]{1,128}\Z")


class AgentGatewayError(RuntimeError):
    """A transport or MAGI contract error safe to expose to an MCP client."""

    def __init__(self, message: str, *, status: int = 0, payload: Any = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.payload = payload


def _text(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{field} must not be empty")
    if len(result) > max_length:
        raise ValueError(f"{field} exceeds the {max_length}-character limit")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in result):
        raise ValueError(f"{field} contains a control character")
    return result


def _optional_text(value: Any, field: str, *, max_length: int) -> str:
    if value in (None, ""):
        return ""
    return _text(value, field, max_length=max_length)


def _bounded_int(value: Any, field: str, *, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    return max(minimum, min(maximum, result))


def _bounded_bool(value: Any, field: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    raise ValueError(f"{field} must be a boolean")


def _validate_id(value: Any, field: str) -> str:
    result = _text(value, field, max_length=128)
    if not _SAFE_ID_RE.fullmatch(result):
        raise ValueError(f"{field} contains an unsafe character")
    return result


def _validate_plan_id(value: Any) -> str:
    result = _text(value, "plan_id", max_length=64).lower()
    if not _PLAN_ID_RE.fullmatch(result):
        raise ValueError("plan_id has an invalid format")
    return result


def _validate_token(value: Any) -> str:
    result = _text(value, "confirmation_token", max_length=32).lower()
    if not _TOKEN_RE.fullmatch(result):
        raise ValueError("confirmation_token has an invalid format")
    return result


@dataclass(frozen=True, slots=True)
class MagiAgentGatewayConfig:
    """Configuration for one identity-bound gateway client."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    user_id: str = "local-agent"
    platform: str = "MCP"
    timeout_sec: int = 60

    def __post_init__(self) -> None:
        parsed = urlsplit(str(self.base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be an HTTP(S) URL without embedded credentials")
        canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        object.__setattr__(self, "base_url", canonical)
        if not str(self.api_key or "").strip():
            raise ValueError("api_key is required")
        object.__setattr__(self, "api_key", str(self.api_key).strip())
        object.__setattr__(self, "user_id", _validate_id(self.user_id, "user_id"))
        object.__setattr__(self, "platform", _validate_id(self.platform, "platform").upper())
        object.__setattr__(self, "timeout_sec", max(5, min(300, int(self.timeout_sec))))

    @classmethod
    def from_env(cls) -> "MagiAgentGatewayConfig":
        api_key = (
            os.environ.get("MAGI_AGENT_GATEWAY_API_KEY")
            or os.environ.get("MAGI_EXTERNAL_API_KEY")
            or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
            or ""
        ).strip()
        return cls(
            base_url=os.environ.get("MAGI_AGENT_GATEWAY_URL", DEFAULT_BASE_URL),
            api_key=api_key,
            user_id=os.environ.get("MAGI_AGENT_USER_ID", "local-agent"),
            platform=os.environ.get("MAGI_AGENT_PLATFORM", "MCP"),
            timeout_sec=_bounded_int(
                os.environ.get("MAGI_AGENT_GATEWAY_TIMEOUT_SEC"),
                "MAGI_AGENT_GATEWAY_TIMEOUT_SEC",
                default=60,
                minimum=5,
                maximum=300,
            ),
        )


class MagiAgentGatewayClient:
    """Typed HTTP client for the safe MAGI Agent Gateway surface."""

    def __init__(
        self,
        config: MagiAgentGatewayConfig,
        *,
        opener: Callable[..., Any] = urlopen,
        request_id_factory: Callable[[], str] | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.config = config
        self._opener = opener
        self._request_id_factory = request_id_factory or (lambda: f"magi-agent-{uuid.uuid4().hex[:16]}")
        self._tracer = tracer or Tracer("magi.agent_gateway.client")

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise AgentGatewayError("gateway path is invalid")
        query_string = urlencode({key: value for key, value in (query or {}).items() if value not in (None, "")})
        url = f"{self.config.base_url}{path}" + (f"?{query_string}" if query_string else "")
        route_template = re.sub(
            r"/ca-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}(?=/|$)",
            "/{plan_id}",
            path,
        )
        with self._tracer.start_span(
            "magi.agent.gateway.client",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "MAGI",
                "rpc.system": "mcp",
                "rpc.method": "agent_gateway",
                "http.request.method": method,
                "http.route": route_template,
            },
        ) as span:
            headers = {
                "Accept": "application/json",
                "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
                "X-API-Key": self.config.api_key,
                "X-MAGI-Agent-User-ID": self.config.user_id,
                "X-MAGI-Agent-Platform": self.config.platform,
                "X-User-ID": self.config.user_id,
                "X-Platform": self.config.platform,
                "X-Request-ID": self._request_id_factory(),
                "traceparent": span.context.traceparent,
            }
            data = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request = Request(url, data=data, method=method, headers=headers)
            try:
                with self._opener(request, timeout=self.config.timeout_sec) as response:
                    raw = response.read()
                    status = int(getattr(response, "status", getattr(response, "code", 200)))
            except HTTPError as exc:
                raw = exc.read()
                status = int(exc.code)
            except (URLError, TimeoutError, OSError) as exc:
                span.record_error(type(exc).__name__)
                raise AgentGatewayError(f"MAGI Agent Gateway is unreachable: {type(exc).__name__}") from exc
            span.set_attribute("http.response.status_code", status)
            if status >= 400:
                span.record_error("AgentGatewayHttpError")
            try:
                decoded = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                span.record_error("InvalidJsonResponse")
                raise AgentGatewayError(f"gateway returned invalid JSON (HTTP {status})", status=status) from exc
            if not isinstance(decoded, dict):
                span.record_error("InvalidResponseEnvelope")
                raise AgentGatewayError("gateway response must be a JSON object", status=status, payload=decoded)
            if status >= 400:
                message = str(decoded.get("error") or decoded.get("message") or f"HTTP {status}")
                raise AgentGatewayError(message, status=status, payload=decoded)
            span.set_attribute("magi.outcome", "passed")
            return decoded

    def health(self) -> dict[str, Any]:
        return self._request("/agent/v1/health")

    def capabilities(self) -> dict[str, Any]:
        return self._request("/agent/v1/capabilities")

    def read(self, message: str, *, has_attachment: bool = False, timeout_sec: int = 60) -> dict[str, Any]:
        return self._request(
            "/agent/v1/read",
            method="POST",
            payload={
                "message": _text(message, "message", max_length=4_000),
                "has_attachment": _bounded_bool(has_attachment, "has_attachment"),
                "timeout_sec": _bounded_int(timeout_sec, "timeout_sec", default=60, minimum=10, maximum=300),
            },
        )

    def case_status(
        self,
        *,
        query: str = "",
        case_number: str = "",
        row_id: str = "",
        max_cases: int = 6,
        max_files_per_case: int = 20,
        full_scan: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "/agent/v1/case-status",
            method="POST",
            payload={
                "query": _optional_text(query, "query", max_length=256),
                "case_number": _optional_text(case_number, "case_number", max_length=128),
                "row_id": _optional_text(row_id, "row_id", max_length=128),
                "max_cases": _bounded_int(max_cases, "max_cases", default=6, minimum=1, maximum=20),
                "max_files_per_case": _bounded_int(
                    max_files_per_case, "max_files_per_case", default=20, minimum=1, maximum=50
                ),
                "full_scan": _bounded_bool(full_scan, "full_scan"),
            },
        )

    def search(self, query: str, *, num_results: int = 5) -> dict[str, Any]:
        return self._request(
            "/agent/v1/search",
            method="POST",
            payload={
                "query": _text(query, "query", max_length=1_000),
                "num_results": _bounded_int(num_results, "num_results", default=5, minimum=1, maximum=10),
            },
        )

    def research(self, topic: str, *, depth: int = 3) -> dict[str, Any]:
        return self._request(
            "/agent/v1/research",
            method="POST",
            payload={
                "topic": _text(topic, "topic", max_length=2_000),
                "depth": _bounded_int(depth, "depth", default=3, minimum=1, maximum=5),
            },
        )

    def fetch(self, url: str) -> dict[str, Any]:
        return self._request(
            "/agent/v1/fetch",
            method="POST",
            payload={"url": _text(url, "url", max_length=2_048)},
        )

    def summarize(self, text: str) -> dict[str, Any]:
        return self._request(
            "/agent/v1/summarize",
            method="POST",
            payload={"text": _text(text, "text", max_length=50_000)},
        )

    def prepare_action(self, message: str, *, has_attachment: bool = False, ttl_minutes: int = 30) -> dict[str, Any]:
        return self._request(
            "/agent/v1/plans",
            method="POST",
            payload={
                "message": _text(message, "message", max_length=4_000),
                "has_attachment": _bounded_bool(has_attachment, "has_attachment"),
                "ttl_minutes": _bounded_int(ttl_minutes, "ttl_minutes", default=30, minimum=5, maximum=120),
            },
        )

    def list_plans(self, *, limit: int = 5) -> dict[str, Any]:
        return self._request(
            "/agent/v1/plans",
            method="GET",
            payload=None,
            query={"limit": _bounded_int(limit, "limit", default=5, minimum=1, maximum=20)},
        )

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self._request(f"/agent/v1/plans/{_validate_plan_id(plan_id)}")

    def confirm_plan(self, plan_id: str, confirmation_token: str) -> dict[str, Any]:
        return self._request(
            f"/agent/v1/plans/{_validate_plan_id(plan_id)}/confirm",
            method="POST",
            payload={"confirmation_token": _validate_token(confirmation_token)},
        )

    def cancel_plan(self, plan_id: str) -> dict[str, Any]:
        return self._request(
            f"/agent/v1/plans/{_validate_plan_id(plan_id)}/cancel",
            method="POST",
            payload={},
        )

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Dispatch exactly one published tool name to its typed method."""

        args = dict(arguments or {})
        handlers: dict[str, Callable[[], dict[str, Any]]] = {
            "magi_health": lambda: self.health(),
            "magi_capabilities": lambda: self.capabilities(),
            "magi_read": lambda: self.read(
                args.get("message", ""),
                has_attachment=args.get("has_attachment", False),
                timeout_sec=args.get("timeout_sec", 60),
            ),
            "magi_case_status": lambda: self.case_status(
                query=args.get("query", ""),
                case_number=args.get("case_number", ""),
                row_id=args.get("row_id", ""),
                max_cases=args.get("max_cases", 6),
                max_files_per_case=args.get("max_files_per_case", 20),
                full_scan=args.get("full_scan", False),
            ),
            "magi_search": lambda: self.search(args.get("query", ""), num_results=args.get("num_results", 5)),
            "magi_research": lambda: self.research(args.get("topic", ""), depth=args.get("depth", 3)),
            "magi_fetch": lambda: self.fetch(args.get("url", "")),
            "magi_summarize": lambda: self.summarize(args.get("text", "")),
            "magi_prepare_action": lambda: self.prepare_action(
                args.get("message", ""),
                has_attachment=args.get("has_attachment", False),
                ttl_minutes=args.get("ttl_minutes", 30),
            ),
            "magi_list_plans": lambda: self.list_plans(limit=args.get("limit", 5)),
            "magi_get_plan": lambda: self.get_plan(args.get("plan_id", "")),
            "magi_confirm_plan": lambda: self.confirm_plan(
                args.get("plan_id", ""), args.get("confirmation_token", "")
            ),
            "magi_cancel_plan": lambda: self.cancel_plan(args.get("plan_id", "")),
        }
        try:
            handler = handlers[name]
        except KeyError as exc:
            raise AgentGatewayError(f"unknown MAGI Agent Gateway tool: {name}", status=404) from exc
        try:
            return handler()
        except AgentGatewayError:
            raise
        except (TypeError, ValueError) as exc:
            raise AgentGatewayError(str(exc), status=400) from exc


TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "magi_health",
        "description": "Read MAGI Agent Gateway and controlled-autonomy health. No side effects.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "sideEffect": "none",
    },
    {
        "name": "magi_capabilities",
        "description": "List the small, allowlisted MAGI capabilities exposed to external agents.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "sideEffect": "none",
    },
    {
        "name": "magi_read",
        "description": "Ask MAGI to handle a read-only request. Mutable requests are rejected and must use magi_prepare_action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "minLength": 1, "maxLength": 4000},
                "has_attachment": {"type": "boolean", "default": False},
                "timeout_sec": {"type": "integer", "minimum": 10, "maximum": 300, "default": 60},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        "sideEffect": "read",
    },
    {
        "name": "magi_case_status",
        "description": "Read a compact case, document, calendar, and folder snapshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 256},
                "case_number": {"type": "string", "maxLength": 128},
                "row_id": {"type": "string", "maxLength": 128},
                "max_cases": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
                "max_files_per_case": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "full_scan": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "sideEffect": "read",
    },
    {
        "name": "magi_search",
        "description": "Search the web through MAGI's guarded search route.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 1000}, "num_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "sideEffect": "external_read",
    },
    {
        "name": "magi_research",
        "description": "Run bounded web research through MAGI; results are evidence, not legal advice.",
        "inputSchema": {
            "type": "object",
            "properties": {"topic": {"type": "string", "minLength": 1, "maxLength": 2000}, "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3}},
            "required": ["topic"],
            "additionalProperties": False,
        },
        "sideEffect": "external_read",
    },
    {
        "name": "magi_fetch",
        "description": "Fetch a public URL through MAGI's SSRF-protected fetch route.",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 2048, "format": "uri"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        "sideEffect": "external_read",
    },
    {
        "name": "magi_summarize",
        "description": "Summarize supplied text through MAGI's bounded summarization route.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 50000}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "sideEffect": "read",
    },
    {
        "name": "magi_prepare_action",
        "description": "Create a durable, user-bound plan for a mutable action. This never executes the action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "minLength": 1, "maxLength": 4000},
                "has_attachment": {"type": "boolean", "default": False},
                "ttl_minutes": {"type": "integer", "minimum": 5, "maximum": 120, "default": 30},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        "sideEffect": "none",
    },
    {
        "name": "magi_list_plans",
        "description": "List the current identity's recent controlled-autonomy plans without revealing confirmation tokens.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}}, "additionalProperties": False},
        "sideEffect": "read",
    },
    {
        "name": "magi_get_plan",
        "description": "Read one identity-bound plan and its receipt; confirmation tokens are never returned here.",
        "inputSchema": {
            "type": "object",
            "properties": {"plan_id": {"type": "string", "pattern": "^ca-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$"}},
            "required": ["plan_id"],
            "additionalProperties": False,
        },
        "sideEffect": "read",
    },
    {
        "name": "magi_confirm_plan",
        "description": "Execute one previously prepared plan using its one-time confirmation token. This is the only mutable gateway tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "pattern": "^ca-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$"},
                "confirmation_token": {"type": "string", "pattern": "^[a-f0-9]{12}$"},
            },
            "required": ["plan_id", "confirmation_token"],
            "additionalProperties": False,
        },
        "sideEffect": "write_or_external",
        "requiresConfirmation": True,
    },
    {
        "name": "magi_cancel_plan",
        "description": "Cancel an identity-bound mutable plan before it is executed.",
        "inputSchema": {
            "type": "object",
            "properties": {"plan_id": {"type": "string", "pattern": "^ca-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$"}},
            "required": ["plan_id"],
            "additionalProperties": False,
        },
        "sideEffect": "none",
    },
)


def mcp_tool_definitions() -> list[dict[str, Any]]:
    """Return JSON-safe tool definitions for stdio and HTTP adapters."""

    return [json.loads(json.dumps(item, ensure_ascii=False)) for item in TOOL_DEFINITIONS]


def _mcp_text_result(payload: Mapping[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(dict(payload), ensure_ascii=False, indent=2)}],
        "structuredContent": dict(payload),
        "isError": bool(is_error),
    }


class MagiStdioMcpServer:
    """Dependency-free dual-era JSON-RPC server for MCP tool calls.

    Modern 2026-07-28 requests are independently validated and traced.  The
    old initialize path remains only as an explicit compatibility adapter.
    """

    def __init__(self, client: MagiAgentGatewayClient) -> None:
        self.client = client
        self._tracer = Tracer("magi.mcp.server")

    @staticmethod
    def _error(request_id: Any, error: McpProtocolError | Mapping[str, Any]) -> dict[str, Any]:
        payload = error.as_error() if isinstance(error, McpProtocolError) else dict(error)
        return {"jsonrpc": "2.0", "id": request_id, "error": payload}

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        try:
            context = request_context(message)
        except McpProtocolError as exc:
            return self._error(request_id, exc)

        # Valid notifications never produce JSON-RPC responses.  Removed
        # modern notifications are rejected above before reaching this line.
        if method.startswith("notifications/"):
            return None

        with self._tracer.start_span(
            "magi.mcp.request",
            parent=context.trace_parent,
            attributes={
                "gen_ai.operation.name": "execute_tool" if method == "tools/call" else "invoke_agent",
                "gen_ai.agent.name": "MAGI",
                "rpc.system": "mcp",
                "rpc.method": method,
                "magi.component": "mcp_gateway",
            },
        ) as span:
            if method == "server/discover":
                result = discover_result(
                    server_name=SERVER_NAME,
                    server_version=SERVER_VERSION,
                    traceparent=span.context.traceparent,
                )
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            if method == "ping":
                return {"jsonrpc": "2.0", "id": request_id, "result": {}}
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": legacy_initialize_version(message),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                }
            if method == "tools/list":
                result: dict[str, Any] = {"tools": mcp_tool_definitions()}
                if context.modern:
                    result.update(
                        {
                            "resultType": "complete",
                            "ttlMs": 60_000,
                            "cacheScope": "private",
                            "_meta": response_meta(
                                server_name=SERVER_NAME,
                                server_version=SERVER_VERSION,
                                traceparent=span.context.traceparent,
                            ),
                        }
                    )
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            if method == "tools/call":
                params = message.get("params") or {}
                if not isinstance(params, Mapping) or not str(params.get("name") or ""):
                    return self._error(
                        request_id,
                        {"code": -32602, "message": "tool name is required"},
                    )
                try:
                    payload = self.client.call_tool(str(params["name"]), params.get("arguments") or {})
                    result = _mcp_text_result(payload)
                    span.set_attribute("magi.outcome", "passed")
                except AgentGatewayError as exc:
                    payload = exc.payload if isinstance(exc.payload, Mapping) else {
                        "error": str(exc),
                        "status": exc.status,
                    }
                    result = _mcp_text_result(payload, is_error=True)
                    span.set_attribute("magi.outcome", "failed")
                    span.record_error("AgentGatewayError")
                if context.modern:
                    result["resultType"] = "complete"
                    result["_meta"] = response_meta(
                        server_name=SERVER_NAME,
                        server_version=SERVER_VERSION,
                        traceparent=span.context.traceparent,
                    )
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            return self._error(
                request_id,
                {"code": -32601, "message": f"method not found: {method}"},
            )

    def serve(self, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        input_stream = stdin or sys.stdin
        output_stream = stdout or sys.stdout
        for line in input_stream:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, Mapping):
                    raise ValueError("JSON-RPC message must be an object")
                response = self.handle(message)
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                output_stream.flush()


def create_fastmcp_server(client: MagiAgentGatewayClient | None = None) -> Any:
    """Create an official ``mcp`` FastMCP server when the optional package exists."""

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise AgentGatewayError("the optional 'mcp' package is required for HTTP MCP transport") from exc

    gateway = client or MagiAgentGatewayClient(MagiAgentGatewayConfig.from_env())
    server = FastMCP(SERVER_NAME)

    @server.tool(name="magi_health", description=TOOL_DEFINITIONS[0]["description"])
    def magi_health() -> dict[str, Any]:
        return gateway.health()

    @server.tool(name="magi_capabilities", description=TOOL_DEFINITIONS[1]["description"])
    def magi_capabilities() -> dict[str, Any]:
        return gateway.capabilities()

    @server.tool(name="magi_read", description=TOOL_DEFINITIONS[2]["description"])
    def magi_read(message: str, has_attachment: bool = False, timeout_sec: int = 60) -> dict[str, Any]:
        return gateway.read(message, has_attachment=has_attachment, timeout_sec=timeout_sec)

    @server.tool(name="magi_case_status", description=TOOL_DEFINITIONS[3]["description"])
    def magi_case_status(
        query: str = "",
        case_number: str = "",
        row_id: str = "",
        max_cases: int = 6,
        max_files_per_case: int = 20,
        full_scan: bool = False,
    ) -> dict[str, Any]:
        return gateway.case_status(
            query=query,
            case_number=case_number,
            row_id=row_id,
            max_cases=max_cases,
            max_files_per_case=max_files_per_case,
            full_scan=full_scan,
        )

    @server.tool(name="magi_search", description=TOOL_DEFINITIONS[4]["description"])
    def magi_search(query: str, num_results: int = 5) -> dict[str, Any]:
        return gateway.search(query, num_results=num_results)

    @server.tool(name="magi_research", description=TOOL_DEFINITIONS[5]["description"])
    def magi_research(topic: str, depth: int = 3) -> dict[str, Any]:
        return gateway.research(topic, depth=depth)

    @server.tool(name="magi_fetch", description=TOOL_DEFINITIONS[6]["description"])
    def magi_fetch(url: str) -> dict[str, Any]:
        return gateway.fetch(url)

    @server.tool(name="magi_summarize", description=TOOL_DEFINITIONS[7]["description"])
    def magi_summarize(text: str) -> dict[str, Any]:
        return gateway.summarize(text)

    @server.tool(name="magi_prepare_action", description=TOOL_DEFINITIONS[8]["description"])
    def magi_prepare_action(message: str, has_attachment: bool = False, ttl_minutes: int = 30) -> dict[str, Any]:
        return gateway.prepare_action(message, has_attachment=has_attachment, ttl_minutes=ttl_minutes)

    @server.tool(name="magi_list_plans", description=TOOL_DEFINITIONS[9]["description"])
    def magi_list_plans(limit: int = 5) -> dict[str, Any]:
        return gateway.list_plans(limit=limit)

    @server.tool(name="magi_get_plan", description=TOOL_DEFINITIONS[10]["description"])
    def magi_get_plan(plan_id: str) -> dict[str, Any]:
        return gateway.get_plan(plan_id)

    @server.tool(name="magi_confirm_plan", description=TOOL_DEFINITIONS[11]["description"])
    def magi_confirm_plan(plan_id: str, confirmation_token: str) -> dict[str, Any]:
        return gateway.confirm_plan(plan_id, confirmation_token)

    @server.tool(name="magi_cancel_plan", description=TOOL_DEFINITIONS[12]["description"])
    def magi_cancel_plan(plan_id: str) -> dict[str, Any]:
        return gateway.cancel_plan(plan_id)

    return server


__all__ = [
    "AgentGatewayError",
    "GATEWAY_SCHEMA",
    "MagiAgentGatewayClient",
    "MagiAgentGatewayConfig",
    "MagiStdioMcpServer",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOL_DEFINITIONS",
    "create_fastmcp_server",
    "mcp_tool_definitions",
]
