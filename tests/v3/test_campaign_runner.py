from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.v3_campaign.runner as campaign_runner_module
from scripts.v3_campaign.offline_probes import bound_cron_jobs
from scripts.v3_campaign.runner import (
    COMPLETION_MARKER,
    MANIFEST_NAME,
    OFFLINE_COMMANDS,
    CampaignContext,
    CampaignRunner,
    CampaignSafetyError,
)
from scripts.v3_python_runtime_snapshot import build_runtime_manifest

SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_CONFIG = SOURCE_ROOT / "config" / "v3_validation_campaign.json"
SOURCE_GATES = SOURCE_ROOT / "config" / "v3_cutover_gates.json"
SOURCE_SCHEDULE_BASELINE = SOURCE_ROOT / "config" / "v3_schedule_realism_baseline.json"
SOURCE_CRON_JOBS = SOURCE_ROOT / "cron_jobs.json"
SOURCE_LAUNCHER = SOURCE_ROOT / "bin" / "magi-v3-python"
SOURCE_RUNTIME_SNAPSHOT = SOURCE_ROOT / "scripts" / "v3_python_runtime_snapshot.py"
ROUTE_CERT_RELEASE_SOURCES = (
    "docs/architecture/v3/generated/v2_runtime_routes.json",
    "scripts/v3_validation/actual_route_replay.py",
    "scripts/v3_validation/route-method-review.json",
    "scripts/v3_validation/route-method-review-supplement.json",
    "scripts/v3_validation/route-success-proof-review.json",
    "scripts/v3_validation/route_certification.py",
    "scripts/v3_validation/route_success_trace_plugin.py",
)
HEALTH_CERT_RELEASE_SOURCES = (
    "magi_v3/health.py",
    "scripts/v3_validation/health_certification.py",
)
SCHEDULE_CERT_RELEASE_SOURCES = (
    "config/v3_schedule_dispatch_policy.json",
    "config/v3_schedule_body_adapter_registry.json",
    "config/v3_schedule_realism_baseline.json",
    "scripts/v3_validation/schedule_capacity_certification.py",
    "scripts/v3_validation/schedule_body_registry.py",
)
FAULT_CERT_RELEASE_SOURCES = (
    "scripts/v3_validation/fault_certification.py",
    "scripts/v3_validation/fault_realism.py",
)
QUALITY_CERT_RELEASE_SOURCES = (
    "config/v3_release_quality_suites.json",
    "scripts/v3_validation/golden_flows.py",
    "scripts/v3_validation/pytest_transcript_plugin.py",
    "scripts/v3_validation/release_quality_certification.py",
    "scripts/v3_validation/release_quality_evidence.py",
    "scripts/v3_validation/side_effects.py",
    "tests/v3/compat/behavior_fixtures/osc-file-content.json",
)
RESOURCE_PERF_RELEASE_SOURCES = (
    "config/v3_resource_policy.json",
    "config/v3_service_manifest.json",
    "magi_v3/dispatcher.py",
    "magi_v3/ledger.py",
    "magi_v3/macos_resources.py",
    "magi_v3/resource.py",
    "magi_v3/supervisor.py",
    "scripts/v3_validation/perf_compat.py",
    "scripts/v3_validation/perf_certification.py",
    "scripts/v3_validation/isolated_resource_window.py",
    "scripts/v3_validation/isolated_resource_window_collector.py",
    "scripts/v3_validation/isolated_resource_window_plan_builder.py",
    "scripts/v3_validation/g8_isolated_smb.py",
    "scripts/v3_validation/resource_window_core_adapter.py",
    "scripts/v3_validation/resource_window_model_adapter.py",
    "scripts/v3_validation/resource_performance_certification.py",
    "scripts/v3_validation/resource_performance_evidence.py",
)


def _schedule_fixture_ids() -> tuple[list[str], list[str]]:
    baseline = json.loads(SOURCE_SCHEDULE_BASELINE.read_text(encoding="utf-8"))
    cron_jobs, _cron_sha256 = bound_cron_jobs(SOURCE_ROOT)
    allowlisted = sorted(
        str(item["job_id"]) for item in baseline["representative_body_allowlist"]
    )
    enabled = {
        str(item["id"]) for item in cron_jobs if item.get("enabled") is True
    }
    return allowlisted, sorted(enabled - set(allowlisted))


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def create_release(
    tmp_path: Path,
    *,
    armed: bool = False,
    add_unknown_workload: bool = False,
    armed_with_blockers: bool = False,
) -> tuple[Path, str]:
    root = tmp_path / "release"
    config = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    if not armed and not armed_with_blockers:
        config["campaign_state"] = "ready_unarmed"
        config["armed"] = False
        config["offline_campaign"]["harness_certification"]["status"] = "blocked"
        config["offline_campaign"]["harness_certification"]["arming_blockers"] = list(
            config["offline_campaign"]["harness_certification"][
                "historical_arming_blockers"
            ]
        )
    if armed:
        config["campaign_state"] = "certifying_offline"
        config["armed"] = True
        config["offline_campaign"]["harness_certification"] = {
            "status": "certified",
            "arming_blockers": [],
        }
    if armed_with_blockers:
        config["campaign_state"] = "certifying_offline"
        config["armed"] = True
        config["offline_campaign"]["harness_certification"]["arming_blockers"] = list(
            config["offline_campaign"]["harness_certification"][
                "historical_arming_blockers"
            ]
        )
    if add_unknown_workload:
        config["offline_campaign"]["workloads"].append("launch_production")

    files: dict[str, tuple[bytes, int]] = {
        "bin/magi-v3-python": (b"#!/bin/sh\nexit 99\n", 0o555),
        "config/v3_validation_campaign.json": (_json_bytes(config), 0o444),
        "config/v3_cutover_gates.json": (SOURCE_GATES.read_bytes(), 0o444),
    }
    for command in OFFLINE_COMMANDS.values():
        for target in (
            argument
            for argument in command
            if argument.startswith(("tests/", "scripts/"))
        ):
            files[target] = (f"# offline fixture: {target}\n".encode(), 0o444)
    for relative in ROUTE_CERT_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    for relative in HEALTH_CERT_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    for relative in SCHEDULE_CERT_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    for relative in FAULT_CERT_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    quality_manifest = json.loads(
        (SOURCE_ROOT / "config/v3_release_quality_suites.json").read_text(encoding="utf-8")
    )
    for relative in QUALITY_CERT_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    for relative in RESOURCE_PERF_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    quality_tests = {
        path
        for section in (
            quality_manifest["v3_suites"],
            quality_manifest["quality_contract_groups"],
            quality_manifest["golden_sets"],
        )
        for rows in section.values()
        for path in rows
    } | set(quality_manifest["side_effect_test_targets"])
    for relative in sorted(quality_tests):
        files.setdefault(relative, (f"# quality fixture: {relative}\n".encode(), 0o444))
    entries = []
    for relative, (data, mode) in sorted(files.items()):
        _write(root / relative, data, mode)
        entries.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": f"{mode:04o}",
            }
        )
    snapshot_sha = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "release_id": "v3-campaign-fixture",
        "commit": "a" * 40,
        "generated_at": "2026-07-14T00:00:00+00:00",
        "immutable": True,
        "release_sha256": snapshot_sha,
        "source_snapshot_sha256": snapshot_sha,
        "source_file_count": len(entries),
        "source_allowlist": sorted(files),
        "excluded_components": [],
        "excluded_mutable_files": [],
        "files": entries,
    }
    manifest_bytes = _json_bytes(manifest)
    _write(root / MANIFEST_NAME, manifest_bytes)
    marker = {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "commit": manifest["commit"],
        "completed_at": manifest["generated_at"],
        "manifest": MANIFEST_NAME,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "release_sha256": snapshot_sha,
        "source_snapshot_sha256": snapshot_sha,
        "source_file_count": len(entries),
    }
    _write(root / COMPLETION_MARKER, _json_bytes(marker))
    return root, snapshot_sha


def create_real_launcher_release(tmp_path: Path) -> tuple[Path, str]:
    """Build the minimum immutable campaign release accepted by the real launcher."""

    root = tmp_path / "sealed-release"
    files: dict[str, tuple[bytes, int]] = {
        "bin/magi-v3-python": (SOURCE_LAUNCHER.read_bytes(), 0o555),
        "scripts/v3_python_runtime_snapshot.py": (
            SOURCE_RUNTIME_SNAPSHOT.read_bytes(),
            0o555,
        ),
        "config/v3_validation_campaign.json": (SOURCE_CONFIG.read_bytes(), 0o444),
        "config/v3_cutover_gates.json": (SOURCE_GATES.read_bytes(), 0o444),
    }
    for command in OFFLINE_COMMANDS.values():
        for target in (
            argument
            for argument in command
            if argument.startswith(("tests/", "scripts/"))
        ):
            files[target] = (f"# offline fixture: {target}\n".encode(), 0o444)
    for relative in ROUTE_CERT_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    for relative in HEALTH_CERT_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    for relative in FAULT_CERT_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    quality_manifest = json.loads(
        (SOURCE_ROOT / "config/v3_release_quality_suites.json").read_text(encoding="utf-8")
    )
    for relative in QUALITY_CERT_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    for relative in RESOURCE_PERF_RELEASE_SOURCES:
        files[relative] = ((SOURCE_ROOT / relative).read_bytes(), 0o444)
    quality_tests = {
        path
        for section in (
            quality_manifest["v3_suites"],
            quality_manifest["quality_contract_groups"],
            quality_manifest["golden_sets"],
        )
        for rows in section.values()
        for path in rows
    } | set(quality_manifest["side_effect_test_targets"])
    for relative in sorted(quality_tests):
        files.setdefault(relative, (f"# quality fixture: {relative}\n".encode(), 0o444))
    entries = []
    for relative, (data, mode) in sorted(files.items()):
        _write(root / relative, data, mode)
        entries.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": f"{mode:04o}",
            }
        )
    snapshot_sha = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "release_id": "v3-real-launcher-fixture",
        "commit": "b" * 40,
        "generated_at": "2026-07-14T00:00:00+00:00",
        "immutable": True,
        "release_sha256": snapshot_sha,
        "source_snapshot_sha256": snapshot_sha,
        "source_file_count": len(entries),
        "source_allowlist": sorted(files),
        "excluded_components": [],
        "excluded_mutable_files": [],
        "files": entries,
    }
    manifest_bytes = _json_bytes(manifest)
    _write(root / MANIFEST_NAME, manifest_bytes, 0o444)
    marker = {
        "schema_version": 1,
        "release_id": manifest["release_id"],
        "commit": manifest["commit"],
        "completed_at": manifest["generated_at"],
        "manifest": MANIFEST_NAME,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "release_sha256": snapshot_sha,
        "source_snapshot_sha256": snapshot_sha,
        "source_file_count": len(entries),
    }
    _write(root / COMPLETION_MARKER, _json_bytes(marker), 0o444)
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o555)
    root.chmod(0o555)
    return root, snapshot_sha


def release_context(release: Path, release_sha: str, campaign_id: str = "campaign-1") -> CampaignContext:
    gates = release / "config" / "v3_cutover_gates.json"
    return CampaignContext(
        campaign_id,
        release_sha,
        "mac-mini-test",
        hashlib.sha256(gates.read_bytes()).hexdigest(),
    )


