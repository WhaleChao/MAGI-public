from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.v3_campaign import runner as runner_module
from scripts.v3_campaign.runner import CampaignRunner, CampaignSafetyError, OFFLINE_COMMANDS
from scripts.v3_validation import g8_isolated_smb as g8
from scripts.v3_validation.resource_performance_evidence import (
    GATE_IDS,
    ResourcePerformanceEvidenceError,
    derive_metrics,
    sha256_json,
    summarize_report,
)
from tests.v3 import test_campaign_runner as campaign_fixtures
from tests.v3.test_g8_isolated_smb import FakeSMB
from tests.v3.test_resource_performance_certification import _partial_inputs


def _g8_report(tmp_path: Path, release: Path) -> tuple[Path, str]:
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    service = release / "config/v3_service_manifest.json"
    service_sha = hashlib.sha256(service.read_bytes()).hexdigest()
    ownership = tmp_path / "g8-inputs/ownership.json"
    ownership.parent.mkdir(parents=True)
    website_root = tmp_path / "execution-inputs" / "website"
    website_admin = website_root / "admin" / "admin_server.py"
    website_admin.parent.mkdir(parents=True)
    website_admin.write_bytes(b"class AdminHandler: pass\n")
    website_admin_sha = hashlib.sha256(website_admin.read_bytes()).hexdigest()
    ownership.write_text(
        json.dumps(
            {
                "release_id": manifest["release_id"],
                "service_manifest": str(service),
                "service_manifest_sha256": service_sha,
                "roles": ["control", "gateway", "supervisor"],
                "external_inputs": {
                    "website_root": str(website_root),
                    "website_admin_sha256": website_admin_sha,
                },
            },
            sort_keys=True,
        )
        + "\n"
    )
    matched = tmp_path / "g8-inputs/matched-performance.json"
    matched.write_text(
        json.dumps(
            campaign_fixtures._passing_matched_performance_report(
                certifier_sha256=hashlib.sha256(
                    (release / "scripts/v3_validation/perf_certification.py").read_bytes()
                ).hexdigest(),
                runtime_sha256=hashlib.sha256(
                    b"#!/bin/sh\nprintf 'MAGI_V3_PYTHON_OK:3\\n'\n"
                ).hexdigest(),
            ),
            sort_keys=True,
        )
        + "\n"
    )
    mount = tmp_path / "fake-remote-smb"
    target = mount / "operator-created-empty-validation"
    target.mkdir(parents=True)
    adapter = FakeSMB(mount)
    plan = tmp_path / "g8-inputs/plan.json"
    token = tmp_path / "g8-inputs/token.txt"
    prepared = g8.prepare_plan(
        target=target,
        release_manifest=manifest_path,
        service_manifest=service,
        ownership_manifest=ownership,
        matched_performance_report=matched,
        output=plan,
        token_output=token,
        adapter=adapter,
        samples_per_arm=10,
    )
    plan_value = json.loads(plan.read_text())
    phrase = (
        f"AUTHORIZE MAGI G8 SMB {plan_value['plan_id']} "
        f"{plan_value['target_sha256']}"
    )
    authorization = tmp_path / "g8-inputs/authorization.json"
    authorized = g8.authorize_plan(
        plan_path=plan,
        plan_file_sha256=prepared["plan_file_sha256"],
        output=authorization,
        input_reader=lambda _prompt: phrase,
        local_uid=501,
        local_user="ai",
        tty_name="/dev/ttys-test",
    )
    report = tmp_path / "g8-inputs/report.json"
    g8.execute_plan(
        plan_path=plan,
        plan_file_sha256=prepared["plan_file_sha256"],
        authorization_path=authorization,
        authorization_sha256=authorized["authorization_sha256"],
        token_file=token,
        output=report,
        adapter=adapter,
    )
    return report, hashlib.sha256(report.read_bytes()).hexdigest()


def _runner(
    tmp_path: Path,
    release: Path,
    release_sha: str,
    report: Path | None,
    digest: str | None,
    *,
    state_name: str = "state",
    real_backend: bool = False,
) -> CampaignRunner:
    return CampaignRunner(
        release_root=release,
        state_dir=tmp_path / state_name,
        context=campaign_fixtures.release_context(release, release_sha),
        command_runner=None if real_backend else campaign_fixtures.successful_runner([]),
        clock=campaign_fixtures.Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc)),
        g8_smb_report=report,
        g8_smb_report_sha256=digest,
        **(campaign_fixtures.execution_inputs(tmp_path) if real_backend else {}),
    )


def test_campaign_requires_path_hash_pair_and_rejects_tamper(tmp_path: Path) -> None:
    release, release_sha = campaign_fixtures.create_release(tmp_path)
    report, digest = _g8_report(tmp_path, release)
    with pytest.raises(CampaignSafetyError, match="requires"):
        _runner(tmp_path, release, release_sha, report, None)
    report.chmod(0o600)
    report.write_text(report.read_text().replace('"status":"passed"', '"status":"failed"'))
    with pytest.raises(CampaignSafetyError, match="hash-bound"):
        _runner(tmp_path, release, release_sha, report, digest)


