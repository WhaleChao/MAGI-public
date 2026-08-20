#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Start slow closed-case archive worker in the background.

The archive worker may need hours for very large case folders over SMB.  Cron
must not wait for that transfer, otherwise minute-based jobs scheduled after it
can be skipped.  This launcher records one PID and returns immediately.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = Path(os.environ.get("MAGI_RUNTIME_DIR", "").strip() or ROOT / ".runtime").expanduser()
PID_PATH = RUNTIME_DIR / "slow_archive_closed_cases_worker.pid"
LOG_PATH = RUNTIME_DIR / "slow_archive_closed_cases_worker.log"
TRIGGER_PATH = RUNTIME_DIR / "slow_archive_closed_cases_trigger_latest.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch slow_archive_closed_cases.py once in background.")
    parser.add_argument("--print-json", action="store_true")
    parser.add_argument("--foreground", action="store_true", help="Wait for the archive worker and return its terminal status.")
    parser.add_argument("--foreground-timeout-sec", type=int, default=120)
    args, worker_args = parser.parse_known_args(argv)

    worker_args = list(worker_args)
    if worker_args and worker_args[0] == "--":
        worker_args = worker_args[1:]

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    existing_pid = _read_pid(PID_PATH)
    if existing_pid and _pid_alive(existing_pid):
        payload = {
            "ok": True,
            "already_running": True,
            "pid": existing_pid,
            "log": str(LOG_PATH),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        _write_json(TRIGGER_PATH, payload)
        if args.print_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if existing_pid:
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass

    worker = ROOT / "scripts" / "ops" / "slow_archive_closed_cases.py"
    cmd = [sys.executable, str(worker), *worker_args]
    if args.foreground:
        if "--print-json" not in cmd:
            cmd.append("--print-json")
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(10, min(int(args.foreground_timeout_sec or 120), 7200)),
                check=False,
                env=os.environ.copy(),
            )
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as log_fh:
                if completed.stdout:
                    log_fh.write(completed.stdout)
                if completed.stderr:
                    log_fh.write(completed.stderr)
            worker_report: dict[str, Any] = {}
            try:
                decoder = json.JSONDecoder()
                for index, char in enumerate(completed.stdout or ""):
                    if char != "{":
                        continue
                    candidate, _ = decoder.raw_decode((completed.stdout or "")[index:])
                    if isinstance(candidate, dict):
                        worker_report = candidate
                        break
            except (ValueError, json.JSONDecodeError):
                worker_report = {}
            payload = {
                "ok": completed.returncode == 0 and worker_report.get("ok") is True,
                "foreground": True,
                "terminal": True,
                "returncode": completed.returncode,
                "command": cmd,
                "log": str(LOG_PATH),
                "worker_report": worker_report,
                "stderr_tail": (completed.stderr or "")[-1000:],
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
        except subprocess.TimeoutExpired as exc:
            payload = {
                "ok": False,
                "foreground": True,
                "terminal": True,
                "status": "timeout",
                "returncode": None,
                "command": cmd,
                "log": str(LOG_PATH),
                "stderr_tail": str(exc.stderr or "")[-1000:],
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
        _write_json(TRIGGER_PATH, payload)
        if args.print_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 1
    log_fh = LOG_PATH.open("ab")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=os.environ.copy(),
    )
    PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    payload = {
        "ok": True,
        "started": True,
        "pid": proc.pid,
        "command": cmd,
        "log": str(LOG_PATH),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(TRIGGER_PATH, payload)
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
