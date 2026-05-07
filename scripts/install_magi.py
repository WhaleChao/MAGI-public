#!/usr/bin/env python3
"""Beginner installer for MAGI.

By default this script is safe and descriptive. Pass --yes to run install
commands, or --dry-run to print the plan without changing the machine.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = REPO_ROOT / ".venv"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class InstallStep:
    name: str
    command: list[str]
    required: bool = True
    description: str = ""


def venv_python(venv_dir: Path = VENV_DIR) -> Path:
    if platform.system() == "Windows":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def build_install_plan(*, include_optional: bool = True, venv_dir: Path = VENV_DIR) -> list[InstallStep]:
    python = sys.executable or "python3"
    pip_python = str(venv_python(venv_dir))
    steps = [
        InstallStep("create_venv", [python, "-m", "venv", str(venv_dir)], description="Create an isolated Python environment."),
        InstallStep("upgrade_pip", [pip_python, "-m", "pip", "install", "--upgrade", "pip", "wheel"], description="Upgrade packaging tools."),
        InstallStep("install_core", [pip_python, "-m", "pip", "install", "-r", str(REPO_ROOT / "requirements.txt")], description="Install MAGI core dependencies."),
        InstallStep("doctor", [pip_python, str(REPO_ROOT / "scripts" / "magi_doctor.py"), "--json"], description="Verify the installation."),
    ]
    if include_optional:
        steps.insert(
            3,
            InstallStep(
                "install_optional",
                [pip_python, "-m", "pip", "install", "-r", str(REPO_ROOT / "requirements-optional.txt")],
                required=False,
                description="Install optional local model acceleration dependencies.",
            ),
        )
    return steps


def run_step(step: InstallStep, *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"name": step.name, "ok": True, "skipped": True, "command": step.command}
    proc = subprocess.run(step.command, cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return {
        "name": step.name,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": step.command,
        "output_tail": proc.stdout[-4000:],
    }


def live_checks() -> dict[str, Any]:
    from scripts.magi_doctor import collect_report
    from scripts.public_release_audit import scan_tracked_files, summarize

    return {
        "doctor": collect_report(live=True),
        "public_release_audit": summarize(scan_tracked_files()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install MAGI for a first-time local user.")
    parser.add_argument("--yes", action="store_true", help="actually run installation commands")
    parser.add_argument("--dry-run", action="store_true", help="print the install plan without changing anything")
    parser.add_argument("--no-optional", action="store_true", help="skip optional model acceleration dependencies")
    parser.add_argument("--check-live", action="store_true", help="run doctor and public-release checks")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)

    dry_run = args.dry_run or not args.yes
    plan = build_install_plan(include_optional=not args.no_optional)
    results: list[dict[str, Any]] = []
    for step in plan:
        if not step.required and dry_run:
            results.append({"name": step.name, "ok": True, "skipped": True, "command": step.command})
            continue
        result = run_step(step, dry_run=dry_run)
        results.append(result)
        if not result["ok"] and step.required:
            break

    payload: dict[str, Any] = {
        "ok": all(r.get("ok") for r in results),
        "dry_run": dry_run,
        "plan": [asdict(s) for s in plan],
        "results": results,
    }
    if args.check_live:
        payload["live_checks"] = live_checks()
        payload["ok"] = payload["ok"] and payload["live_checks"]["doctor"]["ok"] and payload["live_checks"]["public_release_audit"]["ok"]

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        mode = "DRY RUN" if dry_run else "INSTALL"
        print(f"MAGI installer {mode}: {'OK' if payload['ok'] else 'CHECK NEEDED'}")
        for step in plan:
            print(f"- {step.name}: {' '.join(step.command)}")
        if dry_run:
            print("Pass --yes to run the installer.")

    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
