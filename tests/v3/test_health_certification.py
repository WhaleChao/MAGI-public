from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.v3_validation import health_certification as certification
import scripts.v3_evidence_compiler as compiler
from scripts.v3_campaign.runner import ReleaseBundle
from scripts.v3_evidence_compiler import CompileContext, compile_campaign_evidence
from scripts.v3_release_gate import _validate_health_inner_report, evaluate_evidence


ROOT = Path(__file__).resolve().parents[2]


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value: object) -> str:
    data = _canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _inner_report(
    profile: dict[str, object], script_sha: str, health_sha: str
) -> dict[str, object]:
    report: dict[str, object] = {
        "schema": certification.SCHEMA,
        "generated_at": "2026-07-16T00:00:00+00:00",
        "status": "certified",
        "probe": "production_health_service_liveness",
        "validation_profile": profile,
        "measurements": {
            "probe_count": 1_000,
            "successful_probes": 1_000,
            "failed_probes": 0,
            "model_imports": 0,
            "models_loaded": 0,
            "model_probe_flags": 0,
            "newly_loaded_heavy_modules": [],
            "state_mutations": [],
            "total_duration_us": float(profile["fault_seed"]),
            "maximum_probe_us": 1.0,
        },
        "release_binding": {
            "certifier_script_sha256": script_sha,
            "health_module_sha256": health_sha,
        },
        "safety": {
            "network_access_performed": False,
            "service_start_performed": False,
            "production_port_access_performed": False,
            "launchctl_performed": False,
            "runtime_initialized": False,
        },
    }
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return report


def test_thousand_production_health_probes_are_model_free_and_read_only(tmp_path: Path) -> None:
    evidence = certification.run_health_certification(tmp_path / "sandbox")
    certification.verify_health_evidence(evidence)

    assert evidence["status"] == "certified"
    assert evidence["measurements"]["probe_count"] == 1_000
    assert evidence["measurements"]["successful_probes"] == 1_000
    assert evidence["measurements"]["failed_probes"] == 0
    assert evidence["measurements"]["model_imports"] == 0
    assert evidence["measurements"]["models_loaded"] == 0
    assert evidence["measurements"]["state_mutations"] == []
    assert evidence["safety"]["runtime_initialized"] is False


def test_health_certification_rejects_live_source_and_nonempty_roots(tmp_path: Path) -> None:
    with pytest.raises(certification.HealthCertificationError, match="overlaps"):
        certification.run_health_certification(certification.REPO_ROOT)

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "state").write_text("do not touch", encoding="utf-8")
    with pytest.raises(certification.HealthCertificationError, match="empty"):
        certification.run_health_certification(nonempty)


def test_health_evidence_hash_rejects_tampering(tmp_path: Path) -> None:
    evidence = certification.run_health_certification(tmp_path / "sandbox")
    evidence["measurements"]["failed_probes"] = 1
    with pytest.raises(certification.HealthCertificationError, match="identity/hash"):
        certification.verify_health_evidence(evidence)


def test_health_certification_emits_structured_campaign_evidence(
    tmp_path: Path,
) -> None:
    profile = {
        "profile_id": os.environ.get(
            "MAGI_V3_VALIDATION_PROFILE_ID", "health_unit_profile"
        ),
        "replay_start_local": os.environ.get(
            "MAGI_V3_REPLAY_START_LOCAL", "2026-07-13T00:00:00+08:00"
        ),
        "fault_seed": int(os.environ.get("MAGI_V3_FAULT_SEED", "1101")),
    }
    report = certification.run_health_certification(
        tmp_path / "campaign-sandbox", validation_profile=profile
    )
    certification.verify_health_evidence(report)
    evidence = certification.campaign_evidence(report)
    print(
        "MAGI_V3_OFFLINE_EVIDENCE="
        + json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    assert evidence["status"] == "passed"
    assert evidence["report"]["validation_profile"] == profile


def test_campaign_cli_runs_direct_certifier_with_profile_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = {
        "profile_id": "ordinary_week",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    monkeypatch.setenv("MAGI_V3_OFFLINE_CERTIFICATION", "1")
    monkeypatch.setenv("MAGI_V3_VALIDATION_PROFILE_ID", profile["profile_id"])
    monkeypatch.setenv("MAGI_V3_REPLAY_START_LOCAL", profile["replay_start_local"])
    monkeypatch.setenv("MAGI_V3_FAULT_SEED", str(profile["fault_seed"]))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "campaign-tmp"))

    assert certification.main(["--campaign-evidence"]) == 0

    output = capsys.readouterr().out.strip()
    assert output.startswith(certification.EVIDENCE_PREFIX)
    outer = json.loads(output.removeprefix(certification.EVIDENCE_PREFIX))
    assert outer["report"]["validation_profile"] == profile
    assert outer["report"]["measurements"]["successful_probes"] == 1_000


