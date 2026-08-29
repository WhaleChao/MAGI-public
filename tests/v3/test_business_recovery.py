from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from magi_v3.business_recovery import audit_recovery_catalog, decide_recovery
from skills.ops.cron_command_identity import command_definition_sha256
from skills.ops import cron_scheduler as scheduler_module
from skills.ops.cron_result_policy import (
    legacy_candidate_rejection_reason,
    terminal_schedule_deferral_reason,
)


ROOT = Path(__file__).resolve().parents[2]


def test_only_expected_schedule_waits_are_terminal_deferrals() -> None:
    for reason in (
        "large_files_waiting_for_offpeak_window",
        "regex_budget_exhausted",
        "repair_budget_reserved",
        "candidate_rejected",
    ):
        payload = (
            '{"success":false,"status":"deferred","deferred":true,'
            f'"partial":false,"reason":"{reason}"'
            "}"
        )
        assert terminal_schedule_deferral_reason(payload) == reason

    for recoverable in ("storage_unavailable", "judicial_yuan_upstream_unavailable"):
        payload = (
            '{"success":false,"status":"deferred","deferred":true,'
            f'"partial":false,"reason":"{recoverable}"'
            "}"
        )
        assert terminal_schedule_deferral_reason(payload) == ""


def test_legacy_candidate_rejection_is_exact_job_and_strong_evidence_only() -> None:
    quality_tail = (
        "channel_marker_leak insufficient_traditional_chinese too_much_english"
    )
    assert legacy_candidate_rejection_reason(
        "job_distill_train_gemma", stderr=quality_tail
    ) == "candidate_rejected"
    assert legacy_candidate_rejection_reason(
        "job_other", stderr=quality_tail
    ) == ""
    assert legacy_candidate_rejection_reason(
        "job_distill_train_gemma", stderr="training process exited unexpectedly"
    ) == ""


def test_transient_business_failure_is_bounded_retry() -> None:
    decision = decide_recovery(
        {"id": "job_business", "command": "python3 worker.py"},
        returncode=1,
        error="connection reset by upstream",
        status="failed",
    )
    assert decision.retryable is True
    assert decision.human_required is False
    assert decision.max_attempts == 3
    assert decision.reason_code == "upstream_unavailable"


def test_explicit_human_requirement_is_not_blindly_retried() -> None:
    decision = decide_recovery(
        {"id": "job_business", "command": "python3 worker.py"},
        returncode=1,
        stdout='{"success":false,"action_required":true,"reason":"missing required document"}',
        status="failed",
    )
    assert decision.retryable is False
    assert decision.human_required is True


def test_zero_semantic_collision_counter_does_not_request_a_person() -> None:
    decision = decide_recovery(
        {"id": "job_drive_case_sync_all_files", "command": "python3 worker.py"},
        returncode=0,
        stdout=(
            '{"success":false,"status":"deferred","deferred":true,'
            '"reason":"storage_unavailable","action_required":false,'
            '"file_sync_summary":{"semantic_collision_files":0}}'
        ),
        error="storage_unavailable",
        status="deferred",
    )
    assert decision.retryable is True
    assert decision.human_required is False
    assert decision.reason_code == "storage_unavailable"


def test_false_timeout_field_does_not_mislabel_business_failure() -> None:
    decision = decide_recovery(
        {"id": "job_function_health_index", "command": "python3 health.py"},
        returncode=1,
        stdout=(
            '{"ok":false,"status":"failed","health":{"failed":['
            '{"path":"cron:job_x","reason":"returncode=1"}]},'
            '"last_timed_out":false}'
        ),
        status="failed",
        timed_out=False,
    )
    assert decision.retryable is True
    assert decision.human_required is False
    assert decision.reason_code == "business_failure"


def test_topology_transition_is_excluded_from_generic_retry() -> None:
    decision = decide_recovery(
        {"id": "job_reboot_before_day_model_switch", "command": "python3 reboot.py"},
        returncode=1,
        error="process interrupted",
        status="failed",
    )
    assert decision.retryable is False


def test_unknown_failure_retries_declared_business_owner_only() -> None:
    business = decide_recovery(
        {"id": "job_file_review_check", "command": "python3 worker.py"},
        returncode=2,
        error="unexpected worker exit",
        status="failed",
    )
    maintenance = decide_recovery(
        {"id": "unregistered_cleanup", "command": "python3 cleanup.py"},
        returncode=2,
        error="unexpected worker exit",
        status="failed",
    )
    assert business.retryable is True
    assert business.business_domain == "file_review_payment"
    assert maintenance.retryable is False


