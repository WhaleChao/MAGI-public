#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run Cloudflare Quick Tunnel for the Paperclip share gateway.

This is launchd-friendly: cloudflared remains the foreground child, and the
supervisor extracts the current trycloudflare URL into runtime files.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


MAGI_ROOT = Path(os.environ.get("MAGI_ROOT") or "/Users/ai/Desktop/MAGI_v2")
PORT = os.environ.get("PAPERCLIP_SHARE_GATEWAY_PORT") or "5014"
CLOUDFLARED = os.environ.get("MAGI_CLOUDFLARED_PATH") or "/opt/homebrew/bin/cloudflared"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def main() -> int:
    log_dir = MAGI_ROOT / "logs"
    runtime_dir = MAGI_ROOT / ".runtime"
    agent_dir = MAGI_ROOT / ".agent"
    log_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / "paperclip_share_cloudflared.log"
    gateway_log_path = log_dir / "paperclip_share_gateway.log"
    url_path = runtime_dir / "osc_share_public_base_url.txt"
    agent_url_path = agent_dir / "paperclip_share_tunnel_url.txt"
    pid_path = agent_dir / "paperclip_share_cloudflared.pid"
    gateway_pid_path = agent_dir / "paperclip_share_gateway.pid"

    cmd = [
        CLOUDFLARED,
        "tunnel",
        "--url",
        f"http://127.0.0.1:{PORT}",
        "--no-autoupdate",
    ]
    gateway_env = dict(os.environ)
    gateway_env["PAPERCLIP_SHARE_GATEWAY_TARGET"] = os.environ.get(
        "PAPERCLIP_SHARE_GATEWAY_TARGET",
        "http://127.0.0.1:5002",
    )
    gateway_cmd = [
        sys.executable,
        str(MAGI_ROOT / "scripts" / "share_gateway.py"),
        "--port",
        PORT,
    ]

    with log_path.open("a", encoding="utf-8") as log, gateway_log_path.open("a", encoding="utf-8") as gateway_log:
        log.write("\n=== starting Paperclip share cloudflared ===\n")
        log.flush()
        gateway_log.write("\n=== starting Paperclip share gateway ===\n")
        gateway_log.flush()

        gateway_proc = subprocess.Popen(
            gateway_cmd,
            stdout=gateway_log,
            stderr=subprocess.STDOUT,
            env=gateway_env,
            cwd=str(MAGI_ROOT),
        )
        gateway_pid_path.write_text(str(gateway_proc.pid), encoding="utf-8")
        time.sleep(0.5)
        if gateway_proc.poll() is not None:
            log.write(f"share gateway exited early with {gateway_proc.returncode}\n")
            log.flush()
            return int(gateway_proc.returncode or 1)

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        pid_path.write_text(str(proc.pid), encoding="utf-8")
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                m = URL_RE.search(line)
                if m:
                    url = m.group(0).rstrip("/")
                    url_path.write_text(url + "\n", encoding="utf-8")
                    agent_url_path.write_text(url + "\n", encoding="utf-8")
                    print(f"Paperclip share URL: {url}", flush=True)
            return proc.wait()
        finally:
            for child in (proc, gateway_proc):
                try:
                    if child.poll() is None:
                        child.send_signal(signal.SIGTERM)
                except Exception:
                    pass
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass
            try:
                gateway_pid_path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
