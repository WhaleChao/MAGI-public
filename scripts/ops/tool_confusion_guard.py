#!/usr/bin/env python3
"""Release gate for MAGI tool-confusion regressions."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from scripts.live_magi_mtp_eval import check_tool_confusion_guards  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default="", help="optional path to save JSON report")
    args = parser.parse_args()

    started = time.time()
    result = check_tool_confusion_guards()
    report = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_sec": round(time.time() - started, 3),
        "ok": bool(result.ok),
        "checks": [result.__dict__],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
