from __future__ import annotations

import shlex
from pathlib import Path

from scripts import seed_cron_jobs


def test_canonicalize_job_command_quotes_runtime_root_with_spaces():
    runtime_root = Path("/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2")
    raw = (
        "/Users/ai/Desktop/MAGI_v2/venv/bin/python3 "
        "/Users/ai/Desktop/MAGI_v2/scripts/ops/audit_operational_hardening.py "
        "--json-out /Users/ai/Desktop/MAGI_v2/.runtime/operational_hardening_audit_latest.json"
    )

    job, changed = seed_cron_jobs.canonicalize_job_command({"command": raw}, runtime_root)

    assert changed is True
    parts = shlex.split(job["command"])
    assert parts[0] == str(runtime_root / "venv" / "bin" / "python3")
    assert parts[1] == str(runtime_root / "scripts" / "ops" / "audit_operational_hardening.py")
    assert parts[-1] == str(runtime_root / ".runtime" / "operational_hardening_audit_latest.json")
