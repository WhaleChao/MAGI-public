from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.v3_campaign.offline_probes import OfflineProbeError, bound_cron_jobs
from scripts.v3_campaign.schedule_realism import (
    BASELINE_PATH,
    MIN_SUCCESSFUL_SAMPLES,
    REALISM_WORKLOAD,
    SOURCE_EVIDENCE_RECEIPT_FIELD,
    _logical_definition_sha256,
    _source_evidence_receipt_sha256,
    _validate_adapter,
    _validate_baseline,
    bound_duration_replay_profiles,
    run_schedule_realism_assessment,
)
from scripts.v3_validation.schedule_sample_evidence import (
    verify_sample_evidence_ledger,
)
from skills.ops.cron_command_identity import command_definition_sha256


ROOT = Path(__file__).resolve().parents[2]


def test_command_identity_treats_release_root_rebase_and_quoting_as_equivalent() -> None:
    source = (
        "/opt/magi/source/MAGI_v2/venv/bin/python3 "
        "/opt/magi/source/MAGI_v2/scripts/task.py --max-depth 20"
    )
    live = (
        "'/opt/magi/live/MAGI_v2/venv/bin/python3' "
        "'/opt/magi/live/MAGI_v2/scripts/task.py' "
        "--max-depth 20"
    )

    assert command_definition_sha256(source) == command_definition_sha256(live)


def test_command_identity_treats_v3_checkout_and_legacy_root_as_equivalent() -> None:
    legacy = (
        "/opt/magi/source/MAGI_v2/venv/bin/python3 "
        "/opt/magi/source/MAGI_v2/scripts/task.py --max-depth 20"
    )
    checkout = (
        f"{ROOT / 'venv/bin/python3'} "
        f"{ROOT / 'scripts/task.py'} --max-depth 20"
    )

    assert command_definition_sha256(legacy) == command_definition_sha256(checkout)


def test_command_identity_treats_complete_mutable_checkout_pair_as_rebased(
    tmp_path: Path,
) -> None:
    first = tmp_path / "mutable-a"
    second = tmp_path / "mutable-b"
    command_a = f"{first / 'venv/bin/python3'} {first / 'scripts/task.py'} --safe"
    command_b = f"{second / 'venv/bin/python3'} {second / 'scripts/task.py'} --safe"

    assert command_definition_sha256(command_a) == command_definition_sha256(command_b)


def test_command_identity_treats_manifest_bound_candidate_rebase_as_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_id = "v3-20260718-test"
    candidate = tmp_path / release_id
    runtime = tmp_path / "runtime" / "venv" / "bin" / "python"
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", release_id)
    monkeypatch.setenv(
        "MAGI_V3_RELEASE_MANIFEST", str(candidate / "release-manifest.json")
    )
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(runtime))
    source = (
        "/opt/magi/source/MAGI_v2/venv/bin/python3 "
        "/opt/magi/source/MAGI_v2/scripts/ops/run_with_env.py "
        "MAGI_OBSIDIAN_AGENT_DIR=/opt/magi/source/MAGI_v2/.agent -- "
        "/opt/magi/source/MAGI_v2/venv/bin/python3 "
        "/opt/magi/source/MAGI_v2/scripts/task.py --max-depth 20 "
        "--json-out /opt/magi/source/MAGI_v2/.runtime/latest.json"
    )
    rebound = (
        f"{runtime} {candidate / 'scripts/ops/run_with_env.py'} "
        f"MAGI_OBSIDIAN_AGENT_DIR={candidate / 'agent'} -- "
        f"{runtime} {candidate / 'scripts/task.py'} --max-depth 20 "
        f"--json-out {candidate / 'runtime/latest.json'}"
    )

    assert command_definition_sha256(source) == command_definition_sha256(rebound)


