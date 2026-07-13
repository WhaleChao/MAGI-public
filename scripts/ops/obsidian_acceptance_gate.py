#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Obsidian knowledge factory acceptance gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

DEFAULT_JSON_OUT = MAGI_ROOT / ".runtime" / "obsidian_acceptance_latest.json"


def _status_from_result(result: Dict[str, Any]) -> str:
    status = str(result.get("status") or "").lower()
    if status in {"error", "fail"}:
        return "fail"
    if status in {"warn", "skip"}:
        return "warn"
    return "pass"


def _check(name: str, ok: bool, status: str, detail: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        "status": status,
        "detail": detail,
        "payload": payload or {},
    }


def run_gate() -> Dict[str, Any]:
    from skills.obsidian import action as obsidian_action
    from scripts import knowledge_lint

    checks: List[Dict[str, Any]] = []

    status = obsidian_action.task_status()
    checks.append(
        _check(
            "vault_config",
            bool(status.get("vault_configured")),
            "pass" if status.get("vault_configured") else "fail",
            f"vault={status.get('vault_path') or 'not configured'} notes_on_disk={status.get('notes_on_disk', 0)} indexed={status.get('notes_indexed', 0)}",
            status,
        )
    )

    summary_quality = knowledge_lint.check_obsidian_summary_quality()
    bad = int(summary_quality.get("bad_notes", 0) or 0)
    total = int(summary_quality.get("total_notes", 0) or 0)
    checks.append(
        _check(
            "summary_quality",
            bad == 0 and summary_quality.get("status") == "ok",
            _status_from_result(summary_quality),
            f"bad_notes={bad}/{total} issue_count={summary_quality.get('issue_count', 0)}",
            summary_quality,
        )
    )

    duplicate_plan = obsidian_action.task_cleanup_duplicate_notes(dry_run=True)
    planned = int(duplicate_plan.get("planned_moves", 0) or 0)
    checks.append(
        _check(
            "duplicate_notes",
            planned == 0 and duplicate_plan.get("success") is True,
            "pass" if planned == 0 and duplicate_plan.get("success") is True else "warn",
            f"duplicate_groups={duplicate_plan.get('duplicate_groups', 0)} planned_moves={planned}",
            duplicate_plan,
        )
    )

    wiki = knowledge_lint.check_wiki_staleness()
    stale = int(wiki.get("stale_cases", 0) or 0)
    checks.append(
        _check(
            "wiki_required_pages",
            stale == 0 and wiki.get("status") in {"ok", "skip"},
            _status_from_result(wiki),
            f"stale_or_missing={stale}",
            wiki,
        )
    )

    orphan = knowledge_lint.check_orphan_notes()
    checks.append(
        _check(
            "index_alignment",
            orphan.get("status") in {"ok", "skip"},
            _status_from_result(orphan),
            (
                f"unindexed={orphan.get('unindexed', 0)} "
                f"orphaned={orphan.get('orphaned_index_entries', 0)} "
                f"zero_chunks={orphan.get('zero_chunk_notes', 0)}"
            ),
            orphan,
        )
    )

    fail = sum(1 for c in checks if c["status"] == "fail")
    warn = sum(1 for c in checks if c["status"] == "warn")
    overall = "RED" if fail else ("YELLOW" if warn else "GREEN")
    return {
        "ok": overall != "RED",
        "status": overall,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "agent_dir": str(obsidian_action.AGENT_DIR),
        "summary": {
            "pass": sum(1 for c in checks if c["status"] == "pass"),
            "warn": warn,
            "fail": fail,
        },
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Obsidian knowledge acceptance gate.")
    parser.add_argument("--json-out", type=str, default=str(DEFAULT_JSON_OUT))
    args = parser.parse_args()

    report = run_gate()
    out = Path(args.json_out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] != "RED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
