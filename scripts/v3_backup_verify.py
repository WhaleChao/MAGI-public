#!/usr/bin/env python3
"""Restore a V3 cutover backup into a new sandbox and verify its hash inventory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.v3_backup_prepare import BackupBlocked, verify_backup  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--restore-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = verify_backup(
            archive_path=args.archive,
            archive_sha256=args.archive_sha256,
            restore_dir=args.restore_dir,
        )
    except (OSError, BackupBlocked) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
