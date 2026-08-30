from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from magi_v3.gateway import build_gateway
from scripts import v3_deploy_prepare as deploy
from scripts import v3_static_external_staging as static_external
from scripts.v3_credential_handoff_prepare import materialize_secret_handoff
from scripts.v3_laf_dedup_compat import LAFDedupBlocked
from scripts.v3_pdf_namer_handoff import precopy
from scripts.v3_cutover.core import CutoverError, Owner, Snapshot
from scripts.v3_cutover.activation import ActivationTransaction
import scripts.v3_cutover.mutation as mutation_module
from scripts.v3_cutover.mutation import (
    BoundFile,
    LaunchAgent,
    PreparedCutoverExecutor,
    PreparedRollbackExecutor,
    REQUIRED_V2_APPLICATION_LABELS,
    _default_reconciliation_probe,
    execute_laf_dedup_handoff,
    execute_runtime_state_handoff,
    load_prepared_plan,
    v2_application_set_sha256,
    v2_initial_loaded_set_sha256,
    v2_keepalive_set_sha256,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 13, 19, 5, tzinfo=timezone.utc)  # 03:05 Asia/Taipei
FULL_COVERAGE = frozenset({"process", "pidfile", "port", "launchd", "ownership"})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    _write(path, json.dumps(payload, sort_keys=True) + "\n")


def _seal_test_release(path: Path) -> None:
    for directory, _directory_names, file_names in os.walk(
        path,
        topdown=False,
        followlinks=False,
    ):
        base = Path(directory)
        for name in file_names:
            member = base / name
            member.chmod(0o555 if member.stat().st_mode & 0o111 else 0o444)
        base.chmod(0o555)


def _binding(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": _sha256(path)}


def _conditional_binding(path: Path, *, window: dict[str, str], metrics_sha256: str) -> dict[str, object]:
    return {
        "evidence_path": str(path.resolve()),
        "evidence_sha256": _sha256(path),
        "metrics_sha256": metrics_sha256,
        "conditional_daytime_window": window,
        "conditional_request_sha256": "1" * 64,
        "conditional_receipt_sha256": "2" * 64,
        "conditional_consumption_sha256": "3" * 64,
    }


def test_final_executor_requires_conditional_authorization_when_policy_demands_it() -> None:
    executor = object.__new__(PreparedCutoverExecutor)
    with pytest.raises(CutoverError, match="requires a redeemed conditional"):
        executor._verify_conditional_daytime_authorization(
            gates={"conditional_daytime_authorization_required": True},
            release_gate={},
            expected_context={},
            now=NOW,
        )


def test_final_executor_effective_daytime_window_is_end_exclusive() -> None:
    executor = object.__new__(PreparedCutoverExecutor)
    executor.events = []
    executor.clock = lambda: datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)
    gates = {
        "window": {"start": "02:00", "end": "04:00"},
        "conditional_daytime_authorization_required": True,
        "conditional_daytime_window": {
            "starts_at": "2026-07-27T09:00:00+08:00",
            "ends_at": "2026-07-27T18:00:00+08:00",
            "timezone": "Asia/Taipei",
        },
    }
    assert executor._verify_effective_cutover_window(
        gates=gates,
        now=datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc),
        stage="test-start",
    )["within_window"] is True
    with pytest.raises(CutoverError, match="effective cutover window"):
        executor._verify_effective_cutover_window(
            gates=gates,
            now=datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc),
            stage="test-end",
        )


def test_final_executor_rejects_conditional_metrics_sha_drift(tmp_path: Path, monkeypatch) -> None:
    evidence = tmp_path / "human.json"
    _write_json(evidence, {"placeholder": True})
    window = {
        "starts_at": "2026-07-14T06:00:00+08:00",
        "ends_at": "2026-07-14T08:00:00+08:00",
        "timezone": "Asia/Taipei",
    }
    metrics = {
        "authorization_mode": "conditional_daytime_window",
        "conditional_daytime_window": window,
        "conditional_request_sha256": "1" * 64,
        "conditional_receipt_sha256": "2" * 64,
        "conditional_consumption_sha256": "3" * 64,
    }
    from scripts import v3_release_gate as gate_module

    producer = gate_module.BoundArtifact(
        role="producer_report",
        media_type="application/json",
        path="producer.json",
        sha256="4" * 64,
        data=json.dumps({"metrics": metrics}).encode(),
    )
    monkeypatch.setattr(
        gate_module,
        "load_json",
        lambda _path: {
            "evidence_id": "human_go_approval_recorded",
            "status": "passed",
            "campaign_id": "campaign",
            "release_sha": "a" * 64,
            "hardware_id": "hardware",
            "gate_config_sha256": "b" * 64,
        },
    )
    monkeypatch.setattr(gate_module, "freeze_artifacts", lambda *_args: ([producer], []))
    monkeypatch.setattr(gate_module, "validate_evidence_semantics", lambda *_args, **_kwargs: [])
    executor = object.__new__(PreparedCutoverExecutor)
    with pytest.raises(CutoverError, match="metrics drifted"):
        executor._verify_conditional_daytime_authorization(
            gates={},
            release_gate={
                "conditional_authorization": _conditional_binding(
                    evidence, window=window, metrics_sha256="f" * 64
                )
            },
            expected_context={
                "campaign_id": "campaign",
                "release_sha": "a" * 64,
                "hardware_id": "hardware",
                "gate_config_sha256": "b" * 64,
            },
            now=datetime(2026, 7, 13, 23, 5, tzinfo=timezone.utc),
        )


