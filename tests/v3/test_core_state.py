from __future__ import annotations

import pytest

from magi_v3.errors import InvalidTransition
from magi_v3.state import JobStatus, ensure_transition


def test_success_requires_business_completion() -> None:
    with pytest.raises(InvalidTransition, match="business_completed"):
        ensure_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED, business_completed=False)

    assert (
        ensure_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED, business_completed=True)
        is JobStatus.SUCCEEDED
    )


def test_terminal_status_cannot_transition() -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(JobStatus.SUCCEEDED, JobStatus.QUEUED)


def test_waiting_and_confirmation_paths_are_explicit() -> None:
    assert (
        ensure_transition(JobStatus.RUNNING, JobStatus.WAITING_CHILDREN)
        is JobStatus.WAITING_CHILDREN
    )
    assert (
        ensure_transition(JobStatus.WAITING_CHILDREN, JobStatus.QUEUED)
        is JobStatus.QUEUED
    )
    with pytest.raises(InvalidTransition):
        ensure_transition(JobStatus.WAITING_CHILDREN, JobStatus.RUNNING)
    assert (
        ensure_transition(JobStatus.NEEDS_CONFIRMATION, JobStatus.QUEUED)
        is JobStatus.QUEUED
    )


def test_invalid_shortcut_is_rejected() -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(JobStatus.QUEUED, JobStatus.SUCCEEDED, business_completed=True)


def test_running_job_cannot_be_manually_requeued() -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(JobStatus.RUNNING, JobStatus.QUEUED)