def test_all_declared_business_domains_have_enabled_owners_and_verifiers() -> None:
    jobs = json.loads((ROOT / "cron_jobs.json").read_text(encoding="utf-8"))
    audit = audit_recovery_catalog(jobs)
    assert audit["ok"] is True
    assert audit["domain_count"] == 13
    assert audit["owner_count"] == 79
    assert audit["verifier_count"] == 19
    assert audit["issues"] == []


def test_every_business_domain_uses_the_same_bounded_recovery_contract() -> None:
    catalog = json.loads(
        (ROOT / "config" / "business_recovery_contracts.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(catalog["domains"]) == 13
    for domain, contract in catalog["domains"].items():
        owner = contract["owner_job_ids"][0]
        decision = decide_recovery(
            {"id": owner, "command": "python3 worker.py"},
            returncode=2,
            error="unexpected worker exit",
            status="failed",
        )
        assert decision.business_domain == domain
        assert decision.retryable is True
        assert decision.human_required is False
        assert decision.max_attempts == 3
        assert decision.retry_delays_seconds == (60, 300, 900)


def test_expired_csrf_session_is_recovered_before_asking_a_person() -> None:
    decision = decide_recovery(
        {"id": "job_laf_portal_new_files_scan", "command": "python3 worker.py"},
        returncode=1,
        error="forbidden: invalid CSRF token",
        status="failed",
    )
    assert decision.retryable is True
    assert decision.human_required is False
    assert decision.reason_code == "transient_failure"


def test_cron_retry_is_durable_recoverable_and_cleared_by_success(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_path = tmp_path / "cron_jobs.json"
    state_path = tmp_path / "cron_state.json"
    job = {
        "id": "job_business",
        "cron": "* * * * *",
        "command": "python3 worker.py",
        "desc": "business",
        "enabled": True,
    }
    jobs_path.write_text(json.dumps([job]), encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scheduler_module, "JOB_FILE", str(jobs_path))
    monkeypatch.setattr(scheduler_module, "_use_runtime_dir", lambda: True)
    monkeypatch.setattr(scheduler_module, "_cron_state_path", lambda: state_path)

    scheduler = scheduler_module.CronScheduler()
    command_sha = command_definition_sha256(job)
    retry = scheduler.schedule_job_v3_retry(
        "job_business",
        command_sha256=command_sha,
        reason_code="upstream_unavailable",
        public_reason="外部服務暫時無法使用",
        max_attempts=3,
        delays_seconds=(15, 30, 60),
    )
    assert retry["scheduled"] is True
    assert retry["attempt"] == 1
    assert len(retry["occurrence_id"]) == 64
    recovered = scheduler.recover_v3_retry_jobs()
    assert len(recovered) == 1
    assert recovered[0]["_magi_retry"] is True
    assert recovered[0]["_magi_occurrence_id"] == retry["occurrence_id"]

    assert scheduler.mark_job_result(
        "job_business",
        success=True,
        returncode=0,
        command_sha256=command_sha,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["job_business"]["v3_retry"] is None
    assert state["job_business"]["last_recovery_attempts"] == 1
    assert state["job_business"]["last_recovery_reason_code"] == "upstream_unavailable"
    assert state["job_business"]["last_recovery_occurrence_id"] == retry["occurrence_id"]
    assert state["job_business"]["last_recovered_at"]
    assert scheduler.recover_v3_retry_jobs() == []


def test_retry_receipt_is_retired_when_immutable_command_changes(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_path = tmp_path / "cron_jobs.json"
    state_path = tmp_path / "cron_state.json"
    original = {
        "id": "job_business",
        "cron": "* * * * *",
        "command": "python3 old.py",
        "desc": "business",
        "enabled": True,
    }
    jobs_path.write_text(json.dumps([original]), encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scheduler_module, "JOB_FILE", str(jobs_path))
    monkeypatch.setattr(scheduler_module, "_use_runtime_dir", lambda: True)
    monkeypatch.setattr(scheduler_module, "_cron_state_path", lambda: state_path)
    scheduler = scheduler_module.CronScheduler()
    old_sha = command_definition_sha256(original)
    assert scheduler.schedule_job_v3_retry(
        "job_business",
        command_sha256=old_sha,
        reason_code="transient_failure",
        public_reason="retry",
        max_attempts=3,
        delays_seconds=(15, 30, 60),
    )["scheduled"] is True

    changed = {**original, "command": "python3 new.py"}
    jobs_path.write_text(json.dumps([changed]), encoding="utf-8")
    scheduler._last_file_mtime = 0.0

    assert scheduler.recover_v3_retry_jobs() == []
    state = json.loads(state_path.read_text(encoding="utf-8"))["job_business"]
    assert state["v3_retry"] is None
    assert state["last_retry_superseded_from_sha256"] == old_sha
    assert state["last_retry_superseded_to_sha256"] == command_definition_sha256(
        changed
    )


def test_exhausted_storage_work_is_rearmed_once_after_mount_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_path = tmp_path / "cron_jobs.json"
    state_path = tmp_path / "cron_state.json"
    job = {
        "id": "job_storage",
        "cron": "* * * * *",
        "command": "python3 storage_worker.py",
        "desc": "storage",
        "enabled": True,
    }
    jobs_path.write_text(json.dumps([job]), encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scheduler_module, "JOB_FILE", str(jobs_path))
    monkeypatch.setattr(scheduler_module, "_use_runtime_dir", lambda: True)
    monkeypatch.setattr(scheduler_module, "_cron_state_path", lambda: state_path)
    monkeypatch.setattr(scheduler_module, "_nas_storage_available", lambda: True)
    scheduler = scheduler_module.CronScheduler()
    command_sha = command_definition_sha256(job)
    old_occurrence = "a" * 64
    for _ in range(4):
        exhausted = scheduler.schedule_job_v3_retry(
            "job_storage",
            command_sha256=command_sha,
            occurrence_id=old_occurrence,
            reason_code="storage_unavailable",
            public_reason="NAS unavailable",
            max_attempts=3,
            delays_seconds=(15, 30, 60),
        )
    assert exhausted["exhausted"] is True

    assert scheduler.rearm_recovered_resource_deferrals(
        now=datetime(2033, 5, 18, 7, 0)
    ) == ["job_storage"]
    state = json.loads(state_path.read_text(encoding="utf-8"))["job_storage"]
    assert state["v3_retry"]["status"] == "queued"
    assert state["v3_retry"]["attempt"] == 1
    assert state["v3_resource_recovery"]["from_occurrence_id"] == old_occurrence
    assert scheduler.rearm_recovered_resource_deferrals(
        now=datetime(2033, 5, 18, 7, 5)
    ) == []


def test_terminal_schedule_deferral_clears_prior_retry_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_path = tmp_path / "cron_jobs.json"
    state_path = tmp_path / "cron_state.json"
    job = {
        "id": "job_batch",
        "cron": "* * * * *",
        "command": "python3 worker.py",
        "desc": "batch",
        "enabled": True,
    }
    jobs_path.write_text(json.dumps([job]), encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scheduler_module, "JOB_FILE", str(jobs_path))
    monkeypatch.setattr(scheduler_module, "_use_runtime_dir", lambda: True)
    monkeypatch.setattr(scheduler_module, "_cron_state_path", lambda: state_path)
    scheduler = scheduler_module.CronScheduler()
    command_sha = command_definition_sha256(job)
    scheduler.schedule_job_v3_retry(
        "job_batch",
        command_sha256=command_sha,
        reason_code="timeout",
        public_reason="timeout",
        max_attempts=3,
        delays_seconds=(15, 30, 60),
    )

    assert scheduler.mark_job_result(
        "job_batch",
        success=False,
        returncode=75,
        status="deferred",
        error="regex_budget_exhausted",
        terminal_deferred=True,
        command_sha256=command_sha,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))["job_batch"]
    assert state["last_status"] == "deferred"
    assert state["last_error"] == "regex_budget_exhausted"
    assert state["v3_retry"] is None
    assert scheduler.recover_v3_retry_jobs() == []


def test_startup_reconciles_only_strict_terminal_schedule_deferral(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_path = tmp_path / "cron_jobs.json"
    state_path = tmp_path / "cron_state.json"
    jobs = [
        {
            "id": "job_expected_wait",
            "cron": "* * * * *",
            "command": "python3 expected.py",
            "desc": "expected",
            "enabled": True,
        },
        {
            "id": "job_real_failure",
            "cron": "* * * * *",
            "command": "python3 failed.py",
            "desc": "failed",
            "enabled": True,
        },
        {
            "id": "job_legacy_nim_budget",
            "cron": "* * * * *",
            "command": "python3 weekend_resummary.py",
            "desc": "legacy quota wait",
            "enabled": True,
        },
        {
            "id": "job_distill_train_gemma",
            "cron": "* * * * *",
            "command": "python3 scripts/nightly_distill_gemma.py",
            "desc": "candidate review",
            "enabled": True,
        },
    ]
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "job_expected_wait": {
                    "last_status": "failed",
                    "last_success": False,
                    "last_returncode": 75,
                    "last_timed_out": False,
                    "last_stdout_tail": json.dumps(
                        {
                            "success": False,
                            "status": "deferred",
                            "deferred": True,
                            "partial": False,
                            "reason": "large_files_waiting_for_offpeak_window",
                            "failed": 0,
                            "errors": 0,
                        }
                    ),
                    "v3_retry": {"status": "exhausted"},
                },
                "job_real_failure": {
                    "last_status": "failed",
                    "last_success": False,
                    "last_returncode": 1,
                    "last_timed_out": False,
                    "last_stdout_tail": '{"status":"failed","errors":1}',
                },
                "job_legacy_nim_budget": {
                    "last_status": "failed",
                    "last_success": False,
                    "last_returncode": 0,
                    "last_timed_out": False,
                    "last_stdout_tail": "",
                    "last_stderr_tail": (
                        "provider:nim_daily_budget_exceeded:500/500\n"
                        "週末 NIM 重摘要完成"
                    ),
                    "last_error": "本輪已保存進度",
                    "v3_retry": {"status": "exhausted"},
                },
                "job_distill_train_gemma": {
                    "last_status": "failed",
                    "last_success": False,
                    "last_returncode": 1,
                    "last_timed_out": False,
                    "last_stdout_tail": "",
                    "last_stderr_tail": (
                        "channel_marker_leak insufficient_traditional_chinese "
                        "too_much_english"
                    ),
                    "last_error": "quality output tail",
                    "v3_retry": None,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler_module, "JOB_FILE", str(jobs_path))
    monkeypatch.setattr(scheduler_module, "_use_runtime_dir", lambda: True)
    monkeypatch.setattr(scheduler_module, "_cron_state_path", lambda: state_path)
    scheduler = scheduler_module.CronScheduler()

    assert scheduler.reconcile_terminal_schedule_deferrals() == [
        "job_expected_wait",
        "job_legacy_nim_budget",
        "job_distill_train_gemma",
    ]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["job_expected_wait"]["last_status"] == "deferred"
    assert state["job_expected_wait"]["v3_retry"] is None
    assert state["job_real_failure"]["last_status"] == "failed"
    assert state["job_legacy_nim_budget"]["last_status"] == "deferred"
    assert state["job_legacy_nim_budget"]["last_error"] == (
        "nim_daily_budget_exhausted"
    )
    assert state["job_distill_train_gemma"]["last_status"] == "deferred"
    assert state["job_distill_train_gemma"]["last_error"] == "candidate_rejected"
    assert state["job_distill_train_gemma"]["last_returncode"] == 1
    assert state["job_distill_train_gemma"]["last_review_required"] is True


def test_retry_exhaustion_preserves_one_occurrence_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_path = tmp_path / "cron_jobs.json"
    state_path = tmp_path / "cron_state.json"
    job = {
        "id": "job_business",
        "cron": "* * * * *",
        "command": "python3 worker.py",
        "desc": "business",
        "enabled": True,
    }
    jobs_path.write_text(json.dumps([job]), encoding="utf-8")
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scheduler_module, "JOB_FILE", str(jobs_path))
    monkeypatch.setattr(scheduler_module, "_use_runtime_dir", lambda: True)
    monkeypatch.setattr(scheduler_module, "_cron_state_path", lambda: state_path)
    scheduler = scheduler_module.CronScheduler()
    command_sha = command_definition_sha256(job)
    occurrence_id = "a" * 64
    outcomes = [
        scheduler.schedule_job_v3_retry(
            "job_business",
            command_sha256=command_sha,
            occurrence_id=occurrence_id,
            reason_code="timeout",
            public_reason="作業未在本輪時限內完成",
            max_attempts=3,
            delays_seconds=(15, 30, 60),
        )
        for _ in range(4)
    ]
    assert [row["attempt"] for row in outcomes[:3]] == [1, 2, 3]
    assert outcomes[3]["exhausted"] is True
    assert outcomes[3]["occurrence_id"] == occurrence_id
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["job_business"]["v3_retry"]["status"] == "exhausted"
    assert scheduler.recover_v3_retry_jobs() == []

    next_occurrence = scheduler.schedule_job_v3_retry(
        "job_business",
        command_sha256=command_sha,
        occurrence_id="b" * 64,
        reason_code="timeout",
        public_reason="作業未在本輪時限內完成",
        max_attempts=3,
        delays_seconds=(15, 30, 60),
    )
    assert next_occurrence["scheduled"] is True
    assert next_occurrence["attempt"] == 1
    assert next_occurrence["occurrence_id"] == "b" * 64


def test_restart_reconciles_incomplete_business_work_into_durable_retry(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_path = tmp_path / "cron_jobs.json"
    state_path = tmp_path / "cron_state.json"
    dispatched_at = datetime(2033, 5, 18, 3, 33, 20)
    job = {
        "id": "job_file_review_check",
        "cron": "* * * * *",
        "command": "python3 worker.py",
        "desc": "business",
        "enabled": True,
        "timeout_sec": 60,
    }
    jobs_path.write_text(json.dumps([job]), encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "job_file_review_check": {
                    "last_dispatch_at": dispatched_at.isoformat(),
                    "last_start_at": dispatched_at.isoformat(),
                    "last_status": "running",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler_module, "JOB_FILE", str(jobs_path))
    monkeypatch.setattr(scheduler_module, "_use_runtime_dir", lambda: True)
    monkeypatch.setattr(scheduler_module, "_cron_state_path", lambda: state_path)
    scheduler = scheduler_module.CronScheduler()

    reconciled = scheduler.reconcile_incomplete_jobs(
        now=dispatched_at + timedelta(seconds=61)
    )

    assert reconciled == ["job_file_review_check"]
    state = json.loads(state_path.read_text(encoding="utf-8"))["job_file_review_check"]
    assert state["last_status"] == "deferred"
    assert state["last_returncode"] == 75
    assert state["last_timed_out"] is False
    assert state["v3_retry"]["status"] == "queued"
    assert state["v3_retry"]["attempt"] == 1
    assert len(state["v3_retry"]["occurrence_id"]) == 64


def test_restart_requeues_same_running_retry_without_incrementing_attempt(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_path = tmp_path / "cron_jobs.json"
    state_path = tmp_path / "cron_state.json"
    dispatched_at = datetime(2033, 5, 18, 3, 33, 20)
    job = {
        "id": "job_file_review_check",
        "cron": "* * * * *",
        "command": "python3 worker.py",
        "desc": "business",
        "enabled": True,
        "timeout_sec": 60,
    }
    command_sha = command_definition_sha256(job)
    retry = {
        "job_id": "job_file_review_check",
        "status": "running",
        "attempt": 2,
        "max_attempts": 3,
        "retry_at": dispatched_at.isoformat(),
        "command_sha256": command_sha,
        "occurrence_id": "c" * 64,
    }
    jobs_path.write_text(json.dumps([job]), encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "job_file_review_check": {
                    "last_dispatch_at": dispatched_at.isoformat(),
                    "last_start_at": dispatched_at.isoformat(),
                    "last_status": "running",
                    "v3_retry": retry,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler_module, "JOB_FILE", str(jobs_path))
    monkeypatch.setattr(scheduler_module, "_use_runtime_dir", lambda: True)
    monkeypatch.setattr(scheduler_module, "_cron_state_path", lambda: state_path)
    scheduler = scheduler_module.CronScheduler()

    reconciled = scheduler.reconcile_incomplete_jobs(
        now=dispatched_at + timedelta(seconds=61)
    )

    assert reconciled == ["job_file_review_check"]
    state = json.loads(state_path.read_text(encoding="utf-8"))["job_file_review_check"]
    assert state["v3_retry"]["status"] == "queued"
    assert state["v3_retry"]["attempt"] == 2
    assert state["v3_retry"]["occurrence_id"] == "c" * 64
