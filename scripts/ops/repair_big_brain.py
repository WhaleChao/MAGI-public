#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.brain_manager import action as brain_manager


def main() -> int:
    ap = argparse.ArgumentParser(description="One-click Big Brain repair (distributed)")
    ap.add_argument("--model", default="", help="override target model (default: MAGI_MAIN_MODEL)")
    ap.add_argument("--timeout", type=int, default=240, help="max repair time in seconds")
    args = ap.parse_args()

    model = (args.model or os.environ.get("MAGI_MAIN_MODEL") or "").strip()
    timeout = max(30, int(args.timeout))

    out = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "timeout_sec": timeout,
        "before": {
            "mode": brain_manager.get_brain_mode(),
            "runtime": brain_manager.get_melchior_runtime_status(),
        },
    }

    repair = brain_manager.repair_big_brain(model=model, timeout_sec=timeout, force_cycle=True)
    out["repair"] = repair
    out["after"] = {
        "mode": brain_manager.get_brain_mode(),
        "runtime": brain_manager.get_melchior_runtime_status(),
        "status_text": brain_manager.get_brain_status(),
    }

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if bool(repair.get("success")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
