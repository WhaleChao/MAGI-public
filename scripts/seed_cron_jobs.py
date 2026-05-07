#!/usr/bin/env python3
"""Seed beginner-safe local cron jobs for a fresh MAGI checkout."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def default_python_path(repo_root: Path = REPO_ROOT) -> Path:
    env_python = os.environ.get("MAGI_CRON_PYTHON")
    if env_python:
        return Path(env_python).expanduser()
    venv_dir = Path(os.environ.get("MAGI_VENV_DIR", repo_root / ".venv")).expanduser()
    if platform.system() == "Windows":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def worldmonitor_job(repo_root: Path = REPO_ROOT, python_path: Path | None = None) -> dict[str, Any]:
    python_bin = python_path or default_python_path(repo_root)
    action_path = repo_root / "skills" / "worldmonitor-intel" / "action.py"
    return {
        "id": "job_worldmonitor_intel",
        "cron": "0 8 * * *",
        "command": f"{python_bin} {action_path} --task collect --no-reasoning --plain-output",
        "desc": "每日全球新聞網收集摘要（worldmonitor-intel）",
        "channel_id": None,
        "last_run": None,
        "last_run_minute": None,
        "enabled": True,
    }


def load_jobs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def seed_jobs(repo_root: Path = REPO_ROOT, *, python_path: Path | None = None) -> dict[str, Any]:
    cron_path = repo_root / "cron_jobs.json"
    jobs = load_jobs(cron_path)
    job = worldmonitor_job(repo_root, python_path)
    changed = False

    for idx, existing in enumerate(jobs):
        if existing.get("id") == job["id"]:
            if existing != job:
                jobs[idx] = {**existing, **job}
                changed = True
            break
    else:
        jobs.append(job)
        changed = True

    if changed:
        cron_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {"ok": True, "path": str(cron_path), "changed": changed, "jobs": len(jobs)}


def main() -> int:
    result = seed_jobs()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