def test_campaign_postcheck_detects_g8_report_drift(tmp_path: Path) -> None:
    release, release_sha = campaign_fixtures.create_release(tmp_path)
    report, digest = _g8_report(tmp_path, release)
    runner = _runner(tmp_path, release, release_sha, report, digest)
    report.chmod(0o600)
    report.write_bytes(report.read_bytes() + b"\n")
    with pytest.raises(CampaignSafetyError, match="hash-bound"):
        runner._verify_bundle_stable()


def test_ledger_refuses_old_context_without_g8_binding(tmp_path: Path) -> None:
    release, release_sha = campaign_fixtures.create_release(tmp_path)
    report, digest = _g8_report(tmp_path, release)
    _runner(tmp_path, release, release_sha, report, digest)
    with pytest.raises(CampaignSafetyError, match="ledger context"):
        _runner(tmp_path, release, release_sha, None, None)


def test_real_runner_passes_g8_env_only_to_matched_performance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, release_sha = campaign_fixtures.create_release(tmp_path)
    report, digest = _g8_report(tmp_path, release)
    runner = _runner(
        tmp_path, release, release_sha, report, digest,
        state_name="real-state", real_backend=True,
    )
    observed: list[dict[str, str]] = []

    def capture(argv, *, env, **_kwargs):
        observed.append(dict(env))
        return subprocess.CompletedProcess(list(argv), 0, "ok\n", "")

    monkeypatch.setattr(runner, "_verify_python_runtime", lambda: None)
    monkeypatch.setattr(runner_module.subprocess, "run", capture)
    matched = runner._command_for("matched_v2_v3_performance")
    health = runner._command_for("health_1000_model_free")
    runner._run_command(matched, release)
    runner._run_command(health, release)
    assert observed[0]["MAGI_V3_G8_SMB_REPORT"] == str(report)
    assert observed[0]["MAGI_V3_G8_SMB_REPORT_SHA256"] == digest
    assert "MAGI_V3_G8_SMB_REPORT" not in observed[1]
    assert "MAGI_V3_G8_SMB_REPORT_SHA256" not in observed[1]


def test_all_seven_matched_profiles_and_artifact_context_bind_same_g8_report(
    tmp_path: Path,
) -> None:
    release, release_sha = campaign_fixtures.create_release(tmp_path)
    report, digest = _g8_report(tmp_path, release)
    runner = _runner(tmp_path, release, release_sha, report, digest)
    result = runner.run_today()
    artifact = tmp_path / "state" / result["artifacts"][0]["path"]
    evidence = json.loads(artifact.read_text())
    matched = [
        row for row in evidence["workloads"]
        if row.get("workload") == "matched_v2_v3_performance"
    ]
    assert len(matched) == 7
    assert {row["validation_profile"]["profile_id"] for row in matched} == {
        profile["profile_id"]
        for profile in runner.config["offline_campaign"]["validation_pass_profiles"]
    }
    assert all(row["g8_smb_binding"]["g8_smb_report_sha256"] == digest for row in matched)
    assert evidence["g8_smb_report_sha256"] == digest
    assert evidence["g8_smb_evidence_sha256"]


def test_cli_declares_explicit_g8_binding_arguments() -> None:
    source = Path(runner_module.__file__).read_text()
    assert 'parser.add_argument("--g8-smb-report", type=Path)' in source
    assert 'parser.add_argument("--g8-smb-report-sha256")' in source
    assert OFFLINE_COMMANDS["matched_v2_v3_performance"][-1] == "--campaign-evidence"


def test_resource_gate_promotes_only_recomputed_raw_g8_evidence(tmp_path: Path) -> None:
    release, _release_sha = campaign_fixtures.create_release(tmp_path)
    g8_path, _g8_sha = _g8_report(tmp_path, release)
    partial, release_files, runtime_sha, profile = _partial_inputs(release)
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    g8_report = json.loads(g8_path.read_text())
    partial["g8_transport_composition_receipt"] = g8_report
    partial["matched_production_report"] = g8.extract_bound_matched_performance_report(
        g8_report,
        expected_release_id=manifest["release_id"],
        expected_release_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    partial["metrics"] = derive_metrics(partial, release_files=release_files)
    partial.pop("evidence_sha256")
    partial["evidence_sha256"] = sha256_json(partial)
    metrics = summarize_report(
        partial,
        release_files=release_files,
        python_runtime_sha256=runtime_sha,
        expected_profile=profile,
        expected_release_id=manifest["release_id"],
        expected_release_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    assert metrics[GATE_IDS[0]]["matched_production_dependencies"] is True

    raw = partial["g8_transport_composition_receipt"]
    raw["raw_arms"]["v3"]["samples"][0]["transcript"][1]["path"] = "../escape"
    raw.pop("evidence_sha256")
    raw["evidence_sha256"] = g8.sha256_json(raw)
    with pytest.raises(ResourcePerformanceEvidenceError, match="raw SMB evidence"):
        derive_metrics(partial, release_files=release_files)
