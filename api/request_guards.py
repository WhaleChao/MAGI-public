from __future__ import annotations

import os

from flask import abort, request


OPENCLAW_BLOCKED_PREFIXES = (
    "/openclaw",
    "/openclaw-gateway",
)


def _is_local_host(host: str) -> bool:
    text = (host or "").strip().split(",")[0].strip()
    if ":" in text:
        text = text.split(":", 1)[0]
    return text.lower() in {"localhost", "127.0.0.1", "::1"}


def _is_cloudflare_tunnel_request() -> bool:
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").lower()
    if host.endswith(".trycloudflare.com"):
        return True
    return bool(request.headers.get("Cf-Connecting-Ip") or request.headers.get("Cf-Ray"))


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def install_request_guards(app, logger) -> None:
    @app.before_request
    def _block_public_openclaw_routes():
        path = (request.path or "").strip().lower()
        if not path:
            return None
        blocked = any(path == prefix or path.startswith(prefix + "/") for prefix in OPENCLAW_BLOCKED_PREFIXES)
        if not blocked:
            return None

        host = request.headers.get("X-Forwarded-Host") or request.host or ""
        if _is_local_host(host):
            return None

        logger.warning("Blocked public request to OpenClaw route: host=%s path=%s", host, path)
        abort(404)

    @app.before_request
    def _limit_cloudflare_tunnel_surface():
        if not _is_cloudflare_tunnel_request():
            return None

        path = (request.path or "").strip().lower()
        if _env_truthy("MAGI_ALLOW_CLOUDFLARE_WEB_UI"):
            return None

        allowed_prefixes = ("/line/webhook", "/telegram/webhook", "/callback", "/health")
        allowed = any(path == prefix or path.startswith(prefix + "/") for prefix in allowed_prefixes)
        if allowed:
            return None

        logger.warning(
            "Blocked Cloudflare tunnel request outside allowed surface: host=%s path=%s",
            request.headers.get("X-Forwarded-Host") or request.host or "",
            path,
        )
        abort(403)
