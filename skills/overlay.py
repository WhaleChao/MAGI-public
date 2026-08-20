"""Read-through, copy-on-write storage for mutable MAGI skills.

Release ``skills/`` is a seed catalog.  Runtime edits, generated skills,
versions, dependency installs, and routing definitions belong in an external
overlay so a deployed release remains byte-for-byte immutable.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SEED_METADATA_FILE = ".overlay-seed.json"


def base_skills_dir() -> Path:
    return _ROOT / "skills"


def skill_overlay_dir() -> Path:
    explicit = str(os.environ.get("MAGI_SKILL_OVERLAY_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    shared = str(
        os.environ.get("MAGI_V3_SHARED_STATE_DIR")
        or os.environ.get("MAGI_SHARED_STATE_DIR")
        or ""
    ).strip()
    if shared:
        return Path(shared).expanduser() / "skill-overlays"
    return _ROOT / ".runtime" / "skill-overlays"


def skill_versions_dir() -> Path:
    return skill_overlay_dir() / ".versions"


def skill_runtime_site_packages_dir() -> Path:
    explicit = str(os.environ.get("MAGI_SKILL_RUNTIME_SITE_PACKAGES") or "").strip()
    return Path(explicit).expanduser() if explicit else skill_overlay_dir() / ".runtime-site-packages"


def skill_events_file() -> Path:
    explicit = str(os.environ.get("MAGI_SKILL_EVENTS_FILE") or "").strip()
    return Path(explicit).expanduser() if explicit else skill_overlay_dir() / ".logs" / "skill_runtime_events.jsonl"


def skill_usage_tracker_file() -> Path:
    explicit = str(os.environ.get("MAGI_SKILL_USAGE_TRACKER_FILE") or "").strip()
    return Path(explicit).expanduser() if explicit else skill_overlay_dir() / ".logs" / "skill_usage_events.jsonl"


def validate_skill_name(skill_name: str) -> str:
    name = str(skill_name or "").strip()
    if not _SKILL_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ValueError("invalid_skill_name")
    return name


def _safe_child(root: Path, *parts: str) -> Path:
    root_abs = root.expanduser().absolute()
    candidate = root_abs.joinpath(*parts)
    resolved_root = root_abs.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise ValueError("invalid_skill_path")
    return candidate


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tracked_files(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError(f"unsafe_skill_symlink:{relative.as_posix()}")
        if not path.is_file():
            continue
        if (
            relative.name in {_SEED_METADATA_FILE, ".DS_Store"}
            or "__pycache__" in relative.parts
            or relative.suffix in {".pyc", ".pyo"}
        ):
            continue
        files[relative.as_posix()] = _file_sha256(path)
    return files


def _write_seed_metadata(skill_dir: Path, base_files: dict[str, str]) -> None:
    target = skill_dir / _SEED_METADATA_FILE
    payload = {
        "schema_version": 1,
        "base_files": dict(sorted(base_files.items())),
    }
    fd, temp_name = tempfile.mkstemp(prefix=".overlay-seed.", dir=str(skill_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        Path(temp_name).replace(target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _read_seed_metadata(skill_dir: Path) -> dict[str, str] | None:
    path = skill_dir / _SEED_METADATA_FILE
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe_skill_seed_metadata")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("invalid_skill_seed_metadata") from exc
    files = payload.get("base_files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        raise ValueError("invalid_skill_seed_metadata")
    normalized: dict[str, str] = {}
    for relative, digest in files.items():
        candidate = Path(str(relative))
        if candidate.is_absolute() or ".." in candidate.parts or not str(relative):
            raise ValueError("invalid_skill_seed_metadata_path")
        if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise ValueError("invalid_skill_seed_metadata_digest")
        normalized[candidate.as_posix()] = str(digest)
    return normalized


def _atomic_copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        temp_path.replace(destination)
    finally:
        temp_path.unlink(missing_ok=True)


def rebase_overlay_skill(skill_name: str) -> Path:
    """Refresh untouched copy-on-write files from the current release seed.

    The seed manifest records exactly what was copied when the overlay shadow
    was created.  Files whose overlay hash still equals that recorded seed are
    safe to advance to a newer release; user-edited files (including SKILL.md)
    remain untouched.
    """
    name = validate_skill_name(skill_name)
    overlay_root = skill_overlay_dir().expanduser().absolute()
    if overlay_root.exists() and overlay_root.is_symlink():
        raise ValueError("unsafe_skill_overlay_root")
    destination = _safe_child(overlay_root, name)
    if not destination.is_dir() or destination.is_symlink():
        return destination
    _reject_symlink_tree(destination)
    previous_base = _read_seed_metadata(destination)
    seed = _safe_child(base_skills_dir(), name)
    current_base = _tracked_files(seed)

    # Pre-metadata overlays cannot be safely classified as copied vs. edited.
    # Establish a conservative baseline without overwriting any existing file.
    if previous_base is None:
        for relative in sorted(current_base):
            target = destination / relative
            if not target.exists():
                _atomic_copy_file(seed / relative, target)
        _write_seed_metadata(destination, current_base)
        return destination

    overlay_files = _tracked_files(destination)
    for relative, old_digest in previous_base.items():
        target = destination / relative
        current_overlay_digest = overlay_files.get(relative)
        if current_overlay_digest != old_digest:
            # Missing files are user deletions; differing files are user edits.
            continue
        if relative in current_base:
            if current_overlay_digest != current_base[relative]:
                _atomic_copy_file(seed / relative, target)
        else:
            target.unlink(missing_ok=True)

    for relative in sorted(set(current_base) - set(previous_base)):
        target = destination / relative
        if not target.exists():
            _atomic_copy_file(seed / relative, target)

    _write_seed_metadata(destination, current_base)
    return destination


def skill_roots() -> list[Path]:
    """Return precedence order: mutable overlay first, release seed second."""
    return [skill_overlay_dir(), base_skills_dir()]


def effective_skill_dir(skill_name: str) -> Path:
    name = validate_skill_name(skill_name)
    overlay_root = skill_overlay_dir()
    if overlay_root.exists() and overlay_root.is_symlink():
        raise ValueError("unsafe_skill_overlay_root")
    overlay = _safe_child(overlay_root, name)
    if overlay.is_dir() and not overlay.is_symlink():
        return rebase_overlay_skill(name)
    return _safe_child(base_skills_dir(), name)


def effective_skill_file(skill_name: str, filename: str) -> Path:
    name = validate_skill_name(skill_name)
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise ValueError("invalid_skill_file")
    overlay_root = skill_overlay_dir()
    if overlay_root.exists() and overlay_root.is_symlink():
        raise ValueError("unsafe_skill_overlay_root")
    overlay_file = _safe_child(overlay_root, name, filename)
    if overlay_file.is_file() and not overlay_file.is_symlink():
        rebase_overlay_skill(name)
        if not overlay_file.is_file() or overlay_file.is_symlink():
            return _safe_child(base_skills_dir(), name, filename)
        return overlay_file
    return _safe_child(base_skills_dir(), name, filename)


def runtime_skill_dir(skill_name: str) -> Path:
    """Choose the runnable tree without relocating an unchanged base action.

    A SKILL.md-only NERV edit shadows documentation in the overlay, but many
    legacy actions derive the MAGI root from ``__file__``.  Running a
    byte-identical copied action from external state would therefore break
    those skills.  Use the release seed until executable/support files truly
    diverge; generated or repaired code runs from the overlay.
    """
    name = validate_skill_name(skill_name)
    overlay_root = skill_overlay_dir()
    if overlay_root.exists() and overlay_root.is_symlink():
        raise ValueError("unsafe_skill_overlay_root")
    overlay = _safe_child(overlay_root, name)
    base = _safe_child(base_skills_dir(), name)
    if not overlay.is_dir() or overlay.is_symlink():
        return base
    rebase_overlay_skill(name)
    _reject_symlink_tree(overlay)
    if not base.is_dir() or base.is_symlink():
        return overlay

    def _runtime_files(root: Path) -> dict[str, Path]:
        files: dict[str, Path] = {}
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if relative.name in {"SKILL.md", _SEED_METADATA_FILE} or "__pycache__" in relative.parts:
                continue
            if relative.suffix in {".pyc", ".pyo"} or relative.name == ".DS_Store":
                continue
            files[relative.as_posix()] = path
        return files

    overlay_files = _runtime_files(overlay)
    base_files = _runtime_files(base)
    if set(overlay_files) != set(base_files):
        return overlay
    for relative, overlay_file in overlay_files.items():
        if not filecmp.cmp(overlay_file, base_files[relative], shallow=False):
            return overlay
    return base


def _reject_symlink_tree(root: Path) -> None:
    if root.is_symlink():
        raise ValueError("unsafe_skill_symlink")
    for item in root.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"unsafe_skill_symlink:{item.name}")


def ensure_overlay_skill(skill_name: str) -> Path:
    """Create a complete overlay shadow from the release seed, once."""
    name = validate_skill_name(skill_name)
    overlay_root = skill_overlay_dir().expanduser().absolute()
    if overlay_root.exists() and overlay_root.is_symlink():
        raise ValueError("unsafe_skill_overlay_root")
    overlay_root.mkdir(parents=True, exist_ok=True)
    destination = _safe_child(overlay_root, name)
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("unsafe_skill_overlay_path")
        _reject_symlink_tree(destination)
        return rebase_overlay_skill(name)

    seed = _safe_child(base_skills_dir(), name)
    if not seed.is_dir():
        destination.mkdir(parents=False, exist_ok=False)
        _write_seed_metadata(destination, {})
        return destination
    _reject_symlink_tree(seed)

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=str(overlay_root)))
    try:
        shutil.copytree(seed, temp_dir, dirs_exist_ok=True, symlinks=False)
        _write_seed_metadata(temp_dir, _tracked_files(seed))
        try:
            temp_dir.replace(destination)
        except OSError:
            if not destination.is_dir() or destination.is_symlink():
                raise
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
    return destination


def mutable_skill_file(skill_name: str, filename: str) -> Path:
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise ValueError("invalid_skill_file")
    skill_dir = ensure_overlay_skill(skill_name)
    target = _safe_child(skill_dir, filename)
    if target.exists() and target.is_symlink():
        raise ValueError("unsafe_skill_overlay_file")
    return target


def effective_definitions_path() -> Path:
    overlay_root = skill_overlay_dir()
    if overlay_root.exists() and overlay_root.is_symlink():
        raise ValueError("unsafe_skill_overlay_root")
    overlay = overlay_root / "definitions.json"
    return overlay if overlay.is_file() and not overlay.is_symlink() else base_skills_dir() / "definitions.json"


def mutable_definitions_path() -> Path:
    overlay_root = skill_overlay_dir().expanduser().absolute()
    if overlay_root.exists() and overlay_root.is_symlink():
        raise ValueError("unsafe_skill_overlay_root")
    overlay_root.mkdir(parents=True, exist_ok=True)
    target = _safe_child(overlay_root, "definitions.json")
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise ValueError("unsafe_skill_definitions_path")
        return target
    seed = base_skills_dir() / "definitions.json"
    if seed.is_file() and not seed.is_symlink():
        shutil.copy2(seed, target)
    return target
