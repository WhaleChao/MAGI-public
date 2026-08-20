from __future__ import annotations

import hashlib
import json
import os
import plistlib
from pathlib import Path

import pytest

from magi_v3.mutable_state_handoff import MutableStateHandoffError
from scripts.v3_cutover.core import CutoverError
from scripts.v3_cutover.mutation import (
    BoundFile,
    MutableStateHandoffPlan,
    PreparedCutoverExecutor,
    PreparedCutoverPlan,
    REQUIRED_V2_APPLICATION_LABELS,
    load_prepared_plan,
)
from scripts.v3_cutover.planning import create_prepared_plan
from scripts.v3_pdf_namer_handoff import precopy


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _plan(tmp_path: Path) -> tuple[PreparedCutoverPlan, Path]:
    release = tmp_path / "release" / "release-manifest.json"
    release.parent.mkdir()
    release.write_text(json.dumps({"release_id": "v3-cutover-test"}) + "\n")
    release_sha = hashlib.sha256(release.read_bytes()).hexdigest()
    source = tmp_path / "plan.json"
    source.write_text("{}\n")
    token = tmp_path / "token"
    token.write_text("secret\n")
    token.chmod(0o600)
    source_root = tmp_path / "v2-state"
    source_root.mkdir()
    mutable = MutableStateHandoffPlan(
        source_root=source_root,
        target_shared_root=tmp_path / "v3-shared",
        dry_run_receipt=tmp_path / "receipts" / "dry.json",
        prepare_receipt=tmp_path / "receipts" / "prepare.json",
        staging_root=tmp_path / "staging",
        release_id="v3-cutover-test",
        release_manifest_sha256=release_sha,
        deployment_manifest_sha256=HASH_B,
    )
    dummy = BoundFile(source, HASH_C)
    return (
        PreparedCutoverPlan(
            operation="v2_to_v3_cutover",
            execution_purpose="atomic_drill",
            source=dummy,
            gate_config=dummy,
            pre_cutover_report=dummy,
            deploy_prepared_marker=dummy,
            release_manifest=BoundFile(release, release_sha),
            token_sha256=hashlib.sha256(b"secret").hexdigest(),
            v2_launchagents=(),
            v2_application_set_sha256=HASH_A,
            v2_initial_loaded_set_sha256=HASH_A,
            v2_keepalive_set_sha256=HASH_A,
            v3_install_directory=tmp_path / "LaunchAgents",
            readiness_urls=(
                "http://127.0.0.1:5002/readyz",
                "http://127.0.0.1:5003/readyz",
                "http://127.0.0.1:8088/readyz",
            ),
            laf_dedup_handoff=None,
            pdf_namer_handoff=None,
            mutable_state_handoff=mutable,
        ),
        token,
    )


def _payload(plan: PreparedCutoverPlan, *, action: str, ready: bool) -> dict:
    assert plan.mutable_state_handoff is not None
    return {
        "status": "prepared" if action == "prepare" else "dry_run",
        "ready": ready,
        "contains_business_payload": False,
        "exact_context": {
            "release_id": plan.mutable_state_handoff.release_id,
            "release_manifest_sha256": plan.mutable_state_handoff.release_manifest_sha256,
            "deployment_manifest_sha256": plan.mutable_state_handoff.deployment_manifest_sha256,
            "cutover_plan_sha256": plan.source.sha256,
        },
        "source_snapshot_sha256": HASH_A,
        "target_before_snapshot_sha256": HASH_B,
        "target_snapshot_sha256": HASH_C,
    }


