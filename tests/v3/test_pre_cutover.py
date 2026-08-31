from __future__ import annotations

import hashlib
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.v3_cutover.core import Owner, Snapshot
import scripts.v3_pre_cutover as pre_cutover_module
import scripts.v3_static_external_staging as static_external
from scripts.v3_pre_cutover import (
    ExpectedContext,
    PreCutoverError,
    PreCutoverPreflight,
    RequiredPaths,
    _write_json_atomic,
)
from scripts.v3_pdf_namer_handoff import precopy

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 13, 19, 0, tzinfo=timezone.utc)  # 03:00 Asia/Taipei
Disk = namedtuple("Disk", "total used free")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_pre_cutover_rejects_conditional_authorization_outside_approved_window(
    tmp_path: Path,
) -> None:
    gate_config = tmp_path / "gates.json"
    write_json(gate_config, {})
    evidence = tmp_path / "human.json"
    write_json(evidence, {"placeholder": True})
    window = {
        "starts_at": "2026-07-14T06:00:00+08:00",
        "ends_at": "2026-07-14T07:00:00+08:00",
        "timezone": "Asia/Taipei",
    }
    binding = {
        "evidence_path": str(evidence.resolve()),
        "evidence_sha256": digest(evidence),
        "metrics_sha256": "4" * 64,
        "conditional_daytime_window": window,
        "conditional_request_sha256": "1" * 64,
        "conditional_receipt_sha256": "2" * 64,
        "conditional_consumption_sha256": "3" * 64,
    }

    class Probe:
        context = ExpectedContext("campaign", "a" * 64, "hardware", "b" * 64)
        gate_config_path = gate_config

        def __init__(self) -> None:
            self.observed = []

        def _check(self, name, ok, detail) -> None:
            self.observed.append((name, ok, detail))

    probe = Probe()
    PreCutoverPreflight._check_conditional_authorization(
        probe,
        NOW,
        {"conditional_authorization": binding},
        [],
    )
    assert probe.observed == [
        (
            "conditional_daytime_authorization",
            False,
            ["outside human-approved conditional daytime window"],
        )
    ]


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc), True),
        (datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc), False),
    ],
)
def test_pre_cutover_uses_daytime_window_with_end_exclusive(
    now: datetime,
    expected: bool,
) -> None:
    class Probe:
        def __init__(self) -> None:
            self.observed = []

        def _check(self, name, ok, detail) -> None:
            self.observed.append((name, ok, detail))

    gates = {
        "timezone": "Asia/Taipei",
        "window": {"start": "02:00", "end": "04:00"},
        "conditional_daytime_authorization_required": True,
        "conditional_daytime_window": {
            "starts_at": "2026-07-27T09:00:00+08:00",
            "ends_at": "2026-07-27T18:00:00+08:00",
            "timezone": "Asia/Taipei",
        },
    }
    probe = Probe()
    PreCutoverPreflight._check_window(probe, now, gates)
    assert probe.observed[0][0] == "cutover_window"
    assert probe.observed[0][1] is expected


def safe_snapshot(
    *,
    release: str = "v3",
    ambiguous: bool = False,
    errors=(),
) -> Snapshot:
    return Snapshot(
        owners=(
            Owner(
                release,
                "release",
                f"{release}:current-production",
                "test",
                pid=123,
                ambiguous=ambiguous,
            ),
        ),
        probe_errors=tuple(errors),
        coverage=frozenset({"process", "pidfile", "port", "launchd", "ownership"}),
        observed_at=NOW.isoformat(),
    )


