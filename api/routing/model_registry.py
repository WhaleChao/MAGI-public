"""Centralised model role registry.

Reads ``json/models.json`` once, merges environment-variable overrides,
and provides the same API surface as ``api.model_config`` so callers can
migrate incrementally.

Usage::

    from api.routing.model_registry import resolve_model, get_role_model

    model = resolve_model("gemma-4")               # → "gemma-4-26b-a4b-it-4bit"
    model = get_role_model("vision")               # → "gemma-4-e4b-it-4bit"
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Iterable

from api.runtime_paths import get_json_dir

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelRole:
    name: str
    description: str
    model: str
    fallback_role: Optional[str] = None
    env_override: Optional[str] = None
    env_aliases: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Registry singleton
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_roles: dict[str, ModelRole] = {}
_aliases: set[str] = set()
_loaded = False


def _resolve_env(env_name: Optional[str], aliases: list[str] | None = None) -> Optional[str]:
    """Check env var + aliases, return first non-empty value or None."""
    if env_name:
        val = (os.environ.get(env_name) or "").strip()
        if val:
            return val
    for alias in (aliases or []):
        val = (os.environ.get(alias) or "").strip()
        if val:
            return val
    return None


def _load_registry() -> tuple[dict[str, ModelRole], set[str]]:
    path = get_json_dir() / "models.json"
    if not path.exists():
        _log.warning("models.json not found at %s – using defaults from model_config", path)
        # Fall back to model_config constants
        from api.model_config import (
            TEXT_PRIMARY_MODEL, VISION_MODEL, EMBED_MODEL, TEXT_MODEL_ALIASES,
        )
        roles = {
            "text_primary": ModelRole("text_primary", "Primary text model", TEXT_PRIMARY_MODEL),
            "vision": ModelRole("vision", "Vision model", VISION_MODEL),
            "embedding": ModelRole("embedding", "Embedding model", EMBED_MODEL),
        }
        return roles, TEXT_MODEL_ALIASES

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.exception("Failed to parse models.json")
        return {}, set()

    # Parse roles
    roles: dict[str, ModelRole] = {}
    for name, cfg in raw.get("roles", {}).items():
        env_override = cfg.get("env_override")
        env_aliases = cfg.get("env_aliases", [])
        env_model = _resolve_env(env_override, env_aliases)
        model = env_model or cfg.get("model") or ""
        roles[name] = ModelRole(
            name=name,
            description=cfg.get("description", ""),
            model=model,
            fallback_role=cfg.get("fallback_role"),
            env_override=env_override,
            env_aliases=tuple(env_aliases),
        )

    # Parse aliases
    aliases_cfg = raw.get("aliases", {})
    alias_set = set(str(a).strip().lower() for a in aliases_cfg.get("names", []))

    return roles, alias_set


def _ensure_loaded() -> None:
    global _loaded, _roles, _aliases
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _roles, _aliases = _load_registry()
        _loaded = True


def reload() -> None:
    """Force reload from disk."""
    global _loaded, _roles, _aliases
    with _lock:
        _roles, _aliases = _load_registry()
        _loaded = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_role_model(role: str) -> str:
    """Return the concrete model name for a logical *role*.

    Follows ``fallback_role`` chains up to 3 levels deep.
    """
    _ensure_loaded()
    visited: set[str] = set()
    current = role
    for _ in range(4):
        if current in visited:
            break
        visited.add(current)
        entry = _roles.get(current)
        if entry is None:
            break
        if entry.model:
            return entry.model
        if entry.fallback_role:
            current = entry.fallback_role
        else:
            break
    # Final fallback: text_primary
    primary = _roles.get("text_primary")
    if primary and primary.model:
        return primary.model
    # Absolute fallback
    from api.model_config import TEXT_PRIMARY_MODEL
    return TEXT_PRIMARY_MODEL


def is_alias(name: Optional[str]) -> bool:
    """Return True if *name* is a known legacy model alias."""
    _ensure_loaded()
    return str(name or "").strip().lower() in _aliases


def resolve_model(name: Optional[str] = None, *, available: Iterable[str] | None = None) -> str:
    """Resolve a model name, handling aliases and availability.

    Compatible with ``model_config.resolve_text_model()``.
    """
    _ensure_loaded()
    requested = str(name or "").strip()
    primary = get_role_model("text_primary")
    candidate = primary if is_alias(requested) else (requested or primary)
    if available is None:
        return candidate
    models = [str(m).strip() for m in available if str(m).strip()]
    if not models:
        return candidate
    if candidate in models:
        return candidate
    low = candidate.lower()
    for model in models:
        model_low = model.lower()
        if low and (model_low == low or low in model_low or model_low.startswith(low)):
            return model
    for model in models:
        if "gemma-4" in model.lower():
            return model
    return models[0]


def list_roles() -> list[ModelRole]:
    """Return all registered model roles."""
    _ensure_loaded()
    return list(_roles.values())


def get_role(name: str) -> Optional[ModelRole]:
    """Return the ModelRole for *name*, or None."""
    _ensure_loaded()
    return _roles.get(name)
