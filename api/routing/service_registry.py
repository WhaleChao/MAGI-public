"""Centralised service endpoint registry.

Reads ``json/services.json`` once, merges environment-variable overrides,
and exposes a simple lookup API so that no Python file needs to hard-code
a host, port, or URL.

Usage::

    from api.routing.service_registry import get_service_url, get_service

    url = get_service_url("omlx_inference")       # "http://127.0.0.1:8080"
    svc = get_service("tools_api")                 # ServiceEndpoint(...)
    svc.base_url                                   # "http://127.0.0.1:5003"
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.runtime_paths import get_json_dir

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServiceEndpoint:
    name: str
    description: str
    host: str
    port: int
    protocol: str = "http"

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# Registry singleton
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_registry: dict[str, ServiceEndpoint] = {}
_loaded = False


def _load_registry() -> dict[str, ServiceEndpoint]:
    path = get_json_dir() / "services.json"
    if not path.exists():
        _log.warning("services.json not found at %s – using empty registry", path)
        return {}

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.exception("Failed to parse services.json")
        return {}

    services = raw.get("services", {})
    result: dict[str, ServiceEndpoint] = {}
    for name, cfg in services.items():
        env = cfg.get("env_override", {})
        host = (os.environ.get(env.get("host", "")) or "").strip() or cfg.get("host", "127.0.0.1")
        port_str = (os.environ.get(env.get("port", "")) or "").strip()
        port = int(port_str) if port_str else int(cfg.get("port", 0))
        result[name] = ServiceEndpoint(
            name=name,
            description=cfg.get("description", ""),
            host=host,
            port=port,
            protocol=cfg.get("protocol", "http"),
        )
    return result


def _ensure_loaded() -> None:
    global _loaded, _registry
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _registry = _load_registry()
        _loaded = True


def reload() -> None:
    """Force reload from disk (e.g. after runtime override)."""
    global _loaded, _registry
    with _lock:
        _registry = _load_registry()
        _loaded = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_service(name: str) -> ServiceEndpoint | None:
    """Return the *ServiceEndpoint* for *name*, or ``None``."""
    _ensure_loaded()
    return _registry.get(name)


def get_service_url(name: str, *, path: str = "") -> str:
    """Return the base URL for service *name*.

    Raises ``KeyError`` if the service is not registered.
    """
    _ensure_loaded()
    svc = _registry.get(name)
    if svc is None:
        raise KeyError(f"Unknown service: {name!r}")
    url = svc.base_url
    if path:
        url = url.rstrip("/") + "/" + path.lstrip("/")
    return url


def get_service_host_port(name: str) -> tuple[str, int]:
    """Return ``(host, port)`` for service *name*."""
    _ensure_loaded()
    svc = _registry.get(name)
    if svc is None:
        raise KeyError(f"Unknown service: {name!r}")
    return svc.host, svc.port


def list_services() -> list[ServiceEndpoint]:
    """Return all registered services."""
    _ensure_loaded()
    return list(_registry.values())