def fixture(tmp_path: Path, *, legacy_v2: bool = False):
    gates = tmp_path / "config" / "gates.json"
    gates.parent.mkdir()
    gates.write_bytes((ROOT / "config" / "v3_cutover_gates.json").read_bytes())
    fixture_gates = json.loads(gates.read_text(encoding="utf-8"))
    fixture_gates["window"] = {
        "start": "02:00",
        "end": "04:00",
        "allowed_local_dates": ["2026-07-14"],
    }
    # Most legacy pre-cutover fixtures exercise the original interactive
    # approval path.  Conditional authorization has dedicated tests below and
    # is enabled only when the fixture explicitly opts into the rc110 policy.
    fixture_gates.pop("conditional_daytime_window", None)
    fixture_gates.pop("conditional_daytime_authorization_required", None)
    if legacy_v2:
        fixture_gates["source_contract"]["legacy_v2_validation"] = "enabled"
        fixture_gates["automatic_no_go"] = list(
            dict.fromkeys(
                [
                    *fixture_gates["automatic_no_go"],
                    "v2_process_or_release_owner_still_active_before_v3_start",
                    "v2_port_scheduler_writer_or_model_owner_not_released",
                ]
            )
        )
    write_json(gates, fixture_gates)
    gate_hash = digest(gates)
    context = ExpectedContext("campaign-final", "c" * 64, "mac-mini-01", gate_hash)

    campaign_config = json.loads((ROOT / "config" / "v3_validation_campaign.json").read_text())
    campaign_config["armed"] = True
    campaign_config["production_release"] = "v2" if legacy_v2 else "v3"
    campaign_config_path = tmp_path / "config" / "campaign.json"
    write_json(campaign_config_path, campaign_config)

    campaign_report = tmp_path / "evidence" / "campaign.json"
    write_json(
        campaign_report,
        {
            **context.to_dict(),
            "decision": "GO",
            "certifying": True,
            "offline_complete": True,
            "generated_at": NOW.isoformat(),
        },
    )
    gate_report = tmp_path / "evidence" / "release-gate.json"
    write_json(
        gate_report,
        {
            "schema_version": 1,
            "decision": "GO",
            "fail_closed": True,
            "required_count": len(
                json.loads(gates.read_text(encoding="utf-8"))["required_evidence"]
            ),
            "passed": json.loads(gates.read_text(encoding="utf-8"))["required_evidence"],
            "missing": [],
            "failed": [],
            "invalid": {},
            "generated_at": NOW.isoformat(),
            "expected_context": context.to_dict(),
        },
    )

    backup_root = tmp_path / "backup"
    backup = backup_root / "v2-backup.bin"
    restore = backup_root / "restore-drill.json"
    content_manifest = backup_root / "backup-content.json"
    backup.parent.mkdir()
    backup.write_bytes(b"backup")
    write_json(
        content_manifest,
        {
            "schema_version": 2,
            "coverage": ["sqlite", "website_assets", "website_data"],
            "source_roots": {"v2": str(tmp_path / "v2"), "website": str(tmp_path / "website")},
            "databases": [
                {
                    "source": "state.sqlite3",
                    "backup": "sqlite/state.sqlite3",
                    "sha256": "d" * 64,
                    "size": 1,
                    "quick_check": "ok",
                }
            ],
            "mutable_files": [
                {
                    "scope": "website_data",
                    "source": "data/site-data.json",
                    "backup": "website/data/site-data.json",
                    "sha256": "e" * 64,
                    "size": 1,
                    "mode": "0644",
                }
            ],
            "mutable_directories": [
                {"scope": "website_data", "source": "data", "backup": "website/data", "mode": "0755"},
                {
                    "scope": "website_assets",
                    "source": "assets",
                    "backup": "website/assets",
                    "mode": "0755",
                },
            ],
        },
    )
    write_json(
        restore,
        {
            "schema_version": 2,
            "actual_restore_performed": True,
            "status": "passed",
            "backup_sha256": digest(backup),
            "content_manifest_sha256": digest(content_manifest),
            "verified_scopes": ["sqlite", "website_assets", "website_data"],
            "verified_databases": 1,
            "verified_mutable_files": 1,
            "verified_mutable_directories": 2,
        },
    )
    backup_metadata = backup_root / "metadata.json"
    write_json(
        backup_metadata,
        {
            "schema_version": 2,
            **context.to_dict(),
            "artifact_path": backup.name,
            "sha256": digest(backup),
            "created_at": (NOW - timedelta(hours=1)).isoformat(),
            "source_release_sha": context.release_sha,
            "coverage": ["sqlite", "website_assets", "website_data"],
            "database_count": 1,
            "mutable_file_count": 1,
            "mutable_directory_count": 2,
            "content_manifest": {
                "path": content_manifest.name,
                "sha256": digest(content_manifest),
            },
            "restore_drill": {
                "actual_restore_performed": True,
                "status": "passed",
                "backup_sha256": digest(backup),
                "content_manifest_sha256": digest(content_manifest),
                "verified_scopes": ["sqlite", "website_assets", "website_data"],
                "verified_databases": 1,
                "verified_mutable_files": 1,
                "verified_mutable_directories": 2,
                "evidence_path": restore.name,
                "evidence_sha256": digest(restore),
            },
        },
    )

    external_root = tmp_path / "external-inputs"
    cron_source = external_root / "cron_jobs.json"
    write_json(
        cron_source,
        [{"id": "job_health", "enabled": True, "cron": "*/5 * * * *", "command": "@MAGI health"}],
    )
    cron_source_sha256 = digest(cron_source)
    website_root = external_root / "website"
    website_admin = website_root / "admin" / "admin_server.py"
    website_admin.parent.mkdir(parents=True)
    website_admin.write_text("class AdminHandler: pass\n", encoding="utf-8")

    release = tmp_path / "release"
    payload = release / "payload" / "magi.py"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"immutable v3")
    launcher = release / "bin" / "magi-v3-python"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o555)
    cron_policy = release / "config" / "v3_schedule_dispatch_policy.json"
    write_json(
        cron_policy,
        {"schema_version": 1, "cron_jobs_sha256": cron_source_sha256},
    )
    release_members = (
        (payload, "payload/magi.py", "0444"),
        (launcher, "bin/magi-v3-python", "0555"),
        (cron_policy, "config/v3_schedule_dispatch_policy.json", "0444"),
    )
    manifest = release / "release-manifest.json"
    write_json(
        manifest,
        {
            "schema_version": 1,
            "release_id": "v3-final",
            "commit": "a" * 40,
            "immutable": True,
            "source_snapshot_sha256": "c" * 64,
            "release_sha256": "c" * 64,
            "source_file_count": len(release_members),
            "files": [
                {
                    "path": relative,
                    "sha256": digest(path),
                    "size": path.stat().st_size,
                    "mode": mode,
                }
                for path, relative, mode in release_members
            ],
        },
    )
    write_json(
        release / "RELEASE_COMPLETE.json",
        {
            "schema_version": 1,
            "release_id": "v3-final",
            "commit": "a" * 40,
            "manifest": manifest.name,
            "manifest_sha256": digest(manifest),
            "source_snapshot_sha256": "c" * 64,
            "release_sha256": "c" * 64,
            "source_file_count": len(release_members),
        },
    )
    for release_file in release.rglob("*"):
        if release_file.is_file():
            release_file.chmod(0o555 if release_file == launcher else 0o444)
    for release_directory in sorted(
        (path for path in release.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        release_directory.chmod(0o555)
    release.chmod(0o555)

    readiness_manifest = tmp_path / "config" / "readiness.json"
    required_surfaces = ["production_http", "route_handlers", "session_and_webhooks"]
    write_json(
        readiness_manifest,
        {
            "schema_version": 1,
            "replacement_ready": True,
            "required_surface_ids": required_surfaces,
            "summary": {"required": 3, "implemented": 3, "tested": 3, "blocked": 0},
            "surfaces": [
                {
                    "id": surface_id,
                    "required": True,
                    "implemented": True,
                    "tested": True,
                    "blocked": False,
                    "status": "ready",
                }
                for surface_id in required_surfaces
            ],
        },
    )

    deploy_root = tmp_path / "deploy"
    deploy_manifest = deploy_root / "deploy-manifest.json"
    release_manifest_sha256 = digest(manifest)
    executable = str(launcher.resolve())
    cron_snapshot = deploy_root / "runtime-inputs" / "cron_jobs.v3.json"
    cron_snapshot.parent.mkdir(parents=True)
    cron_snapshot.write_bytes(cron_source.read_bytes())
    python_runtime_manifest = deploy_root / "runtime-inputs" / "python-runtime-manifest.json"
    write_json(
        python_runtime_manifest,
        {"schema_version": 1, "runtime": "/synthetic/python", "files": []},
    )
    static_inputs = tmp_path / "external"
    laf_config = static_inputs / "config.json"
    google_credentials = static_inputs / "credentials.json"
    write_json(laf_config, {})
    write_json(google_credentials, {})
    laf_config.chmod(0o600)
    google_credentials.chmod(0o600)
    source_tokens = {
        "google_calendar_token": static_inputs / "google_calendar_token.json",
        "laf_gmail_token": static_inputs / "laf_gmail_token.pickle",
        "file_review_token": static_inputs / "filereview_token.pickle",
    }
    for key, path in source_tokens.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((key + "\n").encode())
    runtime_root = tmp_path / "v3"
    target_tokens = {
        "google_calendar_token": runtime_root / "shared/secrets/google_calendar_token.json",
        "laf_gmail_token": runtime_root / "shared/secrets/laf_gmail_token.pickle",
        "file_review_token": runtime_root / "shared/secrets/filereview_token.pickle",
        "accounting_sheets_token": runtime_root / "shared/secrets/accounting_sheets_token.json",
        "drive_sync_token": runtime_root / "shared/secrets/drive_sync_token.json",
        "drive_sync_write_token": runtime_root / "shared/secrets/drive_sync_write_token.json",
    }
    target_sources = {
        **source_tokens,
        "accounting_sheets_token": source_tokens["google_calendar_token"],
        "drive_sync_token": source_tokens["google_calendar_token"],
        "drive_sync_write_token": source_tokens["google_calendar_token"],
    }
    for key, target in target_tokens.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(target_sources[key].read_bytes())
        target.chmod(0o600)
    ocr_queue = tmp_path / "external/nas-ocr-queue.db"
    with sqlite3.connect(ocr_queue) as connection:
        connection.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY)")
    env_file = static_inputs / "magi.env"
    env_file.write_text("TEST_ONLY=1\n", encoding="utf-8")
    env_file.chmod(0o600)
    static_snapshot = static_external.snapshot_static_sources(
        manifest,
        expected_release_manifest_sha256=release_manifest_sha256,
        env_file=env_file,
        website_root=website_root,
        config_file=laf_config,
        google_credentials_file=google_credentials,
        accounting_credentials_file=google_credentials,
    )
    static_target = runtime_root / "shared" / "external"
    static_report = static_external.stage_static_external(
        manifest,
        expected_release_manifest_sha256=release_manifest_sha256,
        env_file=env_file,
        website_root=website_root,
        config_file=laf_config,
        google_credentials_file=google_credentials,
        accounting_credentials_file=google_credentials,
        expected_source_snapshot_sha256=static_snapshot["source_snapshot_sha256"],
        target_root=static_target,
    )
    website_root = static_target / "website"
    website_admin = website_root / "admin" / "admin_server.py"
    laf_config = static_target / "config.json"
    google_credentials = static_target / "google-credentials.json"
    accounting_credentials = static_target / "accounting-credentials.json"
    env_file = static_target / ".env"
    static_receipt = static_target / static_external.RECEIPT_NAME
    static_receipt_sha256 = static_report["receipt_sha256"]
    static_release_receipt: Path | None = None
    if not legacy_v2:
        static_release_receipt = (
            deploy_root
            / "runtime-inputs"
            / static_external.RELEASE_BINDING_RECEIPT_NAME
        )
        binding_bytes, binding_report = (
            static_external.render_static_external_release_binding(
                manifest,
                expected_release_manifest_sha256=release_manifest_sha256,
                target_root=static_target,
                binding_receipt=static_release_receipt,
            )
        )
        static_release_receipt.write_bytes(binding_bytes)
        static_release_receipt.chmod(0o600)
        static_receipt = static_release_receipt
        static_receipt_sha256 = binding_report["binding_receipt_sha256"]
    external_inputs = {
        "env_file": str(env_file.resolve()),
        "env_file_sha256": digest(env_file),
        "cron_jobs_file": str(cron_snapshot.resolve()),
        "cron_jobs_sha256": digest(cron_snapshot),
        "cron_jobs_source_file": str(cron_source.resolve()),
        "cron_jobs_source_sha256": cron_source_sha256,
        "website_root": str(website_root.resolve()),
        "website_admin_sha256": digest(website_admin),
        "python_runtime_manifest": str(python_runtime_manifest.resolve()),
        "python_runtime_manifest_sha256": digest(python_runtime_manifest),
        "laf_config_file": str(laf_config.resolve()),
        "laf_config_sha256": digest(laf_config),
        "laf_config_mode": "0600",
        "google_credentials_file": str(google_credentials.resolve()),
        "google_credentials_sha256": digest(google_credentials),
        "google_credentials_mode": "0600",
        "google_calendar_token_source_file": str(source_tokens["google_calendar_token"].resolve()),
        "google_calendar_token_source_sha256": digest(source_tokens["google_calendar_token"]),
        "google_calendar_token_file": str(target_tokens["google_calendar_token"].resolve()),
        "laf_gmail_token_source_file": str(source_tokens["laf_gmail_token"].resolve()),
        "laf_gmail_token_source_sha256": digest(source_tokens["laf_gmail_token"]),
        "laf_gmail_token_file": str(target_tokens["laf_gmail_token"].resolve()),
        "file_review_token_source_file": str(source_tokens["file_review_token"].resolve()),
        "file_review_token_source_sha256": digest(source_tokens["file_review_token"]),
        "file_review_token_file": str(target_tokens["file_review_token"].resolve()),
        "gmail_compose_token_source_file": None,
        "gmail_compose_token_source_sha256": None,
        "gmail_compose_token_file": str(runtime_root / "shared/secrets/gmail_compose_token.json"),
        "optional_degraded_inputs": ["gmail_compose_token"],
        "accounting_credentials_file": str(accounting_credentials.resolve()),
        "accounting_credentials_sha256": digest(accounting_credentials),
        "accounting_credentials_mode": "0600",
        "accounting_sheets_token_source_file": str(source_tokens["google_calendar_token"].resolve()),
        "accounting_sheets_token_source_sha256": digest(source_tokens["google_calendar_token"]),
        "accounting_sheets_token_file": str(target_tokens["accounting_sheets_token"].resolve()),
        "drive_sync_token_source_file": str(source_tokens["google_calendar_token"].resolve()),
        "drive_sync_token_source_sha256": digest(source_tokens["google_calendar_token"]),
        "drive_sync_token_file": str(target_tokens["drive_sync_token"].resolve()),
        "drive_sync_write_token_source_file": str(source_tokens["google_calendar_token"].resolve()),
        "drive_sync_write_token_source_sha256": digest(source_tokens["google_calendar_token"]),
        "drive_sync_write_token_file": str(target_tokens["drive_sync_write_token"].resolve()),
        "nas_ocr_queue_db_file": str(ocr_queue.resolve()),
        "nas_ocr_queue_db_mode": f"{ocr_queue.stat().st_mode & 0o777:04o}",
        "static_external_receipt": str(static_receipt.resolve()),
        "static_external_receipt_sha256": static_receipt_sha256,
        "static_external_source_snapshot_sha256": static_report[
            "source_snapshot_sha256"
        ],
        "static_external_target_snapshot_sha256": static_report[
            "target_snapshot_sha256"
        ],
    }
    external_inputs.update(
        pre_cutover_module.named_mutable_state_paths(runtime_root)
    )
    role_modules = {
        "com.magi.v3.control": "magi_v3.control",
        "com.magi.v3.gateway": "magi_v3.gateway",
        "com.magi.v3.supervisor": "magi_v3.supervisor_service",
    }
    roles = []
    launchagents: list[Path] = []
    plist_binding_names = {
        "MAGI_ENV_FILE": "env_file",
        "MAGI_ENV_FILE_SHA256": "env_file_sha256",
        "MAGI_CRON_JOBS_FILE": "cron_jobs_file",
        "MAGI_CRON_JOBS_SHA256": "cron_jobs_sha256",
        "MAGI_CRON_JOBS_SOURCE_SHA256": "cron_jobs_source_sha256",
        "MAGI_WEBSITE_ROOT": "website_root",
        "MAGI_WEBSITE_ADMIN_SHA256": "website_admin_sha256",
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST": "python_runtime_manifest",
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256": "python_runtime_manifest_sha256",
        "MAGI_LAF_CONFIG_FILE": "laf_config_file",
        "MAGI_LAF_CONFIG_SHA256": "laf_config_sha256",
        "MAGI_CONFIG_PATH": "laf_config_file",
        "MAGI_CONFIG_SHA256": "laf_config_sha256",
        "MAGI_CONFIG_MODE": "laf_config_mode",
        "OSC_CONFIG_PATH": "laf_config_file",
        "MAGI_GOOGLE_CREDENTIALS_PATH": "google_credentials_file",
        "MAGI_GOOGLE_CREDENTIALS_SHA256": "google_credentials_sha256",
        "MAGI_GOOGLE_CREDENTIALS_MODE": "google_credentials_mode",
        "MAGI_GMAIL_CREDENTIALS_PATH": "google_credentials_file",
        "MAGI_GOOGLE_CALENDAR_TOKEN_PATH": "google_calendar_token_file",
        "MAGI_LAF_GMAIL_TOKEN_PATH": "laf_gmail_token_file",
        "MAGI_FILE_REVIEW_TOKEN_PATH": "file_review_token_file",
        "MAGI_GMAIL_COMPOSE_TOKEN_PATH": "gmail_compose_token_file",
        "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH": "accounting_credentials_file",
        "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_SHA256": "accounting_credentials_sha256",
        "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_MODE": "accounting_credentials_mode",
        "MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN": "accounting_sheets_token_file",
        "MAGI_DRIVE_SYNC_CREDENTIALS_PATH": "google_credentials_file",
        "MAGI_DRIVE_SYNC_TOKEN": "drive_sync_token_file",
        "MAGI_DRIVE_SYNC_WRITE_TOKEN": "drive_sync_write_token_file",
        "MAGI_NAS_OCR_QUEUE_DB_PATH": "nas_ocr_queue_db_file",
        "MAGI_V3_STATIC_EXTERNAL_RECEIPT": "static_external_receipt",
        "MAGI_V3_STATIC_EXTERNAL_RECEIPT_SHA256": "static_external_receipt_sha256",
        "MAGI_V3_STATIC_EXTERNAL_TARGET_SNAPSHOT_SHA256": (
            "static_external_target_snapshot_sha256"
        ),
        **{
            env_name: binding_name
            for env_name, (binding_name, _relative) in (
                pre_cutover_module.NAMED_MUTABLE_STATE_BINDINGS.items()
            )
        },
    }
    for label, module in role_modules.items():
        arguments = [executable, "-m", module]
        role = {
            "label": label,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(release.resolve()),
            "release_manifest": str(manifest.resolve()),
            "release_manifest_sha256": release_manifest_sha256,
            "pid_file": str(tmp_path / "v3" / "run" / f"{label}.pid"),
            **{
                binding_name: external_inputs[binding_name]
                for binding_name in plist_binding_names.values()
            },
        }
        roles.append(role)
        launchagent = deploy_root / "launchagents" / f"{label}.plist"
        launchagent.parent.mkdir(parents=True, exist_ok=True)
        launchagent.write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "WorkingDirectory": str(release.resolve()),
                    "ProgramArguments": arguments,
                    "EnvironmentVariables": {
                        "MAGI_V3_DEPLOYMENT_MODE": "production",
                        "MAGI_JSON_DIR": str(laf_config.parent.resolve()),
                        "MAGI_PUBLIC_SOURCE_ROOT_DIR": str(release.resolve()),
                        **{
                            plist_name: external_inputs[binding_name]
                            for plist_name, binding_name in plist_binding_names.items()
                        },
                    },
                },
                sort_keys=True,
            )
        )
        launchagents.append(launchagent)
    ownership_manifest = deploy_root / "ownership" / "ownership-manifest.json"
    write_json(
        ownership_manifest,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "release_id": "v3-final",
            "release_manifest_sha256": release_manifest_sha256,
            "deployment_mode": "production",
            "runtime_root": str(runtime_root.resolve()),
            "static_external_receipt": external_inputs["static_external_receipt"],
            "static_external_receipt_sha256": external_inputs[
                "static_external_receipt_sha256"
            ],
            "static_external_source_snapshot_sha256": external_inputs[
                "static_external_source_snapshot_sha256"
            ],
            "static_external_target_snapshot_sha256": external_inputs[
                "static_external_target_snapshot_sha256"
            ],
            "external_inputs": external_inputs,
            "roles": roles,
        },
    )
    artifact_files = [
        cron_snapshot,
        python_runtime_manifest,
        *([static_release_receipt] if static_release_receipt is not None else []),
        ownership_manifest,
        *launchagents,
    ]
    write_json(
        deploy_manifest,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "mutation_performed": False,
            "release_id": "v3-final",
            "release_manifest": str(manifest.resolve()),
            "release_manifest_sha256": release_manifest_sha256,
            "deployment_mode": "production",
            "runtime_root": str(runtime_root.resolve()),
            "static_external_receipt": external_inputs["static_external_receipt"],
            "static_external_receipt_sha256": external_inputs[
                "static_external_receipt_sha256"
            ],
            "static_external_source_snapshot_sha256": external_inputs[
                "static_external_source_snapshot_sha256"
            ],
            "static_external_target_snapshot_sha256": external_inputs[
                "static_external_target_snapshot_sha256"
            ],
            "external_inputs": external_inputs,
            "ownership_manifest": str(tmp_path / "v3" / "ownership" / "ownership-manifest.json"),
            "ownership_manifest_sha256": digest(ownership_manifest),
            "artifacts": [
                {
                    "path": path.relative_to(deploy_root).as_posix(),
                    "sha256": digest(path),
                    "size": path.stat().st_size,
                }
                for path in artifact_files
            ],
            "roles": roles,
        },
    )
    deploy_prepared_marker = deploy_root / "DEPLOY_PREPARED.json"
    write_json(
        deploy_prepared_marker,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "ready_to_install": True,
            "mutation_performed": False,
            "release_id": "v3-final",
            "deployment_mode": "production",
            "release_manifest_sha256": release_manifest_sha256,
            "ownership_manifest_sha256": digest(ownership_manifest),
            "manifest": deploy_manifest.name,
            "manifest_sha256": digest(deploy_manifest),
        },
    )

    db = tmp_path / "required" / "database.sqlite"
    state = tmp_path / "required" / "state"
    nas = tmp_path / "required" / "nas"
    db.parent.mkdir()
    db.write_bytes(b"db")
    state.mkdir()
    nas.mkdir()
    pdf_source = tmp_path / "v2" / "skills" / "pdf-namer"
    pdf_source.mkdir(parents=True)
    write_json(pdf_source / "training_data.json", [{"synthetic": "private-case-value"}])
    pdf_destination = tmp_path / "v3" / "shared" / "pdf-namer"
    pdf_handoff_manifest = tmp_path / "evidence" / "pdf-namer-handoff.json"
    precopy(pdf_source, pdf_destination, pdf_handoff_manifest, apply=True)

    mutable_source = tmp_path / "v2"
    for relative in (
        "json/processed_laf_emails.json",
        "閱卷下載/processed_emails.json",
        "閱卷下載/payment_registry.json",
        "閱卷下載/payment_proof_registry.json",
        "skills/judgment-collector/judgments.json",
    ):
        write_json(mutable_source / relative, {})
    mutable_target = tmp_path / "v3" / "shared"
    mutable_receipt = tmp_path / "evidence" / "mutable-state-dry-run.json"
    final_pre_cutover_report = tmp_path / "evidence" / "final-pre-cutover.json"
    cutover_plan = tmp_path / "evidence" / "final-cutover-plan.json"

    def path_binding(path: Path) -> dict[str, str]:
        canonical = path.resolve(strict=False)
        return {
            "path": str(canonical),
            "path_sha256": hashlib.sha256(str(canonical).encode()).hexdigest(),
        }

    release_marker = json.loads(
        (release / "RELEASE_COMPLETE.json").read_text(encoding="utf-8")
    )
    deploy_marker = json.loads(deploy_prepared_marker.read_text(encoding="utf-8"))
    write_json(
        cutover_plan,
        {
            "schema_version": 1,
            "operation": "v2_to_v3_cutover",
            "execution_purpose": "final_cutover",
            "pre_cutover_report": path_binding(final_pre_cutover_report),
            "mutable_state_handoff": {
                "source_root": path_binding(mutable_source),
                "target_shared_root": path_binding(mutable_target),
                "dry_run_receipt": path_binding(mutable_receipt),
                "prepare_receipt": path_binding(
                    tmp_path / "evidence" / "mutable-state-prepare.json"
                ),
                "staging_root": path_binding(tmp_path / "mutable-state-staging"),
                "exact_context": {
                    "release_id": release_marker["release_id"],
                    "release_manifest_sha256": release_marker["manifest_sha256"],
                    "deployment_manifest_sha256": deploy_marker["manifest_sha256"],
                },
            },
        },
    )
    cutover_plan_sha256 = digest(cutover_plan)
    pre_cutover_module.replay_mutable_state_handoff(
        action="dry-run",
        source_root=mutable_source,
        target_shared_root=mutable_target,
        receipt_path=mutable_receipt,
        context=pre_cutover_module.ExactContext(
            release_id=release_marker["release_id"],
            release_manifest_sha256=release_marker["manifest_sha256"],
            deployment_manifest_sha256=deploy_marker["manifest_sha256"],
            cutover_plan_sha256=cutover_plan_sha256,
        ),
    )
    return {
        "context": context,
        "gates": gates,
        "campaign_config": campaign_config_path,
        "campaign_report": campaign_report,
        "gate_report": gate_report,
        "backup_metadata": backup_metadata,
        "backup": backup,
        "release": release,
        "readiness_manifest": readiness_manifest,
        "deploy_prepared_marker": deploy_prepared_marker,
        "deploy_manifest": deploy_manifest,
        "db": db,
        "state": state,
        "nas": nas,
        "v2": tmp_path / "v2",
        "v3": tmp_path / "v3",
        "pdf_source": pdf_source,
        "pdf_destination": pdf_destination,
        "pdf_handoff_manifest": pdf_handoff_manifest,
        "cutover_plan": cutover_plan,
        "cutover_plan_sha256": cutover_plan_sha256,
        "mutable_source": mutable_source,
        "mutable_target": mutable_target,
        "mutable_receipt": mutable_receipt,
        "final_pre_cutover_report": final_pre_cutover_report,
        "legacy_v2": legacy_v2,
    }