@pytest.mark.parametrize(
    ("legacy_relative", "shared_relative"),
    [
        (".runtime/latest.json", "runtime/latest.json"),
        (".agent/state.json", "agent/state.json"),
        ("static/latest.json", "static/latest.json"),
        ("exports/latest.json", "exports/latest.json"),
        ("_metrics/latest.json", "metrics/latest.json"),
        ("_autopilot_runs/latest.json", "autopilot-runs/latest.json"),
    ],
)
def test_command_identity_treats_bound_external_runtime_shared_paths_as_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_relative: str,
    shared_relative: str,
) -> None:
    release_id = "v3-20260720-test"
    candidate = tmp_path / release_id
    runtime = tmp_path / "runtime" / "venv" / "bin" / "python"
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", release_id)
    monkeypatch.setenv(
        "MAGI_V3_RELEASE_MANIFEST", str(candidate / "release-manifest.json")
    )
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(runtime))
    source = (
        "/opt/magi/source/MAGI_v2/venv/bin/python3 "
        "/opt/magi/source/MAGI_v2/scripts/task.py --output "
        f"/opt/magi/source/MAGI_v2/{legacy_relative}"
    )
    rebound = (
        f"{runtime} {candidate / 'scripts/task.py'} --output "
        f"{runtime.parent.parent / 'shared' / shared_relative}"
    )

    assert command_definition_sha256(source) == command_definition_sha256(rebound)


def test_command_identity_does_not_collapse_unbound_shared_runtime_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "bound" / "venv" / "bin" / "python"
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(runtime))
    source = "/opt/magi/source/MAGI_v2/.runtime/latest.json"
    unrelated = tmp_path / "unbound" / "venv" / "shared" / "runtime" / "latest.json"

    assert command_definition_sha256(source) != command_definition_sha256(str(unrelated))


@pytest.mark.parametrize(
    "changed",
    [
        "scripts/task.py --max-depth 5 --max-items 1000 --file-timeout-sec 45",
        "scripts/task.py --max-depth 20 --max-items 240 --file-timeout-sec 45",
        "scripts/task.py --max-depth 20 --max-items 1000 --file-timeout-sec 90",
    ],
)
def test_command_identity_detects_argument_value_drift(changed: str) -> None:
    expected = "scripts/task.py --max-depth 20 --max-items 1000 --file-timeout-sec 45"

    assert command_definition_sha256(changed) != command_definition_sha256(expected)


def test_command_identity_does_not_rebase_unrelated_absolute_code_anchor() -> None:
    first = "/srv/tenant-a/scripts/task.py --mode verify"
    second = "/srv/tenant-b/scripts/task.py --mode verify"

    assert command_definition_sha256(first) != command_definition_sha256(second)


def test_command_identity_does_not_collapse_external_python_interpreters() -> None:
    first = "/opt/python-a/bin/python3 scripts/task.py"
    second = "/opt/python-b/bin/python3 scripts/task.py"

    assert command_definition_sha256(first) != command_definition_sha256(second)


def _load_inputs() -> tuple[dict, list[dict], str]:
    baseline = json.loads((ROOT / BASELINE_PATH).read_text(encoding="utf-8"))
    jobs, digest = bound_cron_jobs(ROOT)
    return baseline, jobs, digest


def test_duration_baseline_is_hash_bound_partial_production_evidence() -> None:
    baseline, jobs, digest = _load_inputs()
    observations, gaps = _validate_baseline(baseline, jobs, digest)
    enabled_ids = {row["id"] for row in jobs if row.get("enabled") is True}
    missing_ids = enabled_ids - set(observations)

    assert baseline["schema_version"] == 2
    assert baseline["status"] == "incomplete"
    assert baseline["source_evidence"]["runtime_state_read_only_snapshot"] is True
    assert baseline["source_evidence"]["raw_runtime_state_included"] is False
    assert len(observations) == (
        baseline["coverage"]["enabled_job_definitions"] - len(missing_ids)
    )
    assert "job_file_review_check" in observations
    assert gaps == sorted(missing_ids)
    assert {
        row["job_id"] for row in baseline["invalidated_observations"]
    } <= missing_ids
    assert all(item["sample_count"] >= 1 for item in observations.values())
    deep_sample_count = sum(item["sample_count"] >= 3 for item in observations.values())
    assert deep_sample_count == baseline["coverage"]["jobs_meeting_minimum_samples"]
    assert 0 < deep_sample_count < len(observations)
    assert baseline["coverage"]["global_duration_percentile_available"] is False
    assert baseline["duration_policy"]["ledger_lifecycle_latency_is_not_a_job_body_duration"] is True
    assert baseline["duration_policy"]["global_substitution_allowed"] is False


