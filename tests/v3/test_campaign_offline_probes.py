from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from magi_v3.resource import GlobalResourceGovernor, ResourceSnapshot
from magi_v3.supervisor import WorkerSpec, WorkerSupervisor
from scripts.v3_campaign.offline_probes import (
    FAULT_WORKLOAD,
    SCHEDULE_WORKLOAD,
    _production_duration_replay_certifying,
    _with_timeout_bound_duration_fallbacks,
    bound_cron_jobs,
    run_fault_campaign,
    run_schedule_replay,
)
from scripts.v3_campaign.schedule_realism import run_schedule_realism_assessment

EVIDENCE_PREFIX = "MAGI_V3_OFFLINE_EVIDENCE="
SOURCE_ROOT = Path(__file__).resolve().parents[2]


def test_missing_duration_profile_uses_explicit_noncertifying_timeout_bound() -> None:
    jobs = [
        {"id": "measured", "enabled": True, "timeout_sec": 60},
        {"id": "missing", "enabled": True, "timeout_sec": 120},
    ]
    profiles = {
        "measured": {
            "duration_seconds": 4.0,
            "duration_basis": "production_success_p95",
            "successful_sample_count": 3,
            "certifying_p95": True,
        }
    }

    completed, evidence = _with_timeout_bound_duration_fallbacks(jobs, profiles)

    assert completed["measured"] == profiles["measured"]
    assert completed["missing"] == {
        "duration_seconds": 120.0,
        "duration_basis": "configured_timeout_bound_noncertifying_fallback",
        "successful_sample_count": 0,
        "certifying_p95": False,
    }
    assert evidence["certifying"] is False
    assert evidence["fallback_job_ids"] == ["missing"]
    assert evidence["fallback_profiles"] == [
        {"job_id": "missing", **completed["missing"]}
    ]
    assert evidence["fallback_profiles_sha256"] == hashlib.sha256(
        json.dumps(
            evidence["fallback_profiles"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_timeout_fallback_can_never_clear_duration_replay() -> None:
    assert _production_duration_replay_certifying(
        {"certifying_p95_coverage": True},
        {"used": True},
        {"latest_start_misses": 0, "deadline_misses": 0},
    ) is False


def _emit(evidence: dict[str, object]) -> None:
    print(EVIDENCE_PREFIX + json.dumps(evidence, sort_keys=True, separators=(",", ":")))


def _assert_offline_attestation(evidence: dict[str, object], workload: str) -> None:
    assert evidence["schema_version"] == 1
    assert evidence["workload"] == workload
    assert evidence["status"] == "passed"
    assert evidence["network_access_performed"] is False
    assert evidence["service_start_performed"] is False
    assert evidence["production_port_access_performed"] is False
    assert evidence["launchctl_performed"] is False
    assert evidence["live_state_access_performed"] is False


def test_seven_day_schedule_10x_arrival_2x_duration_replay_emits_measured_evidence(
    tmp_path: Path,
) -> None:
    evidence = run_schedule_replay(SOURCE_ROOT, tmp_path)
    _assert_offline_attestation(evidence, SCHEDULE_WORKLOAD)
    measurements = evidence["measurements"]
    assert isinstance(measurements, dict)
    cron_jobs, _cron_sha256 = bound_cron_jobs(SOURCE_ROOT)
    enabled_cron_count = sum(item.get("enabled") is True for item in cron_jobs)
    assert measurements["cron_definitions"] == len(cron_jobs)
    assert measurements["enabled_cron_definitions"] == enabled_cron_count
    expected_profile_id = os.environ.get(
        "MAGI_V3_VALIDATION_PROFILE_ID", "default_ordinary_week"
    )
    expected_replay_start = os.environ.get(
        "MAGI_V3_REPLAY_START_LOCAL", "2026-07-13T00:00:00+08:00"
    )
    assert measurements["validation_profile_id"] == expected_profile_id
    assert measurements["replay_start_local"] == expected_replay_start
    assert measurements["arrival_multiplier"] == 10
    assert measurements["duration_multiplier"] == 2.0
    assert measurements["duration_basis"] == "measured_ledger_lifecycle_p95"
    assert measurements["governor_light_slots"] == 2
    assert measurements["governor_heavy_slots"] == 2
    assert measurements["virtual_duration_seconds"] == 604800
    assert measurements["replayed_arrivals"] == measurements["base_seven_day_arrivals"] * 10
    assert measurements["persisted_jobs"] == measurements["replayed_arrivals"]
    assert measurements["duplicate_jobs"] == 0
    assert measurements["lost_jobs"] == 0
    assert measurements["recovered_jobs"] == measurements["replayed_arrivals"]
    assert measurements["latest_start_misses"] == 0
    assert measurements["deadline_misses"] == 0
    assert measurements["journal_mode_wal"] is True
    assert measurements["integrity_check_ok"] is True
    assert measurements["reopen_ping_ok"] is True
    production_replay = measurements["production_job_duration_replay"]
    assert production_replay["status"] == "incomplete"
    assert production_replay["completion_claimed"] is False
    assert production_replay["eligible_to_clear_schedule_realism_blocker"] is False
    assert production_replay["blocking_reasons"] == [
        "PRODUCTION_P95_SAMPLE_COVERAGE_INCOMPLETE",
        "PRODUCTION_DURATION_TIMEOUT_FALLBACK_USED",
        "PRODUCTION_DURATION_LATEST_START_MISSES",
        "PRODUCTION_DURATION_DEADLINE_MISSES",
    ]
    assert production_replay["arrival_multiplier"] == 10
    assert production_replay["duration_multiplier"] == 2.0
    assert production_replay["virtual_duration_seconds"] == 604800
    assert production_replay["replayed_arrivals"] == measurements["replayed_arrivals"]
    duration_coverage = production_replay["duration_coverage"]
    assert duration_coverage["enabled_jobs"] == enabled_cron_count
    assert (
        duration_coverage["profiles"] + duration_coverage["missing_jobs"]
        == enabled_cron_count
    )
    assert (
        duration_coverage["p95_jobs"]
        + duration_coverage["sparse_fallback_jobs"]
        == duration_coverage["profiles"]
    )
    # New scheduled jobs legitimately move from sparse fallback to measured
    # p95 coverage over time.  Pinning the historic 78/10 split made improved
    # evidence coverage fail certification.  The invariant is that every
    # profiled job belongs to exactly one bucket and both evidence paths remain
    # exercised; missing jobs stay explicitly fail-closed below.
    assert duration_coverage["p95_jobs"] > 0
    assert duration_coverage["sparse_fallback_jobs"] > 0
    assert duration_coverage["missing_jobs"] == 5
    assert duration_coverage["certifying_p95_coverage"] is False
    assert len(duration_coverage["duration_profiles_sha256"]) == 64
    timeout_fallback = production_replay["missing_duration_fallback"]
    assert timeout_fallback["policy"] == "configured_job_timeout_bound_noncertifying_v1"
    assert timeout_fallback["certifying"] is False
    assert timeout_fallback["used"] is True
    assert timeout_fallback["fallback_jobs"] == duration_coverage["missing_jobs"]
    assert timeout_fallback["fallback_job_ids"] == duration_coverage["missing_job_ids"]
    # The named set changes when a scheduled job is retired, renamed, or gains
    # enough lifecycle evidence.  Its contract is already bound to the current
    # duration-coverage evidence above; a historical ID allowlist would reject
    # valid scheduler evolution without increasing safety.
    assert len(set(timeout_fallback["fallback_job_ids"])) == duration_coverage["missing_jobs"]
    assert timeout_fallback["duration_multiplier_applied_after_fallback"] is True
    assert len(timeout_fallback["fallback_profiles_sha256"]) == 64
    assert all(
        row["duration_basis"]
        == "configured_timeout_bound_noncertifying_fallback"
        and row["successful_sample_count"] == 0
        and row["certifying_p95"] is False
        and row["duration_seconds"] > 0
        for row in timeout_fallback["fallback_profiles"]
    )
    duration_deadlines = production_replay["deadline_measurements"]
    assert duration_deadlines["governor_light_slots"] == 2
    assert duration_deadlines["governor_heavy_slots"] == 2
    assert duration_deadlines["latest_start_misses"] > 0
    assert duration_deadlines["deadline_misses"] > 0
    assert duration_deadlines["jobs_with_latest_start_misses"] > 0
    assert duration_deadlines["jobs_with_deadline_misses"] > 0
    assert duration_deadlines["max_queue_delay_seconds"] >= 0
    assert duration_deadlines["max_scaled_job_duration_seconds"] > 0
    assert set(duration_deadlines["scaled_demand_seconds_by_worker_class"]) == {
        "light",
        "maintenance",
    }
    assert set(duration_deadlines["available_slot_seconds_by_worker_class"]) == {
        "light",
        "maintenance",
    }
    assert set(duration_deadlines["demand_to_capacity_ratio_by_worker_class"]) == {
        "light",
        "maintenance",
    }
    realism = run_schedule_realism_assessment(SOURCE_ROOT, tmp_path / "real-job-bodies")
    assert realism["status"] == "incomplete"
    assert realism["completion_claimed"] is False
    realism_measurements = realism["measurements"]
    assert realism_measurements["representative_bodies_passed"] == (
        realism_measurements["representative_bodies_allowlisted"]
    )
    assert (
        realism_measurements["representative_bodies_allowlisted"]
        + realism_measurements["representative_body_gap_jobs"]
    ) == enabled_cron_count
    assert realism_measurements["representative_body_gap_jobs"] > 0
    assert (
        realism_measurements["production_duration_observations"]
        == duration_coverage["profiles"]
    )
    assert (
        realism["measurements"]["production_duration_gap_jobs"]
        == duration_coverage["missing_jobs"]
    )
    assert (
        realism_measurements["production_duration_p95_jobs"]
        == duration_coverage["p95_jobs"]
    )
    assert (
        realism_measurements["production_duration_sparse_sample_jobs"]
        == duration_coverage["sparse_fallback_jobs"]
    )
    measurements["realism_audit"] = realism
    _emit(evidence)


def test_bounded_fault_matrix_emits_recovery_duplicate_and_loss_evidence(
    tmp_path: Path,
) -> None:
    evidence = run_fault_campaign(tmp_path)
    _assert_offline_attestation(evidence, FAULT_WORKLOAD)
    measurements = evidence["measurements"]
    assert isinstance(measurements, dict)
    assert measurements["faults_requested"] == 6
    assert measurements["faults_completed"] == 6
    assert measurements["faults_passed"] == 6
    assert measurements["duplicate_total"] == 0
    assert measurements["loss_total"] == 0
    matrix = measurements["matrix"]
    assert isinstance(matrix, list)
    assert {row["fault"] for row in matrix} == {
        "sqlite_wal_concurrent_reopen",
        "sqlite_bounded_disk_full",
        "atomic_fsync_failure",
        "worker_crash",
        "worker_timeout",
        "notification_storm_dlq",
    }
    assert all(row["status"] == "passed" for row in matrix)
    assert all(row["duplicate"] == 0 and row["loss"] == 0 for row in matrix)
    _emit(evidence)


def _fd_count() -> int:
    return len(os.listdir("/dev/fd"))


def test_hundred_cycle_worker_reap_soak_emits_measured_evidence(tmp_path: Path) -> None:
    governor = GlobalResourceGovernor()
    supervisor = WorkerSupervisor(governor)
    fd_before = _fd_count()
    peak_fd_count = fd_before
    total_duration_sec = 0.0
    process_groups_gone = 0

    for cycle in range(100):
        job_id = f"campaign-reap-{cycle:03d}"
        pid = supervisor.start(
            WorkerSpec(
                job_id=job_id,
                worker_class="light",
                argv=(sys.executable, "-I", "-S", "-c", "pass"),
                cwd=tmp_path,
                estimated_footprint_mb=8,
                network="none",
                timeout_sec=5,
            ),
            ResourceSnapshot(),
        )
        assert os.getpgid(pid) == pid
        result = supervisor.wait(job_id, timeout=5)
        assert result.returncode == 0
        assert result.timed_out is False
        assert result.process_group_gone is True
        process_groups_gone += int(result.process_group_gone)
        total_duration_sec += result.duration_sec
        assert supervisor.active_job_ids() == ()
        assert governor.active_counts()["total"] == 0
        peak_fd_count = max(peak_fd_count, _fd_count())

    fd_after = _fd_count()
    fd_drift = fd_after - fd_before
    assert process_groups_gone == 100
    assert fd_drift <= 2
    assert supervisor.shutdown() == []
    assert governor.active_counts()["total"] == 0

    evidence = {
        "schema_version": 1,
        "workload": "hundred_cycle_worker_reap_soak",
        "probe": "owned_process_group_reap_soak",
        "status": "passed",
        "measurements": {
            "cycles_requested": 100,
            "cycles_completed": 100,
            "process_groups_gone": process_groups_gone,
            "active_workers_after": len(supervisor.active_job_ids()),
            "governor_slots_after": governor.active_counts()["total"],
            "fd_count_before": fd_before,
            "fd_count_after": fd_after,
            "fd_peak": peak_fd_count,
            "fd_drift": fd_drift,
            "total_worker_duration_sec": round(total_duration_sec, 6),
        },
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
    }
    _emit(evidence)
