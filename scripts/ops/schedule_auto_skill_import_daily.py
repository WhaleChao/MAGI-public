#!/usr/bin/env python3
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from skills.ops.cron_scheduler import CronScheduler


def main():
    scheduler = CronScheduler()
    result = scheduler.ensure_job(
        cron_expr="15 1 * * *",
        command="@MAGI 導入auto-skill",
        description="Daily Auto-Skill Import + DC Summary",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
