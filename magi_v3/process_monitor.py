"""Shared, read-only MAGI process classification for every operator surface.

The Golem console and the macOS Menubar used to answer different questions:
Golem counted any command string containing a worker path whose PPID was 1,
while Menubar counted only persistent ``Z`` processes.  Besides confusing the
operator, substring matching classified a detached ``zsh -lc 'python ...'``
launcher as a second worker.  This module is the single source of truth for
core processes, real Python workers, orphan ancestry, persistent zombies and
duplicate worker groups.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


WORKER_MARKERS: tuple[str, ...] = (
    "skills/judgment-collector/action.py",
    "skills/file-review-orchestrator/action.py",
    "skills/transcript-downloader/action.py",
    "skills/laf-portal-automation/action.py",
    "skills/laf-orchestrator/action.py",
    "skills/laf-withdrawal-report/action.py",
    "skills/laf-refine-case/action.py",
    "skills/osc-orchestrator/action.py",
    "skills/osc-scan-folder/action.py",
    "skills/pdf-namer/action.py",
    "skills/crawler-targets/action.py",
    "skills/statutes-vdb/action.py",
    "skills/magi-autopilot/action.py",
)

_MANAGED_PARENT_MARKERS: tuple[str, ...] = (
    "magi_v3/supervisor_service.py",
    "magi_v3/legacy_background_service.py",
    "skills/ops/cron_scheduler.py",
    "skills/ops/file_review_auto_worker.py",
)
_SHELL_NAMES = {"sh", "bash", "zsh", "dash", "ksh"}
_PYTHON_NAME_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|pypy(?:\d+)?)$", re.IGNORECASE)
_ETIME_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")


def parse_etime_seconds(raw: str) -> int:
    """Convert the macOS ``ps etime`` representation to seconds."""
    match = _ETIME_RE.fullmatch((raw or "").strip())
    if not match:
        return 0
    days, hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def process_monitor_markers(magi_root: Path) -> tuple[list[str], list[str], dict[str, str]]:
    """Return the canonical worker/core markers and public core labels."""
    try:
        from daemon import REAPER_NEVER_KILL as daemon_never_kill

        core_markers = list(daemon_never_kill)
    except Exception:
        core_markers = [
            f"{magi_root}/daemon.py",
            "api/server.py",
            "api/discord_bot.py",
            "rpc-server",
        ]
    core_labels = {
        f"{magi_root}/daemon.py": "Daemon",
        "daemon.py": "Daemon",
        "api/server.py": "API/LINE Webhook",
        "api/discord_bot.py": "Discord Bot",
        "rpc-server": "RPC Worker",
    }
    return list(WORKER_MARKERS), core_markers, core_labels


def parse_ps_rows(output: str) -> list[dict[str, Any]]:
    """Parse the exact unified ps shape used by Web, Golem and Menubar."""
    rows: list[dict[str, Any]] = []
    for raw in (output or "").splitlines():
        parts = raw.strip().split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "pid": pid,
                "ppid": ppid,
                "age_sec": parse_etime_seconds(parts[2]),
                "age": parts[2],
                "stat": parts[3],
                "cmd": parts[4],
            }
        )
    return rows


def _argv_head(command: str) -> str:
    try:
        argv = shlex.split(command or "", posix=True)
    except ValueError:
        return ""
    if not argv:
        return ""
    return Path(argv[0]).name


def _is_shell_command_wrapper(command: str) -> bool:
    try:
        argv = shlex.split(command or "", posix=True)
    except ValueError:
        return True
    if not argv or Path(argv[0]).name not in _SHELL_NAMES:
        return False
    return any(token == "-c" or (token.startswith("-") and "c" in token[1:]) for token in argv[1:3])


def _worker_marker(command: str, worker_markers: Iterable[str]) -> str:
    """Return a worker marker only for the actual Python process.

    A shell command line can contain the complete future Python command.  It is
    a launcher, not a worker, and must never inflate worker/orphan counts.
    """
    if _is_shell_command_wrapper(command):
        return ""
    if not _PYTHON_NAME_RE.fullmatch(_argv_head(command)):
        return ""
    return next((marker for marker in worker_markers if marker in command), "")


def _core_marker(command: str, core_markers: Iterable[str]) -> str:
    if _is_shell_command_wrapper(command):
        return ""
    return next((marker for marker in core_markers if marker in command), "")


def _is_managed_parent(command: str, core_markers: Iterable[str]) -> bool:
    if _is_shell_command_wrapper(command):
        return False
    return bool(_core_marker(command, core_markers)) or any(
        marker in command for marker in _MANAGED_PARENT_MARKERS
    )


def _is_orphan_worker(
    worker: Mapping[str, Any],
    rows_by_pid: Mapping[int, Mapping[str, Any]],
    managed_pids: set[int],
) -> bool:
    """A worker is orphaned when its ancestry reaches init without a MAGI owner."""
    current: Mapping[str, Any] = worker
    seen = {int(worker.get("pid") or 0)}
    for _ in range(32):
        parent_pid = int(current.get("ppid") or 0)
        if parent_pid <= 1:
            return True
        if parent_pid in managed_pids:
            return False
        if parent_pid in seen:
            return True
        seen.add(parent_pid)
        parent = rows_by_pid.get(parent_pid)
        if parent is None:
            return True
        current = parent
    return True


@dataclass
class ZombiePersistence:
    """Suppress sub-five-second exit/reap transitions on every UI."""

    persistence_seconds: float = 5.0
    first_seen: dict[tuple[int, int], float] = field(default_factory=dict)

    def persistent_ids(
        self, observed: Mapping[tuple[int, int], Mapping[str, Any]], *, now: float
    ) -> set[tuple[int, int]]:
        next_seen: dict[tuple[int, int], float] = {}
        persistent: set[tuple[int, int]] = set()
        for identity in observed:
            first = self.first_seen.get(identity, now)
            next_seen[identity] = first
            if now - first >= self.persistence_seconds:
                persistent.add(identity)
        self.first_seen = next_seen
        return persistent


def classify_process_rows(
    rows: list[dict[str, Any]],
    *,
    magi_root: Path,
    zombie_tracker: ZombiePersistence | None = None,
    monotonic_now: float | None = None,
) -> dict[str, Any]:
    """Classify one ps snapshot using the shared operator contract."""
    worker_markers, core_markers, core_labels = process_monitor_markers(magi_root)
    rows_by_pid = {int(row["pid"]): row for row in rows}
    core: list[dict[str, Any]] = []
    all_workers: list[dict[str, Any]] = []
    managed_pids: set[int] = set()

    for row in rows:
        cmd = str(row.get("cmd") or "")
        stat = str(row.get("stat") or "")
        core_match = _core_marker(cmd, core_markers)
        worker_match = _worker_marker(cmd, worker_markers)
        if core_match or worker_match or _is_managed_parent(cmd, core_markers):
            managed_pids.add(int(row["pid"]))
        if core_match and not stat.startswith("Z"):
            entry = dict(row)
            entry["label"] = core_labels.get(core_match, core_match)
            core.append(entry)
        if worker_match:
            entry = dict(row)
            entry["worker_marker"] = worker_match
            all_workers.append(entry)

    workers = [row for row in all_workers if not str(row.get("stat") or "").startswith("Z")]
    orphans = [
        {**row, "orphan_reason": "unmanaged_ancestry"}
        for row in workers
        if _is_orphan_worker(row, rows_by_pid, managed_pids)
    ]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in workers:
        grouped[str(row.get("cmd") or "")].append(row)
    duplicates = [
        {
            "count": len(items),
            "pids": [int(item["pid"]) for item in items],
            "cmd": command[:320],
        }
        for command, items in grouped.items()
        if len(items) > 1
    ]

    zombie_candidates: dict[tuple[int, int], Mapping[str, Any]] = {}
    for row in rows:
        if not str(row.get("stat") or "").startswith("Z"):
            continue
        identity = (int(row["pid"]), int(row["ppid"]))
        if int(row["pid"]) in managed_pids or int(row["ppid"]) in managed_pids:
            zombie_candidates[identity] = row
    if zombie_tracker is None:
        persistent_ids = set(zombie_candidates)
    else:
        persistent_ids = zombie_tracker.persistent_ids(
            zombie_candidates,
            now=time.monotonic() if monotonic_now is None else float(monotonic_now),
        )
    zombies = [dict(zombie_candidates[identity]) for identity in sorted(persistent_ids)]

    orphan_pids = {int(row["pid"]) for row in orphans}
    zombie_pids = {int(row["pid"]) for row in zombies}
    anomaly_count = len(orphan_pids | zombie_pids) + len(duplicates)
    return {
        "ok": True,
        "summary": {
            "core_count": len(core),
            "worker_count": len(workers),
            "orphan_count": len(orphans),
            "zombie_count": len(zombies),
            "duplicate_groups": len(duplicates),
            "anomaly_count": anomaly_count,
        },
        "core": sorted(core, key=lambda item: (item.get("label", ""), item.get("pid", 0))),
        "workers": sorted(workers, key=lambda item: (item.get("age_sec", 0), item.get("pid", 0)), reverse=True),
        "orphans": sorted(orphans, key=lambda item: (item.get("age_sec", 0), item.get("pid", 0)), reverse=True),
        "zombies": sorted(zombies, key=lambda item: (item.get("age_sec", 0), item.get("pid", 0)), reverse=True),
        "duplicates": sorted(duplicates, key=lambda item: item.get("count", 0), reverse=True),
    }


def collect_process_monitor(
    *,
    magi_root: Path,
    run_ps: Callable[..., Any] = subprocess.run,
    zombie_tracker: ZombiePersistence | None = None,
    monotonic_now: float | None = None,
) -> dict[str, Any]:
    """Read and classify the host process table without mutating it."""
    try:
        result = run_ps(
            ["ps", "-axo", "pid=,ppid=,etime=,stat=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
        )
        return classify_process_rows(
            parse_ps_rows(str(getattr(result, "stdout", "") or "")),
            magi_root=magi_root,
            zombie_tracker=zombie_tracker,
            monotonic_now=monotonic_now,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "summary": {
                "core_count": 0,
                "worker_count": 0,
                "orphan_count": 0,
                "zombie_count": 0,
                "duplicate_groups": 0,
                "anomaly_count": 1,
            },
            "core": [],
            "workers": [],
            "orphans": [],
            "zombies": [],
            "duplicates": [],
        }
