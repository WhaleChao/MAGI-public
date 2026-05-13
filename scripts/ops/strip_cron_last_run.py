#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-time: 把 cron_jobs.json 裡的 last_run / last_run_minute 清乾淨，
並把最後一次狀態寫到 .runtime/cron_state.json。

用法：
  python3 scripts/ops/strip_cron_last_run.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CRON_FILE = REPO / "cron_jobs.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not CRON_FILE.exists():
        print(f"❌ 找不到 {CRON_FILE}")
        return 2

    jobs = json.loads(CRON_FILE.read_text(encoding="utf-8"))
    if not isinstance(jobs, list):
        print("❌ cron_jobs.json 不是 list")
        return 2

    state = {}
    stripped = 0
    for j in jobs:
        if not isinstance(j, dict):
            continue
        jid = j.get("id")
        last_run = j.get("last_run")
        last_minute = j.get("last_run_minute")
        if jid and (last_run or last_minute):
            state[jid] = {"last_run": last_run, "last_run_minute": last_minute}
            stripped += 1
        j["last_run"] = None
        j["last_run_minute"] = None

    print(f"將清除 {stripped} 筆 last_run，遷移到 cron_state.json")
    if args.dry_run:
        print("--dry-run：不寫檔")
        return 0

    # 寫 cron_state.json
    os.environ.setdefault("MAGI_USE_RUNTIME_DIR", "1")
    from api.platforms import runtime_dir as rd
    rd.atomic_write_json(rd.cron_state(), state)

    # 寫回 cron_jobs.json（last_run 清 None）
    tmp = CRON_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CRON_FILE)

    print(f"✅ 已寫入 {rd.cron_state()}")
    print(f"✅ 已更新 {CRON_FILE}（last_run 已清為 None）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
