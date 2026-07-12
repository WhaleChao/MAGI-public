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
import time
import urllib.request
from pathlib import Path


MAGI_ROOT = Path(
    os.environ.get("MAGI_ROOT_DIR")
    or os.environ.get("MAGI_ROOT")
    or str(Path(__file__).resolve().parents[1])
).resolve()
PORT = os.environ.get("PAPERCLIP_SHARE_GATEWAY_PORT") or "5014"
CLOUDFLARED = os.environ.get("MAGI_CLOUDFLARED_PATH") or "/opt/homebrew/bin/cloudflared"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _load_dotenv_value(name: str) -> str:
    env_path = MAGI_ROOT / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key.strip() == name:
                return value.strip().strip("'\"")
    except Exception:
        return ""
    return ""


def _normalize_base_url(value: str) -> str:
    base = str(value or "").strip().strip("'\"")
    if not base:
        return ""
    if "://" not in base:
        base = "https://" + base
    return base.rstrip("/")


def _stable_share_base_url() -> str:
    explicit = (
        os.environ.get("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL")
        or _load_dotenv_value("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL")
        or ""
    )
    if explicit:
        return _normalize_base_url(explicit)
    use_magi = (
        os.environ.get("MAGI_OSC_FILE_SHARE_USE_MAGI_PUBLIC_BASE")
        or _load_dotenv_value("MAGI_OSC_FILE_SHARE_USE_MAGI_PUBLIC_BASE")
        or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if use_magi:
        return _normalize_base_url(
            os.environ.get("MAGI_PUBLIC_BASE_URL")
            or _load_dotenv_value("MAGI_PUBLIC_BASE_URL")
            or ""
        )
    return ""


def _gateway_health_ok() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as resp:
            return 200 <= int(resp.status) < 300
    except Exception:
        return False


def _wait_for_gateway(log, timeout_sec: int = 30) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _gateway_health_ok():
            return True
        time.sleep(0.5)
    log.write(f"share gateway is not healthy on port {PORT}; launchd gateway job should own it\n")
    log.flush()
    return False


def _cleanup_orphan_cloudflared(log) -> None:
    """Stop stale quick-tunnel children before launchd starts the managed one."""
    pattern = f"cloudflared tunnel --url http://127.0.0.1:{PORT}"
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except Exception:
        return
    current_pid = os.getpid()
    for raw in (result.stdout or "").splitlines():
        try:
            pid = int(raw.strip())
        except ValueError:
            continue
        if pid <= 1 or pid == current_pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            log.write(f"terminated stale cloudflared pid={pid}\n")
            log.flush()
        except ProcessLookupError:
            continue
        except Exception as exc:
            log.write(f"failed to terminate stale cloudflared pid={pid}: {exc}\n")
            log.flush()


def main() -> int:
    log_dir = MAGI_ROOT / "logs"
    runtime_dir = MAGI_ROOT / ".runtime"
    agent_dir = MAGI_ROOT / ".agent"
    log_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / "paperclip_share_cloudflared.log"
    url_path = runtime_dir / "osc_share_public_base_url.txt"
    agent_url_path = agent_dir / "paperclip_share_tunnel_url.txt"
    pid_path = agent_dir / "paperclip_share_cloudflared.pid"

    stable_base = _stable_share_base_url()
    if stable_base:
        url_path.write_text(stable_base + "\n", encoding="utf-8")
        agent_url_path.write_text(stable_base + "\n", encoding="utf-8")
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n=== Paperclip share using stable base: {stable_base} ===\n")
            log.flush()
            while True:
                url_path.write_text(stable_base + "\n", encoding="utf-8")
                agent_url_path.write_text(stable_base + "\n", encoding="utf-8")
                time.sleep(300)

    cmd = [
        CLOUDFLARED,
        "tunnel",
        "--url",
        f"http://127.0.0.1:{PORT}",
        "--no-autoupdate",
    ]
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n=== starting Paperclip share cloudflared ===\n")
        log.flush()
        _cleanup_orphan_cloudflared(log)
        if not _wait_for_gateway(log):
            return 1

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(MAGI_ROOT),
        )
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
            try:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
            except Exception:
                pass
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
