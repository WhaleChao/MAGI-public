from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from scripts.ops.function_health_index import (
    _cron_occurrence_waiting_or_running,
    _cron_resource_protection_active,
    _cron_semantic_waiting,
    _cron_validation_gate_blocked,
    _operational_health_path_required,
    discover_runtime_health_files,
    evaluate_health_file,
    _cron_omlx_switch_recovered,
    _cron_omlx_superseded_failure_recovered,
)


def test_business_readiness_attention_is_business_state_not_system_failure(tmp_path) -> None:
    path = tmp_path / "business_readiness_latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-08T14:44:57",
                "ok": False,
                "state": "attention",
                "summary": {"attention": 1, "waiting": 1, "ok": 3},
                "items": {"file_review_download": {"state": "attention"}},
            }
        ),
        encoding="utf-8",
    )

    result = evaluate_health_file(
        path, tmp_path, datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc), 2
    )

    assert result["status"] == "ok"
    assert result["contract"] == "business_readiness_snapshot_generated"


def test_malformed_business_readiness_still_fails_closed(tmp_path) -> None:
    path = tmp_path / "business_readiness_latest.json"
    path.write_text(
        json.dumps({"generated_at": "bad", "ok": False, "state": "attention"}),
        encoding="utf-8",
    )

    result = evaluate_health_file(
        path, tmp_path, datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc), 2
    )

    assert result["status"] == "failed"
    assert result["contract"] == "ok"


def test_predecessor_release_health_receipt_is_archived_not_failed(tmp_path) -> None:
    old_root = tmp_path / "releases" / "v3-test-old"
    active_root = tmp_path / "releases" / "v3-test-current"
    active_root.mkdir(parents=True)
    path = tmp_path / "production_live_latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-08T06:00:00+00:00",
                "ok": False,
                "root": str(old_root),
            }
        ),
        encoding="utf-8",
    )

    result = evaluate_health_file(
        path,
        tmp_path,
        datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc),
        2,
        active_release={
            "release_id": "v3-test-current",
            "release_root": str(active_root),
        },
    )

    assert result["status"] == "superseded"
    assert result["ok"] is True
    assert result["contract"] == "release_binding"


def test_current_release_health_failure_remains_failed(tmp_path) -> None:
    active_root = tmp_path / "releases" / "v3-test-current"
    active_root.mkdir(parents=True)
    path = tmp_path / "production_live_latest.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-08T06:00:00+00:00",
                "ok": False,
                "release_id": "v3-test-current",
                "root": str(active_root),
            }
        ),
        encoding="utf-8",
    )

    result = evaluate_health_file(
        path,
        tmp_path,
        datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc),
        2,
        active_release={
            "release_id": "v3-test-current",
            "release_root": str(active_root),
        },
    )

    assert result["status"] == "failed"
    assert result["contract"] == "ok"


def test_unbound_health_failure_still_fails_closed(tmp_path) -> None:
    path = tmp_path / "unbound_health_latest.json"
    path.write_text(json.dumps({"ok": False}), encoding="utf-8")

    result = evaluate_health_file(
        path,
        tmp_path,
        datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc),
        2,
        active_release={
            "release_id": "v3-test-current",
            "release_root": str(tmp_path / "releases" / "v3-test-current"),
        },
    )

    assert result["status"] == "failed"


def test_running_drive_all_files_worker_is_not_a_failed_health_artifact(tmp_path) -> None:
    path = tmp_path / "drive_case_sync_worker_status_latest.json"
    path.write_text(
        json.dumps({"success": False, "status": "direct_all_case_sync_running"}),
        encoding="utf-8",
    )

    result = evaluate_health_file(
        path, tmp_path, datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc), 2
    )

    assert result["status"] == "observed"
    assert result["contract"] == "status_running"


def test_explicit_retryable_deferred_receipt_is_not_a_red_health_artifact(tmp_path) -> None:
    path = tmp_path / "drive_case_sync_worker_skip_latest.json"
    path.write_text(
        json.dumps(
            {
                "ok": False,
                "success": False,
                "deferred": True,
                "retryable": True,
                "action_required": False,
                "reason_code": "owner_conflict",
                "status": "queued_for_retry",
            }
        ),
        encoding="utf-8",
    )

    result = evaluate_health_file(
        path, tmp_path, datetime(2026, 8, 8, 7, 0, tzinfo=timezone.utc), 2
    )

    assert result["status"] == "observed"
    assert result["ok"] is True
    assert result["contract"] == "deferred_nonblocking"


