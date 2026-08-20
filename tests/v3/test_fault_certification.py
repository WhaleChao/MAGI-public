from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.v3_evidence_compiler as compiler
from scripts.v3_campaign.runner import ReleaseBundle
from scripts.v3_evidence_compiler import CompileContext, compile_campaign_evidence
from scripts.v3_release_gate import _validate_fault_inner_report, evaluate_evidence
from scripts.v3_validation.fault_certification import (
    EVIDENCE_PREFIX,
    FaultCertificationError,
    LIVE_ROOT,
    MACH_KILL_DELAYS_US,
    SCHEMA,
    _prepare_sandbox,
    campaign_evidence,
    build_fault_stimulus_plan,
    main,
    run_fault_certification,
    verify_fault_certification,
)
from scripts.v3_validation import physical_fault_drill
from tests.v3.test_physical_fault_drill import _device, _passing_report, _rehash


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS fault certification")
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


def test_controlled_restart_fault_layer_uses_apfs_wal_and_transaction_sigkill(
    tmp_path: Path,
) -> None:
    evidence = run_fault_certification(tmp_path / "fault-certification")

    assert evidence["status"] == "certified_controlled_restart_fault_layer"
    decision = evidence["decision"]
    assert decision["eligible_to_clear_fault_campaign_realism_blocker"] is True
    assert decision["software_equivalent_layer_certified"] is True
    assert decision["transaction_stage_sigkill_certified"] is True
    assert decision["external_device_disconnect_required"] is False
    assert decision["physical_power_cut_required"] is False
    assert decision["controlled_cold_restart_required_at_cutover"] is True
    assert decision["hard_gate_blocked"] is False
    measurements = evidence["measurements"]
    assert measurements["apfs_enospc"]["filesystem"] == "apfs"
    assert measurements["apfs_enospc"]["filesystem_enospc_observed"] is True
    assert measurements["apfs_enospc"]["sqlite_full_observed"] is True
    assert measurements["apfs_enospc"]["sqlite_full_attempt_isolated_to_owned_child"] is True
    assert measurements["apfs_enospc"]["sqlite_recovery_isolated_to_owned_child"] is True
    assert measurements["sqlite_wal_fsync_io_error"]["injected_error"] == "SQLITE_IOERR_FSYNC"
    mach = measurements["mach_clock_sigkill"]
    stimulus_plan = build_fault_stimulus_plan(None)
    assert mach["clock"] == "mach_absolute_time"
    assert mach["wait"] == "mach_wait_until"
    assert mach["offsets_us"] == stimulus_plan["mach_kill_offsets_us"]
    assert mach["cycles_completed"] == len(MACH_KILL_DELAYS_US)
    assert mach["acknowledged_commits_lost"] == 0
    assert mach["partially_visible_transactions"] == 0
    assert mach["duplicate_jobs"] == 0
    assert mach["lost_jobs_after_recovery"] == 0
    assert all(row["timing"]["target_pid"] > 1 for row in mach["cycles"])
    transaction = measurements["logical_transaction_boundary_sweep"]
    assert transaction["stages_completed"] == 37
    assert transaction["acknowledged_commits_lost"] == 0
    assert transaction["partially_visible_transactions"] == 0
    assert evidence["residual_risk"]["accepted_by_equivalent_layer"] is True
    assert evidence["residual_risk"]["hard_gate_blocking"] is False
    assert (
        evidence["residual_risk"]["deferred_gate"]
        == "atomic_release_switch_and_cold_rollback_drill_passed"
    )
    assert evidence["safety"]["live_magi_state_accessed"] is False
    assert evidence["safety"]["signals_sent_only_to_owned_children"] is True
    verify_fault_certification(evidence)


def test_fault_stimulus_plan_is_replayable_and_profile_unique() -> None:
    profiles = [
        {
            "profile_id": f"profile_{index}",
            "replay_start_local": f"2026-07-{13 + index:02d}T00:00:00+08:00",
            "fault_seed": 1101 + index,
        }
        for index in range(7)
    ]
    plans = [build_fault_stimulus_plan(profile) for profile in profiles]

    assert plans == [build_fault_stimulus_plan(profile) for profile in profiles]
    assert len({plan["stimulus_plan_sha256"] for plan in plans}) == 7
    assert all(len(set(plan["mach_kill_offsets_us"])) == 6 for plan in plans)