def make_preflight(data, **overrides):
    values = dict(
        context=data["context"],
        gate_config_path=data["gates"],
        campaign_config_path=data["campaign_config"],
        campaign_report_path=data["campaign_report"],
        release_gate_report_path=data["gate_report"],
        backup_metadata_path=data["backup_metadata"],
        readiness_manifest_path=data["readiness_manifest"],
        deploy_prepared_marker_path=data["deploy_prepared_marker"],
        pdf_namer_handoff_manifest_path=data["pdf_handoff_manifest"],
        pdf_namer_source_path=data["pdf_source"],
        pdf_namer_destination_path=data["pdf_destination"],
        release_dir=data["release"],
        required_paths=RequiredPaths((data["db"],), (data["state"],), (data["nas"],)),
        v2_root=data["v2"],
        v3_root=data["v3"],
        clock=lambda: NOW,
        mount_checker=lambda path: True,
        disk_usage=lambda path: Disk(200 * 1024**3, 1, 199 * 1024**3),
        snapshot_collector=lambda: safe_snapshot(
            release="v2" if data["legacy_v2"] else "v3"
        ),
    )
    if overrides.get("execution_purpose", "final_cutover") == "final_cutover":
        values.update(
            cutover_plan_path=data["cutover_plan"],
            cutover_plan_sha256=data["cutover_plan_sha256"],
            mutable_state_source_root=data["mutable_source"],
            mutable_state_target_shared_root=data["mutable_target"],
            mutable_state_dry_run_receipt_path=data["mutable_receipt"],
            report_output_path=data["final_pre_cutover_report"],
        )
    values.update(overrides)
    return PreCutoverPreflight(**values)


