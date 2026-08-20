from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from magi_v3.dispatcher import (
    DurableDispatcher,
    VerifiedCompletion,
    load_capability_worker_classes,
)
from magi_v3.errors import LeaseConflict, LedgerError, SupervisorError
from magi_v3.ledger import JobLedger, JobSpec
from magi_v3.resource import GlobalResourceGovernor, ResourceSnapshot
from magi_v3.state import JobStatus
from magi_v3.supervisor import WorkerSpec, WorkerSupervisor

TEST_RESOURCE_CLAIM = {
    "memory_mb": 16,
    "metal_mb": 0,
    "cpu_percent": 10,
    "disk_io": "none",
    "nas_io": "none",
    "network": "none",
    "browser_tokens": 0,
}
TEST_CAPABILITY_WORKERS = {"dispatcher_test": "light", "message": "integration"}


@pytest.fixture
def ledger(tmp_path: Path) -> JobLedger:
    value = JobLedger(tmp_path / "state" / "ledger.sqlite3")
    value.initialize()
    return value


def create_read_job(
    ledger: JobLedger,
    base: datetime,
    *,
    job_id: str = "dispatch-job",
    max_attempts: int = 2,
) -> None:
    ledger.create_job(
        JobSpec(
            job_id=job_id,
            capability="dispatcher_test",
            operation="run",
            worker_class="light",
            side_effect_class="read_only",
            input={},
            scheduled_for=base,
            max_attempts=max_attempts,
            timeout_sec=30,
            resource_claim=TEST_RESOURCE_CLAIM,
        ),
        now=base,
    )


def worker_factory(first_code: str, second_code: str | None = None):
    def factory(job, lease):
        code = first_code if lease.attempt_number == 1 else (second_code or first_code)
        return WorkerSpec(
            job_id=job.job_id,
            worker_class=job.worker_class,
            argv=(sys.executable, "-c", code),
            estimated_footprint_mb=16,
            timeout_sec=30,
        )

    return factory


def make_dispatcher(
    ledger: JobLedger,
    factory,
    *,
    supervisor: WorkerSupervisor | None = None,
    lease_seconds: int = 5,
    verifier=None,
    capability_worker_classes=None,
    owner_id: str = "dispatcher-a",
    preemption_grace_sec: float = 0.5,
) -> DurableDispatcher:
    completion_verifier = verifier or (
        lambda job, lease, result: VerifiedCompletion(
            target=JobStatus.SUCCEEDED,
            business_completed=True,
            result={"verified": True},
            artifacts=({"kind": "test_receipt", "uri": f"test://{job.job_id}"},),
        )
    )
    return DurableDispatcher(
        ledger=ledger,
        supervisor=supervisor or WorkerSupervisor(GlobalResourceGovernor()),
        worker_factory=factory,
        completion_verifier=completion_verifier,
        snapshot_provider=ResourceSnapshot,
        owner_id=owner_id,
        lease_seconds=lease_seconds,
        preemption_grace_sec=preemption_grace_sec,
        capability_worker_classes=(
            TEST_CAPABILITY_WORKERS
            if capability_worker_classes is None
            else capability_worker_classes
        ),
    )


def create_priority_job(
    ledger: JobLedger,
    base: datetime,
    *,
    job_id: str,
    capability: str,
    worker_class: str,
    priority_class: str,
    side_effect_class: str = "read_only",
    max_attempts: int = 1,
    memory_mb: int = 32,
) -> None:
    ledger.create_job(
        JobSpec(
            job_id=job_id,
            capability=capability,
            operation="preemption-fixture",
            worker_class=worker_class,
            side_effect_class=side_effect_class,
            priority_class=priority_class,
            input={},
            scheduled_for=base,
            max_attempts=max_attempts,
            timeout_sec=30,
            idempotency_key=(
                f"{job_id}:fixture"
                if side_effect_class in {
                    "local_draft",
                    "reversible_write",
                    "external_commit",
                    "destructive",
                }
                else None
            ),
            resource_claim={
                "memory_mb": memory_mb,
                "metal_mb": 0,
                "cpu_percent": 10,
                "disk_io": "none",
                "nas_io": "none",
                "network": "none",
                "browser_tokens": 0,
            },
        ),
        now=base,
    )


