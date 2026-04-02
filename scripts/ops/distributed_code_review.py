#!/usr/bin/env python3
"""
Distributed Code Review Operator Script
=======================================
Orchestrates the distributed code review process using the Casper `ReviewSkill`.
Delegates logic to `skills.casper.code_review`.

Usage:
    python3 scripts/ops/distributed_code_review.py
"""

import os
import sys

# Add project root to path for module imports
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(project_root)

from skills.casper.code_review import review_skill

TARGET_DIR = os.path.expanduser("~/Desktop/MAGI_v2/skills")

if __name__ == "__main__":
    review_skill.run_review(TARGET_DIR)
