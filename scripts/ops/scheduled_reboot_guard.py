#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Guarded macOS reboot for MAGI day/night model transition windows.

This script is intentionally narrow: it only reboots inside the configured
maintenance windows immediately before oMLX day/night model switches, and only
when explicitly enabled by the operator environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.platforms import runtime_dir  # noqa: E402


WINDOWS = {
    # omlx_switch day runs at 06:55.
    "day": (6 * 60 + 30, 6 * 60 + 54),
    # omlx_switch night runs at 21:50.
    "night": (21 * 60 + 25, 21 * 60 + 49),
}

BLOCKING_PROCESS_MARKERS = (
    "skills/file-review-orchestrator/action.py",
    "laf_orchestrator.py",
    "laf_automation_v2.py",
    "skills/transcript",
    "judicial_automation_v2.py",
    "judgment-collector/action.py",
    "skills/pdf-namer/nightly_train.py",
    "scripts/weekend_bookmark_batch.py",
    "scripts/nightly_distill_gemma.py",
    "document_quality",
    "heavy_translation",
)


@dataclass
class RebootDecision:
    ok_to_reboot: bool
    mode: str
    apply: bool
    reasons: list[str] = field(default_factory=list)
    blockers: list[dict[str, str]] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    command_result: dict[str, Any] | None = None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _minute_of_day(now: datetime | None = None) -> int:
    dt = now or datetime.now()
    return dt.hour * 60 + dt.minute


def _window_for_mode(mode: str) -> tuple[int, int]:
    env_key = f"MAGI_REBOOT_{mode.upper()}_WINDOW"
    raw = os.environ.get(env_key, "").strip()
    if raw:
        try:
            start_s, end_s = raw.split("-", 1)
            sh, sm = [int(x) for x in start_s.split(":", 1)]
            eh, em = [int(x) for x in end_s.split(":", 1)]
            return sh * 60 + sm, eh * 60 + em
        except Exception:
            pass
    return WINDOWS[mode]


def _mode_from_auto(now: datetime | None = None) -> str:
    minute = _minute_of_day(now)
    for mode in ("day", "night"):
        start, end = _window_for_mode(mode)
        if start <= minute <= end:
            return mode
    # Outside reboot windows, choose the nearest upcoming switch for reporting.
    return "day" if minute < WINDOWS["day"][1] else "night"


def _inside_window(mode: str, now: datetime | None = None) -> bool:
    start, end = _window_for_mode(mode)
    return start <= _minute_of_day(now) <= end


def _ps_rows() -> list[dict[str, str]]:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return []
    rows = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, command = line.partition(" ")
        rows.append({"pid": pid.strip(), "command": command.strip()})
    return rows


def _active_magi_blockers() -> list[dict[str, str]]:
    blockers = []
    this_pid = str(os.getpid())
    for row in _ps_rows():
        if row.get("pid") == this_pid:
            continue
        cmd = row.get("command") or ""
        for marker in BLOCKING_PROCESS_MARKERS:
            if marker in cmd:
                blockers.append({"pid": row.get("pid", ""), "marker": marker, "command": cmd[:220]})
                break
    return blockers[:12]


def _office_unsaved_blockers() -> list[dict[str, str]]:
    if _truthy(os.environ.get("MAGI_REBOOT_IGNORE_UNSAVED_OFFICE")):
        return []
    script = r'''
set blockedApps to {}
tell application "System Events"
  set wordRunning to exists process "Microsoft Word"
  set excelRunning to exists process "Microsoft Excel"
  set pptRunning to exists process "Microsoft PowerPoint"
end tell
try
  if wordRunning then
    tell application "Microsoft Word"
      repeat with d in documents
        if saved of d is false then
          set end of blockedApps to "Microsoft Word"
          exit repeat
        end if
      end repeat
    end tell
  end if
end try
try
  if excelRunning then
    tell application "Microsoft Excel"
      repeat with d in workbooks
        if saved of d is false then
          set end of blockedApps to "Microsoft Excel"
          exit repeat
        end if
      end repeat
    end tell
  end if
end try
try
  if pptRunning then
    tell application "Microsoft PowerPoint"
      repeat with d in presentations
        if saved of d is false then
          set end of blockedApps to "Microsoft PowerPoint"
          exit repeat
        end if
      end repeat
    end tell
  end if
end try
set AppleScript's text item delimiters to "\n"
return blockedApps as text
'''
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    blockers = []
    for app in {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}:
        blockers.append({"pid": "", "marker": "unsaved_office_document", "command": app})
    return blockers