def wait_for_path(path: Path, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def assert_pid_gone(pid: int, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def create_bound_job(
    ledger: JobLedger,
    base: datetime,
    *,
    worker_class: str = "light",
    capability: str = "dispatcher_test",
    resource_claim=None,
) -> None:
    ledger.create_job(
        JobSpec(
            job_id="bound-job",
            capability=capability,
            operation="bind",
            worker_class=worker_class,
            side_effect_class="read_only",
            priority_class="P2",
            input={},
            scheduled_for=base,
            timeout_sec=20,
            resource_claim=resource_claim
            or {
                "memory_mb": 64,
                "metal_mb": 32,
                "cpu_percent": 25,
                "disk_io": "heavy",
                "nas_io": "light",
                "network": "light",
                "browser_tokens": 0,
            },
        ),
        now=base,
    )


def bound_factory(**overrides):
    def factory(job, lease):
        spec = WorkerSpec(
            job_id=job.job_id,
            worker_class=job.worker_class,
            argv=(sys.executable, "-c", "pass"),
            estimated_footprint_mb=16,
            estimated_metal_mb=8,
            cpu_percent=10,
            disk_io="light",
            nas_io="none",
            network="light",
            browser_tokens=0,
            priority_class="P2",
            timeout_sec=10,
        )
        return replace(spec, **overrides)

    return factory


def test_default_capability_manifest_loader_is_strict_and_authoritative() -> None:
    mapping = load_capability_worker_classes()

    assert mapping["channels"] == "light"
    assert mapping["laf_legal_aid"] == "browser"
    assert mapping["skills_lifecycle"] == "maintenance"


def test_unknown_or_mismatched_capability_fails_before_factory_or_spawn(
    ledger: JobLedger,
) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)
    factory_calls = 0

    def factory(job, lease):
        nonlocal factory_calls
        factory_calls += 1
        return worker_factory("pass")(job, lease)

    dispatcher = make_dispatcher(ledger, factory, capability_worker_classes={})
    with pytest.raises(ValueError, match="unknown capability"):
        dispatcher.dispatch_next(now=base)
    assert factory_calls == 0
    assert dispatcher.supervisor.active_job_ids() == ()
    assert ledger.get_job("dispatch-job").status is JobStatus.DEFERRED

    ledger.transition_job("dispatch-job", JobStatus.QUEUED, now=base)
    dispatcher = make_dispatcher(
        ledger,
        factory,
        capability_worker_classes={"dispatcher_test": "integration"},
    )
    with pytest.raises(ValueError, match="conflicts with capability manifest"):
        dispatcher.dispatch_next(now=base)
    assert factory_calls == 0
    assert dispatcher.supervisor.active_job_ids() == ()


def test_ledger_contract_is_bound_to_worker_spec_and_resource_lease(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_bound_job(ledger, base)

    class CapturingGovernor(GlobalResourceGovernor):
        captured = None

        def acquire(self, request, snapshot):
            self.captured = request
            return super().acquire(request, snapshot)

    class CapturingSupervisor(WorkerSupervisor):
        captured = None

        def start(self, spec, snapshot):
            self.captured = spec
            return super().start(spec, snapshot)

    governor = CapturingGovernor()
    supervisor = CapturingSupervisor(governor)
    dispatcher = make_dispatcher(
        ledger,
        bound_factory(),
        supervisor=supervisor,
    )
    handle = dispatcher.dispatch_next(now=base)
    assert handle is not None
    assert supervisor.captured is not None
    assert governor.captured is not None
    assert supervisor.captured.priority_class == "P2"
    assert supervisor.captured.timeout_sec == 20
    assert supervisor.captured.estimated_footprint_mb == 64
    assert supervisor.captured.estimated_metal_mb == 32
    assert supervisor.captured.cpu_percent == 25
    assert supervisor.captured.disk_io == "heavy"
    assert supervisor.captured.nas_io == "light"
    assert supervisor.captured.network == "light"
    assert supervisor.captured.browser_tokens == 0
    assert governor.captured.priority_class == "P2"
    assert governor.captured.estimated_footprint_mb == 64
    assert governor.captured.estimated_metal_mb == 32
    assert governor.captured.cpu_percent == 25
    assert governor.captured.disk_io == "heavy"
    assert governor.captured.nas_io == "light"
    assert governor.captured.network == "light"
    assert governor.captured.browser_tokens == 0
    result = supervisor.wait(handle.lease.job.job_id)
    dispatcher.commit_result(handle.lease.job.job_id, result, now=base + timedelta(seconds=1))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"estimated_footprint_mb": 65}, "resource_claim"),
        ({"estimated_metal_mb": 33}, "resource_claim"),
        ({"cpu_percent": 26}, "resource_claim"),
        ({"disk_io": "heavy", "nas_io": "heavy"}, "resource_claim"),
        ({"network": "heavy"}, "resource_claim"),
        ({"browser_tokens": 1}, "resource_claim"),
        ({"timeout_sec": 21}, "timeout_sec"),
        ({"priority_class": "P1"}, "priority_class"),
        ({"interactive": True}, "interactive"),
    ],
)
def test_factory_cannot_raise_ledger_resource_priority_or_timeout(
    ledger: JobLedger,
    override: dict[str, object],
    message: str,
) -> None:
    base = datetime.now(timezone.utc)
    create_bound_job(ledger, base)
    dispatcher = make_dispatcher(ledger, bound_factory(**override))

    with pytest.raises(ValueError, match=message):
        dispatcher.dispatch_next(now=base)
    assert dispatcher.supervisor.active_job_ids() == ()
    assert ledger.get_job("bound-job").status is JobStatus.DEFERRED


