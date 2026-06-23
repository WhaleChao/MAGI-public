"""Shared cache path resolver for Judicial Yuan API artifacts."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

DEFAULT_JUDGMENT_CACHE_ROOT = Path.home() / ".cache" / "judgment_collector"
DEFAULT_JUDGMENT_CACHE_FALLBACK = Path.home() / ".cache" / "judgment_collector_local"


def _expand(path: str | os.PathLike[str] | None, default: Path) -> Path:
    if path is None or str(path).strip() == "":
        return default
    return Path(os.path.expanduser(os.fspath(path)))


def ensure_judgment_cache_root(
    path: str | os.PathLike[str] | None = None,
    *,
    fallback: str | os.PathLike[str] | None = None,
    logger: Optional[logging.Logger] = None,
) -> Path:
    """Return a usable judgment cache root, falling back from broken offload symlinks.

    Several MAGI installs offload ``~/.cache/judgment_collector`` to an external
    volume via symlink.  When the volume is absent, ``Path.mkdir(exist_ok=True)``
    and ``os.makedirs(exist_ok=True)`` still raise ``FileExistsError`` because
    the symlink itself exists while its target does not.  Cron jobs must keep
    running in that state by using the local fallback cache.
    """

    primary = _expand(path or os.environ.get("JUDGMENT_CACHE_ROOT"), DEFAULT_JUDGMENT_CACHE_ROOT)
    try:
        primary.mkdir(parents=True, exist_ok=True)
        if primary.is_dir():
            return primary
    except OSError as exc:
        if logger is not None:
            logger.warning("judgment cache root unavailable, using fallback: %s (%s)", primary, exc)

    fallback_root = _expand(
        fallback or os.environ.get("JUDGMENT_CACHE_ROOT_FALLBACK"),
        DEFAULT_JUDGMENT_CACHE_FALLBACK,
    )
    fallback_root.mkdir(parents=True, exist_ok=True)
    return fallback_root


def judicial_api_cache_root(*, create: bool = True) -> Path:
    """Return the Judicial Yuan API cache root used by pull/process/report jobs."""

    override = os.environ.get("JUDICIAL_API_CACHE_ROOT")
    if override:
        root = _expand(override, DEFAULT_JUDGMENT_CACHE_ROOT / "judicial_api")
        if create:
            try:
                root.mkdir(parents=True, exist_ok=True)
                if root.is_dir():
                    return root
            except OSError:
                fallback_root = ensure_judgment_cache_root() / "judicial_api"
                fallback_root.mkdir(parents=True, exist_ok=True)
                return fallback_root
        return root

    root = ensure_judgment_cache_root() / "judicial_api"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root
