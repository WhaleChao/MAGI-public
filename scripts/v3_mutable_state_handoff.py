#!/usr/bin/env python3
"""Dry-run or prepare the exact V2-to-V3 mutable-state handoff."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from magi_v3.mutable_state_handoff import (
    ExactContext,
    MutableStateHandoffError,
    execute_handoff,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy only the fixed MAGI mutable-state allowlist; never walks a directory."
    )
    parser.add_argument("action", choices=("dry-run", "prepare"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target-shared-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("--deployment-manifest-sha256", required=True)
    parser.add_argument("--cutover-plan-sha256", required=True)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--expected-target-snapshot-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload, receipt_sha256 = execute_handoff(
            action=args.action,
            source_root=args.source_root,
            target_shared_root=args.target_shared_root,
            receipt_path=args.receipt,
            staging_root=args.staging_root,
            context=ExactContext(
                release_id=args.release_id,
                release_manifest_sha256=args.release_manifest_sha256,
                deployment_manifest_sha256=args.deployment_manifest_sha256,
                cutover_plan_sha256=args.cutover_plan_sha256,
            ),
            refresh=args.refresh,
            expected_target_snapshot_sha256=args.expected_target_snapshot_sha256,
        )
    except MutableStateHandoffError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "status": payload["status"],
                "ready": payload["ready"],
                "degraded": payload["degraded"],
                "receipt_sha256": receipt_sha256,
                "target_before_snapshot_sha256": payload["target_before_snapshot_sha256"],
                "target_snapshot_sha256": payload["target_snapshot_sha256"],
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
