#!/usr/bin/env python3
"""
Install or refresh the oMLX Embedding service LaunchAgent.

This creates a LaunchAgent that runs oMLX serve on port 8081
dedicated to embedding (ModernBERT-embed-4bit).
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path


LABEL = "com.magi.omlx-embed"


def run(*args: str) -> None:
    subprocess.run(list(args), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    runtime_root = Path.home() / "Library" / "Application Support" / "MAGI"
    log_dir = runtime_root / "logs"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    uid_target = f"gui/{os.getuid()}"

    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Find omlx binary
    omlx_bin = "/opt/homebrew/opt/omlx/bin/omlx"
    if not os.path.isfile(omlx_bin):
        import shutil
        found = shutil.which("omlx")
        if found:
            omlx_bin = found
        else:
            print(f"ERROR: omlx binary not found at {omlx_bin}")
            return 1

    # Read model dir and cache dir from environment or use defaults
    model_dir = os.environ.get("MAGI_OMLX_MODEL_DIR", str(Path.home() / ".omlx" / "models"))
    cache_dir = os.environ.get("MAGI_OMLX_CACHE_DIR", str(Path.home() / ".omlx" / "cache"))
    embed_port = int(os.environ.get("MAGI_OMLX_EMBED_PORT", "8081"))
    max_memory = os.environ.get("MAGI_OMLX_EMBED_MAX_MEMORY", "30")

    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            omlx_bin, "serve",
            "--model-dir", model_dir,
            "--paged-ssd-cache-dir", cache_dir,
            "--max-process-memory", max_memory,
            "--port", str(embed_port),
            "--max-num-seqs", "1",
            "--completion-batch-size", "1",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "WorkingDirectory": str(runtime_root),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin",
            "MAGI_ROOT_DIR": str(project_root),
        },
        "StandardOutPath": str(log_dir / "omlx_embed.launchd.log"),
        "StandardErrorPath": str(log_dir / "omlx_embed.launchd.log"),
    }

    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)

    # Register
    run("launchctl", "bootout", f"{uid_target}/{LABEL}")
    run("launchctl", "unload", str(plist_path))
    run("launchctl", "bootstrap", uid_target, str(plist_path))
    run("launchctl", "load", str(plist_path))
    run("launchctl", "enable", f"{uid_target}/{LABEL}")
    run("launchctl", "kickstart", "-k", f"{uid_target}/{LABEL}")

    print(f"Installed {LABEL} -> {plist_path}")
    print(f"oMLX embed will serve on port {embed_port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
