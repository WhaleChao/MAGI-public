"""Small authentication helpers for the Flask server entry point."""

from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

DEFAULT_POST_LOGIN_TARGET = "/dashboard"


def env_truthy(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def sanitize_login_next(raw_next: str | None) -> str:
    """Return a safe same-origin post-login redirect target."""
    candidate = (raw_next or "").strip()
    if not candidate or candidate.startswith("//"):
        return DEFAULT_POST_LOGIN_TARGET
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc or not parsed.path or not parsed.path.startswith("/"):
        return DEFAULT_POST_LOGIN_TARGET
    if parsed.path.startswith(("/login", "/register", "/logout")):
        return DEFAULT_POST_LOGIN_TARGET
    return urlunparse(("", "", parsed.path, "", parsed.query, parsed.fragment))


def default_tenant_id() -> str:
    return (os.environ.get("MAGI_TENANT_ID") or "default").strip() or "default"


def tenant_id_from_user_data(user_data: dict | None) -> str:
    if not isinstance(user_data, dict):
        return default_tenant_id()
    return str(
        user_data.get("default_tenant_id")
        or user_data.get("tenant_id")
        or default_tenant_id()
    ).strip() or default_tenant_id()
