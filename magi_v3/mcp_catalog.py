"""Pinned allowlist for MCP client-side server activation.

MAGI never launches an MCP package merely because a model or user supplied a
package name.  Every executable server must be preinstalled in a release,
digest pinned, and explicitly enabled in this catalog.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping


CATALOG_SCHEMA = "magi.mcp-client-catalog/v1"
SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class McpCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovedMcpServer:
    server_id: str
    enabled: bool
    transport: str
    executable: Path
    executable_sha256: str
    source: str
    source_commit: str
    arguments: tuple[str, ...] = ()
    network_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not SERVER_ID_RE.fullmatch(self.server_id):
            raise McpCatalogError("MCP server ID is invalid")
        if self.transport not in {"stdio", "streamable-http"}:
            raise McpCatalogError("MCP transport is not approved")
        if not self.executable.is_absolute():
            raise McpCatalogError("MCP executable must be an absolute release path")
        if not SHA256_RE.fullmatch(self.executable_sha256):
            raise McpCatalogError("MCP executable SHA-256 is invalid")
        if not self.source.startswith("https://github.com/"):
            raise McpCatalogError("MCP source must be an approved GitHub repository")
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise McpCatalogError("MCP source commit must be pinned")
        for argument in self.arguments:
            if argument in {"-m", "pip", "uvx", "npx"} or "latest" in argument.lower():
                raise McpCatalogError("runtime package installation is prohibited")

    def verify_executable(self) -> None:
        if not self.executable.is_file():
            raise McpCatalogError(f"approved MCP executable is missing: {self.server_id}")
        digest = hashlib.sha256(self.executable.read_bytes()).hexdigest()
        if digest != self.executable_sha256:
            raise McpCatalogError(f"approved MCP executable digest changed: {self.server_id}")


class McpClientCatalog:
    def __init__(self, entries: Mapping[str, ApprovedMcpServer]) -> None:
        self.entries = dict(entries)

    @classmethod
    def load(cls, path: Path | str) -> "McpClientCatalog":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("schema") != CATALOG_SCHEMA:
            raise McpCatalogError("MCP client catalog schema is invalid")
        raw_servers = payload.get("servers")
        if not isinstance(raw_servers, list):
            raise McpCatalogError("MCP client catalog servers must be a list")
        entries: dict[str, ApprovedMcpServer] = {}
        for raw in raw_servers:
            if not isinstance(raw, Mapping):
                raise McpCatalogError("MCP catalog entry must be an object")
            entry = ApprovedMcpServer(
                server_id=str(raw.get("server_id") or ""),
                enabled=bool(raw.get("enabled", False)),
                transport=str(raw.get("transport") or ""),
                executable=Path(str(raw.get("executable") or "")),
                executable_sha256=str(raw.get("executable_sha256") or ""),
                source=str(raw.get("source") or ""),
                source_commit=str(raw.get("source_commit") or ""),
                arguments=tuple(str(item) for item in raw.get("arguments") or []),
                network_hosts=tuple(str(item).lower() for item in raw.get("network_hosts") or []),
            )
            if entry.server_id in entries:
                raise McpCatalogError(f"duplicate MCP server ID: {entry.server_id}")
            entries[entry.server_id] = entry
        return cls(entries)

    def resolve(self, server_id: str, *, verify: bool = True) -> ApprovedMcpServer:
        try:
            entry = self.entries[str(server_id)]
        except KeyError as exc:
            raise McpCatalogError("MCP server is not present in the approved catalog") from exc
        if not entry.enabled:
            raise McpCatalogError("MCP server is approved but disabled")
        if verify:
            entry.verify_executable()
        return entry


__all__ = [
    "ApprovedMcpServer",
    "CATALOG_SCHEMA",
    "McpCatalogError",
    "McpClientCatalog",
]
