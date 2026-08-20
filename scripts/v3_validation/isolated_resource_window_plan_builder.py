#!/usr/bin/env python3
"""Build, but never execute, an immutable provisional resource-window plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v3_validation.isolated_resource_window import sha256_json
from scripts.v3_validation.isolated_resource_window_collector import (
    PLAN_SCHEMA,
    REQUIRED_MODEL_OWNER_PATTERNS,
    REQUIRED_STOPPED_LABELS,
)


PROVISIONAL_OUTER_SCHEMA = "magi.v3.provisional-resource-window-plan/v1"
PROVISIONAL_GATE_SCHEMA = "magi.v3.provisional-resource-window-gate/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RESOURCE_GATE_IDS = (
    "matched_v2_warm_cold_performance_baseline_complete",
    "resource_policy_all_budgets_passed",
    "worker_process_group_footprint_and_metal_return_to_baseline",
)
PROVISIONAL_GATE_IDS = tuple(
    sorted(
        {
            "portable_source_inventory_current",
            "runtime_route_inventory_current",
            "v2_regression_passed_in_release_venv",
            "v3_unit_contract_integration_e2e_passed",
            "interaction_agent_kernel_memory_quality_contracts_passed",
            "context_memory_tool_plan_answer_golden_sets_passed",
            "golden_side_effect_diff_approved",
            "health_1000_probes_loaded_zero_models",
            "seven_day_schedule_10x_arrival_2x_duration_replay_passed",
            "heavy_plus_interactive_preemption_benchmark_passed",
            "hundred_cycle_worker_reap_soak_passed",
            "sqlite_wal_disk_full_fsync_faults_passed",
            "notification_storm_and_dlq_faults_passed",
            "database_backup_restore_drill_passed",
            "runtime_state_snapshot_verified",
            "rendered_launchagent_manifest_checksums_saved",
        }
    )
)


class ResourceWindowPlanError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ResourceWindowPlanError("model tree contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        count += 1
    if count == 0:
        raise ResourceWindowPlanError("model tree is empty")
    return digest.hexdigest()


def _regular(path: Path, description: str) -> Path:
    if not path.is_absolute() or path.resolve(strict=False) != path:
        raise ResourceWindowPlanError(f"{description} must be canonical and absolute")
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ResourceWindowPlanError(f"{description} must be a non-symlink file")
    return path


def _python_runtime_binding(runtime: Path, release_root: Path, files: Mapping[str, str]) -> dict[str, str]:
    """Bind either a release member or the launcher's verified external runtime.

    Production candidates intentionally contain a small shell launcher rather
    than a second Python installation.  The resource collector must execute
    the already verified Python binary directly: invoking the shell launcher
    inside its disposable HOME would require canonical-runtime state before
    the adapter has had a chance to create it.
    """

    runtime_sha256 = _sha256(runtime)
    try:
        relative = runtime.relative_to(release_root).as_posix()
    except ValueError:
        launcher_path_raw = os.environ.get("MAGI_V3_PYTHON_RUNTIME", "")
        expected_realpath = os.environ.get("MAGI_V3_PYTHON_RUNTIME_REALPATH", "")
        expected_sha256 = os.environ.get("MAGI_V3_PYTHON_RUNTIME_SHA256", "")
        manifest_raw = os.environ.get("MAGI_V3_PYTHON_RUNTIME_MANIFEST", "")
        manifest_sha256 = os.environ.get(
            "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256", ""
        )
        tree_sha256 = os.environ.get("MAGI_V3_PYTHON_RUNTIME_TREE_SHA256", "")
        try:
            launcher_path = Path(launcher_path_raw).resolve(strict=True)
            manifest = Path(manifest_raw).resolve(strict=True)
        except (OSError, RuntimeError):
            launcher_path = Path()
            manifest = Path()
        if (
            runtime != Path(sys.executable).resolve(strict=True)
            or launcher_path != runtime
            or expected_realpath != str(runtime)
            or expected_sha256 != runtime_sha256
            or not manifest.is_absolute()
            or manifest == Path()
            or manifest.is_symlink()
            or not manifest.is_file()
            or not SHA256_RE.fullmatch(manifest_sha256)
            or _sha256(manifest) != manifest_sha256
            or not SHA256_RE.fullmatch(tree_sha256)
        ):
            raise ResourceWindowPlanError(
                "external Python runtime is not bound to the verified launcher runtime"
            )
        return {
            "kind": "manifest_bound_external",
            "path": str(runtime),
            "launcher_path": launcher_path_raw,
            "sha256": runtime_sha256,
            "realpath": str(runtime),
            "manifest": str(manifest),
            "manifest_sha256": manifest_sha256,
            "tree_sha256": tree_sha256,
        }
    if files.get(relative) != runtime_sha256:
        raise ResourceWindowPlanError("resource-window Python runtime is not release-bound")
    return {
        "kind": "release_member",
        "path": str(runtime),
        "launcher_path": str(runtime),
        "sha256": runtime_sha256,
        "realpath": str(runtime),
        "manifest": "",
        "manifest_sha256": "",
        "tree_sha256": "",
    }


def _external_website(
    website_root: Path,
    expected_admin_sha256: str,
    *,
    release_root: Path,
) -> tuple[Path, Path]:
    """Resolve the deploy-style external Website Admin binding fail closed."""

    if not SHA256_RE.fullmatch(expected_admin_sha256):
        raise ResourceWindowPlanError("external Website Admin SHA-256 is invalid")
    if (
        not website_root.is_absolute()
        or website_root.resolve(strict=False) != website_root
    ):
        raise ResourceWindowPlanError("external website root must be canonical and absolute")
    try:
        metadata = website_root.lstat()
        resolved = website_root.resolve(strict=True)
    except OSError as exc:
        raise ResourceWindowPlanError(f"external website root is missing: {exc}") from exc
    if (
        resolved != website_root
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or website_root.is_relative_to(release_root)
    ):
        raise ResourceWindowPlanError(
            "external website root must be a non-symlink directory outside the release"
        )
    admin = website_root / "admin" / "admin_server.py"
    try:
        resolved_admin = admin.resolve(strict=True)
    except OSError as exc:
        raise ResourceWindowPlanError(f"external Website Admin source is missing: {exc}") from exc
    if resolved_admin != admin:
        raise ResourceWindowPlanError("external Website Admin path must not traverse symlinks")
    _regular(admin, "external Website Admin source")
    if _sha256(admin) != expected_admin_sha256:
        raise ResourceWindowPlanError("external Website Admin SHA-256 mismatch")
    return website_root, admin


def _external_file(
    path: Path,
    expected_sha256: str,
    *,
    description: str,
    release_root: Path,
) -> Path:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ResourceWindowPlanError(f"external {description} SHA-256 is invalid")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ResourceWindowPlanError(f"external {description} is missing: {exc}") from exc
    if resolved != path or path.is_relative_to(release_root):
        raise ResourceWindowPlanError(
            f"external {description} must be canonical and outside the release"
        )
    _regular(path, f"external {description}")
    if _sha256(path) != expected_sha256:
        raise ResourceWindowPlanError(f"external {description} SHA-256 mismatch")
    return path


def _json(path: Path, description: str) -> dict[str, Any]:
    value = json.loads(_regular(path, description).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ResourceWindowPlanError(f"{description} must be a JSON object")
    return value


def _deep_release(release_root: Path, expected_manifest_sha256: str) -> tuple[dict[str, Any], dict[str, str]]:
    if not release_root.is_absolute() or release_root.resolve(strict=True) != release_root:
        raise ResourceWindowPlanError("release root must be canonical and absolute")
    if release_root.stat().st_mode & 0o222:
        raise ResourceWindowPlanError("release root must be immutable")
    manifest_path = _regular(release_root / "release-manifest.json", "release manifest")
    if manifest_path.stat().st_mode & 0o222:
        raise ResourceWindowPlanError("release manifest must be immutable")
    if _sha256(manifest_path) != expected_manifest_sha256:
        raise ResourceWindowPlanError("release manifest SHA-256 mismatch")
    manifest = _json(manifest_path, "release manifest")
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise ResourceWindowPlanError("release file inventory is missing")
    files: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ResourceWindowPlanError("release file row is invalid")
        relative = str(row.get("path") or "")
        expected = str(row.get("sha256") or "")
        if relative in files:
            raise ResourceWindowPlanError(f"duplicate release member: {relative}")
        path = _regular(release_root / relative, f"release member {relative}")
        for parent in path.parents:
            if parent == release_root.parent:
                break
            metadata = parent.lstat()
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_mode & 0o222:
                raise ResourceWindowPlanError(
                    f"release member parent is mutable/linked: {relative}"
                )
            if parent == release_root:
                break
        if (
            not path.is_relative_to(release_root)
            or path.stat().st_mode & 0o222
            or _sha256(path) != expected
        ):
            raise ResourceWindowPlanError(f"release member hash mismatch: {relative}")
        files[relative] = expected
    if any(Path(relative).parts[:1] == ("whalechao.github.io",) for relative in files):
        raise ResourceWindowPlanError("external website must not be bundled in the release")
    if (
        not isinstance(manifest.get("release_id"), str)
        or not manifest["release_id"]
        or manifest.get("source_snapshot_sha256") != manifest.get("release_sha256")
    ):
        raise ResourceWindowPlanError("release identity/source snapshot is invalid")
    return manifest, files


def _exclusive_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _semantic_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        (json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def _policy_thresholds(policy: Mapping[str, Any]) -> dict[str, Any]:
    authoritative = policy.get("authoritative_resource_window")
    budgets = policy.get("budgets")
    performance = policy.get("performance_slo")
    if not all(isinstance(value, dict) for value in (authoritative, budgets, performance)):
        raise ResourceWindowPlanError("resource policy lacks authoritative window sections")
    assert isinstance(authoritative, dict)
    assert isinstance(budgets, dict)
    assert isinstance(performance, dict)
    core = budgets.get("v3_release_core_idle")
    idle = budgets.get("total_magi_deep_idle")
    interactive = budgets.get("interactive_session")
    active = budgets.get("total_magi_active")
    if not all(isinstance(value, dict) for value in (core, idle, interactive, active)):
        raise ResourceWindowPlanError("resource policy budget profiles are incomplete")
    resolved = {
        "minimum_model_tokens_per_second_ratio": authoritative.get(
            "minimum_model_tokens_per_second_ratio"
        ),
        "minimum_application_plane_footprint_reduction_ratio": authoritative.get(
            "minimum_application_plane_footprint_reduction_ratio"
        ),
        "maximum_idle_swapout_growth_mb": performance.get(
            "swapout_growth_mb_per_30_min_max"
        ),
        "worker_metal_return_to_baseline_seconds": performance.get(
            "worker_metal_return_to_baseline_seconds"
        ),
        "worker_footprint_return_to_baseline_seconds": performance.get(
            "worker_footprint_return_to_baseline_seconds"
        ),
        "agx_drift_tolerance_bytes": authoritative.get("agx_drift_tolerance_bytes"),
        "core_max_footprint_mb": core.get("max_footprint_mb"),
        "core_max_average_cpu_percent": core.get("max_average_cpu_percent"),
        "core_max_p95_cpu_percent": core.get("max_p95_cpu_percent"),
        "core_max_heavy_framework_imports": core.get("heavy_framework_imports"),
        "idle_max_footprint_mb": idle.get("max_footprint_mb"),
        "idle_max_loaded_models": idle.get("max_loaded_models"),
        "idle_max_python_service_processes": idle.get("max_python_service_processes"),
        "idle_max_background_heavy_workers": idle.get("max_background_heavy_workers"),
        "interactive_max_loaded_primary_models": interactive.get(
            "max_loaded_primary_models"
        ),
        "interactive_max_background_heavy_workers": interactive.get(
            "max_background_heavy_workers"
        ),
        "interactive_max_browser_workers": interactive.get("max_browser_workers"),
        "interactive_min_foreground_memory_reserve_mb": interactive.get(
            "foreground_memory_reserve_mb"
        ),
        "interactive_max_magi_metal_footprint_mb": interactive.get(
            "max_magi_metal_footprint_mb"
        ),
        "active_hard_footprint_mb": active.get("hard_footprint_mb"),
        "active_hard_metal_mb": active.get("hard_metal_mb"),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        for value in resolved.values()
    ):
        raise ResourceWindowPlanError("resource policy resolved an invalid threshold")
    if (
        authoritative.get("schema")
        != "magi.v3.authoritative-resource-window-policy/v1"
        or authoritative.get("per_process_gpu_permission_required") is not True
        or authoritative.get("seatbelt_network_and_live_state_denial_required") is not True
        or authoritative.get("production_composition_required") is not True
        or authoritative.get("shared_direct_model_backend_forbidden") is not True
        or authoritative.get("one_time_plan_consumption_required") is not True
    ):
        raise ResourceWindowPlanError("authoritative resource-window policy is weakened")
    return resolved


def _verify_outer(
    outer_path: Path,
    outer: Mapping[str, Any],
    *,
    outer_file_sha256: str,
    release_manifest_path: Path,
    release_manifest_sha256: str,
) -> dict[str, Any]:
    unsigned = dict(outer)
    supplied = unsigned.pop("plan_sha256", None)
    release = outer.get("release_manifest")
    gate_binding = outer.get("provisional_gate_report")
    context = outer.get("context")
    if (
        outer.get("schema") != PROVISIONAL_OUTER_SCHEMA
        or outer.get("operation") != "isolated_resource_window_validation"
        or supplied != _semantic_sha256(unsigned)
        or not isinstance(release, dict)
        or Path(str(release.get("path") or "")).resolve(strict=True)
        != release_manifest_path
        or release.get("sha256") != release_manifest_sha256
        or not isinstance(gate_binding, dict)
        or not isinstance(context, dict)
    ):
        raise ResourceWindowPlanError("outer provisional plan identity is invalid")
    gate_path = _regular(
        Path(str(gate_binding.get("path") or "")).resolve(strict=True),
        "provisional gate",
    )
    gate_sha = str(gate_binding.get("sha256") or "")
    gate = _json(gate_path, "provisional gate")
    if (
        gate_path.stat().st_mode & 0o222
        or _sha256(gate_path) != gate_sha
        or gate.get("schema") != PROVISIONAL_GATE_SCHEMA
        or gate.get("status") != "provisional_16_of_19_passed"
        or gate.get("formal_live_eligible") is not False
        or gate.get("excluded_resource_evidence") != sorted(RESOURCE_GATE_IDS)
        or gate.get("required_evidence") != list(PROVISIONAL_GATE_IDS)
        or gate.get("counts")
        != {"required": 16, "passed": 16, "failed": 0, "missing": 0, "invalid": 0}
        or gate.get("release_manifest_sha256") != release_manifest_sha256
        or any(gate.get(key) != context.get(key) for key in (
            "campaign_id", "release_sha", "hardware_id", "gate_config_sha256"
        ))
    ):
        raise ResourceWindowPlanError("provisional 16/19 gate is not authoritative")
    return {
        "outer_plan_file_sha256": outer_file_sha256,
        "outer_plan_semantic_sha256": str(supplied),
        "provisional_gate_path": str(gate_path),
        "provisional_gate_sha256": gate_sha,
        "provisional_gate_context": dict(context),
    }


def build_plan(
    *,
    release_root: Path,
    release_manifest_sha256: str,
    python_runtime: Path,
    model_root: Path,
    prompt_path: Path,
    outer_plan: Path,
    outer_plan_sha256: str,
    output_plan: Path,
    output_token: Path,
    workdir: Path,
    model_backend_kind: str,
    website_root: Path,
    website_admin_sha256: str,
    config_file: Path,
    config_sha256: str,
    google_credentials_file: Path,
    google_credentials_sha256: str,
    google_calendar_token_file: Path,
    google_calendar_token_sha256: str,
    laf_gmail_token_file: Path,
    laf_gmail_token_sha256: str,
    file_review_token_file: Path,
    file_review_token_sha256: str,
) -> dict[str, str]:
    if output_plan.exists() or output_token.exists() or workdir.exists():
        raise ResourceWindowPlanError("plan/token/workdir output already exists")
    release_root = release_root.resolve(strict=True)
    manifest, files = _deep_release(release_root, release_manifest_sha256)
    website, website_admin = _external_website(
        website_root,
        website_admin_sha256,
        release_root=release_root,
    )
    external_files = {
        "laf_config_file": _external_file(
            config_file,
            config_sha256,
            description="runtime config",
            release_root=release_root,
        ),
        "google_credentials_file": _external_file(
            google_credentials_file,
            google_credentials_sha256,
            description="Google credentials",
            release_root=release_root,
        ),
        "google_calendar_token_file": _external_file(
            google_calendar_token_file,
            google_calendar_token_sha256,
            description="Google Calendar token",
            release_root=release_root,
        ),
        "laf_gmail_token_file": _external_file(
            laf_gmail_token_file,
            laf_gmail_token_sha256,
            description="LAF Gmail token",
            release_root=release_root,
        ),
        "file_review_token_file": _external_file(
            file_review_token_file,
            file_review_token_sha256,
            description="FileReview token",
            release_root=release_root,
        ),
    }
    for name in ("laf_config_file", "google_credentials_file"):
        if stat.S_IMODE(external_files[name].stat().st_mode) != 0o600:
            raise ResourceWindowPlanError(f"external {name} permissions must be exactly 0600")
    runtime = _regular(python_runtime.resolve(strict=True), "Python runtime")
    runtime_binding = _python_runtime_binding(runtime, release_root, files)
    if not os.access(runtime, os.X_OK):
        raise ResourceWindowPlanError("resource-window Python runtime is not executable")
    prompt = _regular(prompt_path.resolve(strict=True), "model prompt")
    model = model_root.resolve(strict=True)
    if not model.is_dir() or model_backend_kind not in {"mlx_lm", "mlx_vlm"}:
        raise ResourceWindowPlanError("model root/backend is invalid")
    outer_path = outer_plan.resolve(strict=True)
    outer = _json(outer_path, "outer provisional plan")
    if outer_plan.stat().st_mode & 0o222 or _sha256(outer_plan) != outer_plan_sha256:
        raise ResourceWindowPlanError("outer provisional plan is mutable/hash-mismatched")
    outer_binding = _verify_outer(
        outer_path,
        outer,
        outer_file_sha256=outer_plan_sha256,
        release_manifest_path=release_root / "release-manifest.json",
        release_manifest_sha256=release_manifest_sha256,
    )
    core_adapter = release_root / "scripts/v3_validation/resource_window_core_adapter.py"
    model_adapter = release_root / "scripts/v3_validation/resource_window_model_adapter.py"
    collector = release_root / "scripts/v3_validation/isolated_resource_window_collector.py"
    sandbox_profile = release_root / "config/v3_resource_window.sb"
    policy_path = release_root / "config/v3_resource_policy.json"
    production_sources = (
        release_root / "scripts/ops/run_daemon_no_site.py",
        release_root / "daemon.py",
        release_root / "magi_v3/control.py",
        release_root / "magi_v3/gateway.py",
        release_root / "magi_v3/supervisor_service.py",
        release_root / "config/v3_service_manifest.json",
        release_root / "config/v3_launchagent_roles.json",
    )
    for path in (core_adapter, model_adapter, collector, sandbox_profile, *production_sources):
        relative = path.relative_to(release_root).as_posix()
        if not path.is_file() or files.get(relative) != _sha256(path):
            raise ResourceWindowPlanError(f"required command is not manifest-bound: {relative}")
    policy_raw = policy_path.read_text(encoding="utf-8")
    policy = json.loads(policy_raw)
    if not isinstance(policy, dict):
        raise ResourceWindowPlanError("resource policy must be a JSON object")
    thresholds = _policy_thresholds(policy)
    workload_request = {
        "schema": "magi.v3.resource-window-matched-request/v1",
        "corpus_sha256": _sha256(prompt),
        "model_tree_sha256": _tree_sha256(model),
        "max_tokens": 256,
        "temperature": 0,
        "seed": 63181107,
        "repeats_per_arm": 3,
    }
    http_request = {
        "prompt": prompt.read_text(encoding="utf-8"),
        "model": model.name,
        "timeout_sec": 900,
        "allow_fallback": False,
        "allow_template_fallback": False,
    }
    external_inputs = {
        "website_root": str(website),
        "website_admin_sha256": _sha256(website_admin),
        "laf_config_file": str(external_files["laf_config_file"]),
        "google_credentials_file": str(external_files["google_credentials_file"]),
        "laf_config_sha256": config_sha256,
        "laf_config_mode": "0600",
        "google_credentials_sha256": google_credentials_sha256,
        "google_credentials_mode": "0600",
        "google_calendar_token_source_file": str(external_files["google_calendar_token_file"]),
        "google_calendar_token_source_sha256": google_calendar_token_sha256,
        "laf_gmail_token_source_file": str(external_files["laf_gmail_token_file"]),
        "laf_gmail_token_source_sha256": laf_gmail_token_sha256,
        "file_review_token_source_file": str(external_files["file_review_token_file"]),
        "file_review_token_source_sha256": file_review_token_sha256,
    }
    composition = {
        "schema": "magi.v3.resource-window-production-composition/v1",
        "v2": {
            "entrypoint": "scripts/ops/run_daemon_no_site.py",
            "members": {
                path.relative_to(release_root).as_posix(): files[
                    path.relative_to(release_root).as_posix()
                ]
                for path in production_sources[:2]
            },
        },
        "v3": {
            "entrypoints": {
                "control": "magi_v3.control",
                "gateway": "magi_v3.gateway",
                "supervisor": "magi_v3.supervisor_service",
            },
            "members": {
                path.relative_to(release_root).as_posix(): files[
                    path.relative_to(release_root).as_posix()
                ]
                for path in production_sources[2:]
            },
        },
        "external_inputs": external_inputs,
        "arm_transport": "arm_owned_production_process",
        "shared_direct_backend": False,
    }
    composition["composition_sha256"] = sha256_json(composition)
    approval_token = secrets.token_urlsafe(48)
    phase_token = secrets.token_urlsafe(48)
    token_payload = {
        "schema": "magi.v3.isolated-resource-window-tokens/v1",
        "approval_token": approval_token,
        "zero_owner_phase_token": phase_token,
        "outer_plan_sha256": outer_plan_sha256,
        "release_manifest_sha256": release_manifest_sha256,
        "provisional_gate_sha256": outer_binding["provisional_gate_sha256"],
    }
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "approval_token_sha256": hashlib.sha256(approval_token.encode()).hexdigest(),
        "workdir": str(workdir.resolve()),
        "consumption_receipt_path": str(
            (workdir.resolve().parent / f".{workdir.resolve().name}.consumed.json")
        ),
        "production_ports": [5002, 5003, 5014, 8080, 8081, 8088, 18080],
        "v3_owner_markers": [str(release_root), str(runtime)],
        "v3_pidfiles": [
            str(workdir.resolve() / f"{role}.pid")
            for role in ("control", "gateway", "supervisor")
        ],
        "v3_launch_labels": [
            "com.magi.v3.control",
            "com.magi.v3.gateway",
            "com.magi.v3.supervisor",
        ],
        "orchestration_binding": {
            "caller": "scripts.v3_validation.isolated_live_execute",
            "phase": "resource_window_after_v2_zero_owner",
            "v2_restore_owner": "outer_isolated_live_executor_finally",
            "collector_may_stop_or_restore_v2": False,
            "zero_owner_phase_token_sha256": hashlib.sha256(phase_token.encode()).hexdigest(),
            "outer_plan_sha256": outer_plan_sha256,
            **outer_binding,
        },
        "outer_owner_contract": {
            "required_stopped_launchd_labels": list(REQUIRED_STOPPED_LABELS),
            "required_absent_process_patterns": list(REQUIRED_MODEL_OWNER_PATTERNS),
            "zero_owner_snapshot_required_coverage": [
                "launchd",
                "ownership",
                "pidfile",
                "port",
                "process",
            ],
            "outer_must_capture_initial_label_state": True,
            "outer_finally_restore_initial_label_state_exactly": True,
            "outer_restore_readiness": {
                "v2": [
                    "http://127.0.0.1:5002/health",
                    "http://127.0.0.1:5003/health",
                    "http://127.0.0.1:5014/health",
                    "http://127.0.0.1:8088/health",
                ],
                "model_hosts_if_initially_active": [
                    "http://127.0.0.1:8080/v1/models",
                    "http://127.0.0.1:8081/v1/models",
                ],
            },
            "restore_proof_owner": "outer_isolated_live_executor_finally",
        },
        "release_binding": {
            "release_id": manifest["release_id"],
            "release_root": str(release_root),
            "release_manifest_sha256": release_manifest_sha256,
            "release_snapshot_sha256": manifest["source_snapshot_sha256"],
            "python_runtime": str(runtime),
            "python_runtime_sha256": _sha256(runtime),
            "python_runtime_binding": runtime_binding,
            "resource_policy_sha256": files["config/v3_resource_policy.json"],
            "model_root": str(model),
            "model_tree_sha256": _tree_sha256(model),
            "model_backend": str(model_adapter),
            "model_backend_sha256": files[
                "scripts/v3_validation/resource_window_model_adapter.py"
            ],
            "prompt_path": str(prompt),
            "prompt_sha256": _sha256(prompt),
            "sandbox_profile": str(sandbox_profile),
            "sandbox_profile_sha256": files["config/v3_resource_window.sb"],
        },
        "external_inputs": external_inputs,
        "policy_binding": {
            "policy_raw_json": policy_raw,
            "policy_raw_sha256": files["config/v3_resource_policy.json"],
            "resolved_thresholds": thresholds,
            "resolved_thresholds_sha256": sha256_json(thresholds),
        },
        "workload_binding": {
            "request": workload_request,
            "request_sha256": sha256_json(workload_request),
            "http_request_sha256": hashlib.sha256(
                json.dumps(
                    http_request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "composition": composition,
            "same_corpus_model_request_required": True,
        },
        "commands": {
            "v2_core": [
                [
                    str(runtime), str(core_adapter), "--arm", "v2", "--role", "application",
                    "--release-root", str(release_root),
                ]
            ],
            "v3_core": [
                [
                    str(runtime), str(core_adapter), "--arm", "v3", "--role", role,
                    "--release-root", str(release_root),
                ]
                for role in ("control", "supervisor", "gateway")
            ],
            "v2_model": [
                str(runtime),
                str(model_adapter),
                "--arm",
                "v2-reference",
                "--backend",
                model_backend_kind,
                "--model",
                str(model),
                "--prompt",
                str(prompt),
                "--max-tokens",
                "256",
                "--model-port",
                "18080",
                "--arm-endpoint",
                "http://127.0.0.1:5003/collab/chat",
            ],
            "v3_model": [
                str(runtime),
                str(model_adapter),
                "--arm",
                "v3-candidate",
                "--backend",
                model_backend_kind,
                "--model",
                str(model),
                "--prompt",
                str(prompt),
                "--max-tokens",
                "256",
                "--model-port",
                "18080",
                "--arm-endpoint",
                "http://127.0.0.1:5003/collab/chat",
            ],
            "model_repeats": 3,
        },
        "durations": {
            "negative_control_seconds": 30,
            "v2_reference_seconds": 60,
            "v3_deep_idle_seconds": 1800,
            "sample_interval_seconds": 10,
            "model_timeout_seconds": 900,
            "model_sample_interval_seconds": 1,
            "stop_grace_seconds": 10,
        },
        "thresholds": thresholds,
    }
    plan["plan_sha256"] = sha256_json(plan)
    token_data = (json.dumps(token_payload, sort_keys=True, indent=2) + "\n").encode()
    plan_data = (json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    try:
        _exclusive_write(output_token.resolve(), token_data, 0o600)
        _exclusive_write(output_plan.resolve(), plan_data, 0o400)
    except Exception:
        output_plan.unlink(missing_ok=True)
        output_token.unlink(missing_ok=True)
        raise
    return {
        "status": "prepared_not_executed",
        "release_id": manifest["release_id"],
        "plan_path": str(output_plan.resolve()),
        "plan_sha256": plan["plan_sha256"],
        "token_path": str(output_token.resolve()),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--model-backend-kind", choices=("mlx_lm", "mlx_vlm"), required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--outer-plan", type=Path, required=True)
    parser.add_argument("--outer-plan-sha256", required=True)
    parser.add_argument("--output-plan", type=Path, required=True)
    parser.add_argument("--output-token", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--website-root", type=Path, required=True)
    parser.add_argument("--website-admin-sha256", required=True)
    parser.add_argument("--config-file", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--google-credentials-file", type=Path, required=True)
    parser.add_argument("--google-credentials-sha256", required=True)
    parser.add_argument("--google-calendar-token-file", type=Path, required=True)
    parser.add_argument("--google-calendar-token-sha256", required=True)
    parser.add_argument("--laf-gmail-token-file", type=Path, required=True)
    parser.add_argument("--laf-gmail-token-sha256", required=True)
    parser.add_argument("--file-review-token-file", type=Path, required=True)
    parser.add_argument("--file-review-token-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = build_plan(
            release_root=args.release_root,
            release_manifest_sha256=args.release_manifest_sha256,
            python_runtime=args.python_runtime,
            model_root=args.model_root,
            prompt_path=args.prompt,
            outer_plan=args.outer_plan,
            outer_plan_sha256=args.outer_plan_sha256,
            output_plan=args.output_plan,
            output_token=args.output_token,
            workdir=args.workdir,
            model_backend_kind=args.model_backend_kind,
            website_root=args.website_root,
            website_admin_sha256=args.website_admin_sha256,
            config_file=args.config_file,
            config_sha256=args.config_sha256,
            google_credentials_file=args.google_credentials_file,
            google_credentials_sha256=args.google_credentials_sha256,
            google_calendar_token_file=args.google_calendar_token_file,
            google_calendar_token_sha256=args.google_calendar_token_sha256,
            laf_gmail_token_file=args.laf_gmail_token_file,
            laf_gmail_token_sha256=args.laf_gmail_token_sha256,
            file_review_token_file=args.file_review_token_file,
            file_review_token_sha256=args.file_review_token_sha256,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    # Deliberately report only the token file path, never either token value.
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
