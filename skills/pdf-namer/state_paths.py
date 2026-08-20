#!/usr/bin/env python3
"""Runtime-state routing for the pdf-namer skill.

The source/release directory is a legacy V2 state location.  V3 must keep its
release tree immutable, so mutable files are routed to a dedicated state
directory.  Reads may fall back to a legacy seed, but writes always target the
runtime state directory.

Importing this module is deliberately side-effect free: no directory or file is
created until a caller is about to write.
"""

from __future__ import annotations

import os
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent


def pdf_namer_state_dir() -> Path:
    """Return the configured mutable-state root without creating it."""
    explicit = str(os.environ.get("MAGI_PDF_NAMER_STATE_DIR", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)

    v3_state = str(os.environ.get("MAGI_V3_STATE_DIR", "") or "").strip()
    if v3_state:
        return (Path(v3_state).expanduser() / "pdf-namer").resolve(strict=False)

    # Backward compatibility: V2 historically stored mutable state beside code.
    return SKILL_DIR


def _relative(name: str | os.PathLike[str]) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("pdf-namer state path must be relative")
    return relative


def state_path(name: str | os.PathLike[str]) -> Path:
    """Return the write target for a mutable file, without creating it."""
    relative = _relative(name)
    if relative == Path("_case_index.json"):
        from api.runtime_paths import get_pdf_namer_case_index_path

        return get_pdf_namer_case_index_path()
    return pdf_namer_state_dir() / relative


def legacy_path(name: str | os.PathLike[str]) -> Path:
    return SKILL_DIR / _relative(name)


def read_path(name: str | os.PathLike[str]) -> Path:
    """Prefer runtime state and otherwise read the legacy release seed."""
    target = state_path(name)
    if target.exists():
        return target
    if any(
        str(os.environ.get(key, "") or "").strip()
        for key in ("MAGI_V3_RELEASE_ID", "MAGI_V3_DEPLOYMENT_MODE", "MAGI_V3_RELEASE_MANIFEST")
    ):
        return target
    legacy = legacy_path(name)
    if legacy != target and legacy.exists():
        return legacy
    return target


def configured_read_path(
    name: str | os.PathLike[str], configured_path: str | os.PathLike[str]
) -> Path:
    """Honor an explicit/test path, otherwise apply runtime/legacy fallback."""
    configured = Path(configured_path).expanduser().resolve(strict=False)
    default_target = state_path(name).resolve(strict=False)
    if configured != default_target:
        return configured
    return read_path(name)


def prepare_write(path: str | os.PathLike[str]) -> Path:
    """Create only the write target's parent directory and return the path."""
    isolated = bool(
        str(os.environ.get("MAGI_PDF_NAMER_STATE_DIR", "") or "").strip()
        or str(os.environ.get("MAGI_V3_STATE_DIR", "") or "").strip()
    )
    target = Path(path).expanduser()
    if not isolated:
        target = target.resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    # V3 writes must stay below the configured state root.  Resolve only after
    # checking the lexical path so an existing nested symlink cannot redirect a
    # file (or a directory such as ``logs``) into V2 or back into the release.
    root = pdf_namer_state_dir().expanduser().absolute()
    target = target.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("pdf-namer write target is outside its state directory") from exc
    if not relative.parts:
        raise RuntimeError("pdf-namer write target must name a file below its state directory")
    if root.exists() and root.is_symlink():
        raise RuntimeError("pdf-namer state directory must not be a symlink")

    current = root
    for part in relative.parent.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise RuntimeError("pdf-namer state path contains a symlink")
    if target.exists() and target.is_symlink():
        raise RuntimeError("pdf-namer state file must not be a symlink")

    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise RuntimeError("pdf-namer state path escapes its configured directory")
    if resolved_target == SKILL_DIR or SKILL_DIR in resolved_target.parents:
        raise RuntimeError("refusing to write pdf-namer runtime state into the release tree")

    target.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after mkdir so a pre-existing parent symlink, or a path replaced
    # during directory creation, fails closed before the caller opens the file.
    current = root
    if current.is_symlink():
        raise RuntimeError("pdf-namer state path contains a symlink")
    for part in relative.parent.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError("pdf-namer state path contains a symlink")
    resolved_target = target.resolve(strict=False)
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise RuntimeError("pdf-namer state path escapes its configured directory")
    return target