def _docx_runtime_preflight_stub() -> dict[str, object]:
    """Current complete DOCX evidence for cutover-only fake runtimes."""

    evidence: dict[str, object] = {
        "module": "docx",
        "distribution": "python-docx",
        "minimum_version": "1.0",
        "version": "1.2.0",
        "module_sha256": "d" * 64,
        "distribution_metadata_sha256": "e" * 64,
        "distribution_record_sha256": "f" * 64,
        "runtime_tree_sha256": "c" * 64,
        "distribution_unambiguous": True,
        "distribution_metadata_manifest_bound": True,
        "distribution_record_manifest_bound": True,
        "distribution_module_owned": True,
        "distribution_module_record_bound": True,
        "distribution_version_matches_module": True,
        "import_succeeded": True,
        "module_manifest_bound": True,
        "roundtrip_succeeded": True,
    }
    evidence["evidence_sha256"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return evidence


def _initial_launchd(label: str, *, loaded: bool = True, pid: int | None = 100) -> dict:
    receipt = {
        "argv": ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
        "returncode": 0 if loaded else 113,
        "stdout": f"state = running\npid = {pid}\n" if loaded and pid else (
            "state = not running\n" if loaded else ""
        ),
        "stderr": "" if loaded else "Could not find service",
        "timed_out": False,
    }
    return {
        "loaded": loaded,
        "state": "running" if loaded and pid else ("not running" if loaded else ""),
        "pid": pid if loaded else None,
        "launchctl_receipt": receipt,
        "launchctl_receipt_sha256": hashlib.sha256(
            (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest(),
    }


@dataclass
class PreparedFixture:
    plan: Path
    plan_sha256: str
    token: Path
    staging: Path
    install: Path
    v2_plist: Path
    laf_source: Path
    laf_manifest: Path
    db_env_file: Path
    pdf_source: Path
    pdf_destination: Path
    pdf_manifest: Path


def _prepared_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_unloaded: frozenset[str] = frozenset(),
    loaded_without_pid: frozenset[str] = frozenset(),
    keepalive_labels: frozenset[str] = frozenset(),
) -> PreparedFixture:
    application_support = tmp_path / "Application Support"
    monkeypatch.setattr(deploy, "_application_support_root", lambda: application_support)
    # The shell file below is only a hash-bound stand-in for cutover fixtures;
    # python-docx runtime preflight has dedicated coverage in
    # test_deploy_prepare.py.  Supply the complete current evidence contract so
    # these tests continue to exercise real prepare/cutover behavior.
    monkeypatch.setattr(
        deploy,
        "_probe_python_docx_runtime",
        lambda *_args, **_kwargs: _docx_runtime_preflight_stub(),
    )
    # The fixture runtime below is an inert shell stand-in used to exercise
    # cutover ordering and rollback semantics.  Shapely's exact-runtime probe
    # has its own production-bound positive and fail-closed contract tests in
    # test_shapely_sealed_preflight.py; do not try to import packages through
    # this intentionally non-Python executable.
    monkeypatch.setattr(
        deploy,
        "_validate_shapely_sealed_runtime",
        lambda *_args, **_kwargs: None,
    )
    cron_jobs = tmp_path / "inputs" / "cron_jobs.json"
    _write_json(
        cron_jobs,
        [
            {
                "id": "job-test",
                "cron": "0 9 * * *",
                "command": "@MAGI health",
                "enabled": True,
            }
        ],
    )
    release = tmp_path / "release"
    modules = {
        "gateway": "magi_v3/gateway.py",
        "control": "magi_v3/control.py",
        "supervisor": "magi_v3/supervisor_service.py",
    }
    for relative in modules.values():
        _write(release / relative, "def main():\n    return 0\n")
    roles_relative = "config/v3_launchagent_roles.json"
    _write(
        release / roles_relative,
        (ROOT / roles_relative).read_text(encoding="utf-8"),
    )
    service_manifest_relative = "config/v3_service_manifest.json"
    service_manifest = {
        "schema_version": 1,
        "release_mode": "single_active_replacement",
        "deployment_mode": "production",
        "services": [
            {
                "id": "main_http",
                "role": "gateway",
                "kind": "wsgi",
                "required": True,
                "port": 5002,
                "factory": "magi_v3.compat:create_main_app",
            },
            {
                "id": "tools_http",
                "role": "gateway",
                "kind": "wsgi",
                "required": True,
                "port": 5003,
                "factory": "magi_v3.compat:create_tools_app",
            },
            {
                "id": "website_admin",
                "role": "control",
                "kind": "http_server",
                "required": True,
                "port": 8088,
                "factory": "magi_v3.compat:create_admin_server",
            },
            {
                "id": "heartbeat",
                "role": "supervisor",
                "kind": "process",
                "required": True,
                "argv": ["{python}", "skills/ops/heartbeat.py"],
            },
        ],
        "host_singletons": ["mariadb"],
        "forbidden_release_processes": ["daemon.py"],
    }
    _write_json(release / service_manifest_relative, service_manifest)
    cron_policy_relative = deploy.CRON_DISPATCH_POLICY_NAME
    _write_json(
        release / cron_policy_relative,
        {
            "schema_version": 1,
            "cron_jobs_sha256": _sha256(cron_jobs),
        },
    )
    service_entrypoints = tuple(
        sorted(
            str(service["argv"][1])
            for service in service_manifest["services"]
            if service["kind"] == "process"
        )
    )
    for relative in service_entrypoints:
        _write(release / relative, "def main():\n    return 0\n")
    executable = release / "bin" / "python3"
    _write(executable, "#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    for directory, _directory_names, file_names in os.walk(release):
        base = Path(directory)
        for name in file_names:
            member = base / name
            member.chmod(0o555 if member.stat().st_mode & 0o111 else 0o444)
    files = []
    for relative in (
        *modules.values(),
        *service_entrypoints,
        "bin/python3",
        roles_relative,
        service_manifest_relative,
        cron_policy_relative,
    ):
        path = release / relative
        files.append(
            {
                "path": relative,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "mode": f"{path.stat().st_mode & 0o777:04o}",
            }
        )
    files.sort(key=lambda row: row["path"])
    release_sha256 = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    release_manifest = release / "release-manifest.json"
    _write_json(
        release_manifest,
        {
            "schema_version": 1,
            "immutable": True,
            "release_id": "v3-cutover-test",
            "commit": "a" * 40,
            "source_snapshot_sha256": release_sha256,
            "release_sha256": release_sha256,
            "files": files,
        },
    )
    _write_json(
        release / "RELEASE_COMPLETE.json",
        {
            "schema_version": 1,
            "release_id": "v3-cutover-test",
            "commit": "a" * 40,
            "manifest": release_manifest.name,
            "manifest_sha256": _sha256(release_manifest),
            "source_snapshot_sha256": release_sha256,
            "release_sha256": release_sha256,
        },
    )
    _seal_test_release(release)
    installed_release = deploy._canonical_installed_release_root("v3-cutover-test")
    installed_release.parent.mkdir(parents=True)
    shutil.copytree(release, installed_release)
    installed_release_manifest = installed_release / deploy.RELEASE_MANIFEST_NAME

    env_file = tmp_path / "inputs" / "magi.env"
    _write(env_file, "DISCORD_TOKEN=test-only\n")
    env_file.chmod(0o600)
    website = tmp_path / "inputs" / "website"
    _write(website / "admin" / "admin_server.py", "class AdminHandler: pass\n")
    python_runtime = tmp_path / "inputs" / "python-venv" / "bin" / "python"
    _write(python_runtime, "#!/bin/sh\nexit 0\n")
    python_runtime.chmod(0o755)
    _write(
        python_runtime.parent.parent / "pyvenv.cfg",
        "home = " + str(python_runtime.parent) + "\n"
        "include-system-site-packages = false\n"
        "executable = " + str(python_runtime) + "\n",
    )
    _write(
        python_runtime.parent.parent / "lib" / "python3.14" / "site-packages" / "example.py",
        "VALUE = 1\n",
    )
    nas_root = tmp_path / "inputs" / "nas"
    case_root = nas_root / "01_案件"
    archive_root = nas_root / "10_結案"
    case_root.mkdir(parents=True)
    archive_root.mkdir()
    runtime = tmp_path / "runtime" / "MAGI_v3"
    laf_config = tmp_path / "inputs" / "config.json"
    google_credentials = tmp_path / "inputs" / "credentials.json"
    _write_json(laf_config, {})
    _write_json(google_credentials, {})
    laf_config.chmod(0o600)
    google_credentials.chmod(0o600)
    google_calendar_token = tmp_path / "inputs" / "google_calendar_token.json"
    laf_gmail_token = tmp_path / "inputs" / "laf_gmail_token.pickle"
    file_review_token = tmp_path / "inputs" / "filereview_token.pickle"
    for path in (google_calendar_token, laf_gmail_token, file_review_token):
        _write(path, "inert-token\n")
    ocr_queue = tmp_path / "inputs" / "nas-ocr-queue.db"
    with sqlite3.connect(ocr_queue) as connection:
        connection.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(deploy, "_canonical_nas_ocr_queue_db", lambda: ocr_queue.resolve())
    monkeypatch.setattr(deploy, "_canonical_runtime_root", lambda: runtime)
    static_snapshot = static_external.snapshot_static_sources(
        release_manifest,
        expected_release_manifest_sha256=_sha256(release_manifest),
        env_file=env_file,
        website_root=website,
        config_file=laf_config,
        google_credentials_file=google_credentials,
        accounting_credentials_file=google_credentials,
    )
    static_target = runtime / "shared" / "external"
    static_external.stage_static_external(
        release_manifest,
        expected_release_manifest_sha256=_sha256(release_manifest),
        env_file=env_file,
        website_root=website,
        config_file=laf_config,
        google_credentials_file=google_credentials,
        accounting_credentials_file=google_credentials,
        expected_source_snapshot_sha256=static_snapshot["source_snapshot_sha256"],
        target_root=static_target,
    )
    build_staging = tmp_path / ".prepared-deploy-staging"
    staging = tmp_path / "prepared-deploy"
    deploy.prepare_deployment(
        release,
        build_staging,
        runtime,
        executable,
        env_file=static_target / ".env",
        cron_jobs_file=cron_jobs,
        website_root=static_target / "website",
        python_runtime=python_runtime,
        laf_config_file=static_target / "config.json",
        google_credentials_file=static_target / "google-credentials.json",
        google_calendar_token_file=google_calendar_token,
        laf_gmail_token_file=laf_gmail_token,
        file_review_token_file=file_review_token,
        accounting_credentials_file=static_target / "accounting-credentials.json",
        accounting_sheets_token_file=google_calendar_token,
        drive_sync_token_file=google_calendar_token,
        drive_sync_write_token_file=google_calendar_token,
        nas_ocr_queue_db_path=ocr_queue,
        static_external_receipt=static_target / static_external.RECEIPT_NAME,
        case_root=case_root,
        archive_root=archive_root,
        path_mappings=((str(nas_root), "Z:"),),
        publish_dir=staging,
        now=NOW,
    )
    assert not build_staging.exists()
    prepared_marker = json.loads((staging / deploy.COMPLETION_MARKER_NAME).read_text())
    materialize_secret_handoff(
        staging / deploy.DEPLOY_MANIFEST_NAME,
        expected_manifest_sha256=prepared_marker["manifest_sha256"],
    )

    gates = tmp_path / "evidence" / "gates.json"
    gates.parent.mkdir()
    gates.write_bytes((ROOT / "config" / "v3_cutover_gates.json").read_bytes())
    fixture_gates = json.loads(gates.read_text(encoding="utf-8"))
    fixture_gates["window"] = {
        "start": "02:00",
        "end": "04:00",
        "allowed_local_dates": ["2026-07-14"],
    }
    fixture_gates.pop("conditional_daytime_window", None)
    fixture_gates.pop("conditional_daytime_authorization_required", None)
    fixture_gates["source_contract"]["legacy_v2_validation"] = "enabled"
    fixture_gates["source_contract"]["database_relatives"] = [
        relative for relative, _tables in mutation_module.FORMAL_STATE_DATABASES
    ]
    fixture_gates["automatic_no_go"] = list(
        dict.fromkeys(
            [
                *fixture_gates["automatic_no_go"],
                "v2_process_or_release_owner_still_active_before_v3_start",
                "v2_port_scheduler_writer_or_model_owner_not_released",
            ]
        )
    )
    _write_json(gates, fixture_gates)
    report = tmp_path / "evidence" / "pre-cutover.json"
    release_gate_report = tmp_path / "evidence" / "release-gate.json"
    required_evidence = json.loads(gates.read_text(encoding="utf-8"))["required_evidence"]
    excluded = list(mutation_module.ATOMIC_DRILL_EXCLUDED_EVIDENCE)
    context = {
        "campaign_id": "atomic-drill-test",
        "release_sha": release_sha256,
        "hardware_id": "test-mac",
        "gate_config_sha256": _sha256(gates),
    }
    _write_json(
        release_gate_report,
        {
            "schema_version": 1,
            "decision": "NO_GO",
            "fail_closed": True,
            "required_count": len(required_evidence),
            "expected_context": context,
            "passed": [item for item in required_evidence if item not in excluded],
            "missing": excluded,
            "failed": [],
            "invalid": {},
        },
    )
    required_checks = (
        "cutover_window",
        "gate_config_binding",
        "v2_only_ownership",
        "v3_deploy_prepared",
        "v3_readiness_manifest",
        "v3_release_marker_manifest",
        "pdf_namer_handoff_precopy",
    )
    _write_json(
        report,
        {
            "schema_version": 1,
            "observed_at": NOW.isoformat(),
            "release_sha": release_sha256,
            "gate_config_sha256": _sha256(gates),
            "decision": "GO_FOR_CUTOVER_DRILL_ONLY",
            "execution_purpose": "atomic_drill",
            "gate_stage": f"cutover_drill_{len(required_evidence) - len(excluded)}_of_{len(required_evidence)}",
            "required_evidence_count": len(required_evidence),
            "passed_evidence_count": len(required_evidence) - len(excluded),
            "excluded_evidence": excluded,
            "expected_context": context,
            "release_gate_report": _binding(release_gate_report),
            "fail_closed": True,
            "mutation_performed": False,
            "gaps": [],
            "checks": [{"name": name, "ok": True} for name in required_checks],
        },
    )
    install = tmp_path / "LaunchAgents"
    install.mkdir()
    v2_rows = []
    v2_agents = []
    for label in REQUIRED_V2_APPLICATION_LABELS:
        plist = install / f"{label}.plist"
        plist_payload: dict[str, object] = {
            "Label": label,
            "ProgramArguments": ["/usr/bin/false"],
        }
        if label in keepalive_labels:
            plist_payload["KeepAlive"] = True
        plist.write_bytes(
            plistlib.dumps(plist_payload, sort_keys=True)
        )
        binding = _binding(plist)
        loaded = label not in initial_unloaded
        initial = _initial_launchd(
            label,
            loaded=loaded,
            pid=None if label in loaded_without_pid else 100,
        )
        keepalive_required = label in keepalive_labels
        v2_rows.append(
            {
                "label": label,
                "plist": binding,
                "initial_launchd": initial,
                "keepalive_required_running": keepalive_required,
            }
        )
        v2_agents.append(
            LaunchAgent(
                label,
                BoundFile(plist, binding["sha256"]),
                loaded,
                str(initial["state"]),
                keepalive_required,
                initial["launchctl_receipt_sha256"],
            )
        )
    v2_plist = install / "com.magi.daemon.plist"
    token = tmp_path / "authorization" / "cutover.token"
    _write(token, "one-time-cutover-authorization\n")
    token.chmod(0o600)
    laf_source = tmp_path / "runtime-v2" / "laf_processed_emails.json"
    _write_json(laf_source, ["gmail-message-test"])
    laf_manifest = tmp_path / "handoff" / "laf-dedup-manifest.json"
    laf_manifest.parent.mkdir()
    db_env_file = tmp_path / "inputs" / "db.env"
    _write(db_env_file, "OSC_DB_PASSWORD=test-only-secret\n")
    db_env_file.chmod(0o600)
    pdf_source = tmp_path / "runtime-v2" / "skills" / "pdf-namer"
    pdf_source.mkdir(parents=True)
    _write_json(pdf_source / "training_data.json", [{"synthetic": "case-value"}])
    pdf_destination = tmp_path / "runtime-v3" / "runtime" / "shared" / "pdf-namer"
    pdf_manifest = tmp_path / "handoff" / "pdf-namer-manifest.json"
    precopy(pdf_source, pdf_destination, pdf_manifest, apply=True)
    plan = tmp_path / "cutover-plan.json"
    _write_json(
        plan,
        {
            "schema_version": 1,
            "operation": "v2_to_v3_cutover",
            "execution_purpose": "atomic_drill",
            "gate_config": _binding(gates),
            "pre_cutover_report": _binding(report),
            "deploy_prepared_marker": _binding(staging / "DEPLOY_PREPARED.json"),
            "release_manifest": _binding(installed_release_manifest),
            "token_sha256": hashlib.sha256(b"one-time-cutover-authorization").hexdigest(),
            "v2_launchagents": v2_rows,
            "v2_application_set_sha256": v2_application_set_sha256(tuple(v2_agents)),
            "v2_initial_loaded_set_sha256": v2_initial_loaded_set_sha256(
                tuple(v2_agents)
            ),
            "v2_keepalive_set_sha256": v2_keepalive_set_sha256(tuple(v2_agents)),
            "v3_install_directory": str(install),
            "readiness_urls": [
                "http://127.0.0.1:5002/readyz",
                "http://127.0.0.1:5003/readyz",
                "http://127.0.0.1:8088/readyz",
            ],
            "laf_dedup_handoff": {
                "source_paths": [str(laf_source.resolve())],
                "manifest_output": str(laf_manifest.resolve()),
                "db_env_file": _binding(db_env_file),
            },
            "pdf_namer_handoff": {
                "source": str(pdf_source.resolve()),
                "destination": str(pdf_destination.resolve()),
                "manifest": str(pdf_manifest.resolve()),
            },
        },
    )
    return PreparedFixture(
        plan,
        _sha256(plan),
        token,
        staging,
        install,
        v2_plist,
        laf_source,
        laf_manifest,
        db_env_file,
        pdf_source,
        pdf_destination,
        pdf_manifest,
    )


def _snapshot(state: str) -> Snapshot:
    owners: tuple[Owner, ...]
    launchd: dict[str, dict[str, object]]
    if state == "zero":
        owners = ()
        launchd = {}
    elif state == "mixed":
        owners = (
            Owner("v2", "release", "v2", "test", pid=101),
            Owner("v3", "release", "v3", "test", pid=201),
        )
        launchd = {
            **{
                label: {
                    "loaded": True,
                    "pid": 101 + index,
                    "state": "running",
                }
                for index, label in enumerate(REQUIRED_V2_APPLICATION_LABELS)
            },
            "com.magi.v3.control": {"loaded": True, "pid": 201},
        }
    elif state == "v3":
        owners = (Owner("v3", "release", "v3", "test", pid=201),)
        launchd = {
            "com.magi.v3.control": {"loaded": True, "pid": 201},
            "com.magi.v3.gateway": {"loaded": True, "pid": 202},
            "com.magi.v3.supervisor": {"loaded": True, "pid": 203},
        }
    else:
        owners = (Owner(state, "release", state, "test", pid=101),)  # type: ignore[arg-type]
        launchd = {
            label: {"loaded": True, "pid": 101 + index, "state": "running"}
            for index, label in enumerate(REQUIRED_V2_APPLICATION_LABELS)
        }
    return Snapshot(
        owners=owners,
        coverage=FULL_COVERAGE,
        observed_at=NOW.isoformat(),
        metadata={"launchd": launchd},
    )


class FakeMachine:
    def __init__(self, fixture: PreparedFixture, *, state: str = "v2") -> None:
        self.fixture = fixture
        self.state = state
        self.commands: list[tuple[str, ...]] = []
        self.fail_label = ""
        self.leave_v2_residual = False
        plan = json.loads(fixture.plan.read_text(encoding="utf-8"))
        self.initial_launchd = {
            row["label"]: {
                "loaded": row["initial_launchd"]["loaded"],
                "pid": row["initial_launchd"]["pid"],
                "state": row["initial_launchd"]["state"],
            }
            for row in plan["v2_launchagents"]
        }
        self.launchd_overrides: dict[str, dict[str, object]] = {}

    def probe(self) -> Snapshot:
        if self.state == "v2":
            launchd = {
                label: {**status, **self.launchd_overrides.get(label, {})}
                for label, status in self.initial_launchd.items()
            }
            return Snapshot(
                owners=(Owner("v2", "release", "v2", "test", pid=101),),
                coverage=FULL_COVERAGE,
                observed_at=NOW.isoformat(),
                metadata={"launchd": launchd},
            )
        return _snapshot(self.state)

    def run(self, argv):
        command = tuple(argv)
        self.commands.append(command)
        label_or_path = command[-1]
        if self.fail_label and self.fail_label in label_or_path:
            return SimpleNamespace(returncode=5, stdout="", stderr="injected failure")
        if command[1] == "bootout":
            if self.leave_v2_residual and label_or_path.startswith("gui/501/com.magi."):
                self.state = "v2"
            elif label_or_path.endswith("com.magi.daemon"):
                self.state = "zero"
            else:
                self.state = "zero"
        elif (
            command[1] == "bootstrap"
            and Path(label_or_path).parent == self.fixture.install
            and Path(label_or_path).stem in REQUIRED_V2_APPLICATION_LABELS
        ):
            self.state = "v2"
        else:
            self.state = "v3"
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _executor(
    fixture: PreparedFixture,
    machine: FakeMachine,
    *,
    readiness=True,
    laf_dedup_handoff=None,
) -> PreparedCutoverExecutor:
    plan = load_prepared_plan(
        fixture.plan,
        fixture.plan_sha256,
        canonical_launchagents_directory=fixture.install,
    )
    handoff = laf_dedup_handoff or _fake_laf_handoff
    return PreparedCutoverExecutor(
        plan,
        token_file=fixture.token,
        snapshot_collector=machine.probe,
        runner=machine.run,
        readiness_probe=lambda _urls: (readiness, {"injected": readiness}),
        laf_dedup_handoff=handoff,
        reconciliation_probe=_fake_reconciliation,
        runtime_state_handoff=_fake_runtime_state_handoff,
        clock=lambda: NOW,
        uid=501,
    )


def _fake_laf_handoff(plan):
    plan.manifest_output.write_text("{}\n", encoding="utf-8")
    plan.manifest_output.chmod(0o600)
    return {
        "status": "complete",
        "stages": [
            "snapshot",
            "verify",
            "db_dry_run",
            "apply",
            "dual_table_verify",
            "source_reverify",
        ],
        "manifest_sha256": "d" * 64,
        "record_count": 1,
        "records_sha256": "e" * 64,
        "dry_run_ok": True,
        "apply_ok": True,
        "dual_table_verified": True,
        "contains_business_payload": False,
    }


def _fake_reconciliation(_v2_root, v3_runtime, active_owner):
    source_inventory = [
        {
            "relative_path": relative,
            "tables": sorted(tables),
            "database_file_sha256": "7" * 64,
            "wal_present": False,
            "wal_sha256": "",
            "database_snapshot_sha256": "8" * 64,
        }
        for relative, tables in mutation_module.FORMAL_STATE_DATABASES
    ]
    return {
        "schema_version": 1,
        "certifiable": True,
        "active_owner": active_owner,
        "active_job_store": str(v3_runtime / active_owner / "job_queue.db"),
        "pending_ownership_certified": True,
        "sources_probed": [f"{active_owner}_compat_job_queue"],
        "delivery_receipts_state": "absent_verified",
        "native_ledger_roles": ["control", "gateway", "supervisor"],
        "pending_id_hashes": ["1" * 64],
        "pending_id_hash_occurrences": ["1" * 64],
        "native_pending_id_hashes": [],
        "orphaned_pending_id_hashes": [],
        "terminal_id_hashes": ["2" * 64],
        "committed_id_hashes": ["3" * 64],
        "committed_id_hash_occurrences": ["3" * 64],
        "sent_outbox_id_hashes": ["4" * 64],
        "sent_outbox_id_hash_occurrences": ["4" * 64],
        "duplicate_committed_jobs": 0,
        "duplicate_sent_outbox": 0,
        "source_inventory": source_inventory,
        "source_inventory_sha256": hashlib.sha256(
            json.dumps(source_inventory, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _fake_runtime_state_handoff(_source, _target):
    databases = [
        {
            "relative_path": relative,
            "tables": list(tables),
            "source_snapshot_sha256": "5" * 64,
            "target_database_sha256": "6" * 64,
        }
        for relative, tables in mutation_module.FORMAL_STATE_DATABASES
    ]
    return {
        "schema_version": 1,
        "status": "complete",
        "databases": databases,
        "database_inventory_sha256": hashlib.sha256(
            json.dumps(databases, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "pending_count": 1,
        "pending_id_set_sha256": "6" * 64,
        "delivery_state_files_copied": [],
        "business_payload_copied": True,
        "business_payload_emitted": False,
    }


def _write_compat_queue(path: Path, rows: list[tuple[str, str]]) -> None:
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.executemany("INSERT INTO jobs(id,status) VALUES(?,?)", rows)


def _write_formal_state_databases(root: Path) -> None:
    import sqlite3

    for relative, tables in mutation_module.FORMAL_STATE_DATABASES[1:]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            for table in tables:
                connection.execute(f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY)')


def test_runtime_state_handoff_proves_v3_owns_imported_pending_jobs_and_roundtrips(
    tmp_path: Path,
) -> None:
    v2_root = tmp_path / "v2"
    v2_agent = v2_root / ".agent"
    v3_runtime = tmp_path / "v3-runtime"
    v3_agent = v3_runtime / "shared" / "agent"
    _write_compat_queue(
        v2_agent / "jobs" / "job_queue.db",
        [("pending-1", "queued"), ("complete-1", "done")],
    )
    _write_formal_state_databases(v2_root)
    _write_json(v2_agent / "red_phone_outbox.json", [])
    _write(
        v2_agent / "red_phone_delivery.jsonl",
        json.dumps(
            {
                "event": "outbox_recovered",
                "entry_id": "delivery-1",
                "ts": NOW.isoformat(),
            }
        )
        + "\n",
    )

    forward = execute_runtime_state_handoff(v2_agent, v3_agent)
    from magi_v3.ledger import JobLedger

    for role in ("control", "gateway", "supervisor"):
        JobLedger(v3_runtime / "state" / role / "ledger.sqlite3").initialize()
    active_v3 = _default_reconciliation_probe(v2_root, v3_runtime, "v3")

    assert forward["pending_count"] == 1
    assert active_v3["certifiable"] is True
    assert active_v3["active_owner"] == "v3"
    assert len(active_v3["pending_id_hashes"]) == 1
    assert len(active_v3["committed_id_hashes"]) == 1
    assert len(active_v3["sent_outbox_id_hashes"]) == 1

    import sqlite3

    with sqlite3.connect(v3_agent / "jobs" / "job_queue.db") as connection:
        connection.execute("UPDATE jobs SET status='done' WHERE id='pending-1'")
    reverse = execute_runtime_state_handoff(v3_agent, v2_agent)
    restored_v2 = _default_reconciliation_probe(v2_root, v3_runtime, "v2")
    assert reverse["pending_count"] == 0
    assert restored_v2["certifiable"] is True
    assert len(restored_v2["committed_id_hashes"]) == 2


def _rollback_executor(
    fixture: PreparedFixture,
    machine: FakeMachine,
) -> PreparedRollbackExecutor:
    payload = json.loads(fixture.plan.read_text(encoding="utf-8"))
    payload["operation"] = "v3_to_v2_rollback"
    payload.pop("laf_dedup_handoff")
    payload.pop("pdf_namer_handoff")
    _write_json(fixture.plan, payload)
    fixture.plan_sha256 = _sha256(fixture.plan)
    for source in fixture.staging.glob("launchagents/*.plist"):
        fixture.install.mkdir(parents=True, exist_ok=True)
        (fixture.install / source.name).write_bytes(source.read_bytes())
    deploy_manifest = json.loads(
        (fixture.staging / deploy.DEPLOY_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    ownership_target = Path(deploy_manifest["ownership_manifest"])
    ownership_target.parent.mkdir(parents=True, exist_ok=True)
    ownership_target.write_bytes(
        (fixture.staging / deploy.OWNERSHIP_MANIFEST_NAME).read_bytes()
    )
    plan = load_prepared_plan(
        fixture.plan,
        fixture.plan_sha256,
        canonical_launchagents_directory=fixture.install,
    )
    runtime_root = Path(deploy_manifest["runtime_root"])
    release_payload = json.loads(plan.release_manifest.path.read_text(encoding="utf-8"))
    transaction = ActivationTransaction.begin(
        state_parent=runtime_root.parent,
        plan_sha256="9" * 64,
        release_id=release_payload["release_id"],
        release_root=plan.release_manifest.path.parent,
        release_manifest_sha256=plan.release_manifest.sha256,
        reconciliation_before={"schema_version": 1, "certifiable": True},
        clock=lambda: "2026-07-14T02:00:00+08:00",
    )
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id=release_payload["release_id"],
        release_root=plan.release_manifest.path.parent,
        release_manifest_sha256=plan.release_manifest.sha256,
    )
    transaction.advance("v3_active")
    return PreparedRollbackExecutor(
        plan,
        token_file=fixture.token,
        snapshot_collector=machine.probe,
        runner=machine.run,
        readiness_probe=lambda _urls: (True, {}),
        reconciliation_probe=_fake_reconciliation,
        runtime_state_handoff=_fake_runtime_state_handoff,
        # Rollback remains available after the original cutover report's
        # fifteen-minute freshness window.
        clock=lambda: datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        uid=501,
    )
def test_execute_orders_stop_zero_atomic_install_start_and_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)

    report = _executor(fixture, machine).execute()

    assert report["ok"] is True
    assert report["mutation_performed"] is True
    assert not fixture.token.exists()
    assert machine.state == "v3"
    assert machine.commands == [
        *[
            ("launchctl", "bootout", f"gui/501/{label}")
            for label in REQUIRED_V2_APPLICATION_LABELS
        ],
        (
            "launchctl",
            "bootstrap",
            "gui/501",
            str(fixture.install / "com.magi.v3.control.plist"),
        ),
        (
            "launchctl",
            "bootstrap",
            "gui/501",
            str(fixture.install / "com.magi.v3.gateway.plist"),
        ),
        (
            "launchctl",
            "bootstrap",
            "gui/501",
            str(fixture.install / "com.magi.v3.supervisor.plist"),
        ),
    ]
    assert {path.name for path in fixture.install.glob("*.plist")} == {
        *(f"{label}.plist" for label in REQUIRED_V2_APPLICATION_LABELS),
        "com.magi.v3.control.plist",
        "com.magi.v3.gateway.plist",
        "com.magi.v3.supervisor.plist",
    }
    deploy_manifest = json.loads(
        (fixture.staging / deploy.DEPLOY_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    ownership_target = Path(deploy_manifest["ownership_manifest"])
    assert ownership_target.is_file() and not ownership_target.is_symlink()
    assert _sha256(ownership_target) == deploy_manifest["ownership_manifest_sha256"]
    assert ownership_target.stat().st_ino != (
        fixture.staging / deploy.OWNERSHIP_MANIFEST_NAME
    ).stat().st_ino
    gateway_plist = plistlib.loads(
        (fixture.install / "com.magi.v3.gateway.plist").read_bytes()
    )
    gateway_env = gateway_plist["EnvironmentVariables"]
    assert gateway_env["MAGI_V3_OWNERSHIP_MANIFEST"] == str(ownership_target)
    assert (
        gateway_env["MAGI_V3_OWNERSHIP_MANIFEST_SHA256"]
        == deploy_manifest["ownership_manifest_sha256"]
    )
    actions = [event["action"] for event in report["events"]]
    assert "install_ownership_manifest" in actions
    assert actions.index("pdf_namer_handoff") < actions.index("install_v3")
    assert actions.index("install_v3") > actions.index("verify_ownership")
    assert actions.index("verify_readiness") < max(
        index for index, action in enumerate(actions) if action == "activation_transaction"
    )


def test_interrupted_activation_journal_auto_restores_v2_without_consuming_new_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    deploy_manifest = json.loads(
        (fixture.staging / deploy.DEPLOY_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    release_manifest = Path(deploy_manifest["release_manifest"])
    release_root = release_manifest.parent
    release = json.loads(release_manifest.read_text(encoding="utf-8"))
    transaction = ActivationTransaction.begin(
        state_parent=Path(deploy_manifest["runtime_root"]).parent,
        plan_sha256=fixture.plan_sha256,
        release_id=release["release_id"],
        release_root=release_root,
        release_manifest_sha256=_sha256(release_manifest),
        reconciliation_before={
            "schema_version": 1,
            "certifiable": True,
            "committed_id_hashes": [],
            "sent_outbox_id_hashes": [],
            "duplicate_committed_jobs": 0,
            "duplicate_sent_outbox": 0,
        },
        clock=lambda: NOW.isoformat(),
    )
    transaction.advance("v2_zero")
    machine = FakeMachine(fixture, state="zero")

    report = _executor(fixture, machine).execute()

    assert report["status"] == "v2_restored"
    assert report["rollback_ok"] is True
    assert machine.state == "v2"
    assert fixture.token.exists()
    assert ActivationTransaction.resume(
        state_parent=Path(deploy_manifest["runtime_root"]).parent
    ).document()["phase"] == "complete"


def test_pdf_namer_destination_drift_is_rejected_before_v2_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)
    source_before = (fixture.pdf_source / "training_data.json").read_bytes()
    target = fixture.pdf_destination / "training_data.json"
    target.write_text("[]", encoding="utf-8")
    target.chmod(0o600)

    with pytest.raises(CutoverError, match="precopy evidence"):
        _executor(fixture, machine)

    assert machine.state == "v2"
    assert machine.commands == []
    assert not list(fixture.install.glob("com.magi.v3.*.plist"))
    assert (fixture.pdf_source / "training_data.json").read_bytes() == source_before


@pytest.mark.parametrize("race_target", ["ownership", "launchagent"])
def test_no_clobber_publish_race_preserves_created_target_and_rolls_back_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race_target: str,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)
    deploy_manifest = json.loads(
        (fixture.staging / deploy.DEPLOY_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    raced_path = (
        Path(deploy_manifest["ownership_manifest"])
        if race_target == "ownership"
        else fixture.install / "com.magi.v3.control.plist"
    )
    sentinel = f"race-created-{race_target}".encode("utf-8")
    original_link = os.link
    injected = False

    def race_link(source, destination, *, follow_symlinks=True):
        nonlocal injected
        target = Path(destination)
        if not injected and target == raced_path:
            injected = True
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(sentinel)
        return original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(mutation_module.os, "link", race_link)

    report = _executor(fixture, machine).execute()

    assert injected is True
    assert report["ok"] is False
    assert report["rollback_ok"] is True
    assert machine.state == "v2"
    assert raced_path.read_bytes() == sentinel
    assert not any(
        command[1] == "bootstrap" and "com.magi.v3" in command[-1]
        for command in machine.commands
    )


def test_cutover_readiness_builds_gateway_from_installed_hash_bound_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)
    plan = load_prepared_plan(
        fixture.plan,
        fixture.plan_sha256,
        canonical_launchagents_directory=fixture.install,
    )
    built: list[Path] = []

    def readiness(_urls):
        gateway_plist = plistlib.loads(
            (fixture.install / "com.magi.v3.gateway.plist").read_bytes()
        )
        env = gateway_plist["EnvironmentVariables"]
        ownership_path = Path(env["MAGI_V3_OWNERSHIP_MANIFEST"])
        gateway = build_gateway(
            env,
            service_manifest_path=ROOT / "config" / "v3_service_manifest.json",
            app_factories={"main_http": lambda: object(), "tools_http": lambda: object()},
            server_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("readiness composition must not open sockets")
            ),
            role_guard_factory=lambda *_args: SimpleNamespace(
                acquired=False, acquire=lambda: None, release=lambda: None
            ),
            control_owner_factory=lambda *_args: lambda: True,
            runtime=SimpleNamespace(),
        )
        assert gateway.ownership.manifest_path == ownership_path
        built.append(ownership_path)
        return True, {"ownership_manifest": str(ownership_path)}

    report = PreparedCutoverExecutor(
        plan,
        token_file=fixture.token,
        snapshot_collector=machine.probe,
        runner=machine.run,
        readiness_probe=readiness,
        laf_dedup_handoff=_fake_laf_handoff,
        reconciliation_probe=_fake_reconciliation,
        runtime_state_handoff=_fake_runtime_state_handoff,
        clock=lambda: NOW,
        uid=501,
    ).execute()

    assert report["ok"] is True
    assert len(built) == 1 and built[0].is_file()


def test_cutover_rollback_never_removes_hash_drifted_installed_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)
    plan = load_prepared_plan(
        fixture.plan,
        fixture.plan_sha256,
        canonical_launchagents_directory=fixture.install,
    )
    ownership_target: Path | None = None

    def tamper_then_fail(_urls):
        nonlocal ownership_target
        deploy_manifest = json.loads(
            (fixture.staging / deploy.DEPLOY_MANIFEST_NAME).read_text(encoding="utf-8")
        )
        ownership_target = Path(deploy_manifest["ownership_manifest"])
        ownership_target.write_text("{\"tampered\":true}\n", encoding="utf-8")
        return False, {"injected": "ownership drift"}

    report = PreparedCutoverExecutor(
        plan,
        token_file=fixture.token,
        snapshot_collector=machine.probe,
        runner=machine.run,
        readiness_probe=tamper_then_fail,
        laf_dedup_handoff=_fake_laf_handoff,
        reconciliation_probe=_fake_reconciliation,
        # This test certifies the ownership-manifest rollback guard.  Keep the
        # earlier runtime-state handoff explicitly isolated so a previously
        # imported, environment-bound V2 compatibility module cannot make the
        # test stop before the ownership manifest is installed and tampered.
        runtime_state_handoff=_fake_runtime_state_handoff,
        clock=lambda: NOW,
        uid=501,
    ).execute()

    assert report["ok"] is False
    assert report["status"] == "blocked"
    assert report["rollback_ok"] is False
    assert ownership_target is not None and ownership_target.is_file()
    assert json.loads(ownership_target.read_text()) == {"tampered": True}


def test_laf_handoff_pipeline_is_snapshot_verify_dry_run_apply_then_dual_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    plan = load_prepared_plan(
        fixture.plan,
        fixture.plan_sha256,
        canonical_launchagents_directory=fixture.install,
    )
    assert plan.laf_dedup_handoff is not None
    calls: list[str] = []

    def snapshot(paths, output):
        assert tuple(paths) == (fixture.laf_source.resolve(),)
        assert output == fixture.laf_manifest.resolve()
        calls.append("snapshot")
        return {"status": "snapshot_created", "manifest_sha256": "a" * 64, "record_count": 1}

    manifest = {"record_count": 1, "records_sha256": "b" * 64, "records": ["id"]}

    verification_count = 0

    def verify_manifest(output, digest):
        nonlocal verification_count
        assert output == fixture.laf_manifest.resolve()
        assert digest == "a" * 64
        verification_count += 1
        calls.append("verify" if verification_count == 1 else "source_reverify")
        return manifest

    connection = SimpleNamespace(close=lambda: calls.append("close"))

    def connect(env_file, *, expected_sha256):
        assert env_file == fixture.db_env_file.resolve()
        assert expected_sha256 == _sha256(fixture.db_env_file)
        calls.append("connect")
        return connection

    store = object()

    def import_db(actual_manifest, actual_store, *, apply):
        assert actual_manifest is manifest and actual_store is store
        calls.append("apply" if apply else "db_dry_run")
        return {
            "status": "imported" if apply else "dry_run",
            "transaction_committed": apply,
            "mutation_performed": apply,
        }

    def verify_db(actual_manifest, actual_store):
        assert actual_manifest is manifest and actual_store is store
        calls.append("dual_table_verify")
        return {
            "status": "dual_store_verified",
            "record_count": 1,
            "laf_email_records_verified": 1,
            "dedup_registry_verified": 1,
            "mutation_performed": False,
        }

    report = execute_laf_dedup_handoff(
        plan.laf_dedup_handoff,
        manifest_creator=snapshot,
        manifest_loader=verify_manifest,
        connection_factory=connect,
        store_factory=lambda actual: store if actual is connection else None,
        importer=import_db,
        verifier=verify_db,
    )

    assert calls == [
        "snapshot",
        "verify",
        "connect",
        "db_dry_run",
        "apply",
        "dual_table_verify",
        "source_reverify",
        "close",
    ]
    assert report["dual_table_verified"] is True
    assert "OSC_DB_PASSWORD" not in json.dumps(report)


def test_laf_handoff_runs_only_after_zero_and_failure_restores_v2_without_secret_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)
    observed: list[str] = []

    def fail_at_zero(_plan):
        assert machine.state == "zero"
        assert not list(fixture.install.glob("com.magi.v3.*.plist"))
        observed.append("zero_before_laf")
        raise RuntimeError("OSC_DB_PASSWORD=must-not-leak")

    report = _executor(
        fixture,
        machine,
        laf_dedup_handoff=fail_at_zero,
    ).execute()

    assert observed == ["zero_before_laf"]
    assert report["ok"] is False
    assert report["rollback_ok"] is True
    assert machine.state == "v2"
    assert not list(fixture.install.glob("com.magi.v3.*.plist"))
    serialized = json.dumps(report)
    assert "must-not-leak" not in serialized
    assert "LAF dedup compatibility handoff failed closed" in report["error"]


def test_v2_respawn_after_laf_handoff_is_blocked_before_install_and_v2_remains_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)

    def handoff_then_respawn(plan):
        report = _fake_laf_handoff(plan)
        machine.state = "v2"
        return report

    report = _executor(
        fixture,
        machine,
        laf_dedup_handoff=handoff_then_respawn,
    ).execute()

    assert report["ok"] is False
    assert report["rollback_ok"] is True
    assert machine.state == "v2"
    assert machine.commands == [
        ("launchctl", "bootout", f"gui/501/{label}")
        for label in REQUIRED_V2_APPLICATION_LABELS
    ]
    assert not list(fixture.install.glob("com.magi.v3.*.plist"))
    zero_checks = [
        event
        for event in report["events"]
        if event["action"] == "verify_ownership" and event["expected"] == "zero"
    ]
    assert len(zero_checks) == 2
    assert zero_checks[-1]["assessment"]["go"] is False


def test_source_drift_after_dual_table_verify_fails_before_handoff_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    plan = load_prepared_plan(
        fixture.plan,
        fixture.plan_sha256,
        canonical_launchagents_directory=fixture.install,
    )
    assert plan.laf_dedup_handoff is not None
    connection = SimpleNamespace(close=lambda: None)

    def import_db(manifest, _store, *, apply):
        return {
            "status": "imported" if apply else "dry_run",
            "transaction_committed": apply,
            "mutation_performed": apply,
            "record_count": manifest["record_count"],
        }

    def verify_then_drift(manifest, _store):
        fixture.laf_source.write_text(
            json.dumps(["gmail-message-test", "gmail-message-late"]),
            encoding="utf-8",
        )
        return {
            "status": "dual_store_verified",
            "record_count": manifest["record_count"],
            "laf_email_records_verified": manifest["record_count"],
            "dedup_registry_verified": manifest["record_count"],
            "mutation_performed": False,
        }

    with pytest.raises(LAFDedupBlocked, match="changed after the snapshot"):
        execute_laf_dedup_handoff(
            plan.laf_dedup_handoff,
            connection_factory=lambda _path, **_kwargs: connection,
            store_factory=lambda _connection: object(),
            importer=import_db,
            verifier=verify_then_drift,
        )


def test_static_gate_rejects_env_or_manifest_output_symlink_swap_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_fixture = _prepared_fixture(tmp_path / "env", monkeypatch)
    env_machine = FakeMachine(env_fixture)
    env_executor = _executor(env_fixture, env_machine)
    replacement = env_fixture.db_env_file.with_name("replacement.env")
    replacement.write_bytes(env_fixture.db_env_file.read_bytes())
    replacement.chmod(0o600)
    env_fixture.db_env_file.unlink()
    env_fixture.db_env_file.symlink_to(replacement)

    env_report = env_executor.execute()

    assert env_report["mutation_performed"] is False
    assert env_machine.commands == []
    assert env_fixture.token.exists()

    output_fixture = _prepared_fixture(tmp_path / "output", monkeypatch)
    output_machine = FakeMachine(output_fixture)
    output_executor = _executor(output_fixture, output_machine)
    moved_parent = output_fixture.laf_manifest.parent.with_name("handoff-real")
    output_fixture.laf_manifest.parent.rename(moved_parent)
    output_fixture.laf_manifest.parent.symlink_to(moved_parent, target_is_directory=True)

    output_report = output_executor.execute()

    assert output_report["mutation_performed"] is False
    assert output_machine.commands == []
    assert output_fixture.token.exists()


def test_post_handoff_output_parent_swap_is_blocked_and_restores_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)

    def handoff_then_swap_parent(plan):
        report = _fake_laf_handoff(plan)
        original_parent = plan.manifest_output.parent
        moved_parent = original_parent.with_name("handoff-moved")
        original_parent.rename(moved_parent)
        original_parent.symlink_to(moved_parent, target_is_directory=True)
        return report

    report = _executor(
        fixture,
        machine,
        laf_dedup_handoff=handoff_then_swap_parent,
    ).execute()

    assert report["ok"] is False
    assert report["rollback_ok"] is True
    assert machine.state == "v2"
    assert machine.commands[-1] == (
        "launchctl",
        "bootstrap",
        "gui/501",
        str(fixture.install / f"{REQUIRED_V2_APPLICATION_LABELS[-1]}.plist"),
    )
    assert not list(fixture.install.glob("com.magi.v3.*.plist"))


def test_readiness_or_partial_start_failure_stops_v3_removes_plists_and_restores_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)

    report = _executor(fixture, machine, readiness=False).execute()

    assert report["ok"] is False
    assert report["status"] == "rolled_back"
    assert report["rollback_ok"] is True
    assert machine.state == "v2"
    assert not list(fixture.install.glob("com.magi.v3.*.plist"))
    assert machine.commands[-1] == (
        "launchctl",
        "bootstrap",
        "gui/501",
        str(fixture.install / f"{REQUIRED_V2_APPLICATION_LABELS[-1]}.plist"),
    )
    v3_bootouts = [
        command
        for command in machine.commands
        if command[1] == "bootout" and "/com.magi.v3." in command[-1]
    ]
    assert [command[-1] for command in v3_bootouts] == [
        "gui/501/com.magi.v3.supervisor",
        "gui/501/com.magi.v3.gateway",
        "gui/501/com.magi.v3.control",
    ]


def test_residual_owner_blocks_v3_start_and_hash_drift_rolls_back_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    residual = FakeMachine(fixture)
    residual.leave_v2_residual = True

    residual_report = _executor(fixture, residual).execute()

    assert residual_report["ok"] is False
    assert residual_report["rollback_ok"] is True
    assert residual.commands == [
        ("launchctl", "bootout", f"gui/501/{label}")
        for label in REQUIRED_V2_APPLICATION_LABELS
    ]
    assert not list(fixture.install.glob("com.magi.v3.*.plist"))

    # A second prepared fixture demonstrates drift after V2 has stopped.
    drift_fixture = _prepared_fixture(tmp_path / "drift", monkeypatch)
    drift_machine = FakeMachine(drift_fixture)
    original_probe = drift_machine.probe
    changed = False

    def drift_after_stop() -> Snapshot:
        nonlocal changed
        snapshot = original_probe()
        if drift_machine.state == "zero" and not changed:
            changed = True
            target = drift_fixture.staging / "launchagents" / "com.magi.v3.control.plist"
            target.write_bytes(target.read_bytes() + b"drift")
        return snapshot

    executor = _executor(drift_fixture, drift_machine)
    executor.snapshot_collector = drift_after_stop
    drift_report = executor.execute()

    assert drift_report["ok"] is False
    assert drift_report["rollback_ok"] is True
    assert "drift detected" in drift_report["error"]
    assert drift_machine.state == "v2"
    assert not list(drift_fixture.install.glob("com.magi.v3.*.plist"))


@pytest.mark.parametrize(
    "failure",
    [
        "token_mode",
        "token_hash",
        "artifact_hash",
        "release_extra",
        "release_identity",
        "initial_state",
    ],
)
def test_all_pre_mutation_gates_fail_without_launchctl_or_token_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)
    if failure == "token_mode":
        fixture.token.chmod(0o644)
    elif failure == "token_hash":
        fixture.token.write_text("wrong-token\n", encoding="utf-8")
        fixture.token.chmod(0o600)
    elif failure == "artifact_hash":
        target = fixture.staging / "launchagents" / "com.magi.v3.gateway.plist"
        target.write_bytes(target.read_bytes() + b"tamper")
    elif failure == "release_extra":
        plan = json.loads(fixture.plan.read_text(encoding="utf-8"))
        release = Path(plan["release_manifest"]["path"]).parent
        release.chmod(0o755)
        _write(release / "unexpected.pyc", "not immutable\n")
    elif failure == "release_identity":
        plan = json.loads(fixture.plan.read_text(encoding="utf-8"))
        report_path = Path(plan["pre_cutover_report"]["path"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["release_sha"] = "a" * 40
        _write_json(report_path, report)
        plan["pre_cutover_report"] = _binding(report_path)
        _write_json(fixture.plan, plan)
        fixture.plan_sha256 = _sha256(fixture.plan)
    else:
        machine.state = "mixed"

    report = _executor(fixture, machine).execute()

    assert report["ok"] is False
    assert report["mutation_performed"] is False
    assert machine.commands == []
    assert fixture.token.exists()
    assert not list(fixture.install.glob("com.magi.v3.*.plist"))


def test_partial_v3_bootstrap_failure_rolls_back_only_started_v3_then_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture)
    machine.fail_label = "com.magi.v3.gateway.plist"

    report = _executor(fixture, machine).execute()

    assert report["ok"] is False
    assert report["rollback_ok"] is True
    assert machine.state == "v2"
    assert ("launchctl", "bootout", "gui/501/com.magi.v3.control") in machine.commands
    assert ("launchctl", "bootout", "gui/501/com.magi.v3.gateway") not in machine.commands
    assert machine.commands[-1][-1] == str(
        fixture.install / f"{REQUIRED_V2_APPLICATION_LABELS[-1]}.plist"
    )


def test_no_go_or_outside_window_never_consumes_token_or_calls_launchctl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    plan_payload = json.loads(fixture.plan.read_text(encoding="utf-8"))
    report_path = Path(plan_payload["pre_cutover_report"]["path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["decision"] = "NO_GO"
    _write_json(report_path, report)
    plan_payload["pre_cutover_report"] = _binding(report_path)
    _write_json(fixture.plan, plan_payload)
    fixture.plan_sha256 = _sha256(fixture.plan)
    machine = FakeMachine(fixture)

    no_go = _executor(fixture, machine).execute()

    assert no_go["ok"] is False
    assert "GO" in no_go["error"]
    assert machine.commands == []
    assert fixture.token.exists()

    fresh = _prepared_fixture(tmp_path / "outside", monkeypatch)
    outside_machine = FakeMachine(fresh)
    executor = _executor(fresh, outside_machine)
    executor.clock = lambda: datetime(2026, 7, 13, 21, 0, tzinfo=timezone.utc)
    outside = executor.execute()

    assert outside["ok"] is False
    assert "release-bound effective cutover window" in outside["error"]
    assert outside_machine.commands == []
    assert fresh.token.exists()


def test_plan_hash_and_explicit_v2_label_plan_are_mandatory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    with pytest.raises(CutoverError, match="plan SHA-256 mismatch"):
        load_prepared_plan(
            fixture.plan,
            "0" * 64,
            canonical_launchagents_directory=fixture.install,
        )

    payload = json.loads(fixture.plan.read_text(encoding="utf-8"))
    payload["v2_launchagents"] = []
    _write_json(fixture.plan, payload)
    with pytest.raises(CutoverError, match="explicit hash-bound V2"):
        load_prepared_plan(
            fixture.plan,
            _sha256(fixture.plan),
            canonical_launchagents_directory=fixture.install,
        )

    rollback = _prepared_fixture(tmp_path / "rollback-laf", monkeypatch)
    rollback_payload = json.loads(rollback.plan.read_text(encoding="utf-8"))
    rollback_payload["operation"] = "v3_to_v2_rollback"
    _write_json(rollback.plan, rollback_payload)
    with pytest.raises(CutoverError, match="rollback plan must not contain"):
        load_prepared_plan(
            rollback.plan,
            _sha256(rollback.plan),
            canonical_launchagents_directory=rollback.install,
        )


@pytest.mark.parametrize("fault", ("missing", "extra", "set_hash", "plist_drift"))
def test_exact_v2_application_set_rejects_missing_extra_hash_and_plist_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    payload = json.loads(fixture.plan.read_text(encoding="utf-8"))
    if fault == "missing":
        payload["v2_launchagents"].pop()
    elif fault == "extra":
        label = "com.magi.unreviewed-extra"
        plist = fixture.install / f"{label}.plist"
        plist.write_bytes(plistlib.dumps({"Label": label}))
        payload["v2_launchagents"].append(
            {
                "label": label,
                "plist": _binding(plist),
                "initial_launchd": _initial_launchd(label),
                "keepalive_required_running": False,
            }
        )
    elif fault == "set_hash":
        payload["v2_application_set_sha256"] = "0" * 64
    else:
        fixture.v2_plist.write_bytes(fixture.v2_plist.read_bytes() + b"drift")
    if fault != "plist_drift":
        _write_json(fixture.plan, payload)

    match = "SHA-256 mismatch" if fault == "plist_drift" else "application launchagent set|set hash"
    with pytest.raises(CutoverError, match=match):
        load_prepared_plan(
            fixture.plan,
            _sha256(fixture.plan),
            canonical_launchagents_directory=fixture.install,
        )


def test_exact_initial_state_restores_six_loaded_and_preserves_nine_unloaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unloaded = frozenset(
        {
            "com.magi.insight-sync",
            "com.magi.laf-nightly-audit",
            "com.magi.mlx-mtp",
            "com.magi.nightly-health-report",
            "com.magi.obsidian-ingest",
            "com.magi.pdf-namer-nightly",
            "com.magi.purge-persona-memories",
            "com.magi.reprocess-insights",
            "com.magi.weekend-resummary",
        }
    )
    no_pid = frozenset({"com.magi.log-rotate", "com.magi.omlx-restore"})
    keepalive = frozenset(
        {
            "com.magi.daemon",
            "com.magi.menubar",
            "com.magi.paperclip-share-gateway",
            "com.magi.paperclip-share-tunnel",
        }
    )
    fixture = _prepared_fixture(
        tmp_path,
        monkeypatch,
        initial_unloaded=unloaded,
        loaded_without_pid=no_pid,
        keepalive_labels=keepalive,
    )
    machine = FakeMachine(fixture)
    report = _executor(fixture, machine, readiness=False).execute()

    assert report["ok"] is False
    assert report["rollback_ok"] is True
    loaded = [
        label for label in REQUIRED_V2_APPLICATION_LABELS if label not in unloaded
    ]
    assert [
        command[-1].removeprefix("gui/501/")
        for command in machine.commands[: len(loaded)]
    ] == loaded
    restored = [
        Path(command[-1]).stem
        for command in machine.commands
        if command[1] == "bootstrap"
        and Path(command[-1]).stem in REQUIRED_V2_APPLICATION_LABELS
    ]
    assert restored == loaded
    assert all(
        machine.initial_launchd[label]["loaded"] is (label not in unloaded)
        for label in REQUIRED_V2_APPLICATION_LABELS
    )


def test_keepalive_loaded_without_pid_and_initial_state_drift_fail_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keepalive = frozenset({"com.magi.daemon"})
    no_pid = frozenset({"com.magi.daemon"})
    fixture = _prepared_fixture(
        tmp_path / "keepalive",
        monkeypatch,
        loaded_without_pid=no_pid,
        keepalive_labels=keepalive,
    )
    machine = FakeMachine(fixture)
    report = _executor(fixture, machine).execute()
    assert report["ok"] is False
    assert report["mutation_performed"] is False
    assert machine.commands == []
    assert fixture.token.exists()

    unloaded_label = "com.magi.insight-sync"
    drift = _prepared_fixture(
        tmp_path / "state-drift",
        monkeypatch,
        initial_unloaded=frozenset({unloaded_label}),
    )
    drift_machine = FakeMachine(drift)
    drift_machine.launchd_overrides[unloaded_label] = {
        "loaded": True,
        "state": "running",
        "pid": 999,
    }
    drift_report = _executor(drift, drift_machine).execute()
    assert drift_report["ok"] is False
    assert drift_report["mutation_performed"] is False
    assert drift_machine.commands == []


def test_raw_initial_launchctl_receipt_tamper_and_unknown_missing_code_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "com.magi.insight-sync"
    fixture = _prepared_fixture(
        tmp_path,
        monkeypatch,
        initial_unloaded=frozenset({label}),
    )
    payload = json.loads(fixture.plan.read_text(encoding="utf-8"))
    row = next(item for item in payload["v2_launchagents"] if item["label"] == label)
    receipt = row["initial_launchd"]["launchctl_receipt"]
    receipt["returncode"] = 1
    row["initial_launchd"]["launchctl_receipt_sha256"] = hashlib.sha256(
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    _write_json(fixture.plan, payload)
    with pytest.raises(CutoverError, match="known-missing 113"):
        load_prepared_plan(
            fixture.plan,
            _sha256(fixture.plan),
            canonical_launchagents_directory=fixture.install,
        )

    receipt["returncode"] = 113
    receipt["stderr"] = "tampered"
    _write_json(fixture.plan, payload)
    with pytest.raises(CutoverError, match="raw launchctl receipt drifted"):
        load_prepared_plan(
            fixture.plan,
            _sha256(fixture.plan),
            canonical_launchagents_directory=fixture.install,
        )


def test_independent_hash_bound_rollback_stops_v3_then_starts_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture, state="v3")

    report = _rollback_executor(fixture, machine).execute()

    assert report["ok"] is True
    assert report["status"] == "rollback_complete"
    assert machine.state == "v2"
    assert not fixture.token.exists()
    assert [command[-1] for command in machine.commands[:3]] == [
        "gui/501/com.magi.v3.supervisor",
        "gui/501/com.magi.v3.gateway",
        "gui/501/com.magi.v3.control",
    ]
    assert machine.commands[-1][-1] == str(
        fixture.install / f"{REQUIRED_V2_APPLICATION_LABELS[-1]}.plist"
    )


def test_failed_independent_rollback_restores_v3_and_prearm_drift_is_inert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_path, monkeypatch)
    machine = FakeMachine(fixture, state="v3")
    machine.fail_label = "com.magi.daemon.plist"

    report = _rollback_executor(fixture, machine).execute()

    assert report["ok"] is False
    assert report["status"] == "v3_restored"
    assert report["recovery_ok"] is True
    assert machine.state == "v3"

    drift = _prepared_fixture(tmp_path / "drift", monkeypatch)
    drift_machine = FakeMachine(drift, state="v3")
    executor = _rollback_executor(drift, drift_machine)
    target = drift.install / "com.magi.v3.gateway.plist"
    target.write_bytes(target.read_bytes() + b"drift")
    blocked = executor.execute()
    assert blocked["mutation_performed"] is False
    assert drift_machine.commands == []
    assert drift.token.exists()
