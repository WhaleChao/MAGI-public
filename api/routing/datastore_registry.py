"""Centralised datastore connection registry.

Reads ``json/datastores.json`` once, merges environment-variable overrides,
and exposes connection parameters so that no Python file needs to hard-code
DB hosts, ports, or credentials.

Usage::

    from api.routing.datastore_registry import get_datastore, get_connection_params

    ds = get_datastore("local_mariadb")
    params = get_connection_params("local_mariadb")
    # {"host": "127.0.0.1", "port": 3307, "database": "magi_brain", ...}
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from api.runtime_paths import get_json_dir

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Datastore:
    name: str
    description: str
    driver: str
    host: Optional[str]
    port: Optional[int]
    database: Optional[str]
    user: Optional[str] = None
    password: Optional[str] = None


# ---------------------------------------------------------------------------
# Registry singleton
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_stores: dict[str, Datastore] = {}
_loaded = False


def _load_registry() -> dict[str, Datastore]:
    path = get_json_dir() / "datastores.json"
    if not path.exists():
        _log.warning("datastores.json not found at %s", path)
        return {}

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.exception("Failed to parse datastores.json")
        return {}

    result: dict[str, Datastore] = {}
    for name, cfg in raw.get("datastores", {}).items():
        env = cfg.get("env_override", {})

        def _env_or(key: str, fallback: Any = None) -> Optional[str]:
            env_name = env.get(key, "")
            val = (os.environ.get(env_name) or "").strip() if env_name else ""
            return val if val else (cfg.get(key) if cfg.get(key) is not None else fallback)

        host = _env_or("host")
        port_raw = _env_or("port")
        port = int(port_raw) if port_raw else None
        result[name] = Datastore(
            name=name,
            description=cfg.get("description", ""),
            driver=cfg.get("driver", "mariadb"),
            host=str(host) if host else None,
            port=port,
            database=_env_or("database"),
            user=_env_or("user"),
            password=_env_or("password"),
        )
    return result


def _ensure_loaded() -> None:
    global _loaded, _stores
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _stores = _load_registry()
        _loaded = True


def reload() -> None:
    """Force reload from disk."""
    global _loaded, _stores
    with _lock:
        _stores = _load_registry()
        _loaded = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_datastore(name: str) -> Optional[Datastore]:
    """Return the *Datastore* for *name*, or ``None``."""
    _ensure_loaded()
    return _stores.get(name)


def get_connection_params(name: str) -> dict[str, Any]:
    """Return a dict of connection parameters suitable for DB driver kwargs.

    Raises ``KeyError`` if the datastore is not registered.
    """
    _ensure_loaded()
    ds = _stores.get(name)
    if ds is None:
        raise KeyError(f"Unknown datastore: {name!r}")
    params: dict[str, Any] = {}
    if ds.host:
        params["host"] = ds.host
    if ds.port:
        params["port"] = ds.port
    if ds.database:
        params["database"] = ds.database
    if ds.user:
        params["user"] = ds.user
    if ds.password:
        params["password"] = ds.password
    return params


def list_datastores() -> list[Datastore]:
    """Return all registered datastores."""
    _ensure_loaded()
    return list(_stores.values())