def _passing_route_certification(
    release: Path,
    validation_profile: dict[str, object] | None,
    runtime_binding: dict[str, object] | None = None,
) -> dict[str, object]:
    manifest_path = release / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    inventory = json.loads(
        (
            release
            / "docs"
            / "architecture"
            / "v3"
            / "generated"
            / "v2_runtime_routes.json"
        ).read_text(encoding="utf-8")
    )
    normalized: list[dict[str, object]] = []
    dispositions: list[dict[str, object]] = []
    for service, rows in inventory["services"].items():
        for row in rows:
            methods = sorted({str(method).upper() for method in row["methods"]})
            normalized.append(
                {
                    "service": service,
                    "rule": row["rule"],
                    "methods": methods,
                    "endpoint": row["endpoint"],
                }
            )
            for method in methods:
                dispositions.append(
                    {
                        "service": service,
                        "rule": row["rule"],
                        "method": method,
                        "endpoint": row["endpoint"],
                        "disposition": "actual_handler_passed",
                        "reviewed": True,
                        "side_effect_class": "read_only",
                        "branch_class": "representative_success_path",
                        "handler_dispatch_passed": True,
                        "representative_success_path_passed": True,
                        "evidence_sha256": "d" * 64,
                    }
                )
    normalized.sort(
        key=lambda row: (row["service"], row["rule"], row["methods"], row["endpoint"])
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    file_hashes = {row["path"]: row["sha256"] for row in manifest["files"]}
    profile_id = (
        str(validation_profile["profile_id"])
        if validation_profile is not None
        else "unprofiled"
    )
    route_workspace = release.parent / "route-certification" / profile_id
    runtime_root = release.parent / "bound-runtime"
    base_runtime_root = release.parent / "bound-base-runtime"
    route_runtime_binding = runtime_binding or {
        "certifying": True,
        "mode": "formal_manifest_bound",
        "python_runtime": str(runtime_root / "bin" / "python"),
        "python_runtime_realpath": str(base_runtime_root / "bin" / "python3"),
        "python_runtime_sha256": "1" * 64,
        "runtime_manifest": str(release.parent / "python-runtime-manifest.json"),
        "runtime_manifest_sha256": "2" * 64,
        "runtime_tree_sha256": "3" * 64,
        "runtime_root": str(runtime_root),
        "base_runtime_root": str(base_runtime_root),
        "pythonpath_roots": [
            str(release),
            str(runtime_root / "lib" / "python3.14" / "site-packages"),
        ],
        "user_site_included": False,
        "parent_sys_path_inherited": False,
        "site_processing_disabled": True,
    }
    exact = {
        "pinned_routes": 347,
        "fully_replayed_routes": 347,
        "remaining_routes": 0,
        "pinned_route_methods": 431,
        "representative_success_path_passed": 431,
        "remaining_route_methods": 0,
    }
    report: dict[str, object] = {
        "schema_version": 1,
        "workload": "346_route_contract_replay",
        "status": "passed",
        "certifying": True,
        "diagnostic_passed": False,
        "runtime_binding": route_runtime_binding,
        "release_id": manifest["release_id"],
        "release_sha": manifest["source_snapshot_sha256"],
        "release_manifest": str(manifest_path),
        "release_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "release_commit": manifest["commit"],
        "inventory_fingerprint": fingerprint,
        "inventory_counts": inventory["counts"],
        **exact,
        "measurements": {"validation_profile_id": profile_id, **exact},
        "trace_promotions": 0,
        "trace_rejections": [],
        "route_method_dispositions": dispositions,
        "safety": {
            "offline": True,
            "production_service_started": False,
            "production_database_accessed": False,
            "nas_accessed": False,
            "external_storage_roots": [
                *campaign_runner_module._route_external_storage_roots(),
            ],
            "external_storage_access_attempts": 0,
            "trace_external_storage_attested": True,
            "base_external_storage_attested": True,
            "external_storage_attested": True,
            "trace_isolation_attempts": {},
            "base_isolation_attempts": {"external_storage_access": 0},
            "seatbelt": campaign_runner_module._route_seatbelt_attestation(
                route_workspace
            ),
            "base_safe_execution": True,
        },
        "source_binding": {
            "compiler_sha256": file_hashes[
                "scripts/v3_validation/route_certification.py"
            ],
            "actual_route_replay_sha256": file_hashes[
                "scripts/v3_validation/actual_route_replay.py"
            ],
            "trace_plugin_sha256": file_hashes[
                "scripts/v3_validation/route_success_trace_plugin.py"
            ],
            "proof_review_manifest_sha256": file_hashes[
                "scripts/v3_validation/route-success-proof-review.json"
            ],
            "primary_side_effect_review_sha256": file_hashes[
                "scripts/v3_validation/route-method-review.json"
            ],
            "supplemental_side_effect_review_sha256": file_hashes[
                "scripts/v3_validation/route-method-review-supplement.json"
            ],
            "base_evidence_sha256": "e" * 64,
        },
        "blockers": {
            "ROUTE_REPLAY_NOT_IMPLEMENTED": {
                "retained": False,
                "remaining_routes": 0,
                "remaining_route_methods": 0,
                "reason": "strict fixture complete",
            }
        },
        "coverage_complete": True,
        "passed": True,
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
        "live_state_access_performed": False,
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


def _passing_health_certification(
    release: Path, validation_profile: dict[str, object] | None
) -> dict[str, object]:
    manifest = json.loads((release / MANIFEST_NAME).read_text(encoding="utf-8"))
    file_hashes = {row["path"]: row["sha256"] for row in manifest["files"]}
    profile = validation_profile or {
        "profile_id": "health_unit_profile",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    measurements = {
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
    }
    report: dict[str, object] = {
        "schema": "magi.v3.health-probe-certification/v1",
        "generated_at": "2026-07-14T00:00:00+00:00",
        "status": "certified",
        "probe": "production_health_service_liveness",
        "validation_profile": profile,
        "measurements": measurements,
        "release_binding": {
            "certifier_script_sha256": file_hashes[
                "scripts/v3_validation/health_certification.py"
            ],
            "health_module_sha256": file_hashes["magi_v3/health.py"],
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
    return {
        "schema_version": 1,
        "workload": "health_1000_model_free",
        "status": "passed",
        "probe": report["probe"],
        "measurements": measurements,
        "report": report,
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
    }


def _passing_fault_certification(
    release: Path, validation_profile: dict[str, object] | None
) -> dict[str, object]:
    from scripts.v3_validation.fault_certification import build_fault_stimulus_plan

    manifest = json.loads((release / MANIFEST_NAME).read_text(encoding="utf-8"))
    file_hashes = {row["path"]: row["sha256"] for row in manifest["files"]}
    profile = validation_profile or {
        "profile_id": "fault_unit_profile",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    stages = [
        "READY",
        "BEGIN",
        "JOB_INSERT",
        *(f"PAYLOAD_{index:02d}" for index in range(32)),
        "COMMIT_STARTED",
        "COMMIT_ACK",
    ]
    logical = {
        "stages_requested": 37,
        "stages_completed": 37,
        "stage_markers": stages,
        "acknowledged_commits_lost": 0,
        "partially_visible_transactions": 0,
        "final_job_rows": 37,
        "final_unique_jobs": 37,
        "final_payload_rows": 1_184,
        "duplicate_jobs": 0,
        "lost_jobs_after_recovery": 0,
        "integrity_check": "ok",
        "cycles": [
            {
                "target_stage": stage,
                "signal": "SIGKILL",
                "final_job_rows": 1,
                "final_payload_rows": 32,
                "integrity_check": "ok",
            }
            for stage in stages
        ],
    }
    stimulus_plan = build_fault_stimulus_plan(profile)
    offsets = stimulus_plan["mach_kill_offsets_us"]
    mach_cycles = [
        {
            "job_id": f"mach-{index}",
            "timing": {
                "clock": "mach_absolute_time",
                "wait": "mach_wait_until",
                "signal": "SIGKILL",
                "target_pid": 10_000 + index,
                "scheduled_delay_ns": delay * 1_000,
                "observed_delay_ns": delay * 1_000,
                "timebase_numer": 1,
                "timebase_denom": 1,
            },
            "final_job_rows": 1,
            "final_payload_rows": 32,
            "integrity_check": "ok",
        }
        for index, delay in enumerate(offsets)
    ]
    measurements = {
        "apfs_enospc": {
            "status": "passed",
            "filesystem": "apfs",
            "image_type": "sparsebundle",
            "image_capacity_bytes": 33_554_432,
            "recovery_reserve_bytes": 4_194_304,
            "sqlite_overhead_reserve_bytes": 1_048_576,
            "sqlite_full_attempt_isolated_to_owned_child": True,
            "sqlite_recovery_isolated_to_owned_child": True,
            "fault_filler_removed_before_recovery": True,
            "filler_bytes_before_enospc": 26_214_400,
            "filesystem_enospc_observed": True,
            "filesystem_enospc_operation": "write",
            "sqlite_full_observed": True,
            "sqlite_error_code": 13,
            "sqlite_error_name": "SQLITE_FULL",
            "committed_rows_preserved": 1,
            "partial_rows_visible": 0,
            "final_jobs": 2,
            "integrity_check": "ok",
        },
        "sqlite_wal_fsync_io_error": {
            "status": "passed",
            "injection_boundary": "custom SQLite VFS xSync",
            "injected_error": "SQLITE_IOERR_FSYNC",
            "injected_file_role": "wal",
            "commit_rc": 1034,
            "extended_rc": 1034,
            "expected_extended_rc": 1034,
            "sync_calls_after_arm": 1,
            "injected": 1,
            "baseline_rows": 1,
            "partial_rows": 0,
            "recovery_rc": 0,
            "final_rows": 2,
            "integrity_ok": 1,
            "journal_mode": "wal",
            "synchronous": "FULL",
            "power_loss_simulated": False,
            "source_sha256": "d" * 64,
            "executable_sha256": "e" * 64,
        },
        "mach_clock_sigkill": {
            "clock": "mach_absolute_time",
            "wait": "mach_wait_until",
            "offsets_us": offsets,
            "cycles_completed": 6,
            "acknowledged_commits_lost": 0,
            "partially_visible_transactions": 0,
            "duplicate_jobs": 0,
            "lost_jobs_after_recovery": 0,
            "final_job_rows": 6,
            "final_payload_rows": 192,
            "integrity_check": "ok",
            "cycles": mach_cycles,
        },
        "logical_transaction_boundary_sweep": logical,
    }
    report: dict[str, object] = {
        "schema": "magi.v3.fault-certification/v2",
        "generated_at": "2026-07-16T00:00:00+00:00",
        "status": "certified_controlled_restart_fault_layer",
        "validation_profile": profile,
        "stimulus_plan": stimulus_plan,
        "decision": {
            "blocker_code": "FAULT_CAMPAIGN_CONTROLLED_RESTART_DEFERRED",
            "required_evidence_id": "sqlite_wal_disk_full_fsync_faults_passed",
            "eligible_to_clear_fault_campaign_realism_blocker": True,
            "software_equivalent_layer_certified": True,
            "transaction_stage_sigkill_certified": True,
            "external_device_disconnect_required": False,
            "physical_power_cut_required": False,
            "controlled_cold_restart_required_at_cutover": True,
            "hard_gate_blocked": False,
            "basis": "owned disposable controlled-restart fault layer",
        },
        "release_binding": {
            "certifier_script_sha256": file_hashes[
                "scripts/v3_validation/fault_certification.py"
            ],
            "fault_probe_script_sha256": file_hashes[
                "scripts/v3_validation/fault_realism.py"
            ],
            "python_executable_sha256": "a" * 64,
            "mach_helper": {
                "source_sha256": "b" * 64,
                "executable_sha256": "c" * 64,
            },
        },
        "measurements": measurements,
        "residual_risk": {
            "accepted_by_equivalent_layer": True,
            "hard_gate_blocking": False,
            "deferred_gate": "atomic_release_switch_and_cold_rollback_drill_passed",
            "required_before_final_replacement": [
                "controlled cold restart with boot-session change",
                "V2 readiness and single-owner restoration after restart",
            ],
            "items": ["controlled restart is deferred to the cutover gate"],
            "rationale": "no external device or hard power removal is required",
        },
        "safety": {
            "live_magi_state_accessed": False,
            "live_business_database_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "network_accessed": False,
            "signals_sent_only_to_owned_children": True,
            "apfs_mount_was_disposable_sparse_image": True,
            "apfs_image_detached_and_removed": True,
            "sandbox_path_sha256": "f" * 64,
        },
        "hash_scheme": "sha256(canonical-json-without-evidence_sha256)",
    }
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "workload": "fault_recovery_certification",
        "status": "passed",
        "measurements": measurements,
        "report": report,
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
    }


def _passing_release_quality_certification(
    release: Path, validation_profile: dict[str, object] | None
) -> dict[str, object]:
    from fnmatch import fnmatch

    from scripts.v3_validation.release_quality_evidence import (
        sha256_json,
        summarize_report,
    )

    manifest = json.loads((release / MANIFEST_NAME).read_text(encoding="utf-8"))
    file_hashes = {row["path"]: row["sha256"] for row in manifest["files"]}
    suites = json.loads(
        (release / "config/v3_release_quality_suites.json").read_text(encoding="utf-8")
    )
    v2_paths = sorted(
        path
        for path in file_hashes
        if any(fnmatch(path, pattern) for pattern in suites["v2_regression"]["include_globs"])
    )
    v3_paths = sorted(
        {path for rows in suites["v3_suites"].values() for path in rows}
    )

    def transcript(paths: list[str]) -> dict[str, object]:
        nodeids = [f"{path}::test_release_quality_fixture" for path in paths]
        return {
            "schema_version": 1,
            "pytest_exitstatus": 0,
            "python_runtime_sha256": runtime_sha,
            "python_runtime_realpath_sha256": runtime_sha,
            "collected_nodeids": nodeids,
            "phase_reports": [
                {
                    "nodeid": nodeid,
                    "when": when,
                    "outcome": "passed",
                    "wasxfail": False,
                    "longrepr_sha256": hashlib.sha256(b"").hexdigest(),
                }
                for nodeid in nodeids
                for when in ("setup", "call", "teardown")
            ],
        }

    def flow(flow_id: str, ordinal: int) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "flow_id": flow_id,
            "passed": True,
            "expected_outcomes_sha256": "a" * 64,
            "observed_outcomes_sha256": "a" * 64,
            "reviewed_routes": [
                {
                    "service": "5002",
                    "rule": f"/fixture/{ordinal}",
                    "method": "GET",
                    "endpoint": f"fixture.endpoint_{ordinal}",
                }
            ],
            "network_access_performed": False,
            "external_writes_performed": False,
            "sandbox_writes_only": True,
            "staged_files_remaining": 0,
            "production_state_accessed": False,
            "service_start_performed": False,
            "inventory_fingerprint": "b" * 64,
        }
        if ordinal == 1:
            payload["fixture_sha256"] = file_hashes[
                "tests/v3/compat/behavior_fixtures/osc-file-content.json"
            ]
        payload["evidence_sha256"] = sha256_json(payload)
        return payload

    profile = validation_profile or {
        "profile_id": "ordinary_week",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    # ``execution_inputs`` installs this exact certifiable runtime fixture.
    # Keep the helper bound to its bytes instead of a generic digest so the
    # production validator exercises the same runtime-SHA trust boundary.
    runtime_sha = hashlib.sha256(
        b"#!/bin/sh\nprintf 'MAGI_V3_PYTHON_OK:3\\n'\n"
    ).hexdigest()
    report: dict[str, object] = {
        "schema": "magi.v3.release-quality-certification/v1",
        "status": "certified",
        "workload": "golden_business_flows",
        "validation_profile": profile,
        "release_binding": {
            "release_id": manifest["release_id"],
            "release_manifest_sha256": hashlib.sha256(
                (release / MANIFEST_NAME).read_bytes()
            ).hexdigest(),
            "python_runtime_sha256": runtime_sha,
            "python_runtime_observed_sha256": runtime_sha,
            "certifier_script_sha256": file_hashes[
                "scripts/v3_validation/release_quality_certification.py"
            ],
            "evidence_module_sha256": file_hashes[
                "scripts/v3_validation/release_quality_evidence.py"
            ],
            "pytest_plugin_sha256": file_hashes[
                "scripts/v3_validation/pytest_transcript_plugin.py"
            ],
            "suite_manifest_sha256": file_hashes[
                "config/v3_release_quality_suites.json"
            ],
            "golden_flows_sha256": file_hashes[
                "scripts/v3_validation/golden_flows.py"
            ],
            "side_effects_sha256": file_hashes[
                "scripts/v3_validation/side_effects.py"
            ],
        },
        "test_source_sha256": {
            path: file_hashes[path] for path in sorted({*v2_paths, *v3_paths})
        },
        "golden_dependency_sha256": {
            path: file_hashes[path]
            for path in (
                "tests/v3/compat/behavior_fixtures/osc-file-content.json",
                "docs/architecture/v3/generated/v2_runtime_routes.json",
                "scripts/v3_validation/route-method-review.json",
                "scripts/v3_validation/route-method-review-supplement.json",
            )
        },
        "pytest_runs": {
            "v2_regression": transcript(v2_paths),
            "v3_suites": transcript(v3_paths),
        },
        "golden_flows": [
            flow("osc_preview_range_download_v1", 1),
            flow("nas_office_provider_session_v1", 2),
        ],
        "side_effect_snapshot": {
            "offline": {
                effect: {"allowed": True, "execute": False}
                for effect in (
                    "none",
                    "read_only",
                    "local_draft",
                    "reversible_write",
                    "external_commit",
                    "destructive",
                )
            },
            "isolated_live_default": {
                "none": {"allowed": True, "execute": True},
                "read_only": {"allowed": True, "execute": True},
                "local_draft": {"allowed": False, "execute": False},
                "reversible_write": {"allowed": False, "execute": False},
                "external_commit": {"allowed": False, "execute": False},
                "destructive": {"allowed": False, "execute": False},
            },
            "isolated_live_explicit_sandbox": {
                "local_draft": {"allowed": True, "execute": True},
                "reversible_write": {"allowed": True, "execute": True},
                "external_commit": {"allowed": False, "execute": False},
                "destructive": {"allowed": False, "execute": False},
            },
        },
        "safety": {
            "live_state_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "external_writes": False,
            "network_denied_by_seatbelt": True,
            "writes_restricted_to_sandbox": True,
            "pytest_home_isolated": True,
        },
    }
    report["evidence_sha256"] = sha256_json(report)
    report["metrics"] = summarize_report(
        report,
        manifest=suites,
        release_files=file_hashes,
        python_runtime_sha256=runtime_sha,
        expected_profile=profile,
        expected_release_id=str(manifest["release_id"]),
        expected_release_manifest_sha256=hashlib.sha256(
            (release / MANIFEST_NAME).read_bytes()
        ).hexdigest(),
    )
    report.pop("evidence_sha256")
    report["evidence_sha256"] = sha256_json(report)
    return {
        "schema_version": 1,
        "workload": "golden_business_flows",
        "status": "passed",
        "measurements": report["metrics"],
        "report": report,
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
    }


def _passing_matched_performance_report(
    *, certifier_sha256: str, runtime_sha256: str
) -> dict[str, object]:
    from scripts.v3_validation.perf_certification import (
        BLOCKER_CODE,
        REQUEST_PLAN,
        SCHEMA as MATCHED_SCHEMA,
        compare_arm_reports,
        request_plan_sha256,
    )

    def arm(name: str, pid: int, p95: int) -> dict[str, object]:
        responses = {
            "unauthorized_get": {"status": 401, "unauthorized": True},
            "authenticated_get": {
                "status": 200,
                "ok": True,
                "case_numbers": ["2099-9001"],
            },
            "idempotent_upsert": {
                "status": 200,
                "ok": True,
                "id": "perf-upsert",
                "case_number": "2099-9001",
                "mode": "upsert",
                "folder_ok": False,
                "archive_ok": False,
                "error": "",
            },
            "create_case_folder": {"status": 200, "folder_ok": True},
            "archive_closed_case": {"status": 200, "archive_ok": True},
        }
        value: dict[str, object] = {
            "schema": "magi.v3.matched-production-performance-arm/v1",
            "arm": name,
            "pid": pid,
            "parent_pid": 4000,
            "started_and_completed_in_one_process": True,
            "request_plan_sha256": request_plan_sha256(),
            "release_binding": {
                "script_sha256": certifier_sha256,
                "v2_handler_sha256": "a" * 64,
                "v3_handler_sha256": "b" * 64,
                "python_executable_sha256": runtime_sha256,
            },
            "backend": {
                "engine": "MariaDB",
                "transport": "unix_socket",
                "tcp_networking": False,
                "database": f"fixture_{name}",
                "innodb_flush_log_at_trx_commit": 1,
                "sync_binlog": 1,
            },
            "parameters": {"iterations": 10},
            "responses": responses,
            "scenario_latency_us": {
                "authenticated_get": p95,
                "idempotent_upsert": p95,
                "create_case_folder": p95,
                "archive_closed_case": p95,
            },
            "warm": {"samples": 10, "p50_us": p95, "p95_us": p95, "p99_us": p95},
            "filesystem": {"entries": [{"path": "matched"}]},
            "database_state": [{"id": "matched"}],
            "safety": {
                "live_state_accessed": False,
                "production_service_started": False,
                "listener_started": False,
                "production_port_accessed": False,
                "launchctl_invoked": False,
                "database_transport_was_unix_socket": True,
                "database_tcp_networking_disabled": True,
                "filesystem_root_was_disposable": True,
            },
        }
        value["evidence_sha256"] = hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return value

    v2 = arm("v2", 4101, 100)
    v3 = arm("v3", 4102, 100)
    comparison = compare_arm_reports([v2], [v3], p95_regression_limit=0.05)
    value: dict[str, object] = {
        "schema": MATCHED_SCHEMA,
        "generated_at": "2026-07-16T00:00:00+00:00",
        "status": "certified",
        "workload": "matched_v2_v3_performance",
        "probe": "sequential_release_bound_mariadb_session_nas_folder_archive",
        "release_binding": {
            "request_plan_sha256": request_plan_sha256(),
            "certifier_script_sha256": certifier_sha256,
            "python_executable_sha256": runtime_sha256,
            "v2_handler_sha256": "a" * 64,
            "v3_handler_sha256": "b" * 64,
        },
        "request_plan": list(REQUEST_PLAN),
        "parameters": {"iterations": 10, "repeats": 1},
        "execution_order": [
            {
                "ordinal": 0,
                "repeat": 0,
                "arm": "v2",
                "pid": 4101,
                "started_monotonic_ns": 100,
                "completed_monotonic_ns": 200,
            },
            {
                "ordinal": 1,
                "repeat": 0,
                "arm": "v3",
                "pid": 4102,
                "started_monotonic_ns": 201,
                "completed_monotonic_ns": 300,
            },
        ],
        "sequential_process_proof": {
            "maximum_simultaneous_version_arms": 1,
            "blocking_subprocess_run_used": True,
            "distinct_child_pid_per_arm": True,
            "intervals_non_overlapping": True,
        },
        "reports": {"v2": [v2], "v3": [v3]},
        "comparison": comparison,
        "gate": {
            "blocker_code": BLOCKER_CODE,
            "eligible_to_clear_full_v2_v3_performance_blocker": True,
            "decision": "clear",
            "gaps": [],
        },
        "safety": {
            "live_state_accessed": False,
            "live_business_database_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "version_arms_ran_concurrently": False,
            "mariadb_tcp_networking_disabled": True,
            "mariadb_unix_socket_removed_after_shutdown": True,
        },
    }
    value["evidence_sha256"] = hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def _passing_resource_performance_partial(
    release: Path,
    validation_profile: dict[str, object] | None,
    performance_report: dict[str, object],
) -> dict[str, object]:
    from scripts.v3_validation.resource_performance_evidence import (
        EXPECTED_GAPS,
        SCHEMA,
        derive_metrics,
        sha256_json,
    )

    manifest_path = release / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    file_hashes = {row["path"]: row["sha256"] for row in manifest["files"]}
    runtime_sha = hashlib.sha256(
        b"#!/bin/sh\nprintf 'MAGI_V3_PYTHON_OK:3\\n'\n"
    ).hexdigest()
    profile = validation_profile or {
        "profile_id": "ordinary_week",
        "replay_start_local": "2026-07-13T00:00:00+08:00",
        "fault_seed": 1101,
    }
    source_paths = {
        "certifier_script_sha256": "scripts/v3_validation/resource_performance_certification.py",
        "evidence_module_sha256": "scripts/v3_validation/resource_performance_evidence.py",
        "perf_source_sha256": "scripts/v3_validation/perf_compat.py",
        "matched_perf_source_sha256": "scripts/v3_validation/perf_certification.py",
        "isolated_window_source_sha256": "scripts/v3_validation/isolated_resource_window.py",
        "isolated_window_collector_sha256": "scripts/v3_validation/isolated_resource_window_collector.py",
        "isolated_window_plan_builder_sha256": "scripts/v3_validation/isolated_resource_window_plan_builder.py",
        "g8_smb_source_sha256": "scripts/v3_validation/g8_isolated_smb.py",
        "resource_window_core_adapter_sha256": "scripts/v3_validation/resource_window_core_adapter.py",
        "resource_window_model_adapter_sha256": "scripts/v3_validation/resource_window_model_adapter.py",
        "resource_source_sha256": "magi_v3/resource.py",
        "dispatcher_source_sha256": "magi_v3/dispatcher.py",
        "ledger_source_sha256": "magi_v3/ledger.py",
        "supervisor_source_sha256": "magi_v3/supervisor.py",
        "macos_resource_source_sha256": "magi_v3/macos_resources.py",
        "resource_policy_sha256": "config/v3_resource_policy.json",
    }
    candidate_launcher = "/fixture/certifiable/python"
    preemption_samples = [
        {
            "cycle": cycle,
            "incoming_priority_class": "P0" if cycle % 2 else "P1",
            "queue_class": "interactive_browser",
            "heavy_job_id": f"cert-heavy-{profile['profile_id']}-{cycle}",
            "interactive_job_id": f"cert-interactive-{profile['profile_id']}-{cycle}",
            "heavy_worker_pid": 1000 + cycle * 2,
            "heavy_descendant_pid": 1001 + cycle * 2,
            "automatic_preemption_count": 1,
            "preemption_source": "dispatch_handle.preemptions",
            "manual_terminate_invoked": False,
            "worker_killed_after_bounded_grace": True,
            "process_group_gone": True,
            "leader_pid_gone": True,
            "descendant_pid_gone": True,
            "heavy_requeued": True,
            "attempts": [[1, "preempted"], [2, "succeeded"]],
            "retry_attempt_number": 2,
            "retry_completed_once": True,
            "active_leases_after_completion": 0,
            "interactive_queue_ms": float(7 + cycle),
            "deadline_budget_ms": 1000.0,
            "deadline_missed": False,
            "orphan_process_groups": 0,
            "duplicate_completions": 0,
            "lost_jobs": 0,
        }
        for cycle in range(1, 5)
    ]
    worker_footprint_samples = [
        {
            "cycle": cycle,
            "job_id": f"cert-worker-footprint-{profile['profile_id']}-{cycle}",
            "leader_pid": 2000 + cycle * 2,
            "descendant_pid": 2001 + cycle * 2,
            "observed_group_rss_mb": 64.0,
            "observed_group_physical_footprint_mb": 48.0,
            "rss_source": (
                "libproc.proc_pid_rusage(RUSAGE_INFO_V4).ri_resident_size"
            ),
            "physical_footprint_source": (
                "libproc.proc_pid_rusage(RUSAGE_INFO_V4).ri_phys_footprint"
            ),
            "leader_proc_start_abstime": 900000 + cycle * 2,
            "descendant_proc_start_abstime": 900001 + cycle * 2,
            "return_budget_seconds": 30.0,
            "return_seconds": float(cycle),
            "returncode": 0,
            "timed_out": False,
            "killed": False,
            "process_group_gone": True,
            "leader_pid_gone": True,
            "descendant_pid_gone": True,
            "rss_returned_to_zero": True,
            "physical_footprint_returned_to_zero": True,
            "production_state_accessed": False,
            "sample_errors": [],
        }
        for cycle in range(1, 4)
    ]
    report: dict[str, object] = {
        "schema": SCHEMA,
        "status": "incomplete",
        "workload": "matched_v2_v3_performance",
        "validation_profile": profile,
        "release_binding": {
            "release_id": manifest["release_id"],
            "release_manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "python_runtime_sha256": runtime_sha,
            "python_runtime_observed_sha256": runtime_sha,
            "python_runtime_realpath": candidate_launcher,
            **{field: file_hashes[path] for field, path in source_paths.items()},
        },
        "performance_report": performance_report,
        "matched_production_report": _passing_matched_performance_report(
            certifier_sha256=file_hashes[
                "scripts/v3_validation/perf_certification.py"
            ],
            runtime_sha256=runtime_sha,
        ),
        "resource_probe": {
            "owned_probe_pid": 101,
            "owned_probe_physical_footprint_mb": 64.0,
            "physical_footprint_source": "/usr/bin/footprint --noCategories -f bytes",
            "swapout_before_mb": 10.0,
            "swapout_after_mb": 10.0,
            "swapout_growth_mb": 0.0,
            "observation_seconds": 0.25,
            "required_idle_observation_seconds": 1800,
            "complete_budget_profiles_measured": False,
            "model_loaded": False,
            "production_state_accessed": False,
            "sample_errors": [],
        },
        "preemption_probe": {
            "probe_version": "durable_dispatcher_automatic_preemption_v1",
            "candidate_launcher": candidate_launcher,
            "seatbelt_child": True,
            "sample_count": len(preemption_samples),
            "samples": preemption_samples,
            "automatic_preemption_observed": True,
            "manual_owned_cleanup_performed": False,
            "p0_p1_deadline_misses": 0,
            "interactive_queue_p95_ms": 11.0,
            "p1_browser_queue_p95_seconds": 0.011,
            "orphan_process_groups": 0,
            "duplicate_completions": 0,
            "lost_jobs": 0,
            "preempted_jobs_requeued": len(preemption_samples),
            "attempt_two_unique_completions": len(preemption_samples),
            "production_state_accessed": False,
        },
        "worker_capability_probe": {
            "probe_version": "owned_worker_group_footprint_return_v1",
            "candidate_launcher": candidate_launcher,
            "seatbelt_child": True,
            "sample_count": len(worker_footprint_samples),
            "return_budget_seconds": 30.0,
            "return_p95_seconds": 3.0,
            "samples": worker_footprint_samples,
            "metal_measurement_available": False,
            "magi_metal_mb": None,
            "metal_missing_reason": (
                "no validated non-privileged per-process Metal allocation-byte source"
            ),
            "production_state_accessed": False,
        },
        "capability_gaps": list(EXPECTED_GAPS),
        "safety": {
            "live_state_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "external_writes": False,
            "network_denied_by_seatbelt": True,
            "writes_restricted_to_sandbox": True,
            "models_loaded": False,
        },
    }
    report["metrics"] = derive_metrics(report)
    report["evidence_sha256"] = sha256_json(report)
    return {
        "schema_version": 1,
        "workload": "matched_v2_v3_performance",
        "status": "passed",
        "probe": "release_bound_resource_performance_partial",
        "measurements": report["metrics"],
        "report": report,
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
        "live_state_access_performed": False,
    }


def _passing_schedule_certification(
    release_root: Path, validation_profile: dict[str, object] | None
) -> dict[str, object]:
    manifest_path = release_root / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    release_files = {row["path"]: row["sha256"] for row in manifest["files"]}
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    profile_id = (
        str(validation_profile["profile_id"])
        if validation_profile is not None
        else "default_ordinary_week"
    )
    entries = [
        {"job_id": f"job-{index:03d}", "classification": "safe_adapter", "blockers": []}
        for index in range(93)
    ]
    results = [
        {
            "job_id": f"job-{index:03d}",
            "status": "passed",
            "semantic_success": True,
            "successful_samples": 3,
            "duration_sample_count": 3,
            "network_denied_by_seatbelt": True,
            "notifications_disabled": True,
        }
        for index in range(93)
    ]
    body: dict[str, object] = {
        "schema": "magi.v3.schedule-body-adapter-registry/v1",
        "status": "passed",
        "completion_claimed": True,
        "release_binding": {
            "release_id": manifest["release_id"],
            "release_manifest_sha256": manifest_sha,
            "cron_jobs_sha256": "c" * 64,
            "registry_sha256": release_files[
                "config/v3_schedule_body_adapter_registry.json"
            ],
            "inherited_baseline_sha256": release_files[
                "config/v3_schedule_realism_baseline.json"
            ],
        },
        "measurements": {
            "enabled_jobs": 93,
            "safe_adapter_coverage_jobs": 93,
            "blocked_jobs": 0,
            "body_jobs_passed": 93,
            "all_safe_bodies_passed": True,
            "registry_complete_for_enabled_jobs": True,
        },
        "registry_entries": entries,
        "body_results": results,
        "sandbox_escape_probes": {"status": "passed"},
        "network_access_performed": False,
        "external_network_access_performed": False,
        "production_database_access_performed": False,
        "nas_access_performed": False,
        "production_state_write_performed": False,
    }
    body["evidence_sha256"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    delay = 100.0
    capacity = {
        "delivery_multiplier": 10,
        "duration_multiplier": 2.0,
        "all_deliveries_accounted": True,
        "all_distinct_occurrences_accounted": True,
        "same_job_concurrency_violations": 0,
        "coalesced_distinct_occurrences": 0,
        "durable_backlog_coalescing_job_ids": [
            "job_drive_case_sync_all_files",
            "job_legacy_judgment_resummary_quality"
        ],
        "durable_backlog_coalesced_occurrences": 0,
        "durable_backlog_coalesced_occurrences_by_job": {},
        "loss_sensitive_coalesced_occurrences": 0,
        "loss_sensitive_coalesced_occurrences_by_job": {},
        "coalescing_safety_passed": True,
        "latest_start_misses": 0,
        "deadline_misses": 0,
        "global_worker_cap": 4,
        "max_queue_delay_seconds": delay,
    }
    report: dict[str, object] = {
        "schema": "magi.v3.schedule-capacity-certification/v1",
        "status": "certified",
        "validation_profile_id": profile_id,
        "release_binding": {
            "release_id": manifest["release_id"],
            "release_manifest_sha256": manifest_sha,
            "cron_jobs_sha256": "c" * 64,
            "dispatch_policy_sha256": release_files[
                "config/v3_schedule_dispatch_policy.json"
            ],
            "certifier_script_sha256": release_files[
                "scripts/v3_validation/schedule_capacity_certification.py"
            ],
            "real_job_body_registry_script_sha256": release_files[
                "scripts/v3_validation/schedule_body_registry.py"
            ],
            "real_job_body_registry_sha256": release_files[
                "config/v3_schedule_body_adapter_registry.json"
            ],
            "duration_baseline_sha256": release_files[
                "config/v3_schedule_realism_baseline.json"
            ],
            "real_job_body_evidence_sha256": body["evidence_sha256"],
        },
        "layers": {
            "control_plane": {"status": "passed", "measurements": capacity},
            "business_body_plane": {
                "status": "passed",
                "duration_evidence": {
                    "enabled_jobs": 93,
                    "p95_jobs": 93,
                    "sparse_fallback_jobs": 0,
                    "missing_jobs": 0,
                    "certifying_p95_coverage": True,
                },
                "body_evidence": {
                    "enabled_jobs": 93,
                    "jobs_with_three_successful_real_body_samples": 93,
                    "jobs_missing_real_body_adapter": 0,
                    "body_adapter_coverage_complete": True,
                    "registry_evidence_sha256": body["evidence_sha256"],
                },
                "deadline_measurements": {
                    "latest_start_misses": 0,
                    "deadline_misses": 0,
                    "max_queue_delay_seconds": delay,
                },
            },
        },
        "gate": {
            "eligible_to_clear_schedule_realism_blocker": True,
            "decision": "clear",
            "blocking_reasons": [],
        },
        "safety": {
            "live_state_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "body_network_access_performed": False,
            "body_external_network_access_performed": False,
            "body_nas_access_performed": False,
            "body_production_database_access_performed": False,
            "sandbox_writes_only": True,
        },
    }
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "workload": "seven_day_schedule_10x_arrival_2x_duration_replay",
        "status": "passed",
        "measurements": {
            "validation_profile_id": profile_id,
            "enabled_jobs": 93,
            "covered_jobs": 93,
            "passed_jobs": 93,
            "blocked_jobs": 0,
            "latest_start_misses": 0,
            "deadline_misses": 0,
        },
        "report": report,
        "body_evidence": body,
        "network_access_performed": False,
        "external_network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
        "live_state_access_performed": False,
    }


def successful_runner(
    calls,
    *,
    route_runtime_binding: dict[str, object] | None = None,
):
    allowlisted_body_ids, representative_gap_ids = _schedule_fixture_ids()

    def run(argv, cwd, validation_profile=None):
        calls.append((tuple(argv), cwd))
        stdout = "passed"
        if argv[-1].endswith("scripts/v3_validation/route_certification.py"):
            stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
                _passing_route_certification(
                    cwd,
                    validation_profile,
                    runtime_binding=(
                        json.loads(json.dumps(route_runtime_binding))
                        if route_runtime_binding is not None
                        else None
                    ),
                ),
                separators=(",", ":"),
                sort_keys=True,
            )
        elif any(
            argument.endswith("scripts/v3_validation/release_quality_certification.py")
            for argument in argv
        ):
            stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
                _passing_release_quality_certification(cwd, validation_profile),
                separators=(",", ":"),
                sort_keys=True,
            )
        elif any(
            argument.endswith("scripts/v3_validation/health_certification.py")
            for argument in argv
        ):
            stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
                _passing_health_certification(cwd, validation_profile),
                separators=(",", ":"),
                sort_keys=True,
            )
        elif any(
            argument.endswith("scripts/v3_validation/fault_certification.py")
            for argument in argv
        ):
            stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
                _passing_fault_certification(cwd, validation_profile),
                separators=(",", ":"),
                sort_keys=True,
            )
        elif any(
            argument.endswith("scripts/v3_validation/schedule_capacity_certification.py")
            for argument in argv
        ):
            stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
                _passing_schedule_certification(cwd, validation_profile),
                separators=(",", ":"),
                sort_keys=True,
            )
        elif argv[-1].endswith("tests/v3/test_campaign_offline_probes.py"):
            selector = argv[argv.index("-k") + 1]
            if "seven_day_schedule" in selector:
                structured = {
                    "schema_version": 1,
                    "workload": "seven_day_schedule_10x_arrival_2x_duration_replay",
                    "probe": "measured_schedule_ledger_replay",
                    "status": "passed",
                    "measurements": {
                        "cron_definitions": 102,
                        "enabled_cron_definitions": 93,
                        "validation_profile_id": (
                            validation_profile["profile_id"]
                            if validation_profile is not None
                            else "default_ordinary_week"
                        ),
                        "replay_start_local": (
                            validation_profile["replay_start_local"]
                            if validation_profile is not None
                            else "2026-07-13T00:00:00+08:00"
                        ),
                        "base_seven_day_arrivals": 5547,
                        "replayed_arrivals": 55470,
                        "arrival_multiplier": 10,
                        "duration_multiplier": 2.0,
                        "duration_basis": "measured_ledger_lifecycle_p95",
                        "virtual_duration_seconds": 604800,
                        "governor_light_slots": 2,
                        "governor_heavy_slots": 1,
                        "wall_duration_seconds": 10.0,
                        "acceleration_factor": 60480.0,
                        "calibrated_light_p95_ms": 2.0,
                        "calibrated_maintenance_p95_ms": 3.0,
                        "persisted_jobs": 55470,
                        "duplicate_jobs": 0,
                        "lost_jobs": 0,
                        "recovered_jobs": 55470,
                        "latest_start_misses": 0,
                        "deadline_misses": 0,
                        "max_queue_delay_ms": 20.0,
                        "journal_mode_wal": True,
                        "integrity_check_ok": True,
                        "reopen_ping_ok": True,
                        "cron_jobs_sha256": "c" * 64,
                        "realism_audit": {
                            "schema_version": 1,
                            "workload": "production_duration_and_representative_job_body_sandbox",
                            "status": "incomplete",
                            "completion_claimed": False,
                            "measurements": {
                                "cron_definitions": 102,
                                "enabled_cron_definitions": 93,
                                "production_duration_observations": 92,
                                "production_duration_gap_jobs": 0,
                                "production_duration_percentile_available": False,
                                "representative_bodies_allowlisted": len(allowlisted_body_ids),
                                "representative_bodies_passed": len(allowlisted_body_ids),
                                "representative_body_gap_jobs": len(representative_gap_ids),
                                "all_allowlisted_bodies_passed": True,
                                "cron_jobs_sha256": "c" * 64,
                            },
                            "body_results": [
                                {
                                    "job_id": job_id,
                                    "status": "passed",
                                    "executed": True,
                                    "semantic_success": True,
                                    "adapter_mode": "real_entrypoint_dry_run_v1",
                                    "adapter_dry_run": True,
                                    "network_denied_by_seatbelt": True,
                                    "notifications_disabled": True,
                                    "sandbox_profile_sha256": "d" * 64,
                                    "adapter_fixture_manifest_sha256": "e" * 64,
                                }
                                for job_id in allowlisted_body_ids
                            ],
                            "gaps": [
                                {
                                    "job_id": job_id,
                                    "gap_type": "representative_job_body",
                                    "reasons": [
                                        "NOT_EXACTLY_ALLOWLISTED_FOR_OFFLINE_BODY_EXECUTION"
                                    ],
                                }
                                for job_id in representative_gap_ids
                            ],
                            "network_access_performed": False,
                            "production_database_access_performed": False,
                            "nas_access_performed": False,
                            "live_service_access_performed": False,
                            "production_state_write_performed": False,
                            "sandbox_writes_only": True,
                        },
                    },
                    "network_access_performed": False,
                    "service_start_performed": False,
                    "production_port_access_performed": False,
                    "launchctl_performed": False,
                    "live_state_access_performed": False,
                }
            elif "bounded_fault_matrix" in selector:
                faults = [
                    "sqlite_wal_concurrent_reopen",
                    "sqlite_bounded_disk_full",
                    "atomic_fsync_failure",
                    "worker_crash",
                    "worker_timeout",
                    "notification_storm_dlq",
                ]
                structured = {
                    "schema_version": 1,
                    "workload": "fault_injection",
                    "probe": "bounded_offline_fault_matrix",
                    "status": "passed",
                    "measurements": {
                        "matrix": [
                            {
                                "fault": fault,
                                "status": "passed",
                                "duplicate": 0,
                                "loss": 0,
                                "recovery_ms": 1.0,
                            }
                            for fault in faults
                        ],
                        "faults_requested": 6,
                        "faults_completed": 6,
                        "faults_passed": 6,
                        "duplicate_total": 0,
                        "loss_total": 0,
                        "recovered_total": 6,
                        "maximum_recovery_ms": 1.0,
                        "wall_duration_seconds": 1.0,
                        "realism_audit": {
                            "schema_version": 1,
                            "generated_at": "2026-07-14T00:00:00+00:00",
                            "workload": "fault_injection_realism_audit",
                            "probe": "owned_sqlite_wal_sigkill_commit_window_sweep",
                            "status": "passed_partial_evidence",
                            "measurements": {
                                "cycles_requested": 12,
                                "cycles_completed": 12,
                                "acknowledged_commits_lost": 0,
                                "partially_visible_transactions": 0,
                                "final_job_rows": 12,
                                "final_unique_jobs": 12,
                                "final_payload_rows": 384,
                                "duplicate_jobs": 0,
                                "lost_jobs_after_recovery": 0,
                                "integrity_check": "ok",
                                "journal_mode": "wal",
                                "synchronous": "FULL",
                                "apfs_sparse_image": {
                                    "status": "passed",
                                    "filesystem": "apfs",
                                    "image_type": "sparsebundle",
                                    "image_capacity_bytes": 33_554_432,
                                    "recovery_reserve_bytes": 4_194_304,
                                    "filler_bytes_before_enospc": 26_214_400,
                                    "filesystem_enospc_observed": True,
                                    "filesystem_enospc_operation": "write",
                                    "sqlite_full_observed": True,
                                    "sqlite_error_code": 13,
                                    "sqlite_error_name": "SQLITE_FULL",
                                    "committed_rows_preserved": 1,
                                    "partial_rows_visible": 0,
                                    "final_jobs": 2,
                                    "integrity_check": "ok",
                                },
                                "transaction_instruction_boundary_sweep": {
                                    "stages_requested": 37,
                                    "stages_completed": 37,
                                    "stage_markers": [
                                        "READY",
                                        "BEGIN",
                                        "JOB_INSERT",
                                        *(f"PAYLOAD_{index:02d}" for index in range(32)),
                                        "COMMIT_STARTED",
                                        "COMMIT_ACK",
                                    ],
                                    "acknowledged_commits_lost": 0,
                                    "partially_visible_transactions": 0,
                                    "final_job_rows": 37,
                                    "final_unique_jobs": 37,
                                    "final_payload_rows": 1184,
                                    "duplicate_jobs": 0,
                                    "lost_jobs_after_recovery": 0,
                                    "integrity_check": "ok",
                                    "cycles": [
                                        {
                                            "target_stage": stage,
                                            "signal": "SIGKILL",
                                            "final_job_rows": 1,
                                            "final_payload_rows": 32,
                                            "integrity_check": "ok",
                                        }
                                        for stage in [
                                            "READY",
                                            "BEGIN",
                                            "JOB_INSERT",
                                            *(f"PAYLOAD_{index:02d}" for index in range(32)),
                                            "COMMIT_STARTED",
                                            "COMMIT_ACK",
                                        ]
                                    ],
                                },
                                "bounded_time_offset_sigkill_sweep": {
                                    "offsets_requested": 6,
                                    "offsets_completed": 6,
                                    "scheduled_offsets_us": [0, 50, 250, 1_000, 5_000, 20_000],
                                    "acknowledged_commits_lost": 0,
                                    "partially_visible_transactions": 0,
                                    "final_job_rows": 6,
                                    "final_unique_jobs": 6,
                                    "final_payload_rows": 192,
                                    "duplicate_jobs": 0,
                                    "lost_jobs_after_recovery": 0,
                                    "integrity_check": "ok",
                                    "cycles": [
                                        {
                                            "scheduled_kill_offset_us": delay,
                                            "signal": "SIGKILL",
                                            "final_job_rows": 1,
                                            "final_payload_rows": 32,
                                            "integrity_check": "ok",
                                        }
                                        for delay in [0, 50, 250, 1_000, 5_000, 20_000]
                                    ],
                                },
                                "sqlite_vfs_fsync_io_error": {
                                    "status": "passed",
                                    "injection_boundary": "custom SQLite VFS xSync",
                                    "injected_error": "SQLITE_IOERR_FSYNC",
                                    "injected_file_role": "wal",
                                    "commit_rc": 1034,
                                    "extended_rc": 1034,
                                    "expected_extended_rc": 1034,
                                    "sync_calls_after_arm": 1,
                                    "injected": 1,
                                    "injected_open_flags": 3_670_022,
                                    "baseline_rows": 1,
                                    "partial_rows": 0,
                                    "recovery_rc": 0,
                                    "final_rows": 2,
                                    "integrity_ok": 1,
                                    "journal_mode": "wal",
                                    "synchronous": "FULL",
                                    "power_loss_simulated": False,
                                    "source_sha256": "a" * 64,
                                    "executable_sha256": "b" * 64,
                                },
                                "machine_instruction_offset_sigkill": {
                                    "status": "blocked",
                                    "method_evaluated": (
                                        "macOS ptrace PT_TRACE_ME/PT_STEP on an owned child"
                                    ),
                                    "reason": (
                                        "no stable, reap-safe instruction-step trace evidence "
                                        "was available"
                                    ),
                                    "logical_transaction_boundary_sweep_substituted": False,
                                },
                            },
                            "coverage": {
                                "owned_process_sigkill_at_commit_boundary": True,
                                "owned_process_sigkill_at_bounded_time_offsets": True,
                                "sqlite_wal_full_synchronous_sigkill": True,
                                "sqlite_wal_reopen_and_integrity_check": True,
                                "idempotent_recovery_from_known_input_plan": True,
                                "all_logical_transaction_boundaries_sigkill": True,
                                "sandbox_apfs_sparse_image_enospc": True,
                                "physical_apfs_enospc": False,
                                "physical_power_interruption": False,
                                "custom_sqlite_vfs_power_loss": False,
                                "sqlite_vfs_fsync_io_error_injection": True,
                                "arbitrary_instruction_offset_sigkill": False,
                            },
                            "blocker": {
                                "code": "FAULT_CAMPAIGN_REALISM_INCOMPLETE",
                                "eligible_to_clear": False,
                                "decision": "blocker_retained",
                            },
                            "safety": {
                                "live_magi_state_accessed": False,
                                "production_service_imported": False,
                                "listener_started": False,
                                "network_api_invoked": False,
                                "production_port_accessed": False,
                                "launchctl_invoked": False,
                                "signals_sent_only_to_owned_children": True,
                                "owned_custom_vfs_compiled_and_executed": True,
                                "compiler_network_access": False,
                                "owned_disk_image_attach_performed": True,
                                "owned_disk_image_detached_and_removed": True,
                            },
                            "hash_scheme": "sha256(canonical-json-without-evidence_sha256)",
                        },
                    },
                    "network_access_performed": False,
                    "service_start_performed": False,
                    "production_port_access_performed": False,
                    "launchctl_performed": False,
                    "live_state_access_performed": False,
                }
                realism = structured["measurements"]["realism_audit"]
                realism["evidence_sha256"] = hashlib.sha256(
                    json.dumps(
                        realism,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            else:
                structured = {
                    "schema_version": 1,
                    "workload": "hundred_cycle_worker_reap_soak",
                    "probe": "owned_process_group_reap_soak",
                    "status": "passed",
                    "measurements": {
                        "cycles_requested": 100,
                        "cycles_completed": 100,
                        "process_groups_gone": 100,
                        "active_workers_after": 0,
                        "governor_slots_after": 0,
                        "fd_count_before": 6,
                        "fd_count_after": 6,
                        "fd_peak": 6,
                        "fd_drift": 0,
                        "total_worker_duration_sec": 1.0,
                    },
                    "network_access_performed": False,
                    "service_start_performed": False,
                    "production_port_access_performed": False,
                    "launchctl_performed": False,
                }
            stdout = (
                "MAGI_V3_OFFLINE_EVIDENCE="
                + json.dumps(structured, separators=(",", ":"), sort_keys=True)
            )
        elif any(
            argument.endswith(
                (
                    "tests/v3/test_perf_compat.py",
                    "scripts/v3_validation/resource_performance_certification.py",
                )
            )
            for argument in argv
        ):
            post_transcript = {
                "database": "sqlite_memory_disposable",
                "target_state_sha256": "d" * 64,
                "target_state": {
                    "id": "synthetic-case-000",
                    "case_number": "2026-0001",
                    "notes": "production-shaped-post-fixture",
                },
                "transaction_event_counts": {
                    "begin": 550,
                    "insert": 0,
                    "update": 550,
                    "commit": 550,
                    "rollback": 0,
                },
                "post_transaction_count": 550,
                "post_transaction_transcript_sha256": "a" * 64,
                "balanced_transactions": True,
                "external_writes": False,
                "production_state_accessed": False,
                "nas_accessed": False,
            }
            report = {
                "schema_version": 1,
                "workload": "native_gateway_livez",
                "offline": True,
                "parameters": {"warmup": 100, "iterations": 1000, "repeats": 3},
                "equivalence_proof": {
                    "comparison_valid": True,
                    "responses_correct": True,
                    "same_python_runtime": True,
                    "same_request_plan": True,
                    "handler_identities": {
                        "v2_actual_livez_wsgi": {
                            "implementation": "production_v2",
                            "source_sha256": "1" * 64,
                        },
                        "v3_native_gateway_livez_wsgi": {
                            "implementation": "native_v3",
                            "source_sha256": "2" * 64,
                        },
                    },
                },
                "claim_coverage": {
                    "warm_latency_p50_p95_p99": True,
                    "cold_first_request_latency": True,
                    "production_v2_handler": True,
                    "native_v3_handler": True,
                    "native_v3_gateway_probe": True,
                    "production_business_workload": False,
                    "representative_synthetic_business_corpus_defined": True,
                    "matched_native_business_handler": True,
                    "synthetic_business_workload": True,
                    "synthetic_business_get_measured": True,
                    "synthetic_business_post_measured": True,
                    "rss_evidence": True,
                    "file_descriptor_evidence": True,
                    "same_host_sequential_not_concurrent": True,
                    "release_gateway_thresholds_applied": True,
                },
                "gateway_threshold_evaluation": {
                    "passed": True,
                    "aggregation": "median_of_three_isolated_run_percentiles",
                    "checks": {
                        name: {"observed_us": 1.0, "maximum_us": maximum, "passed": True}
                        for name, maximum in {
                            "gateway_livez_p95_us": 10_000.0,
                            "gateway_added_overhead_p95_us": 5_000.0,
                            "gateway_added_overhead_p99_us": 10_000.0,
                        }.items()
                    },
                },
                "comparison": {
                    "latency_p95_v2_us": 2.0,
                    "latency_p95_native_v3_us": 2.0,
                },
                "sequential_process_proof": {
                    "maximum_simultaneous_benchmark_children": 1,
                    "blocking_subprocess_run_used": True,
                    "children": [
                        {
                            "ordinal": index,
                            "mode": mode,
                            "pid": 1000 + index,
                            "parent_pid": 999,
                        }
                        for index, mode in enumerate(
                            (
                                "v2_actual_livez_wsgi",
                                "v3_native_gateway_livez_wsgi",
                                "v3_native_gateway_livez_wsgi",
                                "v2_actual_livez_wsgi",
                                "v2_actual_livez_wsgi",
                                "v3_native_gateway_livez_wsgi",
                                "v2_actual_osc_cases_wsgi",
                                "v3_native_osc_cases_wsgi",
                                "v3_native_osc_cases_wsgi",
                                "v2_actual_osc_cases_wsgi",
                                "v2_actual_osc_cases_wsgi",
                                "v3_native_osc_cases_wsgi",
                            )
                        )
                    ],
                },
                "runs": {
                    mode: [
                        {
                            "mode": mode,
                            "workload": "native_gateway_livez",
                            "correctness_passed": True,
                            "runtime": {"executable_sha256": "3" * 64},
                            "request_plan_sha256": "4" * 64,
                            "latency": {"p50": 1.0, "p95": 2.0, "p99": 3.0},
                            "memory": {
                                "rss_before_bytes": 100,
                                "rss_after_bytes": 100,
                                "rss_growth_bytes": 0,
                            },
                            "file_descriptors": {"before": 4, "after": 4, "drift": 0},
                            "safety": {
                                "listener_started": False,
                                "production_service_imported": False,
                                "production_handler_module_imported": True,
                                "network_connections_blocked": True,
                                "live_state_accessed": False,
                                "external_writes": False,
                                "production_state_writes": False,
                                "production_port_accessed": False,
                                "nas_accessed": False,
                                "launchctl_invoked": False,
                            },
                        }
                        for _index in range(3)
                    ]
                    for mode in ("v2_actual_livez_wsgi", "v3_native_gateway_livez_wsgi")
                },
                "representative_business_corpus": {
                    "synthetic_only": True,
                    "production_state_accessed": False,
                    "request_count": 2,
                    "request_plan_sha256": "b" * 64,
                    "database_corpus": {
                        "backend": "sqlite_memory",
                        "row_count": 32,
                        "sha256": "e" * 64,
                        "production_state_accessed": False,
                    },
                    "v3_native_handler": {
                        "composed_in_service_manifest": False,
                        "source_sha256": "f" * 64,
                    },
                    "matched_measurement_status": "matched_synthetic_get_and_post_measured",
                    "measured_methods": ["GET", "POST"],
                    "unmeasured_methods": [],
                    "architecture_gap": {
                        "code": "NATIVE_OSC_CASES_NOT_COMPOSED_IN_SERVICE_MANIFEST",
                        "factory_kind": "v2_compatibility",
                        "gateway_application_factories": {
                            "main_http": "magi_v3.compat:create_main_app",
                            "tools_http": "magi_v3.compat:create_tools_app",
                        },
                        "manifest_sha256": "c" * 64,
                    },
                },
                "synthetic_business_benchmark": {
                    "schema_version": 1,
                    "workload": "synthetic_osc_cases",
                    "synthetic_only": True,
                    "production_business_workload": False,
                    "release_thresholds_applied": False,
                    "parameters": {"warmup": 100, "iterations": 1000, "repeats": 3},
                    "measured_methods": ["GET", "POST"],
                    "unmeasured_methods": [],
                    "same_python_runtime": True,
                    "same_request_plan": True,
                    "same_route_identity": True,
                    "response_projection_equivalent": True,
                    "isolation_contract": {
                        "actual_v2_blueprint_view_executed": True,
                        "actual_native_wsgi_and_service_executed": True,
                        "v2_database_override": "_osc_exec_bounded_disposable_in_memory_sqlite",
                        "v2_manual_schema_guard": "pre_satisfied_no_ddl",
                        "v2_path_mapper": "identity_for_get_and_nas_resolution_forbidden",
                        "v2_settings_lookup": "forbidden_rows_have_explicit_lawyer",
                        "authentication": "flask_login_disabled_and_native_authorizer_true_synthetic_only",
                        "network": "socket_connect_blocked",
                    },
                    "sequential_process_proof": {
                        "maximum_simultaneous_benchmark_children": 1,
                        "blocking_subprocess_run_used": True,
                        "children": [
                            {
                                "ordinal": 6 + index,
                                "mode": mode,
                                "pid": 2000 + index,
                                "parent_pid": 999,
                            }
                            for index, mode in enumerate((
                                "v2_actual_osc_cases_wsgi",
                                "v3_native_osc_cases_wsgi",
                                "v3_native_osc_cases_wsgi",
                                "v2_actual_osc_cases_wsgi",
                                "v2_actual_osc_cases_wsgi",
                                "v3_native_osc_cases_wsgi",
                            ))
                        ],
                    },
                    "handler_identities": {
                        "v2_actual_osc_cases_wsgi": {
                            "implementation": "production_v2",
                            "source_sha256": "8" * 64,
                        },
                        "v3_native_osc_cases_wsgi": {
                            "implementation": "native_v3",
                            "source_sha256": "9" * 64,
                        },
                    },
                    "comparison": {
                        "latency_p50_v2_us": 1.0,
                        "latency_p50_native_v3_us": 1.0,
                        "latency_p95_v2_us": 2.0,
                        "latency_p95_native_v3_us": 2.0,
                        "latency_p99_v2_us": 3.0,
                        "latency_p99_native_v3_us": 3.0,
                        "cold_start_v2_us": 10.0,
                        "cold_start_native_v3_us": 10.0,
                        "rss_growth_v2_bytes": 0,
                        "rss_growth_native_v3_bytes": 0,
                        "fd_drift_v2": 0,
                        "fd_drift_native_v3": 0,
                    },
                    "side_effect_transcript": {
                        "v2_actual_osc_cases_wsgi": post_transcript,
                        "v3_native_osc_cases_wsgi": post_transcript,
                    },
                    "runs": {
                        mode: [
                            {
                                "mode": mode,
                                "workload": "synthetic_osc_cases",
                                "correctness_passed": True,
                                "response_sequence_sha256": "7" * 64,
                                "expected_response_sequence_sha256": "7" * 64,
                                "runtime": {"executable_sha256": "3" * 64},
                                "cold_start": {"latency_us": 10.0},
                                "latency": {"p50": 1.0, "p95": 2.0, "p99": 3.0},
                                "memory": {
                                    "rss_before_bytes": 100,
                                    "rss_after_bytes": 100,
                                    "rss_growth_bytes": 0,
                                },
                                "file_descriptors": {"before": 4, "after": 4, "drift": 0},
                                "safety": {
                                    "listener_started": False,
                                    "production_service_imported": False,
                                    "production_handler_module_imported": mode
                                    == "v2_actual_osc_cases_wsgi",
                                    "network_connections_blocked": True,
                                    "live_state_accessed": False,
                                    "external_writes": False,
                                    "production_state_writes": False,
                                    "production_port_accessed": False,
                                    "nas_accessed": False,
                                    "launchctl_invoked": False,
                                },
                                "synthetic_corpus": {
                                    "database": "sqlite_memory_disposable",
                                    "row_count": 32,
                                    "read_only": False,
                                    "disposable": True,
                                    "measured_methods": ["GET", "POST"],
                                    "unmeasured_methods": [],
                                    "opposite_handler_module_imported": False,
                                    "corpus_sha256": "e" * 64,
                                    "side_effect_transcript": post_transcript,
                                },
                            }
                            for _index in range(3)
                        ]
                        for mode in (
                            "v2_actual_osc_cases_wsgi",
                            "v3_native_osc_cases_wsgi",
                        )
                    },
                },
                "gate": {
                    "blocker_code": "MATCHED_PRODUCTION_PERFORMANCE_NOT_IMPLEMENTED",
                    "eligible_to_clear_full_v2_v3_performance_blocker": False,
                    "decision": "blocker_retained",
                    "thresholds_applied": True,
                    "threshold_scope": "gateway_livez_native_v2_v3_only",
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
            structured = _passing_resource_performance_partial(
                cwd, validation_profile, report
            )
            stdout = (
                "MAGI_V3_OFFLINE_EVIDENCE="
                + json.dumps(structured, separators=(",", ":"), sort_keys=True)
            )
        elif argv[-1].endswith("tests/v3/test_ime_candidate_native.py"):
            structured = {
                "schema_version": 1,
                "workload": "ime_candidate_window_pressure_probe",
                "probe": "native_mcbopomofo_candidate_window_pressure",
                "status": "passed",
                "measurements": {
                    "cycles_requested": 3,
                    "cycles_completed": 3,
                    "candidate_windows_detected": 3,
                    "candidate_window_failures": 0,
                    "candidate_latency_p95_ms": 120.0,
                    "candidate_latency_max_ms": 140.0,
                    "pressure_allocated_mb": 256,
                    "memory_free_percent_before": 63.0,
                    "memory_free_percent_during": 59.0,
                    "text_services_healthy": True,
                    "input_source_id_sha256": "a" * 64,
                },
                "network_access_performed": False,
                "service_start_performed": False,
                "production_port_access_performed": False,
                "launchctl_performed": False,
                "external_write_performed": False,
                "live_magi_state_access_performed": False,
                "temporary_native_ui_performed": True,
                "unsaved_document_cleanup_performed": True,
                "unsaved_documents_remaining": 0,
            }
            stdout = (
                "MAGI_V3_OFFLINE_EVIDENCE="
                + json.dumps(structured, separators=(",", ":"), sort_keys=True)
            )
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return run


def test_schedule_realism_parser_accepts_bound_dynamic_body_totals() -> None:
    legacy_probe = (
        "python",
        "-k",
        "test_seven_day_schedule_10x_arrival_2x_duration_replay_emits_measured_evidence",
        "tests/v3/test_campaign_offline_probes.py",
    )
    completed = successful_runner([])(
        legacy_probe,
        SOURCE_ROOT,
    )
    payload = json.loads(completed.stdout.split("=", 1)[1])
    measurements = payload["measurements"]
    measurements["cron_definitions"] = 7
    measurements["enabled_cron_definitions"] = 5
    realism = measurements["realism_audit"]
    realism_measurements = realism["measurements"]
    realism_measurements.update(
        {
            "cron_definitions": 7,
            "enabled_cron_definitions": 5,
            "production_duration_observations": 5,
            "production_duration_gap_jobs": 0,
            "representative_bodies_allowlisted": 2,
            "representative_bodies_passed": 2,
            "representative_body_gap_jobs": 3,
        }
    )
    realism["body_results"] = realism["body_results"][:2]
    realism["gaps"] = [
        {
            "job_id": f"dynamic-gap-{index}",
            "gap_type": "representative_job_body",
            "reasons": ["NOT_EXACTLY_ALLOWLISTED_FOR_OFFLINE_BODY_EXECUTION"],
        }
        for index in range(3)
    ]
    stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )

    parsed = campaign_runner_module._structured_workload_evidence(
        "seven_day_schedule_10x_arrival_2x_duration_replay",
        stdout,
    )

    assert parsed is not None
    assert parsed["measurements"]["realism_audit"]["measurements"][
        "representative_body_gap_jobs"
    ] == 3


def test_route_workload_invokes_strict_compiler_and_rejects_382_of_430(
    tmp_path: Path,
) -> None:
    release, release_sha = create_release(tmp_path)
    bundle = campaign_runner_module.verify_release_bundle(release, release_sha)
    profile = {"profile_id": "ordinary-week"}
    report = _passing_route_certification(release, profile)
    report["representative_success_path_passed"] = 382
    report["remaining_route_methods"] = 48
    report["measurements"]["representative_success_path_passed"] = 382
    report["measurements"]["remaining_route_methods"] = 48
    report.pop("evidence_sha256")
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
        report, separators=(",", ":"), sort_keys=True
    )

    assert OFFLINE_COMMANDS["346_route_contract_replay"] == (
        "scripts/v3_validation/route_certification.py",
    )
    with pytest.raises(CampaignSafetyError, match="not strict 431/431"):
        campaign_runner_module._structured_workload_evidence(
            "346_route_contract_replay",
            stdout,
            profile,
            bundle,
        )


def test_route_workload_rejects_nonzero_external_storage_attempt_counter(
    tmp_path: Path,
) -> None:
    release, release_sha = create_release(tmp_path)
    bundle = campaign_runner_module.verify_release_bundle(release, release_sha)
    profile = {"profile_id": "ordinary-week"}
    report = _passing_route_certification(release, profile)
    report["safety"]["external_storage_access_attempts"] = 1
    report["safety"]["trace_isolation_attempts"] = {
        "external_storage_access": 1
    }
    report.pop("evidence_sha256")
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
        report, separators=(",", ":"), sort_keys=True
    )

    with pytest.raises(CampaignSafetyError, match="safety proof is invalid"):
        campaign_runner_module._structured_workload_evidence(
            "346_route_contract_replay",
            stdout,
            profile,
            bundle,
        )


def test_route_workload_accepts_attested_zero_external_storage_attempts(
    tmp_path: Path,
) -> None:
    release, release_sha = create_release(tmp_path)
    bundle = campaign_runner_module.verify_release_bundle(release, release_sha)
    profile = {"profile_id": "ordinary-week"}
    report = _passing_route_certification(release, profile)
    stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
        report, separators=(",", ":"), sort_keys=True
    )

    parsed = campaign_runner_module._structured_workload_evidence(
        "346_route_contract_replay",
        stdout,
        profile,
        bundle,
    )

    assert parsed is not None
    assert parsed["safety"]["external_storage_attested"] is True
    assert parsed["safety"]["external_storage_access_attempts"] == 0