def test_browser_capability_must_reserve_browser_token(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_bound_job(
        ledger,
        base,
        worker_class="browser",
        capability="browser_test",
        resource_claim={
            "memory_mb": 64,
            "metal_mb": 0,
            "cpu_percent": 25,
            "disk_io": "light",
            "nas_io": "none",
            "network": "light",
            "browser_tokens": 0,
        },
    )
    dispatcher = make_dispatcher(
        ledger,
        bound_factory(estimated_metal_mb=0),
        capability_worker_classes={"browser_test": "browser"},
    )

    with pytest.raises(ValueError, match="browser token"):
        dispatcher.dispatch_next(now=base)
    assert dispatcher.supervisor.active_job_ids() == ()


def test_worker_result_before_expiry_commits_only_through_dispatcher(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)
    dispatcher = make_dispatcher(ledger, worker_factory("pass"))

    handle = dispatcher.dispatch_next(now=base)
    assert handle is not None
    result = dispatcher.supervisor.wait(handle.lease.job.job_id)
    assert result.lease_token == handle.lease.token
    assert result.attempt_number == handle.lease.attempt_number
    outcome = dispatcher.commit_result(handle.lease.job.job_id, result, now=base + timedelta(seconds=1))

    assert outcome.fenced is False
    assert outcome.job.status is JobStatus.SUCCEEDED
    assert outcome.job.business_completed is True
    assert dispatcher.active_job_ids() == ()
    with pytest.raises(LeaseConflict):
        dispatcher.commit_result(handle.lease.job.job_id, result, now=base + timedelta(seconds=2))


def test_expiry_fences_then_kills_before_requeue_and_old_result_cannot_commit(
    ledger: JobLedger,
) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)

    class ObservingSupervisor(WorkerSupervisor):
        observed_fenced_running = False

        def terminate(self, job_id: str, *, grace_sec: float = 5.0):
            with sqlite3.connect(ledger.path) as conn:
                status = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
                fenced_at = conn.execute(
                    "SELECT fenced_at FROM leases WHERE job_id=?", (job_id,)
                ).fetchone()[0]
            self.observed_fenced_running = status == "running" and fenced_at is not None
            return super().terminate(job_id, grace_sec=grace_sec)

    supervisor = ObservingSupervisor(GlobalResourceGovernor())
    dispatcher = make_dispatcher(
        ledger,
        worker_factory("import time; time.sleep(30)", "pass"),
        supervisor=supervisor,
        lease_seconds=1,
    )
    first = dispatcher.dispatch_next(now=base)
    assert first is not None
    expired = dispatcher.expire(first.lease.job.job_id, now=base + timedelta(seconds=2))

    assert supervisor.observed_fenced_running is True
    assert expired.fenced is True
    assert expired.job.status is JobStatus.QUEUED
    assert expired.worker_result is not None and expired.worker_result.process_group_gone

    second = dispatcher.dispatch_next(now=base + timedelta(seconds=3))
    assert second is not None and second.lease.attempt_number == 2
    assert expired.worker_result is not None
    with pytest.raises(LeaseConflict, match="generation"):
        dispatcher.commit_result(
            second.lease.job.job_id,
            expired.worker_result,
            now=base + timedelta(seconds=3),
        )
    result = dispatcher.supervisor.wait(second.lease.job.job_id)
    assert dispatcher.commit_result(
        second.lease.job.job_id,
        result,
        now=base + timedelta(seconds=3, milliseconds=500),
    ).job.status is JobStatus.SUCCEEDED


def test_external_commit_expiry_becomes_ambiguous_and_never_auto_retries(
    ledger: JobLedger,
) -> None:
    base = datetime.now(timezone.utc)
    token = "dispatcher-external-confirmation-0001"
    ledger.create_job(
        JobSpec(
            job_id="external",
            capability="message",
            operation="send",
            worker_class="integration",
            side_effect_class="external_commit",
            idempotency_key="message:dispatcher",
            confirmation_token=token,
            confirmation_expires_at=base + timedelta(minutes=5),
            input={},
            scheduled_for=base,
            max_attempts=2,
            timeout_sec=30,
            resource_claim=TEST_RESOURCE_CLAIM,
        ),
        now=base,
    )
    ledger.confirm_job("external", token, now=base)
    dispatcher = make_dispatcher(
        ledger,
        worker_factory("import time; time.sleep(30)"),
        lease_seconds=1,
    )
    handle = dispatcher.dispatch_next(now=base)
    assert handle is not None
    outcome = dispatcher.expire("external", now=base + timedelta(seconds=2))

    assert outcome.job.status is JobStatus.NEEDS_CONFIRMATION
    assert dispatcher.dispatch_next(now=base + timedelta(seconds=3)) is None
    with pytest.raises(LeaseConflict):
        ledger._commit_worker_result(
            handle.lease.token,
            JobStatus.SUCCEEDED,
            owner_id=handle.lease.owner_id,
            attempt_number=handle.lease.attempt_number,
            business_completed=True,
            now=base + timedelta(seconds=3),
        )


