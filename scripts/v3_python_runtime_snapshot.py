#!/usr/bin/env python3
"""Hash and verify the complete external Python environment used by MAGI V3."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence



class _PortableFileLock:
    """Standalone advisory lock used before the release package is importable."""

    if os.name == "nt":
        _backend = importlib.import_module("msvcrt")
        LOCK_SH = 1
        LOCK_EX = 2
        LOCK_NB = 4
        LOCK_UN = 8

        @classmethod
        def flock(cls, descriptor: int, operation: int) -> None:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if operation & cls.LOCK_UN:
                try:
                    cls._backend.locking(descriptor, cls._backend.LK_UNLCK, 1)
                except OSError:
                    pass
                return
            mode = cls._backend.LK_NBLCK if operation & cls.LOCK_NB else cls._backend.LK_LOCK
            try:
                cls._backend.locking(descriptor, mode, 1)
            except OSError as exc:
                raise BlockingIOError(str(exc)) from exc

    else:
        _backend = importlib.import_module("fcntl")
        LOCK_SH = _backend.LOCK_SH
        LOCK_EX = _backend.LOCK_EX
        LOCK_NB = _backend.LOCK_NB
        LOCK_UN = _backend.LOCK_UN
        flock = staticmethod(_backend.flock)


fcntl = _PortableFileLock

sys.dont_write_bytecode = True

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BYTECODE_CACHE_POLICY = {
    "default_cache_directories": "structurally_validated_but_not_hash_bound",
    "required_pycache_prefix": "/dev/null",
    "write_bytecode": False,
}
_SAFE_EXECUTABLE_PTH_LINES = frozenset(
    {
        "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local') == 'local'; enabled and __import__('_distutils_hack').add_shim();",
    }
)


class PythonRuntimeBlocked(ValueError):
    """The external Python environment is missing, unsafe, or has drifted."""


def _owned_by_current_user(metadata: os.stat_result) -> bool:
    """Use POSIX ownership when available; Windows ACLs have no st_uid API."""

    getuid = getattr(os, "getuid", None)
    return True if getuid is None else metadata.st_uid == getuid()


def _private_file_mode(metadata: os.stat_result) -> bool:
    """Enforce 0600 on POSIX; Windows privacy is provided by user-scoped ACLs."""

    if os.name == "nt":
        return True
    return stat.S_IMODE(metadata.st_mode) == 0o600


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_root(python_runtime: Path) -> tuple[Path, Path, Path]:
    declared = Path(os.path.abspath(python_runtime.expanduser()))
    if not declared.is_file() or not os.access(declared, os.X_OK):
        raise PythonRuntimeBlocked("Python runtime must be an executable file")
    realpath = declared.resolve(strict=True)
    if not realpath.is_file():
        raise PythonRuntimeBlocked("Python runtime must resolve to a regular file")
    venv_root = declared.parent.parent
    root = venv_root if (venv_root / "pyvenv.cfg").is_file() else declared.parent
    if root.is_symlink() or not root.is_dir():
        raise PythonRuntimeBlocked("Python runtime root must be a non-symlink directory")
    return root.resolve(strict=True), declared, realpath


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_pth(root: Path, path: Path) -> None:
    """Reject startup hooks that can import code from outside the bound runtime.

    ``.pth`` files execute before the release entry point.  Hashing one is not
    sufficient when it adds a mutable V2 checkout to ``sys.path`` or imports a
    project module from there.  Only an internal path entry or setuptools'
    fixed distutils shim is accepted.
    """

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PythonRuntimeBlocked(f"runtime .pth file is unreadable: {path}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("import ") or line.startswith("import\t"):
            if line not in _SAFE_EXECUTABLE_PTH_LINES:
                raise PythonRuntimeBlocked(
                    f"runtime .pth executable line is not allowlisted: {path.name}"
                )
            continue
        entry = Path(line).expanduser()
        resolved = (entry if entry.is_absolute() else path.parent / entry).resolve(strict=False)
        if not _inside(resolved, root) or not resolved.exists():
            raise PythonRuntimeBlocked(
                f"runtime .pth path escapes the bound environment: {path.name}"
            )


def _validate_excluded_bytecode_cache(path: Path) -> None:
    """Allow only inert CPython cache files in an excluded __pycache__ tree.

    Production starts Python with ``-B -X pycache_prefix=/dev/null``.  Default
    ``__pycache__`` directories therefore are neither read nor written, and
    must not make the supply-chain tree drift as Homebrew imports new stdlib
    modules.  Reject every non-regular or non-bytecode member so the exclusion
    can never hide source, native code, startup hooks, or symlink escapes.
    """

    try:
        members = tuple(path.iterdir())
    except OSError as exc:
        raise PythonRuntimeBlocked(f"runtime bytecode cache is unreadable: {path}") from exc
    for member in members:
        metadata = member.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or member.suffix != ".pyc"
        ):
            raise PythonRuntimeBlocked(
                f"runtime bytecode cache contains a forbidden member: {member}"
            )


def _scan(
    root: Path,
    *,
    external_python: Optional[Path],
    allow_internal_directory_symlinks: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    files: list[dict[str, Any]] = []
    directories: list[dict[str, Any]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        directory_names.sort()
        file_names.sort()
        relative_directory = base.relative_to(root).as_posix()
        directories.append(
            {
                "path": relative_directory,
                "mode": f"{stat.S_IMODE(base.stat().st_mode):04o}",
            }
        )
        for name in tuple(directory_names):
            candidate = base / name
            if name == "__pycache__" and not candidate.is_symlink():
                _validate_excluded_bytecode_cache(candidate)
                directory_names.remove(name)
                continue
            if candidate.is_symlink():
                relative = candidate.relative_to(root).as_posix()
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise PythonRuntimeBlocked(f"broken runtime symlink: {relative}") from exc
                if not allow_internal_directory_symlinks or not _inside(resolved, root):
                    raise PythonRuntimeBlocked(
                        f"symlinked directory is forbidden in Python runtime: {relative}"
                    )
                files.append(
                    {
                        "path": relative,
                        "kind": "directory_symlink",
                        "target": os.readlink(candidate),
                    }
                )
                directory_names.remove(name)
        for name in file_names:
            candidate = base / name
            relative = candidate.relative_to(root).as_posix()
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(candidate)
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise PythonRuntimeBlocked(f"broken runtime symlink: {relative}") from exc
                if not resolved.is_file():
                    raise PythonRuntimeBlocked(f"runtime symlink target is not a file: {relative}")
                if not _inside(resolved, root) and not (
                    external_python is not None
                    and relative.startswith("bin/")
                    and resolved == external_python
                ):
                    raise PythonRuntimeBlocked(
                        f"runtime symlink escapes the bound environment: {relative}"
                    )
                if candidate.suffix == ".pth":
                    _validate_pth(root, candidate)
                files.append(
                    {
                        "path": relative,
                        "kind": "symlink",
                        "target": target,
                        "target_sha256": _sha256_file(resolved),
                    }
                )
            elif stat.S_ISREG(metadata.st_mode):
                if candidate.suffix == ".pth":
                    _validate_pth(root, candidate)
                files.append(
                    {
                        "path": relative,
                        "kind": "file",
                        "sha256": _sha256_file(candidate),
                        "size": metadata.st_size,
                        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                    }
                )
            else:
                raise PythonRuntimeBlocked(f"special runtime member is forbidden: {relative}")
    files.sort(key=lambda row: row["path"])
    directories.sort(key=lambda row: row["path"])
    return files, directories


def _pyvenv_config(root: Path, realpath: Path) -> dict[str, str]:
    path = root / "pyvenv.cfg"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PythonRuntimeBlocked("Python virtual environment has no readable pyvenv.cfg") from exc
    values: dict[str, str] = {}
    for raw in lines:
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip().lower()] = value.strip()
    if values.get("include-system-site-packages", "").lower() != "false":
        raise PythonRuntimeBlocked("pyvenv.cfg must disable system site-packages")
    executable_text = values.get("executable", "")
    if not executable_text or not Path(executable_text).is_absolute():
        raise PythonRuntimeBlocked("pyvenv.cfg executable must be absolute")
    try:
        if Path(executable_text).resolve(strict=True) != realpath:
            raise PythonRuntimeBlocked("pyvenv.cfg executable does not match Python runtime")
    except OSError as exc:
        raise PythonRuntimeBlocked("pyvenv.cfg executable is unavailable") from exc
    home_text = values.get("home", "")
    if not home_text or not Path(home_text).is_absolute():
        raise PythonRuntimeBlocked("pyvenv.cfg home must be absolute")
    home = Path(home_text).expanduser()
    candidates = (home / realpath.name, home / "python3", home / "python")
    if not any(candidate.exists() and candidate.resolve(strict=True) == realpath for candidate in candidates):
        raise PythonRuntimeBlocked("pyvenv.cfg home does not resolve to Python runtime")
    return values


def _base_runtime_root(realpath: Path) -> Path:
    # CPython's base prefix is the parent of ``bin`` for both ordinary Unix
    # installs and macOS Framework builds.  For Homebrew, bind the complete
    # immutable formula version root (including the Framework and formula
    # site-packages) using the realpath layout, never a forgeable marker file.
    base_prefix = realpath.parent.parent.resolve(strict=True)
    parts = base_prefix.parts
    try:
        cellar_index = parts.index("Cellar")
    except ValueError:
        return base_prefix
    if len(parts) <= cellar_index + 2:
        raise PythonRuntimeBlocked("Homebrew Python runtime layout is incomplete")
    formula_root = Path(*parts[: cellar_index + 3]).resolve(strict=True)
    if not _inside(base_prefix, formula_root):
        raise PythonRuntimeBlocked("Homebrew Python base prefix escapes formula root")
    return formula_root


def build_runtime_manifest(python_runtime: Path) -> tuple[bytes, dict[str, Any]]:
    root, declared, realpath = _runtime_root(python_runtime)
    _pyvenv_config(root, realpath)
    files, directories = _scan(root, external_python=realpath)
    if not files:
        raise PythonRuntimeBlocked("Python runtime inventory is empty")
    base_root = _base_runtime_root(realpath)
    if _inside(base_root, root):
        base_files: list[dict[str, Any]] = []
        base_directories: list[dict[str, Any]] = []
    else:
        base_files, base_directories = _scan(
            base_root,
            external_python=None,
            allow_internal_directory_symlinks=True,
        )
        if not base_files:
            raise PythonRuntimeBlocked("Python base runtime inventory is empty")
    inventory = {
        "directories": directories,
        "files": files,
        "base_directories": base_directories,
        "base_files": base_files,
    }
    tree_sha256 = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema_version": 1,
        "bytecode_cache_policy": BYTECODE_CACHE_POLICY,
        "runtime_root": str(root),
        "base_runtime_root": str(base_root),
        "python_runtime": str(declared),
        "python_runtime_realpath": str(realpath),
        "python_runtime_sha256": _sha256_file(realpath),
        "tree_sha256": tree_sha256,
        "file_count": len(files) + len(base_files),
        "directory_count": len(directories) + len(base_directories),
        **inventory,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    return encoded, {
        "runtime_root": str(root),
        "tree_sha256": tree_sha256,
        "file_count": len(files) + len(base_files),
        "directory_count": len(directories) + len(base_directories),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def verify_runtime_manifest(
    path: Path,
    *,
    expected_tree_sha256: Optional[str] = None,
    expected_python_runtime: Optional[Path] = None,
    expected_python_realpath: Optional[Path] = None,
) -> dict[str, Any]:
    manifest_path = path.expanduser()
    if not manifest_path.is_absolute() or manifest_path.is_symlink():
        raise PythonRuntimeBlocked("Python runtime manifest must be an absolute non-symlink file")
    try:
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PythonRuntimeBlocked(f"Python runtime manifest is unreadable: {exc}") from exc
    if not isinstance(expected, dict) or expected.get("schema_version") != 1:
        raise PythonRuntimeBlocked("Python runtime manifest schema_version must equal 1")
    if expected.get("bytecode_cache_policy") != BYTECODE_CACHE_POLICY:
        raise PythonRuntimeBlocked("Python runtime bytecode cache policy is invalid")
    tree_sha = expected.get("tree_sha256")
    runtime_text = expected.get("python_runtime")
    if not isinstance(tree_sha, str) or not SHA256_RE.fullmatch(tree_sha):
        raise PythonRuntimeBlocked("Python runtime manifest tree SHA-256 is invalid")
    if expected_tree_sha256 is not None and tree_sha != expected_tree_sha256:
        raise PythonRuntimeBlocked("Python runtime manifest tree SHA-256 binding mismatch")
    if not isinstance(runtime_text, str) or not Path(runtime_text).is_absolute():
        raise PythonRuntimeBlocked("Python runtime manifest executable path is invalid")
    if (
        expected_python_runtime is not None
        and Path(os.path.abspath(expected_python_runtime.expanduser())) != Path(runtime_text)
    ):
        raise PythonRuntimeBlocked("Python runtime manifest declared executable binding mismatch")
    realpath_text = expected.get("python_runtime_realpath")
    if (
        expected_python_realpath is not None
        and (
            not isinstance(realpath_text, str)
            or expected_python_realpath.expanduser().resolve(strict=True) != Path(realpath_text)
        )
    ):
        raise PythonRuntimeBlocked("Python runtime manifest realpath binding mismatch")
    encoded, report = build_runtime_manifest(Path(runtime_text))
    observed = json.loads(encoded)
    if observed != expected:
        raise PythonRuntimeBlocked("Python runtime tree or interpreter binding drift detected")
    return {"status": "passed", **report}


def verify_runtime_manifest_singleflight(
    path: Path,
    *,
    cache_path: Path,
    max_age_seconds: float,
    expected_tree_sha256: Optional[str] = None,
    expected_python_runtime: Optional[Path] = None,
    expected_python_realpath: Optional[Path] = None,
) -> dict[str, Any]:
    """Coalesce the full tree hash for roles in one host startup wave."""

    cache = cache_path.expanduser()
    if not cache.is_absolute() or cache.is_symlink():
        raise PythonRuntimeBlocked(
            "runtime verification cache must be absolute and non-symlinked"
        )
    parent = cache.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or parent.resolve(strict=True) != parent:
        raise PythonRuntimeBlocked("runtime verification cache parent is unsafe")
    max_age = max(0.1, min(float(max_age_seconds), 5.0))
    lock_path = cache.with_name(cache.name + ".lock")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size == 0:
            # msvcrt locks a byte range and therefore needs one private byte.
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not _private_file_mode(metadata)
            or not _owned_by_current_user(metadata)
        ):
            raise PythonRuntimeBlocked("runtime verification lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        binding = {
            "schema_version": 1,
            "manifest": str(path.expanduser()),
            "expected_tree_sha256": str(expected_tree_sha256 or ""),
            "expected_python_runtime": str(expected_python_runtime or ""),
            "expected_python_realpath": str(expected_python_realpath or ""),
        }
        try:
            cache_metadata = cache.lstat()
            if (
                stat.S_ISREG(cache_metadata.st_mode)
                and not stat.S_ISLNK(cache_metadata.st_mode)
                and _private_file_mode(cache_metadata)
                and _owned_by_current_user(cache_metadata)
            ):
                cached = json.loads(cache.read_text(encoding="utf-8"))
                verified_at = float(cached.get("verified_at_epoch") or 0.0)
                if (
                    cached.get("binding") == binding
                    and cached.get("result", {}).get("status") == "passed"
                    and 0.0 <= time.time() - verified_at <= max_age
                ):
                    return {
                        **cached["result"],
                        "verification_mode": "startup_singleflight_receipt",
                    }
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            pass

        result = verify_runtime_manifest(
            path,
            expected_tree_sha256=expected_tree_sha256,
            expected_python_runtime=expected_python_runtime,
            expected_python_realpath=expected_python_realpath,
        )
        payload = {
            "binding": binding,
            "verified_at_epoch": time.time(),
            "result": result,
        }
        temporary = cache.with_name(f".{cache.name}.{os.getpid()}.tmp")
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        output = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
            os.write(output, encoded)
            os.fsync(output)
        finally:
            os.close(output)
        os.replace(temporary, cache)
        return {**result, "verification_mode": "full"}
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _write_exclusive(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise PythonRuntimeBlocked("runtime manifest output must not already exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--python-runtime", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-tree-sha256")
    verify.add_argument("--expected-python-runtime", type=Path)
    verify.add_argument("--expected-python-realpath", type=Path)
    verify.add_argument("--singleflight-cache", type=Path)
    verify.add_argument("--singleflight-max-age-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            encoded, report = build_runtime_manifest(args.python_runtime)
            _write_exclusive(args.output, encoded)
            result = {"status": "passed", **report}
        else:
            verify_kwargs = {
                "expected_tree_sha256": args.expected_tree_sha256,
                "expected_python_runtime": args.expected_python_runtime,
                "expected_python_realpath": args.expected_python_realpath,
            }
            if args.singleflight_cache is not None:
                result = verify_runtime_manifest_singleflight(
                    args.manifest,
                    cache_path=args.singleflight_cache,
                    max_age_seconds=args.singleflight_max_age_seconds,
                    **verify_kwargs,
                )
            else:
                result = verify_runtime_manifest(args.manifest, **verify_kwargs)
    except (OSError, PythonRuntimeBlocked) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