def test_route_workload_recomputes_dynamic_seatbelt_profile_hash(
    tmp_path: Path,
) -> None:
    release, release_sha = create_release(tmp_path)
    bundle = campaign_runner_module.verify_release_bundle(release, release_sha)
    profile = {"profile_id": "ordinary-week"}
    report = _passing_route_certification(release, profile)
    report["safety"]["seatbelt"]["profile_sha256"] = "0" * 64
    report.pop("evidence_sha256")
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(report)

    with pytest.raises(CampaignSafetyError, match="safety proof is invalid"):
        campaign_runner_module._structured_workload_evidence(
            "346_route_contract_replay", stdout, profile, bundle
        )


def test_campaign_route_seatbelt_attestation_matches_formal_certifier(
    tmp_path: Path,
) -> None:
    from scripts.v3_validation.route_certification import _seatbelt_attestation

    workspace = (tmp_path / "route-certification" / "ordinary_week").resolve()
    assert campaign_runner_module._route_seatbelt_attestation(
        workspace
    ) == _seatbelt_attestation(workspace)


def test_route_workload_rejects_unbound_seatbelt_workspace_even_with_valid_hash(
    tmp_path: Path,
) -> None:
    release, release_sha = create_release(tmp_path)
    bundle = campaign_runner_module.verify_release_bundle(release, release_sha)
    profile = {"profile_id": "ordinary-week"}
    report = _passing_route_certification(release, profile)
    report["safety"]["seatbelt"] = campaign_runner_module._route_seatbelt_attestation(
        tmp_path / "unbound-workspace"
    )
    report.pop("evidence_sha256")
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(report)

    with pytest.raises(CampaignSafetyError, match="safety proof is invalid"):
        campaign_runner_module._structured_workload_evidence(
            "346_route_contract_replay", stdout, profile, bundle
        )


