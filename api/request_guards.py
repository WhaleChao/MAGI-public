from __future__ import annotations

import os
import secrets
import time

from flask import abort, g, request, session
from flask_login import current_user

from api.saas_audit import append_audit_event


RETIRED_LEGACY_ENTRYPOINTS = (
    "/openclaw",
    "/openclaw-gateway",
)

CLOUDFLARE_WEBHOOK_PREFIXES = (
    "/line/webhook",
    "/telegram/webhook",
    "/callback",
)

# These routes may cross the tunnel only because their handlers return
# deliberately minimal probe payloads to unauthenticated callers.  Detailed
# aggregate/SaaS evidence remains session/API-key protected in admin_runtime.
CLOUDFLARE_MINIMAL_PROBE_PREFIXES = (
    "/health",
    "/livez",
    "/readyz",
    "/saas-readyz",
)

CLOUDFLARE_PUBLIC_PREFIXES = (
    *CLOUDFLARE_WEBHOOK_PREFIXES,
    *CLOUDFLARE_MINIMAL_PROBE_PREFIXES,
    "/login",
    "/register",
    "/favicon.ico",
    "/static",
    "/lottery",
    "/api/lottery",
    "/cookie-cutter",
    "/api/cookie-cutter",
    "/exam-tutor",
    "/api/exam-tutor",
    "/tools",
    "/video-studio",
    "/api/video-studio",
)

CLOUDFLARE_AUTHENTICATED_UI_PREFIXES = (
    "/",
    "/dashboard",
    "/golem",
    "/osc",
    "/status",
    "/magi-adjust",
    "/magi-settings",
    "/research",
    "/intel",
    "/mobile",
    "/app",
    "/mobile-admin",
    "/app-admin",
    "/wa",
    "/ops",
    "/api/osc",
    "/api/golem",
    "/api/nerv",
    "/api/ops",
    "/api/status",
    "/api/live-log",
    "/api/live-validation",
    "/api/system-test",
    "/api/self-repair",
    "/api/skills",
)


def _path_matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _is_cloudflare_tunnel_request() -> bool:
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").lower()
    if host.endswith(".trycloudflare.com"):
        return True
    return bool(request.headers.get("Cf-Connecting-Ip") or request.headers.get("Cf-Ray"))


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _formal_saas_mode() -> bool:
    raw = str(os.environ.get("MAGI_DEPLOYMENT_MODE") or "").strip().lower()
    return _env_truthy("MAGI_SAAS_MODE") or raw in {"saas", "formal_saas", "managed_saas", "multi_tenant_saas"}


def _expected_tenant_id() -> str:
    return str(os.environ.get("MAGI_TENANT_ID") or "").strip()


def install_request_guards(app, logger) -> None:
    @app.before_request
    def _block_retired_legacy_entrypoints():
        path = (request.path or "").strip().lower()
        if not path:
            return None
        blocked = any(path == prefix or path.startswith(prefix + "/") for prefix in RETIRED_LEGACY_ENTRYPOINTS)
        if not blocked:
            return None

        host = request.headers.get("X-Forwarded-Host") or request.host or ""
        logger.warning("Blocked retired legacy entrypoint: host=%s path=%s", host, path)
        abort(404)

    @app.before_request
    def _limit_cloudflare_tunnel_surface():
        if not _is_cloudflare_tunnel_request():
            return None

        path = (request.path or "").strip().lower()
        if _env_truthy("MAGI_ALLOW_CLOUDFLARE_WEB_UI"):
            return None

        if _path_matches_prefix(path, CLOUDFLARE_PUBLIC_PREFIXES) or request.endpoint == "callback":
            return None

        if _path_matches_prefix(path, CLOUDFLARE_AUTHENTICATED_UI_PREFIXES):
            return None

        logger.warning(
            "Blocked Cloudflare tunnel request outside allowed surface: host=%s path=%s",
            request.headers.get("X-Forwarded-Host") or request.host or "",
            path,
        )
        abort(403)

    @app.before_request
    def _enforce_formal_saas_session_tenant():
        if not _formal_saas_mode():
            return None
        expected = _expected_tenant_id()
        if not expected:
            logger.error("Formal SaaS mode is enabled without MAGI_TENANT_ID")
            abort(503)

        path = (request.path or "").strip().lower()
        if _path_matches_prefix(path, CLOUDFLARE_PUBLIC_PREFIXES + ("/logout",)):
            return None

        try:
            authenticated = bool(current_user and current_user.is_authenticated)
        except Exception:
            authenticated = False
        if not authenticated:
            return None

        user_tenant = str(getattr(current_user, "tenant_id", "") or "").strip()
        session_tenant = str(session.get("tenant_id") or user_tenant or "").strip()
        if user_tenant == expected and session_tenant == expected:
            return None

        logger.warning(
            "Blocked formal SaaS tenant mismatch: path=%s user_tenant=%s session_tenant=%s expected=%s",
            path,
            user_tenant or "<missing>",
            session_tenant or "<missing>",
            expected,
        )
        session.clear()
        abort(403)

    @app.before_request
    def _audit_protected_mutation_start():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        path = (request.path or "").strip().lower()
        # Public webhook/auth surfaces have their own authentication and rate
        # limiting.  Writing before those checks would let anonymous traffic
        # amplify the audit sink.  All authenticated business/API mutations
        # are covered here, including API-key callers.
        if _path_matches_prefix(path, CLOUDFLARE_PUBLIC_PREFIXES) or request.endpoint == "callback":
            return None
        audit_id = secrets.token_hex(12)
        g.magi_mutation_audit_id = audit_id
        g.magi_mutation_audit_started_at = time.monotonic()
        try:
            append_audit_event(
                "http.mutation.started",
                resource_type="http_endpoint",
                resource_id=audit_id,
                status="started",
                metadata={
                    "endpoint": str(request.endpoint or "unmatched"),
                    "method": str(request.method or ""),
                },
            )
            g.magi_mutation_audit_started = True
        except Exception as exc:
            g.magi_mutation_audit_started = False
            logger.error("Mutation audit unavailable before side effect: %s", exc)
            if _formal_saas_mode():
                abort(503, description="operation audit unavailable")
        return None

    @app.after_request
    def _audit_protected_mutation_completion(response):
        if not bool(getattr(g, "magi_mutation_audit_started", False)):
            return response
        audit_id = str(getattr(g, "magi_mutation_audit_id", "") or "")
        elapsed_ms = max(
            0,
            round(
                (time.monotonic() - float(getattr(g, "magi_mutation_audit_started_at", time.monotonic())))
                * 1000
            ),
        )
        try:
            append_audit_event(
                "http.mutation.completed",
                resource_type="http_endpoint",
                resource_id=audit_id,
                status="ok" if int(response.status_code) < 400 else "rejected_or_failed",
                metadata={
                    "endpoint": str(request.endpoint or "unmatched"),
                    "method": str(request.method or ""),
                    "status_code": int(response.status_code),
                    "elapsed_ms": elapsed_ms,
                },
            )
        except Exception as exc:
            # The start receipt is already durable and identifies an incomplete
            # operation.  Replacing a completed business response with 503 here
            # could cause an unsafe duplicate retry, so surface the gap to logs
            # and the commercial readiness chain instead.
            logger.critical("Mutation completion audit failed for %s: %s", audit_id, exc)
        return response