def test_queued_or_running_occurrence_is_deferred() -> None:
    assert _cron_occurrence_waiting_or_running(
        {"v3_pending_occurrence": {"status": "queued"}}
    ) == (True, "queued")
    assert _cron_occurrence_waiting_or_running(
        {"v3_pending_occurrence": {"status": "running"}}
    ) == (True, "running")
    assert _cron_occurrence_waiting_or_running(
        {"v3_pending_occurrence": {"status": "completed"}}
    ) == (False, "completed")


def test_ci_and_release_matrix_outputs_are_not_daily_runtime_requirements() -> None:
    assert not _operational_health_path_required(
        {"sources": ["matrix_json_out"]}
    )
    assert _operational_health_path_required(
        {"sources": ["cron_json_out", "matrix_json_out"]}
    )
    assert _operational_health_path_required({"sources": ["cron_json_out"]})


def test_health_index_does_not_rescan_prior_acceptance_gate_result(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "magi_acceptance_function_health_latest.json").write_text(
        json.dumps({"ok": False}), encoding="utf-8"
    )
    (runtime / "independent_health_latest.json").write_text(
        json.dumps({"ok": True}), encoding="utf-8"
    )

    discovered = discover_runtime_health_files(tmp_path, runtime, include_static=False)

    assert discovered == [runtime / "independent_health_latest.json"]


def test_reclaimed_distill_candidate_is_a_nonblocking_validation_outcome() -> None:
    state = {
        "last_status": "failed",
        "last_returncode": 1,
        "last_stderr_tail": "candidate_rejected; v011 reclaimed after quality gate",
    }

    assert _cron_validation_gate_blocked("job_distill_train_gemma", "", state)


def test_partial_and_external_results_are_waiting_not_failed() -> None:
    assert _cron_semantic_waiting(
        {"last_error": "worker exited", "v3_retry": {"status": "queued", "attempt": 1}},
        job_id="job_business",
        returncode=1,
        timed_out=False,
        last_status="failed",
    )
    assert _cron_semantic_waiting(
        {"last_error": "semantic_path_collision_requires_human_review"},
        job_id="job_drive_case_sync_all_files",
        returncode=0,
        timed_out=False,
        last_status="deferred",
    )
    assert _cron_semantic_waiting(
        {"last_stdout_tail": '{"status": "partial", "errors": 1}'},
        returncode=75,
        timed_out=False,
        last_status="failed",
    )
    assert _cron_semantic_waiting(
        {"last_error": "upstream Judicial Yuan HTTP 500"},
        returncode=0,
        timed_out=False,
        last_status="failed",
    )
    assert not _cron_semantic_waiting(
        {"last_error": "local database failed"},
        returncode=1,
        timed_out=False,
        last_status="failed",
    )
    assert not _cron_semantic_waiting(
        {"last_error": "upstream timeout"},
        returncode=143,
        timed_out=True,
        last_status="failed",
    )
    assert _cron_semantic_waiting(
        {"last_error": "PermissionError while writing sealed export"},
        job_id="job_accounting_monthly_bonus",
        returncode=1,
        timed_out=False,
        last_status="failed",
    )
    assert _cron_semantic_waiting(
        {"last_error": "provider validation unavailable"},
        job_id="job_heavy_translation_quality_live",
        returncode=1,
        timed_out=False,
        last_status="failed",
    )
    assert _cron_semantic_waiting(
        {"last_error": "all_judgment_reason_searches_failed"},
        job_id="job_1770705679",
        returncode=0,
        timed_out=False,
        last_status="failed",
    )


