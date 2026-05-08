#!/usr/bin/env python3
"""
Install or refresh the oMLX profile restore LaunchAgent.

The restore agent runs once at login/boot and calls the canonical repo switch
script in auto mode. It intentionally avoids the historical runtime symlink
under ~/Library/Application Support/MAGI/bin because macOS may block that path
after reboot, leaving 8080 on the previous profile.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path


LABEL = "com.magi.omlx-restore"


def run(*args: str) -> None:
    subprocess.run(list(args), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    python_bin = project_root / "venv" / "bin" / "python3"
    if not python_bin.exists():
        python_bin = Path(sys.executable)

    runner = project_root / "scripts" / "ops" / "run_with_env.py"
    switch_script = project_root / "config" / "bin" / "omlx_switch_model.sh"
    if not runner.exists() or not switch_script.exists():
        print("Missing oMLX restore dependency", file=sys.stderr)
        print(f"runner={runner}", file=sys.stderr)
        print(f"switch_script={switch_script}", file=sys.stderr)
        return 1

    runtime_root = Path.home() / "Library" / "Application Support" / "MAGI"
    runtime_root.mkdir(parents=True, exist_ok=True)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    uid_target = f"gui/{os.getuid()}"

    command = "sleep 90 && exec {py} {runner} -- /bin/bash {switch} auto".format(
        py=shlex.quote(str(python_bin)),
        runner=shlex.quote(str(runner)),
        switch=shlex.quote(str(switch_script)),
    )
    plist = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", "-c", command],
        "RunAtLoad": True,
        "KeepAlive": False,
        "WorkingDirectory": str(project_root),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin",
            "MAGI_ROOT_DIR": str(project_root),
            "MAGI_RUNTIME_DIR": str(runtime_root),
        },
        "StandardOutPath": "/opt/homebrew/var/log/omlx_switch.log",
        "StandardErrorPath": "/opt/homebrew/var/log/omlx_switch.log",
    }

    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)

    run("launchctl", "bootout", f"{uid_target}/{LABEL}")
    run("launchctl", "unload", str(plist_path))
    run("launchctl", "bootstrap", uid_target, str(plist_path))
    run("launchctl", "enable", f"{uid_target}/{LABEL}")
    run("launchctl", "kickstart", "-k", f"{uid_target}/{LABEL}")

    print(f"Installed {LABEL} -> {plist_path}")
    print(f"Restore command: {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
