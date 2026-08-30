"""Stateless MCP HTTP transport with local OAuth resource-server validation.

The application serves modern 2026-07-28 and legacy JSON-RPC requests over
POST without protocol sessions.  Public exposure is supported only through a
TLS reverse proxy and requires locally verifiable OAuth access tokens.  Token
verification never downloads keys at request time.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from api.agentic.mcp_gateway import MagiStdioMcpServer
from magi_v3.mcp_conformance import (
    LEGACY_PROTOCOL_VERSIONS,
    MODERN_PROTOCOL_VERSION,
    McpProtocolError,
    request_context,
)


MAX_BODY_BYTES = 512 * 1024
READ_SCOPE = "magi:read"
PLAN_SCOPE = "magi:plan"
CONFIRM_SCOPE = "magi:confirm"


class McpHttpSecurityError(ValueError):
    pass


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _https_url(value: str, field: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise McpHttpSecurityError(f"{field} must be a credential-free HTTPS URL")
    return str(value).rstrip("/")


def protected_resource_metadata_url(resource_url: str) -> str:
    """Return the RFC 9728 well-known URL for one protected resource.

    A resource path is appended *after* the well-known component.  For
    example, ``https://magi.example/mcp`` maps to
    ``https://magi.example/.well-known/oauth-protected-resource/mcp``.
    """

    parsed = urlsplit(_https_url(resource_url, "resource_url"))
    resource_path = parsed.path.rstrip("/")
    metadata_path = "/.well-known/oauth-protected-resource"
    if resource_path and resource_path != "/":
        metadata_path += resource_path if resource_path.startswith("/") else f"/{resource_path}"
    return urlunsplit((parsed.scheme, parsed.netloc, metadata_path, "", ""))


@dataclass(frozen=True, slots=True)
class OAuthResourceConfig:
    required: bool = False
    resource_url: str = ""
    issuer_url: str = ""
    audience: str = ""
    public_key_file: Path | None = None
    jwks_file: Path | None = None
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    scopes: tuple[str, ...] = (READ_SCOPE, PLAN_SCOPE, CONFIRM_SCOPE)

    def __post_init__(self) -> None:
        if not self.required:
            return
        object.__setattr__(self, "resource_url", _https_url(self.resource_url, "resource_url"))
        object.__setattr__(self, "issuer_url", _https_url(self.issuer_url, "issuer_url"))
        if not str(self.audience or "").strip():
            raise McpHttpSecurityError("OAuth audience is required")
        if bool(self.public_key_file) == bool(self.jwks_file):
            raise McpHttpSecurityError("configure exactly one local OAuth public-key or JWKS file")
        key_path = self.public_key_file or self.jwks_file
        if key_path is None or not Path(key_path).expanduser().is_file():
            raise McpHttpSecurityError("OAuth verification key file is missing")
        allowed = tuple(item for item in self.algorithms if item in {"RS256", "RS384", "RS512", "ES256", "ES384"})
        if not allowed or len(allowed) != len(self.algorithms):
            raise McpHttpSecurityError("OAuth JWT algorithms are not allowlisted")

    @classmethod
    def from_env(cls) -> "OAuthResourceConfig":
        required = _truthy(os.environ.get("MAGI_MCP_OAUTH_REQUIRED"))
        public_key = str(os.environ.get("MAGI_MCP_OAUTH_PUBLIC_KEY_FILE") or "").strip()
        jwks = str(os.environ.get("MAGI_MCP_OAUTH_JWKS_FILE") or "").strip()
        algorithms = tuple(
            item.strip().upper()
            for item in str(os.environ.get("MAGI_MCP_OAUTH_ALGORITHMS") or "RS256,ES256").split(",")
            if item.strip()
        )
        scopes = tuple(
            item.strip()
            for item in str(
                os.environ.get("MAGI_MCP_OAUTH_SCOPES")
                or f"{READ_SCOPE},{PLAN_SCOPE},{CONFIRM_SCOPE}"
            ).split(",")
            if item.strip()
        )
        return cls(
            required=required,
            resource_url=str(os.environ.get("MAGI_MCP_RESOURCE_URL") or ""),
            issuer_url=str(os.environ.get("MAGI_MCP_OAUTH_ISSUER") or ""),
            audience=str(os.environ.get("MAGI_MCP_OAUTH_AUDIENCE") or ""),
            public_key_file=Path(public_key).expanduser() if public_key else None,
            jwks_file=Path(jwks).expanduser() if jwks else None,
            algorithms=algorithms,
            scopes=scopes,
        )

    def protected_resource_metadata(self) -> dict[str, Any]:
        if not self.required:
            raise McpHttpSecurityError("OAuth protected-resource metadata is disabled")
        return {
            "resource": self.resource_url,
            "authorization_servers": [self.issuer_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(self.scopes),
        }


@dataclass(frozen=True, slots=True)
class OAuthAccess:
    subject: str
    client_id: str
    scopes: frozenset[str]


class LocalJwtVerifier:
    """Verify issuer/audience/scope using a release-provided key snapshot."""

    def __init__(self, config: OAuthResourceConfig) -> None:
        if not config.required:
            raise McpHttpSecurityError("OAuth verifier requires an enabled resource config")
        self.config = config

    def _key(self, token: str) -> Any:
        import jwt

        if self.config.public_key_file:
            return self.config.public_key_file.read_text(encoding="utf-8")
        header = jwt.get_unverified_header(token)
        kid = str(header.get("kid") or "").strip()
        if not kid:
            raise McpHttpSecurityError("OAuth JWT kid is required for JWKS verification")
        payload = json.loads(self.config.jwks_file.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        keys = payload.get("keys") if isinstance(payload, Mapping) else None
        if not isinstance(keys, list):
            raise McpHttpSecurityError("OAuth JWKS file is invalid")
        candidates = [item for item in keys if isinstance(item, Mapping) and str(item.get("kid") or "") == kid]
        if len(candidates) != 1:
            raise McpHttpSecurityError("OAuth JWT kid is not uniquely pinned")
        return jwt.PyJWK.from_dict(dict(candidates[0])).key

    def verify(self, token: str, *, required_scope: str) -> OAuthAccess:
        import jwt

        raw = str(token or "").strip()
        if not raw or len(raw) > 16_384:
            raise McpHttpSecurityError("OAuth bearer token is missing or invalid")
        try:
            claims = jwt.decode(
                raw,
                key=self._key(raw),
                algorithms=list(self.config.algorithms),
                audience=self.config.audience,
                issuer=self.config.issuer_url,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except Exception as exc:
            raise McpHttpSecurityError("OAuth bearer token validation failed") from exc
        raw_scope = claims.get("scope") or claims.get("scp") or ""
        if isinstance(raw_scope, str):
            scopes = frozenset(raw_scope.split())
        elif isinstance(raw_scope, list):
            scopes = frozenset(str(item) for item in raw_scope)
        else:
            scopes = frozenset()
        if required_scope not in scopes:
            raise McpHttpSecurityError(f"OAuth scope is required: {required_scope}")
        return OAuthAccess(
            subject=str(claims.get("sub") or ""),
            client_id=str(claims.get("client_id") or claims.get("azp") or ""),
            scopes=scopes,
        )


def required_scope(message: Mapping[str, Any]) -> str:
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
    tool = str(params.get("name") or "")
    if method == "tools/call" and tool == "magi_confirm_plan":
        return CONFIRM_SCOPE
    if method == "tools/call" and tool in {"magi_prepare_action", "magi_cancel_plan"}:
        return PLAN_SCOPE
    return READ_SCOPE


class MagiMcpHttpApplication:
    """Small ASGI application for stateless MCP POST requests."""

    def __init__(
        self,
        server: MagiStdioMcpServer,
        *,
        oauth: OAuthResourceConfig | None = None,
        verifier: LocalJwtVerifier | Any | None = None,
    ) -> None:
        self.server = server
        self.oauth = oauth or OAuthResourceConfig()
        self.verifier = verifier or (LocalJwtVerifier(self.oauth) if self.oauth.required else None)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return
        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "/")
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        metadata_paths = {"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"}
        if path in metadata_paths:
            if not self.oauth.required:
                await self._json(send, 404, {"error": "oauth_metadata_disabled"})
            else:
                await self._json(send, 200, self.oauth.protected_resource_metadata())
            return
        if path != "/mcp":
            await self._json(send, 404, {"error": "not_found"})
            return
        if method != "POST":
            await self._json(send, 405, {"error": "stateless_mcp_accepts_post_only"}, [(b"allow", b"POST")])
            return
        if "mcp-session-id" in headers:
            await self._json(send, 400, {"error": "protocol_sessions_are_not_supported"})
            return
        body = bytearray()
        while True:
            event = await receive()
            if event.get("type") != "http.request":
                continue
            body.extend(event.get("body") or b"")
            if len(body) > MAX_BODY_BYTES:
                await self._json(send, 413, {"error": "request_body_too_large"})
                return
            if not event.get("more_body"):
                break
        try:
            message = json.loads(bytes(body).decode("utf-8"))
            if not isinstance(message, Mapping):
                raise ValueError("JSON-RPC body must be an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            await self._json(
                send,
                400,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}},
            )
            return

        header_version = str(headers.get("mcp-protocol-version") or "").strip()
        try:
            context = request_context(message)
            if context.modern and header_version != MODERN_PROTOCOL_VERSION:
                raise McpProtocolError(-32602, "MCP-Protocol-Version header is required for modern HTTP")
            if context.modern and header_version != context.protocol_version:
                raise McpProtocolError(-32022, "HTTP and JSON-RPC MCP protocol versions disagree")
            if not context.modern and header_version and header_version not in LEGACY_PROTOCOL_VERSIONS:
                raise McpProtocolError(
                    -32022,
                    "unsupported legacy MCP HTTP protocol version",
                    data={"requested": header_version, "supported": list(LEGACY_PROTOCOL_VERSIONS)},
                )
            if headers.get("mcp-method") and headers["mcp-method"] != str(message.get("method") or ""):
                raise McpProtocolError(-32602, "Mcp-Method header does not match the JSON-RPC method")
            params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
            if headers.get("mcp-name") and headers["mcp-name"] != str(params.get("name") or ""):
                raise McpProtocolError(-32602, "Mcp-Name header does not match the JSON-RPC name")
        except McpProtocolError as exc:
            await self._json(
                send,
                exc.http_status,
                {"jsonrpc": "2.0", "id": message.get("id"), "error": exc.as_error()},
            )
            return

        if self.oauth.required:
            authorization = str(headers.get("authorization") or "")
            if not authorization.startswith("Bearer "):
                await self._oauth_error(send, "invalid_token")
                return
            try:
                self.verifier.verify(authorization[7:], required_scope=required_scope(message))
            except McpHttpSecurityError as exc:
                await self._oauth_error(send, "insufficient_scope" if "scope" in str(exc) else "invalid_token")
                return

        response = self.server.handle(message)
        if response is None:
            await self._empty(send, 202)
            return
        status = 200
        if isinstance(response.get("error"), Mapping):
            status = 400 if int(response["error"].get("code") or 0) in {-32600, -32601, -32602, -32021, -32022} else 500
        traceparent = ""
        result = response.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("_meta"), Mapping):
            traceparent = str(result["_meta"].get("traceparent") or "")
        extra = [(b"mcp-protocol-version", context.protocol_version.encode("ascii"))] if context.modern else []
        if traceparent:
            extra.append((b"traceparent", traceparent.encode("ascii")))
        await self._json(send, status, response, extra)

    async def _oauth_error(self, send: Any, error: str) -> None:
        metadata = protected_resource_metadata_url(self.oauth.resource_url)
        challenge = f'Bearer error="{error}", resource_metadata="{metadata}"'
        await self._json(send, 401, {"error": error}, [(b"www-authenticate", challenge.encode("latin-1"))])

    @staticmethod
    async def _empty(send: Any, status: int) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _json(
        send: Any,
        status: int,
        payload: Mapping[str, Any],
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode("ascii"))]
        headers.extend(extra_headers or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "CONFIRM_SCOPE",
    "LocalJwtVerifier",
    "MagiMcpHttpApplication",
    "McpHttpSecurityError",
    "OAuthAccess",
    "OAuthResourceConfig",
    "PLAN_SCOPE",
    "READ_SCOPE",
    "protected_resource_metadata_url",
    "required_scope",
]
