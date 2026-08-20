from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.v3_evidence_compiler as compiler
from scripts.v3_campaign.runner import ReleaseBundle
from scripts.v3_evidence_compiler import CompileContext, compile_campaign_evidence
from scripts.v3_release_gate import (
    BoundArtifact,
    _authoritative_normalized_metrics,
    evaluate_evidence,
)
from scripts.v3_validation.schedule_evidence import (
    ScheduleEvidenceError,
    derive_schedule_gate_metrics,
)
from scripts.v3_validation.schedule_capacity_certification import (
    _duration_profile_hash_payload,
)
from scripts.v3_validation.schedule_sample_evidence import (
    build_sample_evidence,
    canonical_sha256,
)


HASHES = {
    "cron_jobs_sha256": "1" * 64,
    "dispatch_policy_sha256": "2" * 64,
    "certifier_sha256": "3" * 64,
    "registry_script_sha256": "4" * 64,
    "registry_config_sha256": "5" * 64,
    "duration_baseline_sha256": "6" * 64,
    "release_id": "release-g11",
    "release_manifest_sha256": "7" * 64,
}


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sign(payload: dict[str, object]) -> None:
    payload.pop("evidence_sha256", None)
    payload["evidence_sha256"] = _digest(payload)


def _raw_passes(
    bindings: dict[str, str] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    bindings = bindings or HASHES
    reports: list[dict[str, object]] = []
    bodies: list[dict[str, object]] = []
    for validation_pass in range(1, 8):
        entries = [
            {
                "job_id": f"job-{index:03d}",
                "classification": "safe_adapter",
                "blockers": [],
                "actual_entrypoint": f"scripts/job-{index:03d}.py",
                "production_command_sha256": hashlib.sha256(
                    f"command-{index:03d}".encode()
                ).hexdigest(),
            }
            for index in range(93)
        ]
        results = []
        for index in range(93):
            job_id = f"job-{index:03d}"
            entrypoint_sha256 = hashlib.sha256(
                f"entrypoint-{index:03d}".encode()
            ).hexdigest()
            sample_evidence = []
            for sample_index, duration in enumerate((1.0, 2.0, 3.0), 1):
                sample_evidence.append(
                    build_sample_evidence(
                        {
                            "execution_nonce_sha256": hashlib.sha256(
                                f"nonce-{validation_pass}-{job_id}-{sample_index}".encode()
                            ).hexdigest(),
                            "status": "passed",
                            "executed": True,
                            "returncode": 0,
                            "duration_seconds": duration,
                            "semantic_success": True,
                            "sandbox_profile_sha256": hashlib.sha256(
                                f"sandbox-{index:03d}-{sample_index}".encode()
                            ).hexdigest(),
                            "stdout_sha256": hashlib.sha256(
                                f"stdout-{index:03d}-{sample_index}".encode()
                            ).hexdigest(),
                            "stderr_sha256": hashlib.sha256(
                                f"stderr-{index:03d}-{sample_index}".encode()
                            ).hexdigest(),
                            "diagnostic_evidence_relative_path": "diagnostics/execution.json",
                            "diagnostic_evidence_sha256": hashlib.sha256(
                                f"diagnostic-{index:03d}-{sample_index}".encode()
                            ).hexdigest(),
                            "fixture_binding_sha256": hashlib.sha256(
                                f"fixture-binding-{index:03d}-{sample_index}".encode()
                            ).hexdigest(),
                            "fixture_initial_inventory_sha256": "8" * 64,
                            "fixture_final_inventory_sha256": "9" * 64,
                            "fixture_final_file_count": 1,
                            "no_fixture_symlinks": True,
                            "success_contract_evidence": {
                                "checks": {"terminal_postcondition": True},
                                "receipt_sha256": hashlib.sha256(
                                    f"receipt-{validation_pass}-{job_id}-{sample_index}".encode()
                                ).hexdigest(),
                            },
                            "dependency_evidence": {
                                "kind": "none",
                                "request_count": 0,
                                "request_counts": {},
                                "expected_requests_satisfied": True,
                                "transcript_sha256": canonical_sha256([]),
                                "postcondition_count": 0,
                                "passed_postcondition_count": 0,
                                "postconditions_passed": True,
                                "postconditions_sha256": canonical_sha256([]),
                            },
                            "adapter_mode": "real_entrypoint_fixture_v1",
                            "network_denied_by_seatbelt": True,
                            "notifications_disabled": True,
                        },
                        sample_index=sample_index,
                        execution_kind="reviewed_real_entrypoint_fixture_v1",
                        entrypoint_sha256=entrypoint_sha256,
                    )
                )
            results.append({
                "job_id": f"job-{index:03d}",
                "status": "passed",
                "semantic_success": True,
                "successful_samples": 3,
                "duration_sample_count": 3,
                "duration_samples_seconds": [1.0, 2.0, 3.0],
                "duration_p95_seconds": 3.0,
                "sample_statuses": ["passed", "passed", "passed"],
                "sample_evidence": sample_evidence,
                "sample_evidence_sha256": canonical_sha256(sample_evidence),
                "entrypoint_sha256": entrypoint_sha256,
                "sandbox_profile_sha256_samples": [
                    row["sandbox_profile_sha256"] for row in sample_evidence
                ],
                "stdout_sha256_samples": [
                    row["stdout_sha256"] for row in sample_evidence
                ],
                "stderr_sha256_samples": [
                    row["stderr_sha256"] for row in sample_evidence
                ],
                "runner": "real_entrypoint_fixture_v1",
                "adapter_mode": "real_entrypoint_fixture_v1",
                "network_denied_by_seatbelt": True,
                "notifications_disabled": True,
            })
        body: dict[str, object] = {
            "schema": "magi.v3.schedule-real-body-registry/v1",
            "status": "passed",
            "completion_claimed": True,
            "validation_pass": validation_pass,
            "release_binding": {
                "release_id": bindings["release_id"],
                "release_manifest_sha256": bindings["release_manifest_sha256"],
                "cron_jobs_sha256": bindings["cron_jobs_sha256"],
                "registry_sha256": bindings["registry_config_sha256"],
                "inherited_baseline_sha256": bindings["duration_baseline_sha256"],
            },
            "measurements": {
                "enabled_jobs": 93,
                "safe_adapter_coverage_jobs": 93,
                "blocked_jobs": 0,
                "body_jobs_passed": 93,
                "all_safe_bodies_passed": True,
            },
            "registry_entries": entries,
            "body_results": results,
            "network_access_performed": False,
            "external_network_access_performed": False,
            "production_database_access_performed": False,
            "nas_access_performed": False,
            "production_state_write_performed": False,
        }
        _sign(body)
        body_hash = body["evidence_sha256"]
        profile_bindings = []
        for entry, result in zip(entries, results, strict=True):
            active = {
                "sample_kind": "compressed_active_bounded_real_entrypoint",
                "p95_kind": "compressed_active_bounded_body_p95",
                "successful_samples": result["successful_samples"],
                "semantic_success": result["semantic_success"],
                "duration_samples_seconds": result["duration_samples_seconds"],
                "duration_p95_seconds": result["duration_p95_seconds"],
                "sample_evidence": result["sample_evidence"],
                "sample_evidence_sha256": result["sample_evidence_sha256"],
                "actual_entrypoint": entry["actual_entrypoint"],
                "entrypoint_sha256": result["entrypoint_sha256"],
                "production_command_sha256": entry["production_command_sha256"],
                "runner": result["runner"],
                "adapter_mode": result["adapter_mode"],
                "network_denied_by_seatbelt": result[
                    "network_denied_by_seatbelt"
                ],
                "notifications_disabled": result["notifications_disabled"],
                "sandbox_profile_sha256_samples": result[
                    "sandbox_profile_sha256_samples"
                ],
                "stdout_sha256_samples": result["stdout_sha256_samples"],
                "stderr_sha256_samples": result["stderr_sha256_samples"],
            }
            profile_bindings.append(
                {
                    "job_id": entry["job_id"],
                    "selected_duration_seconds": 4.0,
                    "selected_duration_basis": "max_historical_production_p95_and_compressed_active_bounded_body_p95",
                    "historical_production_p95_seconds": 4.0,
                    "historical_sparse_observed_max_seconds": None,
                    "compressed_active": active,
                }
            )
        duration_evidence = {
            "enabled_jobs": 93,
            "profiles": 93,
            "p95_jobs": 93,
            "historical_production_p95_jobs": 92,
            "compressed_active_p95_jobs": 93,
            "sparse_fallback_jobs": 0,
            "missing_jobs": 0,
            "minimum_successful_samples": 3,
            "certifying_p95_coverage": True,
            "p95_job_ids": [entry["job_id"] for entry in entries],
            "historical_production_p95_job_ids": [entry["job_id"] for entry in entries],
            "compressed_active_p95_job_ids": [entry["job_id"] for entry in entries],
            "sparse_fallback_job_ids": [],
            "missing_job_ids": [],
            "cron_jobs_sha256": bindings["cron_jobs_sha256"],
            "baseline_sha256": bindings["duration_baseline_sha256"],
            "active_body_evidence_sha256": body_hash,
            "selected_duration_rule": "max(historical_production_p95, compressed_active_bounded_body_p95)",
            "active_sample_disclosure": "compressed active bounded real-entrypoint samples; not historical production observations",
            "profile_bindings": profile_bindings,
        }
        duration_evidence["duration_profiles_sha256"] = _digest(
            _duration_profile_hash_payload(duration_evidence)
        )
        delay = 100.0 + validation_pass
        measurements = {
            "delivery_multiplier": 10,
            "duration_multiplier": 2.0,
            "all_deliveries_accounted": True,
            "all_distinct_occurrences_accounted": True,
            "same_job_concurrency_violations": 0,
            "coalesced_distinct_occurrences": 0,
            "durable_backlog_coalescing_job_ids": [
                "job_drive_case_sync_all_files",
                "job_legacy_judgment_resummary_quality"
            ],
            "durable_backlog_coalesced_occurrences": 0,
            "durable_backlog_coalesced_occurrences_by_job": {},
            "loss_sensitive_coalesced_occurrences": 0,
            "loss_sensitive_coalesced_occurrences_by_job": {},
            "coalescing_safety_passed": True,
            "latest_start_misses": 0,
            "deadline_misses": 0,
            "global_worker_cap": 4,
            "max_queue_delay_seconds": delay,
        }
        report: dict[str, object] = {
            "schema": "magi.v3.schedule-capacity-certification/v1",
            "status": "certified",
            "validation_profile_id": f"profile-{validation_pass}",
            "release_binding": {
                "cron_jobs_sha256": bindings["cron_jobs_sha256"],
                "dispatch_policy_sha256": bindings["dispatch_policy_sha256"],
                "certifier_script_sha256": bindings["certifier_sha256"],
                "real_job_body_registry_script_sha256": bindings[
                    "registry_script_sha256"
                ],
                "real_job_body_registry_sha256": bindings["registry_config_sha256"],
                "duration_baseline_sha256": bindings["duration_baseline_sha256"],
                "duration_profiles_sha256": duration_evidence[
                    "duration_profiles_sha256"
                ],
                "real_job_body_evidence_sha256": body_hash,
                "release_id": bindings["release_id"],
                "release_manifest_sha256": bindings["release_manifest_sha256"],
            },
            "layers": {
                "control_plane": {"status": "passed", "measurements": measurements},
                "business_body_plane": {
                    "status": "passed",
                    "duration_evidence": duration_evidence,
                    "body_evidence": {
                        "enabled_jobs": 93,
                        "jobs_with_three_successful_real_body_samples": 93,
                        "jobs_missing_real_body_adapter": 0,
                        "body_adapter_coverage_complete": True,
                        "registry_evidence_sha256": body_hash,
                    },
                    "deadline_measurements": {
                        "latest_start_misses": 0,
                        "deadline_misses": 0,
                        "max_queue_delay_seconds": delay,
                    },
                },
            },
            "gate": {
                "eligible_to_clear_schedule_realism_blocker": True,
                "blocking_reasons": [],
            },
            "safety": {
                "live_state_accessed": False,
                "production_service_started": False,
                "production_port_accessed": False,
                "launchctl_invoked": False,
                "body_network_access_performed": False,
                "body_external_network_access_performed": False,
                "body_nas_access_performed": False,
                "body_production_database_access_performed": False,
                "sandbox_writes_only": True,
            },
        }
        _sign(report)
        reports.append(report)
        bodies.append(body)
    return reports, bodies


def test_seven_raw_passes_recompute_the_g11_metrics() -> None:
    reports, bodies = _raw_passes()

    metrics = derive_schedule_gate_metrics(reports, bodies, **HASHES)

    assert metrics == {
        "independent_passes": 7,
        "arrival_multiplier": 10,
        "duration_multiplier": 2.0,
        "p0_p1_deadline_misses": 0,
        "p2_deadline_success_ratio": 1.0,
        "unbounded_queue_growth": 0,
        "all_enabled_job_bodies_covered": True,
    }


def test_duplicate_or_missing_profiles_and_bodies_fail_closed() -> None:
    reports, bodies = _raw_passes()
    reports[1]["validation_profile_id"] = reports[0]["validation_profile_id"]
    _sign(reports[1])
    with pytest.raises(ScheduleEvidenceError, match="profile is missing or duplicated"):
        derive_schedule_gate_metrics(reports, bodies, **HASHES)

    reports, bodies = _raw_passes()
    with pytest.raises(ScheduleEvidenceError, match="requires seven raw"):
        derive_schedule_gate_metrics(reports, bodies[:-1], **HASHES)

    reports, bodies = _raw_passes()
    bodies[1] = copy.deepcopy(bodies[0])
    with pytest.raises(ScheduleEvidenceError, match="body evidence hash is invalid or duplicated"):
        derive_schedule_gate_metrics(reports, bodies, **HASHES)


def test_cron_or_release_source_drift_fails_closed() -> None:
    reports, bodies = _raw_passes()
    drifted = {**HASHES, "cron_jobs_sha256": "f" * 64}

    with pytest.raises(ScheduleEvidenceError, match="source binding drifted"):
        derive_schedule_gate_metrics(reports, bodies, **drifted)


def test_fake_aggregate_cannot_hide_incomplete_body_evidence() -> None:
    reports, bodies = _raw_passes()
    fake_aggregate = {"all_enabled_job_bodies_covered": True, "independent_passes": 7}
    assert fake_aggregate["all_enabled_job_bodies_covered"] is True
    bodies[0]["measurements"]["safe_adapter_coverage_jobs"] = 91  # type: ignore[index]
    _sign(bodies[0])
    body_hash = bodies[0]["evidence_sha256"]
    reports[0]["release_binding"]["real_job_body_evidence_sha256"] = body_hash  # type: ignore[index]
    reports[0]["layers"]["business_body_plane"]["body_evidence"][  # type: ignore[index]
        "registry_evidence_sha256"
    ] = body_hash
    _sign(reports[0])

    with pytest.raises(ScheduleEvidenceError, match="real body coverage is incomplete"):
        derive_schedule_gate_metrics(reports, bodies, **HASHES)


def test_g11_rejects_compressed_active_body_hash_or_raw_sample_drift() -> None:
    reports, bodies = _raw_passes()
    body = bodies[0]
    report = reports[0]
    body["body_results"][0]["sample_evidence_sha256"] = "0" * 64  # type: ignore[index]
    _sign(body)
    body_hash = body["evidence_sha256"]
    report["release_binding"]["real_job_body_evidence_sha256"] = body_hash  # type: ignore[index]
    report["layers"]["business_body_plane"]["body_evidence"][  # type: ignore[index]
        "registry_evidence_sha256"
    ] = body_hash
    _sign(report)

    with pytest.raises(ScheduleEvidenceError, match="duration body hash drifted"):
        derive_schedule_gate_metrics(reports, bodies, **HASHES)

    duration = report["layers"]["business_body_plane"]["duration_evidence"]  # type: ignore[index]
    duration["active_body_evidence_sha256"] = body_hash
    duration["duration_profiles_sha256"] = _digest(
        _duration_profile_hash_payload(duration)
    )
    report["release_binding"]["duration_profiles_sha256"] = duration[  # type: ignore[index]
        "duration_profiles_sha256"
    ]
    _sign(report)
    with pytest.raises(ScheduleEvidenceError, match="raw binding drifted"):
        derive_schedule_gate_metrics(reports, bodies, **HASHES)


@pytest.mark.parametrize("tamper", ["contract_evidence", "database_postcondition"])
def test_g11_recomputes_each_sample_semantic_ledger(tamper: str) -> None:
    reports, bodies = _raw_passes()
    body = bodies[0]
    report = reports[0]
    result = body["body_results"][0]  # type: ignore[index]
    sample = result["sample_evidence"][0]
    if tamper == "contract_evidence":
        contract = sample["success_contract_evidence"]
        contract["checks"]["terminal_postcondition"] = False
        contract["passed_check_count"] = 0
        contract.pop("evidence_sha256")
        contract["evidence_sha256"] = canonical_sha256(contract)
    else:
        dependency = sample["dependency_evidence"]
        dependency["postcondition_count"] = 1
        dependency["passed_postcondition_count"] = 0
        dependency["postconditions_passed"] = False
        dependency.pop("evidence_sha256")
        dependency["evidence_sha256"] = canonical_sha256(dependency)
    sample.pop("evidence_sha256")
    sample["evidence_sha256"] = canonical_sha256(sample)
    result["sample_evidence_sha256"] = canonical_sha256(
        result["sample_evidence"]
    )
    _sign(body)
    body_hash = body["evidence_sha256"]

    duration = report["layers"]["business_body_plane"]["duration_evidence"]  # type: ignore[index]
    active = duration["profile_bindings"][0]["compressed_active"]
    active["sample_evidence"] = result["sample_evidence"]
    active["sample_evidence_sha256"] = result["sample_evidence_sha256"]
    duration["active_body_evidence_sha256"] = body_hash
    duration["duration_profiles_sha256"] = _digest(
        _duration_profile_hash_payload(duration)
    )
    report["release_binding"]["real_job_body_evidence_sha256"] = body_hash  # type: ignore[index]
    report["release_binding"]["duration_profiles_sha256"] = duration[  # type: ignore[index]
        "duration_profiles_sha256"
    ]
    report["layers"]["business_body_plane"]["body_evidence"][  # type: ignore[index]
        "registry_evidence_sha256"
    ] = body_hash
    _sign(report)

    with pytest.raises(
        ScheduleEvidenceError, match="compressed active duration binding failed"
    ):
        derive_schedule_gate_metrics(reports, bodies, **HASHES)


def test_release_gate_rejects_g11_source_role_drift_before_trusting_metrics() -> None:
    artifact = BoundArtifact(
        role="upstream_fake_schedule_aggregate",
        media_type="application/json",
        path="sources/fake.json",
        sha256=hashlib.sha256(b"{}").hexdigest(),
        data=b"{}",
    )

    with pytest.raises(ValueError, match="source roles are not exact"):
        _authoritative_normalized_metrics(
            "seven_day_schedule_10x_arrival_2x_duration_replay_passed",
            [artifact],
            {
                "campaign_id": "campaign-g11",
                "release_sha": "a" * 64,
                "hardware_id": "test-mac",
                "gate_config_sha256": "b" * 64,
            },
            {},
            {},
        )


def _write_json(path: Path, payload: object) -> str:
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_compiler_emits_g11_and_release_gate_recomputes_raw_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    release = tmp_path / "release"
    source_relatives = (
        "config/v3_schedule_dispatch_policy.json",
        "scripts/v3_validation/schedule_capacity_certification.py",
        "scripts/v3_validation/schedule_body_registry.py",
        "config/v3_schedule_body_adapter_registry.json",
        "config/v3_schedule_realism_baseline.json",
    )
    for relative in source_relatives:
        destination = release / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((root / relative).read_bytes())
    source_rows = [
        {
            "path": relative,
            "sha256": hashlib.sha256((release / relative).read_bytes()).hexdigest(),
            "size": (release / relative).stat().st_size,
            "mode": "0444",
        }
        for relative in sorted(source_relatives)
    ]
    release_sha = hashlib.sha256(
        json.dumps(source_rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "immutable": True,
        "release_id": "release-g11-integration",
        "commit": "b" * 40,
        "release_sha256": release_sha,
        "source_snapshot_sha256": release_sha,
        "source_file_count": len(source_rows),
        "files": source_rows,
    }
    manifest_path = release / "release-manifest.json"
    manifest_sha = _write_json(manifest_path, manifest)
    _write_json(
        release / "RELEASE_COMPLETE.json",
        {
            "schema_version": 1,
            "release_id": manifest["release_id"],
            "commit": manifest["commit"],
            "manifest": "release-manifest.json",
            "manifest_sha256": manifest_sha,
            "release_sha256": release_sha,
            "source_snapshot_sha256": release_sha,
            "source_file_count": len(source_rows),
        },
    )
    bundle = ReleaseBundle(
        release,
        str(manifest["release_id"]),
        str(manifest["commit"]),
        release_sha,
        manifest_sha,
        tuple(
            (row["path"], row["sha256"], row["size"], 0o444)  # type: ignore[arg-type]
            for row in source_rows
        ),
    )
    context = CompileContext("campaign-g11", release_sha, "test-mac", "a" * 64)
    state = tmp_path / "campaign"
    cron_path = state / "cron-jobs.json"
    cron_sha = _write_json(cron_path, [])
    bindings = {
        "cron_jobs_sha256": cron_sha,
        "dispatch_policy_sha256": source_rows[1]["sha256"],
        "certifier_sha256": source_rows[3]["sha256"],
        "registry_script_sha256": source_rows[4]["sha256"],
        "registry_config_sha256": source_rows[0]["sha256"],
        "duration_baseline_sha256": source_rows[2]["sha256"],
        "release_id": str(manifest["release_id"]),
        "release_manifest_sha256": manifest_sha,
    }
    by_path = {row["path"]: row["sha256"] for row in source_rows}
    bindings.update(
        {
            "dispatch_policy_sha256": by_path["config/v3_schedule_dispatch_policy.json"],
            "certifier_sha256": by_path[
                "scripts/v3_validation/schedule_capacity_certification.py"
            ],
            "registry_script_sha256": by_path[
                "scripts/v3_validation/schedule_body_registry.py"
            ],
            "registry_config_sha256": by_path[
                "config/v3_schedule_body_adapter_registry.json"
            ],
            "duration_baseline_sha256": by_path[
                "config/v3_schedule_realism_baseline.json"
            ],
        }
    )
    reports, bodies = _raw_passes(bindings)
    profiles = [
        {
            "profile_id": f"profile-{index}",
            "replay_start_local": "2026-07-13T00:00:00+08:00",
            "fault_seed": index,
        }
        for index in range(1, 8)
    ]
    outcomes: list[dict[str, object]] = []
    for index, (profile, capacity, body) in enumerate(
        zip(profiles, reports, bodies, strict=True), 1
    ):
        capacity_path = state / "artifacts" / f"capacity-{index}.json"
        body_path = state / "artifacts" / f"body-{index}.json"
        capacity_sha = _write_json(capacity_path, capacity)
        body_sha = _write_json(body_path, body)
        structured = {
            "schema_version": 1,
            "workload": "seven_day_schedule_10x_arrival_2x_duration_replay",
            "status": "passed",
            "measurements": {"aggregate_claim": "passed"},
            "report": capacity,
                "body_evidence": body,
                "network_access_performed": False,
                "external_network_access_performed": False,
                "service_start_performed": False,
            "production_port_access_performed": False,
            "launchctl_performed": False,
        }
        outcomes.append(
            {
                "validation_pass": index,
                "validation_profile": profile,
                "workload": "seven_day_schedule_10x_arrival_2x_duration_replay",
                "returncode": 0,
                "status": "offline_passed",
                "structured_evidence": structured,
                "inner_report_artifact": {
                    "path": capacity_path.relative_to(state).as_posix(),
                    "sha256": capacity_sha,
                },
                "body_evidence_artifact": {
                    "path": body_path.relative_to(state).as_posix(),
                    "sha256": body_sha,
                },
            }
        )
    now = "2026-07-17T00:00:00+00:00"
    shared = {
        **context.as_dict(),
        "release_id": manifest["release_id"],
        "release_commit": manifest["commit"],
        "release_manifest_sha256": manifest_sha,
        "cron_jobs_sha256": cron_sha,
    }
    day = {
        "schema_version": 1,
        **shared,
        "status": "offline_passed",
        "release_gate_eligible": True,
        "completed_independent_passes": 7,
        "started_at": now,
        "completed_at": now,
        "generated_at": now,
        "live_execution_performed": False,
        "workloads": outcomes,
    }
    day_path = state / "artifacts/day.json"
    day_sha = _write_json(day_path, day)
    campaign = {
        "schema_version": 1,
        **shared,
        "release_sha": release_sha,
        "evidence_class": "immutable_release_offline_campaign",
        "armed": True,
        "certifying": True,
        "harness_certified": True,
        "offline_complete": True,
        "decision": "GO",
        "execution_backend": "release_launcher",
        "fail_closed": False,
        "required_independent_passes": 7,
        "live_execution_performed": False,
        "cutover_execution_performed": False,
        "cron_jobs_file": str(cron_path),
        "artifacts": [{"path": "artifacts/day.json", "sha256": day_sha}],
    }
    campaign_path = state / "campaign-report.json"
    _write_json(campaign_path, campaign)
    monkeypatch.setattr(compiler, "_verify_release", lambda *_args: bundle)
    monkeypatch.setattr(
        compiler,
        "_verify_campaign",
        lambda *_args: (campaign, [day], [campaign_path, day_path]),
    )
    output = tmp_path / "evidence"
    gate_path = root / "config/v3_cutover_gates.json"
    gate_config = json.loads(gate_path.read_text(encoding="utf-8"))

    statuses = compile_campaign_evidence(
        report_path=campaign_path,
        release_root=release,
        output=output,
        context=context,
        config=gate_config,
    )
    decision = evaluate_evidence(
        gate_config,
        output,
        expected_context=context.as_dict(),
        now=datetime(2026, 7, 17, 0, 1, tzinfo=timezone.utc),
    )

    evidence_id = "seven_day_schedule_10x_arrival_2x_duration_replay_passed"
    assert statuses[evidence_id] == "passed"
    assert evidence_id in decision["passed"]
    assert evidence_id not in decision["invalid"]
