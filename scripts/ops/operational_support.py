#!/usr/bin/env python3
"""Emit a bounded, de-identified support bundle from the canonical V3 ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.ledger import JobLedger
from magi_v3.observability import support_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    if not args.ledger.is_file():
        print(json.dumps({"ok": False, "error": "找不到工作紀錄；請確認 ledger 路徑。"}, ensure_ascii=False))
        return 2
    try:
        records = JobLedger(args.ledger).recent_jobs(limit=args.limit)
        print(json.dumps({"ok": True, **support_bundle(records, max_records=args.limit)}, ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"無法建立支援包：{exc}"}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