def test_route_workload_rejects_source_noncertifying_diagnostic(
    tmp_path: Path,
) -> None:
    release, release_sha = create_release(tmp_path)
    bundle = campaign_runner_module.verify_release_bundle(release, release_sha)
    profile = {"profile_id": "ordinary-week"}
    report = _passing_route_certification(release, profile)
    report["certifying"] = False
    report["diagnostic_passed"] = True
    report["runtime_binding"]["certifying"] = False
    report["runtime_binding"]["mode"] = "source_diagnostic"
    report.pop("evidence_sha256")
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(report)

    with pytest.raises(CampaignSafetyError, match="non-certifying diagnostic"):
        campaign_runner_module._structured_workload_evidence(
            "346_route_contract_replay", stdout, profile, bundle
        )


@pytest.mark.parametrize("tamper", ["unbound", "user-site", "manifest-sha"])
def test_route_workload_rejects_unbound_or_tampered_runtime_binding(
    tmp_path: Path, tamper: str
) -> None:
    release, release_sha = create_release(tmp_path)
    bundle = campaign_runner_module.verify_release_bundle(release, release_sha)
    profile = {"profile_id": "ordinary-week"}
    report = _passing_route_certification(release, profile)
    expected = json.loads(json.dumps(report["runtime_binding"]))
    if tamper == "unbound":
        report["runtime_binding"]["pythonpath_roots"].append(
            str(tmp_path / "unbound" / "site-packages")
        )
    elif tamper == "user-site":
        report["runtime_binding"]["pythonpath_roots"][1] = str(
            Path.home() / "Library" / "Python" / "3.14" / "lib" / "python" / "site-packages"
        )
    else:
        report["runtime_binding"]["runtime_manifest_sha256"] = "0" * 64
    report.pop("evidence_sha256")
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(report)

    with pytest.raises(CampaignSafetyError, match="runtime binding is invalid"):
        campaign_runner_module._structured_workload_evidence(
            "346_route_contract_replay",
            stdout,
            profile,
            bundle,
            expected_route_runtime_binding=expected,
        )