def _executor(tmp_path: Path, handoff):
    plan, token = _plan(tmp_path)
    executor = PreparedCutoverExecutor(
        plan,
        token_file=token,
        snapshot_collector=lambda: None,  # overridden below
        mutable_state_handoff=handoff,
        readiness_probe=lambda _urls: (True, {}),
    )
    executor._validate_static_gates = lambda: (  # type: ignore[method-assign]
        {},
        {"runtime_root": str(tmp_path / "runtime"), "roles": []},
        (),
    )
    executor._verify_effective_cutover_window = (  # type: ignore[method-assign]
        lambda **kwargs: executor._event(
            "test_window", stage=kwargs.get("stage", "")
        )
    )
    executor._recover_interrupted_activation = lambda _deploy, _agents: None  # type: ignore[method-assign]
    executor._assess = lambda expected: executor._event("test_assess", expected=expected)  # type: ignore[method-assign]
    executor._reconciliation = lambda _owner, **_kwargs: {"ok": True}  # type: ignore[method-assign]
    executor._execute_runtime_state_handoff = (  # type: ignore[method-assign]
        lambda **_kwargs: executor._event("test_runtime")
    )
    executor._execute_pdf_namer_handoff = lambda: executor._event("test_pdf")  # type: ignore[method-assign]
    executor._execute_laf_dedup_handoff = lambda: executor._event("test_laf")  # type: ignore[method-assign]
    executor._atomic_install = lambda _agents, _deploy: executor._event("test_install")  # type: ignore[method-assign]
    return executor, token


def test_handoff_runs_after_zero_before_install_and_hashes_enter_evidence(tmp_path: Path) -> None:
    calls: list[dict] = []
    plan_box: list[PreparedCutoverPlan] = []

    def handoff(**kwargs):
        calls.append(kwargs)
        action = kwargs["action"]
        return _payload(plan_box[0], action=action, ready=action == "prepare"), (
            "d" * 64 if action == "prepare" else "e" * 64
        )

    executor, token = _executor(tmp_path, handoff)
    plan_box.append(executor.plan)
    report = executor.execute()
    assert report["ok"] is True
    actions = [row["action"] for row in report["events"]]
    assert actions.index("test_assess", actions.index("activation_transaction")) < actions.index(
        "mutable_state_prepare"
    ) < actions.index("consume_token") < actions.index("test_install")
    assert [call["action"] for call in calls] == ["dry-run", "dry-run", "prepare"]
    assert calls[-1]["refresh"] is True
    assert calls[-1]["expected_target_snapshot_sha256"] == HASH_B
    assert report["hash_context"]["mutable_state_prepare_receipt_sha256"] == "d" * 64
    assert report["hash_context"]["mutable_state_target_snapshot_sha256"] == HASH_C
    assert not token.exists()


def test_post_zero_source_drift_restores_v2_without_consuming_token(tmp_path: Path) -> None:
    calls = 0
    plan_box: list[PreparedCutoverPlan] = []

    def handoff(**kwargs):
        nonlocal calls
        calls += 1
        payload = _payload(plan_box[0], action=kwargs["action"], ready=True)
        if calls == 2:
            payload["source_snapshot_sha256"] = "f" * 64
        return payload, "e" * 64

    executor, token = _executor(tmp_path, handoff)
    plan_box.append(executor.plan)
    report = executor.execute()
    assert report["ok"] is False
    assert report["rollback_ok"] is True
    assert token.exists()
    assert not any(row["action"] == "consume_token" for row in report["events"])
    assert any(row["action"] == "preserve_mutable_state_handoff" for row in report["events"])


def test_execute_replays_the_exact_pre_cutover_receipt_identity(tmp_path: Path) -> None:
    plan_box: list[PreparedCutoverPlan] = []

    def handoff(**kwargs):
        payload = _payload(plan_box[0], action=kwargs["action"], ready=True)
        payload["allowlist_sha256"] = HASH_A
        return payload, HASH_B

    executor, _token = _executor(tmp_path, handoff)
    plan_box.append(executor.plan)
    expected = _payload(executor.plan, action="dry-run", ready=True)
    executor.mutable_state_preflight = {
        "receipt_sha256": HASH_B,
        "allowlist_sha256": HASH_A,
        "source_snapshot_sha256": expected["source_snapshot_sha256"],
        "target_before_snapshot_sha256": expected[
            "target_before_snapshot_sha256"
        ],
        "target_snapshot_sha256": expected["target_snapshot_sha256"],
    }

    executor._execute_mutable_state_dry_run()
    assert executor.mutable_state_dry_run is not None

    executor.mutable_state_preflight = {
        **executor.mutable_state_preflight,
        "receipt_sha256": HASH_C,
    }
    with pytest.raises(CutoverError, match="does not replay"):
        executor._execute_mutable_state_dry_run()


