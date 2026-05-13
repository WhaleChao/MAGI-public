#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime

MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)

from api.runtime_paths import get_magi_root_dir
from skills.management.auto_skill import AutoSkill
from skills.management.code_autofix import autofix_codebase
from skills.bridge.melchior_manager import sync_skills_to_melchior

MAGI_ROOT = str(get_magi_root_dir())


def run_cycle() -> dict:
    started = datetime.now().isoformat()
    autofix = autofix_codebase(
        target="magi",
        max_files=120,
        max_rounds=2,
        dry_run=False,
        include_tests=False,
        task_hint="scheduled code hygiene and internalization",
        internalize_skill=True,
        internalize_name="casper-autofix-knowledge",
    )
    autoskill = AutoSkill()
    code_skill = autoskill.internalize_codebase_as_skills(
        source_dir=MAGI_ROOT,
        max_files=100,
        force=False,
        auto_activate=True,
        enable_release=True,
        canary_percent=20,
        promote_min_runs=12,
        promote_max_failure_rate=0.2,
    )

    # Remote enhancement: keep Melchior skills in sync so it can execute latest tools.
    melchior_sync = sync_skills_to_melchior(f"{MAGI_ROOT}/skills")
    return {
        "success": bool(autofix.get("success")) and bool(code_skill.get("success")),
        "started_at": started,
        "finished_at": datetime.now().isoformat(),
        "autofix": autofix,
        "code_internalization": code_skill,
        "melchior_skill_sync": melchior_sync,
    }


if __name__ == "__main__":
    print(json.dumps(run_cycle(), ensure_ascii=False, indent=2))
