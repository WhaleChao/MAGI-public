#!/usr/bin/env python3
"""Run MAGI's dependency-light stdio MCP bridge or optional HTTP MCP server."""

from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys

# Make ``python /absolute/path/to/MAGI/bin/agent_mcp.py`` work even when the
# MCP client launches it from a different working directory.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.agentic.mcp_gateway import (
    AgentGatewayError,
    MagiAgentGatewayClient,
    MagiAgentGatewayConfig,
    MagiStdioMcpServer,
    create_fastmcp_server,
)
from api.agentic.mcp_http import MagiMcpHttpApplication, OAuthResourceConfig


def _config_from_args(args: argparse.Namespace) -> MagiAgentGatewayConfig:
    config = MagiAgentGatewayConfig.from_env()
    changes = {}
    for field in ("base_url", "user_id", "platform", "timeout_sec"):
        value = getattr(args, field, None)
        if value is not None:
            changes[field] = value
    return replace(config, **changes) if changes else config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI identity-bound MCP Agent Gateway")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default=os.environ.get("MAGI_AGENT_MCP_TRANSPORT", "stdio"),
        help="stdio needs no optional dependency; HTTP/SSE needs mcp[cli].",
    )
    parser.add_argument("--base-url", dest="base_url")
    parser.add_argument("--user-id", dest="user_id")
    parser.add_argument("--platform")
    parser.add_argument("--timeout-sec", dest="timeout_sec", type=int)
    parser.add_argument("--host", default=os.environ.get("MAGI_AGENT_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MAGI_AGENT_MCP_PORT", "8765")))
    args = parser.parse_args(argv)

    try:
        config = _config_from_args(args)
        client = MagiAgentGatewayClient(config)
        if args.transport == "stdio":
            MagiStdioMcpServer(client).serve()
        elif args.transport == "streamable-http":
            if args.host not in {"127.0.0.1", "::1", "localhost"}:
                raise AgentGatewayError(
                    "MCP HTTP must bind to loopback; expose it only through the approved TLS/OAuth reverse proxy"
                )
            try:
                import uvicorn
            except ImportError as exc:
                raise AgentGatewayError("uvicorn is required for stateless MCP HTTP") from exc
            app = MagiMcpHttpApplication(
                MagiStdioMcpServer(client),
                oauth=OAuthResourceConfig.from_env(),
            )
            uvicorn.run(app, host=args.host, port=max(1, min(65535, int(args.port))), log_level="info")
        else:
            if args.host not in {"127.0.0.1", "::1", "localhost"}:
                raise AgentGatewayError("legacy MCP SSE transport is restricted to loopback")
            server = create_fastmcp_server(client)
            server.run(transport=args.transport)
    except (AgentGatewayError, ValueError) as exc:
        print(f"magi-agent-mcp: {exc}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
