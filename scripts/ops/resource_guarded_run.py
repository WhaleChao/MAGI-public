#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a command only when MAGI has enough local resources.

This is a non-destructive guard for cron jobs. It does not touch databases,
case folders, NAS data, user documents, or model files. When the machine is in
throttle/core-only/critical mode, it can skip non-core heavy jobs and record the
decision as operational telemetry instead of letting cron start work that may
fill disk or push the Mac into swap.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.platforms import runtime_dir  # noqa: E402
from scripts.ops import resource_governor  # noqa: E402


LEVEL_RANK = {"normal": 0, "throttle": 1, "core_only": 2, "critical": 3}


def _append_event(payload: dict[str, Any]) -> None:
    runtime_dir.atomic_append_jsonl(
        runtime_dir.root() / "resource_guarded_run.jsonl",
        payload,
        rotate_at=500,
        keep_tail=300,
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _drive_status_is_terminal_completion(status: dict[str, Any], *, kind: str, child_pid: int = 0) -> bool:
    if not isinstance(status, dict) or not status:
        return False
    candidates = [status]
    by_kind = status.get("status_by_kind")
    if isinstance(by_kind, dict) and isinstance(by_kind.get(kind), dict):
        candidates.insert(0, by_kind[kind])
    for candidate in candidates:
        try:
            pid = int(candidate.get("pid") or 0)
        except Exception:
            pid = 0
        if child_pid and pid and pid != child_pid:
            continue
        status_text = str(candidate.get("status") or "").strip().lower()
        if not candidate.get("finished_at"):
            continue
        if not status_text or "running" in status_text or status_text in {"interrupted", "timeout"}:
            continue
        return True
    return False


def _mark_drive_sync_guard_timeout(job_id: str, timeout_sec: int, *, child_pid: int = 0) -> None:
    if not job_id.startswith("job_drive_case_sync"):
        return
    kind = "all_files" if "all_files" in job_id else ("priority" if "bidirectional" in job_id else "inventory")
    drive_dir = runtime_dir.root() / "drive_sync"
    status_path = drive_dir / "drive_case_sync_worker_status_latest.json"
    try:
        previous = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
        if not isinstance(previous, dict):
            previous = {}
    except Exception:
        previous = {}
    if _drive_status_is_terminal_completion(previous, kind=kind, child_pid=child_pid):
        return
    status = {
        "ok": False,
        "status": "timeout",
        "action_required": False,
        "pid": int(child_pid or 0),
        "worker_kind": kind,
        "message": f"outer_guard_timeout:{timeout_sec}s",
        "finished_at": _iso_now(),
        "next_step": "外層 watchdog 已中止卡住的 Drive/NAS 同步；下次排程會重試近期待辦案件。",
    }
    status_by_kind = previous.get("status_by_kind")
    if not isinstance(status_by_kind, dict):
        status_by_kind = {}
    status_by_kind = {str(k): dict(v) for k, v in status_by_kind.items() if isinstance(v, dict)}
    status_by_kind[kind] = status
    merged_status = {**previous, **status, "status_by_kind": status_by_kind}
    _write_json_atomic(status_path, merged_status)
    _write_json_atomic(drive_dir / f"drive_case_sync_worker_status_{kind}_latest.json", status)
    state_path = drive_dir / "worker_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if not isinstance(state, dict):
            state = {}
        state["last_status"] = status
        state["last_summary"] = {"timeout": True, "outer_guard_timeout": True}
        state_status_by_kind = state.get("status_by_kind")
        if not isinstance(state_status_by_kind, dict):
            state_status_by_kind = {}
        state_status_by_kind[kind] = status
        state["status_by_kind"] = state_status_by_kind
        _write_json_atomic(state_path, state)
        _write_json_atomic(drive_dir / f"worker_state_{kind}.json", state)
    except Exception:
        pass


def _strip_separator(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _split_env_prefix(command: list[str]) -> tuple[dict[str, str], list[str]]:
    """Extract leading KEY=value tokens from guarded cron commands.

    Cron job builders sometimes prepend environment overrides before the
    executable. ``subprocess.Popen(list)`` does not understand shell-style
    assignments, so the guard must translate them into the child environment.
    """
    env: dict[str, str] = {}
    idx = 0
    while idx < len(command) and _ENV_ASSIGNMENT_RE.match(command[idx] or ""):
        key, value = command[idx].split("=", 1)
        env[key] = value
        idx += 1
    return env, command[idx:]


def _should_block(
    decision: resource_governor.ResourceDecision,
    *,
    block_at: str,
    require_disk_free_gb: float | None,
    require_free_inactive_gb: float | None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if LEVEL_RANK[decision.level] >= LEVEL_RANK[block_at]:
        reasons.append(f"resource_level>={block_at}:{decision.level}")
    if require_disk_free_gb is not None and decision.snapshot.disk_free_gb < require_disk_free_gb:
        reasons.append(
            f"disk_free<{require_disk_free_gb:g}GB:{decision.snapshot.disk_free_gb:g}GB"
        )
    if (
        require_free_inactive_gb is not None
        and decision.snapshot.free_plus_inactive_gb < require_free_inactive_gb
    ):
        reasons.append(
            "free_plus_inactive"
            f"<{require_free_inactive_gb:g}GB:{decision.snapshot.free_plus_inactive_gb:g}GB"
        )
    return bool(reasons), reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guard cron commands by MAGI resource level.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--block-at",
        choices=sorted(LEVEL_RANK, key=LEVEL_RANK.get),
        default="core_only",
        help="Skip command when resource level is this level or worse.",
    )
    parser.add_argument("--require-disk-free-gb", type=float)
    parser.add_argument("--require-free-inactive-gb", type=float)
    parser.add_argument("--timeout-sec", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = _strip_separator(args.command)
    env_prefix, command = _split_env_prefix(command)
    if not command:
        parser.error("missing command after --")

    decision = resource_governor.classify(resource_governor.collect_snapshot())
    blocked, block_reasons = _should_block(
        decision,
        block_at=args.block_at,
        require_disk_free_gb=args.require_disk_free_gb,
        require_free_inactive_gb=args.require_free_inactive_gb,
    )
    event: dict[str, Any] = {
        "ts": time.time(),
        "job_id": args.job_id,
        "block_at": args.block_at,
        "command": command,
        "env_prefix_keys": sorted(env_prefix),
        "decision": asdict(decision),
        "blocked": blocked,
        "block_reasons": block_reasons,
    }

    if blocked:
        event["returncode"] = 0
        _append_event(event)
        message = (
            f"MAGI resource guard skipped {args.job_id}: "
            + ", ".join(block_reasons)
        )
        if args.json:
            print(json.dumps(event, ensure_ascii=False, indent=2))
        else:
            print(message)
        return 0

    child_env = os.environ.copy()
    child_env.update(env_prefix)
    proc = subprocess.Popen(command, start_new_session=True, env=child_env)
    try:
        returncode = proc.wait(timeout=max(0, int(args.timeout_sec or 0)) or None)
    except subprocess.TimeoutExpired:
        event["timeout"] = True
        event["timeout_sec"] = int(args.timeout_sec or 0)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        returncode = 124
        _mark_drive_sync_guard_timeout(args.job_id, int(args.timeout_sec or 0), child_pid=proc.pid)
    event["returncode"] = int(returncode)
    _append_event(event)
    return int(returncode)


if __name__ == "__main__":
    raise SystemExit(main())