def test_duration_profiles_use_p95_only_with_three_independent_successes() -> None:
    baseline, jobs, digest = _load_inputs()
    profiles, coverage = bound_duration_replay_profiles(ROOT, jobs, digest)
    invalidated_ids = coverage["missing_job_ids"]

    assert len(profiles) == (
        baseline["coverage"]["enabled_job_definitions"] - len(invalidated_ids)
    )
    assert coverage["enabled_jobs"] == 96
    assert coverage["profiles"] == len(profiles)
    assert coverage["p95_jobs"] == baseline["coverage"]["jobs_meeting_minimum_samples"]
    assert coverage["sparse_fallback_jobs"] == len(profiles) - coverage["p95_jobs"]
    assert coverage["missing_jobs"] == len(invalidated_ids)
    assert coverage["missing_job_ids"] == invalidated_ids
    assert coverage["minimum_successful_samples"] == MIN_SUCCESSFUL_SAMPLES
    assert coverage["certifying_p95_coverage"] is False
    assert len(coverage["duration_profiles_sha256"]) == 64
    assert all(
        row["duration_basis"] == "production_success_p95"
        for row in profiles.values()
        if row["certifying_p95"] is True
    )
    assert all(
        row["duration_basis"]
        == "production_success_observed_max_sparse_fallback"
        for row in profiles.values()
        if row["certifying_p95"] is False
    )


def test_duration_baseline_rejects_forged_p95() -> None:
    baseline, jobs, digest = _load_inputs()
    changed = json.loads(json.dumps(baseline))
    observation = next(
        row for row in changed["observations"] if row["sample_count"] >= 3
    )
    observation["duration_p95_seconds"] += 0.001

    with pytest.raises(OfflineProbeError, match="p95 is inconsistent"):
        _validate_baseline(changed, jobs, digest)


def test_p95_uses_the_same_nearest_rank_definition_as_baseline_capture() -> None:
    from scripts.v3_campaign.schedule_realism import _p95

    assert _p95([float(value) for value in range(1, 21)]) == 19.0


@pytest.mark.parametrize(
    ("field", "tampered", "error"),
    [
        ("runtime_state_sha256", "A" * 64, "source hash is malformed"),
        (
            "runtime_state_read_only_snapshot",
            False,
            "runtime state source evidence is not fail-closed",
        ),
        (
            "raw_runtime_state_included",
            True,
            "runtime state source evidence is not fail-closed",
        ),
        (
            "legacy_naive_timestamp_timezone",
            "UTC",
            "runtime state source evidence is not fail-closed",
        ),
        (
            "observation_timestamps_normalized_to_utc",
            False,
            "runtime state source evidence is not fail-closed",
        ),
    ],
)
def test_duration_baseline_rejects_tampered_runtime_source_evidence(
    field: str, tampered: object, error: str
) -> None:
    baseline, jobs, digest = _load_inputs()
    changed = json.loads(json.dumps(baseline))
    source = changed["source_evidence"]
    source[SOURCE_EVIDENCE_RECEIPT_FIELD] = _source_evidence_receipt_sha256(source)
    source[field] = tampered
    source[SOURCE_EVIDENCE_RECEIPT_FIELD] = _source_evidence_receipt_sha256(source)

    with pytest.raises(OfflineProbeError, match=error):
        _validate_baseline(changed, jobs, digest)


def test_duration_baseline_rejects_legal_runtime_hash_substitution() -> None:
    baseline, jobs, digest = _load_inputs()
    changed = json.loads(json.dumps(baseline))
    source = changed["source_evidence"]
    source[SOURCE_EVIDENCE_RECEIPT_FIELD] = _source_evidence_receipt_sha256(source)
    replacement = "0" * 64
    if source["runtime_state_sha256"] == replacement:
        replacement = "1" * 64
    source["runtime_state_sha256"] = replacement

    with pytest.raises(OfflineProbeError, match="source receipt does not match"):
        _validate_baseline(changed, jobs, digest)


def test_duration_baseline_rejects_definition_drift() -> None:
    baseline, jobs, _digest = _load_inputs()
    changed = [dict(item) for item in jobs]
    changed[0]["cron"] = "1 1 1 1 1"

    with pytest.raises(OfflineProbeError, match="not bound"):
        _validate_baseline(baseline, changed, "0" * 64)