def run_preflight(data, **overrides):
    return make_preflight(data, **overrides).run()


def rehash_deployment(data) -> None:
    deployment = json.loads(data["deploy_manifest"].read_text(encoding="utf-8"))
    deploy_root = data["deploy_manifest"].parent
    ownership = deploy_root / "ownership" / "ownership-manifest.json"
    deployment["ownership_manifest_sha256"] = digest(ownership)
    for artifact in deployment["artifacts"]:
        path = deploy_root / artifact["path"]
        artifact.update({"sha256": digest(path), "size": path.stat().st_size})
    write_json(data["deploy_manifest"], deployment)
    marker = json.loads(data["deploy_prepared_marker"].read_text(encoding="utf-8"))
    marker["deployment_mode"] = deployment["deployment_mode"]
    marker["ownership_manifest_sha256"] = deployment["ownership_manifest_sha256"]
    marker["manifest_sha256"] = digest(data["deploy_manifest"])
    write_json(data["deploy_prepared_marker"], marker)


def test_all_read_only_attestations_are_machine_readable_go(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    report = run_preflight(data)

    assert report["decision"] == "GO"
    assert report["gaps"] == []
    assert report["mutation_performed"] is False
    assert report["network_access_performed"] is False
    assert report["backup_performed"] is False
    assert report["restore_performed"] is False
    check_names = {row["name"] for row in report["checks"]}
    assert "mutable_state_handoff" not in check_names
    assert "pdf_namer_handoff_precopy" not in check_names
    assert "previous_v3_only_ownership" in check_names
    assert "cutover_plan" not in report


def test_final_cutover_cannot_omit_mutable_state_plan_evidence(tmp_path: Path) -> None:
    data = fixture(tmp_path, legacy_v2=True)
    report = run_preflight(
        data,
        cutover_plan_path=None,
        cutover_plan_sha256=None,
        mutable_state_source_root=None,
        mutable_state_target_shared_root=None,
        mutable_state_dry_run_receipt_path=None,
        report_output_path=None,
    )

    assert report["decision"] == "NO_GO"
    assert "mutable_state_handoff" in report["gaps"]


def test_atomic_drill_exemption_is_explicit_and_not_a_final_exemption(
    tmp_path: Path,
) -> None:
    data = fixture(tmp_path)
    gates = json.loads(data["gates"].read_text(encoding="utf-8"))
    excluded = list(pre_cutover_module.ATOMIC_DRILL_EXCLUDED_EVIDENCE)
    gate = json.loads(data["gate_report"].read_text(encoding="utf-8"))
    gate.update(
        {
            "decision": "NO_GO",
            "passed": [
                item for item in gates["required_evidence"] if item not in excluded
            ],
            "missing": excluded,
        }
    )
    write_json(data["gate_report"], gate)

    report = run_preflight(data, execution_purpose="atomic_drill")

    assert report["decision"] == "GO_FOR_CUTOVER_DRILL_ONLY"
    assert "mutable_state_handoff" not in {
        row["name"] for row in report["checks"]
    }


@pytest.mark.parametrize(
    "tamper",
    [
        "plan_hash",
        "receipt_context",
        "allowlist",
        "business_payload",
        "not_ready",
        "source_drift",
        "source_binding",
    ],
)
def test_mutable_state_final_gate_rejects_tamper_and_source_drift_without_payload(
    tmp_path: Path,
    tamper: str,
) -> None:
    data = fixture(tmp_path, legacy_v2=True)
    overrides = {}
    secret = "private-party-state-must-not-leak"
    if tamper == "plan_hash":
        overrides["cutover_plan_sha256"] = "0" * 64
    elif tamper == "source_drift":
        write_json(
            data["mutable_source"] / "json" / "processed_laf_emails.json",
            {"synthetic": secret},
        )
    elif tamper == "source_binding":
        other = tmp_path / "other-source"
        other.mkdir()
        overrides["mutable_state_source_root"] = other
    else:
        receipt = json.loads(data["mutable_receipt"].read_text(encoding="utf-8"))
        if tamper == "receipt_context":
            receipt["exact_context"]["cutover_plan_sha256"] = "0" * 64
        elif tamper == "allowlist":
            receipt["allowlist_sha256"] = "0" * 64
        elif tamper == "business_payload":
            receipt["contains_business_payload"] = True
            receipt["unexpected_payload"] = secret
        else:
            receipt["ready"] = False
        write_json(data["mutable_receipt"], receipt)
        data["mutable_receipt"].chmod(0o600)

    report = run_preflight(data, **overrides)

    assert report["decision"] == "NO_GO"
    assert "mutable_state_handoff" in report["gaps"]
    assert secret not in json.dumps(report, ensure_ascii=False)


def test_atomic_drill_only_gate_cannot_be_reused_for_final_cutover(tmp_path: Path) -> None:
    data = fixture(tmp_path, legacy_v2=True)
    gates = json.loads(data["gates"].read_text(encoding="utf-8"))
    excluded = [
        "atomic_release_switch_and_cold_rollback_drill_passed",
        "human_go_approval_recorded",
    ]
    gate = json.loads(data["gate_report"].read_text(encoding="utf-8"))
    gate.update(
        {
            "decision": "NO_GO",
            "passed": [item for item in gates["required_evidence"] if item not in excluded],
            "missing": excluded,
            "failed": [],
            "invalid": {},
        }
    )
    write_json(data["gate_report"], gate)

    drill = run_preflight(data, execution_purpose="atomic_drill")
    final = run_preflight(data, execution_purpose="final_cutover")

    assert drill["decision"] == "GO_FOR_CUTOVER_DRILL_ONLY"
    assert drill["gate_stage"] == "cutover_drill_12_of_14"
    assert drill["excluded_evidence"] == excluded
    assert final["decision"] == "NO_GO"
    assert "release_gate_evidence" in final["gaps"]


def test_isolated_deployment_is_legal_for_atomic_drill_but_not_final_cutover(
    tmp_path: Path,
) -> None:
    data = fixture(tmp_path, legacy_v2=True)
    deployment = json.loads(data["deploy_manifest"].read_text(encoding="utf-8"))
    deployment["deployment_mode"] = "isolated_live_validation"
    write_json(data["deploy_manifest"], deployment)
    ownership_path = data["deploy_manifest"].parent / "ownership" / "ownership-manifest.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    ownership["deployment_mode"] = "isolated_live_validation"
    write_json(ownership_path, ownership)
    for plist_path in (data["deploy_manifest"].parent / "launchagents").glob("*.plist"):
        plist = plistlib.loads(plist_path.read_bytes())
        plist["EnvironmentVariables"]["MAGI_V3_DEPLOYMENT_MODE"] = (
            "isolated_live_validation"
        )
        plist_path.write_bytes(plistlib.dumps(plist, sort_keys=True))
    rehash_deployment(data)

    gates = json.loads(data["gates"].read_text(encoding="utf-8"))
    excluded = list(pre_cutover_module.ATOMIC_DRILL_EXCLUDED_EVIDENCE)
    gate = json.loads(data["gate_report"].read_text(encoding="utf-8"))
    gate.update(
        {
            "decision": "NO_GO",
            "passed": [
                item for item in gates["required_evidence"] if item not in excluded
            ],
            "missing": excluded,
            "failed": [],
            "invalid": {},
        }
    )
    write_json(data["gate_report"], gate)

    drill = run_preflight(data, execution_purpose="atomic_drill")
    final = run_preflight(data, execution_purpose="final_cutover")

    assert "v3_deploy_prepared" not in drill["gaps"]
    assert drill["decision"] == "GO_FOR_CUTOVER_DRILL_ONLY"
    assert "v3_deploy_prepared" in final["gaps"]


@pytest.mark.parametrize(
    "tamper",
    ["cron_snapshot", "website_admin", "python_runtime_manifest", "ownership", "plist"],
)
def test_pre_cutover_revalidates_external_deployment_bindings(
    tmp_path: Path,
    tamper: str,
) -> None:
    data = fixture(tmp_path, legacy_v2=True)
    deploy_root = data["deploy_manifest"].parent
    if tamper == "cron_snapshot":
        (deploy_root / "runtime-inputs" / "cron_jobs.v3.json").write_text(
            "[]\n", encoding="utf-8"
        )
    elif tamper == "website_admin":
        (
            tmp_path
            / "v3"
            / "shared"
            / "external"
            / "website"
            / "admin"
            / "admin_server.py"
        ).write_text("raise RuntimeError\n", encoding="utf-8")
    elif tamper == "python_runtime_manifest":
        (deploy_root / "runtime-inputs" / "python-runtime-manifest.json").write_text(
            "{}\n", encoding="utf-8"
        )
    elif tamper == "ownership":
        ownership_path = deploy_root / "ownership" / "ownership-manifest.json"
        ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        ownership["external_inputs"]["website_admin_sha256"] = "0" * 64
        write_json(ownership_path, ownership)
    else:
        plist_path = deploy_root / "launchagents" / "com.magi.v3.gateway.plist"
        plist = plistlib.loads(plist_path.read_bytes())
        plist["EnvironmentVariables"]["MAGI_CRON_JOBS_SHA256"] = "0" * 64
        plist_path.write_bytes(plistlib.dumps(plist, sort_keys=True))
    rehash_deployment(data)

    report = run_preflight(data)

    assert "v3_deploy_prepared" in report["gaps"]


@pytest.mark.parametrize("target_kind", ["wrong_shared", "release"])
def test_pre_cutover_rejects_internally_consistent_nonexact_named_state_binding(
    tmp_path: Path,
    target_kind: str,
) -> None:
    data = fixture(tmp_path)
    deploy_root = data["deploy_manifest"].parent
    deployment = json.loads(data["deploy_manifest"].read_text(encoding="utf-8"))
    ownership_path = deploy_root / "ownership" / "ownership-manifest.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    target = (
        data["release"] / "payment_registry.json"
        if target_kind == "release"
        else tmp_path / "v3" / "shared" / "file-review" / "downloads" / "wrong.json"
    ).resolve()
    binding_name = "payment_registry_path"
    env_name = "MAGI_PAYMENT_REGISTRY_PATH"
    deployment["external_inputs"][binding_name] = str(target)
    ownership["external_inputs"][binding_name] = str(target)
    for role in deployment["roles"]:
        role[binding_name] = str(target)
    for role in ownership["roles"]:
        role[binding_name] = str(target)
    for plist_path in (deploy_root / "launchagents").glob("*.plist"):
        plist = plistlib.loads(plist_path.read_bytes())
        plist["EnvironmentVariables"][env_name] = str(target)
        plist_path.write_bytes(plistlib.dumps(plist, sort_keys=True))
    write_json(ownership_path, ownership)
    write_json(data["deploy_manifest"], deployment)
    rehash_deployment(data)

    report = run_preflight(data)

    assert "v3_deploy_prepared" in report["gaps"]


