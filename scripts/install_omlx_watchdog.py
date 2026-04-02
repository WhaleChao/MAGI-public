#!/usr/bin/env python3
"""
Install or refresh the oMLX inference watchdog LaunchAgent.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path


LABEL = "com.magi.omlx-watchdog"


def run(*args: str) -> None:
    subprocess.run(list(args), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    script_path = project_root / "scripts" / "omlx_watchdog.sh"
    runtime_root = Path.home() / "Library" / "Application Support" / "MAGI"
    runtime_bin = runtime_root / "bin"
    runtime_script = runtime_bin / "omlx_watchdog.sh"
    log_dir = runtime_root / "logs"
    state_dir = runtime_root
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    uid_target = f"gui/{os.getuid()}"

    runtime_bin.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    mode = script_path.stat().st_mode
    script_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shutil.copy2(script_path, runtime_script)
    runtime_script.chmod(runtime_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    plist = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(runtime_script)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "WorkingDirectory": str(runtime_root),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin",
            "MAGI_ROOT_DIR": str(project_root),
            "MAGI_RUNTIME_DIR": str(runtime_root),
            "MAGI_OMLX_WATCHDOG_STATE_PATH": str(state_dir / "omlx_watchdog_state.json"),
            "MAGI_OMLX_WATCHDOG_MODEL": os.environ.get("MAGI_OMLX_WATCHDOG_MODEL", "TAIDE-12b-Chat-mlx-4bit"),
        },
        "StandardOutPath": str(log_dir / "omlx_watchdog.launchd.log"),
        "StandardErrorPath": str(log_dir / "omlx_watchdog.launchd.log"),
    }

    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)

    run("launchctl", "bootout", f"{uid_target}/{LABEL}")
    run("launchctl", "unload", str(plist_path))
    run("launchctl", "bootstrap", uid_target, str(plist_path))
    run("launchctl", "load", str(plist_path))
    run("launchctl", "enable", f"{uid_target}/{LABEL}")
    run("launchctl", "kickstart", "-k", f"{uid_target}/{LABEL}")

    print(f"Installed {LABEL} -> {plist_path}")
    print(f"Watchdog script: {runtime_script}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
