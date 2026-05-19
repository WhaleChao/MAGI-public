#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a command only when MAGI has enough local resources.

This is a non-destructive guard for cron jobs. It does not touch databases,
case folders, NAS data, user documents, or model files. When the machine is in
throttle/core-only/critical mode, it can skip non-core heavy jobs and record the
decision as operational telemetry instead of letting cron start work that may
fill disk or push the Mac into swap.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.platforms import runtime_dir  # noqa: E402
from scripts.ops import resource_governor  # noqa: E402


LEVEL_RANK = {"normal": 0, "throttle": 1, "core_only": 2, "critical": 3}


def _append_event(payload: dict[str, Any]) -> None:
    runtime_dir.atomic_append_jsonl(
        runtime_dir.root() / "resource_guarded_run.jsonl",
        payload,
        rotate_at=500,
        keep_tail=300,
    )


def _strip_separator(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def _should_block(
    decision: resource_governor.ResourceDecision,
    *,
    block_at: str,
    require_disk_free_gb: float | None,
    require_free_inactive_gb: float | None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if LEVEL_RANK[decision.level] >= LEVEL_RANK[block_at]:
        reasons.append(f"resource_level>={block_at}:{decision.level}")
    if require_disk_free_gb is not None and decision.snapshot.disk_free_gb < require_disk_free_gb:
        reasons.append(
            f"disk_free<{require_disk_free_gb:g}GB:{decision.snapshot.disk_free_gb:g}GB"
        )
    if (
        require_free_inactive_gb is not None
        and decision.snapshot.free_plus_inactive_gb < require_free_inactive_gb
    ):
        reasons.append(
            "free_plus_inactive"
            f"<{require_free_inactive_gb:g}GB:{decision.snapshot.free_plus_inactive_gb:g}GB"
        )
    return bool(reasons), reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guard cron commands by MAGI resource level.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--block-at",
        choices=sorted(LEVEL_RANK, key=LEVEL_RANK.get),
        default="core_only",
        help="Skip command when resource level is this level or worse.",
    )
    parser.add_argument("--require-disk-free-gb", type=float)
    parser.add_argument("--require-free-inactive-gb", type=float)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = _strip_separator(args.command)
    if not command:
        parser.error("missing command after --")

    decision = resource_governor.classify(resource_governor.collect_snapshot())
    blocked, block_reasons = _should_block(
        decision,
        block_at=args.block_at,
        require_disk_free_gb=args.require_disk_free_gb,
        require_free_inactive_gb=args.require_free_inactive_gb,
    )
    event: dict[str, Any] = {
        "ts": time.time(),
        "job_id": args.job_id,
        "block_at": args.block_at,
        "command": command,
        "decision": asdict(decision),
        "blocked": blocked,
        "block_reasons": block_reasons,
    }

    if blocked:
        event["returncode"] = 0
        _append_event(event)
        message = (
            f"MAGI resource guard skipped {args.job_id}: "
            + ", ".join(block_reasons)
        )
        if args.json:
            print(json.dumps(event, ensure_ascii=False, indent=2))
        else:
            print(message)
        return 0

    proc = subprocess.run(command, check=False)
    event["returncode"] = int(proc.returncode)
    _append_event(event)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
