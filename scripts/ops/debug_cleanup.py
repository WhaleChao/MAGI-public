#!/usr/bin/env python3
"""Clean old debug capture files for the MAGI cron scheduler."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Cron 環境執行時 cwd 與 sys.path 不一定包含專案根目錄
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.debug_capture import cleanup_old


def main() -> int:
    adapter = os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") or ""
    if adapter:
        fixture_root = Path(os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT") or "")
        if not (
            adapter == "real_entrypoint_dry_run_v1"
            and os.environ.get("MAGI_V3_SCHEDULE_DRY_RUN") == "1"
            and os.environ.get("MAGI_V3_SCHEDULE_NO_NETWORK") == "1"
            and os.environ.get("MAGI_V3_SCHEDULE_NO_NOTIFY") == "1"
            and (fixture_root / ".magi-v3-schedule-fixture").is_file()
        ):
            raise SystemExit("invalid schedule realism adapter")
        captures = fixture_root / "debug-captures"
        candidates = sorted(path for path in captures.glob("*") if path.is_file())
        print(json.dumps({
            "success": True,
            "adapter": adapter,
            "dry_run": True,
            "candidate_count": len(candidates),
            "deleted_count": 0,
            "network_attempted": False,
            "notification_attempted": False,
        }, sort_keys=True))
        return 0
    cleaned = cleanup_old(48)
    print(f"cleaned {cleaned} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
