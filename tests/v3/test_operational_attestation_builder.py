from dataclasses import replace
from datetime import datetime, timedelta, timezone

from magi_v3.ledger import JobLedger, JobSpec
from magi_v3.state import JobStatus
from scripts.ops.build_operational_attestation import build_attestation


def _ledger(tmp_path, *, status=JobStatus.SUCCEEDED, receipts=True):
    ledger = JobLedger(tmp_path / "ledger.sqlite3")
    ledger.initialize()
    created = ledger.create_job(
        JobSpec(
            job_id="attestation-fixture",
            capability="calendar",
            operation="sync",
            worker_class="integration",
            side_effect_class="external_commit",
            idempotency_key="attestation-fixture:1",
            confirmation_token="attestation-confirmation-token",
            confirmation_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            input={},
        )
    )
    record = replace(
        created,
        status=status,
        side_effect_receipts=(
            [{"kind": "calendar", "reference": "remote-id", "committed_at": datetime.now(timezone.utc).isoformat()}]
            if receipts
            else []
        ),
    )
    return ledger, record


def test_builder_requires_real_records_receipts_and_restore(tmp_path, monkeypatch):
    ledger, record = _ledger(tmp_path)
    monkeypatch.setattr(ledger, "recent_jobs", lambda limit: (record,))
    monkeypatch.setattr("scripts.ops.build_operational_attestation.JobLedger", lambda _path: ledger)
    payload = build_attestation(
        ledger_path=ledger.path,
        dr_report={
            "status": "passed",
            "actual_restore_performed": True,
            "backup_age_seconds": 20,
            "restore_elapsed_seconds": 5,
        },
        release_id="v3-test-release",
        limit=10,
        max_rpo_seconds=60,
        max_rto_seconds=30,
    )
    assert payload["ok"] is True
    assert payload["slo"]["sample_size"] == 1
    assert payload["dr"]["verified"] is True


def test_builder_refuses_green_for_empty_or_unreceipted_work(tmp_path, monkeypatch):
    ledger, unreceipted = _ledger(tmp_path, receipts=False)
    monkeypatch.setattr("scripts.ops.build_operational_attestation.JobLedger", lambda _path: ledger)
    monkeypatch.setattr(ledger, "recent_jobs", lambda limit: (unreceipted,))
    payload = build_attestation(
        ledger_path=ledger.path,
        dr_report={
            "status": "passed",
            "actual_restore_performed": False,
            "backup_age_seconds": 20,
            "restore_elapsed_seconds": 5,
        },
        release_id="v3-test-release",
        limit=10,
        max_rpo_seconds=60,
        max_rto_seconds=30,
    )
    assert payload["ok"] is False
    assert payload["slo"]["ok"] is False
    assert payload["dr"]["verified"] is False