def test_schedule_realism_parser_rejects_dynamic_total_drift() -> None:
    legacy_probe = (
        "python",
        "-k",
        "test_seven_day_schedule_10x_arrival_2x_duration_replay_emits_measured_evidence",
        "tests/v3/test_campaign_offline_probes.py",
    )
    completed = successful_runner([])(
        legacy_probe,
        SOURCE_ROOT,
    )
    payload = json.loads(completed.stdout.split("=", 1)[1])
    payload["measurements"]["realism_audit"]["measurements"][
        "representative_body_gap_jobs"
    ] -= 1
    stdout = campaign_runner_module.EVIDENCE_PREFIX + json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )

    with pytest.raises(CampaignSafetyError, match="dynamic coverage totals"):
        campaign_runner_module._structured_workload_evidence(
            "seven_day_schedule_10x_arrival_2x_duration_replay",
            stdout,
        )


def execution_inputs(tmp_path: Path) -> dict[str, object]:
    runtime_root = tmp_path / "execution-runtime"
    python = runtime_root / "bin" / "python"
    _write(python, b"#!/bin/sh\nprintf 'MAGI_V3_PYTHON_OK:3\\n'\n", 0o755)
    _write(
        runtime_root / "pyvenv.cfg",
        (
            f"home = {python.parent}\n"
            "include-system-site-packages = false\n"
            f"executable = {python}\n"
        ).encode("utf-8"),
        0o600,
    )
    _write(
        runtime_root
        / "lib"
        / "python3.14"
        / "site-packages"
        / ".magi-fixture-runtime",
        b"fixture\n",
        0o444,
    )
    runtime_manifest = tmp_path / "execution-inputs" / "python-runtime-manifest.json"
    encoded, report = build_runtime_manifest(python)
    _write(runtime_manifest, encoded, 0o600)
    cron_jobs = tmp_path / "execution-inputs" / "cron-jobs.json"
    _write(cron_jobs, b"[]\n", 0o600)
    cron_sha = hashlib.sha256(cron_jobs.read_bytes()).hexdigest()
    cron_source = tmp_path / "execution-inputs" / "cron-source.json"
    _write(cron_source, cron_jobs.read_bytes(), 0o600)
    cron_source_sha = hashlib.sha256(cron_source.read_bytes()).hexdigest()
    website_root = tmp_path / "execution-inputs" / "website"
    website_admin = website_root / "admin" / "admin_server.py"
    _write(website_admin, b"class AdminHandler: pass\n", 0o444)
    website_admin_sha = hashlib.sha256(website_admin.read_bytes()).hexdigest()
    realpath = python.resolve(strict=True)
    return {
        "python_runtime": python,
        "python_runtime_realpath": realpath,
        "python_runtime_sha256": hashlib.sha256(realpath.read_bytes()).hexdigest(),
        "python_runtime_manifest": runtime_manifest,
        "python_runtime_manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "python_runtime_tree_sha256": report["tree_sha256"],
        "cron_jobs_file": cron_jobs,
        "cron_jobs_sha256": cron_sha,
        "cron_jobs_source_file": cron_source,
        "cron_jobs_source_sha256": cron_source_sha,
        "website_root": website_root,
        "website_admin_sha256": website_admin_sha,
    }


