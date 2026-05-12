#!/usr/bin/env python3
"""Install or update lawchat-oss/mcp-taiwan-legal-db for MAGI."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT / ".runtime" / "mcp-taiwan-legal-db"
REPO = "https://github.com/lawchat-oss/mcp-taiwan-legal-db.git"


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd or ROOT), check=True)


def main() -> int:
    target = Path(os.environ.get("MAGI_TAIWAN_LEGAL_MCP_ROOT") or DEFAULT_TARGET).expanduser()
    py = Path(os.environ.get("MAGI_SKILL_PYTHON") or ROOT / "venv" / "bin" / "python").expanduser()
    pip = [str(py), "-m", "pip"]

    target.parent.mkdir(parents=True, exist_ok=True)
    if (target / ".git").exists():
        run(["git", "-C", str(target), "pull", "--ff-only"])
    else:
        run(["git", "clone", "--depth", "1", REPO, str(target)])

    run([*pip, "install", "aiosqlite", "tenacity", "truststore", "mcp[cli]>=1.0.0"])
    print(f"taiwan legal MCP ready: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

