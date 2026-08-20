#!/usr/bin/env python3
"""Render V2 cron definitions as a release-bound, immutable V3 artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class CronSnapshotBlocked(ValueError):
    """The source schedule cannot be safely rebound to one V3 release."""


_RUNTIME_FIELDS = {
    "result",
    "result_evidence",
    "stdout",
    "stderr",
    "returncode",
    "timed_out",
    "duration_sec",
}
_PYTHON_NAME = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_CODE_TOP_LEVEL = frozenset({"api", "casper_ecosystem", "config", "gui", "scripts", "skills"})
_ENV_ASSIGNMENT = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>/.*)$")


@dataclass(frozen=True, slots=True)
class CronSourceIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _identity(stat_result: os.stat_result) -> CronSourceIdentity:
    return CronSourceIdentity(
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        ctime_ns=stat_result.st_ctime_ns,
    )


def _source_path(source: Path) -> Path:
    raw = source.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise CronSnapshotBlocked("cron source must be an absolute non-symlink file")
    try:
        path = raw.resolve(strict=True)
    except OSError as exc:
        raise CronSnapshotBlocked("cron source is unavailable") from exc
    if not path.is_file():
        raise CronSnapshotBlocked("cron source must be a regular file")
    return path


def _assert_source_identity(path: Path, identity: CronSourceIdentity) -> None:
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise CronSnapshotBlocked("cron source changed while rendering") from exc
    if not stat.S_ISREG(observed.st_mode) or _identity(observed) != identity:
        raise CronSnapshotBlocked("cron source changed while rendering")


def read_verified_cron_source(source: Path) -> tuple[Path, bytes, CronSourceIdentity]:
    """Read raw cron exactly once, rejecting symlinks and replacement races."""

    path = _source_path(source)
    initial = os.lstat(path)
    if not stat.S_ISREG(initial.st_mode):
        raise CronSnapshotBlocked("cron source must be a regular file")
    identity = _identity(initial)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != identity:
            raise CronSnapshotBlocked("cron source changed before it could be read")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise CronSnapshotBlocked("cron source changed while it was being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise CronSnapshotBlocked("cron source changed while it was being read")
        if _identity(os.fstat(descriptor)) != identity:
            raise CronSnapshotBlocked("cron source changed while it was being read")
    finally:
        os.close(descriptor)
    _assert_source_identity(path, identity)
    return path, b"".join(chunks), identity


def _clean_job(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in row.items()
        if str(key) not in _RUNTIME_FIELDS and not str(key).startswith("last_")
    }


def _source_roots(argv: Iterable[str]) -> set[Path]:
    roots: set[Path] = set()
    for value in argv:
        assignment = _ENV_ASSIGNMENT.fullmatch(value)
        if assignment:
            value = assignment.group("value")
        if not value.startswith("/"):
            continue
        candidate = Path(value)
        if _PYTHON_NAME.fullmatch(candidate.name) and candidate.parent.name == "bin":
            if candidate.parent.parent.name in {"venv", ".venv"}:
                roots.add(candidate.parent.parent.parent)
        for marker in _CODE_TOP_LEVEL | {".runtime", ".agent", "static", "exports", "_metrics", "_autopilot_runs"}:
            parts = candidate.parts
            if marker in parts:
                index = parts.index(marker)
                if index > 0:
                    roots.add(Path(*parts[:index]))
                break
    return roots


def _release_files(release_root: Path) -> set[str]:
    manifest = release_root / "release-manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        rows = payload["files"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise CronSnapshotBlocked(f"release manifest is unreadable: {exc}") from exc
    files = {
        str(row.get("path"))
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    if not files:
        raise CronSnapshotBlocked("release manifest file inventory is empty")
    return files


def _rebase_absolute(
    value: str,
    *,
    roots: set[Path],
    release_root: Path,
    release_files: set[str],
    runtime_root: Path,
    python_runtime: Path,
) -> str:
    path = Path(value)
    if _PYTHON_NAME.fullmatch(path.name) and path.parent.name == "bin" and path.parent.parent.name in {
        "venv",
        ".venv",
    }:
        return str(python_runtime)

    for source_root in sorted(roots, key=lambda item: len(item.parts), reverse=True):
        try:
            relative = path.relative_to(source_root)
        except ValueError:
            continue
        if not relative.parts:
            return str(release_root)
        head, tail = relative.parts[0], relative.parts[1:]
        if head == ".runtime":
            return str(runtime_root / "shared" / "runtime" / Path(*tail))
        if head == ".agent":
            return str(runtime_root / "shared" / "agent" / Path(*tail))
        if head == "exports":
            return str(runtime_root / "shared" / "exports" / Path(*tail))
        if head == "_metrics":
            return str(runtime_root / "shared" / "metrics" / Path(*tail))
        if head == "_autopilot_runs":
            return str(runtime_root / "shared" / "autopilot-runs" / Path(*tail))
        relative_text = relative.as_posix()
        if head == "static" and relative_text not in release_files:
            return str(runtime_root / "shared" / "static" / Path(*tail))
        if relative_text in release_files:
            return str(release_root / relative)
        raise CronSnapshotBlocked(f"cron argument is inside V2 but absent from V3 release: {value}")
    return value


def render_snapshot(
    *,
    source: Path,
    release_root: Path,
    runtime_root: Path,
    python_runtime: Path,
    source_bytes: bytes | None = None,
    source_identity: CronSourceIdentity | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Return deterministic JSON bytes and non-secret binding evidence."""

    if source_bytes is None or source_identity is None:
        source_path, raw_bytes, identity = read_verified_cron_source(source)
    else:
        source_path = _source_path(source)
        raw_bytes = source_bytes
        identity = source_identity
        _assert_source_identity(source_path, identity)
    release = release_root.expanduser().resolve(strict=True)
    runtime = runtime_root.expanduser().resolve(strict=False)
    python = Path(os.path.abspath(python_runtime.expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise CronSnapshotBlocked("V3 Python runtime must be an executable file")
    release_files = _release_files(release)
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CronSnapshotBlocked(f"cron source is unreadable: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise CronSnapshotBlocked("cron source must be a non-empty JSON list")

    source_digest_before = hashlib.sha256(raw_bytes).hexdigest()
    output: list[dict[str, Any]] = []
    source_roots: set[Path] = set()
    ids: set[str] = set()
    process_jobs = 0
    macro_jobs = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CronSnapshotBlocked(f"cron job {index} must be an object")
        job = _clean_job(item)
        job_id = str(job.get("id") or "").strip()
        command = str(job.get("command") or "").strip()
        if not job_id or job_id in ids or not command:
            raise CronSnapshotBlocked(f"cron job {index} has an invalid id or command")
        ids.add(job_id)
        if command.startswith("@MAGI"):
            macro_jobs += 1
            job["command"] = command
            output.append(job)
            continue
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise CronSnapshotBlocked(f"cron job {job_id} command is not parseable: {exc}") from exc
        if len(argv) >= 3 and argv[0] == "cd" and argv[2] == "&&":
            if not Path(argv[1]).is_absolute():
                raise CronSnapshotBlocked(f"cron job {job_id} has an unsafe cd prefix")
            argv = argv[3:]
        if not argv:
            raise CronSnapshotBlocked(f"cron job {job_id} has an empty process command")
        roots = _source_roots(argv)
        source_roots.update(roots)
        rewritten: list[str] = []
        for value in argv:
            assignment = _ENV_ASSIGNMENT.fullmatch(value)
            prefix = f"{assignment.group('name')}=" if assignment else ""
            absolute_value = assignment.group("value") if assignment else value
            if absolute_value.startswith("/"):
                absolute_value = _rebase_absolute(
                    absolute_value,
                    roots=roots,
                    release_root=release,
                    release_files=release_files,
                    runtime_root=runtime,
                    python_runtime=python,
                )
            rewritten.append(prefix + absolute_value)
        job["command"] = shlex.join(rewritten)
        process_jobs += 1
        output.append(job)

    encoded = (json.dumps(output, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _assert_source_identity(source_path, identity)
    forbidden = [str(root).encode() for root in source_roots if root != release]
    if any(prefix in encoded for prefix in forbidden):
        raise CronSnapshotBlocked("rendered cron snapshot retains a V2 source root")
    return encoded, {
        "source_sha256": source_digest_before,
        "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
        "job_count": len(output),
        "enabled_job_count": sum(1 for row in output if row.get("enabled", True) is True),
        "process_job_count": process_jobs,
        "macro_job_count": macro_jobs,
        "source_roots_rebased": sorted(str(root) for root in source_roots),
    }


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise CronSnapshotBlocked("cron snapshot permissions are not 0600")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data, report = render_snapshot(
            source=args.source,
            release_root=args.release_root,
            runtime_root=args.runtime_root,
            python_runtime=args.python_runtime,
        )
        if args.output.exists() or args.output.is_symlink():
            raise CronSnapshotBlocked("cron snapshot output must not already exist")
        _write_exclusive(args.output, data)
    except (OSError, CronSnapshotBlocked) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "passed", **report}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
