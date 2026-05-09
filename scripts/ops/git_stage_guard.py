#!/usr/bin/env python3
"""Guard MAGI commits from accidental blanket staging.

Codex Desktop can occasionally spawn a repo-wide ``git add``.  The add itself
cannot be intercepted from inside the repository, so this guard blocks the next
commit if unsafe paths reached the index.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
from pathlib import Path


BLOCKED_PREFIXES = (
    ".codex_tmp_",
    ".openclaw/",
    ".openclaw_archived_",
    ".agent/",
    ".runtime/",
    ".runtime_site_packages/",
    ".claude/",
    ".claire/",
    "Paperclip_rebuild/",
    "static/exports/",
    "exports/",
    "laf_downloads/",
    "法扶資料/",
    "筆錄下載/",
    "閱卷下載/",
)

BLOCKED_GLOBS = (
    "**/.laf_chrome_profile/**",
    "**/*credentials*",
    "**/*secret*",
    "**/*_token.json",
    "**/*_token.pickle",
    "**/*.pem",
    "**/*.p12",
    "**/*.pfx",
    "**/*.key",
)

MAX_STAGED_PATHS = 250


def _repo_root() -> Path:
    raw = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    return Path(raw)


def _staged_paths() -> list[str]:
    raw = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"])
    if not raw:
        return []
    return [p.decode("utf-8", errors="replace") for p in raw.split(b"\0") if p]


def is_blocked_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    if any(normalized.startswith(prefix) for prefix in BLOCKED_PREFIXES):
        return True
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in BLOCKED_GLOBS)


def validate_staged_paths(paths: list[str]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if len(paths) > MAX_STAGED_PATHS and os.environ.get("MAGI_ALLOW_LARGE_COMMIT") != "1":
        problems.append(
            f"too many staged paths ({len(paths)} > {MAX_STAGED_PATHS}); "
            "set MAGI_ALLOW_LARGE_COMMIT=1 only after reviewing the index"
        )
    blocked = [p for p in paths if is_blocked_path(p)]
    if blocked:
        preview = "\n".join(f"  - {p}" for p in blocked[:30])
        more = "" if len(blocked) <= 30 else f"\n  ... and {len(blocked) - 30} more"
        problems.append("blocked runtime/private paths are staged:\n" + preview + more)
    return (not problems), problems


def main() -> int:
    try:
        os.chdir(_repo_root())
        paths = _staged_paths()
        ok, problems = validate_staged_paths(paths)
    except Exception as exc:  # noqa: BLE001
        print(f"git_stage_guard: failed to inspect staged paths: {exc}", file=sys.stderr)
        return 1
    if ok:
        return 0
    print("git_stage_guard: refusing commit; unsafe staging detected.", file=sys.stderr)
    for problem in problems:
        print(f"\n{problem}", file=sys.stderr)
    print("\nUse explicit git add paths and unstage unsafe files before committing.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