def test_heartbeat_and_fence_are_atomic_and_generation_bound(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)
    dispatcher = make_dispatcher(
        ledger,
        worker_factory("import time; time.sleep(30)"),
        lease_seconds=2,
    )
    handle = dispatcher.dispatch_next(now=base)
    assert handle is not None
    dispatcher.heartbeat(handle.lease.job.job_id, now=base + timedelta(seconds=1))
    with pytest.raises(LeaseConflict, match="unexpired"):
        ledger.fence_lease(
            handle.lease.token,
            owner_id=handle.lease.owner_id,
            attempt_number=handle.lease.attempt_number,
            now=base + timedelta(seconds=2),
        )
    with pytest.raises(LeaseConflict):
        ledger.heartbeat_lease(
            handle.lease.token,
            owner_id="wrong-owner",
            attempt_number=handle.lease.attempt_number,
            extend_seconds=2,
            now=base + timedelta(seconds=2),
        )
    dispatcher.expire(handle.lease.job.job_id, now=base + timedelta(seconds=4))
    with pytest.raises(LeaseConflict):
        ledger.heartbeat_lease(
            handle.lease.token,
            owner_id=handle.lease.owner_id,
            attempt_number=handle.lease.attempt_number,
            extend_seconds=2,
            now=base + timedelta(seconds=4),
        )


def test_concurrent_heartbeat_and_fence_have_exactly_one_sqlite_winner(
    ledger: JobLedger,
) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)
    lease = ledger.lease_next("dispatcher-a", lease_seconds=1, now=base)
    assert lease is not None
    ledger.mark_running(
        lease.token,
        owner_id=lease.owner_id,
        attempt_number=lease.attempt_number,
        now=base,
    )
    barrier = threading.Barrier(3)
    successes: list[str] = []
    errors: list[BaseException] = []

    def heartbeat() -> None:
        try:
            barrier.wait()
            JobLedger(ledger.path).heartbeat_lease(
                lease.token,
                owner_id=lease.owner_id,
                attempt_number=lease.attempt_number,
                extend_seconds=10,
                now=base + timedelta(milliseconds=999),
            )
            successes.append("heartbeat")
        except BaseException as exc:
            errors.append(exc)

    def fence() -> None:
        try:
            barrier.wait()
            JobLedger(ledger.path).fence_lease(
                lease.token,
                owner_id=lease.owner_id,
                attempt_number=lease.attempt_number,
                now=base + timedelta(seconds=1),
            )
            successes.append("fence")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=heartbeat), threading.Thread(target=fence)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(successes) == 1
    assert len(errors) == 1 and isinstance(errors[0], LeaseConflict)


def test_expiry_drains_descendant_process_group(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base, job_id="descendants")
    code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']);"
        "time.sleep(30)"
    )
    dispatcher = make_dispatcher(ledger, worker_factory(code), lease_seconds=1)
    handle = dispatcher.dispatch_next(now=base)
    assert handle is not None
    outcome = dispatcher.expire("descendants", now=base + timedelta(seconds=2))

    assert outcome.worker_result is not None and outcome.worker_result.process_group_gone
    with pytest.raises(ProcessLookupError):
        os.killpg(handle.worker_pid, 0)


def test_factory_cannot_enable_ambient_environment_inheritance(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)

    def unsafe_factory(job, lease):
        return WorkerSpec(
            job_id=job.job_id,
            worker_class=job.worker_class,
            argv=(sys.executable, "-c", "pass"),
            estimated_footprint_mb=1,
            inherit_environment=True,
        )

    dispatcher = make_dispatcher(ledger, unsafe_factory)
    with pytest.raises(ValueError, match="ambient environment"):
        dispatcher.dispatch_next(now=base)
    assert ledger.get_job("dispatch-job").status is JobStatus.DEFERRED
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0