def test_resource_governor_critical_result_is_waiting_but_generic_rc2_fails() -> None:
    critical = {
        "last_stdout_tail": (
            '{"ok": false, "level": "critical", '
            '"snapshot": {"free_plus_inactive_gb": 1.77}, '
            '"actions": ["pause_heavy_backlog_jobs"]}'
        )
    }
    assert _cron_resource_protection_active(
        critical,
        job_id="job_resource_governor",
        returncode=2,
        timed_out=False,
    )
    assert _cron_semantic_waiting(
        critical,
        job_id="job_resource_governor",
        returncode=2,
        timed_out=False,
        last_status="failed",
    )
    assert not _cron_resource_protection_active(
        {"last_stderr_tail": "usage: resource_governor.py"},
        job_id="job_resource_governor",
        returncode=2,
        timed_out=False,
    )


def test_omlx_failed_cron_is_recovered_only_by_newer_matching_live_gate(tmp_path) -> None:
    gate = tmp_path / "model_live_gate_latest.json"
    gate.write_text(
        json.dumps(
            {
                "ok": True,
                "expected_profile": "night",
                "active_profile": "night-e4b-degraded",
                "failures": [],
                "endpoints": [
                    {"port": 8080, "ok": True, "model_id": "gemma-4-e4b-it-4bit"}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert _cron_omlx_switch_recovered(
        tmp_path,
        "job_omlx_switch_night",
        {"last_failure_at": "2000-01-01T00:00:00+00:00"},
    )
    assert not _cron_omlx_switch_recovered(
        tmp_path,
        "job_omlx_switch_day",
        {"last_failure_at": "2000-01-01T00:00:00+00:00"},
    )


def test_superseded_omlx_command_uses_only_newer_green_current_topology(tmp_path) -> None:
    gate = tmp_path / "model_live_gate_latest.json"
    gate.write_text(
        json.dumps(
            {
                "ok": True,
                "expected_profile": "day",
                "active_profile": "day",
                "failures": [],
                "endpoints": [
                    {"port": 8080, "ok": True, "model_id": "gemma-4-e4b-it-4bit"}
                ],
            }
        ),
        encoding="utf-8",
    )
    old_sha = "1" * 64
    new_sha = "2" * 64
    state = {"last_failure_at": "2000-01-01T00:00:00+00:00", "command_sha256": old_sha}
    assert _cron_omlx_superseded_failure_recovered(
        tmp_path,
        "job_omlx_switch_night",
        state,
        current_command_sha256=new_sha,
    )
    assert not _cron_omlx_superseded_failure_recovered(
        tmp_path,
        "job_omlx_switch_night",
        state,
        current_command_sha256=old_sha,
    )

    payload = json.loads(gate.read_text(encoding="utf-8"))
    payload["ok"] = False
    gate.write_text(json.dumps(payload), encoding="utf-8")
    assert not _cron_omlx_superseded_failure_recovered(
        tmp_path,
        "job_omlx_switch_night",
        state,
        current_command_sha256=new_sha,
    )


def test_new_release_can_supersede_old_switch_failure_only_after_new_live_gate(
    tmp_path, monkeypatch
) -> None:
    gate = tmp_path / "model_live_gate_latest.json"
    gate.write_text(
        json.dumps(
            {
                "ok": True,
                "expected_profile": "day",
                "active_profile": "day",
                "failures": [],
                "endpoints": [
                    {"port": 8080, "ok": True, "model_id": "gemma-4-e4b-it-4bit"}
                ],
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "active-release.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "magi.v3.active-release/v1",
                "release_id": "v3-test-new",
                "committed_at": "2000-01-02T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    os.utime(gate, (946857600.0, 946857600.0))  # 2000-01-03 UTC
    monkeypatch.setenv("MAGI_V3_ACTIVE_RELEASE_MARKER", str(marker))
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-test-new")
    state = {
        "last_failure_at": "2000-01-01T00:00:00+00:00",
        "command_sha256": "1" * 64,
    }
    assert _cron_omlx_superseded_failure_recovered(
        tmp_path,
        "job_omlx_switch_night",
        state,
        current_command_sha256="1" * 64,
    )

    marker.write_text(
        json.dumps(
            {
                "schema": "magi.v3.active-release/v1",
                "release_id": "v3-test-new",
                "committed_at": "1999-12-31T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    assert not _cron_omlx_superseded_failure_recovered(
        tmp_path,
        "job_omlx_switch_night",
        state,
        current_command_sha256="1" * 64,
    )
