from __future__ import annotations

import hashlib
import json
import plistlib
import subprocess
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from scripts import v3_deploy_prepare as deploy_prepare
from scripts.v3_cutover import probe as cutover_probe
from scripts.v3_cutover.core import Owner, Snapshot
from scripts.v3_validation.isolated_live_execute import (
    DEPLOYMENT_MODE,
    OFFLINE_MACHINE_EVIDENCE,
    IsolatedLiveBlocked,
    IsolatedLiveMachine,
    ProbeSpec,
    ValidationRole,
    VerifiedDeployment,
    execute_isolated_live_validation,
    load_isolated_live_plan,
    verify_static_plan,
)
from scripts.v3_validation.isolated_live_macos import (
    LAUNCHCTL,
    V2_READINESS_URLS,
    MacOSIsolatedLiveMachine,
    main as macos_main,
)
from scripts.v3_validation.isolated_resource_window import (
    REQUIRED_STOPPED_LABELS,
)
from scripts.v3_validation.paths import (
    ISOLATED_LIVE_EXECUTION_PLAN_SCHEMA_PATH,
    LIVE_PLAN_SCHEMA_PATH,
)
from scripts.v3_validation.schema import ContractValidationError, load_json, validate_json


COVERAGE = frozenset({"process", "pidfile", "port", "launchd", "ownership"})
INSIDE_WINDOW = datetime(2026, 7, 15, 18, 30, tzinfo=timezone.utc)  # 02:30 Asia/Taipei
OUTSIDE_WINDOW = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)


def test_macos_v2_restore_uses_actual_production_readiness_routes() -> None:
    assert V2_READINESS_URLS == (
        "http://127.0.0.1:5002/readyz",
        "http://127.0.0.1:5003/health",
        "http://127.0.0.1:5014/health",
        "http://127.0.0.1:8088/health",
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes | str, *, mode: int = 0o644) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode() if isinstance(data, str) else data
    path.write_bytes(raw)
    path.chmod(mode)
    return _digest(raw)


def _json(path: Path, payload: Mapping[str, Any], *, mode: int = 0o644) -> str:
    return _write(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        mode=mode,
    )


def _probes() -> list[dict[str, str]]:
    urls = [
        "http://127.0.0.1:5002/livez",
        "http://127.0.0.1:5002/readyz",
        "http://127.0.0.1:5002/validation/ping",
        "http://127.0.0.1:5003/livez",
        "http://127.0.0.1:5003/readyz",
        "http://127.0.0.1:5003/validation/ping",
        "http://127.0.0.1:5003/validation/osc/document-preview",
        "http://127.0.0.1:5003/validation/osc/document-download",
        "http://127.0.0.1:8088/health",
    ]
    return [{"method": "GET", "url": url} for url in urls]


@dataclass
class PreparedFixture:
    plan: Path
    plan_sha256: str
    token: Path
    report: Path
    release_manifest: Path
    deploy_manifest: Path
    marker: Path
    offline: Path
    fixture_sha256: str