def test_missing_pdf_namer_precopy_evidence_blocks_cutover_without_case_data(tmp_path: Path) -> None:
    data = fixture(tmp_path, legacy_v2=True)
    data["pdf_handoff_manifest"].unlink()

    report = run_preflight(data)

    assert report["decision"] == "NO_GO"
    assert "pdf_namer_handoff_precopy" in report["gaps"]
    encoded = json.dumps(report, ensure_ascii=False)
    assert "private-case-value" not in encoded


def test_pdf_namer_source_may_keep_learning_after_precopy_until_v2_zero(tmp_path: Path) -> None:
    data = fixture(tmp_path, legacy_v2=True)
    write_json(
        data["pdf_source"] / "training_data.json",
        [{"synthetic": "private-case-value"}, {"synthetic": "new-live-learning"}],
    )

    report = run_preflight(data)

    assert report["decision"] == "GO"
    assert "pdf_namer_handoff_precopy" not in report["gaps"]


@pytest.mark.parametrize("tamper", ["bytes", "extra", "symlink", "hardlink", "mode"])
def test_pdf_namer_precopy_destination_must_be_complete_private_and_untampered(
    tmp_path: Path,
    tamper: str,
) -> None:
    data = fixture(tmp_path, legacy_v2=True)
    target = data["pdf_destination"] / "training_data.json"
    if tamper == "bytes":
        target.write_text("[]", encoding="utf-8")
        target.chmod(0o600)
    elif tamper == "extra":
        extra = data["pdf_destination"] / "unexpected.json"
        extra.write_text("{}", encoding="utf-8")
        extra.chmod(0o600)
    elif tamper == "symlink":
        outside = tmp_path / "symlink-target.json"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
    elif tamper == "hardlink":
        outside = tmp_path / "hardlink-target.json"
        outside.write_bytes(target.read_bytes())
        outside.chmod(0o600)
        target.unlink()
        os.link(outside, target)
    else:
        target.chmod(0o644)

    report = run_preflight(data)

    assert report["decision"] == "NO_GO"
    assert "pdf_namer_handoff_precopy" in report["gaps"]
    assert "private-case-value" not in json.dumps(report, ensure_ascii=False)


