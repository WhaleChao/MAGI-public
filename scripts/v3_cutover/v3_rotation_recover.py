#!/usr/bin/env python3
"""Explicit emergency recovery for an interrupted V3-to-V3 rotation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v3_cutover.core import CutoverError
from scripts.v3_cutover.v3_rotation_execute import recover_previous_from_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollback-deploy", type=Path, required=True)
    parser.add_argument("--state-parent", type=Path, required=True)
    parser.add_argument("--launchagents-directory", type=Path, required=True)
    parser.add_argument("--rollback-snapshot-directory", type=Path, required=True)
    parser.add_argument("--snapshot-manifest-sha256", required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = recover_previous_from_snapshot(
            rollback_deploy_root=args.rollback_deploy,
            state_parent=args.state_parent,
            launchagents_directory=args.launchagents_directory,
            rollback_snapshot_directory=args.rollback_snapshot_directory,
            expected_snapshot_manifest_sha256=args.snapshot_manifest_sha256,
            report_output=args.report_output,
        )
    except (CutoverError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
