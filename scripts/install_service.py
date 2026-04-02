#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Install MAGI as a persistent background service.

macOS:   LaunchAgent (~/Library/LaunchAgents/)
Windows: Task Scheduler (schtasks)
Linux:   systemd user service (~/.config/systemd/user/)
"""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from skills.ops.platform_utils import (
    IS_MACOS, IS_WINDOWS, IS_LINUX,
    get_service_manager, get_venv_python,
)

SERVICE_NAME = "com.magi.casper"
DAEMON_SCRIPT = os.path.join(PROJECT_ROOT, "daemon.py")


def install():
    venv_python = get_venv_python()
    if not os.path.exists(venv_python):
        print(f"❌ Python not found at {venv_python}. Please create venv first.")
        sys.exit(1)

    command = f"{venv_python} {DAEMON_SCRIPT}"
    mgr = get_service_manager()

    print("--- Installing MAGI Persistence ---")
    print(f"  Platform: {'macOS' if IS_MACOS else 'Windows' if IS_WINDOWS else 'Linux'}")
    print(f"  Service:  {SERVICE_NAME}")
    print(f"  Command:  {command}")

    # Uninstall old version first
    mgr.uninstall(SERVICE_NAME)

    if mgr.install(SERVICE_NAME, command, description="MAGI Daemon — Multi-Agent Governance Infrastructure"):
        print(f"✅ Service {SERVICE_NAME} installed.")
        if mgr.start(SERVICE_NAME):
            print("🚀 MAGI is now persistent and running!")
        else:
            print("⚠️ Service installed but failed to start. Check logs.")
    else:
        print("❌ Failed to install service.")
        sys.exit(1)

    print("-----------------------------------")


def uninstall():
    mgr = get_service_manager()
    mgr.stop(SERVICE_NAME)
    if mgr.uninstall(SERVICE_NAME):
        print(f"✅ Service {SERVICE_NAME} removed.")
    else:
        print("⚠️ Service may not have been fully removed.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        uninstall()
    else:
        install()