def make_runner(
    tmp_path: Path,
    clock: Clock,
    calls,
    *,
    release: Path | None = None,
    release_sha: str | None = None,
    command_runner=None,
    context: CampaignContext | None = None,
    certifiable_backend: bool = False,
) -> CampaignRunner:
    if release is None:
        release, release_sha = create_release(tmp_path)
    assert release_sha is not None
    runtime_inputs = execution_inputs(tmp_path) if certifiable_backend else {}
    runner = CampaignRunner(
        release_root=release,
        state_dir=tmp_path / "state",
        context=context or release_context(release, release_sha),
        command_runner=(
            None if certifiable_backend else (command_runner or successful_runner(calls))
        ),
        clock=clock,
        **runtime_inputs,
    )
    if certifiable_backend:
        # Preserve the production backend binding while isolating this unit test
        # from actually executing the release fixture's placeholder test files.
        route_runtime_binding = runner._route_runtime_binding()
        synthetic_runner = successful_runner(
            calls,
            route_runtime_binding=route_runtime_binding,
        )

        def run_certifiable_fixture(argv, cwd, validation_profile=None):
            if argv[-1].endswith("scripts/v3_validation/route_certification.py"):
                runner._last_route_runtime_binding = json.loads(
                    json.dumps(route_runtime_binding)
                )
            return synthetic_runner(argv, cwd, validation_profile)

        runner._run_command = run_certifiable_fixture  # type: ignore[method-assign]
    return runner


