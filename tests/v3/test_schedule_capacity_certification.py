from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.v3_validation.schedule_capacity_certification as capacity
from scripts.v3_validation.schedule_capacity_certification import (
    ScheduleCapacityError,
    classify_coalescing_safety,
    LIVE_ROOT,
    Occurrence,
    SameJobCoalescer,
    _combined_duration_replay_profiles,
    _cleanup_certified_campaign_workdir,
    _duration_profile_hash_payload,
    _prepare_workdir,
    run_schedule_capacity_certification,
    simulate_layered_capacity,
    verify_compressed_active_duration_evidence,
    verify_schedule_capacity_evidence,
)
from scripts.v3_validation.schedule_sample_evidence import (
    build_sample_evidence,
    canonical_sha256,
)


ROOT = Path(__file__).resolve().parents[2]


def _occurrence(job_id: str, scheduled_for: float) -> Occurrence:
    return Occurrence(job_id, scheduled_for, "light", 600, 120.0)


def test_coalescing_safety_allows_only_declared_durable_checkpoint_backlogs() -> None:
    safe = {
        "coalesced_distinct_occurrences": 3,
        "coalesced_distinct_occurrences_by_job": {
            "job_drive_case_sync_all_files": 1,
            "job_legacy_judgment_resummary_quality": 2,
        },
    }
    classify_coalescing_safety(safe)
    assert safe["coalescing_safety_passed"] is True
    assert safe["loss_sensitive_coalesced_occurrences"] == 0

    unsafe = {
        "coalesced_distinct_occurrences": 1,
        "coalesced_distinct_occurrences_by_job": {"job_calendar_sync": 1},
    }
    classify_coalescing_safety(unsafe)
    assert unsafe["coalescing_safety_passed"] is False
    assert unsafe["loss_sensitive_coalesced_occurrences_by_job"] == {
        "job_calendar_sync": 1
    }

    with pytest.raises(ScheduleCapacityError, match="total drifted"):
        classify_coalescing_safety(
            {
                "coalesced_distinct_occurrences": 2,
                "coalesced_distinct_occurrences_by_job": {
                    "job_legacy_judgment_resummary_quality": 1
                },
            }
        )


