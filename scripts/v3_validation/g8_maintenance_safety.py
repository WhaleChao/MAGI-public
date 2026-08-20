"""Fail-closed process-group and restore rules for the G8 maintenance wrapper.

This module intentionally contains no mutation.  The shell wrapper takes a
fresh ``ps`` snapshot before every signal and asks this module which *whole*
process groups, if any, are still safe to address.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


V2_RESTORE_ENDPOINTS: tuple[tuple[int, str], ...] = (
    (5002, "/readyz"),
    (5003, "/health"),
    (5014, "/health"),
    (8088, "/health"),
)


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    pgid: int
    session: int
    command: str


def parse_ps_rows(text: str) -> tuple[ProcessRow, ...]:
    """Parse ``ps pid,ppid,pgid,sess,command`` output without guessing.

    Malformed rows are ignored: absence of a trustworthy row is a reason not
    to signal a process group, never a reason to broaden matching.
    """

    rows: list[ProcessRow] = []
    for raw in text.splitlines():
        fields = raw.strip().split(None, 4)
        if len(fields) != 5:
            continue
        try:
            pid, ppid, pgid, session = (int(value) for value in fields[:4])
        except ValueError:
            continue
        if min(pid, ppid, pgid, session) < 0:
            continue
        rows.append(ProcessRow(pid, ppid, pgid, session, fields[4]))
    return tuple(rows)


def ancestor_pids(rows: Sequence[ProcessRow], caller_pid: int) -> frozenset[int]:
    """Return the caller and its known ancestors, stopping on broken chains."""

    by_pid = {row.pid: row for row in rows}
    protected: set[int] = set()
    current = caller_pid
    while current and current not in protected:
        protected.add(current)
        row = by_pid.get(current)
        if row is None or row.ppid == current:
            break
        current = row.ppid
    return frozenset(protected)


def eligible_v2_process_groups(
    rows: Sequence[ProcessRow],
    *,
    runtime_marker: str,
    verified_v2_markers: Iterable[str],
    caller_pid: int,
    caller_pgid: int,
    caller_session: int,
    protected_pids: Iterable[int] = (),
) -> tuple[int, ...]:
    """Return only independent, leader-verified V2 groups safe to signal.

    A group is eligible only when its leader has ``pid == pgid`` and its
    command proves V2 ownership via the sealed runtime path or an explicitly
    verified V2 label.  Any overlap with the wrapper's caller/ancestor set,
    caller process group, or caller session rejects the complete group.
    """

    markers = tuple(marker for marker in verified_v2_markers if marker)
    protected = set(protected_pids)
    protected.update(ancestor_pids(rows, caller_pid))
    groups: dict[int, list[ProcessRow]] = {}
    for row in rows:
        groups.setdefault(row.pgid, []).append(row)

    eligible: list[int] = []
    for pgid, members in groups.items():
        if pgid <= 1 or pgid == caller_pgid:
            continue
        leader = next((row for row in members if row.pid == pgid), None)
        if leader is None or leader.session == caller_session:
            continue
        if any(row.pid in protected or row.session == caller_session for row in members):
            continue
        owned = runtime_marker in leader.command or any(
            marker in leader.command for marker in markers
        )
        if not owned:
            continue
        eligible.append(pgid)
    return tuple(sorted(eligible))


def reverify_group(
    rows: Sequence[ProcessRow],
    pgid: int,
    **policy: object,
) -> bool:
    """Require a second fresh snapshot to yield exactly the same safe group."""

    return pgid in eligible_v2_process_groups(rows, **policy)  # type: ignore[arg-type]