def test_exit_zero_without_business_receipt_is_not_success(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)
    dispatcher = make_dispatcher(
        ledger,
        worker_factory("pass"),
        verifier=lambda job, lease, result: VerifiedCompletion(
            target=JobStatus.SUCCEEDED,
            business_completed=True,
            result={"queued": True},
        ),
    )
    handle = dispatcher.dispatch_next(now=base)
    assert handle is not None
    result = dispatcher.supervisor.wait(handle.lease.job.job_id)
    outcome = dispatcher.commit_result(
        handle.lease.job.job_id,
        result,
        now=base + timedelta(seconds=1),
    )
    assert outcome.job.status is JobStatus.FAILED
    assert outcome.job.business_completed is False
    assert outcome.job.error["code"] == "completion_verification_failed"


def test_waiting_children_verification_suspends_instead_of_succeeding(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)
    dispatcher = make_dispatcher(
        ledger,
        worker_factory("pass"),
        verifier=lambda job, lease, result: VerifiedCompletion(
            target=JobStatus.WAITING_CHILDREN,
            business_completed=False,
            result={"child_job_ids": ["child-1"]},
        ),
    )
    handle = dispatcher.dispatch_next(now=base)
    assert handle is not None
    result = dispatcher.supervisor.wait(handle.lease.job.job_id)
    outcome = dispatcher.commit_result(
        handle.lease.job.job_id,
        result,
        now=base + timedelta(seconds=1),
    )
    assert outcome.job.status is JobStatus.WAITING_CHILDREN
    assert outcome.job.business_completed is False


def test_verified_completion_receipt_is_required_for_success(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)
    dispatcher = make_dispatcher(ledger, worker_factory("pass"))
    handle = dispatcher.dispatch_next(now=base)
    assert handle is not None
    result = dispatcher.supervisor.wait(handle.lease.job.job_id)
    outcome = dispatcher.commit_result(
        handle.lease.job.job_id,
        result,
        now=base + timedelta(seconds=1),
    )
    assert outcome.job.status is JobStatus.SUCCEEDED
    assert outcome.job.result["artifacts"][0]["kind"] == "test_receipt"


def test_factory_raise_and_start_failure_leave_no_orphan_lease(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base, job_id="factory-fail")

    def raising_factory(job, lease):
        raise RuntimeError("factory boom")

    dispatcher = make_dispatcher(ledger, raising_factory)
    with pytest.raises(RuntimeError, match="factory boom"):
        dispatcher.dispatch_next(now=base)
    assert ledger.get_job("factory-fail").status is JobStatus.DEFERRED

    ledger.transition_job("factory-fail", JobStatus.QUEUED, now=base)

    class StartFailureSupervisor(WorkerSupervisor):
        def start(self, spec, snapshot):
            raise OSError("spawn failed")

    dispatcher = make_dispatcher(
        ledger,
        worker_factory("pass"),
        supervisor=StartFailureSupervisor(GlobalResourceGovernor()),
    )
    with pytest.raises(OSError, match="spawn failed"):
        dispatcher.dispatch_next(now=base)
    assert ledger.get_job("factory-fail").status is JobStatus.FAILED
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0


def test_pid_bind_failure_kills_worker_and_closes_lease(
    ledger: JobLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base)
    dispatcher = make_dispatcher(
        ledger,
        worker_factory("import time; time.sleep(30)"),
    )

    def fail_bind(*args, **kwargs):
        raise LeaseConflict("simulated bind race")

    monkeypatch.setattr(ledger, "bind_worker_pid", fail_bind)
    with pytest.raises(LeaseConflict, match="bind race"):
        dispatcher.dispatch_next(now=base)
    assert dispatcher.supervisor.active_job_ids() == ()
    assert ledger.get_job("dispatch-job").status is JobStatus.FAILED
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0


