#!/usr/bin/env python3
"""
Install or refresh the oMLX text inference LaunchAgent.

This keeps the local text model launch configuration reproducible and avoids
silent drift in launchctl environment overrides.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path


LABEL = "com.magi.omlx"


def run(*args: str) -> None:
    subprocess.run(list(args), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_launch_agent_plist(project_root: Path, runtime_root: Path) -> dict:
    log_path = Path("/opt/homebrew/var/log/omlx.log")
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", "/opt/homebrew/bin/omlx-magi-start-text"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin",
            "MAGI_ROOT_DIR": str(project_root),
            "MAGI_RUNTIME_DIR": str(runtime_root),
            "OMLX_TEXT_BASE_PATH": os.environ.get("OMLX_TEXT_BASE_PATH", str(Path.home() / ".omlx")),
            "OMLX_TEXT_MODEL_DIR": os.environ.get("OMLX_TEXT_MODEL_DIR", str(Path.home() / ".omlx" / "models-text")),
            "OMLX_TEXT_PORT": os.environ.get("OMLX_TEXT_PORT", "8080"),
            "OMLX_TEXT_MAX_MODEL_MEMORY": os.environ.get("OMLX_TEXT_MAX_MODEL_MEMORY", "10GB"),
            "OMLX_TEXT_MAX_PROCESS_MEMORY": os.environ.get("OMLX_TEXT_MAX_PROCESS_MEMORY", "auto"),
            "OMLX_TEXT_MAX_NUM_SEQS": os.environ.get("OMLX_TEXT_MAX_NUM_SEQS", "2"),
            "OMLX_TEXT_COMPLETION_BATCH_SIZE": os.environ.get("OMLX_TEXT_COMPLETION_BATCH_SIZE", "2"),
            "OMLX_TEXT_INITIAL_CACHE_BLOCKS": os.environ.get("OMLX_TEXT_INITIAL_CACHE_BLOCKS", "32"),
            "OMLX_TEXT_DISABLE_CACHE": os.environ.get("OMLX_TEXT_DISABLE_CACHE", "0"),
        },
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "Nice": 1,
    }


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    runtime_root = Path.home() / "Library" / "Application Support" / "MAGI"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    uid_target = f"gui/{os.getuid()}"

    runtime_root.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist = build_launch_agent_plist(project_root, runtime_root)

    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)

    run("launchctl", "bootout", f"{uid_target}/{LABEL}")
    run("launchctl", "unload", str(plist_path))
    run("launchctl", "bootstrap", uid_target, str(plist_path))
    run("launchctl", "load", str(plist_path))
    run("launchctl", "enable", f"{uid_target}/{LABEL}")
    run("launchctl", "kickstart", "-k", f"{uid_target}/{LABEL}")

    print(f"Installed {LABEL} -> {plist_path}")
    print("oMLX text service will serve on port 8080")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
