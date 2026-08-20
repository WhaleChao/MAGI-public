from dataclasses import replace
from datetime import datetime, timedelta, timezone

from magi_v3.ledger import JobLedger, JobSpec
from magi_v3.observability import outcome_slo, support_bundle, task_trace, verify_dr_report
from magi_v3.state import JobStatus


def _record(tmp_path):
    ledger = JobLedger(tmp_path / "ledger.sqlite3")
    ledger.initialize()
    return ledger.create_job(
        JobSpec(
            job_id="support-fixture",
            capability="casework",
            operation="sync",
            worker_class="maintenance",
            side_effect_class="external_commit",
            idempotency_key="support-fixture:1",
            confirmation_token="support-fixture-confirmation-token",
            confirmation_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            input={"must_not": "appear"},
        )
    )


def test_trace_is_deidentified_but_links_intent_model_effect_and_receipt(tmp_path):
    created = _record(tmp_path)
    record = replace(
        created,
        status=JobStatus.SUCCEEDED,
        business_completed=True,
        metrics={"provider": "test-provider", "model": "test-model", "duration_ms": 12},
        artifacts=[{"kind": "tool_call", "uri": "memory://private"}],
        side_effect_receipts=[{"kind": "remote", "reference": "customer-123", "committed_at": datetime.now(timezone.utc).isoformat()}],
    )
    trace = task_trace(record)
    assert trace["intent"] == {"capability": "casework", "operation": "sync"}
    assert trace["model"]["name"] == "test-model"
    assert trace["tools"] == ["tool_call"]
    assert trace["receipts"][0]["reference"] != "customer-123"
    assert "input" not in trace


def test_support_bundle_uses_ephemeral_pseudonyms_and_rejects_free_text_labels(tmp_path):
    created = _record(tmp_path)
    private_path = "/" + "Users" + "/alice/case"
    unsafe = replace(
        created,
        capability="王小明案件",
        operation=f"sync {private_path}",
        status=JobStatus.SUCCEEDED,
        side_effect_receipts=[
            {
                "kind": "remote",
                "reference": "short-customer-id",
                "committed_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    )
    first = support_bundle([unsafe])
    second = support_bundle([unsafe])
    assert first["traces"][0]["trace_id"] != second["traces"][0]["trace_id"]
    assert first["traces"][0]["intent"] == {"capability": None, "operation": None}
    rendered = str(first)
    assert "王小明" not in rendered
    assert private_path not in rendered
    assert "short-customer-id" not in rendered


def test_slo_keeps_deferred_separate_and_support_bundle_redacts_errors(tmp_path):
    created = _record(tmp_path)
    private_path = "/" + "Users" + "/alice/case"
    failed = replace(
        created,
        status=JobStatus.FAILED,
        error={"code": "storage_timeout", "message": f"token=super-secret at {private_path}"},
    )
    deferred = replace(created, job_id="later", status=JobStatus.DEFERRED)
    slo = outcome_slo([failed, deferred])
    assert slo["terminal_failure_count"] == 1
    assert slo["deferred_count"] == 1
    rendered = str(support_bundle([failed]))
    assert "super-secret" not in rendered and private_path not in rendered
    assert "稍後重試" in rendered


def test_dr_verification_requires_real_restore_and_both_targets():
    good = verify_dr_report(
        {"status": "passed", "actual_restore_performed": True, "backup_age_seconds": 30, "restore_elapsed_seconds": 10},
        max_rpo_seconds=60,
        max_rto_seconds=30,
    )
    assert good["verified"] is True
    bad = verify_dr_report(
        {"status": "passed", "actual_restore_performed": False, "backup_age_seconds": 30, "restore_elapsed_seconds": 10},
        max_rpo_seconds=60,
        max_rto_seconds=30,
    )
    assert bad["verified"] is False


def test_recent_jobs_reader_does_not_request_wal_mutation(tmp_path, monkeypatch):
    ledger = JobLedger(tmp_path / "ledger.sqlite3")
    ledger.initialize()
    ledger.create_job(
        JobSpec(
            job_id="read-only-fixture",
            capability="casework",
            operation="read",
            worker_class="light",
            side_effect_class="read_only",
            input={},
        )
    )

    def forbid_writer():
        raise AssertionError("operations support must not open a write connection")

    monkeypatch.setattr(ledger, "_connect", forbid_writer)
    assert ledger.recent_jobs(limit=1)[0].job_id == "read-only-fixture"