def test_health_reports_compile_and_gate_from_seven_release_bound_inner_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    script = release / "scripts/v3_validation/health_certification.py"
    health = release / "magi_v3/health.py"
    campaign_config = release / "config/v3_validation_campaign.json"
    script.parent.mkdir(parents=True)
    health.parent.mkdir(parents=True)
    campaign_config.parent.mkdir(parents=True)
    script.write_bytes((ROOT / "scripts/v3_validation/health_certification.py").read_bytes())
    health.write_bytes((ROOT / "magi_v3/health.py").read_bytes())
    campaign_config.write_bytes(
        (ROOT / "config/v3_validation_campaign.json").read_bytes()
    )
    source_rows = [
        {
            "path": path.relative_to(release).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
            "mode": "0444",
        }
        for path in sorted((campaign_config, health, script))
    ]
    release_sha = hashlib.sha256(
        json.dumps(source_rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "immutable": True,
        "release_id": "health-release",
        "commit": "b" * 40,
        "release_sha256": release_sha,
        "source_snapshot_sha256": release_sha,
        "source_file_count": len(source_rows),
        "files": source_rows,
    }
    manifest_path = release / "release-manifest.json"
    manifest_sha = _write_json(manifest_path, manifest)
    marker = {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "commit": manifest["commit"],
        "manifest": "release-manifest.json",
        "manifest_sha256": manifest_sha,
        "release_sha256": release_sha,
        "source_snapshot_sha256": release_sha,
        "source_file_count": len(source_rows),
    }
    _write_json(release / "RELEASE_COMPLETE.json", marker)
    files = tuple(
        (
            str(row["path"]),
            str(row["sha256"]),
            int(row["size"]),
            0o444,
        )
        for row in source_rows
    )
    bundle = ReleaseBundle(
        release,
        str(manifest["release_id"]),
        str(manifest["commit"]),
        release_sha,
        manifest_sha,
        files,
    )
    context = CompileContext("health-campaign", release_sha, "test-mac", "a" * 64)
    state = tmp_path / "campaign"
    cron = state / "cron-jobs.json"
    cron_sha = _write_json(cron, [])
    profiles = json.loads(
        (ROOT / "config/v3_validation_campaign.json").read_text(encoding="utf-8")
    )["offline_campaign"]["validation_pass_profiles"]
    script_sha = next(
        row["sha256"]
        for row in source_rows
        if row["path"] == "scripts/v3_validation/health_certification.py"
    )
    health_sha = next(
        row["sha256"] for row in source_rows if row["path"] == "magi_v3/health.py"
    )
    outcomes: list[dict[str, object]] = []
    for validation_pass, profile in enumerate(profiles, 1):
        inner = _inner_report(profile, str(script_sha), str(health_sha))
        inner_path = state / "artifacts" / f"health-{validation_pass}.json"
        inner_sha = _write_json(inner_path, inner)
        outer = {
            "schema_version": 1,
            "workload": "health_1000_model_free",
            "status": "passed",
            "probe": inner["probe"],
            "measurements": inner["measurements"],
            "report": inner,
            "network_access_performed": False,
            "service_start_performed": False,
            "production_port_access_performed": False,
            "launchctl_performed": False,
        }
        outcomes.append(
            {
                "validation_pass": validation_pass,
                "validation_profile": profile,
                "workload": "health_1000_model_free",
                "returncode": 0,
                "status": "offline_passed",
                "structured_evidence": outer,
                "inner_report_artifact": {
                    "path": inner_path.relative_to(state).as_posix(),
                    "sha256": inner_sha,
                },
            }
        )
    now = "2026-07-16T00:00:00+00:00"
    shared = {
        **context.as_dict(),
        "release_id": manifest["release_id"],
        "release_commit": manifest["commit"],
        "release_manifest_sha256": manifest_sha,
        "campaign_config_sha256": hashlib.sha256(
            campaign_config.read_bytes()
        ).hexdigest(),
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
        "cron_jobs_file": str(cron),
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
    gate_config = json.loads(
        (ROOT / "config/v3_cutover_gates.json").read_text(encoding="utf-8")
    )

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
        now=datetime(2026, 7, 16, 0, 1, tzinfo=timezone.utc),
    )

    assert statuses["health_1000_probes_loaded_zero_models"] == "passed"
    assert "health_1000_probes_loaded_zero_models" in decision["passed"]
    assert "health_1000_probes_loaded_zero_models" not in decision["invalid"]
    producer = json.loads(
        (output / "reports/health_1000_probes_loaded_zero_models.json").read_text()
    )
    assert producer["metrics"] == {
        "profile_count": 7,
        "probe_count": 1_000,
        "successful_probes": 1_000,
        "total_probe_count": 7_000,
        "failed_probes": 0,
        "model_imports": 0,
        "models_loaded": 0,
        "state_mutations": 0,
    }
    assert sum(
        row["role"] == "upstream_health_certification_report"
        for row in producer["source_artifacts"]
    ) == 7

    # A trusted-looking normalized producer assertion is insufficient: the
    # gate recomputes from the seven frozen inner reports and catches drift.
    producer["metrics"]["model_imports"] = 1
    producer["metrics_sha256"] = hashlib.sha256(
        _canonical(producer["metrics"])
    ).hexdigest()
    producer_path = output / "reports/health_1000_probes_loaded_zero_models.json"
    producer_path.write_bytes(_canonical(producer))
    envelope_path = output / "health_1000_probes_loaded_zero_models.json"
    envelope = json.loads(envelope_path.read_text())
    envelope["metrics_sha256"] = producer["metrics_sha256"]
    next(
        row for row in envelope["artifacts"] if row["role"] == "producer_report"
    )["sha256"] = hashlib.sha256(producer_path.read_bytes()).hexdigest()
    envelope_path.write_bytes(_canonical(envelope))
    tampered = evaluate_evidence(
        gate_config,
        output,
        expected_context=context.as_dict(),
        now=datetime(2026, 7, 16, 0, 1, tzinfo=timezone.utc),
    )
    assert any(
        "metrics do not match code-owned authoritative recomputation" in error
        for error in tampered["invalid"]["health_1000_probes_loaded_zero_models"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("failed_probes", 1),
        ("successful_probes", 999),
        ("model_imports", 1),
        ("models_loaded", 1),
        ("state_mutations", ["state.db"]),
    ],
)
def test_gate_rejects_non_strict_health_inner_report(
    field: str, value: object
) -> None:
    profile = {
        "profile_id": "ordinary_week",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    report = _inner_report(profile, "a" * 64, "b" * 64)
    report["measurements"][field] = value  # type: ignore[index]
    report.pop("evidence_sha256")
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    release_files = {
        "scripts/v3_validation/health_certification.py": {"sha256": "a" * 64},
        "magi_v3/health.py": {"sha256": "b" * 64},
    }

    with pytest.raises(ValueError, match="strict 1000/1000"):
        _validate_health_inner_report(
            report,
            expected_profile=profile,
            release_files=release_files,
        )
