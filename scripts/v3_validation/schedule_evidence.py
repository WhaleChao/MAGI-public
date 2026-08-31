"""Authoritative G11 derivation from campaign-bound schedule/body reports."""

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


def enabled_job_ids_from_cron(payload: Any) -> tuple[str, ...]:
    """Return the exact enabled-job set from a sealed cron snapshot.

    G11 must be derived from the release-bound schedule input instead of a
    manually maintained count.  Validate the complete snapshot fail-closed so
    malformed disabled rows cannot be hidden outside the certifying set.
    """

    if not isinstance(payload, list) or not payload:
        raise ScheduleEvidenceError("G11 cron snapshot must be a non-empty JSON list")
    seen: set[str] = set()
    enabled: list[str] = []
    for index, row in enumerate(payload):
        if not isinstance(row, Mapping):
            raise ScheduleEvidenceError(f"G11 cron row {index} must be an object")
        job_id = row.get("id")
        if not isinstance(job_id, str) or not job_id or job_id.strip() != job_id:
            raise ScheduleEvidenceError(f"G11 cron row {index} has an invalid job id")
        if job_id in seen:
            raise ScheduleEvidenceError(f"G11 cron snapshot duplicates job id: {job_id}")
        seen.add(job_id)
        if type(row.get("enabled")) is not bool:
            raise ScheduleEvidenceError(
                f"G11 cron row {index} enabled flag must be an exact boolean"
            )
        if row["enabled"]:
            enabled.append(job_id)
    if not enabled:
        raise ScheduleEvidenceError("G11 cron snapshot has no enabled jobs")
    return tuple(sorted(enabled))


def _job_ids(rows: Any, description: str) -> tuple[str, ...]:
    if not isinstance(rows, list):
        raise ScheduleEvidenceError(f"{description} must be a list")
    values: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ScheduleEvidenceError(f"{description} row {index} must be an object")
        job_id = row.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ScheduleEvidenceError(f"{description} row {index} has an invalid job_id")
        values.append(job_id)
    if len(values) != len(set(values)):
        raise ScheduleEvidenceError(f"{description} contains duplicate job ids")
    return tuple(sorted(values))


def _string_ids(values: Any, description: str) -> tuple[str, ...]:
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise ScheduleEvidenceError(f"{description} must be a list of non-empty strings")
    if len(values) != len(set(values)):
        raise ScheduleEvidenceError(f"{description} contains duplicate job ids")
    return tuple(sorted(values))


def _sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def derive_schedule_gate_metrics(
    reports: Sequence[Mapping[str, Any]],
    body_reports: Sequence[Mapping[str, Any]],
    *,
    enabled_job_ids: Sequence[str],
    cron_jobs_sha256: str,
    dispatch_policy_sha256: str,
    certifier_sha256: str,
    registry_script_sha256: str,
    registry_config_sha256: str,
    duration_baseline_sha256: str,
    release_id: str,
    release_manifest_sha256: str,
) -> dict[str, Any]:
    if isinstance(enabled_job_ids, (str, bytes)):
        raise ScheduleEvidenceError("G11 enabled job ids must be a sequence")
    raw_expected_job_ids = tuple(enabled_job_ids)
    if (
        not raw_expected_job_ids
        or any(
            not isinstance(job_id, str) or not job_id
            for job_id in raw_expected_job_ids
        )
        or len(raw_expected_job_ids) != len(set(raw_expected_job_ids))
    ):
        raise ScheduleEvidenceError("G11 enabled job ids are empty, invalid, or duplicated")
    expected_job_ids = tuple(sorted(raw_expected_job_ids))
    expected_job_count = len(expected_job_ids)
    if not reports or len(reports) != len(body_reports):
        raise ScheduleEvidenceError(
            "G11 requires matching non-empty raw schedule and body reports"
        )
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
        entry_job_ids = _job_ids(entries, "G11 registry entries")
        result_job_ids = _job_ids(results, "G11 body results")
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
            or body_measurements.get("enabled_jobs") != expected_job_count
            or body_measurements.get("safe_adapter_coverage_jobs") != expected_job_count
            or body_measurements.get("blocked_jobs") != 0
            or body_measurements.get("body_jobs_passed") != expected_job_count
            or body_measurements.get("all_safe_bodies_passed") is not True
            or entry_job_ids != expected_job_ids
            or any(row.get("classification") != "safe_adapter" or row.get("blockers") != [] for row in entries)
            or result_job_ids != expected_job_ids
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
            or durations.get("enabled_jobs") != expected_job_count
            or durations.get("p95_jobs") != expected_job_count
            or _string_ids(durations.get("p95_job_ids"), "G11 p95 job ids")
            != expected_job_ids
            or durations.get("sparse_fallback_jobs") != 0
            or durations.get("missing_jobs") != 0
            or durations.get("certifying_p95_coverage") is not True
            or not isinstance(bodies, dict)
            or bodies.get("enabled_jobs") != expected_job_count
            or bodies.get("jobs_with_three_successful_real_body_samples") != expected_job_count
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
        "independent_passes": len(reports),
        "arrival_multiplier": 10,
        "duration_multiplier": 2.0,
        "p0_p1_deadline_misses": 0,
        "p2_deadline_success_ratio": 1.0,
        "unbounded_queue_growth": 0,
        "all_enabled_job_bodies_covered": True,
    }
