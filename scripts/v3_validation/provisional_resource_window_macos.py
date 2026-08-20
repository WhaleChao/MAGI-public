#!/usr/bin/env python3
"""Execute the provisional 16/19 resource window on the verified macOS host.

This is the supported bridge between the immutable outer/inner plans and the
real macOS host adapter.  It never installs or starts production V3.  The
outer executor stops V2, proves zero ownership, runs the disposable collector,
and restores the exact captured V2/model-host state in ``finally``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v3_validation.isolated_live_execute import (
    BoundArtifact,
    IsolatedLiveBlocked,
    IsolatedLivePlan,
    verify_static_plan,
)
from scripts.v3_validation.isolated_live_macos import MacOSIsolatedLiveMachine
from scripts.v3_validation.provisional_resource_window_execute import (
    ProvisionalResourceWindowExecutor,
    verify_plan,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bound(path: Path, expected: str, description: str) -> BoundArtifact:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=True) != raw:
        raise IsolatedLiveBlocked(f"{description} path is not canonical absolute")
    metadata = raw.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o222
        or _sha256(raw) != expected
    ):
        raise IsolatedLiveBlocked(f"{description} is mutable or hash-mismatched")
    return BoundArtifact(raw, expected)


def _write_new(path: Path, payload: Mapping[str, Any]) -> str:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.exists():
        raise IsolatedLiveBlocked("resource-window report output is unsafe or already exists")
    data = (
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    raw.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        raw,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return hashlib.sha256(data).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outer-plan", type=Path, required=True)
    parser.add_argument("--outer-plan-sha256", required=True)
    parser.add_argument("--outer-token", type=Path, required=True)
    parser.add_argument("--inner-plan", type=Path, required=True)
    parser.add_argument("--inner-plan-sha256", required=True)
    parser.add_argument("--inner-token", type=Path, required=True)
    parser.add_argument("--deploy-manifest", type=Path, required=True)
    parser.add_argument("--deploy-manifest-sha256", required=True)
    parser.add_argument("--deploy-prepared-marker", type=Path, required=True)
    parser.add_argument("--deploy-prepared-marker-sha256", required=True)
    parser.add_argument("--artifact-directory", type=Path, required=True)
    parser.add_argument("--collector-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    execution_started = False
    try:
        outer, _inner = verify_plan(
            args.outer_plan,
            args.outer_plan_sha256,
            args.inner_plan,
            args.inner_plan_sha256,
        )
        release_raw = outer["release_manifest"]
        gate_raw = outer["provisional_gate_report"]
        release = _bound(
            Path(release_raw["path"]), release_raw["sha256"], "release manifest"
        )
        gate = _bound(
            Path(gate_raw["path"]), gate_raw["sha256"], "provisional gate"
        )
        provisional_plan = IsolatedLivePlan(
            plan_id="provisional-resource-window",
            plan_sha256=args.outer_plan_sha256,
            release_manifest=release,
            deploy_manifest=_bound(
                args.deploy_manifest,
                args.deploy_manifest_sha256,
                "deploy manifest",
            ),
            deploy_prepared_marker=_bound(
                args.deploy_prepared_marker,
                args.deploy_prepared_marker_sha256,
                "deploy prepared marker",
            ),
            offline_gate_report=gate,
            token_sha256="0" * 64,
            probes=(),
        )
        # The outer 16/19 gate was verified above.  Only the circular resource
        # gates are absent, so deployment integrity can now be checked without
        # pretending that ordinary isolated LIVE is 19/19 eligible.
        deployment = verify_static_plan(
            provisional_plan, require_offline_machine_gate=False
        )
        machine = MacOSIsolatedLiveMachine(
            deployment,
            artifact_directory=args.artifact_directory,
        )
        execution_started = True
        report = ProvisionalResourceWindowExecutor(
            outer_plan_path=args.outer_plan,
            outer_plan_sha256=args.outer_plan_sha256,
            inner_plan_path=args.inner_plan,
            inner_plan_sha256=args.inner_plan_sha256,
            outer_token_file=args.outer_token,
            inner_token_file=args.inner_token,
            collector_output=args.collector_output,
            machine=machine,
        ).execute()
        report_sha256 = _write_new(args.report_output, report)
        print(
            json.dumps(
                {
                    "status": report.get("status"),
                    "ok": report.get("ok"),
                    "v2_restored": report.get("v2_restored"),
                    "report_output": str(args.report_output),
                    "report_sha256": report_sha256,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if report.get("ok") is True else 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "mutation_performed": execution_started,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
