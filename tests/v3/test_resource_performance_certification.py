from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.v3_evidence_compiler import CompileContext, compile_campaign_evidence
from scripts.v3_release_gate import evaluate_evidence
from scripts.v3_validation import resource_performance_certification as certification
from scripts.v3_validation.resource_performance_evidence import (
    GATE_IDS,
    MATCHED_PRODUCTION_MISSING_REQUIREMENTS,
    MATCHED_PRODUCTION_SCOPE_LIMITATIONS,
    ResourcePerformanceEvidenceError,
    _worker_metrics,
    sha256_json,
    summarize_report,
)
from tests.v3 import test_campaign_runner as campaign_fixtures
from tests.v3.test_isolated_resource_window import passing_report as passing_window_report


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _partial_inputs(
    release: Path,
) -> tuple[dict[str, object], dict[str, str], str, dict[str, object]]:
    command = (
        str(release / "bin/magi-v3-python"),
        str(
            release
            / "scripts/v3_validation/resource_performance_certification.py"
        ),
        "--campaign-evidence",
    )
    profile = {
        "profile_id": "ordinary_week",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    completed = campaign_fixtures.successful_runner([])(command, release, profile)
    outer = json.loads(completed.stdout.split("=", 1)[1])
    report = outer["report"]
    manifest = json.loads((release / "release-manifest.json").read_text())
    release_files = {str(row["path"]): str(row["sha256"]) for row in manifest["files"]}
    runtime_sha = str(report["release_binding"]["python_runtime_sha256"])
    return report, release_files, runtime_sha, profile


@pytest.mark.parametrize(
    "mutation",
    [
        "model_ratio",
        "matched_dependencies",
        "disposable_dependencies",
        "manual_preemption",
        "attempt_status",
        "normalized_fake_pass",
        "worker_return",
        "metal",
        "safety",
    ],
)
def test_partial_report_tampering_never_promotes_a_missing_capability(
    tmp_path: Path, mutation: str
) -> None:
    release, _release_sha = campaign_fixtures.create_release(tmp_path)
    report, release_files, runtime_sha, profile = _partial_inputs(release)
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "model_ratio":
        report["metrics"][GATE_IDS[0]]["minimum_model_tokens_per_second_ratio"] = 1.0
    elif mutation == "matched_dependencies":
        report["metrics"][GATE_IDS[0]]["matched_production_dependencies"] = True
    elif mutation == "disposable_dependencies":
        report["metrics"][GATE_IDS[0]]["matched_disposable_dependencies"] = False
    elif mutation == "manual_preemption":
        report["preemption_probe"]["manual_owned_cleanup_performed"] = True
    elif mutation == "attempt_status":
        report["preemption_probe"]["samples"][0]["attempts"][0][1] = "failed"
    elif mutation == "normalized_fake_pass":
        # Retain the already-passing normalized metrics while corrupting the
        # raw automatic-preemption observation. Rehashing cannot promote it.
        report["preemption_probe"]["samples"][0]["manual_terminate_invoked"] = True
    elif mutation == "metal":
        report["worker_capability_probe"]["metal_measurement_available"] = True
    elif mutation == "worker_return":
        report["worker_capability_probe"]["samples"][0][
            "physical_footprint_returned_to_zero"
        ] = False
    else:
        report["safety"]["network_denied_by_seatbelt"] = False
    report.pop("evidence_sha256")
    report["evidence_sha256"] = sha256_json(report)

    with pytest.raises(ResourcePerformanceEvidenceError):
        summarize_report(
            report,
            release_files=release_files,
            python_runtime_sha256=runtime_sha,
            expected_profile=profile,
            expected_release_id=str(manifest["release_id"]),
            expected_release_manifest_sha256=hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
        )


def test_owned_resource_probe_and_real_automatic_preemption_benchmark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(certification.SEATBELT_CHILD_ENV, "1")
    resource = certification._resource_probe(observation_seconds=0.01)
    preemption = certification._preemption_probe(tmp_path)

    assert resource["swapout_growth_mb"] >= 0
    assert resource["observation_seconds"] < 1800
    assert resource["complete_budget_profiles_measured"] is False
    assert preemption["seatbelt_child"] is True
    assert preemption["automatic_preemption_observed"] is True
    assert preemption["manual_owned_cleanup_performed"] is False
    assert preemption["sample_count"] == 4
    assert {row["incoming_priority_class"] for row in preemption["samples"]} == {
        "P0",
        "P1",
    }
    assert preemption["p0_p1_deadline_misses"] == 0
    assert preemption["orphan_process_groups"] == 0
    assert preemption["duplicate_completions"] == 0
    assert preemption["lost_jobs"] == 0
    assert all(
        row["attempts"] == [[1, "preempted"], [2, "succeeded"]]
        and row["retry_completed_once"] is True
        and row["manual_terminate_invoked"] is False
        for row in preemption["samples"]
    )


def test_owned_worker_group_physical_footprint_returns_within_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(certification.SEATBELT_CHILD_ENV, "1")
    probe = certification._worker_footprint_probe(tmp_path)
    metrics = _worker_metrics(probe)

    assert probe["sample_count"] == 3
    assert probe["return_p95_seconds"] <= 30.0
    assert all(
        row["observed_group_rss_mb"] > 0
        and row["observed_group_physical_footprint_mb"] > 0
        and row["process_group_gone"] is True
        and row["rss_returned_to_zero"] is True
        and row["physical_footprint_returned_to_zero"] is True
        for row in probe["samples"]
    )
    assert metrics["rss_returned_to_baseline"] is True
    assert metrics["physical_footprint_returned_to_baseline"] is True
    assert metrics["metal_measurement_available"] is False
    assert metrics["metal_returned_to_baseline"] is False


def test_hash_bound_v2_stopped_window_upgrades_only_g8_g9_g25_capabilities(
    tmp_path: Path,
) -> None:
    release, _release_sha = campaign_fixtures.create_release(tmp_path)
    report, release_files, runtime_sha, profile = _partial_inputs(release)
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    window = passing_window_report()
    window["release_binding"].update(
        release_id=manifest["release_id"],
        release_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        python_runtime_sha256=runtime_sha,
        resource_policy_sha256=release_files["config/v3_resource_policy.json"],
    )
    window["execution_binding"]["collector_source_sha256"] = release_files[
        "scripts/v3_validation/isolated_resource_window_collector.py"
    ]
    composition_receipt = window["execution_binding"]["production_composition_receipt"]
    composition_receipt["release_id"] = manifest["release_id"]
    composition_receipt["python_runtime_sha256"] = runtime_sha
    composition_receipt.pop("receipt_sha256", None)
    composition_receipt["receipt_sha256"] = sha256_json(composition_receipt)
    for arm in window["model_benchmark"]["arms"]:
        arm["python_runtime_sha256"] = runtime_sha
    window.pop("evidence_sha256")
    window["evidence_sha256"] = sha256_json(window)
    report["isolated_resource_window_report"] = window
    report["capability_gaps"] = []
    report["metrics"] = certification.derive_metrics(report)
    report.pop("evidence_sha256")
    report["evidence_sha256"] = sha256_json(report)

    metrics = summarize_report(
        report,
        release_files=release_files,
        python_runtime_sha256=runtime_sha,
        expected_profile=profile,
        expected_release_id=str(manifest["release_id"]),
        expected_release_manifest_sha256=hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
    )

    assert metrics[GATE_IDS[0]]["model_tokens_per_second_measured"] is True
    assert metrics[GATE_IDS[0]]["minimum_model_tokens_per_second_ratio"] >= 0.95
    assert metrics[GATE_IDS[0]]["matched_disposable_dependencies"] is True
    assert metrics[GATE_IDS[0]]["matched_production_dependencies"] is False
    assert set(MATCHED_PRODUCTION_MISSING_REQUIREMENTS).issubset(
        metrics[GATE_IDS[0]]["missing_requirements"]
    )
    assert metrics[GATE_IDS[0]]["scope_limitations"] == list(
        MATCHED_PRODUCTION_SCOPE_LIMITATIONS
    )
    assert metrics[GATE_IDS[1]]["all_budgets_passed"] is True
    assert metrics[GATE_IDS[3]]["metal_returned_to_baseline"] is True
    assert metrics[GATE_IDS[3]]["per_process_metal_bytes_available"] is False


def test_campaign_entrypoint_reexecs_complete_partial_producer_under_seatbelt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.delenv(certification.SEATBELT_CHILD_ENV, raising=False)
    observed: dict[str, object] = {}

    def capture(argv, *, cwd, env, **_kwargs):
        observed.update(argv=list(argv), cwd=cwd, env=dict(env))
        return subprocess.CompletedProcess(
            list(argv), 0, certification.EVIDENCE_PREFIX + '{"status":"passed"}\n', ""
        )

    monkeypatch.setattr(certification.subprocess, "run", capture)

    assert certification.main(["--campaign-evidence"]) == 0

    argv = observed["argv"]
    env = observed["env"]
    assert isinstance(argv, list) and argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert "(deny network*)" in argv[2]
    assert "(deny file-write*)" in argv[2]
    assert isinstance(env, dict) and env[certification.SEATBELT_CHILD_ENV] == "1"
    assert capsys.readouterr().out.startswith(certification.EVIDENCE_PREFIX)


def test_partial_reports_promote_only_real_preemption_and_tamper_is_invalid(
    tmp_path: Path,
) -> None:
    release, release_sha = campaign_fixtures.create_release(tmp_path, armed=True)
    campaign_context = campaign_fixtures.release_context(release, release_sha)
    runner = campaign_fixtures.make_runner(
        tmp_path,
        campaign_fixtures.Clock(datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)),
        [],
        release=release,
        release_sha=release_sha,
        context=campaign_context,
        certifiable_backend=True,
    )
    report = runner.run_today()
    report_path = runner.state_dir / "campaign-report.json"
    day_path = runner.state_dir / str(report["artifacts"][0]["path"])
    day = json.loads(day_path.read_text())
    day["workloads"] = [
        row
        for row in day["workloads"]
        if row["workload"]
        in {"matched_v2_v3_performance", "hundred_cycle_worker_reap_soak"}
    ]
    day_path.write_bytes(_canonical(day))
    report["artifacts"][0]["sha256"] = hashlib.sha256(day_path.read_bytes()).hexdigest()
    report_path.write_bytes(_canonical(report))
    context = CompileContext(
        campaign_context.campaign_id,
        campaign_context.release_sha,
        campaign_context.hardware_id,
        campaign_context.gate_config_sha256,
    )
    gate_config = json.loads(
        (release / "config/v3_cutover_gates.json").read_text(encoding="utf-8")
    )
    output = tmp_path / "evidence"

    statuses = compile_campaign_evidence(
        report_path=report_path,
        release_root=release,
        output=output,
        context=context,
        config=gate_config,
    )
    decision = evaluate_evidence(
        gate_config,
        output,
        expected_context=context.as_dict(),
        now=datetime.fromisoformat(str(report["generated_at"]))
        + timedelta(minutes=1),
    )

    preemption_gate = GATE_IDS[2]
    assert statuses[preemption_gate] == "passed"
    assert preemption_gate in decision["passed"], decision
    assert all(
        statuses[gate] == "failed" and gate in decision["failed"]
        for gate in (*GATE_IDS[:2], GATE_IDS[3])
    ), decision
    for gate in GATE_IDS:
        producer = json.loads((output / f"reports/{gate}.json").read_text())
        assert producer["status"] == (
            "passed" if gate == preemption_gate else "failed"
        )
        assert sum(
            row["role"] == "upstream_resource_performance_report"
            for row in producer["source_artifacts"]
        ) == 7

    performance_producer = json.loads(
        (output / f"reports/{GATE_IDS[0]}.json").read_text()
    )
    assert performance_producer["metrics"]["matched_disposable_dependencies"] is True
    assert performance_producer["metrics"]["matched_production_dependencies"] is False
    assert set(MATCHED_PRODUCTION_MISSING_REQUIREMENTS).issubset(
        performance_producer["metrics"]["missing_requirements"]
    )

    gate = GATE_IDS[0]
    producer_path = output / f"reports/{gate}.json"
    producer = json.loads(producer_path.read_text())
    producer["status"] = "passed"
    producer["metrics"].update(
        matched_production_dependencies=True,
        minimum_model_tokens_per_second_ratio=1.0,
    )
    producer["metrics_sha256"] = hashlib.sha256(
        _canonical(producer["metrics"])
    ).hexdigest()
    producer_path.write_bytes(_canonical(producer))
    envelope_path = output / f"{gate}.json"
    envelope = json.loads(envelope_path.read_text())
    envelope["status"] = "passed"
    envelope["metrics_sha256"] = producer["metrics_sha256"]
    next(
        row for row in envelope["artifacts"] if row["role"] == "producer_report"
    )["sha256"] = hashlib.sha256(producer_path.read_bytes()).hexdigest()
    envelope_path.write_bytes(_canonical(envelope))

    tampered = evaluate_evidence(
        gate_config,
        output,
        expected_context=context.as_dict(),
        now=datetime.fromisoformat(str(report["generated_at"]))
        + timedelta(minutes=1),
    )
    assert any(
        "authoritative recomputation" in error for error in tampered["invalid"][gate]
    )

    preemption_producer_path = output / f"reports/{preemption_gate}.json"
    preemption_producer = json.loads(preemption_producer_path.read_text())
    preemption_producer["metrics"]["interactive_queue_p95_ms"] = 0.0
    preemption_producer["metrics_sha256"] = hashlib.sha256(
        _canonical(preemption_producer["metrics"])
    ).hexdigest()
    preemption_producer_path.write_bytes(_canonical(preemption_producer))
    preemption_envelope_path = output / f"{preemption_gate}.json"
    preemption_envelope = json.loads(preemption_envelope_path.read_text())
    preemption_envelope["metrics_sha256"] = preemption_producer["metrics_sha256"]
    next(
        row
        for row in preemption_envelope["artifacts"]
        if row["role"] == "producer_report"
    )["sha256"] = hashlib.sha256(preemption_producer_path.read_bytes()).hexdigest()
    preemption_envelope_path.write_bytes(_canonical(preemption_envelope))

    tampered_preemption = evaluate_evidence(
        gate_config,
        output,
        expected_context=context.as_dict(),
        now=datetime.fromisoformat(str(report["generated_at"]))
        + timedelta(minutes=1),
    )
    assert any(
        "authoritative recomputation" in error
        for error in tampered_preemption["invalid"][preemption_gate]
    )