def test_prepare_failure_preserves_receipts_and_target_for_retry(tmp_path: Path) -> None:
    plan_box: list[PreparedCutoverPlan] = []

    def handoff(**kwargs):
        if kwargs["action"] == "prepare":
            raise MutableStateHandoffError("injected target race")
        return _payload(plan_box[0], action="dry-run", ready=False), "e" * 64

    executor, token = _executor(tmp_path, handoff)
    plan_box.append(executor.plan)
    report = executor.execute()
    assert report["ok"] is False and report["rollback_ok"] is True
    assert token.exists()
    assert any(row["action"] == "mutable_state_prepare" and not row["ok"] for row in report["events"])
    assert any(row["action"] == "preserve_mutable_state_handoff" for row in report["events"])


def test_plan_hash_binds_mutable_roots_receipts_and_exact_context(tmp_path: Path) -> None:
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n")

    gates = tmp_path / "gates.json"
    release_gate = tmp_path / "release-gate.json"
    write_json(gates, {"schema_version": 1})
    write_json(release_gate, {"schema_version": 1})
    report = tmp_path / "pre.json"
    write_json(
        report,
        {
            "gate_config_sha256": hashlib.sha256(gates.read_bytes()).hexdigest(),
            "execution_purpose": "atomic_drill",
            "gate_stage": "cutover_drill_26_of_28",
            "decision": "GO_FOR_CUTOVER_DRILL_ONLY",
            "required_evidence_count": 28,
            "passed_evidence_count": 26,
            "excluded_evidence": [
                "atomic_release_switch_and_cold_rollback_drill_passed",
                "human_go_approval_recorded",
            ],
            "release_gate_report": {
                "path": str(release_gate.resolve()),
                "sha256": hashlib.sha256(release_gate.read_bytes()).hexdigest(),
            },
        },
    )
    release = tmp_path / "release.json"
    write_json(release, {"release_id": "v3-test"})
    release_sha = hashlib.sha256(release.read_bytes()).hexdigest()
    marker = tmp_path / "marker.json"
    write_json(
        marker,
        {
            "release_id": "v3-test",
            "release_manifest_sha256": release_sha,
            "manifest_sha256": HASH_B,
        },
    )
    install = tmp_path / "LaunchAgents"
    install.mkdir()
    bindings = []
    for label in REQUIRED_V2_APPLICATION_LABELS:
        plist = install / f"{label}.plist"
        plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/usr/bin/false"]}))
        bindings.append(f"{label}={plist}")
    token = tmp_path / "token-plan"
    token.write_text("secret\n")
    token.chmod(0o600)
    laf_source = tmp_path / "laf.json"
    write_json(laf_source, [])
    db_env = tmp_path / "db.env"
    db_env.write_text("DB=test\n")
    db_env.chmod(0o600)
    pdf_source = tmp_path / "pdf-source"
    pdf_source.mkdir()
    pdf_target = tmp_path / "pdf-target"
    pdf_manifest = tmp_path / "pdf.json"
    precopy(pdf_source, pdf_target, pdf_manifest, apply=True)
    source_root = tmp_path / "v2-state"
    source_root.mkdir()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    output = tmp_path / "plan.json"

    result = create_prepared_plan(
        operation="cutover",
        execution_purpose="atomic_drill",
        output=output,
        token_file=token,
        gate_config=gates,
        pre_cutover_report=report,
        deploy_prepared_marker=marker,
        release_manifest=release,
        v2_launchagents=bindings,
        v3_install_directory=install,
        laf_dedup_sources=[laf_source],
        laf_dedup_manifest_output=tmp_path / "laf-manifest.json",
        laf_dedup_db_env_file=db_env,
        pdf_namer_source=pdf_source,
        pdf_namer_destination=pdf_target,
        pdf_namer_manifest=pdf_manifest,
        mutable_state_source_root=source_root,
        mutable_state_target_shared_root=tmp_path / "shared",
        mutable_state_dry_run_receipt=receipts / "dry.json",
        mutable_state_prepare_receipt=receipts / "prepare.json",
        mutable_state_staging_root=tmp_path / "state-stage",
        launchd_probe=lambda label: {
            "argv": ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            "returncode": 0,
            "stdout": "state = running\npid = 100\n",
            "stderr": "",
            "timed_out": False,
        },
    )
    loaded = load_prepared_plan(
        output,
        result["plan_sha256"],
        canonical_launchagents_directory=install,
        require_mutable_state_handoff=True,
    )
    assert loaded.mutable_state_handoff is not None
    document = json.loads(output.read_text())
    assert document["mutable_state_handoff"]["exact_context"] == {
        "release_id": "v3-test",
        "release_manifest_sha256": release_sha,
        "deployment_manifest_sha256": HASH_B,
    }

    with pytest.raises(CutoverError, match="complete mutable-state"):
        create_prepared_plan(
            operation="cutover",
            execution_purpose="final_cutover",
            output=tmp_path / "unsafe-final-plan.json",
            token_file=token,
            gate_config=gates,
            pre_cutover_report=report,
            final_pre_cutover_report=tmp_path / "unsafe-final-report.json",
            deploy_prepared_marker=marker,
            release_manifest=release,
            v2_launchagents=bindings,
            v3_install_directory=install,
            laf_dedup_sources=[laf_source],
            laf_dedup_manifest_output=tmp_path / "unsafe-laf-manifest.json",
            laf_dedup_db_env_file=db_env,
            pdf_namer_source=pdf_source,
            pdf_namer_destination=pdf_target,
            pdf_namer_manifest=pdf_manifest,
        )

    final_plan = tmp_path / "final-plan.json"
    final_report = tmp_path / "final-pre-cutover.json"
    final_result = create_prepared_plan(
        operation="cutover",
        execution_purpose="final_cutover",
        output=final_plan,
        token_file=token,
        gate_config=gates,
        pre_cutover_report=report,
        final_pre_cutover_report=final_report,
        deploy_prepared_marker=marker,
        release_manifest=release,
        v2_launchagents=bindings,
        v3_install_directory=install,
        laf_dedup_sources=[laf_source],
        laf_dedup_manifest_output=tmp_path / "final-laf-manifest.json",
        laf_dedup_db_env_file=db_env,
        pdf_namer_source=pdf_source,
        pdf_namer_destination=pdf_target,
        pdf_namer_manifest=pdf_manifest,
        mutable_state_source_root=source_root,
        mutable_state_target_shared_root=tmp_path / "final-shared",
        mutable_state_dry_run_receipt=receipts / "final-dry.json",
        mutable_state_prepare_receipt=receipts / "final-prepare.json",
        mutable_state_staging_root=tmp_path / "final-state-stage",
        launchd_probe=lambda label: {
            "argv": ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            "returncode": 0,
            "stdout": "state = running\npid = 100\n",
            "stderr": "",
            "timed_out": False,
        },
    )
    final_document = json.loads(final_plan.read_text(encoding="utf-8"))
    assert final_document["plan_preparation_report"] == {
        "path": str(report.resolve()),
        "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
    }
    assert final_document["pre_cutover_report"] == {
        "path": str(final_report.resolve()),
        "path_sha256": hashlib.sha256(str(final_report.resolve()).encode()).hexdigest(),
    }
    with pytest.raises(CutoverError, match="final pre-cutover report is unavailable"):
        load_prepared_plan(
            final_plan,
            final_result["plan_sha256"],
            canonical_launchagents_directory=install,
            require_mutable_state_handoff=True,
        )
    write_json(final_report, {"schema_version": 1, "synthetic": True})
    final_loaded = load_prepared_plan(
        final_plan,
        final_result["plan_sha256"],
        canonical_launchagents_directory=install,
        require_mutable_state_handoff=True,
    )
    assert final_loaded.execution_purpose == "final_cutover"
    assert final_loaded.plan_preparation_report is not None
    assert final_loaded.pre_cutover_report.path == final_report.resolve()

    document["mutable_state_handoff"]["source_root"]["path"] = str(tmp_path / "other")
    output.write_text(json.dumps(document, sort_keys=True) + "\n")
    tampered_sha = hashlib.sha256(output.read_bytes()).hexdigest()
    with pytest.raises(CutoverError, match="path hash mismatch"):
        load_prepared_plan(
            output,
            tampered_sha,
            canonical_launchagents_directory=install,
            require_mutable_state_handoff=True,
        )
