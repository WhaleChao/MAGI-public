#!/usr/bin/env python3
"""Operational hardening audit for MAGI.

Checks the items that basic /health cannot see: cron fallback compatibility,
cron time collisions, dirty worktree categories, and recent issue agenda
failures.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.platforms.safe_process import parse_cron_command, _validate_argv  # noqa: E402


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def audit_cron() -> dict[str, Any]:
    jobs = _load_json(ROOT / "cron_jobs.json", [])
    enabled = [j for j in jobs if j.get("enabled", True)]
    parse_failures = []
    collisions = []
    by_cron: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for job in enabled:
        by_cron[job.get("cron", "")].append(job)
        command = (job.get("command") or "").strip()
        if not command or command.startswith("@MAGI"):
            continue
        try:
            argv = parse_cron_command(command)
            _validate_argv(argv)
        except Exception as exc:
            parse_failures.append({
                "id": job.get("id"),
                "cron": job.get("cron"),
                "desc": job.get("desc"),
                "error": f"{type(exc).__name__}: {exc}",
                "command": command,
            })

    for cron, grouped in sorted(by_cron.items()):
        if len(grouped) <= 1:
            continue
        heavy = [
            j for j in grouped
            if not (j.get("command") or "").strip().startswith("@MAGI")
        ]
        if len(grouped) > 1 and heavy:
            collisions.append({
                "cron": cron,
                "jobs": [
                    {"id": j.get("id"), "desc": j.get("desc"), "command": j.get("command")}
                    for j in grouped
                ],
            })

    return {
        "enabled_count": len(enabled),
        "parse_failure_count": len(parse_failures),
        "parse_failures": parse_failures,
        "collision_count": len(collisions),
        "collisions": collisions,
    }


def audit_git() -> dict[str, Any]:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    generated_prefixes = (
        "?? static/worldmonitor_reports/",
        " D static/worldmonitor_reports/",
        "?? cron_jobs.json.bak.",
        "?? .claude/worktrees/",
    )
    generated = [line for line in lines if line.startswith(generated_prefixes)]
    source = [line for line in lines if line not in generated]
    return {
        "dirty_count": len(lines),
        "source_or_review_count": len(source),
        "generated_or_runtime_count": len(generated),
        "source_or_review": source,
        "generated_or_runtime": generated[:80],
    }


def audit_issue_agenda(limit: int = 20) -> dict[str, Any]:
    path = ROOT / ".runtime" / "issue_agenda.jsonl"
    if not path.exists():
        return {"exists": False, "recent": []}
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return {
        "exists": True,
        "recent_count": len(rows),
        "recent": [
            {
                "iso": r.get("iso"),
                "command": r.get("command"),
                "severity": r.get("severity"),
                "error": (r.get("error") or "")[:500],
            }
            for r in rows
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", default=str(ROOT / ".runtime" / "operational_hardening_audit_latest.json"))
    parser.add_argument("--fail-on-red", action="store_true")
    args = parser.parse_args()

    report = {
        "cron": audit_cron(),
        "git": audit_git(),
        "issue_agenda": audit_issue_agenda(),
    }
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "cron_parse_failures": report["cron"]["parse_failure_count"],
        "cron_collisions": report["cron"]["collision_count"],
        "dirty_count": report["git"]["dirty_count"],
        "recent_issues": report["issue_agenda"]["recent_count"],
        "json_out": str(out),
    }, ensure_ascii=False))

    if args.fail_on_red and (
        report["cron"]["parse_failure_count"] > 0
        or report["cron"]["collision_count"] > 0
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