def _already_rebooted_today(mode: str) -> bool:
    if _truthy(os.environ.get("MAGI_REBOOT_ALLOW_REPEAT")):
        return False
    path = runtime_dir.root() / "scheduled_reboot_last.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    return data.get("date") == today and data.get("mode") == mode and data.get("status") == "requested"


def _record_last(mode: str, status: str) -> None:
    payload = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "mode": mode,
        "status": status,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    runtime_dir.atomic_write_json(runtime_dir.root() / "scheduled_reboot_last.json", payload)


def _write_report(decision: RebootDecision) -> None:
    payload = asdict(decision)
    runtime_dir.atomic_write_json(runtime_dir.root() / "scheduled_reboot_guard_latest.json", payload)
    runtime_dir.atomic_append_jsonl(
        runtime_dir.root() / "scheduled_reboot_guard.jsonl",
        payload,
        rotate_at=200,
        keep_tail=120,
    )


def decide(mode: str, *, apply: bool, force_window: bool = False) -> RebootDecision:
    mode = _mode_from_auto() if mode == "auto" else mode
    reasons: list[str] = []
    blockers: list[dict[str, str]] = []

    if mode not in WINDOWS:
        return RebootDecision(False, mode, apply, [f"unknown_mode:{mode}"])
    if not force_window and not _inside_window(mode):
        reasons.append("outside_maintenance_window")
    if _already_rebooted_today(mode):
        reasons.append("already_requested_today")
    if apply and not _truthy(os.environ.get("MAGI_ALLOW_SCHEDULED_REBOOT")):
        reasons.append("MAGI_ALLOW_SCHEDULED_REBOOT_not_set")

    blockers.extend(_active_magi_blockers())
    blockers.extend(_office_unsaved_blockers())
    if blockers:
        reasons.append("active_blockers_present")

    return RebootDecision(
        ok_to_reboot=not reasons and not blockers,
        mode=mode,
        apply=apply,
        reasons=reasons,
        blockers=blockers,
    )


def _restart_system() -> dict[str, Any]:
    command = ["/usr/bin/osascript", "-e", 'tell application "System Events" to restart']
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "command": command}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-500:],
        "stderr": (proc.stderr or "")[-500:],
        "duration_sec": round(time.time() - started, 2),
        "command": command,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded reboot before MAGI day/night model switches.")
    parser.add_argument("--mode", choices=["day", "night", "auto"], default="auto")
    parser.add_argument("--apply", action="store_true", help="Request macOS restart when all guards pass.")
    parser.add_argument("--force-window", action="store_true", help="Testing only: ignore maintenance window.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    decision = decide(args.mode, apply=bool(args.apply), force_window=bool(args.force_window))
    if decision.ok_to_reboot and args.apply:
        _record_last(decision.mode, "requested")
        decision.command_result = _restart_system()
        if not (decision.command_result or {}).get("ok"):
            _record_last(decision.mode, "failed")
            decision.ok_to_reboot = False
            decision.reasons.append("restart_command_failed")
    _write_report(decision)

    payload = asdict(decision)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        status = "requesting reboot" if decision.ok_to_reboot and args.apply else ("ready" if decision.ok_to_reboot else "skipped")
        print(f"MAGI scheduled reboot guard: {status} mode={decision.mode}")
        if decision.reasons:
            print("reasons: " + ", ".join(decision.reasons))
        if decision.blockers:
            print("blockers: " + ", ".join(b.get("marker", "") for b in decision.blockers))
    if "restart_command_failed" in decision.reasons:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
