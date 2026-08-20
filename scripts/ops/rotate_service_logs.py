#!/usr/bin/env python3
"""Bound fixed launchd service logs without replacing active file handles."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


DEFAULT_LOGS = (
    Path("/opt/homebrew/var/log/omlx.log"),
    Path("/opt/homebrew/var/log/omlx-embed.log"),
    Path("/opt/homebrew/var/log/omlx-phi4.log"),
    Path("/opt/homebrew/var/log/omlx-smol.log"),
    Path("/opt/homebrew/var/log/omlx_switch.log"),
    Path("/opt/homebrew/var/log/magi-db-proxy.log"),
)


def rotate_file(path: Path, *, max_bytes: int, backups: int = 3, dry_run: bool = False) -> dict:
    result = {"path": str(path), "rotated": False, "size_before": 0, "error": ""}
    try:
        if path.is_symlink() or not path.is_file():
            return result
        size = int(path.stat().st_size)
        result["size_before"] = size
        if size <= max_bytes:
            return result
        if dry_run:
            result["rotated"] = True
            return result

        for index in range(max(1, backups), 1, -1):
            older = path.with_name(f"{path.name}.{index - 1}")
            newer = path.with_name(f"{path.name}.{index}")
            if older.exists():
                os.replace(older, newer)
        first = path.with_name(f"{path.name}.1")
        shutil.copy2(path, first)
        with path.open("r+b") as handle:
            handle.truncate(0)
            handle.flush()
            os.fsync(handle.fileno())
        result["rotated"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-mb", type=int, default=20)
    parser.add_argument("--backups", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    results = [
        rotate_file(
            path,
            max_bytes=max(1, args.max_mb) * 1024 * 1024,
            backups=max(1, args.backups),
            dry_run=args.dry_run,
        )
        for path in DEFAULT_LOGS
    ]
    payload = {"ok": not any(item["error"] for item in results), "results": results}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