def _prepared(tmp_path: Path) -> PreparedFixture:
    release = (tmp_path / "release").resolve()
    deploy = (tmp_path / "deploy").resolve()
    validation = (tmp_path / "validation-inputs").resolve()
    runtime = (tmp_path / "Application Support" / "MAGI" / "runtime" / "MAGI_v3").resolve()

    service_payload = {
        "schema_version": 1,
        "release_mode": "single_active_replacement",
        "deployment_mode": DEPLOYMENT_MODE,
        "services": [
            {
                "id": "main_http",
                "role": "gateway",
                "kind": "wsgi",
                "required": True,
                "port": 5002,
                "factory": "magi_v3.live_validation:create_main_app",
            },
            {
                "id": "tools_http",
                "role": "gateway",
                "kind": "wsgi",
                "required": True,
                "port": 5003,
                "factory": "magi_v3.live_validation:create_tools_app",
            },
            {
                "id": "website_admin",
                "role": "control",
                "kind": "http_server",
                "required": True,
                "port": 8088,
                "factory": "magi_v3.live_validation:create_admin_server",
            },
            {
                "id": "live_validation_probe",
                "role": "supervisor",
                "kind": "process",
                "required": True,
                "argv": ["{python}", "magi_v3/live_validation_probe_service.py"],
            },
        ],
        "host_singletons": [],
        "forbidden_release_processes": ["daemon.py"],
    }
    service = release / "config" / "v3_live_validation_service_manifest.json"
    service_sha = _json(service, service_payload)
    release_files = {"config/v3_live_validation_service_manifest.json": service_sha}
    gate_config = release / "config" / "v3_cutover_gates.json"
    gate_config_sha = _json(
        gate_config,
        {
            "schema_version": 1,
            "timezone": "Asia/Taipei",
            "window": {"start": "02:00", "end": "04:00"},
            "required_evidence": [],
            "promotion_thresholds": {
                "minimum_isolated_live_validation_runs": 3,
                "offline_replay_independent_passes": 7,
                "pre_cutover_validation_window_hours": 24,
                "unresolved_dual_writers": 0,
            },
        },
    )
    release_files["config/v3_cutover_gates.json"] = gate_config_sha
    campaign_config = release / "config" / "v3_validation_campaign.json"
    campaign_config_sha = _json(
        campaign_config,
        {
            "schema_version": 1,
            "offline_campaign": {"required_independent_passes": 7},
            "isolated_live_validation": {
                "required_runs": 3,
                "minimum_reset_minutes": 10,
                "completion_window_hours": 24,
            },
        },
    )
    release_files["config/v3_validation_campaign.json"] = campaign_config_sha
    builder_source = release / "scripts/v3_validation/offline_machine_gate_builder.py"
    release_files["scripts/v3_validation/offline_machine_gate_builder.py"] = _write(
        builder_source, "SCHEMA = 'magi.v3.offline-machine-gate/v1'\n"
    )
    ime_source = release / "scripts/v3_validation/ime_candidate_probe.py"
    release_files["scripts/v3_validation/ime_candidate_probe.py"] = _write(
        ime_source, "def main(): return 0\n"
    )
    for relative in (
        "magi_v3/live_validation.py",
        "magi_v3/live_validation_probe_service.py",
        "magi_v3/control.py",
        "magi_v3/gateway.py",
        "magi_v3/supervisor_service.py",
    ):
        release_files[relative] = _write(release / relative, "def main(): return 0\n")
    executable = release / "bin" / "magi-v3-python"
    release_files["bin/magi-v3-python"] = _write(executable, "#!/bin/sh\nexit 0\n", mode=0o755)
    release_manifest = release / "release-manifest.json"
    release_snapshot_sha = "1" * 64
    release_sha = _json(
        release_manifest,
        {
            "schema_version": 1,
            "release_id": "v3-isolated-test",
            "immutable": True,
            "release_sha256": release_snapshot_sha,
            "source_snapshot_sha256": release_snapshot_sha,
            "files": [
                {"path": relative, "sha256": digest}
                for relative, digest in sorted(release_files.items())
            ],
        },
    )
    _json(
        release / "RELEASE_COMPLETE.json",
        {
            "schema_version": 1,
            "release_id": "v3-isolated-test",
            "manifest": "release-manifest.json",
            "manifest_sha256": release_sha,
        },
    )

    env = validation / "validation.env"
    env_sha = _write(env, "MAGI_V3_VALIDATION_FIXTURE=1\n", mode=0o600)
    cron_source = validation / "cron.json"
    cron_sha = _json(
        cron_source,
        [
            {
                "id": "v3_live_validation_inert",
                "enabled": False,
                "cron": "0 0 31 2 *",
                "command": "@MAGI validation_inert",
            }
        ],
    )
    website = validation / "website"
    admin = website / "admin" / "admin_server.py"
    admin_sha = _write(admin, "class AdminHandler: pass\n")
    fixture = website / "data" / "live-validation-document.txt"
    fixture_sha = _write(fixture, "isolated preview fixture\n")
    laf_config = validation / "laf-config.json"
    laf_config.write_bytes(deploy_prepare.VALIDATION_LAF_CONFIG_BYTES)
    laf_config.chmod(0o600)
    credentials = validation / "credentials.json"
    credentials.write_bytes(deploy_prepare.VALIDATION_GOOGLE_CREDENTIALS_BYTES)
    credentials.chmod(0o600)
    calendar_source = validation / "google-calendar-token.json"
    calendar_source.write_bytes(deploy_prepare.VALIDATION_GOOGLE_CALENDAR_TOKEN_BYTES)
    laf_source = validation / "laf-gmail-token.pickle"
    laf_source.write_bytes(deploy_prepare.VALIDATION_LAF_GMAIL_TOKEN_BYTES)
    file_review_source = validation / "filereview-token.pickle"
    file_review_source.write_bytes(deploy_prepare.VALIDATION_LAF_GMAIL_TOKEN_BYTES)
    ocr_queue = validation / "nas-ocr-queue.db"
    with sqlite3.connect(ocr_queue) as connection:
        connection.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY)")
    secrets_root = runtime / "shared" / "secrets"
    target_tokens = {
        "google_calendar_token_file": secrets_root / "google_calendar_token.json",
        "laf_gmail_token_file": secrets_root / "laf_gmail_token.pickle",
        "file_review_token_file": secrets_root / "filereview_token.pickle",
        "gmail_compose_token_file": secrets_root / "gmail_compose_token.json",
    }

    rendered_cron = deploy / "runtime-inputs" / "cron_jobs.v3.json"
    rendered_cron_sha = _json(
        rendered_cron,
        {"schema_version": 1, "jobs": [], "source_sha256": cron_sha},
        mode=0o600,
    )
    external = {
        "env_file": str(env),
        "env_file_sha256": env_sha,
        "cron_jobs_file": str(rendered_cron),
        "cron_jobs_sha256": rendered_cron_sha,
        "cron_jobs_source_file": str(cron_source),
        "cron_jobs_source_sha256": cron_sha,
        "website_root": str(website),
        "website_admin_sha256": admin_sha,
        "laf_config_file": str(laf_config),
        "laf_config_sha256": hashlib.sha256(laf_config.read_bytes()).hexdigest(),
        "laf_config_mode": "0600",
        "google_credentials_file": str(credentials),
        "google_credentials_sha256": hashlib.sha256(credentials.read_bytes()).hexdigest(),
        "google_credentials_mode": "0600",
        "google_calendar_token_source_file": str(calendar_source),
        "google_calendar_token_source_sha256": hashlib.sha256(calendar_source.read_bytes()).hexdigest(),
        "laf_gmail_token_source_file": str(laf_source),
        "laf_gmail_token_source_sha256": hashlib.sha256(laf_source.read_bytes()).hexdigest(),
        "file_review_token_source_file": str(file_review_source),
        "file_review_token_source_sha256": hashlib.sha256(file_review_source.read_bytes()).hexdigest(),
        **{name: str(path) for name, path in target_tokens.items()},
        "gmail_compose_token_source_file": None,
        "gmail_compose_token_source_sha256": None,
        "optional_degraded_inputs": ["gmail_compose_token"],
        "accounting_credentials_file": str(credentials),
        "accounting_credentials_sha256": hashlib.sha256(credentials.read_bytes()).hexdigest(),
        "accounting_credentials_mode": "0600",
        "accounting_sheets_token_source_file": str(calendar_source),
        "accounting_sheets_token_source_sha256": hashlib.sha256(calendar_source.read_bytes()).hexdigest(),
        "accounting_sheets_token_file": str(secrets_root / "accounting_sheets_token.json"),
        "drive_sync_token_source_file": str(calendar_source),
        "drive_sync_token_source_sha256": hashlib.sha256(calendar_source.read_bytes()).hexdigest(),
        "drive_sync_token_file": str(secrets_root / "drive_sync_token.json"),
        "drive_sync_write_token_source_file": str(calendar_source),
        "drive_sync_write_token_source_sha256": hashlib.sha256(calendar_source.read_bytes()).hexdigest(),
        "drive_sync_write_token_file": str(secrets_root / "drive_sync_write_token.json"),
        "nas_ocr_queue_db_file": str(ocr_queue),
        "nas_ocr_queue_db_mode": f"{ocr_queue.stat().st_mode & 0o777:04o}",
    }
    external.update(deploy_prepare.named_mutable_state_paths(runtime))
    ownership_path = deploy / "ownership" / "ownership-manifest.json"
    ownership_sha = _json(
        ownership_path,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "release_id": "v3-isolated-test",
            "deployment_mode": DEPLOYMENT_MODE,
            "runtime_root": str(runtime),
            "service_manifest": str(service),
            "service_manifest_sha256": service_sha,
        },
    )

    labels = {
        "control": "com.magi.v3.control",
        "gateway": "com.magi.v3.gateway",
        "supervisor": "com.magi.v3.supervisor",
    }
    roles = []
    artifacts = [
        {
            "path": "runtime-inputs/cron_jobs.v3.json",
            "sha256": rendered_cron_sha,
            "size": rendered_cron.stat().st_size,
        },
        {
            "path": "ownership/ownership-manifest.json",
            "sha256": ownership_sha,
            "size": ownership_path.stat().st_size,
        },
    ]
    for role, label in labels.items():
        module = "supervisor_service" if role == "supervisor" else role
        arguments = [str(executable), "-m", f"magi_v3.{module}"]
        role_row = {
            "role": role,
            "label": label,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(release),
            "deployment_mode": DEPLOYMENT_MODE,
            "service_manifest": str(service),
            "service_manifest_sha256": service_sha,
            "runtime_root": str(runtime),
            "release_id": "v3-isolated-test",
            "release_manifest": str(release_manifest),
            "release_manifest_sha256": release_sha,
            "state_dir": str(runtime / "state" / role),
            "log_dir": str(runtime / "logs" / role),
            "pid_file": str(runtime / "pids" / f"{role}.pid"),
            **deploy_prepare.named_mutable_state_paths(runtime),
        }
        roles.append(role_row)
        environment = {
            "MAGI_V3_ROLE": role,
            "MAGI_V3_RELEASE_ID": "v3-isolated-test",
            "MAGI_V3_RELEASE_MANIFEST": str(release_manifest),
            "MAGI_V3_RELEASE_MANIFEST_SHA256": release_sha,
            "MAGI_V3_DEPLOYMENT_MODE": DEPLOYMENT_MODE,
            "MAGI_V3_SERVICE_MANIFEST": str(service),
            "MAGI_V3_SERVICE_MANIFEST_SHA256": service_sha,
            "MAGI_V3_LIVE_VALIDATION": "1",
            "MAGI_V3_EXTERNAL_WRITES_ENABLED": "0",
            "MAGI_V3_NOTIFICATIONS_ENABLED": "0",
            "MAGI_V3_SCHEDULER_ENABLED": "0",
            "MAGI_ENV_FILE": str(env),
            "MAGI_ENV_FILE_SHA256": env_sha,
            "MAGI_CRON_JOBS_FILE": str(rendered_cron),
            "MAGI_CRON_JOBS_SHA256": rendered_cron_sha,
            "MAGI_CRON_JOBS_SOURCE_SHA256": cron_sha,
            "MAGI_WEBSITE_ROOT": str(website),
            "MAGI_LAF_CONFIG_FILE": str(laf_config),
            "MAGI_LAF_CONFIG_SHA256": external["laf_config_sha256"],
            "MAGI_CONFIG_PATH": str(laf_config),
            "MAGI_CONFIG_SHA256": external["laf_config_sha256"],
            "MAGI_CONFIG_MODE": "0600",
            "MAGI_JSON_DIR": str(validation),
            "MAGI_GOOGLE_CREDENTIALS_PATH": str(credentials),
            "MAGI_GOOGLE_CREDENTIALS_SHA256": external["google_credentials_sha256"],
            "MAGI_GOOGLE_CREDENTIALS_MODE": "0600",
            "MAGI_GMAIL_CREDENTIALS_PATH": str(credentials),
            "MAGI_GOOGLE_CALENDAR_TOKEN_PATH": external["google_calendar_token_file"],
            "MAGI_LAF_GMAIL_TOKEN_PATH": external["laf_gmail_token_file"],
            "MAGI_FILE_REVIEW_TOKEN_PATH": external["file_review_token_file"],
            "MAGI_GMAIL_COMPOSE_TOKEN_PATH": external["gmail_compose_token_file"],
            "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH": str(credentials),
            "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_SHA256": external[
                "accounting_credentials_sha256"
            ],
            "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_MODE": "0600",
            "MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN": external[
                "accounting_sheets_token_file"
            ],
            "MAGI_DRIVE_SYNC_CREDENTIALS_PATH": str(credentials),
            "MAGI_DRIVE_SYNC_TOKEN": external["drive_sync_token_file"],
            "MAGI_DRIVE_SYNC_WRITE_TOKEN": external["drive_sync_write_token_file"],
            "MAGI_NAS_OCR_QUEUE_DB_PATH": str(ocr_queue),
            "MAGI_PUBLIC_SOURCE_ROOT_DIR": str(release),
            "OSC_CONFIG_PATH": str(laf_config),
            **{
                env_name: external[binding_name]
                for env_name, (binding_name, _relative) in (
                    deploy_prepare.NAMED_MUTABLE_STATE_BINDINGS.items()
                )
            },
        }
        plist = plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": arguments,
                "WorkingDirectory": str(release),
                "EnvironmentVariables": environment,
                "StandardOutPath": str(runtime / "logs" / role / "stdout.log"),
                "StandardErrorPath": str(runtime / "logs" / role / "stderr.log"),
                "ProcessType": "Background",
                "RunAtLoad": False,
                "KeepAlive": False,
            },
            sort_keys=True,
        )
        plist_path = deploy / "launchagents" / f"{label}.plist"
        plist_sha = _write(plist_path, plist)
        artifacts.append(
            {
                "path": f"launchagents/{label}.plist",
                "sha256": plist_sha,
                "size": plist_path.stat().st_size,
            }
        )

    ownership_sha = _json(
        ownership_path,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "release_id": "v3-isolated-test",
            "deployment_mode": DEPLOYMENT_MODE,
            "runtime_root": str(runtime),
            "service_manifest": str(service),
            "service_manifest_sha256": service_sha,
            "external_inputs": external,
            "roles": roles,
        },
    )
    artifacts[1].update(
        {"sha256": ownership_sha, "size": ownership_path.stat().st_size}
    )

    deploy_manifest = deploy / "deploy-manifest.json"
    deploy_sha = _json(
        deploy_manifest,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "mutation_performed": False,
            "release_id": "v3-isolated-test",
            "release_manifest": str(release_manifest),
            "release_manifest_sha256": release_sha,
            "deployment_mode": DEPLOYMENT_MODE,
            "service_manifest": str(service),
            "service_manifest_sha256": service_sha,
            "validation_input_root": str(validation),
            "runtime_root": str(runtime),
            "ownership_manifest_sha256": ownership_sha,
            "external_inputs": external,
            "roles": roles,
            "artifacts": artifacts,
        },
    )
    marker = deploy / "DEPLOY_PREPARED.json"
    marker_sha = _json(
        marker,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "ready_to_install": True,
            "mutation_performed": False,
            "release_id": "v3-isolated-test",
            "deployment_mode": DEPLOYMENT_MODE,
            "release_manifest_sha256": release_sha,
            "ownership_manifest_sha256": ownership_sha,
            "manifest": "deploy-manifest.json",
            "manifest_sha256": deploy_sha,
        },
    )
    offline = tmp_path / "offline-gate.json"
    offline_evidence = (tmp_path / "offline-evidence").resolve()
    context = {
        "campaign_id": "campaign-isolated-test",
        "release_sha": release_snapshot_sha,
        "hardware_id": "test-mac-hardware",
        "gate_config_sha256": gate_config_sha,
    }
    generated_at = datetime.now(timezone.utc)
    evidence_records: dict[str, Any] = {}
    for evidence_id in sorted(OFFLINE_MACHINE_EVIDENCE):
        producer_report = offline_evidence / "reports" / f"{evidence_id}.json"
        producer_sha = _json(
            producer_report,
            {
                "schema_version": 1,
                "evidence_id": evidence_id,
                "status": "passed",
                **context,
            },
        )
        envelope = offline_evidence / f"{evidence_id}.json"
        envelope_sha = _json(
            envelope,
            {
                "schema_version": 1,
                "evidence_id": evidence_id,
                "status": "passed",
                "generated_at": generated_at.isoformat(),
                **context,
                "artifacts": [
                    {
                        "role": "producer_report",
                        "path": f"reports/{evidence_id}.json",
                        "sha256": producer_sha,
                    }
                ],
            },
        )
        evidence_records[evidence_id] = {
            "status": "passed",
            "passed": True,
            "envelope": {"path": str(envelope), "sha256": envelope_sha},
            "artifacts": [
                {
                    "role": "producer_report",
                    "path": str(producer_report),
                    "sha256": producer_sha,
                }
            ],
            "errors": [],
        }
    compiler_summary = offline_evidence / "evidence-compile-summary.json"
    compiler_sha = _json(
        compiler_summary,
        {
            "schema_version": 1,
            "generated_at": generated_at.isoformat(),
            **context,
            "normalizer": "scripts.v3_evidence_compiler",
            "service_start_performed": False,
            "live_state_accessed": False,
            "emitted": {name: "passed" for name in OFFLINE_MACHINE_EVIDENCE},
            "decision": "EVIDENCE_COMPLETE",
        },
    )
    release_gate_report = (tmp_path / "offline-gate.json.release-gate.json").resolve()
    release_gate_sha = _json(
        release_gate_report,
        {
            "schema_version": 1,
            "generated_at": generated_at.isoformat(),
            "decision": "NO_GO",
            "fail_closed": True,
            "expected_context": context,
            "required_count": 28,
            "passed": sorted(OFFLINE_MACHINE_EVIDENCE),
            "missing": ["human_go_approval_recorded"],
            "failed": [],
            "invalid": {},
        },
    )
    offline_sha = _json(
        offline,
        {
            "schema_version": 1,
            "builder_schema": "magi.v3.offline-machine-gate/v1",
            "status": "GO",
            "deployment_mode": DEPLOYMENT_MODE,
            "generated_at": generated_at.isoformat(),
            "valid_until": (generated_at + timedelta(hours=24)).isoformat(),
            **context,
            "release_manifest_sha256": release_sha,
            "deploy_manifest_sha256": deploy_sha,
            "deploy_prepared_marker_sha256": marker_sha,
            "candidate_runtime": {
                "launcher_sha256": release_files["bin/magi-v3-python"],
                "python_runtime_sha256": "2" * 64,
                "builder_source_sha256": release_files[
                    "scripts/v3_validation/offline_machine_gate_builder.py"
                ],
            },
            "source_reports": {
                "compiler_summary": {"path": str(compiler_summary), "sha256": compiler_sha},
                "release_gate_report": {
                    "path": str(release_gate_report),
                    "sha256": release_gate_sha,
                },
            },
            "required_evidence": sorted(OFFLINE_MACHINE_EVIDENCE),
            "evidence": evidence_records,
            "counts": {"required": 19, "passed": 19, "failed": 0, "missing": 0, "invalid": 0},
            "unproven_gaps": [],
            "live_execution_performed": False,
            "launchctl_invoked": False,
        },
    )
    token = (tmp_path / "arm.token").resolve()
    token_value = b"isolated-live-one-time-token"
    _write(token, token_value + b"\n", mode=0o600)
    plan = (tmp_path / "isolated-live-plan.json").resolve()
    plan_sha = _json(
        plan,
        {
            "schema_version": 1,
            "plan_id": "isolated-live-test-001",
            "operation": DEPLOYMENT_MODE,
            "release_manifest": {"path": str(release_manifest), "sha256": release_sha},
            "deploy_manifest": {"path": str(deploy_manifest), "sha256": deploy_sha},
            "deploy_prepared_marker": {"path": str(marker), "sha256": marker_sha},
            "offline_gate_report": {"path": str(offline), "sha256": offline_sha},
            "token_sha256": _digest(token_value),
            "probes": _probes(),
        },
    )
    return PreparedFixture(
        plan=plan,
        plan_sha256=plan_sha,
        token=token,
        report=(tmp_path / "reports" / "isolated-live.json").resolve(),
        release_manifest=release_manifest,
        deploy_manifest=deploy_manifest,
        marker=marker,
        offline=offline,
        fixture_sha256=fixture_sha,
    )