def test_ledger_is_running_before_spawn_and_mark_failure_never_spawns(
    ledger: JobLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = datetime.now(timezone.utc)
    create_read_job(ledger, base, job_id="ordered-start")

    class OrderingSupervisor(WorkerSupervisor):
        observed_running_without_pid = False

        def start(self, spec, snapshot):
            with sqlite3.connect(ledger.path) as conn:
                row = conn.execute(
                    """
                    SELECT jobs.status, attempts.worker_pid
                    FROM jobs JOIN attempts USING(job_id)
                    WHERE jobs.job_id=? AND attempts.attempt_number=?
                    """,
                    (spec.job_id, spec.attempt_number),
                ).fetchone()
            self.observed_running_without_pid = row == ("running", None)
            return super().start(spec, snapshot)

    supervisor = OrderingSupervisor(GlobalResourceGovernor())
    dispatcher = make_dispatcher(
        ledger,
        worker_factory("pass"),
        supervisor=supervisor,
    )
    handle = dispatcher.dispatch_next(now=base)
    assert handle is not None and supervisor.observed_running_without_pid
    result = supervisor.wait("ordered-start")
    dispatcher.commit_result("ordered-start", result, now=base + timedelta(seconds=1))

    create_read_job(ledger, base, job_id="mark-fail")
    second_supervisor = OrderingSupervisor(GlobalResourceGovernor())
    second = make_dispatcher(
        ledger,
        worker_factory("pass"),
        supervisor=second_supervisor,
    )

    def fail_mark(*args, **kwargs):
        raise LeaseConflict("simulated mark race")

    monkeypatch.setattr(ledger, "mark_running", fail_mark)
    with pytest.raises(LeaseConflict, match="mark race"):
        second.dispatch_next(now=base)
    assert second_supervisor.active_job_ids() == ()
    assert ledger.get_job("mark-fail").status is JobStatus.DEFERRED


def test_interactive_dispatch_preempts_owned_heavy_group_and_resumes_once(
    ledger: JobLedger,
    tmp_path: Path,
) -> None:
    base = datetime.now(timezone.utc)
    ready = tmp_path / "heavy.ready"
    term_seen = tmp_path / "heavy.term"
    child_pid_path = tmp_path / "heavy-child.pid"
    resumed = tmp_path / "heavy.resumed"
    interactive_started = tmp_path / "interactive.started"
    heavy_first = f"""
import pathlib, signal, subprocess, sys, time
ready = pathlib.Path({str(ready)!r})
term_seen = pathlib.Path({str(term_seen)!r})
child_pid_path = pathlib.Path({str(child_pid_path)!r})
def on_term(_signum, _frame):
    term_seen.write_text('term')
signal.signal(signal.SIGTERM, on_term)
child = subprocess.Popen([
    sys.executable,
    '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)',
])
child_pid_path.write_text(str(child.pid))
ready.write_text('ready')
while True:
    time.sleep(0.1)
"""

    def factory(job, lease):
        if job.job_id == "heavy" and lease.attempt_number == 1:
            code = heavy_first
        elif job.job_id == "heavy":
            code = f"from pathlib import Path; Path({str(resumed)!r}).write_text('once')"
        else:
            code = (
                "from pathlib import Path; "
                f"Path({str(interactive_started)!r}).write_text('started')"
            )
        return WorkerSpec(
            job_id=job.job_id,
            worker_class=job.worker_class,
            argv=(sys.executable, "-c", code),
            estimated_footprint_mb=16,
            cpu_percent=10,
            priority_class=job.priority_class,
            timeout_sec=10,
        )

    create_priority_job(
        ledger,
        base,
        job_id="heavy",
        capability="message",
        worker_class="integration",
        priority_class="P3",
        max_attempts=1,
    )
    dispatcher = make_dispatcher(
        ledger,
        factory,
        preemption_grace_sec=0.05,
    )
    first = dispatcher.dispatch_next(now=base)
    assert first is not None
    wait_for_path(ready)
    child_pid = int(child_pid_path.read_text())

    create_priority_job(
        ledger,
        base,
        job_id="interactive",
        capability="dispatcher_test",
        worker_class="light",
        priority_class="P0",
        memory_mb=16,
    )
    started = time.monotonic()
    interactive = dispatcher.dispatch_next(
        now=base + timedelta(seconds=1),
        interactive=True,
    )
    latency = time.monotonic() - started

    assert interactive is not None and interactive.lease.job.job_id == "interactive"
    assert 0.04 <= latency < 1.0
    assert len(interactive.preemptions) == 1
    preemption = interactive.preemptions[0]
    assert preemption.job.job_id == "heavy"
    assert preemption.job.status is JobStatus.QUEUED
    assert preemption.worker_result.killed is True
    assert preemption.worker_result.process_group_gone is True
    assert term_seen.read_text() == "term"
    assert_pid_gone(first.worker_pid)
    assert_pid_gone(child_pid)
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute(
            "SELECT status FROM attempts WHERE job_id='heavy' AND attempt_number=1"
        ).fetchone()[0] == "preempted"
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 1

    interactive_result = dispatcher.supervisor.wait("interactive")
    dispatcher.commit_result(
        "interactive",
        interactive_result,
        now=base + timedelta(seconds=2),
    )
    retry = dispatcher.dispatch_next(now=base + timedelta(seconds=3))
    assert retry is not None
    assert retry.lease.job.job_id == "heavy"
    assert retry.lease.attempt_number == 2
    retry_result = dispatcher.supervisor.wait("heavy")
    completed = dispatcher.commit_result(
        "heavy",
        retry_result,
        now=base + timedelta(seconds=4),
    )

    assert completed.job.status is JobStatus.SUCCEEDED
    assert resumed.read_text() == "once"
    assert interactive_started.read_text() == "started"
    assert dispatcher.active_job_ids() == ()
    assert dispatcher.supervisor.active_job_ids() == ()
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
        assert conn.execute(
            "SELECT GROUP_CONCAT(status, ',') FROM attempts WHERE job_id='heavy' "
            "ORDER BY attempt_number"
        ).fetchone()[0] == "preempted,succeeded"


def test_termination_failure_cancels_fence_and_defers_incoming(
    ledger: JobLedger,
    tmp_path: Path,
) -> None:
    base = datetime.now(timezone.utc)
    ready = tmp_path / "terminate-fault.ready"

    class TerminationFaultSupervisor(WorkerSupervisor):
        fail_termination = True

        def terminate(self, job_id: str, *, grace_sec: float = 5.0):
            if self.fail_termination:
                raise SupervisorError("simulated preemption termination failure")
            return super().terminate(job_id, grace_sec=grace_sec)

    def factory(job, lease):
        code = (
            f"from pathlib import Path; Path({str(ready)!r}).write_text('ready'); "
            "import time; time.sleep(30)"
            if job.job_id == "heavy"
            else "pass"
        )
        return WorkerSpec(
            job_id=job.job_id,
            worker_class=job.worker_class,
            argv=(sys.executable, "-c", code),
            estimated_footprint_mb=16,
            cpu_percent=10,
            priority_class=job.priority_class,
            timeout_sec=10,
        )

    create_priority_job(
        ledger,
        base,
        job_id="heavy",
        capability="message",
        worker_class="integration",
        priority_class="P3",
    )
    supervisor = TerminationFaultSupervisor(GlobalResourceGovernor())
    dispatcher = make_dispatcher(ledger, factory, supervisor=supervisor)
    dispatcher.dispatch_next(now=base)
    wait_for_path(ready)
    create_priority_job(
        ledger,
        base,
        job_id="interactive",
        capability="dispatcher_test",
        worker_class="light",
        priority_class="P0",
        memory_mb=16,
    )

    with pytest.raises(SupervisorError, match="termination failure"):
        dispatcher.dispatch_next(
            now=base + timedelta(seconds=1),
            interactive=True,
        )

    assert ledger.get_job("heavy").status is JobStatus.RUNNING
    assert ledger.get_job("interactive").status is JobStatus.DEFERRED
    assert dispatcher.active_job_ids() == ("heavy",)
    with sqlite3.connect(ledger.path) as conn:
        lease = conn.execute(
            "SELECT fenced_at, fence_reason FROM leases WHERE job_id='heavy'"
        ).fetchone()
        assert lease == (None, None)
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 1

    supervisor.fail_termination = False
    result = supervisor.terminate("heavy", grace_sec=0.05)
    dispatcher.commit_result(
        "heavy",
        result,
        now=base + timedelta(seconds=2),
    )


def test_interactive_preemption_is_single_owner(
    ledger: JobLedger,
    tmp_path: Path,
) -> None:
    base = datetime.now(timezone.utc)
    ready = tmp_path / "owner-scope.ready"

    def factory(job, lease):
        code = (
            f"from pathlib import Path; Path({str(ready)!r}).write_text('ready'); "
            "import time; time.sleep(30)"
            if job.job_id == "heavy"
            else "pass"
        )
        return WorkerSpec(
            job_id=job.job_id,
            worker_class=job.worker_class,
            argv=(sys.executable, "-c", code),
            estimated_footprint_mb=16,
            cpu_percent=10,
            priority_class=job.priority_class,
            timeout_sec=10,
        )

    create_priority_job(
        ledger,
        base,
        job_id="heavy",
        capability="message",
        worker_class="integration",
        priority_class="P3",
        side_effect_class="read_only",
    )
    supervisor = WorkerSupervisor(GlobalResourceGovernor())
    owner = make_dispatcher(
        ledger,
        factory,
        supervisor=supervisor,
        owner_id="owner-a",
    )
    owner.dispatch_next(now=base)
    wait_for_path(ready)
    create_priority_job(
        ledger,
        base,
        job_id="interactive",
        capability="dispatcher_test",
        worker_class="light",
        priority_class="P0",
        memory_mb=16,
    )
    other_owner = make_dispatcher(
        ledger,
        factory,
        supervisor=supervisor,
        owner_id="owner-b",
    )

    interactive = other_owner.dispatch_next(
        now=base + timedelta(seconds=1),
        interactive=True,
    )

    assert interactive is not None and interactive.preemptions == ()
    assert ledger.get_job("heavy").status is JobStatus.RUNNING
    assert set(supervisor.active_job_ids()) == {"heavy", "interactive"}
    interactive_result = supervisor.wait("interactive")
    other_owner.commit_result(
        "interactive",
        interactive_result,
        now=base + timedelta(seconds=2),
    )
    heavy_result = supervisor.terminate("heavy", grace_sec=0.05)
    owner.commit_result("heavy", heavy_result, now=base + timedelta(seconds=3))


def test_requeue_transaction_failure_marks_recovery_and_never_starts_incoming(
    ledger: JobLedger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = datetime.now(timezone.utc)
    ready = tmp_path / "requeue-fault.ready"
    incoming_started = tmp_path / "must-not-start"

    def factory(job, lease):
        code = (
            f"from pathlib import Path; Path({str(ready)!r}).write_text('ready'); "
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
            if job.job_id == "heavy"
            else f"from pathlib import Path; Path({str(incoming_started)!r}).write_text('bad')"
        )
        return WorkerSpec(
            job_id=job.job_id,
            worker_class=job.worker_class,
            argv=(sys.executable, "-c", code),
            estimated_footprint_mb=16,
            cpu_percent=10,
            priority_class=job.priority_class,
            timeout_sec=10,
        )

    create_priority_job(
        ledger,
        base,
        job_id="heavy",
        capability="message",
        worker_class="integration",
        priority_class="P3",
    )
    dispatcher = make_dispatcher(
        ledger,
        factory,
        preemption_grace_sec=0.05,
    )
    heavy = dispatcher.dispatch_next(now=base)
    assert heavy is not None
    wait_for_path(ready)
    create_priority_job(
        ledger,
        base,
        job_id="interactive",
        capability="dispatcher_test",
        worker_class="light",
        priority_class="P0",
        memory_mb=16,
    )

    def fail_requeue(*_args, **_kwargs):
        raise LedgerError("simulated requeue transaction failure")

    monkeypatch.setattr(ledger, "resolve_preempted_lease", fail_requeue)
    with pytest.raises(SupervisorError, match="automatic requeue failed"):
        dispatcher.dispatch_next(
            now=base + timedelta(seconds=1),
            interactive=True,
        )

    assert_pid_gone(heavy.worker_pid)
    victim = ledger.get_job("heavy")
    incoming = ledger.get_job("interactive")
    assert victim.status is JobStatus.DEFERRED
    assert victim.error["code"] == "preemption_requeue_recovery_required"
    assert incoming.status is JobStatus.DEFERRED
    assert incoming.error["code"] == "interactive_preemption_failed"
    assert not incoming_started.exists()
    assert dispatcher.active_job_ids() == ()
    assert dispatcher.supervisor.active_job_ids() == ()
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM attempts WHERE job_id='heavy'"
        ).fetchone()[0] == "preempted_recovery_required"


def test_incoming_start_failure_leaves_victim_requeued_and_incoming_recoverable(
    ledger: JobLedger,
    tmp_path: Path,
) -> None:
    base = datetime.now(timezone.utc)
    ready = tmp_path / "start-fault.ready"

    class IncomingStartFaultSupervisor(WorkerSupervisor):
        def start(self, spec, snapshot):
            if spec.job_id == "interactive":
                raise OSError("simulated interactive spawn failure")
            return super().start(spec, snapshot)

    def factory(job, lease):
        code = (
            f"from pathlib import Path; Path({str(ready)!r}).write_text('ready'); "
            "import time; time.sleep(30)"
            if job.job_id == "heavy"
            else "pass"
        )
        return WorkerSpec(
            job_id=job.job_id,
            worker_class=job.worker_class,
            argv=(sys.executable, "-c", code),
            estimated_footprint_mb=16,
            cpu_percent=10,
            priority_class=job.priority_class,
            timeout_sec=10,
        )

    create_priority_job(
        ledger,
        base,
        job_id="heavy",
        capability="message",
        worker_class="integration",
        priority_class="P3",
    )
    supervisor = IncomingStartFaultSupervisor(GlobalResourceGovernor())
    dispatcher = make_dispatcher(
        ledger,
        factory,
        supervisor=supervisor,
        preemption_grace_sec=0.05,
    )
    dispatcher.dispatch_next(now=base)
    wait_for_path(ready)
    create_priority_job(
        ledger,
        base,
        job_id="interactive",
        capability="dispatcher_test",
        worker_class="light",
        priority_class="P0",
        memory_mb=16,
    )

    with pytest.raises(OSError, match="interactive spawn failure"):
        dispatcher.dispatch_next(
            now=base + timedelta(seconds=1),
            interactive=True,
        )

    assert ledger.get_job("heavy").status is JobStatus.QUEUED
    incoming = ledger.get_job("interactive")
    assert incoming.status is JobStatus.DEFERRED
    assert incoming.error["code"] == "interactive_worker_start_recovery_required"
    assert dispatcher.active_job_ids() == ()
    assert supervisor.active_job_ids() == ()
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