def test_logical_definition_ignores_runtime_result_evidence() -> None:
    _baseline, jobs, _digest = _load_inputs()
    changed = [dict(job) for job in jobs]
    changed[0]["result_evidence"] = {
        "status": "runtime_observation_only",
        "observed_at": "2099-01-01T00:00:00Z",
    }

    assert _logical_definition_sha256(changed) == _logical_definition_sha256(jobs)


def test_real_job_body_harness_is_sandboxed_and_keeps_gaps_machine_readable(
    tmp_path: Path,
) -> None:
    baseline, jobs, _digest = _load_inputs()
    allowlisted_ids = {
        item["job_id"] for item in baseline["representative_body_allowlist"]
    }
    enabled_ids = {item["id"] for item in jobs if item.get("enabled") is True}
    evidence = run_schedule_realism_assessment(ROOT, tmp_path)

    assert evidence["schema_version"] == 1
    assert evidence["workload"] == REALISM_WORKLOAD
    assert evidence["status"] == "incomplete"
    assert evidence["completion_claimed"] is False
    assert evidence["blocker"] == {
        "code": "SCHEDULE_LOAD_REALISM_INCOMPLETE",
        "eligible_to_clear": False,
        "decision": "blocker_retained",
        "reasons": [
            "MISSING_SUCCESSFUL_PRODUCTION_DURATION",
            "PRODUCTION_P95_SAMPLE_COVERAGE_INCOMPLETE",
            "REAL_JOB_BODY_ADAPTER_COVERAGE_INCOMPLETE",
        ],
    }
    assert evidence["network_access_performed"] is False
    assert evidence["production_database_access_performed"] is False
    assert evidence["nas_access_performed"] is False
    assert evidence["live_service_access_performed"] is False
    assert evidence["production_state_write_performed"] is False
    assert evidence["sandbox_writes_only"] is True
    supplied_hash = evidence["evidence_sha256"]
    unhashed = dict(evidence)
    unhashed.pop("evidence_sha256")
    assert supplied_hash == hashlib.sha256(
        json.dumps(
            unhashed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert len(evidence["release_binding"]["baseline_sha256"]) == 64
    assert evidence["release_binding"]["cron_jobs_sha256"] == _digest

    measurements = evidence["measurements"]
    assert measurements["cron_definitions"] == len(jobs)
    assert measurements["enabled_cron_definitions"] == len(enabled_ids)
    successful_duration_jobs = baseline["coverage"][
        "enabled_jobs_with_successful_duration"
    ]
    p95_jobs = baseline["coverage"]["jobs_meeting_minimum_samples"]
    assert measurements["production_duration_observations"] == successful_duration_jobs
    assert measurements["production_duration_gap_jobs"] == len(enabled_ids) - successful_duration_jobs
    assert measurements["production_duration_p95_jobs"] == p95_jobs
    assert measurements["production_duration_sparse_sample_jobs"] == successful_duration_jobs - p95_jobs
    assert measurements["minimum_successful_duration_samples"] == 3
    assert measurements["production_duration_percentile_available"] is False
    assert measurements["representative_bodies_allowlisted"] == len(allowlisted_ids)
    assert measurements["representative_bodies_passed"] == len(allowlisted_ids)
    assert measurements["representative_body_gap_jobs"] == len(enabled_ids - allowlisted_ids)
    assert measurements["representative_body_minimum_samples"] == 3
    assert measurements["representative_body_jobs_meeting_minimum_samples"] == len(
        allowlisted_ids
    )

    body_results = {item["job_id"]: item for item in evidence["body_results"]}
    assert set(body_results) == allowlisted_ids
    assert all(item["executed"] for item in body_results.values())
    assert all(item["status"] == "passed" for item in body_results.values())
    assert all(item["semantic_success"] is True for item in body_results.values())
    assert all(item["samples_requested"] == 3 for item in body_results.values())
    assert all(item["successful_samples"] == 3 for item in body_results.values())
    assert all(item["duration_sample_count"] == 3 for item in body_results.values())
    assert all(len(item["duration_samples_seconds"]) == 3 for item in body_results.values())
    assert all(item["duration_p95_seconds"] > 0 for item in body_results.values())
    assert all(len(item["sample_evidence_sha256"]) == 64 for item in body_results.values())
    assert all(
        verify_sample_evidence_ledger(item, minimum_samples=3)
        for item in body_results.values()
    )
    assert all(len(item["sandbox_profile_sha256"]) == 64 for item in body_results.values())
    assert all(item["adapter_mode"] == "real_entrypoint_dry_run_v1" for item in body_results.values())
    assert all(item["adapter_dry_run"] is True for item in body_results.values())
    assert all(item["network_denied_by_seatbelt"] is True for item in body_results.values())
    assert all(item["notifications_disabled"] is True for item in body_results.values())
    assert all(len(item["adapter_fixture_manifest_sha256"]) == 64 for item in body_results.values())

    gaps = evidence["gaps"]
    duration_gaps = [item for item in gaps if item["gap_type"] == "production_duration"]
    body_gaps = [item for item in gaps if item["gap_type"] == "representative_job_body"]
    # The exact identities are governed by the hash-bound baseline.  Hard-coding
    # an older list made a legitimate command update (for example the new
    # all-case calendar scan) look like a product regression even though the
    # baseline correctly invalidated the stale duration sample.
    observed_duration_ids = {item["job_id"] for item in baseline["observations"]}
    assert {item["job_id"] for item in duration_gaps} == (
        enabled_ids - observed_duration_ids
    )
    assert len(body_gaps) == len(enabled_ids - allowlisted_ids)
    assert {item["job_id"] for item in body_gaps} == enabled_ids - allowlisted_ids
    assert all(item["job_id"] and item["reasons"] for item in gaps)
    depth_gaps = [
        item
        for item in gaps
        if item["gap_type"] == "production_duration_sample_depth"
    ]
    assert len(depth_gaps) == successful_duration_jobs - p95_jobs
    assert any("NETWORK_OR_EXTERNAL_SERVICE_RISK" in item["reasons"] for item in body_gaps)
    assert any("NAS_OR_EXTERNAL_STORAGE_RISK" in item["reasons"] for item in body_gaps)
    assert any("PRODUCTION_MUTATION_RISK" in item["reasons"] for item in body_gaps)


def test_non_allowlisted_jobs_are_never_executed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    baseline, jobs, _digest = _load_inputs()
    allowlisted_ids = sorted(
        item["job_id"] for item in baseline["representative_body_allowlist"]
    )
    enabled_count = sum(item.get("enabled") is True for item in jobs)
    calls: list[str] = []

    def fake_execute(source_root, workdir, job, allow):
        calls.append(str(job["id"]))
        return {
            "job_id": allow["job_id"],
            "status": "passed",
            "executed": True,
            "semantic_success": True,
            "duration_seconds": 0.001,
            "sandbox_profile_sha256": "a" * 64,
            "adapter_mode": "real_entrypoint_dry_run_v1",
            "adapter_dry_run": True,
            "adapter_fixture_manifest_sha256": "b" * 64,
            "adapter_fixture_files": [],
            "network_denied_by_seatbelt": True,
            "notifications_disabled": True,
        }

    monkeypatch.setattr(
        "scripts.v3_campaign.schedule_realism._execute_allowlisted_body",
        fake_execute,
    )
    evidence = run_schedule_realism_assessment(ROOT, tmp_path)

    assert calls == [
        job_id
        for job_id in allowlisted_ids
        for _sample in range(MIN_SUCCESSFUL_SAMPLES)
    ]
    assert evidence["measurements"]["representative_body_gap_jobs"] == (
        enabled_count - len(allowlisted_ids)
    )


def test_real_body_adapter_contract_fails_closed_on_missing_controls() -> None:
    with pytest.raises(OfflineProbeError, match="exact dry-run adapter contract"):
        _validate_adapter({
            "job_id": "job_fixture",
            "adapter": {
                "mode": "real_entrypoint_dry_run_v1",
                "dry_run": True,
                "fixture_root_required": True,
                "network": "deny_seatbelt",
                "notifications": "allow",
                "writes": "fixture_root_only",
            },
        })