def test_live_plan_schema_matches_the_executor_contract(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    payload = load_json(prepared.plan)
    validate_json(
        payload,
        load_json(ISOLATED_LIVE_EXECUTION_PLAN_SCHEMA_PATH),
        label="isolated LIVE execution plan",
    )
    with pytest.raises(ContractValidationError):
        validate_json(
            payload,
            load_json(LIVE_PLAN_SCHEMA_PATH),
            label="compatibility LIVE validation plan",
        )


@dataclass
class FakeMachine(IsolatedLiveMachine):
    fixture_sha256: str
    state: str = "v2"
    fail_once: set[str] = field(default_factory=set)
    rollback_failure: bool = False
    integrity_fail_once: bool = False
    actions: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    blackout: bool = False
    installed: bool = False

    def _action(self, name: str) -> None:
        self.actions.append(name)
        if name in self.fail_once:
            self.fail_once.remove(name)
            raise RuntimeError(f"injected failure: {name}")

    def activate_maintenance_blackout(self) -> Mapping[str, Any]:
        self._action("activate_blackout")
        self.blackout = True
        return {
            "ok": True,
            "active": True,
            "blocked_priorities": ["P2", "P3", "P4"],
            "portal_write_and_destructive_catchup": False,
        }

    def deactivate_maintenance_blackout(self) -> Mapping[str, Any]:
        self._action("deactivate_blackout")
        self.blackout = False
        return {"ok": True, "active": False}

    def collect_ownership_snapshot(self) -> Snapshot:
        self._action("snapshot")
        owners: tuple[Owner, ...]
        if self.state == "v2":
            owners = (
                Owner("v2", "scheduler", "v2-scheduler", "fake", pid=101),
                Owner("v2", "port", "v2-5002", "fake", pid=101),
            )
        elif self.state == "v3":
            owners = (
                Owner("v3", "scheduler", "v3-control", "fake", pid=201),
                Owner("v3", "port", "v3-5002", "fake", pid=202),
            )
        else:
            owners = ()
        return Snapshot(owners=owners, coverage=COVERAGE, observed_at="2026-07-16T02:30:00+08:00")

    def stop_v2(self) -> Mapping[str, Any]:
        self._action("stop_v2")
        self.state = "zero"
        return {"ok": True}

    def install_validation(self, deployment: VerifiedDeployment) -> Mapping[str, Any]:
        self._action("install_validation")
        self.installed = True
        return {
            "ok": True,
            "deployment_mode": DEPLOYMENT_MODE,
            "ownership_manifest_sha256": deployment.ownership_manifest.sha256,
            "plist_sha256": {role.label: role.plist.sha256 for role in deployment.roles},
        }

    def start_v3_role(self, role: ValidationRole) -> Mapping[str, Any]:
        self._action(f"start_{role.role}")
        self.started.append(role.role)
        self.state = "v3"
        return {"ok": True, "role": role.role, "label": role.label}

    def probe(self, probe: ProbeSpec) -> Mapping[str, Any]:
        path = probe.url.split("127.0.0.1", 1)[1].split("/", 1)[1]
        path = "/" + path
        self._action(f"probe:{path}")
        response: dict[str, Any] = {"ok": True, "status_code": 200, "headers": {}}
        if path == "/validation/ping":
            response["json"] = {"status": "ok", "mode": DEPLOYMENT_MODE}
            response["headers"] = {"X-MAGI-Validation-Mode": DEPLOYMENT_MODE}
        elif path in {"/livez", "/readyz", "/health"}:
            response["json"] = {"status": "ready", "ready": True}
        elif path.endswith("document-preview"):
            response["body_sha256"] = self.fixture_sha256
            response["headers"] = {"Content-Disposition": "inline; filename=test.txt"}
        elif path.endswith("document-download"):
            response["body_sha256"] = self.fixture_sha256
            response["headers"] = {"Content-Disposition": "attachment; filename=test.txt"}
        return response

    def run_native_ime_candidate_probe(
        self, deployment: VerifiedDeployment
    ) -> Mapping[str, Any]:
        self._action("native_ime_probe")
        release = json.loads(
            (deployment.release_root / "release-manifest.json").read_text(encoding="utf-8")
        )
        inventory = {
            row["path"]: row["sha256"]
            for row in release["files"]
            if isinstance(row, dict) and "path" in row and "sha256" in row
        }
        launcher_sha = inventory["bin/magi-v3-python"]
        source_sha = inventory["scripts/v3_validation/ime_candidate_probe.py"]
        evidence = {
            "schema_version": 1,
            "workload": "ime_candidate_window_pressure_probe",
            "probe": "native_mcbopomofo_candidate_window_pressure",
            "status": "passed",
            "observations": [
                {
                    "cycle": 1,
                    "detected": True,
                    "window_count": 1,
                    "preexisting_window_count": 0,
                    "preexisting_window_ids": [],
                    "observed_candidate_windows": [
                        {
                            "owner": "mcbopomofo",
                            "window_id": 9001,
                            "layer": 1,
                            "width": 240,
                            "height": 80,
                        }
                    ],
                    "new_candidate_windows": [
                        {
                            "owner": "mcbopomofo",
                            "window_id": 9001,
                            "layer": 1,
                            "width": 240,
                            "height": 80,
                        }
                    ],
                    "latency_ms": 80.0,
                }
            ],
            "measurements": {
                "cycles_requested": 1,
                "cycles_completed": 1,
                "candidate_windows_detected": 1,
                "candidate_window_failures": 0,
                "pressure_allocated_mb": 256,
                "pressure_touched_bytes": 256 * 1024 * 1024,
                "text_services_healthy": True,
            },
            "unsaved_document_cleanup_performed": True,
            "unsaved_documents_remaining": 0,
            "input_source_restored": True,
            "frontmost_application_restored": True,
            "textedit_state_restored": True,
            "external_write_performed": False,
            "network_access_performed": False,
            "service_start_performed": False,
            "production_port_access_performed": False,
            "launchctl_performed": False,
            "live_magi_state_access_performed": False,
            "temporary_native_ui_performed": True,
        }
        digest = hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return {
            "ok": True,
            "candidate_release_id": deployment.release_id,
            "candidate_launcher_verified": True,
            "candidate_probe_source_verified": True,
            "launcher_sha256": launcher_sha,
            "probe_source_sha256": source_sha,
            "command_receipt": {
                "schema_version": 1,
                "started_at": INSIDE_WINDOW.isoformat(),
                "finished_at": INSIDE_WINDOW.isoformat(),
                "returncode": 0,
                "timed_out": False,
                "launcher_sha256": launcher_sha,
                "probe_source_sha256": source_sha,
                "host_cleanup": {
                    "input_source_restored": True,
                    "frontmost_application_restored": True,
                    "textedit_document_baseline_restored": True,
                    "errors": [],
                },
            },
            "evidence_sha256": digest,
            "evidence": evidence,
        }

    def stop_v3_role(self, role: ValidationRole) -> Mapping[str, Any]:
        self._action(f"stop_{role.role}")
        if role.role in self.started:
            self.started.remove(role.role)
        if not self.started and self.state == "v3":
            self.state = "zero"
        return {"ok": True}

    def remove_validation(self, deployment: VerifiedDeployment) -> Mapping[str, Any]:
        del deployment
        self._action("remove_validation")
        self.installed = False
        return {
            "ok": True,
            "validation_artifacts_removed": True,
            "runtime_ownership_removed": True,
            "remaining_validation_artifacts": 0,
        }

    def restore_v2(self) -> Mapping[str, Any]:
        self._action("restore_v2")
        if self.rollback_failure:
            raise RuntimeError("injected rollback failure")
        self.state = "v2"
        return {"ok": True}

    def verify_v2_readiness_integrity(self) -> Mapping[str, Any]:
        self._action("verify_v2")
        if self.integrity_fail_once:
            self.integrity_fail_once = False
            return {"ok": False, "ready": False, "integrity_ok": False}
        return {"ok": self.state == "v2", "ready": self.state == "v2", "integrity_ok": self.state == "v2"}


def _execute(prepared: PreparedFixture, machine: FakeMachine, *, now: datetime = INSIDE_WINDOW):
    return execute_isolated_live_validation(
        plan_path=prepared.plan,
        plan_sha256=prepared.plan_sha256,
        token_file=prepared.token,
        report_output=prepared.report,
        machine=machine,
        clock=lambda: now,
    )


def test_success_runs_single_active_sequence_and_restores_v2(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    machine = FakeMachine(prepared.fixture_sha256)

    report = _execute(prepared, machine)

    assert report["status"] == "validation_passed_v2_restored"
    assert report["ok"] is True
    assert report["final_cutover"] is False
    assert report["v2_restored"] is True
    assert report["probes_completed"] == report["probes_planned"] == 9
    assert machine.state == "v2"
    assert machine.started == []
    assert machine.installed is False
    assert machine.blackout is False
    assert not prepared.token.exists()
    assert json.loads(prepared.report.read_text(encoding="utf-8")) == report
    assert machine.actions.index("stop_v2") < machine.actions.index("install_validation")
    assert machine.actions.index("start_control") < machine.actions.index("start_gateway")
    assert machine.actions.index("start_gateway") < machine.actions.index("start_supervisor")
    assert machine.actions.index("stop_supervisor") < machine.actions.index("stop_gateway")
    assert machine.actions.index("stop_gateway") < machine.actions.index("stop_control")
    assert machine.actions.index("remove_validation") < machine.actions.index("restore_v2")
    snapshots = [
        event["detail"]["snapshot"]
        for event in report["events"]
        if event["action"] == "ownership_snapshot"
    ]
    assert snapshots and all("snapshot_sha256" in snapshot for snapshot in snapshots)
    assert report["hash_context"]["deploy_manifest_sha256"] == _digest(
        prepared.deploy_manifest.read_bytes()
    )
    assert "isolated-live-one-time-token" not in json.dumps(report)


@pytest.mark.parametrize(
    "failure",
    [
        "activate_blackout",
        "stop_v2",
        "install_validation",
        "start_control",
        "start_gateway",
        "start_supervisor",
        "probe:/readyz",
        "probe:/validation/osc/document-download",
        "stop_supervisor",
        "remove_validation",
    ],
)
def test_any_middle_failure_fails_validation_and_recovers_v2(
    tmp_path: Path,
    failure: str,
) -> None:
    prepared = _prepared(tmp_path)
    machine = FakeMachine(prepared.fixture_sha256, fail_once={failure})

    report = _execute(prepared, machine)

    assert report["ok"] is False
    assert report["final_cutover"] is False
    assert report["status"] == "validation_failed_v2_restored"
    assert report["v2_restored"] is True
    assert machine.state == "v2"
    assert machine.blackout is False


def test_rollback_failure_is_a_hard_block_and_never_claims_cutover(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    machine = FakeMachine(
        prepared.fixture_sha256,
        fail_once={"probe:/readyz"},
        rollback_failure=True,
    )

    report = _execute(prepared, machine)

    assert report["ok"] is False
    assert report["status"] == "blocked_v2_restore_failed"
    assert report["v2_restored"] is False
    assert report["final_cutover"] is False
    assert "rollback failure" in report["rollback_detail"]


def test_partial_v2_state_is_repaired_when_first_integrity_check_fails(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    machine = FakeMachine(
        prepared.fixture_sha256,
        fail_once={"stop_v2"},
        integrity_fail_once=True,
    )

    report = _execute(prepared, machine)

    assert report["status"] == "validation_failed_v2_restored"
    assert report["v2_restored"] is True
    assert machine.actions.count("restore_v2") == 1
    assert machine.actions.count("verify_v2") == 2


def test_token_is_private_hash_bound_and_one_time(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    prepared.token.write_text("wrong-token\n", encoding="utf-8")
    prepared.token.chmod(0o600)
    machine = FakeMachine(prepared.fixture_sha256)

    report = _execute(prepared, machine)

    assert report["status"] == "preflight_blocked"
    assert machine.actions == ["snapshot"]
    assert prepared.token.exists()

    second = _prepared(tmp_path / "second")
    second.token.chmod(0o644)
    report = _execute(second, FakeMachine(second.fixture_sha256))
    assert report["status"] == "preflight_blocked"
    assert second.token.exists()


def test_window_is_hard_taipei_gate_before_token_or_machine_mutation(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    machine = FakeMachine(prepared.fixture_sha256)

    report = _execute(prepared, machine, now=OUTSIDE_WINDOW)

    assert report["status"] == "preflight_blocked"
    assert "outside isolated LIVE validation window" in report["primary_error"]
    assert machine.actions == []
    assert prepared.token.exists()


def test_window_expiry_after_v3_start_forces_cleanup_and_v2_restore(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    machine = FakeMachine(prepared.fixture_sha256)

    def clock() -> datetime:
        return OUTSIDE_WINDOW if "start_control" in machine.actions else INSIDE_WINDOW

    report = execute_isolated_live_validation(
        plan_path=prepared.plan,
        plan_sha256=prepared.plan_sha256,
        token_file=prepared.token,
        report_output=prepared.report,
        machine=machine,
        clock=clock,
    )

    assert report["status"] == "validation_failed_v2_restored"
    assert "outside isolated LIVE validation window" in report["primary_error"]
    assert machine.state == "v2"
    assert machine.started == []


def test_hash_drift_after_v2_stop_is_caught_before_install_and_rolls_back(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    machine = FakeMachine(prepared.fixture_sha256)
    original_stop = machine.stop_v2

    def stop_and_drift() -> Mapping[str, Any]:
        receipt = original_stop()
        prepared.deploy_manifest.write_bytes(prepared.deploy_manifest.read_bytes() + b" ")
        return receipt

    machine.stop_v2 = stop_and_drift  # type: ignore[method-assign]

    report = _execute(prepared, machine)

    assert report["status"] == "validation_failed_v2_restored"
    assert "drift" in report["primary_error"]
    assert "install_validation" not in machine.actions
    assert machine.state == "v2"


def test_existing_report_path_blocks_before_token_or_machine_action(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    prepared.report.parent.mkdir(parents=True)
    prepared.report.write_text("operator-owned\n", encoding="utf-8")
    machine = FakeMachine(prepared.fixture_sha256)

    with pytest.raises(IsolatedLiveBlocked, match="already exists"):
        _execute(prepared, machine)

    assert prepared.token.exists()
    assert machine.actions == []
    assert prepared.report.read_text(encoding="utf-8") == "operator-owned\n"


@pytest.mark.parametrize("artifact", ["release", "deploy", "marker", "offline"])
def test_every_hash_context_drift_blocks_before_token_and_mutation(
    tmp_path: Path,
    artifact: str,
) -> None:
    prepared = _prepared(tmp_path)
    path = {
        "release": prepared.release_manifest,
        "deploy": prepared.deploy_manifest,
        "marker": prepared.marker,
        "offline": prepared.offline,
    }[artifact]
    path.write_bytes(path.read_bytes() + b" ")
    machine = FakeMachine(prepared.fixture_sha256)

    report = _execute(prepared, machine)

    assert report["status"] == "preflight_blocked"
    assert "drift" in report["primary_error"]
    assert machine.actions == []
    assert prepared.token.exists()


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "http://127.0.0.1:5002/validation/ping"),
        ("GET", "http://127.0.0.1:5002/api/cases"),
        ("GET", "http://localhost:5002/livez"),
        ("GET", "http://127.0.0.1:5002/livez?write=1"),
        ("GET", "https://127.0.0.1:5002/livez"),
    ],
)
def test_non_allowlisted_probe_is_rejected_while_loading_plan(
    tmp_path: Path,
    method: str,
    url: str,
) -> None:
    prepared = _prepared(tmp_path)
    payload = json.loads(prepared.plan.read_text(encoding="utf-8"))
    payload["probes"].append({"method": method, "url": url})
    plan_sha = _json(prepared.plan, payload)

    with pytest.raises(IsolatedLiveBlocked, match="allowlist|canonical GET or HEAD"):
        load_isolated_live_plan(prepared.plan, plan_sha)


def test_deployment_mode_and_safety_env_are_recomputed_from_plists(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    payload = json.loads(prepared.deploy_manifest.read_text(encoding="utf-8"))
    payload["deployment_mode"] = "production"
    deploy_sha = _json(prepared.deploy_manifest, payload)
    plan = json.loads(prepared.plan.read_text(encoding="utf-8"))
    plan["deploy_manifest"]["sha256"] = deploy_sha
    plan_sha = _json(prepared.plan, plan)

    report = execute_isolated_live_validation(
        plan_path=prepared.plan,
        plan_sha256=plan_sha,
        token_file=prepared.token,
        report_output=prepared.report,
        machine=FakeMachine(prepared.fixture_sha256),
        clock=lambda: INSIDE_WINDOW,
    )

    assert report["status"] == "preflight_blocked"
    assert "isolated validation binding" in report["primary_error"]


def test_isolated_verifier_rejects_hash_bound_named_state_plist_drift(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    deploy_root = prepared.deploy_manifest.parent
    plist_path = deploy_root / "launchagents/com.magi.v3.gateway.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    plist["EnvironmentVariables"]["MAGI_PAYMENT_REGISTRY_PATH"] = str(
        prepared.release_manifest.parent / "payment_registry.json"
    )
    plist_path.write_bytes(plistlib.dumps(plist, sort_keys=True))
    deployment = json.loads(prepared.deploy_manifest.read_text(encoding="utf-8"))
    artifact = next(
        row
        for row in deployment["artifacts"]
        if row["path"] == "launchagents/com.magi.v3.gateway.plist"
    )
    artifact.update(
        {"sha256": _digest(plist_path.read_bytes()), "size": plist_path.stat().st_size}
    )
    deploy_sha = _json(prepared.deploy_manifest, deployment)
    marker = json.loads(prepared.marker.read_text(encoding="utf-8"))
    marker["manifest_sha256"] = deploy_sha
    marker_sha = _json(prepared.marker, marker)
    plan = json.loads(prepared.plan.read_text(encoding="utf-8"))
    plan["deploy_manifest"]["sha256"] = deploy_sha
    plan["deploy_prepared_marker"]["sha256"] = marker_sha
    plan_sha = _json(prepared.plan, plan)

    report = execute_isolated_live_validation(
        plan_path=prepared.plan,
        plan_sha256=plan_sha,
        token_file=prepared.token,
        report_output=prepared.report,
        machine=FakeMachine(prepared.fixture_sha256),
        clock=lambda: INSIDE_WINDOW,
    )

    assert report["status"] == "preflight_blocked"
    assert "plist safety binding drifted" in report["primary_error"]


def test_offline_gate_does_not_accept_future_live_or_human_evidence(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    offline = json.loads(prepared.offline.read_text(encoding="utf-8"))
    offline["evidence"]["human_go_approval_recorded"] = {"passed": True}
    offline_sha = _json(prepared.offline, offline)
    plan = json.loads(prepared.plan.read_text(encoding="utf-8"))
    plan["offline_gate_report"]["sha256"] = offline_sha
    plan_sha = _json(prepared.plan, plan)

    report = execute_isolated_live_validation(
        plan_path=prepared.plan,
        plan_sha256=plan_sha,
        token_file=prepared.token,
        report_output=prepared.report,
        machine=FakeMachine(prepared.fixture_sha256),
        clock=lambda: INSIDE_WINDOW,
    )

    assert report["status"] == "preflight_blocked"
    assert "future LIVE, cutover, or human" in report["primary_error"]
    assert prepared.token.exists()


def test_offline_gate_rejects_legacy_generic_passed_fixture(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    generic_sha = _json(
        prepared.offline,
        {
            "schema_version": 1,
            "status": "GO",
            "deployment_mode": DEPLOYMENT_MODE,
            "release_manifest_sha256": _digest(prepared.release_manifest.read_bytes()),
            "deploy_manifest_sha256": _digest(prepared.deploy_manifest.read_bytes()),
            "required_evidence": sorted(OFFLINE_MACHINE_EVIDENCE),
            "evidence": {name: {"passed": True} for name in OFFLINE_MACHINE_EVIDENCE},
            "unproven_gaps": [],
        },
    )
    plan = json.loads(prepared.plan.read_text(encoding="utf-8"))
    plan["offline_gate_report"]["sha256"] = generic_sha
    plan_sha = _json(prepared.plan, plan)

    report = execute_isolated_live_validation(
        plan_path=prepared.plan,
        plan_sha256=plan_sha,
        token_file=prepared.token,
        report_output=prepared.report,
        machine=FakeMachine(prepared.fixture_sha256),
        clock=lambda: INSIDE_WINDOW,
    )

    assert report["status"] == "preflight_blocked"
    assert "candidate context" in report["primary_error"]
    assert prepared.token.exists()


def test_offline_gate_rejects_bound_artifact_tamper(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    gate = json.loads(prepared.offline.read_text(encoding="utf-8"))
    first = gate["evidence"][sorted(OFFLINE_MACHINE_EVIDENCE)[0]]["artifacts"][0]
    Path(first["path"]).write_text("tampered after builder\n", encoding="utf-8")

    report = execute_isolated_live_validation(
        plan_path=prepared.plan,
        plan_sha256=prepared.plan_sha256,
        token_file=prepared.token,
        report_output=prepared.report,
        machine=FakeMachine(prepared.fixture_sha256),
        clock=lambda: INSIDE_WINDOW,
    )

    assert report["status"] == "preflight_blocked"
    assert "hard gates are not proven" in report["primary_error"]
    assert prepared.token.exists()


def test_offline_gate_rejects_expired_builder_report(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    gate = json.loads(prepared.offline.read_text(encoding="utf-8"))
    gate["generated_at"] = "2020-01-01T00:00:00+00:00"
    gate["valid_until"] = "2020-01-02T00:00:00+00:00"
    offline_sha = _json(prepared.offline, gate)
    plan = json.loads(prepared.plan.read_text(encoding="utf-8"))
    plan["offline_gate_report"]["sha256"] = offline_sha
    plan_sha = _json(prepared.plan, plan)

    report = execute_isolated_live_validation(
        plan_path=prepared.plan,
        plan_sha256=plan_sha,
        token_file=prepared.token,
        report_output=prepared.report,
        machine=FakeMachine(prepared.fixture_sha256),
        clock=lambda: INSIDE_WINDOW,
    )

    assert report["status"] == "preflight_blocked"
    assert "fresh, builder-bound" in report["primary_error"]
    assert prepared.token.exists()


class FakeLaunchdRunner:
    def __init__(self, loaded: set[str]) -> None:
        self.loaded = loaded
        self.commands: list[tuple[str, ...]] = []
        self.timeout_verb = ""

    def __call__(self, argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        command = tuple(argv)
        self.commands.append(command)
        verb = command[1]
        if verb == self.timeout_verb:
            raise subprocess.TimeoutExpired(command, 1, output="partial-out", stderr="partial-err")
        if verb == "print":
            label = command[-1].rsplit("/", 1)[-1]
            if label not in self.loaded:
                return SimpleNamespace(
                    returncode=113,
                    stdout="",
                    stderr="Could not find service",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=f"state = running\npid = {1000 + len(self.loaded)}\n",
                stderr="",
            )
        if verb == "bootout":
            self.loaded.discard(command[-1].rsplit("/", 1)[-1])
            return SimpleNamespace(returncode=0, stdout="bootout-ok\n", stderr="")
        if verb == "bootstrap":
            payload = plistlib.loads(Path(command[-1]).read_bytes())
            self.loaded.add(payload["Label"])
            if str(payload["Label"]).startswith("com.magi.v3."):
                for key, content in (
                    ("StandardOutPath", b"service-stdout-complete\n"),
                    ("StandardErrorPath", b"service-stderr-complete\n"),
                ):
                    target = Path(payload[key])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            return SimpleNamespace(returncode=0, stdout="bootstrap-ok\n", stderr="")
        raise AssertionError(f"unexpected subprocess argv: {command}")


class FakeResourceWindowRunner(FakeLaunchdRunner):
    def __init__(
        self,
        loaded: set[str],
        *,
        ps_stdout: str = "1 0 0 1 /sbin/launchd\n",
        lsof_stdout: str = "",
    ) -> None:
        super().__init__(loaded)
        self.ps_stdout = ps_stdout
        self.lsof_stdout = lsof_stdout
        self.fail_bootstrap_labels: set[str] = set()

    def __call__(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        command = tuple(argv)
        if command[0] == "/bin/ps":
            self.commands.append(command)
            return SimpleNamespace(returncode=0, stdout=self.ps_stdout, stderr="")
        if command[0] == "/usr/sbin/lsof":
            self.commands.append(command)
            return SimpleNamespace(returncode=1, stdout=self.lsof_stdout, stderr="")
        if len(command) > 1 and command[1] == "bootstrap":
            label = plistlib.loads(Path(command[-1]).read_bytes())["Label"]
            if label in self.fail_bootstrap_labels:
                self.commands.append(command)
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="injected bootstrap failure"
                )
        return super().__call__(argv, **kwargs)


class FakeURLResponse:
    def __init__(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self) -> "FakeURLResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeHTTPOpener:
    def __init__(self, fixture_body: bytes) -> None:
        self.fixture_body = fixture_body
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: Any, _timeout: float) -> FakeURLResponse:
        method = request.get_method()
        url = request.full_url
        self.requests.append((method, url))
        path = "/" + url.split("/", 3)[-1]
        if path == "/validation/ping":
            body = json.dumps({"status": "ok", "mode": DEPLOYMENT_MODE}).encode()
            return FakeURLResponse(
                200,
                {"X-MAGI-Validation-Mode": DEPLOYMENT_MODE},
                body,
            )
        if path.endswith("document-preview"):
            return FakeURLResponse(
                200,
                {"Content-Disposition": "inline; filename=test.txt"},
                self.fixture_body,
            )
        if path.endswith("document-download"):
            return FakeURLResponse(
                200,
                {"Content-Disposition": "attachment; filename=test.txt"},
                self.fixture_body,
            )
        return FakeURLResponse(
            200,
            {"Content-Type": "application/json"},
            b'{"status":"ready","ready":true}',
        )


class FakeReleaseSnapshotCollector:
    def __init__(self, runner: FakeLaunchdRunner) -> None:
        self.runner = runner
        self.calls: list[tuple[tuple[str, ...], tuple[int, ...]]] = []

    def __call__(self, specs: Any, ports: Any) -> Snapshot:
        specs = tuple(specs)
        ports = tuple(ports)
        self.calls.append((tuple(spec.name for spec in specs), ports))
        owners = []
        for spec in specs:
            for label in spec.launchd_labels:
                if label in self.runner.loaded:
                    owners.append(
                        Owner(
                            spec.name,
                            "release",
                            f"launchd:{label}",
                            "launchd",
                            pid=101 if spec.name == "v2" else 201,
                            root=str(spec.root),
                            namespace=spec.namespace,
                        )
                    )
        return Snapshot(
            owners=tuple(owners),
            coverage=COVERAGE,
            observed_at="2026-07-16T02:30:00+08:00",
            metadata={"ports": {str(port): [] for port in ports}},
        )


def _host_layout(tmp_path: Path, prepared: PreparedFixture) -> tuple[Path, Path]:
    v2_root = (tmp_path / "host-v2").resolve()
    launchagents = (tmp_path / "host-LaunchAgents").resolve()
    v2_root.mkdir()
    launchagents.mkdir()
    for label, script in (
        ("com.magi.daemon", "run_daemon.py"),
        ("com.magi.worker", "run_worker.py"),
    ):
        (launchagents / f"{label}.plist").write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": ["/usr/bin/python3", str(v2_root / script)],
                    "WorkingDirectory": str(v2_root),
                },
                sort_keys=True,
            )
        )
    assert not Path(
        json.loads(prepared.deploy_manifest.read_text(encoding="utf-8"))["runtime_root"]
    ).exists()
    return v2_root, launchagents


def _resource_window_host_layout(
    tmp_path: Path, prepared: PreparedFixture
) -> tuple[Path, Path]:
    v2_root, launchagents = _host_layout(tmp_path, prepared)
    for label in REQUIRED_STOPPED_LABELS[1:]:
        (launchagents / f"{label}.plist").write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": ["/usr/bin/true"],
                },
                sort_keys=True,
            )
        )
    return v2_root, launchagents


def _resource_window_machine(
    tmp_path: Path,
    prepared: PreparedFixture,
    runner: FakeResourceWindowRunner,
) -> MacOSIsolatedLiveMachine:
    deployment = verify_static_plan(
        load_isolated_live_plan(prepared.plan, prepared.plan_sha256)
    )
    v2_root, launchagents = _resource_window_host_layout(tmp_path, prepared)
    fixture_body = (
        deployment.validation_input_root
        / "website"
        / "data"
        / "live-validation-document.txt"
    ).read_bytes()
    return MacOSIsolatedLiveMachine(
        deployment,
        artifact_directory=(tmp_path / "resource-window-artifacts").resolve(),
        runner=runner,
        http_opener=FakeHTTPOpener(fixture_body),
        snapshot_collector=FakeReleaseSnapshotCollector(runner),
        clock=lambda: INSIDE_WINDOW,
        sleeper=lambda _seconds: None,
        uid=501,
        platform_system=lambda: "Darwin",
        v2_root=v2_root,
        launchagents_directory=launchagents,
        expected_runtime_root=deployment.runtime_root,
    )


def test_macos_resource_window_restores_exact_initial_label_set_and_readiness(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    initially_loaded = {
        "com.magi.daemon",
        "com.magi.worker",
        "com.magi.omlx",
        "com.magi.omlx-embed",
    }
    runner = FakeResourceWindowRunner(set(initially_loaded))
    machine = _resource_window_machine(tmp_path, prepared, runner)

    capture = machine.capture_resource_window_host_state(REQUIRED_STOPPED_LABELS)
    machine.stop_v2()
    stopped = machine.stop_resource_window_labels(capture)
    zero = machine.collect_resource_window_zero_receipt(capture)
    restored = machine.restore_resource_window_labels(capture)
    v2_restored = machine.restore_v2()
    readiness = machine.verify_resource_window_readiness(capture)

    assert stopped["ok"] is True and zero["ok"] is True
    assert restored["ok"] is True and v2_restored["ok"] is True
    assert readiness["ok"] is True
    assert runner.loaded == initially_loaded
    assert readiness["required_urls"] == [
        "http://127.0.0.1:5002/health",
        "http://127.0.0.1:5003/health",
        "http://127.0.0.1:8088/health",
        "http://127.0.0.1:8080/v1/models",
        "http://127.0.0.1:8081/v1/models",
    ]
    assert readiness["originally_inactive_not_started"] == sorted(
        set(REQUIRED_STOPPED_LABELS) - initially_loaded
    )
    assert zero["ps_receipt"]["receipt_sha256"]
    assert zero["lsof_receipt"]["receipt_sha256"]
    daemon_bootstraps = [
        command
        for command in runner.commands
        if len(command) > 1
        and command[1] == "bootstrap"
        and command[-1].endswith("com.magi.daemon.plist")
    ]
    assert len(daemon_bootstraps) == 1


@pytest.mark.parametrize(
    "process_command",
    [
        "/tmp/host-v2/run_daemon.py",
        "/usr/bin/python3 -m mlx_lm.server --port 8080",
    ],
)
def test_macos_resource_window_zero_proof_rejects_residual_process(
    tmp_path: Path, process_command: str
) -> None:
    prepared = _prepared(tmp_path)
    runner = FakeResourceWindowRunner(
        {"com.magi.daemon", "com.magi.worker"},
        ps_stdout=f"987 501 1 987 {process_command}\n",
    )
    machine = _resource_window_machine(tmp_path, prepared, runner)
    if process_command.startswith("/tmp/host-v2"):
        runner.ps_stdout = (
            f"987 501 1 987 {machine.v2_root}/run_daemon.py\n"
        )

    capture = machine.capture_resource_window_host_state(REQUIRED_STOPPED_LABELS)
    machine.stop_v2()
    machine.stop_resource_window_labels(capture)
    zero = machine.collect_resource_window_zero_receipt(capture)

    assert zero["ok"] is False
    assert bool(zero["v2_processes"]) is ("host-v2" in process_command)
    assert bool(zero["model_processes"]) is ("mlx_lm.server" in process_command)


def test_macos_resource_restore_continues_after_one_label_fails(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    initially_loaded = {
        "com.magi.daemon",
        "com.magi.worker",
        "com.magi.omlx",
        "com.magi.omlx-embed",
    }
    runner = FakeResourceWindowRunner(set(initially_loaded))
    machine = _resource_window_machine(tmp_path, prepared, runner)
    capture = machine.capture_resource_window_host_state(REQUIRED_STOPPED_LABELS)
    machine.stop_v2()
    machine.stop_resource_window_labels(capture)
    runner.fail_bootstrap_labels.add("com.magi.omlx")

    assert machine.restore_v2()["ok"] is True
    restored = machine.restore_resource_window_labels(capture)

    assert restored["ok"] is False
    assert any("com.magi.omlx" in error for error in restored["errors"])
    assert "com.magi.omlx" not in runner.loaded
    assert "com.magi.omlx-embed" in runner.loaded
    assert {"com.magi.daemon", "com.magi.worker"} <= runner.loaded


def test_macos_v2_restore_continues_after_one_agent_fails(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    runner = FakeResourceWindowRunner({"com.magi.daemon", "com.magi.worker"})
    machine = _resource_window_machine(tmp_path, prepared, runner)
    machine.stop_v2()
    runner.fail_bootstrap_labels.add("com.magi.daemon")

    restored = machine.restore_v2()

    assert restored["ok"] is False
    assert any("com.magi.daemon" in error for error in restored["errors"])
    assert "com.magi.daemon" not in runner.loaded
    assert "com.magi.worker" in runner.loaded


def test_macos_host_adapter_executes_exact_launchd_handoff_and_preserves_artifacts(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    plan = load_isolated_live_plan(prepared.plan, prepared.plan_sha256)
    deployment = verify_static_plan(plan)
    v2_root, launchagents = _host_layout(tmp_path, prepared)
    runner = FakeLaunchdRunner({"com.magi.daemon", "com.magi.worker"})
    collector = FakeReleaseSnapshotCollector(runner)
    fixture_body = (deployment.validation_input_root / "website" / "data" / "live-validation-document.txt").read_bytes()
    opener = FakeHTTPOpener(fixture_body)
    artifacts = (tmp_path / "host-artifacts").resolve()
    machine = MacOSIsolatedLiveMachine(
        deployment,
        artifact_directory=artifacts,
        runner=runner,
        http_opener=opener,
        snapshot_collector=collector,
        clock=lambda: INSIDE_WINDOW,
        sleeper=lambda _seconds: None,
        uid=501,
        platform_system=lambda: "Darwin",
        v2_root=v2_root,
        launchagents_directory=launchagents,
        expected_runtime_root=deployment.runtime_root,
    )
    # This host handoff test uses a fake launchctl runner; native UI execution
    # has its own bounded probe tests, so provide the same raw receipt contract
    # without asking the launchctl fake to emulate a Python candidate process.
    machine.run_native_ime_candidate_probe = FakeMachine(
        deployment.fixture_sha256
    ).run_native_ime_candidate_probe

    report = execute_isolated_live_validation(
        plan_path=prepared.plan,
        plan_sha256=prepared.plan_sha256,
        token_file=prepared.token,
        report_output=prepared.report,
        machine=machine,
        clock=lambda: INSIDE_WINDOW,
    )

    assert report["status"] == "validation_passed_v2_restored"
    assert runner.loaded == {"com.magi.daemon", "com.magi.worker"}
    assert not deployment.runtime_root.exists()
    assert not any(launchagents.glob("com.magi.v3.*.plist"))
    assert collector.calls and all(call[0] == ("v2", "v3") for call in collector.calls)
    assert all(command[0] == LAUNCHCTL for command in runner.commands)
    assert {command[1] for command in runner.commands} <= {"print", "bootout", "bootstrap"}
    v3_bootstraps = [
        Path(command[-1]).stem
        for command in runner.commands
        if command[1] == "bootstrap" and "com.magi.v3." in command[-1]
    ]
    assert v3_bootstraps == [
        "com.magi.v3.control",
        "com.magi.v3.gateway",
        "com.magi.v3.supervisor",
    ]
    v3_bootouts = [
        command[-1].rsplit("/", 1)[-1]
        for command in runner.commands
        if command[1] == "bootout" and "com.magi.v3." in command[-1]
    ]
    assert v3_bootouts[-3:] == [
        "com.magi.v3.supervisor",
        "com.magi.v3.gateway",
        "com.magi.v3.control",
    ]
    assert any(
        command[1] == "bootstrap" and command[-1].endswith("com.magi.daemon.plist")
        for command in runner.commands
    )
    command_artifacts = list(artifacts.glob("*-launchctl-*.json"))
    assert command_artifacts
    recorded = "\n".join(path.read_text(encoding="utf-8") for path in command_artifacts)
    assert "bootstrap-ok" in recorded and "bootout-ok" in recorded
    assert list(artifacts.rglob("*.body"))
    service_logs = list((artifacts / "service-stdout-stderr").rglob("*.log"))
    assert len(service_logs) == 6
    assert any(path.read_bytes() == b"service-stdout-complete\n" for path in service_logs)
    assert any(path.read_bytes() == b"service-stderr-complete\n" for path in service_logs)


def test_macos_adapter_rejects_non_darwin_and_non_allowlisted_subprocess_argv(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    deployment = verify_static_plan(
        load_isolated_live_plan(prepared.plan, prepared.plan_sha256)
    )
    v2_root, launchagents = _host_layout(tmp_path, prepared)

    with pytest.raises(IsolatedLiveBlocked, match="requires Darwin"):
        MacOSIsolatedLiveMachine(
            deployment,
            artifact_directory=(tmp_path / "linux-artifacts").resolve(),
            platform_system=lambda: "Linux",
            v2_root=v2_root,
            launchagents_directory=launchagents,
            expected_runtime_root=deployment.runtime_root,
        )

    runner = FakeLaunchdRunner({"com.magi.daemon", "com.magi.worker"})
    machine = MacOSIsolatedLiveMachine(
        deployment,
        artifact_directory=(tmp_path / "darwin-artifacts").resolve(),
        runner=runner,
        snapshot_collector=FakeReleaseSnapshotCollector(runner),
        platform_system=lambda: "Darwin",
        v2_root=v2_root,
        launchagents_directory=launchagents,
        expected_runtime_root=deployment.runtime_root,
    )
    commands_before = list(runner.commands)
    with pytest.raises(IsolatedLiveBlocked, match="allowlist"):
        machine._run_launchctl(("/bin/sh", "-c", "echo unsafe"), action="unsafe")
    assert runner.commands == commands_before


def test_macos_adapter_timeout_records_full_stdout_stderr_and_fails_closed(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    deployment = verify_static_plan(load_isolated_live_plan(prepared.plan, prepared.plan_sha256))
    v2_root, launchagents = _host_layout(tmp_path, prepared)
    runner = FakeLaunchdRunner({"com.magi.daemon", "com.magi.worker"})
    runner.timeout_verb = "bootout"
    artifacts = (tmp_path / "timeout-artifacts").resolve()
    machine = MacOSIsolatedLiveMachine(
        deployment,
        artifact_directory=artifacts,
        runner=runner,
        snapshot_collector=FakeReleaseSnapshotCollector(runner),
        platform_system=lambda: "Darwin",
        v2_root=v2_root,
        launchagents_directory=launchagents,
        expected_runtime_root=deployment.runtime_root,
    )

    with pytest.raises(IsolatedLiveBlocked, match="timed out"):
        machine.activate_maintenance_blackout()

    records = list(artifacts.glob("*-launchctl-bootout-*.json"))
    assert records
    payload = json.loads(records[-1].read_text(encoding="utf-8"))
    assert payload["timed_out"] is True
    assert payload["stdout"] == "partial-out"
    assert payload["stderr"] == "partial-err"


def test_macos_adapter_records_raw_ps_lsof_probe_stdout_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(tmp_path)
    deployment = verify_static_plan(load_isolated_live_plan(prepared.plan, prepared.plan_sha256))
    v2_root, launchagents = _host_layout(tmp_path, prepared)
    runner = FakeLaunchdRunner({"com.magi.daemon", "com.magi.worker"})

    def raw_runner(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout="raw-process-output\n",
            stderr="raw-process-warning\n",
        )

    monkeypatch.setattr(cutover_probe.subprocess, "run", raw_runner)

    def collector(_specs: Any, _ports: Any) -> Snapshot:
        cutover_probe._run_probe_command(
            [cutover_probe.PS_EXECUTABLE, "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        cutover_probe._run_probe_command(
            [cutover_probe.LSOF_EXECUTABLE, "-n", "-d", "cwd", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return Snapshot(coverage=COVERAGE)

    artifacts = (tmp_path / "raw-probe-artifacts").resolve()
    machine = MacOSIsolatedLiveMachine(
        deployment,
        artifact_directory=artifacts,
        runner=runner,
        snapshot_collector=collector,
        platform_system=lambda: "Darwin",
        v2_root=v2_root,
        launchagents_directory=launchagents,
        expected_runtime_root=deployment.runtime_root,
    )

    machine.collect_ownership_snapshot()

    records = list(artifacts.glob("*-ownership-command-*.json"))
    assert len(records) == 2
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in records]
    assert {payload["argv"][0] for payload in payloads} == {
        cutover_probe.PS_EXECUTABLE,
        cutover_probe.LSOF_EXECUTABLE,
    }
    assert all(payload["stdout"] == "raw-process-output\n" for payload in payloads)
    assert all(payload["stderr"] == "raw-process-warning\n" for payload in payloads)


def test_macos_adapter_preserves_initially_unloaded_v2_jobs_on_restore(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    deployment = verify_static_plan(load_isolated_live_plan(prepared.plan, prepared.plan_sha256))
    v2_root, launchagents = _host_layout(tmp_path, prepared)
    runner = FakeLaunchdRunner({"com.magi.daemon"})
    machine = MacOSIsolatedLiveMachine(
        deployment,
        artifact_directory=(tmp_path / "initial-state-artifacts").resolve(),
        runner=runner,
        snapshot_collector=FakeReleaseSnapshotCollector(runner),
        http_opener=FakeHTTPOpener(b"fixture"),
        platform_system=lambda: "Darwin",
        v2_root=v2_root,
        launchagents_directory=launchagents,
        expected_runtime_root=deployment.runtime_root,
    )

    machine.activate_maintenance_blackout()
    receipt = machine.restore_v2()

    assert receipt["initially_loaded_labels"] == ["com.magi.daemon"]
    assert runner.loaded == {"com.magi.daemon"}


def test_macos_adapter_never_deletes_preexisting_v3_artifacts_it_did_not_install(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    deployment = verify_static_plan(load_isolated_live_plan(prepared.plan, prepared.plan_sha256))
    v2_root, launchagents = _host_layout(tmp_path, prepared)
    runner = FakeLaunchdRunner({"com.magi.daemon", "com.magi.worker"})
    machine = MacOSIsolatedLiveMachine(
        deployment,
        artifact_directory=(tmp_path / "foreign-artifact-audit").resolve(),
        runner=runner,
        snapshot_collector=FakeReleaseSnapshotCollector(runner),
        platform_system=lambda: "Darwin",
        v2_root=v2_root,
        launchagents_directory=launchagents,
        expected_runtime_root=deployment.runtime_root,
    )
    foreign_plist = launchagents / "com.magi.v3.control.plist"
    foreign_plist.write_text("operator-owned\n", encoding="utf-8")
    deployment.runtime_root.mkdir(parents=True)
    foreign_runtime = deployment.runtime_root / "operator-owned.txt"
    foreign_runtime.write_text("keep\n", encoding="utf-8")

    with pytest.raises(IsolatedLiveBlocked, match="runtime must be absent"):
        machine.install_validation(deployment)
    receipt = machine.remove_validation(deployment)

    assert receipt["ok"] is False
    assert receipt["remaining_validation_artifacts"] == 2
    assert foreign_plist.read_text(encoding="utf-8") == "operator-owned\n"
    assert foreign_runtime.read_text(encoding="utf-8") == "keep\n"


class InspectOnlyAdapter:
    constructed = 0
    inspected = 0

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        type(self).constructed += 1

    def inspect(self) -> dict[str, Any]:
        type(self).inspected += 1
        return {
            "schema_version": 1,
            "mode": "inspect_dry_run",
            "mutation_performed": False,
            "execution_authorized": False,
        }


def test_macos_cli_defaults_to_inspect_and_never_requires_or_consumes_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = _prepared(tmp_path)
    InspectOnlyAdapter.constructed = InspectOnlyAdapter.inspected = 0

    result = macos_main(
        [
            "--plan",
            str(prepared.plan),
            "--plan-sha256",
            prepared.plan_sha256,
            "--artifact-directory",
            str((tmp_path / "inspect-artifacts").resolve()),
        ],
        adapter_factory=InspectOnlyAdapter,  # type: ignore[arg-type]
        platform_system=lambda: "Linux",
    )

    assert result == 0
    assert InspectOnlyAdapter.constructed == InspectOnlyAdapter.inspected == 1
    assert prepared.token.exists()
    assert json.loads(capsys.readouterr().out)["mutation_performed"] is False


def test_macos_cli_real_adapter_inspect_uses_only_read_only_launchctl_print(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = _prepared(tmp_path)
    v2_root, launchagents = _host_layout(tmp_path, prepared)
    runner = FakeLaunchdRunner({"com.magi.daemon", "com.magi.worker"})
    collector = FakeReleaseSnapshotCollector(runner)
    opener = FakeHTTPOpener(b"fixture")

    def factory(deployment: VerifiedDeployment, **kwargs: Any) -> MacOSIsolatedLiveMachine:
        return MacOSIsolatedLiveMachine(
            deployment,
            artifact_directory=kwargs["artifact_directory"],
            runner=runner,
            http_opener=opener,
            snapshot_collector=collector,
            platform_system=lambda: "Darwin",
            v2_root=v2_root,
            launchagents_directory=launchagents,
            expected_runtime_root=deployment.runtime_root,
        )

    result = macos_main(
        [
            "--plan",
            str(prepared.plan),
            "--plan-sha256",
            prepared.plan_sha256,
            "--artifact-directory",
            str((tmp_path / "real-inspect-artifacts").resolve()),
        ],
        adapter_factory=factory,  # type: ignore[arg-type]
        platform_system=lambda: "Darwin",
    )

    assert result == 0
    assert runner.commands and {command[1] for command in runner.commands} == {"print"}
    assert prepared.token.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "inspect_dry_run"
    assert report["execution_authorized"] is False
    assert report["v2_readiness"]["ok"] is True
    assert {url for _method, url in opener.requests} == set(V2_READINESS_URLS)


def test_macos_cli_blocks_unhealthy_v2_before_token_or_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = _prepared(tmp_path)
    plan = load_isolated_live_plan(prepared.plan, prepared.plan_sha256)
    machine = FakeMachine(prepared.fixture_sha256, integrity_fail_once=True)

    result = macos_main(
        [
            "--plan",
            str(prepared.plan),
            "--plan-sha256",
            prepared.plan_sha256,
            "--artifact-directory",
            str((tmp_path / "unhealthy-v2-artifacts").resolve()),
            "--token-file",
            str(prepared.token),
            "--report-output",
            str(prepared.report),
            "--expected-release-manifest-sha256",
            plan.release_manifest.sha256,
            "--expected-deploy-manifest-sha256",
            plan.deploy_manifest.sha256,
            "--expected-offline-gate-sha256",
            plan.offline_gate_report.sha256,
            "--execute-isolated-live",
        ],
        adapter_factory=lambda *_args, **_kwargs: machine,  # type: ignore[arg-type]
        clock=lambda: INSIDE_WINDOW,
        platform_system=lambda: "Darwin",
    )

    assert result == 2
    assert prepared.token.exists()
    assert not prepared.report.exists()
    assert machine.actions == ["verify_v2"]
    assert "preflight V2 readiness" in json.loads(capsys.readouterr().err)["error"]


def test_macos_cli_execute_requires_flag_window_token_and_three_exact_hashes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = _prepared(tmp_path)
    plan = load_isolated_live_plan(prepared.plan, prepared.plan_sha256)
    factory_calls = 0

    def factory(*_args: Any, **_kwargs: Any) -> FakeMachine:
        nonlocal factory_calls
        factory_calls += 1
        return FakeMachine(prepared.fixture_sha256)

    base = [
        "--plan",
        str(prepared.plan),
        "--plan-sha256",
        prepared.plan_sha256,
        "--artifact-directory",
        str((tmp_path / "execute-artifacts").resolve()),
        "--token-file",
        str(prepared.token),
        "--report-output",
        str(prepared.report),
        "--execute-isolated-live",
    ]
    assert (
        macos_main(
            base,
            adapter_factory=factory,  # type: ignore[arg-type]
            clock=lambda: INSIDE_WINDOW,
            platform_system=lambda: "Darwin",
        )
        == 2
    )
    assert factory_calls == 0 and prepared.token.exists()
    assert "exact matching" in json.loads(capsys.readouterr().err)["error"]

    result = macos_main(
        [
            *base,
            "--expected-release-manifest-sha256",
            plan.release_manifest.sha256,
            "--expected-deploy-manifest-sha256",
            plan.deploy_manifest.sha256,
            "--expected-offline-gate-sha256",
            plan.offline_gate_report.sha256,
        ],
        adapter_factory=factory,  # type: ignore[arg-type]
        clock=lambda: INSIDE_WINDOW,
        platform_system=lambda: "Darwin",
    )
    assert result == 0
    assert factory_calls == 1
    assert not prepared.token.exists()
    assert json.loads(capsys.readouterr().out)["final_cutover"] is False


def test_macos_cli_execute_outside_window_blocks_before_adapter_or_token(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    plan = load_isolated_live_plan(prepared.plan, prepared.plan_sha256)
    called = False

    def factory(*_args: Any, **_kwargs: Any) -> FakeMachine:
        nonlocal called
        called = True
        return FakeMachine(prepared.fixture_sha256)

    result = macos_main(
        [
            "--plan",
            str(prepared.plan),
            "--plan-sha256",
            prepared.plan_sha256,
            "--artifact-directory",
            str((tmp_path / "outside-artifacts").resolve()),
            "--token-file",
            str(prepared.token),
            "--report-output",
            str(prepared.report),
            "--expected-release-manifest-sha256",
            plan.release_manifest.sha256,
            "--expected-deploy-manifest-sha256",
            plan.deploy_manifest.sha256,
            "--expected-offline-gate-sha256",
            plan.offline_gate_report.sha256,
            "--execute-isolated-live",
        ],
        adapter_factory=factory,  # type: ignore[arg-type]
        clock=lambda: OUTSIDE_WINDOW,
        platform_system=lambda: "Darwin",
    )

    assert result == 2
    assert called is False
    assert prepared.token.exists()


def test_macos_cli_refuses_report_output_inside_immutable_release(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    plan = load_isolated_live_plan(prepared.plan, prepared.plan_sha256)
    called = False

    def factory(*_args: Any, **_kwargs: Any) -> FakeMachine:
        nonlocal called
        called = True
        return FakeMachine(prepared.fixture_sha256)

    result = macos_main(
        [
            "--plan",
            str(prepared.plan),
            "--plan-sha256",
            prepared.plan_sha256,
            "--artifact-directory",
            str((tmp_path / "protected-output-artifacts").resolve()),
            "--token-file",
            str(prepared.token),
            "--report-output",
            str((prepared.release_manifest.parent / "unsafe-report.json").resolve()),
            "--expected-release-manifest-sha256",
            plan.release_manifest.sha256,
            "--expected-deploy-manifest-sha256",
            plan.deploy_manifest.sha256,
            "--expected-offline-gate-sha256",
            plan.offline_gate_report.sha256,
            "--execute-isolated-live",
        ],
        adapter_factory=factory,  # type: ignore[arg-type]
        clock=lambda: INSIDE_WINDOW,
        platform_system=lambda: "Darwin",
    )

    assert result == 2
    assert called is False
    assert prepared.token.exists()
