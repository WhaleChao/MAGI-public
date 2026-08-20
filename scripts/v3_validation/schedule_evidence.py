"""Authoritative G11 derivation from seven raw schedule/body reports."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from scripts.v3_validation.schedule_capacity_certification import (
    DURABLE_BACKLOG_COALESCING_JOB_IDS,
    ScheduleCapacityError,
    verify_compressed_active_duration_evidence,
    verify_schedule_capacity_evidence,
)


class ScheduleEvidenceError(ValueError):
    pass


def _sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def derive_schedule_gate_metrics(
    reports: Sequence[Mapping[str, Any]],
    body_reports: Sequence[Mapping[str, Any]],
    *,
    cron_jobs_sha256: str,
    dispatch_policy_sha256: str,
    certifier_sha256: str,
    registry_script_sha256: str,
    registry_config_sha256: str,
    duration_baseline_sha256: str,
    release_id: str,
    release_manifest_sha256: str,
) -> dict[str, Any]:
    if len(reports) != 7 or len(body_reports) != 7:
        raise ScheduleEvidenceError("G11 requires seven raw schedule and body reports")
    profiles: set[str] = set()
    report_hashes: set[str] = set()
    body_hashes: set[str] = set()
    for index, (report, body) in enumerate(zip(reports, body_reports), 1):
        verify_schedule_capacity_evidence(report)
        profile = str(report.get("validation_profile_id") or "")
        if not profile or profile in profiles:
            raise ScheduleEvidenceError("G11 schedule validation profile is missing or duplicated")
        profiles.add(profile)
        digest = str(report.get("evidence_sha256") or "")
        if digest in report_hashes:
            raise ScheduleEvidenceError("G11 schedule report is duplicated")
        report_hashes.add(digest)
        unsigned_body = dict(body)
        supplied_body_sha = str(unsigned_body.pop("evidence_sha256", ""))
        if supplied_body_sha != _sha(unsigned_body) or supplied_body_sha in body_hashes:
            raise ScheduleEvidenceError("G11 body evidence hash is invalid or duplicated")
        body_hashes.add(supplied_body_sha)
        if (
            report.get("status") != "certified"
            or report.get("gate", {}).get("eligible_to_clear_schedule_realism_blocker") is not True
            or report.get("gate", {}).get("blocking_reasons") != []
        ):
            raise ScheduleEvidenceError("G11 schedule report is not certifying")
        binding = report.get("release_binding")
        if not isinstance(binding, dict) or any(
            binding.get(key) != expected
            for key, expected in {
                "cron_jobs_sha256": cron_jobs_sha256,
                "dispatch_policy_sha256": dispatch_policy_sha256,
                "certifier_script_sha256": certifier_sha256,
                "real_job_body_registry_sha256": registry_config_sha256,
                "real_job_body_registry_script_sha256": registry_script_sha256,
                "duration_baseline_sha256": duration_baseline_sha256,
                "real_job_body_evidence_sha256": supplied_body_sha,
                "release_id": release_id,
                "release_manifest_sha256": release_manifest_sha256,
            }.items()
        ):
            raise ScheduleEvidenceError("G11 schedule source binding drifted")
        body_binding = body.get("release_binding")
        body_measurements = body.get("measurements")
        entries = body.get("registry_entries")
        results = body.get("body_results")
        if (
            not isinstance(body_binding, dict)
            or body_binding.get("release_id") != release_id
            or body_binding.get("release_manifest_sha256") != release_manifest_sha256
            or body_binding.get("cron_jobs_sha256") != cron_jobs_sha256
            or body_binding.get("registry_sha256") != registry_config_sha256
            or body_binding.get("inherited_baseline_sha256") != duration_baseline_sha256
            or not isinstance(body_measurements, dict)
            or body.get("status") != "passed"
            or body.get("completion_claimed") is not True
            or body_measurements.get("enabled_jobs") != 93
            or body_measurements.get("safe_adapter_coverage_jobs") != 93
            or body_measurements.get("blocked_jobs") != 0
            or body_measurements.get("body_jobs_passed") != 93
            or body_measurements.get("all_safe_bodies_passed") is not True
            or not isinstance(entries, list)
            or len(entries) != 93
            or any(row.get("classification") != "safe_adapter" or row.get("blockers") != [] for row in entries)
            or not isinstance(results, list)
            or len(results) != 93
            or len({row.get("job_id") for row in results}) != 93
            or any(
                row.get("status") != "passed"
                or row.get("semantic_success") is not True
                or row.get("successful_samples") != 3
                or row.get("duration_sample_count") != 3
                for row in results
            )
        ):
            raise ScheduleEvidenceError("G11 real body coverage is incomplete or inconsistent")
        duration_evidence = (
            report.get("layers", {})
            .get("business_body_plane", {})
            .get("duration_evidence")
        )
        if not isinstance(duration_evidence, Mapping):
            raise ScheduleEvidenceError("G11 compressed active duration evidence is missing")
        if (
            duration_evidence.get("baseline_sha256") != duration_baseline_sha256
            or binding.get("duration_profiles_sha256")
            != duration_evidence.get("duration_profiles_sha256")
        ):
            raise ScheduleEvidenceError("G11 duration source binding drifted")
        try:
            verify_compressed_active_duration_evidence(
                duration_evidence,
                body,
                cron_jobs_sha256=cron_jobs_sha256,
                require_complete=True,
            )
        except ScheduleCapacityError as exc:
            raise ScheduleEvidenceError(
                f"G11 compressed active duration binding failed: {exc}"
            ) from exc
        safety = report.get("safety")
        body_network = body.get("network_access_performed")
        if not isinstance(safety, dict) or type(
            safety.get("body_network_access_performed")
        ) is not bool or type(body_network) is not bool or safety.get(
            "body_network_access_performed"
        ) is not body_network or any(
            safety.get(field) is not False
            for field in (
                "live_state_accessed",
                "production_service_started",
                "production_port_accessed",
                "launchctl_invoked",
                "body_external_network_access_performed",
                "body_nas_access_performed",
                "body_production_database_access_performed",
            )
        ) or any(
            body.get(field) is not False
            for field in (
                "external_network_access_performed",
                "production_database_access_performed",
                "nas_access_performed",
                "production_state_write_performed",
            )
        ) or safety.get("sandbox_writes_only") is not True:
            raise ScheduleEvidenceError("G11 offline safety attestation failed")
        control = report.get("layers", {}).get("control_plane", {})
        business = report.get("layers", {}).get("business_body_plane", {})
        measurements = control.get("measurements")
        durations = business.get("duration_evidence")
        bodies = business.get("body_evidence")
        deadlines = business.get("deadline_measurements")
        if (
            control.get("status") != "passed"
            or not isinstance(measurements, dict)
            or measurements.get("delivery_multiplier") != 10
            or measurements.get("duration_multiplier") != 2.0
            or measurements.get("all_deliveries_accounted") is not True
            or measurements.get("all_distinct_occurrences_accounted") is not True
            or measurements.get("same_job_concurrency_violations") != 0
            or measurements.get("loss_sensitive_coalesced_occurrences") != 0
            or measurements.get("coalescing_safety_passed") is not True
            or measurements.get("durable_backlog_coalescing_job_ids")
            != sorted(DURABLE_BACKLOG_COALESCING_JOB_IDS)
            or measurements.get("coalesced_distinct_occurrences")
            != measurements.get("durable_backlog_coalesced_occurrences")
            or measurements.get("latest_start_misses") != 0
            or measurements.get("deadline_misses") != 0
            or measurements.get("global_worker_cap") != 4
            or not isinstance(durations, dict)
            or durations.get("enabled_jobs") != 93
            or durations.get("p95_jobs") != 93
            or durations.get("sparse_fallback_jobs") != 0
            or durations.get("missing_jobs") != 0
            or durations.get("certifying_p95_coverage") is not True
            or not isinstance(bodies, dict)
            or bodies.get("enabled_jobs") != 93
            or bodies.get("jobs_with_three_successful_real_body_samples") != 93
            or bodies.get("jobs_missing_real_body_adapter") != 0
            or bodies.get("body_adapter_coverage_complete") is not True
            or bodies.get("registry_evidence_sha256") != supplied_body_sha
            or deadlines != {
                "latest_start_misses": 0,
                "deadline_misses": 0,
                "max_queue_delay_seconds": measurements.get("max_queue_delay_seconds"),
            }
        ):
            raise ScheduleEvidenceError("G11 replay/body/deadline measurements failed")
    return {
        "independent_passes": 7,
        "arrival_multiplier": 10,
        "duration_multiplier": 2.0,
        "p0_p1_deadline_misses": 0,
        "p2_deadline_success_ratio": 1.0,
        "unbounded_queue_growth": 0,
        "all_enabled_job_bodies_covered": True,
    }