def test_real_backend_supplies_hash_bound_launcher_environment_and_canonical_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, release_sha = create_release(tmp_path)
    runtime_inputs = execution_inputs(tmp_path)
    runner = CampaignRunner(
        release_root=release,
        state_dir=tmp_path / "state",
        context=release_context(release, release_sha),
        clock=Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc)),
        **runtime_inputs,
    )
    observed: dict[str, object] = {}

    def capture(argv, *, cwd, env, **_kwargs):
        observed.update({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        return subprocess.CompletedProcess(list(argv), 0, "launcher-ok\n", "")

    monkeypatch.setattr(runner, "_verify_python_runtime", lambda: None)
    monkeypatch.setattr(campaign_runner_module.subprocess, "run", capture)

    launcher = release / "bin" / "magi-v3-python"
    result = runner._run_command([str(launcher), "-c", "pass"], release)

    assert result.returncode == 0
    env = observed["env"]
    assert isinstance(env, dict)
    isolated_home = (tmp_path / "state" / "offline-home").resolve()
    canonical_runtime = (
        isolated_home / "Library/Application Support/MAGI/runtime/MAGI_v3"
    )
    assert env["HOME"] == str(isolated_home)
    assert env["MAGI_V3_STATE_DIR"] == str(
        canonical_runtime / "state" / "offline-campaign"
    )
    assert env["MAGI_V3_SHARED_STATE_DIR"] == str(canonical_runtime / "shared")
    assert env["MAGI_JSON_DIR"] == str(canonical_runtime / "shared" / "external")
    assert Path(str(env["MAGI_V3_STATE_DIR"])).is_relative_to(
        canonical_runtime / "state"
    )
    assert env["MAGI_V3_RELEASE_ID"] == runner.bundle.release_id
    assert env["MAGI_V3_RELEASE_MANIFEST"] == str(release / MANIFEST_NAME)
    assert env["MAGI_V3_RELEASE_MANIFEST_SHA256"] == runner.bundle.manifest_sha256
    assert env["MAGI_CRON_JOBS_SOURCE_FILE"] == str(runtime_inputs["cron_jobs_source_file"])

    quality = release / "scripts/v3_validation/release_quality_certification.py"
    runner._run_command(
        [str(launcher), str(quality), "--campaign-evidence"],
        release,
    )
    quality_env = observed["env"]
    assert isinstance(quality_env, dict)
    assert quality_env["MAGI_WEBSITE_ROOT"] == str(runtime_inputs["website_root"])
    assert quality_env["MAGI_WEBSITE_ADMIN_SHA256"] == runtime_inputs[
        "website_admin_sha256"
    ]


def test_real_backend_rejects_tampered_external_website_admin(tmp_path: Path) -> None:
    release, release_sha = create_release(tmp_path)
    runtime_inputs = execution_inputs(tmp_path)
    website_root = runtime_inputs["website_root"]
    assert isinstance(website_root, Path)
    website_admin = website_root / "admin" / "admin_server.py"
    website_admin.chmod(0o644)
    website_admin.write_text(
        "tampered\n", encoding="utf-8"
    )

    with pytest.raises(CampaignSafetyError, match="canonical, hash-bound"):
        CampaignRunner(
            release_root=release,
            state_dir=tmp_path / "state",
            context=release_context(release, release_sha),
            **runtime_inputs,
        )


def test_real_backend_rejects_tampered_original_cron_source(tmp_path: Path) -> None:
    release, release_sha = create_release(tmp_path)
    runtime_inputs = execution_inputs(tmp_path)
    cron_source = runtime_inputs["cron_jobs_source_file"]
    assert isinstance(cron_source, Path)
    cron_source.write_bytes(b"[{\"id\":\"drift\"}]\n")

    with pytest.raises(CampaignSafetyError, match="runtime manifest or cron binding drift"):
        CampaignRunner(
            release_root=release,
            state_dir=tmp_path / "state",
            context=release_context(release, release_sha),
            **runtime_inputs,
        )


def test_real_campaign_backend_executes_the_sealed_release_launcher(tmp_path: Path) -> None:
    release, release_sha = create_real_launcher_release(tmp_path)
    runtime_inputs = execution_inputs(tmp_path)
    runner = CampaignRunner(
        release_root=release,
        state_dir=tmp_path / "real-launcher-state",
        context=release_context(release, release_sha),
        clock=Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc)),
        **runtime_inputs,
    )

    result = runner._run_command(
        [str(release / "bin" / "magi-v3-python"), "-c", "pass"],
        release,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MAGI_V3_PYTHON_OK:3"


def test_unarmed_release_records_hashed_noneligible_day_only_from_release(tmp_path: Path) -> None:
    calls = []
    clock = Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc))
    release, release_sha = create_release(tmp_path)
    runner = make_runner(
        tmp_path,
        clock,
        calls,
        release=release,
        release_sha=release_sha,
        certifiable_backend=True,
    )

    report = runner.run_today()

    assert len(calls) == len(OFFLINE_COMMANDS) * 7
    assert all(cwd == release for _argv, cwd in calls)
    assert all(Path(argv[0]).is_relative_to(release) for argv, _cwd in calls)
    assert all(
        all(
            Path(argument).is_relative_to(release)
            for argument in argv
            if Path(argument).is_absolute()
        )
        for argv, _cwd in calls
    )
    assert report["decision"] == "NO_GO"
    assert report["generated_at"] == clock.value.astimezone(runner.timezone).isoformat()
    assert report["decision_scope"] == "offline_campaign_only"
    assert report["passed_days"] == ["2026-07-14"]
    assert report["evidence_class"] == "immutable_release_offline_campaign"
    assert report["certifying"] is False
    assert report["offline_complete"] is False
    assert report["live_execution_performed"] is False
    assert report["cutover_execution_performed"] is False
    assert "campaign_unarmed" in report["no_go_reasons"]
    assert len(report["arming_blockers"]) == 3
    artifact = tmp_path / "state" / "artifacts" / "day-2026-07-14.json"
    evidence = json.loads(artifact.read_text())
    assert evidence["release_sha"] == release_sha
    assert evidence["release_manifest_sha256"] == report["release_manifest_sha256"]
    assert evidence["status"] == "offline_passed"
    assert evidence["required_independent_passes"] == 7
    assert evidence["completed_independent_passes"] == 7
    assert {item["validation_pass"] for item in evidence["workloads"]} == set(range(1, 8))
    schedule_runs = [
        item
        for item in evidence["workloads"]
        if item["workload"] == "seven_day_schedule_10x_arrival_2x_duration_replay"
    ]
    assert len(schedule_runs) == 7
    assert len({item["validation_profile"]["profile_id"] for item in schedule_runs}) == 7
    assert all(
        item["structured_evidence"]["report"]["validation_profile_id"]
        == item["validation_profile"]["profile_id"]
        for item in schedule_runs
    )
    assert all(item["inner_report_artifact"]["sha256"] for item in schedule_runs)
    assert all(item["body_evidence_artifact"]["sha256"] for item in schedule_runs)
    assert evidence["release_gate_eligible"] is False
    soak = next(
        item
        for item in evidence["workloads"]
        if item["workload"] == "hundred_cycle_worker_reap_soak"
    )
    assert soak["structured_evidence"]["measurements"]["cycles_completed"] == 100
    health = [
        item
        for item in evidence["workloads"]
        if item["workload"] == "health_1000_model_free"
    ]
    assert len(health) == 7
    assert {
        item["structured_evidence"]["report"]["validation_profile"]["profile_id"]
        for item in health
    } == {
        item["validation_profile"]["profile_id"] for item in health
    }
    assert all(item["inner_report_artifact"]["sha256"] for item in health)
    faults = [
        item
        for item in evidence["workloads"]
        if item["workload"] == "fault_recovery_certification"
    ]
    assert len(faults) == 7
    assert all(
        item["structured_evidence"]["report"]["decision"]["hard_gate_blocked"]
        is False
        for item in faults
    )
    assert all(
        item["structured_evidence"]["report"]["decision"][
            "controlled_cold_restart_required_at_cutover"
        ]
        is True
        for item in faults
    )
    assert all(item["inner_report_artifact"]["sha256"] for item in faults)
    with runner._connect() as db:
        row = db.execute("SELECT artifact_sha256 FROM days").fetchone()
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert row[0] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    persisted_report = json.loads((tmp_path / "state" / "campaign-report.json").read_text())
    assert persisted_report == report


