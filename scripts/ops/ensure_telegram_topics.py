#!/usr/bin/env python3
"""Ensure MAGI Telegram forum topics and routing map exist."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.ops.red_phone import ensure_telegram_forum_topics


def main() -> int:
    parser = argparse.ArgumentParser(description="Create missing Telegram forum topics for MAGI notifications.")
    parser.add_argument("--dry-run", action="store_true", help="Report missing topics without creating them.")
    parser.add_argument("--json-out", default="", help="Optional JSON output path.")
    args = parser.parse_args()

    payload = ensure_telegram_forum_topics(dry_run=bool(args.dry_run))
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["json_out"] = str(out)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
