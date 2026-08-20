#!/usr/bin/env python3
"""Build rc606's tracked canonical cron source from the rc605 receipt.

Only the all-files Drive job is replaced.  The builder refuses a changed rc605
base, ambiguous job identity, or a mismatch with the rc606 seed contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.seed_cron_jobs import business_jobs
from scripts.v3_campaign.schedule_realism import (
    _command_definition_sha256,
    _logical_definition_sha256,
)
from scripts.v3_schedule_baseline_capture import _source_evidence_receipt_sha256


RC605_SHA256 = "376e5bc559a8e3148eeec1db9f5a3bc7d138c3fecfe6d0d5063490a994696ffa"
JOB_ID = "job_drive_case_sync_all_files"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _rc606_command(command: str) -> str:
    replacements = {
        "--direct-all-case-limit 4": "--direct-all-case-limit 1 --all-case-chunk-size 1",
        "--inventory-timeout-sec 5400": "--inventory-timeout-sec 5400 --terminal-headroom-sec 300",
    }
    for old, new in replacements.items():
        if command.count(old) != 1:
            raise RuntimeError(f"unexpected rc605 all-files command contract: {old}")
        command = command.replace(old, new)
    if command.count("--timeout-sec 6000") != 1:
        raise RuntimeError("resource timeout contract changed")
    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    root = args.root.resolve()
    if args.base.is_symlink() or not args.base.is_file() or _sha(args.base) != RC605_SHA256:
        raise RuntimeError("rc605 canonical cron receipt is unavailable or changed")
    jobs = json.loads(args.base.read_text(encoding="utf-8"))
    if not isinstance(jobs, list):
        raise RuntimeError("cron base must be a job list")
    matched = [item for item in jobs if isinstance(item, dict) and item.get("id") == JOB_ID]
    if len(matched) != 1:
        raise RuntimeError("all-files job identity is ambiguous")
    seeded = next(item for item in business_jobs(root, root / "venv/bin/python3") if item.get("id") == JOB_ID)
    if seeded.get("timeout_sec") != 6300 or "--timeout-sec 6000" not in str(seeded.get("command")):
        raise RuntimeError("rc606 seed timeout contract changed")
    row = matched[0]
    row["command"] = _rc606_command(str(row.get("command") or ""))
    row["timeout_sec"] = 6300
    row["desc"] = str(seeded.get("desc") or "")
    output = root / "cron_jobs.json"
    _write_json(output, jobs)
    cron_sha = _sha(output)

    baseline_path = root / "config" / "v3_schedule_realism_baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    evidence = baseline["source_evidence"]
    evidence["job_definitions_sha256"] = cron_sha
    evidence["logical_definition_sha256"] = _logical_definition_sha256(jobs)
    invalidated = [
        item
        for item in baseline.get("invalidated_observations", [])
        if isinstance(item, dict) and item.get("job_id") == JOB_ID
    ]
    if len(invalidated) != 1:
        raise RuntimeError("all-files invalidation evidence is ambiguous")
    invalidated[0]["current_command_sha256"] = _command_definition_sha256(row)
    evidence["runtime_source_evidence_receipt_sha256"] = _source_evidence_receipt_sha256(evidence)
    _write_json(baseline_path, baseline)
    baseline_sha = _sha(baseline_path)

    policy_path = root / "config" / "v3_schedule_dispatch_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["cron_jobs_sha256"] = cron_sha
    _write_json(policy_path, policy)

    registry_path = root / "config" / "v3_schedule_body_adapter_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    binding = registry["release_binding"]
    binding["cron_jobs_source_sha256"] = cron_sha
    binding["logical_definition_sha256"] = evidence["logical_definition_sha256"]
    binding["inherited_baseline_sha256"] = baseline_sha
    _write_json(registry_path, registry)

    subprocess.run(
        [
            "python3",
            str(root / "scripts" / "architecture" / "generate_v2_inventory.py"),
            "--root",
            str(root),
            "--output",
            str(root / "docs" / "architecture" / "v3" / "generated" / "v2_inventory.json"),
        ],
        check=True,
    )
    print(json.dumps({
        "status": "passed", "cron_jobs_sha256": cron_sha,
        "baseline_sha256": baseline_sha, "changed_jobs": [JOB_ID],
        "other_jobs_changed": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