def test_campaign_schedule_body_cache_is_release_bound_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (tmp_path / "source").resolve()
    registry_path = source / capacity.REGISTRY_PATH
    baseline_path = source / "config/v3_schedule_realism_baseline.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text("{}\n", encoding="utf-8")
    baseline_path.write_text("{}\n", encoding="utf-8")
    release_id = "v3-cache-test"
    manifest_sha = "a" * 64
    cron_sha = "b" * 64
    evidence = {
        "schema": capacity.BODY_REGISTRY_SCHEMA,
        "status": "passed",
        "completion_claimed": True,
        "release_binding": {
            "release_id": release_id,
            "release_manifest_sha256": manifest_sha,
            "cron_jobs_sha256": cron_sha,
            "registry_sha256": capacity._sha256_file(registry_path),
            "inherited_baseline_sha256": capacity._sha256_file(baseline_path),
        },
        "external_network_access_performed": False,
        "production_database_access_performed": False,
        "nas_access_performed": False,
        "production_state_write_performed": False,
    }
    evidence["evidence_sha256"] = capacity._sha256(evidence)
    calls = 0

    def fake_registry(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return deepcopy(evidence)

    cache = (tmp_path / "campaign-state/tmp/schedule-body-cache.json").resolve()
    monkeypatch.setenv("MAGI_V3_SCHEDULE_BODY_CACHE", str(cache))
    monkeypatch.setattr(capacity, "run_registry_assessment", fake_registry)

    first, first_reused = capacity._registry_assessment_with_campaign_cache(
        source,
        (tmp_path / "body-work-1").resolve(),
        release_id=release_id,
        release_manifest_sha256=manifest_sha,
        cron_sha256=cron_sha,
    )
    second, second_reused = capacity._registry_assessment_with_campaign_cache(
        source,
        (tmp_path / "body-work-2").resolve(),
        release_id=release_id,
        release_manifest_sha256=manifest_sha,
        cron_sha256=cron_sha,
    )

    assert calls == 1
    assert first == second == evidence
    assert first_reused is False
    assert second_reused is True
    assert cache.stat().st_mode & 0o777 == 0o400


def test_certified_campaign_workdir_cleanup_removes_only_owned_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = (tmp_path / "campaign-tmp").resolve()
    temporary.mkdir()
    workdir = temporary / "schedule-capacity-ordinary_week-owned"
    workdir.mkdir()
    (workdir / ".magi-v3-schedule-capacity-sandbox").write_text(
        "owned disposable schedule-capacity state\n", encoding="utf-8"
    )
    (workdir / "large-disposable-fixture").write_bytes(b"fixture")
    monkeypatch.setenv("TMPDIR", str(temporary))

    _cleanup_certified_campaign_workdir(workdir)

    assert not workdir.exists()


def test_certified_campaign_workdir_cleanup_rejects_unowned_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = (tmp_path / "campaign-tmp").resolve()
    temporary.mkdir()
    workdir = temporary / "schedule-capacity-ordinary_week-unowned"
    workdir.mkdir()
    (workdir / "keep-me").write_text("evidence\n", encoding="utf-8")
    monkeypatch.setenv("TMPDIR", str(temporary))

    with pytest.raises(ScheduleCapacityError, match="cleanup ownership is invalid"):
        _cleanup_certified_campaign_workdir(workdir)

    assert (workdir / "keep-me").read_text(encoding="utf-8") == "evidence\n"


def test_same_job_coalescer_distinguishes_exact_dedup_from_latest_pending() -> None:
    coalescer = SameJobCoalescer()
    first = _occurrence("job-a", 0)
    second = _occurrence("job-a", 60)
    third = _occurrence("job-a", 120)

    offered = coalescer.offer(first)
    assert offered.disposition == "ready"
    assert coalescer.start("job-a", int(offered.generation or 0)) == first
    assert coalescer.offer(first).disposition == "exact_duplicate"
    assert coalescer.offer(second).disposition == "deferred_behind_active"
    replaced = coalescer.offer(third)
    assert replaced.disposition == "coalesced_latest_pending"
    assert replaced.replaced == second
    pending = coalescer.complete("job-a")
    assert pending == (third, replaced.generation)
    assert coalescer.start("job-a", int(replaced.generation or 0)) == third
    assert coalescer.complete("job-a") is None
    assert coalescer.exact_duplicates == 1
    assert coalescer.coalesced_replacements == 1
    assert coalescer.active_count == coalescer.pending_count == 0


def test_layered_capacity_accounts_for_10x_delivery_and_never_runs_same_job_twice() -> None:
    occurrences = [_occurrence("job-a", 0), _occurrence("job-a", 60), _occurrence("job-b", 0)]
    result = simulate_layered_capacity(
        occurrences,
        delivery_multiplier=10,
        slots={"light": 1, "maintenance": 1},
    )

    assert result["input_deliveries"] == 30
    assert result["distinct_scheduled_occurrences"] == 3
    assert result["exact_duplicate_deliveries"] == 27
    assert result["all_deliveries_accounted"] is True
    assert result["all_distinct_occurrences_accounted"] is True
    assert result["executed_occurrences"] + result["coalesced_distinct_occurrences"] == 3
    assert sum(result["coalesced_distinct_occurrences_by_job"].values()) == result[
        "coalesced_distinct_occurrences"
    ]
    assert result["same_job_concurrency_violations"] == 0
    assert result["pending_per_job_limit"] == 1
    assert result["duration_multiplier"] == 2.0
    assert result["coalescing_policy"]["coalesced_occurrences_reported_as_executed"] is False
    assert result["latest_start_misses"] == sum(
        result["latest_start_misses_by_job"].values()
    )
    assert result["deadline_misses"] == sum(result["deadline_misses_by_job"].values())
    assert set(result["max_queue_delay_seconds_by_job"]) == {"job-a", "job-b"}


def _registry_fixture(
    source_root: Path, jobs: list[dict], cron_sha: str, p95_by_job: dict[str, float]
) -> dict:
    entries = []
    results = []
    for index, job in enumerate(jobs, 1):
        job_id = str(job["id"])
        relative = f"job-{index}.py"
        entrypoint = source_root / relative
        entrypoint.write_text(f"# {job_id}\n", encoding="utf-8")
        command_sha = hashlib.sha256(str(job["command"]).encode()).hexdigest()
        entries.append(
            {
                "job_id": job_id,
                "classification": "safe_adapter",
                "blockers": [],
                "actual_entrypoint": relative,
                "production_command_sha256": command_sha,
            }
        )
        p95 = float(p95_by_job[job_id])
        durations = [max(0.001, p95 / 3), max(0.001, p95 / 2), p95]
        entrypoint_sha256 = hashlib.sha256(entrypoint.read_bytes()).hexdigest()
        sample_evidence = []
        for sample_index, duration in enumerate(durations, 1):
            sandbox_sha = hashlib.sha256(
                f"sandbox-{job_id}-{sample_index}".encode()
            ).hexdigest()
            stdout_sha = hashlib.sha256(
                f"stdout-{job_id}-{sample_index}".encode()
            ).hexdigest()
            stderr_sha = hashlib.sha256(
                f"stderr-{job_id}-{sample_index}".encode()
            ).hexdigest()
            sample_evidence.append(
                build_sample_evidence(
                    {
                        "execution_nonce_sha256": hashlib.sha256(
                            f"nonce-{job_id}-{sample_index}".encode()
                        ).hexdigest(),
                        "status": "passed",
                        "executed": True,
                        "returncode": 0,
                        "duration_seconds": duration,
                        "semantic_success": True,
                        "sandbox_profile_sha256": sandbox_sha,
                        "stdout_sha256": stdout_sha,
                        "stderr_sha256": stderr_sha,
                        "diagnostic_evidence_relative_path": "diagnostics/execution.json",
                        "diagnostic_evidence_sha256": hashlib.sha256(
                            f"diagnostic-{job_id}-{sample_index}".encode()
                        ).hexdigest(),
                        "fixture_binding_sha256": hashlib.sha256(
                            f"fixture-binding-{job_id}-{sample_index}".encode()
                        ).hexdigest(),
                        "fixture_initial_inventory_sha256": "2" * 64,
                        "fixture_final_inventory_sha256": "3" * 64,
                        "fixture_final_file_count": 1,
                        "no_fixture_symlinks": True,
                        "success_contract_evidence": {
                            "checks": {"terminal_postcondition": True},
                            "receipt_sha256": hashlib.sha256(
                                f"receipt-{job_id}-{sample_index}".encode()
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
        results.append(
            {
                "job_id": job_id,
                "status": "passed",
                "semantic_success": True,
                "successful_samples": 3,
                "duration_sample_count": 3,
                "duration_samples_seconds": durations,
                "duration_p95_seconds": p95,
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
            }
        )
    registry = {
        "release_binding": {"cron_jobs_sha256": cron_sha},
        "registry_entries": entries,
        "body_results": results,
    }
    registry["evidence_sha256"] = hashlib.sha256(
        json.dumps(registry, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return registry


def _rehash_registry(registry: dict) -> None:
    registry.pop("evidence_sha256", None)
    registry["evidence_sha256"] = hashlib.sha256(
        json.dumps(registry, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _rehash_duration(duration: dict) -> None:
    duration["active_body_evidence_sha256"] = duration.pop(
        "_new_body_sha", duration["active_body_evidence_sha256"]
    )
    duration["duration_profiles_sha256"] = hashlib.sha256(
        json.dumps(
            _duration_profile_hash_payload(duration),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def test_compressed_active_duration_selects_max_in_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = [
        {"id": "history-wins", "enabled": True, "command": "history"},
        {"id": "active-wins", "enabled": True, "command": "active"},
    ]
    cron_sha = "9" * 64
    registry = _registry_fixture(
        tmp_path, jobs, cron_sha, {"history-wins": 5.0, "active-wins": 12.0}
    )
    historical = {
        "history-wins": {
            "duration_seconds": 10.0,
            "duration_basis": "production_success_p95",
            "successful_sample_count": 3,
            "certifying_p95": True,
        },
        "active-wins": {
            "duration_seconds": 4.0,
            "duration_basis": "production_success_p95",
            "successful_sample_count": 3,
            "certifying_p95": True,
        },
    }
    monkeypatch.setattr(
        capacity,
        "bound_duration_replay_profiles",
        lambda *_args: (
            historical,
            {"baseline_sha256": "8" * 64},
        ),
    )

    profiles, evidence = _combined_duration_replay_profiles(
        tmp_path, jobs, cron_sha, registry
    )

    assert profiles["history-wins"]["duration_seconds"] == 10.0
    assert profiles["active-wins"]["duration_seconds"] == 12.0
    assert all(
        row["selected_duration_basis"]
        == "max_historical_production_p95_and_compressed_active_bounded_body_p95"
        for row in evidence["profile_bindings"]
    )
    assert evidence["compressed_active_p95_jobs"] == 2
    assert evidence["certifying_p95_coverage"] is True
    assert "not historical production observations" in evidence["active_sample_disclosure"]
    verify_compressed_active_duration_evidence(
        evidence, registry, cron_jobs_sha256=cron_sha, require_complete=True
    )


@pytest.mark.parametrize(
    "tamper",
    ["sample_hash", "entrypoint_hash", "command_hash", "semantic_success"],
)
def test_compressed_active_duration_raw_binding_tamper_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    jobs = [{"id": "job-a", "enabled": True, "command": "fixture-command"}]
    cron_sha = "7" * 64
    registry = _registry_fixture(tmp_path, jobs, cron_sha, {"job-a": 6.0})
    monkeypatch.setattr(
        capacity,
        "bound_duration_replay_profiles",
        lambda *_args: (
            {
                "job-a": {
                    "duration_seconds": 5.0,
                    "certifying_p95": True,
                }
            },
            {"baseline_sha256": "6" * 64},
        ),
    )
    _profiles, evidence = _combined_duration_replay_profiles(
        tmp_path, jobs, cron_sha, registry
    )
    bad_registry = deepcopy(registry)
    if tamper == "sample_hash":
        bad_registry["body_results"][0]["sample_evidence_sha256"] = "0" * 64
    elif tamper == "entrypoint_hash":
        bad_registry["body_results"][0]["entrypoint_sha256"] = "0" * 64
    elif tamper == "command_hash":
        bad_registry["registry_entries"][0]["production_command_sha256"] = "0" * 64
    else:
        bad_registry["body_results"][0]["semantic_success"] = False
    _rehash_registry(bad_registry)
    bad_evidence = deepcopy(evidence)
    bad_evidence["_new_body_sha"] = bad_registry["evidence_sha256"]
    _rehash_duration(bad_evidence)

    with pytest.raises(ScheduleCapacityError, match="raw binding drifted"):
        verify_compressed_active_duration_evidence(
            bad_evidence,
            bad_registry,
            cron_jobs_sha256=cron_sha,
            require_complete=True,
        )


def test_compressed_active_duration_never_certifies_fewer_than_three_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = [{"id": "job-a", "enabled": True, "command": "fixture-command"}]
    cron_sha = "7" * 64
    registry = _registry_fixture(tmp_path, jobs, cron_sha, {"job-a": 6.0})
    result = registry["body_results"][0]
    result["successful_samples"] = 2
    result["duration_sample_count"] = 2
    result["duration_samples_seconds"] = result["duration_samples_seconds"][:2]
    result["sandbox_profile_sha256_samples"] = result[
        "sandbox_profile_sha256_samples"
    ][:2]
    result["stdout_sha256_samples"] = result["stdout_sha256_samples"][:2]
    result["stderr_sha256_samples"] = result["stderr_sha256_samples"][:2]
    _rehash_registry(registry)
    monkeypatch.setattr(
        capacity,
        "bound_duration_replay_profiles",
        lambda *_args: (
            {"job-a": {"duration_seconds": 5.0, "certifying_p95": True}},
            {"baseline_sha256": "6" * 64},
        ),
    )

    with pytest.raises(ScheduleCapacityError, match="binding is invalid"):
        _combined_duration_replay_profiles(tmp_path, jobs, cron_sha, registry)


@pytest.mark.parametrize(
    "tamper",
    ["drop", "replace", "contract_evidence", "database_postcondition"],
)
def test_compressed_active_duration_recomputes_each_sample_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    jobs = [{"id": "job-a", "enabled": True, "command": "fixture-command"}]
    cron_sha = "7" * 64
    registry = _registry_fixture(tmp_path, jobs, cron_sha, {"job-a": 6.0})
    monkeypatch.setattr(
        capacity,
        "bound_duration_replay_profiles",
        lambda *_args: (
            {"job-a": {"duration_seconds": 5.0, "certifying_p95": True}},
            {"baseline_sha256": "6" * 64},
        ),
    )
    _profiles, evidence = _combined_duration_replay_profiles(
        tmp_path, jobs, cron_sha, registry
    )
    bad_registry = deepcopy(registry)
    result = bad_registry["body_results"][0]
    samples = result["sample_evidence"]
    if tamper == "drop":
        samples.pop()
    elif tamper == "replace":
        samples[1] = deepcopy(samples[0])
    elif tamper == "contract_evidence":
        contract = samples[0]["success_contract_evidence"]
        contract["checks"]["terminal_postcondition"] = False
        contract["passed_check_count"] = 0
        contract.pop("evidence_sha256")
        contract["evidence_sha256"] = canonical_sha256(contract)
        samples[0].pop("evidence_sha256")
        samples[0]["evidence_sha256"] = canonical_sha256(samples[0])
    else:
        dependency = samples[0]["dependency_evidence"]
        dependency["postcondition_count"] = 1
        dependency["passed_postcondition_count"] = 0
        dependency["postconditions_passed"] = False
        dependency.pop("evidence_sha256")
        dependency["evidence_sha256"] = canonical_sha256(dependency)
        samples[0].pop("evidence_sha256")
        samples[0]["evidence_sha256"] = canonical_sha256(samples[0])
    result["sample_evidence_sha256"] = canonical_sha256(samples)
    _rehash_registry(bad_registry)

    bad_evidence = deepcopy(evidence)
    active = bad_evidence["profile_bindings"][0]["compressed_active"]
    active["sample_evidence"] = samples
    active["sample_evidence_sha256"] = result["sample_evidence_sha256"]
    bad_evidence["_new_body_sha"] = bad_registry["evidence_sha256"]
    _rehash_duration(bad_evidence)

    with pytest.raises(ScheduleCapacityError, match="raw binding drifted"):
        verify_compressed_active_duration_evidence(
            bad_evidence,
            bad_registry,
            cron_jobs_sha256=cron_sha,
            require_complete=True,
        )


def test_capacity_runs_registry_before_building_duration_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []
    jobs = [{"id": "job-a", "enabled": True, "command": "fixture"}]
    policy = SimpleNamespace(
        lane_caps={"light": 2, "maintenance": 1, "batch": 1},
        policy_sha256="5" * 64,
    )
    monkeypatch.setattr(capacity, "bound_cron_jobs", lambda _root: (jobs, "4" * 64))
    monkeypatch.setattr(capacity, "_replay_profile", lambda: ("profile", None))
    monkeypatch.setattr(capacity, "load_cron_dispatch_policy", lambda _root: policy)
    monkeypatch.setattr(
        capacity,
        "GlobalResourceGovernor",
        lambda: SimpleNamespace(policy=SimpleNamespace(max_light=2, max_heavy=1)),
    )
    monkeypatch.setattr(
        capacity,
        "run_registry_assessment",
        lambda *_args, **_kwargs: events.append("registry") or {},
    )
    monkeypatch.setattr(capacity, "_body_coverage", lambda *_args: {})

    class ProfilesReached(RuntimeError):
        pass

    def profiles(*_args):
        events.append("profiles")
        raise ProfilesReached

    monkeypatch.setattr(capacity, "_combined_duration_replay_profiles", profiles)
    with pytest.raises(ProfilesReached):
        capacity._run_schedule_capacity_certification_bundle(
            ROOT,
            tmp_path / "capacity-order",
            release_id="release-order",
            release_manifest_sha256="3" * 64,
        )
    assert events == ["registry", "profiles"]


def test_current_schedule_capacity_uses_release_bound_compressed_active_p95(
    tmp_path: Path,
) -> None:
    evidence = run_schedule_capacity_certification(ROOT, tmp_path / "schedule-capacity")

    assert evidence["status"] == "certified"
    assert evidence["layers"]["control_plane"]["status"] == "passed"
    control = evidence["layers"]["control_plane"]["measurements"]
    assert control["delivery_multiplier"] == 10
    assert control["duration_multiplier"] == 2.0
    assert control["all_deliveries_accounted"] is True
    assert control["same_job_concurrency_violations"] == 0
    assert evidence["layers"]["control_plane"]["uses_dispatcher_latency_as_body_duration"] is False

    body = evidence["layers"]["business_body_plane"]
    assert body["status"] == "passed"
    duration = body["duration_evidence"]
    baseline = json.loads((ROOT / "config/v3_schedule_realism_baseline.json").read_text(encoding="utf-8"))
    assert duration["p95_jobs"] == 96
    assert duration["historical_production_p95_jobs"] == baseline["coverage"]["jobs_meeting_minimum_samples"]
    assert duration["compressed_active_p95_jobs"] == 96
    assert duration["sparse_fallback_jobs"] == 0
    assert duration["certifying_p95_coverage"] is True
    assert len(duration["profile_bindings"]) == 96
    assert all(
        row["compressed_active"]["sample_kind"]
        == "compressed_active_bounded_real_entrypoint"
        and row["compressed_active"]["successful_samples"] == 3
        and row["compressed_active"]["semantic_success"] is True
        for row in duration["profile_bindings"]
    )
    assert body["body_evidence"]["jobs_with_three_successful_real_body_samples"] == 96
    assert body["body_evidence"]["jobs_missing_real_body_adapter"] == 0
    assert body["body_evidence"]["body_adapter_coverage_complete"] is True
    assert body["dispatcher_or_help_latency_substituted"] is False
    assert body["deadline_measurements"]["latest_start_misses"] == 0
    assert body["deadline_measurements"]["deadline_misses"] == 0
    assert control["coalesced_distinct_occurrences"] > 0
    assert control["durable_backlog_coalescing_job_ids"] == [
        "job_drive_case_sync_all_files",
        "job_legacy_judgment_resummary_quality"
    ]
    assert control["coalesced_distinct_occurrences"] == control[
        "durable_backlog_coalesced_occurrences"
    ]
    assert control["loss_sensitive_coalesced_occurrences"] == 0
    assert control["loss_sensitive_coalesced_occurrences_by_job"] == {}
    assert control["coalescing_safety_passed"] is True
    assert control["worker_slots"] == {"light": 2, "batch": 2, "maintenance": 2}
    assert control["global_worker_cap"] == 4
    assert control["shared_lane_caps"] == {
        "heavy": {"lanes": ["batch", "maintenance"], "slots": 2}
    }
    assert evidence["gate"]["eligible_to_clear_schedule_realism_blocker"] is True
    assert evidence["gate"]["blocking_reasons"] == []
    verify_schedule_capacity_evidence(evidence)


def test_schedule_capacity_hash_and_protected_workdirs_fail_closed(tmp_path: Path) -> None:
    evidence = {"schema": "magi.v3.schedule-capacity-certification/v1", "status": "incomplete"}
    evidence["evidence_sha256"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    evidence["status"] = "certified"
    with pytest.raises(ScheduleCapacityError, match="does not match"):
        verify_schedule_capacity_evidence(evidence)
    with pytest.raises(ScheduleCapacityError, match="live MAGI"):
        _prepare_workdir(LIVE_ROOT)
    with pytest.raises(ScheduleCapacityError, match="source tree"):
        _prepare_workdir(ROOT / "forbidden-schedule-capacity")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "preserve").write_text("preserve", encoding="utf-8")
    with pytest.raises(ScheduleCapacityError, match="empty"):
        _prepare_workdir(occupied)
    assert (occupied / "preserve").read_text(encoding="utf-8") == "preserve"


def test_capacity_diagnostic_reports_are_atomically_retained(tmp_path: Path) -> None:
    report = {"schema": "fixture", "status": "incomplete", "count": 92}

    capacity._persist_diagnostic_report(tmp_path, "capacity.json", report)

    assert json.loads((tmp_path / "capacity.json").read_text(encoding="utf-8")) == report
    assert not list(tmp_path.glob(".capacity.json.tmp-*"))
