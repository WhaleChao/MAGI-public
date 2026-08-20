#!/usr/bin/env python3
"""Build and statically verify one hash-bound isolated LIVE execution plan.

This builder is deliberately non-mutating: it neither invokes launchctl nor
imports the macOS host adapter.  It will only publish a plan after the existing
isolated LIVE verifier proves that the immutable release, prepared deployment,
fresh 19/19 offline-machine gate, and inert validation fixtures agree exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.v3_validation.isolated_live_execute import (
    DEPLOYMENT_MODE,
    IsolatedLiveBlocked,
    load_isolated_live_plan,
    verify_static_plan,
)


PLAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_PROBES: tuple[dict[str, str], ...] = tuple(
    {"method": "GET", "url": url}
    for url in (
        "http://127.0.0.1:5002/livez",
        "http://127.0.0.1:5002/readyz",
        "http://127.0.0.1:5002/validation/ping",
        "http://127.0.0.1:5003/livez",
        "http://127.0.0.1:5003/readyz",
        "http://127.0.0.1:5003/validation/ping",
        "http://127.0.0.1:5003/validation/osc/document-preview",
        "http://127.0.0.1:5003/validation/osc/document-download",
        "http://127.0.0.1:8088/health",
    )
)


class IsolatedLivePlanBlocked(IsolatedLiveBlocked):
    """A plan could not be safely and completely prepared."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_regular(path: Path, *, description: str) -> tuple[Path, bytes]:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise IsolatedLivePlanBlocked(
            f"{description} must be a canonical absolute non-symlink file"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(raw, flags)
    except OSError as exc:
        raise IsolatedLivePlanBlocked(f"{description} is unavailable: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise IsolatedLivePlanBlocked(
                f"{description} must be a regular file with one hard link"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read()
        after = os.fstat(descriptor)
        current = raw.lstat()
        signature = lambda row: (
            row.st_dev,
            row.st_ino,
            row.st_size,
            row.st_mtime_ns,
            row.st_nlink,
        )
        if signature(before) != signature(after) or signature(after) != signature(current):
            raise IsolatedLivePlanBlocked(f"{description} changed while it was read")
        return raw, data
    finally:
        os.close(descriptor)


def _binding(path: Path, *, description: str) -> dict[str, str]:
    canonical, data = _stable_regular(path, description=description)
    return {"path": str(canonical), "sha256": _sha256_bytes(data)}


def _token_digest(path: Path) -> str:
    canonical, data = _stable_regular(path, description="one-time token")
    metadata = canonical.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
        raise IsolatedLivePlanBlocked("one-time token must be owner-only mode 0600")
    if len(data) > 4096:
        raise IsolatedLivePlanBlocked("one-time token is too large")
    token = data.rstrip(b"\r\n")
    if not token:
        raise IsolatedLivePlanBlocked("one-time token is empty")
    return _sha256_bytes(token)


def _target(path: Path) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise IsolatedLivePlanBlocked(
            "plan output must be a canonical absolute non-symlink path"
        )
    if raw.exists():
        raise IsolatedLivePlanBlocked("plan output already exists")
    try:
        raw.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = raw.parent.resolve(strict=True)
    except OSError as exc:
        raise IsolatedLivePlanBlocked(f"plan output parent is unavailable: {exc}") from exc
    if parent != raw.parent or parent.is_symlink() or not parent.is_dir():
        raise IsolatedLivePlanBlocked(
            "plan output parent must be a canonical non-symlink directory"
        )
    return raw


def _write_exclusive(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def create_isolated_live_plan(
    *,
    plan_id: str,
    output: Path,
    token_file: Path,
    release_manifest: Path,
    deploy_manifest: Path,
    deploy_prepared_marker: Path,
    offline_gate_report: Path,
    probes: Sequence[Mapping[str, Any]] = DEFAULT_PROBES,
) -> dict[str, str]:
    """Publish one immutable-input plan after full static verification."""

    if not PLAN_ID_RE.fullmatch(plan_id):
        raise IsolatedLivePlanBlocked("plan_id is invalid")
    target = _target(output)
    payload = {
        "schema_version": 1,
        "plan_id": plan_id,
        "operation": DEPLOYMENT_MODE,
        "release_manifest": _binding(
            release_manifest, description="release manifest"
        ),
        "deploy_manifest": _binding(deploy_manifest, description="deploy manifest"),
        "deploy_prepared_marker": _binding(
            deploy_prepared_marker, description="deploy prepared marker"
        ),
        "offline_gate_report": _binding(
            offline_gate_report, description="offline machine gate report"
        ),
        "token_sha256": _token_digest(token_file),
        "probes": [dict(row) for row in probes],
    }
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    digest = _sha256_bytes(data)
    temporary = target.parent / f".{target.name}.staging-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        _write_exclusive(temporary, data)
        # This is the same deep verifier used immediately before a real LIVE
        # handoff.  It also proves the 19/19 report is fresh and candidate-bound.
        verified = verify_static_plan(load_isolated_live_plan(temporary, digest))
        if target.exists() or target.is_symlink():
            raise IsolatedLivePlanBlocked("plan output appeared during verification")
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "plan_id": plan_id,
        "plan_path": str(target),
        "plan_sha256": digest,
        "release_id": verified.release_id,
        "deployment_mode": DEPLOYMENT_MODE,
        "status": "prepared_not_executed",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build and statically verify a non-mutating, hash-bound isolated LIVE plan."
        )
    )
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--deploy-manifest", type=Path, required=True)
    parser.add_argument("--deploy-prepared-marker", type=Path, required=True)
    parser.add_argument("--offline-gate-report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = create_isolated_live_plan(
            plan_id=args.plan_id,
            output=args.output,
            token_file=args.token_file,
            release_manifest=args.release_manifest,
            deploy_manifest=args.deploy_manifest,
            deploy_prepared_marker=args.deploy_prepared_marker,
            offline_gate_report=args.offline_gate_report,
        )
    except IsolatedLiveBlocked as exc:
        raise SystemExit(f"isolated LIVE plan blocked: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_PROBES",
    "IsolatedLivePlanBlocked",
    "create_isolated_live_plan",
    "main",
]