def test_prepared_v3_ownership_probe_uses_immutable_release_before_install(tmp_path: Path) -> None:
    data = fixture(tmp_path)

    spec = make_preflight(data)._prepared_v3_release_spec()

    # A private pdf-namer precopy may create runtime/shared before launchd
    # installation; ownership must still be probed from immutable release.
    assert data["pdf_destination"].exists()
    assert spec.root == data["release"].resolve()
    assert spec.launchd_labels == (
        "com.magi.v3.control",
        "com.magi.v3.gateway",
        "com.magi.v3.supervisor",
    )
    assert all(path.is_file() for path in spec.launchd_plists.values())


def test_atomic_pre_cutover_report_output_replaces_complete_document(tmp_path: Path) -> None:
    output = tmp_path / "reports" / "pre-cutover.json"
    _write_json_atomic(output, {"decision": "NO_GO", "gaps": ["window"]})
    _write_json_atomic(output, {"decision": "GO", "gaps": []})

    assert json.loads(output.read_text()) == {"decision": "GO", "gaps": []}
    assert list(output.parent.glob(f".{output.name}.tmp-*")) == []


def test_atomic_pre_cutover_output_refuses_symlink_target_and_parent(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text('{"decision":"NO_GO"}', encoding="utf-8")
    symlink = tmp_path / "output.json"
    symlink.symlink_to(real)

    with pytest.raises(Exception, match="symlink"):
        _write_json_atomic(symlink, {"decision": "GO"})
    assert json.loads(real.read_text()) == {"decision": "NO_GO"}

    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked-dir"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(Exception, match="symlink"):
        _write_json_atomic(linked_dir / "report.json", {"decision": "GO"})
    assert not (real_dir / "report.json").exists()


def test_atomic_pre_cutover_output_removes_go_after_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report.json"
    original_fsync = pre_cutover_module.os.fsync
    calls = 0

    def fail_first_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(pre_cutover_module.os, "fsync", fail_first_directory_fsync)

    with pytest.raises(OSError, match="directory fsync"):
        _write_json_atomic(output, {"decision": "GO"})
    assert not output.exists()


def test_atomic_pre_cutover_output_invalidates_old_go_before_temp_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report.json"
    output.write_text('{"decision":"GO"}', encoding="utf-8")
    original_fsync = pre_cutover_module.os.fsync
    calls = 0

    def fail_temp_file_fsync_after_invalidation(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected temp file fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(
        pre_cutover_module.os,
        "fsync",
        fail_temp_file_fsync_after_invalidation,
    )

    with pytest.raises(OSError, match="temp file fsync"):
        _write_json_atomic(output, {"decision": "NO_GO"})
    assert not output.exists()


def test_atomic_pre_cutover_output_uses_owned_unique_temp(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    stale = output.with_name(f".{output.name}.tmp")
    stale.write_text("unowned", encoding="utf-8")

    _write_json_atomic(output, {"decision": "NO_GO"})

    assert stale.read_text() == "unowned"
    assert json.loads(output.read_text()) == {"decision": "NO_GO"}


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, str(ROOT / "scripts" / "v3_pre_cutover.py"), "--help"],
        [sys.executable, "-m", "scripts.v3_pre_cutover", "--help"],
    ],
)
def test_help_works_for_direct_and_module_execution_without_running_preflight(command: list[str]) -> None:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=10)

    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "Traceback" not in result.stderr


