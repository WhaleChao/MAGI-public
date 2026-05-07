#!/usr/bin/env python3
"""CI gate: detect hardcoded IPs and ports that should use config instead.

Exit 0 = clean, exit 1 = violations found.
Run from the repository root:
    python scripts/ci/check_hardcodes.py
"""

import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

PATTERNS = [
    # Tailscale IPs
    (re.compile(r"""(?:"|')100\.\d+\.\d+\.\d+"""), "Tailscale IP (100.x.x.x)"),
    # Hardcoded localhost ports
    (re.compile(r"""(?:"|')127\.0\.0\.1:8080(?:"|')"""), "Hardcoded 127.0.0.1:8080"),
    (re.compile(r"""(?:"|')127\.0\.0\.1:5003(?:"|')"""), "Hardcoded 127.0.0.1:5003"),
    (re.compile(r"""(?:"|')127\.0\.0\.1:5002(?:"|')"""), "Hardcoded 127.0.0.1:5002"),
    # LAN IPs
    (re.compile(r"""(?:"|')192\.168\.1\.\d+"""), "LAN IP (192.168.1.x)"),
]

# ---------------------------------------------------------------------------
# Allowlist -- paths that legitimately contain hardcoded values.
# Patterns are matched as substrings of the path relative to repo root.
# ---------------------------------------------------------------------------

ALLOWLIST = [
    # JSON registry / config data
    "json/",
    # Test fixtures and test files
    "tests/",
    "scripts/tests/",
    # Diagnostic / comparison / ops scripts
    "scripts/ab_compare_",
    "scripts/gemma4_comparison_test.py",
    "scripts/db_sync_to_remote.py",
    "scripts/ingest_raw_judgments.py",
    "scripts/ops/",
    # Git internals
    ".git/",
    # This script itself
    "scripts/ci/check_hardcodes.py",
    # Sync staging (snapshot copies, not runtime code)
    "sync_staging/",
    # Bridge modules -- fallback IPs guarded by env vars / node registry
    "skills/bridge/",
    # Skill action modules with env-guarded fallback IPs
    "skills/iron-dome/",
    "skills/db-dual-sync/",
    "skills/brain_manager/",
    "skills/magi-autopilot/",
    "skills/magi-doctor/",
    "skills/ops/",
    # API modules with env-guarded fallback IPs
    "api/tools_api.py",
    "api/nas_mount_guard.py",
    "api/db_failover.py",
    "api/osc/utils.py",
    # GUI menubar with env-guarded fallback IPs
    "gui/magi_menubar.py",
]

# ---------------------------------------------------------------------------
# Directories always skipped (non-.py or binary trees)
# ---------------------------------------------------------------------------

SKIP_DIRS = {".git", ".claude", "__pycache__", "node_modules", ".venv", "venv", ".agent",
             "cache", "exports", "_autopilot_runs"}


def _is_comment(line: str) -> bool:
    """Return True if the stripped line is a Python comment."""
    return line.lstrip().startswith("#")


def find_repo_root() -> Path:
    """Walk up from CWD to find the repo root (contains .git/)."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists() or (parent / ".git").is_file():
            return parent
    return cwd


def scan(repo_root: Path) -> list[tuple[str, int, str, str]]:
    """Return list of (rel_path, lineno, line_text, description) violations."""
    violations: list[tuple[str, int, str, str]] = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune skipped directories in-place
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for fname in filenames:
            if not fname.endswith(".py"):
                continue

            fpath = Path(dirpath) / fname
            rel = str(fpath.relative_to(repo_root))

            # Check allowlist
            if any(allow in rel for allow in ALLOWLIST):
                continue

            try:
                lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            for lineno, line in enumerate(lines, 1):
                if _is_comment(line):
                    continue
                for pattern, desc in PATTERNS:
                    if pattern.search(line):
                        violations.append((rel, lineno, line.rstrip(), desc))

    return violations


def main() -> int:
    repo_root = find_repo_root()
    violations = scan(repo_root)

    if not violations:
        print("check_hardcodes: PASS -- no hardcoded values detected")
        return 0

    print(f"check_hardcodes: FAIL -- {len(violations)} violation(s) found\n")
    for rel, lineno, line, desc in violations:
        print(f"  {rel}:{lineno}  [{desc}]")
        print(f"    {line}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
