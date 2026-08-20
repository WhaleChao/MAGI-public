from __future__ import annotations

import sqlite3
import threading
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from magi_v3.errors import InvalidTransition, LeaseConflict, LedgerError
from magi_v3.ledger import (
    _MIGRATION_1,
    _MIGRATION_2,
    _MIGRATION_3,
    JobLedger,
    JobSpec,
    OutboxSpec,
)
from magi_v3.state import JobStatus
from scripts.v3_validation.paths import JOB_ENVELOPE_SCHEMA_PATH
from scripts.v3_validation.schema import load_json, validate_json


@pytest.fixture
def ledger(tmp_path: Path) -> JobLedger:
    value = JobLedger(tmp_path / "state" / "ledger.sqlite3")
    value.initialize()
    return value


def test_initialize_is_idempotent_and_enables_wal(ledger: JobLedger) -> None:
    ledger.initialize()
    assert ledger.schema_version() == 4
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"jobs", "attempts", "leases", "outbox", "schema_migrations"} <= tables


def test_schema_v4_migrates_existing_v3_jobs_without_losing_payload(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v3.sqlite3"
    base = datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc)
    timestamp = base.isoformat(timespec="milliseconds")
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.executescript(_MIGRATION_1)
        conn.executescript(_MIGRATION_2)
        conn.executescript(_MIGRATION_3)
        conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(1, timestamp), (2, timestamp), (3, timestamp)],
        )
        conn.execute(
            """
            INSERT INTO jobs(
                job_id, capability, operation, worker_class, side_effect_class,
                priority, status, input_json, created_at, scheduled_for,
                max_attempts, timeout_sec, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "operations_health_security",
                "probe",
                "maintenance",
                "read_only",
                70,
                "queued",
                json.dumps({"preserved": True}),
                timestamp,
                timestamp,
                2,
                30,
                timestamp,
            ),
        )

    migrated = JobLedger(path)
    migrated.initialize()
    record = migrated.get_job("legacy-job")

    assert migrated.schema_version() == 4
    assert record.input == {"preserved": True}
    assert record.priority_class == "P2"
    assert record.not_before == timestamp
    assert record.queue_ttl_sec == 86400
    assert record.commit_phase == "not_applicable"
    assert record.resource_claim == {
        "memory_mb": 0,
        "metal_mb": 0,
        "cpu_percent": 0,
        "disk_io": "none",
        "nas_io": "none",
        "network": "none",
        "browser_tokens": 0,
    }
    validate_json(
        record.to_envelope(),
        load_json(JOB_ENVELOPE_SCHEMA_PATH),
        label="migrated job",
    )


def test_canonical_job_envelope_round_trips_and_validates_schema(ledger: JobLedger) -> None:
    base = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
    claim = {
        "memory_mb": 256,
        "metal_mb": 0,
        "cpu_percent": 50,
        "disk_io": "light",
        "nas_io": "none",
        "network": "light",
        "browser_tokens": 0,
    }
    created = ledger.create_job(
        JobSpec(
            job_id="canonical-round-trip",
            capability="operations_health_security",
            operation="reconcile",
            worker_class="maintenance",
            side_effect_class="reversible_write",
            priority_class="P2",
            scheduled_for=base,
            not_before=base + timedelta(seconds=5),
            latest_start_at=base + timedelta(seconds=60),
            deadline_at=base + timedelta(seconds=90),
            timeout_sec=30,
            queue_ttl_sec=60,
            resource_claim=claim,
            idempotency_key="canonical-round-trip:1",
            input={"scope": "offline"},
        ),
        now=base,
    )
    loaded = JobLedger(ledger.path).get_job(created.job_id)
    envelope = loaded.to_envelope()

    assert loaded == created
    assert loaded.priority_class == "P2"
    assert loaded.priority == 70
    assert loaded.preemptible is True
    assert loaded.resource_claim == claim
    assert loaded.commit_phase == "prepared"
    assert loaded.timeout_sec == 30
    assert loaded.queue_ttl_sec == 60
    validate_json(envelope, load_json(JOB_ENVELOPE_SCHEMA_PATH), label="persisted job")


def test_verified_commit_persists_receipts_artifacts_and_metrics(ledger: JobLedger) -> None:
    base = datetime(2026, 7, 14, 5, 0, tzinfo=timezone.utc)
    token = "canonical-confirmation-token-0001"
    created = ledger.create_job(
        JobSpec(
            job_id="canonical-commit",
            capability="channels",
            operation="send",
            worker_class="light",
            side_effect_class="external_commit",
            priority_class="P1",
            scheduled_for=base,
            timeout_sec=30,
            queue_ttl_sec=60,
            idempotency_key="canonical-commit:1",
            confirmation_token=token,
            confirmation_expires_at=base + timedelta(minutes=1),
            input={"message": "offline fixture"},
        ),
        now=base,
    )
    ledger.confirm_job(created.job_id, token, now=base)
    lease = ledger.lease_next("offline-test", now=base)
    assert lease is not None
    ledger.mark_running(
        lease.token,
        owner_id=lease.owner_id,
        attempt_number=lease.attempt_number,
        now=base,
    )
    finished = ledger._commit_worker_result(
        lease.token,
        JobStatus.SUCCEEDED,
        owner_id=lease.owner_id,
        attempt_number=lease.attempt_number,
        result={
            "artifacts": [{"kind": "receipt", "uri": "memory://artifact/1"}],
            "side_effect_receipts": [
                {
                    "kind": "provider_message",
                    "reference": "offline-provider-1",
                    "committed_at": (base + timedelta(seconds=1)).isoformat(),
                    "idempotency_key": "canonical-commit:1",
                }
            ],
        },
        metrics={"duration_ms": 1000, "peak_footprint_mb": 42.5},
        business_completed=True,
        now=base + timedelta(seconds=1),
    )

    assert finished.commit_phase == "verified"
    assert finished.ambiguous_side_effect is False
    assert finished.artifacts == [{"kind": "receipt", "uri": "memory://artifact/1"}]
    assert finished.side_effect_receipts[0]["reference"] == "offline-provider-1"
    assert finished.metrics == {"duration_ms": 1000, "peak_footprint_mb": 42.5}
    validate_json(
        finished.to_envelope(),
        load_json(JOB_ENVELOPE_SCHEMA_PATH),
        label="committed job",
    )


def test_canonical_job_fields_fail_closed_on_invalid_input_or_stored_data(
    ledger: JobLedger,
) -> None:
    base = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)
    invalid_claim = {
        "memory_mb": 1,
        "metal_mb": 0,
        "cpu_percent": 1,
        "disk_io": "none",
        "nas_io": "none",
        "network": "none",
    }
    with pytest.raises(ValueError, match="resource_claim"):
        ledger.create_job(
            JobSpec(
                capability="operations_health_security",
                operation="invalid",
                worker_class="maintenance",
                resource_claim=invalid_claim,
                input={},
            ),
            now=base,
        )
    with pytest.raises(ValueError, match="deadline_at"):
        ledger.create_job(
            JobSpec(
                capability="operations_health_security",
                operation="invalid-deadline",
                worker_class="maintenance",
                scheduled_for=base,
                latest_start_at=base + timedelta(seconds=60),
                deadline_at=base + timedelta(seconds=61),
                timeout_sec=30,
                queue_ttl_sec=60,
                input={},
            ),
            now=base,
        )

    valid = ledger.create_job(
        JobSpec(
            capability="operations_health_security",
            operation="tamper-test",
            worker_class="maintenance",
            input={},
        ),
        now=base,
    )
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(
            "UPDATE jobs SET resource_claim_json='{}' WHERE job_id=?",
            (valid.job_id,),
        )
    with pytest.raises(LedgerError, match="canonical envelope invariants"):
        ledger.get_job(valid.job_id)


def test_job_past_latest_start_is_not_leased(ledger: JobLedger) -> None:
    base = datetime(2026, 7, 14, 7, 0, tzinfo=timezone.utc)
    job = ledger.create_job(
        JobSpec(
            job_id="expired-before-lease",
            capability="operations_health_security",
            operation="deadline-gate",
            worker_class="maintenance",
            scheduled_for=base,
            queue_ttl_sec=1,
            timeout_sec=1,
            input={},
        ),
        now=base,
    )

    assert job.latest_start_at == (base + timedelta(seconds=1)).isoformat(timespec="milliseconds")
    assert ledger.lease_next("offline-test", now=base + timedelta(seconds=2)) is None
    assert ledger.get_job(job.job_id).status is JobStatus.QUEUED


def test_write_classes_require_contract_idempotency_key(ledger: JobLedger) -> None:
    for side_effect in (
        "local_draft",
        "reversible_write",
        "external_commit",
        "destructive",
    ):
        with pytest.raises(ValueError, match="idempotency"):
            ledger.create_job(
                JobSpec(
                    capability="case",
                    operation="update",
                    worker_class="integration",
                    side_effect_class=side_effect,
                    input={},
                )
            )


def test_lease_run_and_complete_records_attempt(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    created = ledger.create_job(
        JobSpec(
            job_id="job-1",
            capability="health",
            operation="reconcile",
            worker_class="light",
            side_effect_class="read_only",
            input={"scope": "local"},
            scheduled_for=base,
            max_attempts=2,
        )
    )
    assert created.status is JobStatus.QUEUED

    lease = ledger.lease_next("supervisor-a", now=base + timedelta(milliseconds=1))
    assert lease is not None
    assert lease.job.job_id == "job-1"
    assert lease.job.status is JobStatus.LEASED

    running = ledger.mark_running(
        lease.token,
        owner_id=lease.owner_id,
        attempt_number=lease.attempt_number,
        worker_pid=1234,
        now=base + timedelta(seconds=1),
    )
    assert running.status is JobStatus.RUNNING
    completed = ledger._commit_worker_result(
        lease.token,
        JobStatus.SUCCEEDED,
        owner_id=lease.owner_id,
        attempt_number=lease.attempt_number,
        result={"ok": True},
        metrics={"peak_footprint_mb": 20},
        business_completed=True,
        now=base + timedelta(seconds=2),
    )
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.business_completed is True
    assert completed.result == {"ok": True}

    with pytest.raises(LeaseConflict):
        ledger.heartbeat_lease(
            lease.token,
            owner_id=lease.owner_id,
            attempt_number=lease.attempt_number,
            extend_seconds=60,
        )
    with sqlite3.connect(ledger.path) as conn:
        attempt = conn.execute(
            "SELECT status, worker_pid, metrics_json FROM attempts WHERE job_id='job-1'"
        ).fetchone()
        active_leases = conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
    assert attempt[0] == "succeeded"
    assert attempt[1] == 1234
    assert "peak_footprint_mb" in attempt[2]
    assert active_leases == 0


def test_lease_claim_is_single_active(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    ledger.create_job(
        JobSpec(
            job_id="one-owner",
            capability="x",
            operation="y",
            worker_class="light",
            input={},
            scheduled_for=base,
        )
    )
    first = ledger.lease_next("a", now=base + timedelta(seconds=1))
    second = JobLedger(ledger.path).lease_next("b", now=base + timedelta(seconds=1))

    assert first is not None
    assert second is None


def test_concurrent_lease_claim_has_exactly_one_winner(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    ledger.create_job(
        JobSpec(
            job_id="race",
            capability="x",
            operation="y",
            worker_class="light",
            input={},
            scheduled_for=base,
        )
    )
    barrier = threading.Barrier(3)
    results: list[object] = []
    errors: list[BaseException] = []

    def claim(owner: str) -> None:
        try:
            barrier.wait()
            results.append(
                JobLedger(ledger.path).lease_next(
                    owner,
                    worker_classes=["light"],
                    now=base + timedelta(seconds=1),
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert sum(item is not None for item in results) == 1


def test_expired_read_only_requeues_but_external_commit_stops(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    confirmation_token = "unsafe-confirmation-token-0001"
    ledger.create_job(
        JobSpec(
            job_id="safe",
            capability="search",
            operation="read",
            worker_class="integration",
            side_effect_class="read_only",
            input={},
            scheduled_for=base,
            max_attempts=2,
        )
    )
    safe_lease = ledger.lease_next(
        "worker", worker_classes=["integration"], lease_seconds=1, now=base
    )
    assert safe_lease is not None
    fenced = ledger.fence_lease(
        safe_lease.token,
        owner_id=safe_lease.owner_id,
        attempt_number=safe_lease.attempt_number,
        now=base + timedelta(seconds=2),
    )
    assert ledger.get_job("safe").status is JobStatus.LEASED
    assert ledger.resolve_fenced_lease(
        safe_lease.token,
        owner_id=safe_lease.owner_id,
        attempt_number=safe_lease.attempt_number,
        fence_generation=fenced.fence_generation,
        process_group_gone=True,
        now=base + timedelta(seconds=2),
    ).status is JobStatus.QUEUED

    ledger.create_job(
        JobSpec(
            job_id="unsafe",
            capability="message",
            operation="send",
            worker_class="integration",
            side_effect_class="external_commit",
            idempotency_key="message:1",
            input={},
            scheduled_for=base,
            max_attempts=2,
            priority_class="P0",
            confirmation_token=confirmation_token,
            confirmation_expires_at=base + timedelta(minutes=5),
        ),
        now=base,
    )
    assert ledger.confirm_job("unsafe", confirmation_token, now=base).status is JobStatus.QUEUED
    unsafe_lease = ledger.lease_next(
        "worker", worker_classes=["integration"], lease_seconds=1, now=base
    )
    assert unsafe_lease is not None
    fenced = ledger.fence_lease(
        unsafe_lease.token,
        owner_id=unsafe_lease.owner_id,
        attempt_number=unsafe_lease.attempt_number,
        now=base + timedelta(seconds=2),
    )
    ledger.resolve_fenced_lease(
        unsafe_lease.token,
        owner_id=unsafe_lease.owner_id,
        attempt_number=unsafe_lease.attempt_number,
        fence_generation=fenced.fence_generation,
        process_group_gone=True,
        now=base + timedelta(seconds=2),
    )
    assert ledger.get_job("unsafe").status is JobStatus.NEEDS_CONFIRMATION
    assert ledger.get_job("unsafe").confirmed_at is None
    with pytest.raises(InvalidTransition, match="confirm_job"):
        ledger.transition_job("unsafe", JobStatus.QUEUED, now=base + timedelta(seconds=2))
    replacement_token = "unsafe-reconfirmation-token-0002"
    ledger.issue_confirmation_challenge(
        "unsafe",
        replacement_token,
        base + timedelta(minutes=6),
        now=base + timedelta(seconds=2),
    )
    assert ledger.confirm_job(
        "unsafe",
        replacement_token,
        now=base + timedelta(seconds=3),
    ).status is JobStatus.QUEUED


def test_priority_preemption_atomically_requeues_and_refunds_attempt(
    ledger: JobLedger,
) -> None:
    base = datetime.now(timezone.utc)
    ledger.create_job(
        JobSpec(
            job_id="preemptible-heavy",
            capability="search",
            operation="index",
            worker_class="integration",
            side_effect_class="read_only",
            priority_class="P3",
            input={},
            scheduled_for=base,
            max_attempts=1,
        ),
        now=base,
    )
    first = ledger.lease_next(
        "single-owner",
        worker_classes=("integration",),
        now=base,
    )
    assert first is not None
    ledger.mark_running(
        first.token,
        owner_id=first.owner_id,
        attempt_number=first.attempt_number,
        worker_pid=4321,
        now=base,
    )

    fence = ledger.fence_preemptible_lease(
        first.token,
        owner_id=first.owner_id,
        attempt_number=first.attempt_number,
        incoming_priority_class="P0",
        now=base + timedelta(milliseconds=10),
    )
    with pytest.raises(LeaseConflict):
        ledger.heartbeat_lease(
            first.token,
            owner_id=first.owner_id,
            attempt_number=first.attempt_number,
            extend_seconds=10,
            now=base + timedelta(milliseconds=20),
        )
    requeued = ledger.resolve_preempted_lease(
        first.token,
        owner_id=first.owner_id,
        attempt_number=first.attempt_number,
        fence_generation=fence.fence_generation,
        process_group_gone=True,
        incoming_priority_class="P0",
        now=base + timedelta(milliseconds=30),
    )

    assert requeued.status is JobStatus.QUEUED
    assert requeued.error["code"] == "worker_preempted_requeued"
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM attempts WHERE job_id='preemptible-heavy'"
        ).fetchone()[0] == "preempted"

    # A preempted attempt is an interrupted scheduling slice, not a business
    # failure, so max_attempts=1 must still permit exactly one real retry.
    second = ledger.lease_next(
        "single-owner",
        worker_classes=("integration",),
        now=base + timedelta(milliseconds=40),
    )
    assert second is not None and second.attempt_number == 2


def test_priority_preemption_rejects_write_side_effect_worker(
    ledger: JobLedger,
) -> None:
    base = datetime.now(timezone.utc)
    ledger.create_job(
        JobSpec(
            job_id="unsafe-heavy",
            capability="case",
            operation="write",
            worker_class="integration",
            side_effect_class="local_draft",
            priority_class="P3",
            idempotency_key="unsafe-heavy:fixture",
            input={},
            scheduled_for=base,
        ),
        now=base,
    )
    lease = ledger.lease_next(
        "single-owner",
        worker_classes=("integration",),
        now=base,
    )
    assert lease is not None
    ledger.mark_running(
        lease.token,
        owner_id=lease.owner_id,
        attempt_number=lease.attempt_number,
        now=base,
    )
    with pytest.raises(LeaseConflict, match="safe preemptible"):
        ledger.fence_preemptible_lease(
            lease.token,
            owner_id=lease.owner_id,
            attempt_number=lease.attempt_number,
            incoming_priority_class="P0",
            now=base + timedelta(milliseconds=10),
        )


def test_succeeded_without_business_completed_is_rejected(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    ledger.create_job(
        JobSpec(
            capability="x",
            operation="y",
            worker_class="light",
            input={},
            scheduled_for=base,
        )
    )
    lease = ledger.lease_next("owner", now=base + timedelta(seconds=1))
    assert lease is not None
    ledger.mark_running(
        lease.token,
        owner_id=lease.owner_id,
        attempt_number=lease.attempt_number,
        now=base + timedelta(seconds=2),
    )
    with pytest.raises(InvalidTransition):
        ledger._commit_worker_result(
            lease.token,
            JobStatus.SUCCEEDED,
            owner_id=lease.owner_id,
            attempt_number=lease.attempt_number,
        )


def test_failed_and_timed_out_require_error_payload(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    ledger.create_job(
        JobSpec(
            capability="x",
            operation="y",
            worker_class="light",
            input={},
            scheduled_for=base,
        )
    )
    lease = ledger.lease_next("owner", now=base + timedelta(seconds=1))
    assert lease is not None
    ledger.mark_running(
        lease.token,
        owner_id=lease.owner_id,
        attempt_number=lease.attempt_number,
        now=base + timedelta(seconds=2),
    )
    with pytest.raises(InvalidTransition, match="error payload"):
        ledger._commit_worker_result(
            lease.token,
            JobStatus.FAILED,
            owner_id=lease.owner_id,
            attempt_number=lease.attempt_number,
        )


def test_generic_transition_cannot_bypass_active_lease(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    job = ledger.create_job(
        JobSpec(
            capability="x",
            operation="y",
            worker_class="light",
            input={},
            scheduled_for=base,
        )
    )
    assert ledger.lease_next("owner", now=base + timedelta(seconds=1)) is not None
    with pytest.raises(LeaseConflict, match="token-bound"):
        ledger.transition_job(job.job_id, JobStatus.DEFERRED)


def test_outbox_claim_is_idempotent_and_owner_checked(ledger: JobLedger) -> None:
    record = ledger.enqueue_outbox(
        topic="notification",
        payload={"text": "hello"},
        idempotency_key="notify:1",
    )
    with pytest.raises(LedgerError):
        ledger.enqueue_outbox(
            topic="notification",
            payload={"text": "hello"},
            idempotency_key="notify:1",
        )
    claimed = ledger.claim_outbox("sender-a")
    assert claimed is not None
    assert claimed.outbox_id == record.outbox_id
    assert claimed.status == "sending"
    with pytest.raises(LeaseConflict):
        ledger.mark_outbox_sent(
            record.outbox_id,
            "sender-b",
            claim_token=claimed.claim_token,
            claim_generation=claimed.claim_generation,
        )
    sent = ledger.mark_outbox_sent(
        record.outbox_id,
        "sender-a",
        claim_token=claimed.claim_token,
        claim_generation=claimed.claim_generation,
        provider_reference="provider-42",
    )
    assert sent.status == "sent"
    assert ledger.claim_outbox("sender-a") is None


@pytest.mark.parametrize("side_effect", ["external_commit", "destructive"])
def test_dangerous_job_requires_hashed_confirmation_before_lease(
    ledger: JobLedger,
    side_effect: str,
) -> None:
    base = datetime.now(timezone.utc)
    token = f"{side_effect}-challenge-token-0001"
    with pytest.raises(ValueError, match="confirmation challenge"):
        ledger.create_job(
            JobSpec(
                capability="danger",
                operation="commit",
                worker_class="integration",
                side_effect_class=side_effect,
                idempotency_key=f"danger:{side_effect}:missing",
                input={},
            ),
            now=base,
        )

    created = ledger.create_job(
        JobSpec(
            job_id=f"danger-{side_effect}",
            capability="danger",
            operation="commit",
            worker_class="integration",
            side_effect_class=side_effect,
            idempotency_key=f"danger:{side_effect}",
            confirmation_token=token,
            confirmation_expires_at=base + timedelta(minutes=5),
            input={},
            scheduled_for=base,
        ),
        now=base,
    )
    assert created.status is JobStatus.NEEDS_CONFIRMATION
    assert ledger.lease_next("worker", now=base) is None

    with sqlite3.connect(ledger.path) as conn:
        stored = conn.execute(
            "SELECT confirmation_token_sha256 FROM jobs WHERE job_id=?",
            (created.job_id,),
        ).fetchone()[0]
        rendered = " ".join(str(value) for row in conn.execute("SELECT * FROM jobs") for value in row)
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert token not in rendered
    assert token not in repr(created)

    with pytest.raises(InvalidTransition, match="confirm_job"):
        ledger.transition_job(created.job_id, JobStatus.QUEUED, now=base)
    with pytest.raises(LedgerError, match="confirmation rejected"):
        ledger.confirm_job(created.job_id, "wrong-confirmation-token", now=base)

    confirmed = ledger.confirm_job(created.job_id, token, now=base)
    assert confirmed.status is JobStatus.QUEUED
    assert confirmed.confirmed_at is not None
    with sqlite3.connect(ledger.path) as conn:
        consumed = conn.execute(
            "SELECT confirmation_token_sha256, confirmation_expires_at FROM jobs WHERE job_id=?",
            (created.job_id,),
        ).fetchone()
    assert consumed == (None, None)
    assert ledger.lease_next("worker", now=base) is not None


def test_expired_confirmation_is_rejected(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    token = "expiring-confirmation-token-0001"
    job = ledger.create_job(
        JobSpec(
            capability="danger",
            operation="commit",
            worker_class="integration",
            side_effect_class="external_commit",
            idempotency_key="danger:expired",
            confirmation_token=token,
            confirmation_expires_at=base + timedelta(seconds=1),
            input={},
            scheduled_for=base,
        ),
        now=base,
    )
    with pytest.raises(LedgerError, match="confirmation rejected"):
        ledger.confirm_job(job.job_id, token, now=base + timedelta(seconds=2))
    assert ledger.get_job(job.job_id).status is JobStatus.NEEDS_CONFIRMATION


@pytest.mark.parametrize("suspended", [JobStatus.WAITING_CHILDREN, JobStatus.AWAITING_INPUT])
def test_suspended_job_must_requeue_and_obtain_a_new_lease(
    ledger: JobLedger,
    suspended: JobStatus,
) -> None:
    base = datetime.now(timezone.utc)
    job = ledger.create_job(
        JobSpec(
            capability="workflow",
            operation="pause",
            worker_class="light",
            input={},
            scheduled_for=base,
            max_attempts=2,
        )
    )
    first = ledger.lease_next("worker-1", now=base)
    assert first is not None
    ledger.mark_running(
        first.token,
        owner_id=first.owner_id,
        attempt_number=first.attempt_number,
        now=base,
    )
    assert ledger.suspend_lease(
        first.token,
        suspended,
        owner_id=first.owner_id,
        attempt_number=first.attempt_number,
        now=base,
    ).status is suspended

    with pytest.raises(InvalidTransition):
        ledger.transition_job(job.job_id, JobStatus.RUNNING, now=base)
    assert ledger.transition_job(job.job_id, JobStatus.QUEUED, now=base).status is JobStatus.QUEUED
    second = ledger.lease_next("worker-2", now=base)
    assert second is not None
    assert second.attempt_number == 2
    assert ledger.mark_running(
        second.token,
        owner_id=second.owner_id,
        attempt_number=second.attempt_number,
        now=base,
    ).status is JobStatus.RUNNING


def test_expired_or_stale_outbox_claim_cannot_ack_new_generation(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    record = ledger.enqueue_outbox(
        topic="notification",
        payload={"text": "hello"},
        idempotency_key="notify:fenced",
        now=base,
    )
    first = ledger.claim_outbox("sender", lease_seconds=1, now=base)
    assert first is not None and first.claim_token
    with sqlite3.connect(ledger.path) as conn:
        stored_digest = conn.execute(
            "SELECT claim_token_sha256 FROM outbox WHERE outbox_id=?",
            (record.outbox_id,),
        ).fetchone()[0]
    assert stored_digest == hashlib.sha256(first.claim_token.encode()).hexdigest()
    assert stored_digest != first.claim_token
    with pytest.raises(LeaseConflict):
        ledger.mark_outbox_sent(
            record.outbox_id,
            "sender",
            claim_token=first.claim_token,
            claim_generation=first.claim_generation,
            now=base + timedelta(seconds=2),
        )

    second = ledger.claim_outbox("sender", lease_seconds=10, now=base + timedelta(seconds=2))
    assert second is not None and second.claim_token
    assert second.claim_generation == first.claim_generation + 1
    assert second.claim_token != first.claim_token
    with pytest.raises(LeaseConflict):
        ledger.mark_outbox_sent(
            record.outbox_id,
            "sender",
            claim_token=first.claim_token,
            claim_generation=first.claim_generation,
            now=base + timedelta(seconds=3),
        )
    assert ledger.mark_outbox_sent(
        record.outbox_id,
        "sender",
        claim_token=second.claim_token,
        claim_generation=second.claim_generation,
        now=base + timedelta(seconds=3),
    ).status == "sent"


def test_outbox_ttl_and_max_attempts_dead_letter(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    expired = ledger.enqueue_outbox(
        topic="notification",
        payload={},
        idempotency_key="notify:ttl",
        ttl_seconds=1,
        now=base,
    )
    assert ledger.claim_outbox("sender", now=base + timedelta(seconds=2)) is None
    assert ledger.get_outbox(expired.outbox_id).dead_lettered_at is not None

    limited = ledger.enqueue_outbox(
        topic="notification",
        payload={},
        idempotency_key="notify:max-attempts",
        max_attempts=1,
        now=base + timedelta(seconds=3),
    )
    claim = ledger.claim_outbox("sender", now=base + timedelta(seconds=3))
    assert claim is not None and claim.outbox_id == limited.outbox_id and claim.claim_token
    failed = ledger.mark_outbox_failed(
        limited.outbox_id,
        "sender",
        claim_token=claim.claim_token,
        claim_generation=claim.claim_generation,
        error="provider unavailable",
        now=base + timedelta(seconds=4),
    )
    assert failed.dead_lettered_at is not None
    assert ledger.claim_outbox("sender", now=base + timedelta(minutes=2)) is None


def test_outbox_delivery_ttl_fences_an_active_claim(ledger: JobLedger) -> None:
    base = datetime.now(timezone.utc)
    record = ledger.enqueue_outbox(
        topic="notification",
        payload={},
        idempotency_key="notify:active-ttl",
        ttl_seconds=1,
        now=base,
    )
    claim = ledger.claim_outbox("sender", lease_seconds=5, now=base)
    assert claim is not None and claim.claim_token
    with pytest.raises(LeaseConflict):
        ledger.mark_outbox_sent(
            record.outbox_id,
            "sender",
            claim_token=claim.claim_token,
            claim_generation=claim.claim_generation,
            now=base + timedelta(seconds=2),
        )
    assert ledger.claim_outbox("sender", now=base + timedelta(seconds=6)) is None
    assert ledger.get_outbox(record.outbox_id).dead_lettered_at is not None


def test_finish_lease_can_atomically_enqueue_outbox_and_rolls_back_duplicates(
    ledger: JobLedger,
) -> None:
    base = datetime.now(timezone.utc)
    for job_id in ("atomic-1", "atomic-2"):
        ledger.create_job(
            JobSpec(
                job_id=job_id,
                capability="notify",
                operation="prepare",
                worker_class="light",
                input={},
                scheduled_for=base,
            )
        )

    first = ledger.lease_next("worker", now=base)
    assert first is not None
    ledger.mark_running(
        first.token,
        owner_id=first.owner_id,
        attempt_number=first.attempt_number,
        now=base,
    )
    finished = ledger._commit_worker_result(
        first.token,
        JobStatus.SUCCEEDED,
        owner_id=first.owner_id,
        attempt_number=first.attempt_number,
        business_completed=True,
        outbox=[OutboxSpec("notification", {"job": first.job.job_id}, "notify:atomic")],
        now=base,
    )
    assert finished.status is JobStatus.SUCCEEDED
    assert ledger.claim_outbox("sender", now=base) is not None

    second = ledger.lease_next("worker", now=base)
    assert second is not None
    ledger.mark_running(
        second.token,
        owner_id=second.owner_id,
        attempt_number=second.attempt_number,
        now=base,
    )
    with pytest.raises(LedgerError, match="duplicate"):
        ledger._commit_worker_result(
            second.token,
            JobStatus.SUCCEEDED,
            owner_id=second.owner_id,
            attempt_number=second.attempt_number,
            business_completed=True,
            outbox=[OutboxSpec("notification", {"job": second.job.job_id}, "notify:atomic")],
            now=base,
        )
    assert ledger.get_job(second.job.job_id).status is JobStatus.RUNNING
    ledger.heartbeat_lease(
        second.token,
        owner_id=second.owner_id,
        attempt_number=second.attempt_number,
        extend_seconds=30,
        now=base,
    )
