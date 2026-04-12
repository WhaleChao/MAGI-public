"""Centralised node topology registry.

Reads ``json/nodes.json`` once, merges environment-variable overrides,
and exposes lookup for compute/storage nodes so that no Python file
needs to hard-code Tailscale IPs or LAN addresses.

Usage::

    from api.routing.node_registry import get_node, get_node_url

    melchior = get_node("melchior")
    url = get_node_url("melchior", service="inference")  # "http://100.116.54.16:5002"
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from api.runtime_paths import get_json_dir

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeService:
    port: int
    protocol: str = "http"


@dataclass(frozen=True)
class Node:
    name: str
    description: str
    role: str
    tailscale_ip: Optional[str] = None
    lan_ip: Optional[str] = None
    services: dict[str, NodeService] = field(default_factory=dict)

    @property
    def preferred_ip(self) -> Optional[str]:
        """Return the best available IP (Tailscale preferred over LAN)."""
        return self.tailscale_ip or self.lan_ip


# ---------------------------------------------------------------------------
# Registry singleton
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_nodes: dict[str, Node] = {}
_loaded = False


def _load_registry() -> dict[str, Node]:
    path = get_json_dir() / "nodes.json"
    if not path.exists():
        _log.warning("nodes.json not found at %s", path)
        return {}

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.exception("Failed to parse nodes.json")
        return {}

    result: dict[str, Node] = {}
    for name, cfg in raw.get("nodes", {}).items():
        env = cfg.get("env_override") or {}
        ts_ip = (os.environ.get(env.get("tailscale_ip", "")) or "").strip() or cfg.get("tailscale_ip")
        lan_ip = (os.environ.get(env.get("lan_ip", "")) or "").strip() or cfg.get("lan_ip")

        svcs: dict[str, NodeService] = {}
        for svc_name, svc_cfg in cfg.get("services", {}).items():
            port_env = env.get("port", "")
            port_str = (os.environ.get(port_env) or "").strip() if port_env else ""
            port = int(port_str) if port_str else int(svc_cfg.get("port", 0))
            svcs[svc_name] = NodeService(port=port, protocol=svc_cfg.get("protocol", "http"))

        result[name] = Node(
            name=name,
            description=cfg.get("description", ""),
            role=cfg.get("role", ""),
            tailscale_ip=ts_ip if ts_ip else None,
            lan_ip=lan_ip if lan_ip else None,
            services=svcs,
        )
    return result


def _ensure_loaded() -> None:
    global _loaded, _nodes
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _nodes = _load_registry()
        _loaded = True


def reload() -> None:
    """Force reload from disk."""
    global _loaded, _nodes
    with _lock:
        _nodes = _load_registry()
        _loaded = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_node(name: str) -> Optional[Node]:
    """Return the *Node* for *name*, or ``None``."""
    _ensure_loaded()
    return _nodes.get(name)


def get_node_ip(name: str) -> Optional[str]:
    """Return the preferred IP for *name*."""
    _ensure_loaded()
    node = _nodes.get(name)
    return node.preferred_ip if node else None


def get_node_url(name: str, *, service: str = "inference", path: str = "") -> str:
    """Return the URL for a service on *name*.

    Raises ``KeyError`` if the node or service is not registered.
    """
    _ensure_loaded()
    node = _nodes.get(name)
    if node is None:
        raise KeyError(f"Unknown node: {name!r}")
    ip = node.preferred_ip
    if ip is None:
        raise KeyError(f"Node {name!r} has no IP configured")
    svc = node.services.get(service)
    if svc is None:
        raise KeyError(f"Node {name!r} has no service {service!r}")
    url = f"{svc.protocol}://{ip}:{svc.port}"
    if path:
        url = url.rstrip("/") + "/" + path.lstrip("/")
    return url


def list_nodes() -> list[Node]:
    """Return all registered nodes."""
    _ensure_loaded()
    return list(_nodes.values())


def get_nodes_by_role(role: str) -> list[Node]:
    """Return all nodes with a given *role*."""
    _ensure_loaded()
    return [n for n in _nodes.values() if n.role == role]
