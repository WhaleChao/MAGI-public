"""MCP 2026-07-28 request validation with an explicit legacy boundary.

The modern protocol is stateless: each request declares its protocol version
and client capabilities in ``params._meta``.  MAGI keeps the old initialize
adapter for existing local clients, but never silently treats a malformed
modern request as legacy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from magi_v3.telemetry import TraceContext


MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_LEGACY_PROTOCOL_VERSION = "2024-11-05"
PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
REMOVED_MODERN_METHODS = frozenset(
    {
        "initialize",
        "ping",
        "logging/setLevel",
        "notifications/initialized",
        "notifications/roots/list_changed",
    }
)


@dataclass(frozen=True, slots=True)
class McpRequestContext:
    era: str
    protocol_version: str
    client_info: dict[str, Any]
    client_capabilities: dict[str, Any]
    trace_parent: TraceContext | None

    @property
    def modern(self) -> bool:
        return self.era == "modern"


class McpProtocolError(ValueError):
    """JSON-RPC error with the HTTP status required by the MCP transport."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = int(code)
        self.data = dict(data or {})
        self.http_status = int(http_status)

    def as_error(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.data:
            payload["data"] = dict(self.data)
        return payload


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise McpProtocolError(-32602, f"{field} must be an object")
    return dict(value)


def _implementation(value: Any, field: str) -> dict[str, Any]:
    item = _mapping(value, field)
    name = str(item.get("name") or "").strip()
    version = str(item.get("version") or "").strip()
    if not name or len(name) > 128 or not version or len(version) > 64:
        raise McpProtocolError(-32602, f"{field} must contain bounded name and version strings")
    return {"name": name, "version": version}


def request_context(message: Mapping[str, Any]) -> McpRequestContext:
    """Classify and validate one MCP request.

    ``server/discover`` is always modern.  Other requests are modern when any
    modern per-request metadata is present.  A request with no modern markers
    remains on the legacy adapter so existing r59-era clients keep working.
    """

    if str(message.get("jsonrpc") or "") != "2.0":
        raise McpProtocolError(-32600, "jsonrpc must be 2.0")
    method = str(message.get("method") or "").strip()
    if not method:
        raise McpProtocolError(-32600, "method is required")
    raw_params = message.get("params")
    params = {} if raw_params is None else _mapping(raw_params, "params")
    raw_meta = params.get("_meta")
    meta = {} if raw_meta is None else _mapping(raw_meta, "params._meta")
    has_modern_marker = method == "server/discover" or any(
        key in meta
        for key in (PROTOCOL_VERSION_META, CLIENT_INFO_META, CLIENT_CAPABILITIES_META)
    )
    if not has_modern_marker:
        return McpRequestContext("legacy", "", {}, {}, None)

    protocol_version = str(meta.get(PROTOCOL_VERSION_META) or "").strip()
    if not protocol_version:
        raise McpProtocolError(-32602, f"params._meta.{PROTOCOL_VERSION_META} is required")
    if protocol_version != MODERN_PROTOCOL_VERSION:
        raise McpProtocolError(
            -32022,
            "unsupported MCP protocol version",
            data={"requested": protocol_version, "supported": [MODERN_PROTOCOL_VERSION]},
        )
    if CLIENT_CAPABILITIES_META not in meta:
        raise McpProtocolError(-32602, f"params._meta.{CLIENT_CAPABILITIES_META} is required")
    client_capabilities = _mapping(meta[CLIENT_CAPABILITIES_META], CLIENT_CAPABILITIES_META)
    client_info = {}
    if CLIENT_INFO_META in meta:
        client_info = _implementation(meta[CLIENT_INFO_META], CLIENT_INFO_META)
    trace_parent = None
    if "traceparent" in meta:
        trace_parent = TraceContext.parse(str(meta.get("traceparent") or ""))
        if trace_parent is None:
            raise McpProtocolError(-32602, "params._meta.traceparent is invalid")
    if method in REMOVED_MODERN_METHODS:
        raise McpProtocolError(-32601, f"method not available in {MODERN_PROTOCOL_VERSION}: {method}")
    return McpRequestContext(
        "modern",
        protocol_version,
        client_info,
        client_capabilities,
        trace_parent,
    )


def legacy_initialize_version(message: Mapping[str, Any]) -> str:
    params = message.get("params")
    requested = str(params.get("protocolVersion") or "") if isinstance(params, Mapping) else ""
    return requested if requested in LEGACY_PROTOCOL_VERSIONS else DEFAULT_LEGACY_PROTOCOL_VERSION


def response_meta(*, server_name: str, server_version: str, traceparent: str = "") -> dict[str, Any]:
    meta: dict[str, Any] = {
        SERVER_INFO_META: {"name": str(server_name), "version": str(server_version)},
    }
    if traceparent:
        parsed = TraceContext.parse(traceparent)
        if parsed is None:
            raise ValueError("traceparent is invalid")
        meta["traceparent"] = parsed.traceparent
    return meta


def discover_result(*, server_name: str, server_version: str, traceparent: str = "") -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": [MODERN_PROTOCOL_VERSION],
        "capabilities": {"tools": {}},
        "instructions": (
            "MAGI exposes a narrow identity-bound legal operations gateway. "
            "Mutable actions require a durable proposal and one-time human confirmation."
        ),
        "ttlMs": 300_000,
        "cacheScope": "public",
        "_meta": response_meta(
            server_name=server_name,
            server_version=server_version,
            traceparent=traceparent,
        ),
    }


__all__ = [
    "CLIENT_CAPABILITIES_META",
    "CLIENT_INFO_META",
    "DEFAULT_LEGACY_PROTOCOL_VERSION",
    "LEGACY_PROTOCOL_VERSIONS",
    "MODERN_PROTOCOL_VERSION",
    "McpProtocolError",
    "McpRequestContext",
    "PROTOCOL_VERSION_META",
    "SERVER_INFO_META",
    "discover_result",
    "legacy_initialize_version",
    "request_context",
    "response_meta",
]