def test_missing_release_marker_is_no_go(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    data["release"].chmod(0o755)
    (data["release"] / "RELEASE_COMPLETE.json").unlink()
    report = run_preflight(data)
    assert report["decision"] == "NO_GO"
    assert "v3_release_marker_manifest" in report["gaps"]


@pytest.mark.parametrize(
    "tamper",
    ["extra_file", "extra_directory", "symlink", "file_mode", "directory_mode"],
)
def test_release_gate_requires_exact_symlink_free_immutable_tree(
    tmp_path: Path,
    tamper: str,
) -> None:
    data = fixture(tmp_path)
    release = data["release"]
    payload = release / "payload" / "magi.py"
    if tamper == "extra_file":
        release.chmod(0o755)
        extra = release / "unexpected.txt"
        extra.write_text("unexpected", encoding="utf-8")
        extra.chmod(0o444)
        release.chmod(0o555)
    elif tamper == "extra_directory":
        release.chmod(0o755)
        extra = release / "unexpected-directory"
        extra.mkdir()
        extra.chmod(0o555)
        release.chmod(0o555)
    elif tamper == "symlink":
        release.chmod(0o755)
        (release / "payload-link").symlink_to(payload)
        release.chmod(0o555)
    elif tamper == "file_mode":
        payload.chmod(0o644)
    else:
        payload.parent.chmod(0o755)

    report = run_preflight(data)

    assert report["decision"] == "NO_GO"
    assert "v3_release_marker_manifest" in report["gaps"]


def test_restore_must_have_been_actually_drilled_and_hash_bound(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    metadata = json.loads(data["backup_metadata"].read_text())
    metadata["restore_drill"]["actual_restore_performed"] = False
    write_json(data["backup_metadata"], metadata)
    report = run_preflight(data)
    assert "backup_and_restore_drill" in report["gaps"]


def test_sqlite_only_backup_without_website_data_assets_is_no_go(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    metadata = json.loads(data["backup_metadata"].read_text())
    metadata["coverage"] = ["sqlite"]
    metadata["restore_drill"]["verified_scopes"] = ["sqlite"]
    write_json(data["backup_metadata"], metadata)

    report = run_preflight(data)

    assert "backup_and_restore_drill" in report["gaps"]


def test_backup_or_release_tamper_is_no_go(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    data["backup"].write_bytes(b"tampered")
    report = run_preflight(data)
    assert "backup_and_restore_drill" in report["gaps"]


def test_symlinked_backup_artifact_is_no_go(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    original = data["backup"].with_suffix(".original")
    data["backup"].rename(original)
    data["backup"].symlink_to(original)

    report = run_preflight(data)

    assert "backup_and_restore_drill" in report["gaps"]


@pytest.mark.parametrize(
    "snapshot",
    [safe_snapshot(ambiguous=True), safe_snapshot(errors=("stale pidfile v2.pid",))],
)
def test_ambiguous_owner_or_stale_pidfile_is_no_go(tmp_path: Path, snapshot: Snapshot) -> None:
    data = fixture(tmp_path)
    report = run_preflight(data, snapshot_collector=lambda: snapshot)
    assert "previous_v3_only_ownership" in report["gaps"]


def test_noncertifying_campaign_and_no_go_release_gate_are_rejected(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    campaign = json.loads(data["campaign_report"].read_text())
    campaign.update({"decision": "NO_GO", "certifying": False, "offline_complete": False})
    write_json(data["campaign_report"], campaign)
    gate = json.loads(data["gate_report"].read_text())
    gate["decision"] = "NO_GO"
    write_json(data["gate_report"], gate)
    report = run_preflight(data)
    assert "campaign_evidence" in report["gaps"]
    assert "release_gate_evidence" in report["gaps"]


def test_stale_campaign_report_is_no_go(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    campaign = json.loads(data["campaign_report"].read_text())
    campaign["generated_at"] = (NOW - timedelta(hours=25)).isoformat()
    write_json(data["campaign_report"], campaign)

    report = run_preflight(data)

    assert "campaign_evidence" in report["gaps"]


def test_readiness_requires_every_required_surface_and_consistent_summary(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    readiness = json.loads(data["readiness_manifest"].read_text())
    readiness["surfaces"][0].update({"tested": False, "status": "implemented"})
    readiness["summary"]["tested"] = 2
    write_json(data["readiness_manifest"], readiness)

    report = run_preflight(data)

    assert "v3_readiness_manifest" in report["gaps"]


def test_current_repository_readiness_manifest_passes_surface_gate(tmp_path: Path) -> None:
    data = fixture(tmp_path)

    report = run_preflight(data, readiness_manifest_path=ROOT / "config" / "v3_pre_cutover_readiness.json")

    assert "v3_readiness_manifest" not in report["gaps"]


@pytest.mark.parametrize("tamper", ["release_id", "manifest_hash", "mutation"])
def test_deploy_prepared_must_be_hash_and_release_bound(tmp_path: Path, tamper: str) -> None:
    data = fixture(tmp_path)
    marker = json.loads(data["deploy_prepared_marker"].read_text())
    if tamper == "release_id":
        marker["release_id"] = "wrong-release"
        write_json(data["deploy_prepared_marker"], marker)
    elif tamper == "manifest_hash":
        data["deploy_manifest"].write_text("tampered", encoding="utf-8")
    else:
        marker["mutation_performed"] = True
        write_json(data["deploy_prepared_marker"], marker)

    report = run_preflight(data)

    assert "v3_deploy_prepared" in report["gaps"]


def test_prepared_deployment_cannot_point_at_byte_identical_release_copy(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    copied_release = tmp_path / "release-copy"
    copied_release.mkdir()
    for source in data["release"].rglob("*"):
        destination = copied_release / source.relative_to(data["release"])
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())

    preflight = make_preflight(data, release_dir=copied_release)

    with pytest.raises(PreCutoverError, match="another release path"):
        preflight._prepared_v3_release_spec()
    assert "v3_deploy_prepared" in preflight.run()["gaps"]


def test_hash_bound_but_invalid_plist_is_rejected(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    label = "com.magi.v3.gateway"
    plist = data["deploy_manifest"].parent / "launchagents" / f"{label}.plist"
    plist.write_text("not a plist", encoding="utf-8")
    deployment = json.loads(data["deploy_manifest"].read_text())
    plist_artifact = next(
        row
        for row in deployment["artifacts"]
        if row["path"] == f"launchagents/{label}.plist"
    )
    plist_artifact.update(
        {"sha256": digest(plist), "size": plist.stat().st_size}
    )
    write_json(data["deploy_manifest"], deployment)
    marker = json.loads(data["deploy_prepared_marker"].read_text())
    marker["manifest_sha256"] = digest(data["deploy_manifest"])
    write_json(data["deploy_prepared_marker"], marker)

    preflight = make_preflight(data)

    with pytest.raises(PreCutoverError, match="plist is invalid"):
        preflight._prepared_v3_release_spec()
    assert "v3_deploy_prepared" in preflight.run()["gaps"]


def test_outside_cutover_window_is_no_go(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    outside = NOW + timedelta(hours=5)
    report = run_preflight(data, clock=lambda: outside)
    assert "cutover_window" in report["gaps"]


def test_missing_required_paths_low_disk_and_unmounted_nas_are_no_go(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    data["db"].unlink()
    report = run_preflight(
        data,
        mount_checker=lambda path: False,
        disk_usage=lambda path: Disk(10 * 1024**3, 1, 9 * 1024**3),
    )
    assert {"database_paths", "nas_mounts", "disk_free"} <= set(report["gaps"])


def test_release_bound_disk_capacity_replaces_fixed_80_gib_false_blocker(
    tmp_path: Path,
) -> None:
    data = fixture(tmp_path)
    report = run_preflight(
        data,
        disk_usage=lambda _path: Disk(200 * 1024**3, 183 * 1024**3, 17 * 1024**3),
    )

    assert "disk_capacity_policy" not in report["gaps"]
    assert "disk_free" not in report["gaps"]
    detail = next(
        row["detail"] for row in report["checks"] if row["name"] == "disk_free"
    )
    assert detail["policy"] == "release_bound_capacity"
    assert detail["absolute_floor_gib"] == 16.0
    assert detail["operational_headroom_gib"] == 8.0
    assert detail["material_multiplier"] == 4.0
    assert detail["material_bytes"] > 0
    assert detail["minimum_gb"] == 16.0


def test_release_bound_disk_capacity_still_fails_below_absolute_floor(
    tmp_path: Path,
) -> None:
    data = fixture(tmp_path)
    report = run_preflight(
        data,
        disk_usage=lambda _path: Disk(200 * 1024**3, 185 * 1024**3, 15 * 1024**3),
    )

    assert "disk_free" in report["gaps"]


@pytest.mark.parametrize(
    "change",
    [
        {"absolute_floor_gib": 15},
        {"operational_headroom_gib": 7},
        {"material_multiplier": 3},
        {"material_scope": ["candidate_release"]},
    ],
)
def test_release_bound_disk_capacity_policy_cannot_be_silently_weakened(
    tmp_path: Path,
    change: dict,
) -> None:
    data = fixture(tmp_path)
    preflight = make_preflight(data)
    policy = json.loads(data["gates"].read_text())["disk_capacity_policy"]
    policy.update(change)

    with pytest.raises(PreCutoverError, match="capacity policy"):
        preflight._disk_capacity_requirement(policy)


def test_unarmed_campaign_configuration_is_no_go(tmp_path: Path) -> None:
    data = fixture(tmp_path)
    config = json.loads(data["campaign_config"].read_text())
    config["armed"] = False
    write_json(data["campaign_config"], config)
    report = run_preflight(data)
    assert "campaign_configuration" in report["gaps"]
