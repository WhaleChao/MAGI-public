"""Shared cache path resolver for Judicial Yuan API artifacts."""
from __future__ import annotations

import getpass
import logging
import os
from pathlib import Path
from typing import Optional

DEFAULT_JUDGMENT_CACHE_ROOT = Path.home() / ".cache" / "judgment_collector"
DEFAULT_JUDGMENT_CACHE_FALLBACK = Path.home() / ".cache" / "judgment_collector_local"
NAS_FALLBACK_ENV = "JUDGMENT_CACHE_ROOT_NAS_FALLBACK"
DEFAULT_NAS_HOMES_MOUNT = Path("/Volumes/homes")
DEFAULT_NAS_CACHE_SUBPATH = Path("00_MAGI") / "cache" / "judgment_collector"
DEFAULT_NAS_ARCHIVE_CACHE_SUBPATH = Path("MAGI_archives") / "magi_user_cache_offload" / "judgment_collector"


def _expand(path: str | os.PathLike[str] | None, default: Path) -> Path:
    if path is None or str(path).strip() == "":
        return default
    return Path(os.path.expanduser(os.fspath(path)))


def _is_managed_mount_path(path: Path) -> bool:
    parts = path.expanduser().parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return True
    mount_root = Path.home() / ".magi_mounts"
    try:
        path.expanduser().relative_to(mount_root)
        return True
    except ValueError:
        return False


def _managed_mountpoint(path: Path) -> Path | None:
    parts = path.expanduser().parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return Path(parts[0]) / parts[1] / parts[2]
    mount_root = Path.home() / ".magi_mounts"
    try:
        rel = path.expanduser().relative_to(mount_root)
    except ValueError:
        return None
    if not rel.parts:
        return mount_root
    return mount_root / rel.parts[0]


def _managed_mount_is_mounted(path: Path) -> bool:
    mountpoint = _managed_mountpoint(path)
    return mountpoint is None or os.path.ismount(mountpoint)


def _prepare_root(path: Path, *, require_mounted_volume: bool, logger: Optional[logging.Logger]) -> Path | None:
    if require_mounted_volume and not _managed_mount_is_mounted(path):
        if logger is not None:
            logger.warning("judgment NAS cache fallback skipped because volume is not mounted: %s", path)
        return None
    try:
        path.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            return path
    except OSError as exc:
        if logger is not None:
            logger.warning("judgment cache root candidate unavailable: %s (%s)", path, exc)
    return None


def nas_judgment_cache_candidates() -> list[Path]:
    """Return NAS-backed cache candidates ordered by local MAGI configuration."""

    explicit = str(os.environ.get(NAS_FALLBACK_ENV) or "").strip()
    if explicit:
        return [_expand(explicit, DEFAULT_NAS_HOMES_MOUNT / DEFAULT_NAS_CACHE_SUBPATH)]

    homes_mount = _expand(os.environ.get("MAGI_NAS_HOMES_MOUNT"), DEFAULT_NAS_HOMES_MOUNT)
    user_mount_root = _expand(os.environ.get("MAGI_NAS_USER_MOUNT_ROOT"), Path.home() / ".magi_mounts")
    archive_share = str(os.environ.get("MAGI_NAS_CACHE_SHARE") or "lumi").strip().strip("/\\") or "lumi"
    users: list[str] = []
    for value in (
        os.environ.get("MAGI_NAS_HOME_USER"),
        os.environ.get("MAGI_NAS_USER"),
        getpass.getuser(),
    ):
        name = str(value or "").strip()
        if name and name not in users:
            users.append(name)

    candidates: list[Path] = []
    candidates.append(user_mount_root / archive_share / DEFAULT_NAS_ARCHIVE_CACHE_SUBPATH)
    candidates.append(Path("/Volumes") / archive_share / DEFAULT_NAS_ARCHIVE_CACHE_SUBPATH)
    for user in users:
        home = homes_mount / user
        candidates.append(home / DEFAULT_NAS_CACHE_SUBPATH)
        candidates.append(home / ".magi_cache" / "judgment_collector")
    return candidates


def preferred_nas_judgment_cache_root(
    *,
    create: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Path | None:
    for candidate in nas_judgment_cache_candidates():
        if create:
            prepared = _prepare_root(candidate, require_mounted_volume=True, logger=logger)
            if prepared is not None:
                return prepared
            continue
        if _managed_mount_is_mounted(candidate):
            return candidate
    return None


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
    the symlink itself exists while its target does not.  Cron jobs should use a
    mounted NAS fallback when available, otherwise the local fallback cache.
    """

    primary = _expand(path or os.environ.get("JUDGMENT_CACHE_ROOT"), DEFAULT_JUDGMENT_CACHE_ROOT)
    prepared = _prepare_root(primary, require_mounted_volume=False, logger=logger)
    if prepared is not None:
        return prepared

    nas_root = preferred_nas_judgment_cache_root(create=True, logger=logger)
    if nas_root is not None:
        return nas_root

    fallback_root = _expand(
        fallback or os.environ.get("JUDGMENT_CACHE_ROOT_FALLBACK"),
        DEFAULT_JUDGMENT_CACHE_FALLBACK,
    )
    require_mounted_volume = _is_managed_mount_path(fallback_root)
    prepared = _prepare_root(fallback_root, require_mounted_volume=require_mounted_volume, logger=logger)
    if prepared is not None:
        return prepared

    DEFAULT_JUDGMENT_CACHE_FALLBACK.mkdir(parents=True, exist_ok=True)
    return DEFAULT_JUDGMENT_CACHE_FALLBACK


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
