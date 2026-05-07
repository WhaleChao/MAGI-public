"""Minimal git helper compatibility layer."""

from __future__ import annotations

import subprocess
from pathlib import Path


def get_status(repo_root: str | None = None) -> str:
    root = Path(repo_root or Path(__file__).resolve().parents[2]).resolve()
    proc = subprocess.run(
        ["git", "-C", str(root), "status", "--short", "--branch"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "git status failed").strip()
        raise RuntimeError(err)
    return (proc.stdout or "").strip() or "working tree clean"
