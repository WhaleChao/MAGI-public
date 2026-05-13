#!/usr/bin/env python3
"""CI gate: prevent key files from growing beyond monolith thresholds.

Exit 0 = within limits, exit 1 = at least one file exceeded its limit.
Run from the repository root:
    python scripts/ci/check_monolith_size.py
"""

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Thresholds: (relative path from repo root, max lines)
# ---------------------------------------------------------------------------

LIMITS: list[tuple[str, int]] = [
    ("api/server.py", 1000),
    ("api/orchestrator.py", 3000),
    ("templates/osc.html", 3000),
]


def find_repo_root() -> Path:
    """Walk up from CWD to find the repo root (contains .git/)."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists() or (parent / ".git").is_file():
            return parent
    return cwd


def main() -> int:
    repo_root = find_repo_root()
    failures: list[tuple[str, int, int]] = []

    for rel, limit in LIMITS:
        fpath = repo_root / rel
        if not fpath.exists():
            print(f"  SKIP  {rel} (file not found)")
            continue

        count = sum(1 for _ in fpath.open(encoding="utf-8", errors="replace"))

        status = "OK" if count <= limit else "OVER"
        print(f"  {status:4s}  {rel}: {count}/{limit} lines")
        if count > limit:
            failures.append((rel, count, limit))

    if not failures:
        print("\ncheck_monolith_size: PASS")
        return 0

    print(f"\ncheck_monolith_size: FAIL -- {len(failures)} file(s) exceeded limits")
    for rel, count, limit in failures:
        print(f"  {rel}: {count} lines (limit {limit}, over by {count - limit})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
