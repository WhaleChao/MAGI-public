#!/usr/bin/env python3
import json
import os
import shlex
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from skills.ops.cron_scheduler import CronScheduler


def main():
    scheduler = CronScheduler()
    root = Path(__file__).resolve().parents[2]
    python_bin = root / "venv" / "bin" / "python3"
    command = " ".join(
        shlex.quote(str(part))
        for part in (python_bin, root / "scripts" / "ops" / "run_auto_skill_import.py")
    )
    result = scheduler.ensure_job(
        cron_expr="15 1 * * *",
        command=command,
        description="Daily Auto-Skill Import + DC Summary",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
