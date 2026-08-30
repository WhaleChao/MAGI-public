#!/usr/bin/env python3
"""Build immutable, non-secret inputs for release-quality validation.

This deliberately is *not* an isolated-live-validation input builder.  That
mode is restricted to one disabled inert cron fixture.  Quality certification
instead needs a release-bound rendering of the real scheduler definition and a
code-only snapshot of the website.  Neither credentials nor customer content
are copied into this artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v3_cron_snapshot import (
    CronSnapshotBlocked,
    read_verified_cron_source,
    render_snapshot,
)
from scripts.v3_campaign.runner import CampaignSafetyError, verify_release_bundle
from scripts.v3_deploy_prepare import (
    VALIDATION_ENV_BYTES,
    VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES,
    VALIDATION_GOOGLE_CREDENTIALS_BYTES,
    VALIDATION_LAF_CONFIG_BYTES,
    VALIDATION_LAF_GMAIL_TOKEN_BYTES,
)
from scripts.v3_python_runtime_snapshot import PythonRuntimeBlocked, verify_runtime_manifest


class QualityInputBuildError(RuntimeError):
    """A quality input artifact could not be safely built."""


_CODE_SUFFIXES = frozenset({".css", ".cjs", ".html", ".htm", ".js", ".mjs", ".py", ".pyi", ".sh", ".svg", ".txt"})
_EXCLUDED_COMPONENTS = frozenset(
    {
        ".git",
        ".github",
        ".claude",
        "__pycache__",
        "cache",
        "data",
        "node_modules",
        "tmp",
        "uploads",
    }
)
_SENSITIVE_MARKERS = ("credential", "secret", "token", "admin_config", ".env")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _new_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise QualityInputBuildError("quality input output root already exists") from exc
    _fsync_directory(path.parent)


def _absolute_file(
    value: Path, *, label: str, executable: bool = False, allow_symlink: bool = False
) -> Path:
    raw = value.expanduser()
    if not raw.is_absolute() or (raw.is_symlink() and not allow_symlink):
        raise QualityInputBuildError(f"{label} must be an absolute non-symlink file")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise QualityInputBuildError(f"{label} is missing") from exc
    if not resolved.is_file() or (executable and not os.access(resolved, os.X_OK)):
        raise QualityInputBuildError(f"{label} is not a usable file")
    return resolved


def _absolute_directory(value: Path, *, label: str) -> Path:
    raw = value.expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise QualityInputBuildError(f"{label} must be an absolute non-symlink directory")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise QualityInputBuildError(f"{label} is missing") from exc
    if not resolved.is_dir():
        raise QualityInputBuildError(f"{label} is not a directory")
    return resolved


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _relative_files(root: Path) -> Iterable[Path]:
    for base, directories, names in os.walk(root, followlinks=False):
        directory = Path(base)
        retained: list[str] = []
        for name in directories:
            member = directory / name
            if member.is_symlink():
                raise QualityInputBuildError("website source contains a symlink")
            if name.casefold() not in _EXCLUDED_COMPONENTS:
                retained.append(name)
        directories[:] = retained
        for name in names:
            member = directory / name
            if member.is_symlink():
                raise QualityInputBuildError("website source contains a symlink")
            if not member.is_file():
                raise QualityInputBuildError("website source contains a non-file entry")
            yield member.relative_to(root)


def _eligible_website_file(relative: Path) -> bool:
    parts = tuple(part.casefold() for part in relative.parts)
    name = relative.name.casefold()
    if any(part in _EXCLUDED_COMPONENTS for part in parts):
        return False
    if any(marker in name for marker in _SENSITIVE_MARKERS):
        return False
    return relative.suffix.casefold() in _CODE_SUFFIXES


def _read_regular_bytes(path: Path) -> tuple[bytes, os.stat_result]:
    """Read one website file by descriptor and reject in-flight replacement."""

    initial = os.lstat(path)
    if not stat.S_ISREG(initial.st_mode):
        raise QualityInputBuildError("website source contains a non-regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
        ):
            raise QualityInputBuildError("website source changed before it could be read")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise QualityInputBuildError("website source changed while it was being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise QualityInputBuildError("website source changed while it was being read")
        final = os.fstat(descriptor)
        if (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
        ) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise QualityInputBuildError("website source changed while it was being read")
        final_path = os.lstat(path)
        if (
            final_path.st_dev,
            final_path.st_ino,
            final_path.st_size,
            final_path.st_mtime_ns,
        ) != (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns):
            raise QualityInputBuildError("website source path changed while it was being read")
        return b"".join(chunks), opened
    finally:
        os.close(descriptor)


def _copy_website_code(source: Path, destination: Path) -> list[dict[str, str]]:
    copied: list[dict[str, str]] = []
    for relative in sorted(_relative_files(source)):
        if not _eligible_website_file(relative):
            continue
        source_file = source / relative
        destination_file = destination / relative
        destination_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload, source_stat = _read_regular_bytes(source_file)
        _write_new(destination_file, payload)
        copied.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256(destination_file),
                "size": str(source_stat.st_size),
                "mode": f"{stat.S_IMODE(source_stat.st_mode):04o}",
            }
        )
    admin = destination / "admin" / "admin_server.py"
    if not admin.is_file():
        raise QualityInputBuildError("website code snapshot lacks admin/admin_server.py")
    _fsync_directory(destination)
    return copied


def _inert_inputs(root: Path) -> None:
    # These fixed bytes are safe fixtures, never read from an external source.
    rows = {
        "validation.env": VALIDATION_ENV_BYTES,
        "laf-config.json": VALIDATION_LAF_CONFIG_BYTES,
        "credentials.json": VALIDATION_GOOGLE_CREDENTIALS_BYTES,
        "google-calendar-token.json": VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES,
        "laf-gmail-token.pickle": VALIDATION_LAF_GMAIL_TOKEN_BYTES,
        "filereview-token.pickle": VALIDATION_LAF_GMAIL_TOKEN_BYTES,
        "accounting-credentials.json": VALIDATION_GOOGLE_CREDENTIALS_BYTES,
        "accounting-token.json": VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES,
        "drive-token.json": VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES,
        "drive-write-token.json": VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES,
    }
    for name, payload in rows.items():
        _write_new(root / name, payload)


def build_quality_inputs(
    *,
    output_root: Path,
    cron_source: Path,
    website_source: Path,
    release_root: Path,
    runtime_root: Path,
    python_runtime: Path,
    python_runtime_manifest: Path,
    python_runtime_tree_sha256: str,
    expected_job_count: int = 102,
) -> dict[str, Any]:
    """Build a one-time quality artifact without dereferencing secret inputs."""

    if expected_job_count < 1:
        raise QualityInputBuildError("expected job count must be positive")
    cron = _absolute_file(cron_source, label="cron source")
    website = _absolute_directory(website_source, label="website source")
    release = _absolute_directory(release_root, label="release root")
    python_declared = python_runtime.expanduser()
    if not python_declared.is_absolute():
        raise QualityInputBuildError("python runtime must be an absolute executable")
    python = _absolute_file(
        python_declared, label="python runtime", executable=True, allow_symlink=True
    )
    runtime_manifest = _absolute_file(
        python_runtime_manifest, label="python runtime manifest"
    )
    if len(python_runtime_tree_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in python_runtime_tree_sha256
    ):
        raise QualityInputBuildError("python runtime tree SHA-256 is invalid")
    runtime_manifest_sha_before = _sha256(runtime_manifest)
    output_raw = output_root.expanduser()
    if not output_raw.is_absolute() or output_raw.is_symlink() or output_raw.exists():
        raise QualityInputBuildError("quality input output root must be a new absolute path")
    output_parent = output_raw.parent.resolve(strict=True)
    output = output_parent / output_raw.name
    if _inside(output, release) or _inside(output, website):
        raise QualityInputBuildError("quality input output must not be inside an input tree")
    try:
        # The verifier rebuilds the runtime inventory and fails on any tree,
        # declared-path, realpath, or manifest drift.
        runtime_report = verify_runtime_manifest(
            runtime_manifest,
            expected_tree_sha256=python_runtime_tree_sha256,
            expected_python_runtime=python_declared,
            expected_python_realpath=python,
        )
    except (OSError, PythonRuntimeBlocked) as exc:
        raise QualityInputBuildError(f"python runtime binding failed: {exc}") from exc
    runtime_manifest_sha_after = _sha256(runtime_manifest)
    if (
        runtime_manifest_sha_before != runtime_manifest_sha_after
        or runtime_report.get("manifest_sha256") != runtime_manifest_sha_before
    ):
        raise QualityInputBuildError("python runtime manifest changed during verification")
    try:
        manifest_payload = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
        source_snapshot_sha256 = manifest_payload.get("source_snapshot_sha256")
        if not isinstance(source_snapshot_sha256, str):
            raise QualityInputBuildError("release manifest source snapshot is missing")
        release_bundle = verify_release_bundle(release, source_snapshot_sha256)
    except (OSError, UnicodeError, json.JSONDecodeError, CampaignSafetyError) as exc:
        raise QualityInputBuildError(f"release binding failed: {exc}") from exc
    try:
        cron, raw_cron_bytes, cron_identity = read_verified_cron_source(cron)
        raw_jobs = json.loads(raw_cron_bytes.decode("utf-8"))
    except (CronSnapshotBlocked, UnicodeError, json.JSONDecodeError) as exc:
        raise QualityInputBuildError("cron source is not valid JSON") from exc
    if not isinstance(raw_jobs, list) or len(raw_jobs) != expected_job_count:
        raise QualityInputBuildError("cron source job count does not match the required quality contract")
    try:
        transformed, cron_evidence = render_snapshot(
            source=cron,
            release_root=release,
            runtime_root=runtime_root.expanduser().resolve(strict=False),
            python_runtime=python_declared,
            source_bytes=raw_cron_bytes,
            source_identity=cron_identity,
        )
        transformed_jobs = json.loads(transformed.decode("utf-8"))
    except (CronSnapshotBlocked, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualityInputBuildError(f"release-bound cron rendering failed: {exc}") from exc
    if not isinstance(transformed_jobs, list) or len(transformed_jobs) != expected_job_count:
        raise QualityInputBuildError("transformed cron job count does not match the required quality contract")

    _new_directory(output)
    _write_new(output / "cron_jobs.json", raw_cron_bytes)
    _write_new(output / "cron_jobs.v3.json", transformed)
    if cron_evidence.get("source_sha256") != _sha256(output / "cron_jobs.json"):
        raise QualityInputBuildError("cron rendering and raw snapshot SHA-256 disagree")
    _inert_inputs(output)
    website_destination = output / "website"
    _new_directory(website_destination)
    copied_website = _copy_website_code(website, website_destination)
    admin = website_destination / "admin" / "admin_server.py"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "magi.v3.release-quality-inputs",
        "isolated_live_validation_compatible": False,
        "secret_source_read": False,
        "credential_artifacts": "fixed-inert-fixtures-only",
        "cron": {
            "raw_path": "cron_jobs.json",
            "raw_sha256": _sha256(output / "cron_jobs.json"),
            "transformed_path": "cron_jobs.v3.json",
            "transformed_sha256": _sha256(output / "cron_jobs.v3.json"),
            "job_count": expected_job_count,
            "render_evidence": cron_evidence,
        },
        "website": {
            "source_code_only": True,
            "admin_path": "website/admin/admin_server.py",
            "admin_sha256": _sha256(admin),
            "files": copied_website,
        },
        "runtime": {
            "python_runtime": str(python_declared),
            "python_runtime_realpath": str(python),
            "python_runtime_sha256": _sha256(python),
            "python_runtime_manifest": str(runtime_manifest),
            "python_runtime_manifest_sha256": runtime_manifest_sha_after,
            "python_runtime_tree_sha256": python_runtime_tree_sha256,
            "verification": runtime_report,
        },
        "release": {
            "release_id": release_bundle.release_id,
            "release_commit": release_bundle.commit,
            "release_manifest_sha256": release_bundle.manifest_sha256,
            "source_snapshot_sha256": release_bundle.source_snapshot_sha256,
            "source_file_count": len(release_bundle.files),
        },
    }
    _write_new(output / "quality-input-manifest.json", _canonical(manifest))
    _fsync_directory(output)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--cron-source", type=Path, required=True)
    parser.add_argument("--website-source", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--python-runtime-manifest", type=Path, required=True)
    parser.add_argument("--python-runtime-tree-sha256", required=True)
    parser.add_argument("--expected-job-count", type=int, default=102)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_quality_inputs(
            output_root=args.output_root,
            cron_source=args.cron_source,
            website_source=args.website_source,
            release_root=args.release_root,
            runtime_root=args.runtime_root,
            python_runtime=args.python_runtime,
            python_runtime_manifest=args.python_runtime_manifest,
            python_runtime_tree_sha256=args.python_runtime_tree_sha256,
            expected_job_count=args.expected_job_count,
        )
    except QualityInputBuildError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "manifest": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