def test_fault_certification_hash_fails_closed_after_tamper(tmp_path: Path) -> None:
    evidence = {
        "schema": SCHEMA,
        "status": "certified_controlled_restart_fault_layer",
        "decision": {},
    }
    evidence["evidence_sha256"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    evidence["decision"]["physical_power_cut_required"] = True

    with pytest.raises(FaultCertificationError, match="does not match"):
        verify_fault_certification(evidence)


def test_fault_certification_rejects_live_source_and_nonempty_sandboxes(tmp_path: Path) -> None:
    with pytest.raises(FaultCertificationError, match="live MAGI"):
        _prepare_sandbox(LIVE_ROOT)
    source = Path(__file__).resolve().parents[2]
    with pytest.raises(FaultCertificationError, match="source tree"):
        _prepare_sandbox(source / "forbidden-certification")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "preserve.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(FaultCertificationError, match="empty"):
        _prepare_sandbox(occupied)
    assert (occupied / "preserve.txt").read_text(encoding="utf-8") == "preserve"


def test_campaign_cli_emits_profile_bound_inner_report_and_cleans_owned_sandbox(
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

    assert main(["--campaign-evidence"]) == 0

    output = capsys.readouterr().out.strip()
    assert output.startswith(EVIDENCE_PREFIX)
    outer = json.loads(output.removeprefix(EVIDENCE_PREFIX))
    assert outer == campaign_evidence(outer["report"])
    assert outer["report"]["validation_profile"] == profile
    assert outer["report"]["decision"]["hard_gate_blocked"] is False
    assert not (
        tmp_path / "campaign-tmp" / "magi-v3-fault-certification-ordinary_week"
    ).exists()


def test_fault_inner_reports_compile_as_controlled_restart_offline_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    source_paths = [
        "config/v3_validation_campaign.json",
        "scripts/v3_validation/fault_certification.py",
        "scripts/v3_validation/fault_realism.py",
        "scripts/v3_validation/physical_fault_drill.py",
    ]
    for relative in source_paths:
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    source_rows = [
        {
            "path": relative,
            "sha256": hashlib.sha256((release / relative).read_bytes()).hexdigest(),
            "size": (release / relative).stat().st_size,
            "mode": "0444",
        }
        for relative in sorted(source_paths)
    ]
    release_sha = hashlib.sha256(
        json.dumps(source_rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "immutable": True,
        "release_id": "fault-release",
        "commit": "c" * 40,
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
    bundle = ReleaseBundle(
        release,
        str(manifest["release_id"]),
        str(manifest["commit"]),
        release_sha,
        manifest_sha,
        tuple(
            (
                str(row["path"]),
                str(row["sha256"]),
                int(row["size"]),
                0o444,
            )
            for row in source_rows
        ),
    )
    context = CompileContext("fault-campaign", release_sha, "test-mac", "a" * 64)
    state = tmp_path / "campaign"
    cron = state / "cron-jobs.json"
    cron_sha = _write_json(cron, [])
    profiles = json.loads(
        (ROOT / "config/v3_validation_campaign.json").read_text(encoding="utf-8")
    )["offline_campaign"]["validation_pass_profiles"]
    base = run_fault_certification(
        tmp_path / "real-fault-report",
        validation_profile=profiles[0],
    )
    python_sha = base["release_binding"]["python_executable_sha256"]
    runtime_manifest = state / "python-runtime-manifest.json"
    runtime_tree_sha = "d" * 64
    runtime_manifest_sha = _write_json(
        runtime_manifest,
        {
            "schema_version": 1,
            "python_runtime_sha256": python_sha,
            "tree_sha256": runtime_tree_sha,
        },
    )
    outcomes: list[dict[str, object]] = []
    for validation_pass, profile in enumerate(profiles, 1):
        inner = copy.deepcopy(base)
        inner["validation_profile"] = profile
        inner["stimulus_plan"] = build_fault_stimulus_plan(profile)
        profile_offsets = inner["stimulus_plan"]["mach_kill_offsets_us"]
        inner["measurements"]["mach_clock_sigkill"]["offsets_us"] = profile_offsets
        for index, cycle in enumerate(
            inner["measurements"]["mach_clock_sigkill"]["cycles"]
        ):
            cycle["timing"]["scheduled_delay_ns"] = profile_offsets[index] * 1_000
        inner.pop("evidence_sha256")
        inner["evidence_sha256"] = hashlib.sha256(
            json.dumps(
                inner,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        inner_path = state / "artifacts" / f"fault-{validation_pass}.json"
        inner_sha = _write_json(inner_path, inner)
        outer = campaign_evidence(inner)
        outcomes.append(
            {
                "validation_pass": validation_pass,
                "validation_profile": profile,
                "workload": "fault_recovery_certification",
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
    campaign_config = release / "config/v3_validation_campaign.json"
    shared = {
        **context.as_dict(),
        "release_id": manifest["release_id"],
        "release_commit": manifest["commit"],
        "release_manifest_sha256": manifest_sha,
        "campaign_config_sha256": hashlib.sha256(
            campaign_config.read_bytes()
        ).hexdigest(),
        "python_runtime_sha256": python_sha,
        "python_runtime_manifest": str(runtime_manifest),
        "python_runtime_manifest_sha256": runtime_manifest_sha,
        "python_runtime_tree_sha256": runtime_tree_sha,
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

    evidence_id = "sqlite_wal_disk_full_fsync_faults_passed"
    assert statuses[evidence_id] == "passed"
    assert evidence_id in decision["passed"]
    assert evidence_id not in decision["invalid"]
    producer_path = output / f"reports/{evidence_id}.json"
    producer = json.loads(producer_path.read_text())
    assert producer["metrics"]["software_equivalent_layer_passed"] is True
    assert producer["metrics"]["transaction_stage_sigkill_passed"] is True
    assert producer["metrics"]["controlled_cold_restart_deferred_to_cutover"] is True
    assert producer["metrics"]["external_device_disconnect_required"] is False
    assert producer["metrics"]["physical_power_cut_required"] is False
    assert producer["metrics"]["residual_hard_gate_blocked"] is False
    assert producer["metrics"]["acknowledged_commits_lost"] == 0
    assert producer["metrics"]["partially_visible_transactions"] == 0
    assert producer["metrics"]["duplicate_jobs"] == 0
    assert producer["metrics"]["lost_jobs_after_recovery"] == 0

    producer["metrics"]["physical_power_cut_required"] = True
    producer["metrics_sha256"] = hashlib.sha256(
        _canonical(producer["metrics"])
    ).hexdigest()
    producer_path.write_bytes(_canonical(producer))
    envelope_path = output / f"{evidence_id}.json"
    envelope = json.loads(envelope_path.read_text())
    envelope["metrics_sha256"] = producer["metrics_sha256"]
    next(
        row for row in envelope["artifacts"] if row["role"] == "producer_report"
    )["sha256"] = hashlib.sha256(producer_path.read_bytes()).hexdigest()
    envelope_path.write_bytes(_canonical(envelope))
    forged = evaluate_evidence(
        gate_config,
        output,
        expected_context=context.as_dict(),
        now=datetime(2026, 7, 16, 0, 1, tzinfo=timezone.utc),
    )
    assert any(
        "metrics do not match code-owned authoritative recomputation" in error
        for error in forged["invalid"][evidence_id]
    )

    physical_root = tmp_path / "physical"
    physical_root.mkdir()
    physical_now = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    physical_device = _device()
    physical_plan = {
        "schema": physical_fault_drill.PLAN_SCHEMA,
        "schema_version": 2,
        "plan_id": "physical-integration-plan",
        **context.as_dict(),
        "device": physical_device,
        "owned_workdir": "/Volumes/MAGI_PHYSICAL_TEST/.magi-v3-physical-fault-test",
        "token_sha256": "1" * 64,
        "authorized_actions": physical_fault_drill.AUTHORIZED_ACTIONS,
        "minimum_sigkill_cycles": 64,
        "prepared_at": physical_now.isoformat(),
        "expires_at": "2026-07-16T02:00:00+00:00",
        "mutation_performed": False,
    }
    physical_plan["plan_sha256"] = physical_fault_drill._semantic(physical_plan)
    physical_plan_path = physical_root / "plan.json"
    physical_plan_sha = _write_json(physical_plan_path, physical_plan)
    physical_authorization = {
        "schema": physical_fault_drill.AUTH_SCHEMA,
        "schema_version": 2,
        "status": "authorized",
        **context.as_dict(),
        "plan_id": physical_plan["plan_id"],
        "plan_file_sha256": physical_plan_sha,
        "device": physical_device,
        "authorized_actions": physical_fault_drill.AUTHORIZED_ACTIONS,
        "approver_uid": 501,
        "approver_user": "ai",
        "auth_method": "allowlisted_local_owner_interactive_tty",
        "tty_session_sha256": "2" * 64,
        "authorized_at": "2026-07-16T00:01:00+00:00",
        "expires_at": "2026-07-16T00:31:00+00:00",
        "human_interaction_performed": True,
    }
    physical_authorization_path = physical_root / "authorization.json"
    physical_authorization_sha = _write_json(
        physical_authorization_path, physical_authorization
    )
    physical_report = _passing_report()
    physical_report.update(context.as_dict())
    physical_report["started_at"] = "2026-07-16T00:02:00+00:00"
    physical_report["generated_at"] = "2026-07-16T00:03:00+00:00"
    physical_report["plan_file_sha256"] = physical_plan_sha
    physical_report["authorization_sha256"] = physical_authorization_sha
    _rehash(physical_report)
    physical_report_path = physical_root / "report.json"
    _write_json(physical_report_path, physical_report)

    physical_output = tmp_path / "physical-evidence"
    with pytest.raises(
        compiler.EvidenceCompileError,
        match="outside the controlled-restart model",
    ):
        compile_campaign_evidence(
            report_path=campaign_path,
            release_root=release,
            output=physical_output,
            context=context,
            config=gate_config,
            physical_fault_report=physical_report_path,
            physical_fault_plan=physical_plan_path,
            physical_fault_authorization=physical_authorization_path,
        )


def test_gate_rejects_loss_duplicate_partial_and_fault_model_drift(
    tmp_path: Path,
) -> None:
    profile = {
        "profile_id": "ordinary_week",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    report = run_fault_certification(
        tmp_path / "gate-fault-report",
        validation_profile=profile,
    )
    binding = report["release_binding"]
    release_files = {
        "scripts/v3_validation/fault_certification.py": {
            "sha256": binding["certifier_script_sha256"]
        },
        "scripts/v3_validation/fault_realism.py": {
            "sha256": binding["fault_probe_script_sha256"]
        },
    }
    python_sha = binding["python_executable_sha256"]
    assert _validate_fault_inner_report(
        report,
        expected_profile=profile,
        release_files=release_files,
        python_runtime_sha256=python_sha,
    ) == report["measurements"]

    mutations = [
        ("logical_transaction_boundary_sweep", "acknowledged_commits_lost", 1),
        ("mach_clock_sigkill", "partially_visible_transactions", 1),
        ("logical_transaction_boundary_sweep", "duplicate_jobs", 1),
        ("mach_clock_sigkill", "lost_jobs_after_recovery", 1),
    ]
    for section, field, value in mutations:
        tampered = copy.deepcopy(report)
        tampered["measurements"][section][field] = value
        tampered.pop("evidence_sha256")
        tampered["evidence_sha256"] = hashlib.sha256(
            json.dumps(
                tampered,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with pytest.raises(ValueError, match="fault"):
            _validate_fault_inner_report(
                tampered,
                expected_profile=profile,
                release_files=release_files,
                python_runtime_sha256=python_sha,
            )

    model_drift = copy.deepcopy(report)
    model_drift["decision"]["physical_power_cut_required"] = True
    model_drift.pop("evidence_sha256")
    model_drift["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            model_drift,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ValueError, match="controlled-restart deferral"):
        _validate_fault_inner_report(
            model_drift,
            expected_profile=profile,
            release_files=release_files,
            python_runtime_sha256=python_sha,
        )

    reused_or_tampered_plan = copy.deepcopy(report)
    reused_or_tampered_plan["stimulus_plan"]["mach_kill_offsets_us"][0] += 1
    reused_or_tampered_plan.pop("evidence_sha256")
    reused_or_tampered_plan["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            reused_or_tampered_plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ValueError, match="stimulus plan"):
        _validate_fault_inner_report(
            reused_or_tampered_plan,
            expected_profile=profile,
            release_files=release_files,
            python_runtime_sha256=python_sha,
        )
