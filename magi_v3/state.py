"""Explicit MAGI V3 job state machine."""

from __future__ import annotations

from enum import StrEnum

from .errors import InvalidTransition


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    WAITING_CHILDREN = "waiting_children"
    AWAITING_INPUT = "awaiting_input"
    NEEDS_CONFIRMATION = "needs_confirmation"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    DEFERRED = "deferred"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.DEGRADED,
        JobStatus.FAILED,
        JobStatus.SKIPPED,
        JobStatus.CANCELLED,
        JobStatus.TIMED_OUT,
    }
)


ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {JobStatus.LEASED, JobStatus.DEFERRED, JobStatus.SKIPPED, JobStatus.CANCELLED}
    ),
    JobStatus.LEASED: frozenset(
        {
            JobStatus.RUNNING,
            JobStatus.QUEUED,
            JobStatus.DEFERRED,
            JobStatus.NEEDS_CONFIRMATION,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.WAITING_CHILDREN,
            JobStatus.AWAITING_INPUT,
            JobStatus.NEEDS_CONFIRMATION,
            JobStatus.SUCCEEDED,
            JobStatus.DEGRADED,
            JobStatus.FAILED,
            JobStatus.DEFERRED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }
    ),
    JobStatus.WAITING_CHILDREN: frozenset(
        {
            JobStatus.QUEUED,
            JobStatus.SUCCEEDED,
            JobStatus.DEGRADED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
        }
    ),
    JobStatus.AWAITING_INPUT: frozenset(
        {JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.TIMED_OUT}
    ),
    JobStatus.NEEDS_CONFIRMATION: frozenset(
        {JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.TIMED_OUT}
    ),
    JobStatus.DEFERRED: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.DEGRADED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.SKIPPED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.TIMED_OUT: frozenset(),
}


def ensure_transition(
    current: JobStatus | str,
    target: JobStatus | str,
    *,
    business_completed: bool | None = None,
) -> JobStatus:
    """Validate and return the normalized target state."""

    current_status = JobStatus(current)
    target_status = JobStatus(target)
    if target_status not in ALLOWED_TRANSITIONS[current_status]:
        raise InvalidTransition(f"cannot transition {current_status.value} -> {target_status.value}")
    if target_status is JobStatus.SUCCEEDED and business_completed is not True:
        raise InvalidTransition("succeeded requires business_completed=true")
    return target_status