def test_same_taipei_day_cannot_execute_or_credit_twice(tmp_path: Path) -> None:
    calls = []
    clock = Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc))
    runner = make_runner(tmp_path, clock, calls)
    runner.run_today()
    first_count = len(calls)

    report = runner.run_today()

    assert len(calls) == first_count
    assert report["passed_days"] == ["2026-07-14"]
    assert report["ran_today"] is False
    assert "offline_campaign_already_complete" in report["no_go_reasons"]
    persisted_report = json.loads((tmp_path / "state" / "campaign-report.json").read_text())
    assert persisted_report == report


def test_integrity_exception_invalidates_previous_go_report(tmp_path: Path) -> None:
    calls = []
    clock = Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc))
    release, release_sha = create_release(tmp_path, armed=True)
    runner = make_runner(
        tmp_path,
        clock,
        calls,
        release=release,
        release_sha=release_sha,
        certifiable_backend=True,
    )
    report = runner.run_today()
    assert report["decision"] == "GO"
    artifact = tmp_path / "state" / "artifacts" / "day-2026-07-14.json"
    artifact.write_text("{}", encoding="utf-8")

    with pytest.raises(CampaignSafetyError, match="SHA-256 mismatch"):
        runner.run_today()

    persisted = json.loads((tmp_path / "state" / "campaign-report.json").read_text())
    assert persisted["decision"] == "NO_GO"
    assert persisted["offline_complete"] is False
    assert persisted["no_go_reasons"] == ["campaign_safety_error"]


def test_campaign_report_removes_go_after_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = make_runner(
        tmp_path,
        Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc)),
        [],
    )
    target = tmp_path / "state" / "campaign-report.json"
    target.write_text('{"decision":"GO"}', encoding="utf-8")
    original_fsync = campaign_runner_module.os.fsync
    calls = 0

    def fail_first_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(campaign_runner_module.os, "fsync", fail_first_directory_fsync)

    with pytest.raises(OSError, match="directory fsync"):
        runner._write_report({"decision": "GO"})
    assert not target.exists()


def test_armed_certified_bundle_needs_seven_independent_passes_within_one_day_for_go(
    tmp_path: Path,
) -> None:
    calls = []
    clock = Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc))
    release, release_sha = create_release(tmp_path, armed=True)
    runner = make_runner(
        tmp_path,
        clock,
        calls,
        release=release,
        release_sha=release_sha,
        certifiable_backend=True,
    )

    report = runner.run_today()

    assert report["offline_complete"] is True
    assert report["decision"] == "GO"
    assert report["fail_closed"] is False
    assert report["certifying"] is True
    assert len(report["passed_days"]) == 1
    assert report["required_independent_passes"] == 7
    assert len(calls) == len(OFFLINE_COMMANDS) * 7
    call_count = len(calls)
    assert runner.run_today()["decision"] == "GO"
    assert len(calls) == call_count


def test_injected_backend_cannot_create_certifying_go_evidence(tmp_path: Path) -> None:
    calls = []
    clock = Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc))
    release, release_sha = create_release(tmp_path, armed=True)
    runner = make_runner(tmp_path, clock, calls, release=release, release_sha=release_sha)

    report = runner.run_today()

    assert report["decision"] == "NO_GO"
    assert report["offline_complete"] is False
    assert report["certifying"] is False
    assert "execution_backend_not_certifiable" in report["no_go_reasons"]


def test_completed_accelerated_day_does_not_require_future_calendar_dates(tmp_path: Path) -> None:
    calls = []
    clock = Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc))
    runner = make_runner(tmp_path, clock, calls)
    runner.run_today()
    first_count = len(calls)
    clock.value += timedelta(days=2)

    report = runner.run_today()
    clock.value += timedelta(days=1)
    later = runner.run_today()

    assert len(calls) == first_count
    assert "offline_campaign_already_complete" in report["no_go_reasons"]
    assert "offline_campaign_already_complete" in later["no_go_reasons"]
    assert report["offline_complete"] is False


def test_accelerated_campaign_hard_fails_after_24_hour_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    ticks = iter((0.0, 86_401.0, 86_402.0))
    monkeypatch.setattr(campaign_runner_module.time, "monotonic", lambda: next(ticks))
    runner = make_runner(
        tmp_path,
        Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc)),
        calls,
    )

    report = runner.run_today()

    assert calls == []
    assert report["decision"] == "NO_GO"
    artifact = tmp_path / "state" / "artifacts" / "day-2026-07-14.json"
    evidence = json.loads(artifact.read_text())
    assert evidence["status"] == "offline_failed"
    assert evidence["elapsed_seconds"] > 86_400
    assert evidence["workloads"][0]["reason"] == "accelerated_campaign_exceeded_24h_window"


def test_workload_failure_and_skips_are_permanent_for_ledger(tmp_path: Path) -> None:
    calls = []

    def fail_first(argv, cwd):
        calls.append((tuple(argv), cwd))
        return subprocess.CompletedProcess(argv, 1, "", "failed")

    clock = Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc))
    runner = make_runner(tmp_path, clock, calls, command_runner=fail_first)
    first = runner.run_today()
    clock.value += timedelta(days=1)
    second = runner.run_today()

    assert len(calls) == 1
    assert "offline_day_failed_or_skipped" in first["no_go_reasons"]
    assert "prior_offline_day_failed" in second["no_go_reasons"]


def test_hundred_cycle_soak_exit_zero_without_measurements_fails_closed(tmp_path: Path) -> None:
    calls = []
    passing = successful_runner(calls)

    def omit_evidence(argv, cwd):
        result = passing(argv, cwd)
        if "-k" in argv and "hundred_cycle_worker_reap_soak" in argv[argv.index("-k") + 1]:
            return subprocess.CompletedProcess(argv, 0, "passed without measurements", "")
        return result

    runner = make_runner(
        tmp_path,
        Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc)),
        calls,
        command_runner=omit_evidence,
    )

    report = runner.run_today()

    assert report["decision"] == "NO_GO"
    assert "offline_day_failed_or_skipped" in report["no_go_reasons"]
    artifact = tmp_path / "state" / "artifacts" / "day-2026-07-14.json"
    evidence = json.loads(artifact.read_text())
    soak = next(
        item
        for item in evidence["workloads"]
        if item["workload"] == "hundred_cycle_worker_reap_soak"
    )
    assert soak["returncode"] == 65
    assert soak["status"] == "offline_failed"
    assert "structured_evidence" not in soak
    assert soak["failure_category"] == "structured_evidence_rejected"
    assert soak["failure_reason"]


def test_structured_evidence_accepts_pytest_progress_before_prefix(tmp_path: Path) -> None:
    calls = []
    passing = successful_runner(calls)

    def progress_prefixed(argv, cwd):
        result = passing(argv, cwd)
        stdout = result.stdout.replace(
            "MAGI_V3_OFFLINE_EVIDENCE=",
            "................MAGI_V3_OFFLINE_EVIDENCE=",
        )
        return subprocess.CompletedProcess(argv, result.returncode, stdout, result.stderr)

    runner = make_runner(
        tmp_path,
        Clock(datetime(2026, 7, 14, 3, tzinfo=timezone.utc)),
        calls,
        command_runner=progress_prefixed,
    )

    report = runner.run_today()

    assert "offline_day_failed_or_skipped" not in report["no_go_reasons"]
    artifact = tmp_path / "state" / "artifacts" / "day-2026-07-14.json"
    workloads = json.loads(artifact.read_text())["workloads"]
    assert all(item["status"] == "offline_passed" for item in workloads)


def test_unknown_workload_is_never_executed(tmp_path: Path) -> None:
    release, release_sha = create_release(tmp_path, add_unknown_workload=True)
    calls = []
    runner = make_runner(
        tmp_path,
        Clock(datetime(2026, 7, 14, tzinfo=timezone.utc)),
        calls,
        release=release,
        release_sha=release_sha,
    )

    with pytest.raises(CampaignSafetyError, match="non-allowlisted"):
        runner.run_today()
    assert calls == []


def test_armed_config_with_machine_readable_blockers_is_rejected(tmp_path: Path) -> None:
    release, release_sha = create_release(tmp_path, armed_with_blockers=True)
    state = tmp_path / "state"

    with pytest.raises(CampaignSafetyError, match="blocker-free"):
        make_runner(
            tmp_path,
            Clock(datetime.now(timezone.utc)),
            [],
            release=release,
            release_sha=release_sha,
        )
    assert not state.exists()


def test_release_sha_is_source_snapshot_sha256_not_commit(tmp_path: Path) -> None:
    release, release_sha = create_release(tmp_path)
    state = tmp_path / "state"
    context = release_context(release, release_sha)

    with pytest.raises(CampaignSafetyError, match="64-character source_snapshot"):
        make_runner(
            tmp_path,
            Clock(datetime.now(timezone.utc)),
            [],
            release=release,
            release_sha=release_sha,
            context=CampaignContext(
                context.campaign_id,
                "a" * 40,
                context.hardware_id,
                context.gate_config_sha256,
            ),
        )
    assert not state.exists()


@pytest.mark.parametrize("tamper_kind", ["content", "extra", "missing", "manifest_hash"])
def test_full_release_inventory_tamper_is_rejected_before_state_creation(
    tmp_path: Path, tamper_kind: str
) -> None:
    release, release_sha = create_release(tmp_path)
    if tamper_kind == "content":
        target = release / "tests/v3/test_campaign_offline_probes.py"
        target.chmod(0o644)
        target.write_text("changed")
    elif tamper_kind == "extra":
        (release / "unexpected.txt").write_text("extra")
    elif tamper_kind == "missing":
        (release / "tests/v3/test_campaign_offline_probes.py").unlink()
    else:
        marker_path = release / COMPLETION_MARKER
        marker = json.loads(marker_path.read_text())
        marker["manifest_sha256"] = "0" * 64
        marker_path.write_bytes(_json_bytes(marker))

    with pytest.raises(CampaignSafetyError):
        make_runner(
            tmp_path,
            Clock(datetime.now(timezone.utc)),
            [],
            release=release,
            release_sha=release_sha,
        )
    assert not (tmp_path / "state").exists()


def test_release_mutation_during_command_records_permanent_integrity_failure(tmp_path: Path) -> None:
    release, release_sha = create_release(tmp_path)
    calls = []

    def mutate_release(argv, cwd):
        calls.append((tuple(argv), cwd))
        target = release / "tests/v3/test_campaign_offline_probes.py"
        target.chmod(0o644)
        target.write_text("changed during campaign")
        return subprocess.CompletedProcess(argv, 0, "passed", "")

    clock = Clock(datetime(2026, 7, 14, tzinfo=timezone.utc))
    runner = make_runner(
        tmp_path,
        clock,
        calls,
        release=release,
        release_sha=release_sha,
        command_runner=mutate_release,
    )

    report = runner.run_today()

    assert len(calls) == 1
    assert "release_changed_during_campaign" in report["no_go_reasons"]
    artifact = tmp_path / "state" / "artifacts" / "day-2026-07-14.json"
    evidence = json.loads(artifact.read_text())
    assert evidence["status"] == "offline_failed"
    assert evidence["release_gate_eligible"] is False
    assert "hash/size/mode mismatch" in evidence["release_integrity_error"]


def test_gate_hash_and_existing_ledger_context_are_exactly_bound(tmp_path: Path) -> None:
    release, release_sha = create_release(tmp_path)
    valid = release_context(release, release_sha)
    clock = Clock(datetime(2026, 7, 14, tzinfo=timezone.utc))
    with pytest.raises(CampaignSafetyError, match="gate config SHA"):
        make_runner(
            tmp_path,
            clock,
            [],
            release=release,
            release_sha=release_sha,
            context=CampaignContext("campaign-1", release_sha, "mac", "0" * 64),
        )
    make_runner(tmp_path, clock, [], release=release, release_sha=release_sha)
    with pytest.raises(CampaignSafetyError, match="ledger context"):
        make_runner(
            tmp_path,
            clock,
            [],
            release=release,
            release_sha=release_sha,
            context=CampaignContext(
                "campaign-2", release_sha, valid.hardware_id, valid.gate_config_sha256
            ),
        )


def test_tampered_artifact_prevents_resume(tmp_path: Path) -> None:
    calls = []
    clock = Clock(datetime(2026, 7, 14, tzinfo=timezone.utc))
    runner = make_runner(tmp_path, clock, calls)
    runner.run_today()
    artifact = tmp_path / "state" / "artifacts" / "day-2026-07-14.json"
    artifact.write_text("{}")
    clock.value += timedelta(days=1)

    with pytest.raises(CampaignSafetyError, match="SHA-256 mismatch"):
        runner.run_today()


def test_state_directory_inside_release_or_live_runtime_is_refused(tmp_path: Path) -> None:
    release, release_sha = create_release(tmp_path)
    context = release_context(release, release_sha)
    clock = Clock(datetime(2026, 7, 14, tzinfo=timezone.utc))
    with pytest.raises(CampaignSafetyError, match="immutable release"):
        CampaignRunner(
            release_root=release,
            state_dir=release / ".campaign-state",
            context=context,
            command_runner=successful_runner([]),
            clock=clock,
        )
    live_candidate = (
        Path.home() / "Library" / "Application Support" / "MAGI" / "campaign-never-create"
    )
    with pytest.raises(CampaignSafetyError, match="live MAGI runtime"):
        CampaignRunner(
            release_root=release,
            state_dir=live_candidate,
            context=context,
            command_runner=successful_runner([]),
            clock=clock,
        )
    assert not live_candidate.exists()


def test_symlink_in_release_is_rejected(tmp_path: Path) -> None:
    release, release_sha = create_release(tmp_path)
    target = release / "tests/v3/test_campaign_offline_probes.py"
    external = tmp_path / "external.py"
    external.write_text("external")
    target.unlink()
    os.symlink(external, target)

    with pytest.raises(CampaignSafetyError, match="symlinks are forbidden"):
        make_runner(
            tmp_path,
            Clock(datetime.now(timezone.utc)),
            [],
            release=release,
            release_sha=release_sha,
        )
