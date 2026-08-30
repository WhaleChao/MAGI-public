#!/usr/bin/env python3
"""Exec the real V2/V3 production entrypoint for a sealed resource arm.

This file is deliberately only an ``execve`` trampoline.  It does not build a
Flask blueprint, instantiate a gateway/control object, or emulate a service.
The collector binds this source and every target entrypoint to the immutable
release manifest, supplies an isolated production-shaped environment, and
applies the same Seatbelt profile to this process and all descendants.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_files(release_root: Path) -> dict[str, str]:
    manifest = release_root / "release-manifest.json"
    expected = os.environ.get("MAGI_V3_RELEASE_MANIFEST_SHA256", "")
    if _sha256(manifest) != expected:
        raise RuntimeError("resource arm release manifest hash mismatch")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    rows = payload.get("files")
    if not isinstance(rows, list):
        raise RuntimeError("resource arm release inventory is missing")
    return {
        str(row.get("path")): str(row.get("sha256"))
        for row in rows
        if isinstance(row, dict)
    }


def _member(release_root: Path, files: dict[str, str], relative: str) -> Path:
    path = (release_root / relative).resolve(strict=True)
    if not path.is_relative_to(release_root) or files.get(relative) != _sha256(path):
        raise RuntimeError(f"production entrypoint is not release-bound: {relative}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("v2", "v3"), required=True)
    parser.add_argument(
        "--role", choices=("application", "control", "gateway", "supervisor"), required=True
    )
    parser.add_argument("--release-root", type=Path, required=True)
    args = parser.parse_args()
    release_root = args.release_root.resolve(strict=True)
    if release_root != Path(__file__).resolve().parents[2]:
        raise SystemExit("adapter and requested release root differ")
    files = _manifest_files(release_root)
    if args.arm == "v2":
        if args.role != "application":
            raise SystemExit("V2 production composition has one application role")
        target = _member(release_root, files, "scripts/ops/run_daemon_no_site.py")
        _member(release_root, files, "daemon.py")
        argv = [sys.executable, str(target)]
    else:
        modules = {
            "control": ("magi_v3.control", "magi_v3/control.py"),
            "gateway": ("magi_v3.gateway", "magi_v3/gateway.py"),
            "supervisor": (
                "magi_v3.supervisor_service",
                "magi_v3/supervisor_service.py",
            ),
        }
        if args.role not in modules:
            raise SystemExit("V3 production role is invalid")
        module, relative = modules[args.role]
        _member(release_root, files, relative)
        runtime_root = Path(
            os.environ["MAGI_V3_RESOURCE_RUNTIME_ROOT"]
        ).resolve(strict=True)
        ownership_path = Path(os.environ["MAGI_V3_OWNERSHIP_MANIFEST"]).resolve(
            strict=True
        )
        ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        rows = ownership.get("roles")
        binding = next(
            (
                row
                for row in rows
                if isinstance(row, dict) and row.get("role") == args.role
            ),
            None,
        ) if isinstance(rows, list) else None
        if not isinstance(binding, dict):
            raise SystemExit("resource ownership manifest lacks requested role")
        state_dir = runtime_root / "state" / args.role
        state_dir.mkdir(parents=True, exist_ok=True)
        (runtime_root / "pids").mkdir(parents=True, exist_ok=True)
        os.environ.update(
            MAGI_V3_ROLE=args.role,
            MAGI_V3_STATE_DIR=str(state_dir),
            MAGI_V3_PID_FILE=str(runtime_root / "pids" / f"{args.role}.pid"),
            MAGI_V3_PORTS=",".join(str(port) for port in binding.get("ports", [])),
            MAGI_V3_OWNERSHIP_DOMAINS=",".join(
                str(value) for value in binding.get("ownership_domains", [])
            ),
        )
        argv = [sys.executable, "-m", module]
    os.chdir(release_root)
    os.execve(sys.executable, argv, dict(os.environ))
    raise AssertionError("execve returned")


if __name__ == "__main__":
    raise SystemExit(main())
