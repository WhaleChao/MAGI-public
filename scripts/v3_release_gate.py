#!/usr/bin/env python3
"""Fail-closed MAGI V3 release evidence evaluator.

This command is intentionally read-only with respect to services.  It only reads
the cutover policy and evidence documents, then writes (or prints) a decision.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import plistlib
import re
import sqlite3
import stat
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

if __package__ in {None, ""}:  # Support ``python scripts/v3_release_gate.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.v3_source_contract import SourceContractError, account_home, resolve_source_contract
from scripts.architecture.generate_v2_inventory import (
    build_inventory,
    collect_portable_cron_bytes,
    project_inventory_to_release,
)
from scripts.v3_validation.release_quality_evidence import (
    EXPECTED_GOLDEN_SETS,
    EXPECTED_QUALITY_GROUPS,
    EXPECTED_V3_SUITES,
    GOLDEN_DEPENDENCY_PATHS,
    ReleaseQualityEvidenceError,
    summarize_report as summarize_release_quality_report,
)
from scripts.v3_validation.resource_performance_evidence import (
    GATE_IDS as RESOURCE_PERFORMANCE_GATE_IDS,
    ResourcePerformanceEvidenceError,
    summarize_report as summarize_resource_performance_report,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVIDENCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
DEFAULT_MAX_EVIDENCE_AGE_HOURS = 24.0
CONTEXT_FIELDS = ("campaign_id", "release_sha", "hardware_id", "gate_config_sha256")
TRUSTED_NORMALIZER = "scripts.v3_evidence_compiler"
TRUSTED_NORMALIZER_SCHEMA = "magi.v3.trusted-evidence-normalizer/v1"
FORMAL_BACKUP_DATABASES = frozenset(
    {
        ".agent/jobs/job_queue.db",
        ".agent/mq/message_queue.db",
        ".runtime/conversation_history.sqlite3",
        ".runtime/taiwan_legal_mcp/cache.sqlite3",
    }
)
NORMALIZED_EVIDENCE_WHITELIST = frozenset(
    {
        "portable_source_inventory_current",
        "runtime_route_inventory_current",
        "v2_regression_passed_in_release_venv",
        "v3_unit_contract_integration_e2e_passed",
        "interaction_agent_kernel_memory_quality_contracts_passed",
        "context_memory_tool_plan_answer_golden_sets_passed",
        "golden_side_effect_diff_approved",
        "matched_v2_warm_cold_performance_baseline_complete",
        "resource_policy_all_budgets_passed",
        "heavy_plus_interactive_preemption_benchmark_passed",
        "worker_process_group_footprint_and_metal_return_to_baseline",
        "hundred_cycle_worker_reap_soak_passed",
        "notification_storm_and_dlq_faults_passed",
        "database_backup_restore_drill_passed",
        "runtime_state_snapshot_verified",
        "rendered_launchagent_manifest_checksums_saved",
        "health_1000_probes_loaded_zero_models",
        "seven_day_schedule_10x_arrival_2x_duration_replay_passed",
        "sqlite_wal_disk_full_fsync_faults_passed",
        "offline_replay_and_isolated_live_validation_satisfied",
        "isolated_live_validation_single_active_handoff_verified",
        "v2_fully_stopped_before_v3_start_verified",
        "single_scheduler_consumer_writer_ownership_verified",
        "single_active_handoff_test_passed",
        "v3_fully_stopped_before_v2_rollback_verified",
        "input_method_candidate_window_probe_passed",
        "atomic_release_switch_and_cold_rollback_drill_passed",
        "human_go_approval_recorded",
    }
)


def _source_contract(config: dict[str, Any]) -> dict[str, Any]:
    try:
        return resolve_source_contract(
            config.get("source_contract"),
            formal_databases=sorted(FORMAL_BACKUP_DATABASES),
        )
    except SourceContractError as exc:
        raise ValueError(f"cutover config source_contract is invalid: {exc}") from exc


def _nonnegative_int(value: Any, description: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{description} must be a non-negative integer")
    return value


def _exact_nonnegative_int(value: Any, expected: int, description: str) -> int:
    observed = _nonnegative_int(value, description)
    if observed != expected:
        raise ValueError(f"{description} must equal {expected}; observed {observed}")
    return observed


@dataclass(frozen=True)
class MetricRule:
    """One machine-checkable assertion against the producer report metrics."""

    path: str
    operation: str
    expected: Any = None


@dataclass(frozen=True)
class EvidenceSpec:
    """Trusted producer and semantic contract for one release-gate evidence id."""

    producer: str
    report_schema: str
    execution_mode: str
    rules: tuple[MetricRule, ...]


@dataclass(frozen=True)
class BoundArtifact:
    """One evidence artifact read and hash-verified exactly once."""

    role: str
    media_type: str
    path: str
    sha256: str
    data: bytes


def _r(path: str, operation: str, expected: Any = None) -> MetricRule:
    return MetricRule(path, operation, expected)


# These contracts are intentionally code-owned by the immutable release.  A
# generic ``status=passed`` envelope is not release evidence: the producer
# report must carry the exact schema, execution mode, release/campaign binding,
# and every metric below.  Threshold references are resolved from the selected,
# SHA-bound cutover configuration at evaluation time.
EVIDENCE_SPECS: dict[str, EvidenceSpec] = {
    "portable_source_inventory_current": EvidenceSpec(
        "scripts.architecture.generate_v2_inventory",
        "magi.v3.portable-source-inventory/v1",
        "offline",
        (
            _r("inventory_sha_matches", "true"),
            _r("unmapped_interfaces", "le", "threshold:unmapped_interfaces"),
            _r(
                "unapproved_source_runtime_drift",
                "le",
                "threshold:unapproved_source_runtime_drift",
            ),
        ),
    ),
    "runtime_route_inventory_current": EvidenceSpec(
        "scripts.v3_validation.inventory",
        "magi.v3.runtime-route-inventory/v1",
        "offline",
        (
            _r("runtime_routes", "eq", "baseline:runtime_routes"),
            _r("main_routes", "eq", "baseline:main_routes"),
            _r("tools_routes", "eq", "baseline:tools_routes"),
            _r("unmapped_interfaces", "le", "threshold:unmapped_interfaces"),
        ),
    ),
    "v2_regression_passed_in_release_venv": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.pytest-report/v1",
        "offline",
        (
            _r("disabled", "true"),
            _r("failed", "le", "threshold:failed_required_tests"),
        ),
    ),
    "v3_unit_contract_integration_e2e_passed": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.pytest-report/v1",
        "offline",
        (
            _r("failed", "le", "threshold:failed_required_tests"),
            _r("suites", "contains_all", ["unit", "contract", "integration", "e2e"]),
            _r("all_required_suites_passed", "true"),
        ),
    ),
    "interaction_agent_kernel_memory_quality_contracts_passed": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.quality-contracts/v1",
        "offline",
        (
            _r("failed_contracts", "le", "threshold:failed_required_tests"),
            _r(
                "contract_groups",
                "contains_all",
                ["interaction", "agent_kernel", "memory", "quality"],
            ),
            _r("quality_non_regression_passed", "true"),
        ),
    ),
    "context_memory_tool_plan_answer_golden_sets_passed": EvidenceSpec(
        "scripts.v3_validation.golden_flows",
        "magi.v3.golden-sets/v1",
        "offline",
        (
            _r("failed_cases", "le", "threshold:failed_required_tests"),
            _r("sets", "contains_all", ["context", "memory", "tool", "plan", "answer"]),
            _r("all_sets_passed", "true"),
        ),
    ),
    "golden_side_effect_diff_approved": EvidenceSpec(
        "scripts.v3_validation.side_effects",
        "magi.v3.side-effect-diff/v1",
        "offline",
        (
            _r("unapproved_contract_diffs", "le", "threshold:unapproved_contract_diffs"),
            _r("duplicate_side_effects", "le", "threshold:duplicate_side_effects"),
            _r("golden_diff_completed", "true"),
        ),
    ),
    "matched_v2_warm_cold_performance_baseline_complete": EvidenceSpec(
        "scripts.v3_validation.perf_compat",
        "magi.v3.matched-performance/v1",
        "offline",
        (
            _r("matched_production_dependencies", "true"),
            _r("warm_and_cold_measured", "true"),
            _r("maximum_p95_regression_ratio", "le", "threshold:max_p95_regression_ratio"),
            _r(
                "minimum_model_tokens_per_second_ratio",
                "ge",
                "threshold:minimum_model_tokens_per_second_ratio",
            ),
        ),
    ),
    "resource_policy_all_budgets_passed": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.resource-policy/v1",
        "offline",
        (
            _r("all_budgets_passed", "true"),
            _r(
                "application_plane_footprint_reduction_ratio",
                "ge",
                "threshold:minimum_application_plane_footprint_reduction_ratio",
            ),
            _r("idle_swapout_growth_mb", "le", "threshold:maximum_idle_swapout_growth_mb"),
        ),
    ),
    "health_1000_probes_loaded_zero_models": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.health-probe/v1",
        "offline",
        (
            _r("profile_count", "eq", "threshold:offline_replay_independent_passes"),
            _r("probe_count", "eq", 1000),
            _r("successful_probes", "eq", 1000),
            _r("total_probe_count", "eq", 1000),
            _r("failed_probes", "eq", 0),
            _r("model_imports", "eq", 0),
            _r("models_loaded", "eq", 0),
            _r("state_mutations", "eq", 0),
        ),
    ),
    "seven_day_schedule_10x_arrival_2x_duration_replay_passed": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.schedule-replay/v1",
        "offline",
        (
            _r("independent_passes", "ge", "threshold:offline_replay_independent_passes"),
            _r("arrival_multiplier", "ge", 10),
            _r("duration_multiplier", "ge", 2),
            _r("p0_p1_deadline_misses", "le", "threshold:p0_p1_deadline_misses"),
            _r(
                "p2_deadline_success_ratio",
                "ge",
                "threshold:minimum_p2_deadline_success_ratio",
            ),
            _r("unbounded_queue_growth", "le", "threshold:unbounded_queue_growth"),
            _r("all_enabled_job_bodies_covered", "true"),
        ),
    ),
    "heavy_plus_interactive_preemption_benchmark_passed": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.preemption-benchmark/v1",
        "offline",
        (
            _r("preemption_passed", "true"),
            _r("automatic_preemption_observed", "true"),
            _r("independent_passes", "eq", 7),
            _r("independent_samples", "ge", 28),
            _r("p0_p1_deadline_misses", "le", "threshold:p0_p1_deadline_misses"),
            _r(
                "interactive_queue_p95_seconds",
                "le",
                "threshold:p1_browser_queue_p95_seconds",
            ),
            _r("p1_browser_queue_p95_seconds", "le", "threshold:p1_browser_queue_p95_seconds"),
            _r("orphan_process_groups", "eq", 0),
            _r("duplicate_completions", "eq", 0),
            _r("lost_jobs", "eq", 0),
            _r("preempted_jobs_requeued", "ge", 28),
            _r("attempt_two_unique_completions", "ge", 28),
        ),
    ),
    "hundred_cycle_worker_reap_soak_passed": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.worker-reap-soak/v1",
        "offline",
        (
            _r("cycles", "ge", 100),
            _r("unreaped_workers", "eq", 0),
            _r("resource_baseline_restored", "true"),
        ),
    ),
    "sqlite_wal_disk_full_fsync_faults_passed": EvidenceSpec(
        "scripts.v3_validation.fault_certification",
        "magi.v3.fault-recovery-certification/v1",
        "offline",
        (
            _r("profile_count", "eq", "threshold:offline_replay_independent_passes"),
            _r("unique_stimulus_plan_count", "eq", "threshold:offline_replay_independent_passes"),
            _r("software_equivalent_layer_passed", "true"),
            _r("sqlite_wal_fault_passed", "true"),
            _r("apfs_sparse_image_enospc_passed", "true"),
            _r("fsync_fault_passed", "true"),
            _r("logical_transaction_sweep_passed", "true"),
            _r("mach_clock_offset_sigkill_passed", "true"),
            _r("transaction_stage_sigkill_passed", "true"),
            _r("controlled_cold_restart_deferred_to_cutover", "true"),
            _r("external_device_disconnect_required", "false"),
            _r("physical_power_cut_required", "false"),
            _r("acknowledged_commits_lost", "eq", 0),
            _r("partially_visible_transactions", "eq", 0),
            _r("duplicate_jobs", "eq", 0),
            _r("lost_jobs_after_recovery", "eq", 0),
            _r(
                "unreconciled_ambiguous_commits",
                "le",
                "threshold:unreconciled_ambiguous_commits",
            ),
            _r("residual_hard_gate_blocked", "false"),
        ),
    ),
    "notification_storm_and_dlq_faults_passed": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.notification-dlq-faults/v1",
        "offline",
        (
            _r("notification_storm_passed", "true"),
            _r("dlq_recovery_passed", "true"),
            _r("duplicate_side_effects", "le", "threshold:duplicate_side_effects"),
            _r("unbounded_queue_growth", "le", "threshold:unbounded_queue_growth"),
        ),
    ),
    "offline_replay_and_isolated_live_validation_satisfied": EvidenceSpec(
        "scripts.v3_validation.live_validation",
        "magi.v3.offline-live-summary/v1",
        "isolated_live",
        (
            _r("offline_independent_passes", "ge", "threshold:offline_replay_independent_passes"),
            _r(
                "isolated_live_runs",
                "ge",
                "threshold:minimum_isolated_live_validation_runs",
            ),
            _r("all_runs_passed", "true"),
            _r(
                "validation_window_hours",
                "le",
                "threshold:pre_cutover_validation_window_hours",
            ),
        ),
    ),
    "isolated_live_validation_single_active_handoff_verified": EvidenceSpec(
        "scripts.v3_validation.live_validation",
        "magi.v3.single-active-live-validation/v1",
        "isolated_live",
        (
            _r("runs", "ge", "threshold:minimum_isolated_live_validation_runs"),
            _r("single_active_violations", "eq", 0),
            _r("all_runs_passed", "true"),
        ),
    ),
    "database_backup_restore_drill_passed": EvidenceSpec(
        "scripts.v3_backup_prepare",
        "magi.v3.database-backup-restore/v1",
        "offline",
        (
            _r("databases_tested", "ge", 1),
            _r("restore_failures", "eq", 0),
            _r("restored_checksums_verified", "true"),
        ),
    ),
    "runtime_state_snapshot_verified": EvidenceSpec(
        "scripts.v3_backup_prepare",
        "magi.v3.runtime-state-snapshot/v1",
        "offline",
        (
            _r("snapshot_verified", "true"),
            _r("verification_failures", "eq", 0),
        ),
    ),
    "rendered_launchagent_manifest_checksums_saved": EvidenceSpec(
        "scripts.v3_deploy_prepare",
        "magi.v3.launchagent-manifest/v1",
        "offline",
        (
            # V3 intentionally consolidates the 26 installed V2 launchagents
            # into exactly three release-owned roles.  The V2 inventory count
            # is a compatibility baseline, not the expected V3 deploy count.
            _r("roles", "eq", 3),
            _r("checksum_mismatches", "eq", 0),
            _r("checksums_saved", "true"),
        ),
    ),
    "single_active_handoff_test_passed": EvidenceSpec(
        "scripts.v3_cutover",
        "magi.v3.single-active-handoff/v1",
        "cutover_drill",
        (
            _r("single_active_violations", "eq", 0),
            _r("v2_v3_concurrent", "false"),
            _r("handoff_passed", "true"),
        ),
    ),
    "v2_fully_stopped_before_v3_start_verified": EvidenceSpec(
        "scripts.v3_cutover",
        "magi.v3.v2-stop-proof/v1",
        "isolated_live",
        (
            _r("active_v2_processes", "eq", 0),
            _r("owned_ports", "eq", 0),
            _r("scheduler_owners", "eq", 0),
            _r("writer_owners", "eq", 0),
            _r("model_owners", "eq", 0),
        ),
    ),
    "v3_fully_stopped_before_v2_rollback_verified": EvidenceSpec(
        "scripts.v3_cutover",
        "magi.v3.v3-stop-proof/v1",
        "cutover_drill",
        (
            _r("active_v3_processes", "eq", 0),
            _r("owned_ports", "eq", 0),
            _r("scheduler_owners", "eq", 0),
            _r("writer_owners", "eq", 0),
            _r("model_owners", "eq", 0),
        ),
    ),
    "single_scheduler_consumer_writer_ownership_verified": EvidenceSpec(
        "scripts.v3_cutover",
        "magi.v3.writer-ownership/v1",
        "isolated_live",
        (
            _r("scheduler_owners", "le", 1),
            _r("consumer_owners", "le", 1),
            _r("writer_conflicts", "le", "threshold:unresolved_dual_writers"),
            _r("ownership_verified", "true"),
        ),
    ),
    "worker_process_group_footprint_and_metal_return_to_baseline": EvidenceSpec(
        "scripts.v3_campaign",
        "magi.v3.worker-footprint-return/v1",
        "offline",
        (
            _r("cycles", "ge", 100),
            _r("orphan_process_groups", "eq", 0),
            _r("rss_returned_to_baseline", "true"),
            _r("metal_returned_to_baseline", "true"),
        ),
    ),
    "input_method_candidate_window_probe_passed": EvidenceSpec(
        "scripts.v3_validation.ime_candidate_probe",
        "magi.v3.ime-candidate-probe/v1",
        "isolated_live",
        (
            _r("observations", "ge", 1),
            _r("failures", "eq", 0),
            _r("memory_pressure_exercised", "true"),
            _r("candidate_window_healthy", "true"),
        ),
    ),
    "atomic_release_switch_and_cold_rollback_drill_passed": EvidenceSpec(
        "scripts.v3_cutover",
        "magi.v3.atomic-switch-rollback/v1",
        "cutover_drill",
        (
            _r("controlled_cold_restart_verified", "true"),
            _r("atomic_switch_verified", "true"),
            _r("cold_rollback_verified", "true"),
            _r("rollback_rto_seconds", "le", "threshold:rollback_rto_seconds"),
            _r("lost_committed_jobs", "le", "threshold:lost_committed_jobs"),
            _r("duplicate_committed_jobs", "le", "threshold:duplicate_committed_jobs"),
        ),
    ),
    "human_go_approval_recorded": EvidenceSpec(
        "human.magi_release_owner",
        "magi.v3.human-go-approval/v1",
        "human_approval",
        (
            _r("approved", "true"),
            _r("approver_id", "nonempty"),
            _r("approver_role", "eq", "authorized_release_owner"),
            _r("approval_scope", "eq", "exact_release_and_campaign"),
        ),
    ),
}


# RC643 promotes one current V3 release to the next current V3 release.  V2,
# matched V2/V3 performance, native IME pressure, and three-run pre-cutover
# handoff drills are historical diagnostics, not promotion gates.
ACTIVE_V3_EVIDENCE_IDS = (
    "portable_source_inventory_current",
    "runtime_route_inventory_current",
    "v3_unit_contract_integration_e2e_passed",
    "interaction_agent_kernel_memory_quality_contracts_passed",
    "context_memory_tool_plan_answer_golden_sets_passed",
    "golden_side_effect_diff_approved",
    "health_1000_probes_loaded_zero_models",
    "seven_day_schedule_10x_arrival_2x_duration_replay_passed",
    "hundred_cycle_worker_reap_soak_passed",
    "sqlite_wal_disk_full_fsync_faults_passed",
    "notification_storm_and_dlq_faults_passed",
    "rendered_launchagent_manifest_checksums_saved",
    "atomic_release_switch_and_cold_rollback_drill_passed",
    "human_go_approval_recorded",
)
EVIDENCE_SPECS = {
    evidence_id: EVIDENCE_SPECS[evidence_id]
    for evidence_id in ACTIVE_V3_EVIDENCE_IDS
}
NORMALIZED_EVIDENCE_WHITELIST = frozenset(ACTIVE_V3_EVIDENCE_IDS)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        value[key] = item
    return value


def _load_json_value_bytes(data: bytes, description: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{description}: invalid JSON: {exc}") from exc


def _load_json_bytes(data: bytes, description: str) -> dict[str, Any]:
    value = _load_json_value_bytes(data, description)
    if not isinstance(value, dict):
        raise ValueError(f"{description}: top-level JSON value must be an object")
    return value


def load_json(path: Path) -> dict[str, Any]:
    return _load_json_bytes(path.read_bytes(), str(path))


def _parse_timestamp(value: Any) -> tuple[datetime | None, str | None]:
    if not isinstance(value, str):
        return None, "generated_at must be an ISO-8601 string"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, "generated_at is not valid ISO-8601"
    if parsed.tzinfo is None:
        return None, "generated_at must include a timezone"
    return parsed.astimezone(timezone.utc), None


def validate_evidence(
    document: dict[str, Any],
    expected_id: str,
    *,
    expected_context: dict[str, str],
    now: datetime | None = None,
    max_age_hours: float = DEFAULT_MAX_EVIDENCE_AGE_HOURS,
) -> list[str]:
    errors: list[str] = []
    if type(document.get("schema_version")) is not int or document.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    if document.get("evidence_id") != expected_id:
        errors.append("evidence_id does not match the required evidence name")
    if document.get("status") not in {"passed", "failed"}:
        errors.append("status must be passed or failed")
    generated_at, timestamp_error = _parse_timestamp(document.get("generated_at"))
    if timestamp_error:
        errors.append(timestamp_error)
    elif generated_at is not None:
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        age_seconds = (reference - generated_at).total_seconds()
        if age_seconds < -300:
            errors.append("generated_at is more than 5 minutes in the future")
        elif age_seconds > max_age_hours * 3600:
            errors.append(f"generated_at exceeds maximum evidence age of {max_age_hours:g} hours")
    if not isinstance(document.get("producer"), str) or not document["producer"].strip():
        errors.append("producer must be a non-empty string")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("artifacts must be a list")
    elif not artifacts:
        errors.append("artifacts must contain at least one artifact")
    else:
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                errors.append(f"artifacts[{index}] must be an object")
                continue
            if not isinstance(artifact.get("path"), str) or not artifact["path"]:
                errors.append(f"artifacts[{index}].path must be non-empty")
            digest = artifact.get("sha256")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                errors.append(f"artifacts[{index}].sha256 must be lowercase SHA-256")
    for field in CONTEXT_FIELDS:
        expected = expected_context.get(field)
        if not isinstance(expected, str) or not expected.strip():
            errors.append(f"expected context {field} must be a non-empty string")
        elif document.get(field) != expected:
            errors.append(f"{field} does not match the expected release context")
    return errors


def freeze_artifacts(
    document: dict[str, Any], evidence_dir: Path
) -> tuple[list[BoundArtifact], list[str]]:
    """Read every artifact once, then verify its path and digest."""

    frozen: list[BoundArtifact] = []
    errors: list[str] = []
    root = evidence_dir.resolve()
    seen: set[str] = set()
    for index, artifact in enumerate(document.get("artifacts", ())):
        if not isinstance(artifact, dict):
            errors.append(f"artifacts[{index}] must be an object")
            continue
        relative = Path(artifact["path"])
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"artifacts[{index}].path must stay inside the evidence directory")
            continue
        relative_text = relative.as_posix()
        if relative_text in seen:
            errors.append(f"artifacts[{index}].path is duplicated")
            continue
        seen.add(relative_text)
        raw_path = root / relative
        if raw_path.is_symlink():
            errors.append(f"artifacts[{index}] must not be a symlink: {relative}")
            continue
        path = raw_path.resolve()
        try:
            path.relative_to(root)
        except ValueError:
            errors.append(f"artifacts[{index}].path escapes the evidence directory")
            continue
        if not path.is_file():
            errors.append(f"artifacts[{index}] is missing: {relative}")
            continue
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise OSError("artifact is not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
        except OSError as exc:
            errors.append(f"artifacts[{index}] is unreadable: {relative}: {exc}")
            continue
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        signature = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
        )
        if signature(before) != signature(after):
            errors.append(f"artifacts[{index}] changed while being read: {relative}")
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest != artifact["sha256"]:
            errors.append(f"artifacts[{index}] SHA-256 mismatch: {relative}")
            continue
        frozen.append(
            BoundArtifact(
                role=str(artifact.get("role") or ""),
                media_type=str(artifact.get("media_type") or ""),
                path=relative_text,
                sha256=digest,
                data=data,
            )
        )
    return frozen, errors


def verify_artifacts(document: dict[str, Any], evidence_dir: Path) -> list[str]:
    """Compatibility wrapper returning only artifact verification errors."""

    _frozen, errors = freeze_artifacts(document, evidence_dir)
    return errors


def _canonical_json_bytes(value: Any) -> bytes:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (rendered + "\n").encode()


def _nested_value(payload: dict[str, Any], dotted_path: str) -> tuple[Any, bool]:
    current: Any = payload
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True


def _configured_value(config: dict[str, Any], reference: Any) -> Any:
    if not isinstance(reference, str) or ":" not in reference:
        return reference
    namespace, name = reference.split(":", 1)
    section_name = {"threshold": "promotion_thresholds", "baseline": "baseline"}.get(namespace)
    if section_name is None:
        return reference
    section = config.get(section_name)
    if not isinstance(section, dict) or name not in section:
        raise ValueError(f"cutover config is missing {section_name}.{name}")
    return section[name]


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_metric_rule(
    metrics: dict[str, Any], rule: MetricRule, config: dict[str, Any]
) -> str | None:
    actual, present = _nested_value(metrics, rule.path)
    if not present:
        return f"producer report metrics.{rule.path} is required"
    expected = _configured_value(config, rule.expected)
    if rule.operation == "true":
        valid = actual is True
        expectation = "be true"
    elif rule.operation == "false":
        valid = actual is False
        expectation = "be false"
    elif rule.operation == "nonempty":
        valid = isinstance(actual, str) and bool(actual.strip())
        expectation = "be a non-empty string"
    elif rule.operation == "eq":
        if _is_number(expected):
            valid = _is_number(actual) and float(actual) == float(expected)
        else:
            valid = type(actual) is type(expected) and actual == expected
        expectation = f"equal {expected!r}"
    elif rule.operation in {"le", "ge"}:
        valid = _is_number(actual) and _is_number(expected)
        if valid and rule.operation == "le":
            valid = float(actual) <= float(expected)
        elif valid:
            valid = float(actual) >= float(expected)
        expectation = f"be {rule.operation} {expected!r}"
    elif rule.operation == "contains_all":
        valid = (
            isinstance(actual, list)
            and isinstance(expected, list)
            and all(item in actual for item in expected)
        )
        expectation = f"contain all of {expected!r}"
    else:  # A bad code-owned contract must fail closed, never silently pass.
        return f"unsupported semantic operation {rule.operation!r} for metrics.{rule.path}"
    if valid:
        return None
    return f"producer report metrics.{rule.path} must {expectation}; observed {actual!r}"


def _artifacts_by_role(
    artifacts: Sequence[BoundArtifact],
) -> dict[str, list[BoundArtifact]]:
    result: dict[str, list[BoundArtifact]] = {}
    for artifact in artifacts:
        result.setdefault(artifact.role, []).append(artifact)
    return result


def _one(
    by_role: dict[str, list[BoundArtifact]], role: str
) -> BoundArtifact:
    rows = by_role.get(role, [])
    if len(rows) != 1:
        raise ValueError(f"normalized evidence requires exactly one {role}")
    return rows[0]


def _json_artifact(artifact: BoundArtifact) -> dict[str, Any]:
    if artifact.media_type != "application/json":
        raise ValueError(f"{artifact.role} must have application/json media type")
    return _load_json_bytes(artifact.data, artifact.path)


def _context_matches_or_raise(
    payload: dict[str, Any], expected_context: dict[str, str], description: str
) -> None:
    for field, expected in expected_context.items():
        if payload.get(field) != expected:
            raise ValueError(f"{description} {field} does not match release context")


def _verify_release_control_sources(
    by_role: dict[str, list[BoundArtifact]], expected_context: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    marker_artifact = _one(by_role, "upstream_release_marker")
    manifest_artifact = _one(by_role, "upstream_release_manifest")
    marker = _json_artifact(marker_artifact)
    manifest = _json_artifact(manifest_artifact)
    _exact_nonnegative_int(marker.get("schema_version"), 1, "release marker schema_version")
    _exact_nonnegative_int(manifest.get("schema_version"), 1, "release manifest schema_version")
    if marker.get("manifest") != "release-manifest.json":
        raise ValueError("release marker does not name the canonical manifest")
    if marker.get("manifest_sha256") != manifest_artifact.sha256:
        raise ValueError("release marker manifest SHA-256 binding failed")
    release_sha = expected_context["release_sha"]
    if (
        manifest.get("immutable") is not True
        or manifest.get("release_sha256") != release_sha
        or manifest.get("source_snapshot_sha256") != release_sha
        or marker.get("release_sha256") != release_sha
        or marker.get("source_snapshot_sha256") != release_sha
        or marker.get("release_id") != manifest.get("release_id")
        or marker.get("commit") != manifest.get("commit")
    ):
        raise ValueError("release marker/manifest identity binding failed")
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("release manifest file inventory is missing")
    by_path: dict[str, dict[str, Any]] = {}
    snapshot: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"release manifest file {index} is invalid")
        path = row.get("path")
        digest = row.get("sha256")
        size = row.get("size")
        mode = row.get("mode")
        relative = Path(str(path or ""))
        if (
            not isinstance(path, str)
            or not path
            or relative.is_absolute()
            or ".." in relative.parts
            or path in by_path
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or not isinstance(mode, str)
            or not re.fullmatch(r"[0-7]{4}", mode)
        ):
            raise ValueError(f"release manifest file {index} metadata is invalid")
        _nonnegative_int(size, f"release manifest file {index} size")
        by_path[path] = row
        snapshot.append({"path": path, "sha256": digest, "size": size, "mode": mode})
    if [row["path"] for row in snapshot] != sorted(by_path):
        raise ValueError("release manifest file inventory is not canonically sorted")
    snapshot_sha = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_file_count = _exact_nonnegative_int(
        manifest.get("source_file_count"), len(rows), "release manifest source_file_count"
    )
    marker_file_count = _exact_nonnegative_int(
        marker.get("source_file_count"), len(rows), "release marker source_file_count"
    )
    if (
        snapshot_sha != release_sha
        or manifest_file_count != len(rows)
        or marker_file_count != len(rows)
    ):
        raise ValueError("release source snapshot hash/count binding failed")
    return marker, manifest, by_path


def _verify_campaign_context_source(
    by_role: dict[str, list[BoundArtifact]],
    expected_context: dict[str, str],
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
) -> dict[str, Any]:
    report = _json_artifact(_one(by_role, "upstream_campaign_report"))
    _context_matches_or_raise(report, expected_context, "campaign report")
    if (
        report.get("evidence_class") != "immutable_release_offline_campaign"
        or report.get("release_id") != manifest.get("release_id")
        or report.get("release_commit") != manifest.get("commit")
        or report.get("release_manifest_sha256") != manifest_sha256
        or report.get("release_sha") != expected_context["release_sha"]
        or report.get("live_execution_performed") is not False
        or report.get("cutover_execution_performed") is not False
        or report.get("armed") is not True
        or report.get("certifying") is not True
        or report.get("harness_certified") is not True
        or report.get("offline_complete") is not True
        or report.get("decision") != "GO"
        or report.get("execution_backend") != "release_launcher"
        or report.get("fail_closed") is not False
    ):
        raise ValueError(
            "campaign report is not an armed completed certifying campaign bound to the selected immutable release"
        )
    _exact_nonnegative_int(
        report.get("schema_version"), 1, "campaign report schema_version"
    )
    _required_campaign_passes(report)
    return report


def _recompute_backup_metrics(
    by_role: dict[str, list[BoundArtifact]],
    expected_context: dict[str, str],
    source_contract: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    allowed = {
        "upstream_backup_metadata",
        "upstream_backup_content_manifest",
        "upstream_restore_drill",
        "upstream_backup_archive",
    }
    if set(by_role) != allowed or any(len(by_role[role]) != 1 for role in allowed):
        raise ValueError("backup normalized evidence source roles are not exact")
    metadata = _json_artifact(_one(by_role, "upstream_backup_metadata"))
    content_artifact = _one(by_role, "upstream_backup_content_manifest")
    content = _json_artifact(content_artifact)
    drill_artifact = _one(by_role, "upstream_restore_drill")
    drill = _json_artifact(drill_artifact)
    archive = _one(by_role, "upstream_backup_archive")
    _context_matches_or_raise(metadata, expected_context, "backup metadata")
    _exact_nonnegative_int(metadata.get("schema_version"), 2, "backup metadata schema_version")
    _exact_nonnegative_int(content.get("schema_version"), 2, "backup content schema_version")
    _exact_nonnegative_int(drill.get("schema_version"), 2, "restore drill schema_version")
    required_coverage = ["sqlite", "website_assets", "website_data"]
    if metadata.get("coverage") != required_coverage or content.get("coverage") != required_coverage:
        raise ValueError("backup coverage must exactly include SQLite and website data/assets")
    source_roots = content.get("source_roots")
    if not isinstance(source_roots, dict) or set(source_roots) != {"v2", "website"}:
        raise ValueError("backup source roots are missing")
    if (
        source_roots.get("v2") != source_contract["v2_root"]
        or source_roots.get("website") != source_contract["website_root"]
    ):
        raise ValueError("backup source/website roots do not match the formal V2 contract")
    databases = content.get("databases")
    mutable_files = content.get("mutable_files")
    mutable_directories = content.get("mutable_directories")
    if not isinstance(databases, list) or not isinstance(mutable_files, list) or not isinstance(mutable_directories, list):
        raise ValueError("backup content inventories are invalid")
    database_sources = {row.get("source") for row in databases if isinstance(row, dict)}
    if database_sources != FORMAL_BACKUP_DATABASES or len(databases) != len(FORMAL_BACKUP_DATABASES):
        raise ValueError("backup does not contain the exact four formal V2 databases")
    for row in databases:
        if (
            row.get("backup") != f"sqlite/{row.get('source')}"
            or row.get("quick_check") != "ok"
            or not SHA256_RE.fullmatch(str(row.get("sha256") or ""))
        ):
            raise ValueError("backup database manifest row is invalid")
    file_rows = [*databases, *mutable_files]
    for row_index, row in enumerate(file_rows):
        if not isinstance(row, dict):
            raise ValueError(f"backup file row {row_index} is invalid")
        _nonnegative_int(row.get("size"), f"backup file row {row_index} size")
    directory_rows = {
        row.get("backup"): row
        for row in mutable_directories
        if isinstance(row, dict)
    }
    if not {"website/data", "website/assets"} <= set(directory_rows):
        raise ValueError("backup lacks exact website data/assets directory roots")
    expected_metadata_counts = {
        "database_count": len(databases),
        "mutable_file_count": len(mutable_files),
        "mutable_directory_count": len(mutable_directories),
    }
    for name, expected in expected_metadata_counts.items():
        _exact_nonnegative_int(metadata.get(name), expected, f"backup metadata {name}")
    if (
        metadata.get("sha256") != archive.sha256
        or metadata.get("content_manifest")
        != {"path": "backup-content.json", "sha256": content_artifact.sha256}
    ):
        raise ValueError("backup metadata count/hash binding failed")
    restore_ref = metadata.get("restore_drill")
    if (
        not isinstance(restore_ref, dict)
        or restore_ref.get("evidence_sha256") != drill_artifact.sha256
        or restore_ref.get("backup_sha256") != archive.sha256
        or restore_ref.get("content_manifest_sha256") != content_artifact.sha256
        or restore_ref.get("status") != "passed"
        or restore_ref.get("actual_restore_performed") is not True
        or drill.get("status") != "passed"
        or drill.get("actual_restore_performed") is not True
        or drill.get("backup_sha256") != archive.sha256
        or drill.get("content_manifest_sha256") != content_artifact.sha256
    ):
        raise ValueError("backup restore drill binding failed")
    expected_counts = (
        len(databases),
        len(mutable_files),
        len(mutable_directories),
    )
    observed_counts = (
        _exact_nonnegative_int(
            drill.get("verified_databases"), len(databases), "restore drill verified_databases"
        ),
        _exact_nonnegative_int(
            drill.get("verified_mutable_files"),
            len(mutable_files),
            "restore drill verified_mutable_files",
        ),
        _exact_nonnegative_int(
            drill.get("verified_mutable_directories"),
            len(mutable_directories),
            "restore drill verified_mutable_directories",
        ),
    )
    for index, name in enumerate(
        ("verified_databases", "verified_mutable_files", "verified_mutable_directories")
    ):
        _exact_nonnegative_int(
            restore_ref.get(name), observed_counts[index], f"backup metadata restore_drill.{name}"
        )
    if observed_counts != expected_counts or drill.get("verified_scopes") != required_coverage:
        raise ValueError("backup restore drill did not verify every formal scope")
    with tarfile.open(fileobj=io.BytesIO(archive.data), mode="r:gz") as bundle:
        members = bundle.getmembers()
        if any(member.issym() or member.islnk() or not (member.isfile() or member.isdir()) for member in members):
            raise ValueError("backup archive contains links or special files")
        names = [member.name.rstrip("/") for member in members]
        if len(names) != len(set(names)):
            raise ValueError("backup archive contains duplicate members")
        by_name = {member.name.rstrip("/"): member for member in members}
        content_member = by_name.get("backup-content.json")
        if content_member is None or not content_member.isfile():
            raise ValueError("backup archive content manifest is missing")
        stream = bundle.extractfile(content_member)
        if stream is None or stream.read() != content_artifact.data:
            raise ValueError("backup archive content manifest bytes do not match evidence")
        expected_files = {"backup-content.json", *(row["backup"] for row in file_rows)}
        expected_dirs = {row["backup"] for row in mutable_directories}
        actual_files = {name for name, member in by_name.items() if member.isfile()}
        actual_dirs = {name for name, member in by_name.items() if member.isdir()}
        if actual_files != expected_files or actual_dirs != expected_dirs:
            raise ValueError("backup archive members differ from the formal manifest")
        with tempfile.TemporaryDirectory(prefix="magi-gate-backup-") as temporary:
            for row in file_rows:
                member = by_name[row["backup"]]
                stream = bundle.extractfile(member)
                if stream is None:
                    raise ValueError("backup archive member is unreadable")
                data = stream.read()
                if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise ValueError("backup archive member size/hash mismatch")
                if row in databases:
                    path = Path(temporary) / Path(row["backup"]).name
                    path.write_bytes(data)
                    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
                    with sqlite3.connect(uri, uri=True) as connection:
                        result = connection.execute("PRAGMA quick_check").fetchone()
                    if not result or result[0] != "ok":
                        raise ValueError("restored formal SQLite quick_check failed")
    return {
        "database_backup_restore_drill_passed": {
            "databases_tested": len(databases),
            "restore_failures": 0,
            "restored_checksums_verified": True,
        },
        "runtime_state_snapshot_verified": {
            "snapshot_verified": True,
            "verification_failures": 0,
        },
    }


def _recompute_deploy_metrics(
    by_role: dict[str, list[BoundArtifact]], expected_context: dict[str, str]
) -> dict[str, Any]:
    allowed = {
        "upstream_deploy_marker",
        "upstream_deploy_manifest",
        "upstream_release_marker",
        "upstream_release_manifest",
        "upstream_campaign_report",
        "upstream_launchagent",
    }
    if set(by_role) != allowed or len(by_role.get("upstream_launchagent", [])) != 3:
        raise ValueError("deploy normalized evidence source roles are not exact")
    marker, release, _files = _verify_release_control_sources(by_role, expected_context)
    release_artifact = _one(by_role, "upstream_release_manifest")
    campaign = _verify_campaign_context_source(
        by_role,
        expected_context,
        manifest=release,
        manifest_sha256=release_artifact.sha256,
    )
    deploy_marker_artifact = _one(by_role, "upstream_deploy_marker")
    deploy_marker = _json_artifact(deploy_marker_artifact)
    deploy_manifest_artifact = _one(by_role, "upstream_deploy_manifest")
    deploy = _json_artifact(deploy_manifest_artifact)
    _exact_nonnegative_int(
        deploy_marker.get("schema_version"), 1, "deploy marker schema_version"
    )
    _exact_nonnegative_int(deploy.get("schema_version"), 1, "deploy manifest schema_version")
    if (
        deploy_marker.get("status") != "prepared_not_installed"
        or deploy_marker.get("ready_to_install") is not True
        or deploy_marker.get("mutation_performed") is not False
        or deploy_marker.get("manifest") != "deploy-manifest.json"
        or deploy_marker.get("manifest_sha256") != deploy_manifest_artifact.sha256
        or deploy_marker.get("release_id") != release.get("release_id")
        or deploy_marker.get("release_manifest_sha256") != release_artifact.sha256
        or deploy.get("status") != "prepared_not_installed"
        or deploy.get("mutation_performed") is not False
        or deploy.get("release_id") != release.get("release_id")
        or deploy.get("release_manifest_sha256") != release_artifact.sha256
        or campaign.get("release_id") != deploy.get("release_id")
    ):
        raise ValueError("deploy marker/manifest/release/campaign binding failed")
    roles = deploy.get("roles")
    artifacts = deploy.get("artifacts")
    if not isinstance(roles, list) or not isinstance(artifacts, list):
        raise ValueError("deploy roles/artifacts are missing")
    expected_roles = {"control", "gateway", "supervisor"}
    by_role_name = {
        row.get("role"): row for row in roles if isinstance(row, dict)
    }
    if set(by_role_name) != expected_roles or len(roles) != 3:
        raise ValueError("deploy must contain exactly the three V3 roles")
    artifact_rows = {
        row.get("path"): row for row in artifacts if isinstance(row, dict)
    }
    if len(artifact_rows) != len(artifacts):
        raise ValueError("deploy artifact rows are invalid or duplicated")
    for row_index, row in enumerate(artifacts):
        _nonnegative_int(row.get("size"), f"deploy artifact {row_index} size")
    launchagents = by_role.get("upstream_launchagent", [])
    observed_labels: set[str] = set()
    for artifact in launchagents:
        try:
            plist = plistlib.loads(artifact.data)
        except plistlib.InvalidFileException as exc:
            raise ValueError("rendered launchagent is not a valid plist") from exc
        label = plist.get("Label")
        if not isinstance(label, str) or label in observed_labels:
            raise ValueError("rendered launchagent Label is invalid or duplicated")
        observed_labels.add(label)
        relative = f"launchagents/{label}.plist"
        row = artifact_rows.get(relative)
        if (
            not isinstance(row, dict)
            or row.get("sha256") != artifact.sha256
            or _exact_nonnegative_int(
                row.get("size"), len(artifact.data), f"rendered launchagent {label} size"
            )
            != len(artifact.data)
        ):
            raise ValueError("rendered launchagent checksum is absent or mismatched")
    expected_labels = {str(row.get("label")) for row in roles}
    if observed_labels != expected_labels:
        raise ValueError("rendered launchagent labels do not match deploy roles")
    return {"roles": 3, "checksum_mismatches": 0, "checksums_saved": True}


def _route_external_storage_roots() -> list[str]:
    from scripts.v3_validation.route_certification import (
        _expected_external_storage_roots,
    )

    return [str(root) for root in _expected_external_storage_roots()]


def _route_seatbelt_attestation(workspace: Path) -> dict[str, Any]:
    # The certifier owns this contract.  Keeping a second hand-maintained copy
    # here previously made a valid v2 Seatbelt attestation fail the final gate.
    from scripts.v3_validation.route_certification import _seatbelt_attestation

    return _seatbelt_attestation(workspace)


def _route_attested_seatbelt_workspace(
    value: Any, profile_id: str | None
) -> Path | None:
    from scripts.v3_validation.route_certification import (
        _attested_seatbelt_workspace,
    )

    canonical = _attested_seatbelt_workspace(value)
    if canonical is None:
        return None
    if profile_id is not None and (
        canonical.name != profile_id or canonical.parent.name != "route-certification"
    ):
        return None
    return canonical


def _recompute_route_metrics(
    by_role: dict[str, list[BoundArtifact]], expected_context: dict[str, str]
) -> dict[str, Any]:
    allowed = {
        "upstream_release_marker",
        "upstream_release_manifest",
        "upstream_campaign_report",
        "upstream_campaign_day",
        "upstream_runtime_route_inventory",
        "upstream_route_reviews",
        "upstream_route_review_supplement",
        "upstream_capability_manifest",
        "upstream_route_certification_report",
        "upstream_python_runtime_manifest",
    }
    singleton_roles = allowed - {
        "upstream_campaign_day",
        "upstream_route_certification_report",
    }
    if (
        set(by_role) != allowed
        or any(len(by_role[role]) != 1 for role in singleton_roles)
        or not by_role["upstream_campaign_day"]
    ):
        raise ValueError("runtime route normalized evidence source roles are not exact")
    _marker, release, files = _verify_release_control_sources(by_role, expected_context)
    release_artifact = _one(by_role, "upstream_release_manifest")
    campaign = _verify_campaign_context_source(
        by_role,
        expected_context,
        manifest=release,
        manifest_sha256=release_artifact.sha256,
    )
    required_passes = _required_campaign_passes(campaign)
    if len(by_role["upstream_route_certification_report"]) != required_passes:
        raise ValueError("runtime route report count is not campaign-bound")
    runtime_artifact = _one(by_role, "upstream_python_runtime_manifest")
    runtime_manifest = _json_artifact(runtime_artifact)
    if (
        runtime_manifest.get("schema_version") != 1
        or campaign.get("python_runtime_manifest_sha256") != runtime_artifact.sha256
        or runtime_manifest.get("python_runtime") != campaign.get("python_runtime_path")
        or runtime_manifest.get("python_runtime_realpath")
        != campaign.get("python_runtime_realpath")
        or runtime_manifest.get("python_runtime_sha256")
        != campaign.get("python_runtime_sha256")
        or runtime_manifest.get("tree_sha256")
        != campaign.get("python_runtime_tree_sha256")
    ):
        raise ValueError("route campaign Python runtime is not manifest-bound")
    runtime_site_roots: list[str] = []
    for root_key, rows_key in (
        ("runtime_root", "directories"),
        ("base_runtime_root", "base_directories"),
    ):
        root_text = runtime_manifest.get(root_key)
        rows = runtime_manifest.get(rows_key)
        if not isinstance(root_text, str) or not Path(root_text).is_absolute() or not isinstance(rows, list):
            raise ValueError("route runtime manifest site roots are invalid")
        for row in rows:
            relative = row.get("path") if isinstance(row, dict) else None
            if not isinstance(relative, str):
                raise ValueError("route runtime manifest directory row is invalid")
            pure = PurePosixPath(relative)
            if pure.is_absolute() or ".." in pure.parts or pure.name != "site-packages":
                continue
            site_root = str(Path(root_text) / Path(*pure.parts))
            if site_root not in runtime_site_roots:
                runtime_site_roots.append(site_root)
    runtime_site_roots.sort()
    if not runtime_site_roots:
        raise ValueError("route runtime manifest has no site-packages")
    user_python_root = account_home() / "Library" / "Python"
    if any(
        Path(root) == user_python_root or user_python_root in Path(root).parents
        for root in runtime_site_roots
    ):
        raise ValueError("route runtime manifest cannot include user site-packages")
    route_artifact = _one(by_role, "upstream_runtime_route_inventory")
    route_path = "docs/architecture/v3/generated/v2_runtime_routes.json"
    if files.get(route_path, {}).get("sha256") != route_artifact.sha256:
        raise ValueError("runtime route inventory is not bound to release manifest")
    review_artifact = _one(by_role, "upstream_route_reviews")
    review_path = "scripts/v3_validation/route-method-review.json"
    if files.get(review_path, {}).get("sha256") != review_artifact.sha256:
        raise ValueError("route review is not bound to release manifest")
    supplement_artifact = _one(by_role, "upstream_route_review_supplement")
    supplement_path = "scripts/v3_validation/route-method-review-supplement.json"
    if files.get(supplement_path, {}).get("sha256") != supplement_artifact.sha256:
        raise ValueError("route review supplement is not bound to release manifest")
    capability_artifact = _one(by_role, "upstream_capability_manifest")
    capability_path = "config/v3_capability_manifest.json"
    if files.get(capability_path, {}).get("sha256") != capability_artifact.sha256:
        raise ValueError("capability manifest is not bound to release manifest")
    payload = _json_artifact(route_artifact)
    _exact_nonnegative_int(payload.get("schema_version"), 1, "runtime route schema_version")
    if not isinstance(payload.get("services"), dict):
        raise ValueError("runtime route inventory schema is invalid")
    normalized: list[dict[str, Any]] = []
    route_keys: set[tuple[str, str, str, str]] = set()
    for service, rows in payload["services"].items():
        if service not in {"5002", "5003"} or not isinstance(rows, list):
            raise ValueError("runtime route inventory service rows are invalid")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("runtime route inventory row is invalid")
            methods = sorted({str(item).upper() for item in row.get("methods", ())})
            rule = row.get("rule")
            endpoint = row.get("endpoint")
            if not methods or not isinstance(rule, str) or not rule.startswith("/") or not isinstance(endpoint, str) or not endpoint:
                raise ValueError("runtime route inventory route identity is invalid")
            normalized.append({"service": service, "rule": rule, "methods": methods, "endpoint": endpoint})
            for method in methods:
                key = (service, rule, method, endpoint)
                if key in route_keys:
                    raise ValueError("runtime route inventory contains duplicate route-method")
                route_keys.add(key)
    normalized.sort(key=lambda row: (row["service"], row["rule"], row["methods"], row["endpoint"]))
    fingerprint = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    reviewed: set[tuple[str, str, str, str]] = set()
    seen_reviews: set[tuple[str, str, str, str]] = set()
    for review_source in (review_artifact, supplement_artifact):
        reviews = _json_artifact(review_source)
        _exact_nonnegative_int(
            reviews.get("schema_version"), 1, "route review schema_version"
        )
        if (
            reviews.get("review_policy") != "explicit_route_method_only"
            or reviews.get("inventory_fingerprint") != fingerprint
            or not isinstance(reviews.get("reviews"), list)
        ):
            raise ValueError("route review manifest is not bound to route inventory")
        for row in reviews["reviews"]:
            if not isinstance(row, dict):
                raise ValueError("route review row is invalid")
            key = (
                str(row.get("service") or ""),
                str(row.get("rule") or ""),
                str(row.get("method") or "").upper(),
                str(row.get("endpoint") or ""),
            )
            if key not in route_keys or key in seen_reviews:
                raise ValueError("route review row is unknown or duplicated")
            seen_reviews.add(key)
            if row.get("reviewed") is True:
                if not str(row.get("reviewed_by") or "").strip() or not str(
                    row.get("rationale") or ""
                ).strip():
                    raise ValueError("completed route review lacks reviewer/rationale")
                reviewed.add(key)
    capability = _json_artifact(capability_artifact)
    _exact_nonnegative_int(
        capability.get("schema_version"), 1, "capability manifest schema_version"
    )
    if not isinstance(capability.get("capabilities"), list):
        raise ValueError("capability manifest schema is invalid")
    counts = {service: sum(row["service"] == service for row in normalized) for service in ("5002", "5003")}
    declared = payload.get("counts")
    if not isinstance(declared, dict) or set(declared) != {"5002", "5003", "total"}:
        raise ValueError("runtime route declared counts are invalid")
    for name, expected in {
        "5002": counts["5002"],
        "5003": counts["5003"],
        "total": len(normalized),
    }.items():
        _exact_nonnegative_int(declared.get(name), expected, f"runtime route counts.{name}")
    if declared != {"5002": counts["5002"], "5003": counts["5003"], "total": len(normalized)}:
        raise ValueError("runtime route declared counts do not match rows")
    declared_days = campaign.get("artifacts")
    day_artifacts = by_role["upstream_campaign_day"]
    if (
        not isinstance(declared_days, list)
        or len(declared_days) != len(day_artifacts)
        or {
            str(row.get("sha256") or "")
            for row in declared_days
            if isinstance(row, dict)
        }
        != {artifact.sha256 for artifact in day_artifacts}
    ):
        raise ValueError("route certification campaign days are not report-bound")
    certification_artifacts = {
        artifact.sha256: artifact
        for artifact in by_role["upstream_route_certification_report"]
    }
    if len(certification_artifacts) != required_passes:
        raise ValueError("route certification reports are duplicated")
    certification_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for day_index, day_artifact in enumerate(day_artifacts):
        day = _json_artifact(day_artifact)
        _context_matches_or_raise(day, expected_context, f"campaign day {day_index}")
        if (
            day.get("release_id") != release.get("release_id")
            or day.get("release_commit") != release.get("commit")
            or day.get("release_manifest_sha256") != release_artifact.sha256
            or day.get("release_sha") != expected_context["release_sha"]
            or day.get("status") != "offline_passed"
            or day.get("release_gate_eligible") is not True
            or day.get("live_execution_performed") is not False
            or not isinstance(day.get("workloads"), list)
        ):
            raise ValueError("route certification campaign day binding is invalid")
        for outcome in day["workloads"]:
            if (
                not isinstance(outcome, dict)
                or outcome.get("workload") != "346_route_contract_replay"
            ):
                continue
            reference = outcome.get("structured_evidence_artifact")
            inline = outcome.get("structured_evidence")
            artifact = (
                certification_artifacts.get(str(reference.get("sha256") or ""))
                if isinstance(reference, dict)
                else None
            )
            if (
                outcome.get("status") != "offline_passed"
                or outcome.get("returncode") != 0
                or not isinstance(outcome.get("validation_profile"), dict)
                or not isinstance(inline, dict)
                or not isinstance(reference, dict)
                or set(reference) != {"path", "sha256"}
                or artifact is None
            ):
                raise ValueError("route certification campaign outcome is invalid")
            report = _json_artifact(artifact)
            if report != inline:
                raise ValueError("route certification report differs from campaign outcome")
            certification_rows.append((outcome, report))
    if (
        len(certification_rows) != required_passes
        or {row[0].get("validation_pass") for row in certification_rows}
        != set(range(1, required_passes + 1))
    ):
        raise ValueError("route campaign lacks the required certification reports")
    expected_source_files = {
        "compiler_sha256": "scripts/v3_validation/route_certification.py",
        "actual_route_replay_sha256": "scripts/v3_validation/actual_route_replay.py",
        "trace_plugin_sha256": "scripts/v3_validation/route_success_trace_plugin.py",
        "proof_review_manifest_sha256": "scripts/v3_validation/route-success-proof-review.json",
        "primary_side_effect_review_sha256": "scripts/v3_validation/route-method-review.json",
        "supplemental_side_effect_review_sha256": (
            "scripts/v3_validation/route-method-review-supplement.json"
        ),
    }
    profiles: set[str] = set()
    for outcome, report in certification_rows:
        profile_id = str(outcome["validation_profile"].get("profile_id") or "")
        measurements = report.get("measurements")
        blocker = report.get("blockers", {}).get("ROUTE_REPLAY_NOT_IMPLEMENTED")
        safety = report.get("safety")
        source_binding = report.get("source_binding")
        expected_external_storage_roots = _route_external_storage_roots()
        trace_isolation_attempts = (
            safety.get("trace_isolation_attempts")
            if isinstance(safety, dict)
            else None
        )
        external_storage_attempts = (
            safety.get("external_storage_access_attempts")
            if isinstance(safety, dict)
            else None
        )
        base_isolation_attempts = (
            safety.get("base_isolation_attempts")
            if isinstance(safety, dict)
            else None
        )
        all_trace_counters_zero = (
            isinstance(trace_isolation_attempts, dict)
            and all(
                isinstance(name, str) and type(value) is int and value == 0
                for name, value in trace_isolation_attempts.items()
            )
        )
        all_base_counters_zero = (
            isinstance(base_isolation_attempts, dict)
            and all(
                isinstance(name, str) and type(value) is int and value == 0
                for name, value in base_isolation_attempts.items()
            )
        )
        seatbelt_workspace = _route_attested_seatbelt_workspace(
            safety.get("seatbelt") if isinstance(safety, dict) else None,
            profile_id,
        )
        runtime_binding = report.get("runtime_binding")
        release_manifest_text = report.get("release_manifest")
        expected_runtime_binding = {
            "certifying": True,
            "mode": "formal_manifest_bound",
            "python_runtime": campaign.get("python_runtime_path"),
            "python_runtime_realpath": campaign.get("python_runtime_realpath"),
            "python_runtime_sha256": campaign.get("python_runtime_sha256"),
            "runtime_manifest": campaign.get("python_runtime_manifest"),
            "runtime_manifest_sha256": campaign.get("python_runtime_manifest_sha256"),
            "runtime_tree_sha256": campaign.get("python_runtime_tree_sha256"),
            "runtime_root": runtime_manifest.get("runtime_root"),
            "base_runtime_root": runtime_manifest.get("base_runtime_root"),
            "pythonpath_roots": [
                str(Path(release_manifest_text).parent) if isinstance(release_manifest_text, str) else "",
                *runtime_site_roots,
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
        if (
            not profile_id
            or profile_id in profiles
            or report.get("schema_version") != 1
            or report.get("workload") != "346_route_contract_replay"
            or report.get("status") != "passed"
            or report.get("certifying") is not True
            or report.get("diagnostic_passed") is not False
            or runtime_binding != expected_runtime_binding
            or report.get("passed") is not True
            or report.get("coverage_complete") is not True
            or report.get("release_id") != release.get("release_id")
            or report.get("release_sha") != expected_context["release_sha"]
            or report.get("release_manifest_sha256") != release_artifact.sha256
            or report.get("release_commit") != release.get("commit")
            or report.get("inventory_fingerprint") != fingerprint
            or report.get("inventory_counts") != declared
            or not isinstance(measurements, dict)
            or measurements.get("validation_profile_id") != profile_id
            or any(measurements.get(key) != value for key, value in exact.items())
            or any(report.get(key) != value for key, value in exact.items())
            or not isinstance(blocker, dict)
            or blocker.get("retained") is not False
            or blocker.get("remaining_routes") != 0
            or blocker.get("remaining_route_methods") != 0
            or not isinstance(safety, dict)
            or safety.get("offline") is not True
            or safety.get("production_service_started") is not False
            or safety.get("production_database_accessed") is not False
            or safety.get("nas_accessed") is not False
            or safety.get("external_storage_attested") is not True
            or safety.get("trace_external_storage_attested") is not True
            or safety.get("base_external_storage_attested") is not True
            or safety.get("external_storage_roots") != expected_external_storage_roots
            or seatbelt_workspace is None
            or type(external_storage_attempts) is not int
            or external_storage_attempts != 0
            or not all_trace_counters_zero
            or not all_base_counters_zero
            or safety.get("base_safe_execution") is not True
            or report.get("network_access_performed") is not False
            or report.get("service_start_performed") is not False
            or report.get("production_port_access_performed") is not False
            or report.get("launchctl_performed") is not False
            or not isinstance(source_binding, dict)
            or set(source_binding) != {*expected_source_files, "base_evidence_sha256"}
            or any(
                source_binding.get(role) != files.get(path, {}).get("sha256")
                for role, path in expected_source_files.items()
            )
            or not SHA256_RE.fullmatch(
                str(source_binding.get("base_evidence_sha256") or "")
            )
        ):
            raise ValueError("route certification report identity, safety, or binding is invalid")
        dispositions = report.get("route_method_dispositions")
        if not isinstance(dispositions, list) or len(dispositions) != len(route_keys):
            raise ValueError("route certification dispositions are incomplete")
        disposition_keys: set[tuple[str, str, str, str]] = set()
        for disposition in dispositions:
            if not isinstance(disposition, dict):
                raise ValueError("route certification disposition is invalid")
            key = (
                str(disposition.get("service") or ""),
                str(disposition.get("rule") or ""),
                str(disposition.get("method") or "").upper(),
                str(disposition.get("endpoint") or ""),
            )
            if (
                key in disposition_keys
                or disposition.get("disposition") != "actual_handler_passed"
                or disposition.get("reviewed") is not True
                or disposition.get("handler_dispatch_passed") is not True
                or disposition.get("representative_success_path_passed") is not True
                or not SHA256_RE.fullmatch(str(disposition.get("evidence_sha256") or ""))
            ):
                raise ValueError("validation guard cannot certify a route method")
            disposition_keys.add(key)
        if disposition_keys != route_keys:
            raise ValueError("route certification dispositions do not match route inventory")
        claimed_hash = report.get("evidence_sha256")
        unhashed = dict(report)
        unhashed.pop("evidence_sha256", None)
        observed_hash = hashlib.sha256(
            json.dumps(
                unhashed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if claimed_hash != observed_hash:
            raise ValueError("route certification evidence hash is invalid")
        profiles.add(profile_id)
    return {
        "runtime_routes": len(normalized),
        "main_routes": counts["5002"],
        "tools_routes": counts["5003"],
        "unmapped_interfaces": len(route_keys - reviewed),
    }


def _recompute_portable_inventory_metrics(
    by_role: dict[str, list[BoundArtifact]], expected_context: dict[str, str]
) -> dict[str, Any]:
    allowed = {
        "upstream_release_marker",
        "upstream_release_manifest",
        "upstream_campaign_report",
        "upstream_portable_inventory",
        "upstream_campaign_cron_snapshot",
        "upstream_campaign_cron_source",
        "upstream_portable_inventory_input_manifest",
        "upstream_portable_inventory_input",
    }
    singleton_roles = allowed - {"upstream_portable_inventory_input"}
    if (
        set(by_role) != allowed
        or any(len(by_role[role]) != 1 for role in singleton_roles)
        or not by_role["upstream_portable_inventory_input"]
    ):
        raise ValueError("portable inventory normalized evidence source roles are not exact")
    _marker, release, files = _verify_release_control_sources(by_role, expected_context)
    release_artifact = _one(by_role, "upstream_release_manifest")
    campaign = _verify_campaign_context_source(
        by_role,
        expected_context,
        manifest=release,
        manifest_sha256=release_artifact.sha256,
    )
    inventory_artifact = _one(by_role, "upstream_portable_inventory")
    inventory_path = "docs/architecture/v3/generated/v2_inventory.json"
    if files.get(inventory_path, {}).get("sha256") != inventory_artifact.sha256:
        raise ValueError("portable inventory is not bound to release manifest")
    cron_artifact = _one(by_role, "upstream_campaign_cron_snapshot")
    if campaign.get("cron_jobs_sha256") != cron_artifact.sha256:
        raise ValueError("portable inventory cron snapshot is not campaign-bound")
    cron_source_artifact = _one(by_role, "upstream_campaign_cron_source")
    if campaign.get("cron_jobs_source_sha256") != cron_source_artifact.sha256:
        raise ValueError("portable inventory cron source is not campaign-bound")
    inventory = _json_artifact(inventory_artifact)
    cron_payload = _load_json_value_bytes(cron_artifact.data, cron_artifact.path)
    cron_rows = cron_payload.get("jobs", cron_payload) if isinstance(cron_payload, dict) else cron_payload
    _exact_nonnegative_int(
        inventory.get("schema_version"), 1, "portable source inventory schema_version"
    )
    if inventory.get("source") != "derived_from_executable_source_not_readme":
        raise ValueError("portable inventory schema/source is invalid")
    counts = inventory.get("counts")
    if not isinstance(counts, dict) or not isinstance(cron_rows, list):
        raise ValueError("portable inventory counts or cron snapshot is invalid")
    for name, value in counts.items():
        _nonnegative_int(value, f"portable inventory counts.{name}")
    internal_ok = all(
        (
            ("http_routes", "http_routes"),
            ("skill_entrypoints", "skill_entrypoints"),
            ("cron_jobs", "cron_jobs"),
            ("daemon_child_declarations", "daemon_children"),
            ("test_modules", "test_modules"),
        )[index][0] in counts
        for index in range(5)
    )
    if not internal_ok:
        raise ValueError("portable inventory required counts are missing")
    expected_lengths = {
        "http_routes": len(inventory.get("http_routes", ())),
        "skill_entrypoints": len(inventory.get("skill_entrypoints", ())),
        "cron_jobs": len(inventory.get("cron_jobs", ())),
        "daemon_child_declarations": len(inventory.get("daemon_children", ())),
        "test_modules": len(inventory.get("test_modules", ())),
    }
    for key, value in expected_lengths.items():
        _exact_nonnegative_int(
            counts.get(key), value, f"portable inventory counts.{key}"
        )

    input_manifest = _json_artifact(
        _one(by_role, "upstream_portable_inventory_input_manifest")
    )
    _exact_nonnegative_int(
        input_manifest.get("schema_version"),
        1,
        "portable inventory input manifest schema_version",
    )
    if input_manifest.get("selection") != "magi.v3.portable-inventory-inputs/v1":
        raise ValueError("portable inventory input manifest selection is invalid")
    declared_inputs = input_manifest.get("inputs")
    if not isinstance(declared_inputs, list) or not declared_inputs:
        raise ValueError("portable inventory input manifest is empty")

    def selected_source(path: str) -> bool:
        relative = PurePosixPath(path)
        return (
            path == "daemon.py"
            or (len(relative.parts) >= 2 and relative.parts[0] == "api" and path.endswith(".py"))
            or (
                len(relative.parts) >= 3
                and relative.parts[0] == "skills"
                and relative.name == "action.py"
            )
            or (
                len(relative.parts) == 3
                and relative.parts[:2] == ("config", "launchagents")
                and path.endswith(".plist")
            )
            or (
                len(relative.parts) == 2
                and relative.parts[0] == "tests"
                and relative.name.startswith("test_")
                and path.endswith(".py")
            )
        )

    expected_input_rows = [
        {
            "path": path,
            "sha256": row["sha256"],
            "size": row["size"],
            "mode": row["mode"],
        }
        for path, row in sorted(files.items())
        if selected_source(path)
    ]
    if declared_inputs != expected_input_rows:
        raise ValueError(
            "portable inventory input manifest does not exactly match release sources"
        )
    input_artifacts = by_role["upstream_portable_inventory_input"]
    if len(input_artifacts) != len(expected_input_rows):
        raise ValueError("portable inventory input artifact count is incomplete")

    with tempfile.TemporaryDirectory(prefix="magi-v3-portable-recompute-") as temporary:
        source_root = Path(temporary) / "release-source"
        for row, artifact in zip(expected_input_rows, input_artifacts, strict=True):
            if artifact.sha256 != row["sha256"] or len(artifact.data) != row["size"]:
                raise ValueError(
                    f"portable inventory source artifact mismatch: {row['path']}"
                )
            destination = source_root / row["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(artifact.data)
        (source_root / "cron_jobs.json").write_bytes(cron_source_artifact.data)
        regenerated = build_inventory(
            source_root,
            include_installed_launchagents=False,
        )
    cron_inventory = collect_portable_cron_bytes(cron_source_artifact.data)
    # ``build_inventory`` normalized against the temporary recomputation path;
    # replace only cron rows with the independently normalized original source.
    regenerated["cron_jobs"] = cron_inventory
    regenerated["counts"]["cron_jobs"] = len(cron_inventory)
    regenerated["counts"]["enabled_cron_jobs"] = sum(
        1 for row in cron_inventory if row.get("enabled") is True
    )
    expected_projection = project_inventory_to_release(
        inventory,
        set(files),
        cron_jobs=cron_inventory,
    )
    regenerated["root_name"] = expected_projection.get("root_name")
    current = expected_projection == regenerated
    return {
        "inventory_sha_matches": current,
        "unmapped_interfaces": 0,
        "unapproved_source_runtime_drift": 0 if current else 1,
    }


def _verify_campaign_ledger(
    by_role: dict[str, list[BoundArtifact]], expected_context: dict[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    allowed = {
        "upstream_campaign_report",
        "upstream_campaign_day",
        "upstream_campaign_cron_snapshot",
        "upstream_release_marker",
        "upstream_release_manifest",
    }
    if set(by_role) != allowed or len(by_role.get("upstream_campaign_day", [])) < 1:
        raise ValueError("campaign normalized evidence source roles are not exact")
    _marker, release, _files = _verify_release_control_sources(by_role, expected_context)
    release_artifact = _one(by_role, "upstream_release_manifest")
    report = _verify_campaign_context_source(
        by_role,
        expected_context,
        manifest=release,
        manifest_sha256=release_artifact.sha256,
    )
    if (
        report.get("certifying") is not True
        or report.get("offline_complete") is not True
        or report.get("decision") != "GO"
    ):
        raise ValueError("campaign report is not a completed certifying offline campaign")
    required_passes = _required_campaign_passes(report)
    cron = _one(by_role, "upstream_campaign_cron_snapshot")
    if report.get("cron_jobs_sha256") != cron.sha256:
        raise ValueError("campaign cron snapshot SHA-256 binding failed")
    days = [_json_artifact(row) for row in by_role["upstream_campaign_day"]]
    declared = report.get("artifacts")
    if not isinstance(declared, list):
        raise ValueError("campaign report artifact ledger is invalid")
    declared_hashes = {row.get("sha256") for row in declared if isinstance(row, dict)}
    if declared_hashes != {row.sha256 for row in by_role["upstream_campaign_day"]}:
        raise ValueError("campaign report day artifact hashes do not match evidence")
    for day in days:
        _context_matches_or_raise(day, expected_context, "campaign day")
        _exact_nonnegative_int(
            day.get("required_independent_passes"),
            required_passes,
            "campaign day required_independent_passes",
        )
        _exact_nonnegative_int(
            day.get("completed_independent_passes"),
            required_passes,
            "campaign day completed_independent_passes",
        )
        if (
            day.get("release_id") != release.get("release_id")
            or day.get("release_commit") != release.get("commit")
            or day.get("release_manifest_sha256") != release_artifact.sha256
            or day.get("cron_jobs_sha256") != cron.sha256
            or day.get("cron_jobs_source_sha256")
            != report.get("cron_jobs_source_sha256")
            or day.get("status") != "offline_passed"
            or day.get("release_gate_eligible") is not True
            or day.get("live_execution_performed") is not False
        ):
            raise ValueError("campaign day is not certifying or release-bound")
    return report, days


def _required_campaign_passes(campaign: dict[str, Any]) -> int:
    required = campaign.get("required_independent_passes")
    if type(required) is not int or required != 1:
        raise ValueError(
            "targeted V3 campaign must contain exactly one independent pass"
        )
    return required


def _campaign_structured_rows(
    days: list[dict[str, Any]],
    workload: str,
    *,
    required_passes: int = 1,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for day in days:
        workloads = day.get("workloads")
        if not isinstance(workloads, list):
            raise ValueError("campaign day workloads are invalid")
        for row in workloads:
            if not isinstance(row, dict) or row.get("workload") != workload:
                continue
            structured = row.get("structured_evidence")
            if isinstance(row, dict) and row.get("workload") == workload:
                _exact_nonnegative_int(
                    row.get("returncode"), 0, f"campaign {workload} returncode"
                )
                _nonnegative_int(
                    row.get("validation_pass"), f"campaign {workload} validation_pass"
                )
            if isinstance(structured, dict):
                _exact_nonnegative_int(
                    structured.get("schema_version"),
                    1,
                    f"campaign {workload} structured_evidence.schema_version",
                )
            if (
                row.get("status") != "offline_passed"
                or not isinstance(structured, dict)
                or structured.get("workload") != workload
                or structured.get("status") != "passed"
                or not isinstance(structured.get("measurements"), dict)
            ):
                raise ValueError(f"campaign {workload} structured row is invalid")
            # The schedule capacity certification runs real job bodies against
            # a disposable loopback fixture plane.  Its top-level
            # ``network_access_performed`` therefore truthfully reports True,
            # while the separately attested external-network field must remain
            # False.  Every other offline campaign workload remains strictly
            # network-free.
            if workload == "seven_day_schedule_10x_arrival_2x_duration_replay":
                if (
                    type(structured.get("network_access_performed")) is not bool
                    or structured.get("external_network_access_performed") is not False
                ):
                    raise ValueError(f"campaign {workload} safety attestation failed")
            elif structured.get("network_access_performed") is not False:
                raise ValueError(f"campaign {workload} safety attestation failed")
            for field in (
                "service_start_performed",
                "production_port_access_performed",
                "launchctl_performed",
            ):
                if structured.get(field) is not False:
                    raise ValueError(f"campaign {workload} safety attestation failed")
            result.append(row)
    if {row["validation_pass"] for row in result} != set(
        range(1, required_passes + 1)
    ):
        raise ValueError(
            f"campaign {workload} lacks the required independent pass rows"
        )
    return result


def _recompute_release_quality_metrics(
    by_role: dict[str, list[BoundArtifact]],
    expected_context: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Recompute gates 3-7 from exact pytest node outcomes and golden snapshots."""

    campaign_roles = {
        "upstream_campaign_report",
        "upstream_campaign_day",
        "upstream_campaign_cron_snapshot",
        "upstream_release_marker",
        "upstream_release_manifest",
    }
    extra_singletons = {
        "upstream_release_quality_suite_manifest": "config/v3_release_quality_suites.json",
        "upstream_release_quality_certifier": (
            "scripts/v3_validation/release_quality_certification.py"
        ),
        "upstream_release_quality_evidence_module": (
            "scripts/v3_validation/release_quality_evidence.py"
        ),
        "upstream_pytest_transcript_plugin": (
            "scripts/v3_validation/pytest_transcript_plugin.py"
        ),
        "upstream_golden_flows_source": "scripts/v3_validation/golden_flows.py",
        "upstream_side_effects_source": "scripts/v3_validation/side_effects.py",
        **{
            f"upstream_golden_dependency_{index}": path
            for index, path in enumerate(GOLDEN_DEPENDENCY_PATHS)
        },
    }
    expected_roles = campaign_roles | set(extra_singletons) | {
        "upstream_release_quality_report"
    }
    if (
        set(by_role) != expected_roles
        or any(len(by_role.get(role, [])) != 1 for role in extra_singletons)
    ):
        raise ValueError("release quality normalized evidence source roles are not exact")
    campaign_sources = {role: by_role[role] for role in campaign_roles}
    campaign, days = _verify_campaign_ledger(campaign_sources, expected_context)
    required_passes = _required_campaign_passes(campaign)
    if len(by_role.get("upstream_release_quality_report", [])) != required_passes:
        raise ValueError("release quality report count is not campaign-bound")
    _marker, release, release_files = _verify_release_control_sources(
        campaign_sources, expected_context
    )
    for role, relative in extra_singletons.items():
        artifact = _one(by_role, role)
        if release_files.get(relative, {}).get("sha256") != artifact.sha256:
            raise ValueError(f"release quality source is not manifest-bound: {relative}")
    suite_manifest = _json_artifact(
        _one(by_role, "upstream_release_quality_suite_manifest")
    )
    python_runtime_sha256 = str(campaign.get("python_runtime_sha256") or "")
    if not SHA256_RE.fullmatch(python_runtime_sha256):
        raise ValueError("release quality campaign runtime SHA-256 is invalid")
    rows = _campaign_structured_rows(
        days, "golden_business_flows", required_passes=required_passes
    )
    artifacts = {
        artifact.sha256: artifact
        for artifact in by_role["upstream_release_quality_report"]
    }
    if len(artifacts) != required_passes:
        raise ValueError("release quality inner reports are duplicated")
    release_hashes = {
        path: str(row.get("sha256") or "") for path, row in release_files.items()
    }
    per_profile: list[dict[str, dict[str, Any]]] = []
    profiles: set[str] = set()
    used_hashes: set[str] = set()
    passes: set[int] = set()
    for row in rows:
        reference = row.get("inner_report_artifact")
        structured = row.get("structured_evidence")
        profile = row.get("validation_profile")
        validation_pass = row.get("validation_pass")
        artifact = (
            artifacts.get(str(reference.get("sha256") or ""))
            if isinstance(reference, dict)
            else None
        )
        if (
            not isinstance(reference, dict)
            or set(reference) != {"path", "sha256"}
            or not isinstance(structured, dict)
            or not isinstance(profile, dict)
            or type(validation_pass) is not int
            or artifact is None
        ):
            raise ValueError("release quality campaign inner reference is invalid")
        inner = _json_artifact(artifact)
        if inner != structured.get("report"):
            raise ValueError("release quality inner report differs from campaign row")
        profile_id = str(profile.get("profile_id") or "")
        if not profile_id or profile_id in profiles or artifact.sha256 in used_hashes:
            raise ValueError("release quality profile/report is duplicated")
        try:
            metrics = summarize_release_quality_report(
                inner,
                manifest=suite_manifest,
                release_files=release_hashes,
                python_runtime_sha256=python_runtime_sha256,
                expected_profile=profile,
                expected_release_id=str(release.get("release_id") or ""),
                expected_release_manifest_sha256=_one(
                    by_role, "upstream_release_manifest"
                ).sha256,
            )
        except ReleaseQualityEvidenceError as exc:
            raise ValueError(f"release quality inner report failed: {exc}") from exc
        if structured.get("measurements") != metrics or inner.get("metrics") != metrics:
            raise ValueError("release quality producer metrics differ from recomputation")
        per_profile.append(metrics)
        profiles.add(profile_id)
        used_hashes.add(artifact.sha256)
        passes.add(validation_pass)
    if (
        len(profiles) != required_passes
        or passes != set(range(1, required_passes + 1))
        or used_hashes != set(artifacts)
    ):
        raise ValueError(
            "release quality campaign lacks the required independent reports"
        )

    def aggregate(evidence_id: str, field: str) -> int:
        values = [row[evidence_id].get(field) for row in per_profile]
        if any(type(value) is not int for value in values):
            raise ValueError(f"release quality metric is invalid: {evidence_id}.{field}")
        return sum(values)

    return {
        "v2_regression_passed_in_release_venv": {
            "disabled": all(
                row["v2_regression_passed_in_release_venv"].get("disabled") is True
                for row in per_profile
            ),
            "release_venv_verified": False,
            "passed": aggregate("v2_regression_passed_in_release_venv", "passed"),
            "failed": aggregate("v2_regression_passed_in_release_venv", "failed"),
        },
        "v3_unit_contract_integration_e2e_passed": {
            "failed": aggregate("v3_unit_contract_integration_e2e_passed", "failed"),
            "suites": list(EXPECTED_V3_SUITES),
            "all_required_suites_passed": all(
                row["v3_unit_contract_integration_e2e_passed"][
                    "all_required_suites_passed"
                ]
                is True
                for row in per_profile
            ),
        },
        "interaction_agent_kernel_memory_quality_contracts_passed": {
            "failed_contracts": aggregate(
                "interaction_agent_kernel_memory_quality_contracts_passed",
                "failed_contracts",
            ),
            "contract_groups": list(EXPECTED_QUALITY_GROUPS),
            "quality_non_regression_passed": all(
                row["interaction_agent_kernel_memory_quality_contracts_passed"][
                    "quality_non_regression_passed"
                ]
                is True
                for row in per_profile
            ),
        },
        "context_memory_tool_plan_answer_golden_sets_passed": {
            "failed_cases": aggregate(
                "context_memory_tool_plan_answer_golden_sets_passed", "failed_cases"
            ),
            "sets": list(EXPECTED_GOLDEN_SETS),
            "all_sets_passed": all(
                row["context_memory_tool_plan_answer_golden_sets_passed"][
                    "all_sets_passed"
                ]
                is True
                for row in per_profile
            ),
        },
        "golden_side_effect_diff_approved": {
            "unapproved_contract_diffs": aggregate(
                "golden_side_effect_diff_approved", "unapproved_contract_diffs"
            ),
            "duplicate_side_effects": aggregate(
                "golden_side_effect_diff_approved", "duplicate_side_effects"
            ),
            "golden_diff_completed": all(
                row["golden_side_effect_diff_approved"]["golden_diff_completed"]
                is True
                for row in per_profile
            ),
        },
    }


def _recompute_resource_performance_partial_metrics(
    by_role: dict[str, list[BoundArtifact]],
    expected_context: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Recompute resource/performance gates from raw partial reports and soak rows."""

    campaign_roles = {
        "upstream_campaign_report",
        "upstream_campaign_day",
        "upstream_campaign_cron_snapshot",
        "upstream_release_marker",
        "upstream_release_manifest",
    }
    extra = {
        "upstream_resource_performance_certifier": (
            "scripts/v3_validation/resource_performance_certification.py"
        ),
        "upstream_resource_performance_evidence_module": (
            "scripts/v3_validation/resource_performance_evidence.py"
        ),
        "upstream_perf_compat_source": "scripts/v3_validation/perf_compat.py",
        "upstream_matched_performance_source": (
            "scripts/v3_validation/perf_certification.py"
        ),
        "upstream_isolated_resource_window_source": (
            "scripts/v3_validation/isolated_resource_window.py"
        ),
        "upstream_isolated_resource_window_collector": (
            "scripts/v3_validation/isolated_resource_window_collector.py"
        ),
        "upstream_isolated_resource_window_plan_builder": (
            "scripts/v3_validation/isolated_resource_window_plan_builder.py"
        ),
        "upstream_resource_window_core_adapter": (
            "scripts/v3_validation/resource_window_core_adapter.py"
        ),
        "upstream_resource_window_model_adapter": (
            "scripts/v3_validation/resource_window_model_adapter.py"
        ),
        "upstream_resource_source": "magi_v3/resource.py",
        "upstream_dispatcher_source": "magi_v3/dispatcher.py",
        "upstream_ledger_source": "magi_v3/ledger.py",
        "upstream_supervisor_source": "magi_v3/supervisor.py",
        "upstream_macos_resource_source": "magi_v3/macos_resources.py",
        "upstream_resource_policy": "config/v3_resource_policy.json",
    }
    expected_roles = campaign_roles | set(extra) | {
        "upstream_resource_performance_report"
    }
    if (
        set(by_role) != expected_roles
        or any(len(by_role.get(role, [])) != 1 for role in extra)
        or len(by_role.get("upstream_resource_performance_report", [])) != 7
    ):
        raise ValueError("resource/performance partial source roles are not exact")
    campaign_sources = {role: by_role[role] for role in campaign_roles}
    campaign, days = _verify_campaign_ledger(campaign_sources, expected_context)
    _marker, release, release_files = _verify_release_control_sources(
        campaign_sources, expected_context
    )
    for role, relative in extra.items():
        if release_files.get(relative, {}).get("sha256") != _one(by_role, role).sha256:
            raise ValueError(
                f"resource/performance source is not manifest-bound: {relative}"
            )
    runtime_sha = str(campaign.get("python_runtime_sha256") or "")
    if not SHA256_RE.fullmatch(runtime_sha):
        raise ValueError("resource/performance runtime SHA-256 is invalid")
    rows = _campaign_structured_rows(days, "matched_v2_v3_performance")
    artifacts = {
        artifact.sha256: artifact
        for artifact in by_role["upstream_resource_performance_report"]
    }
    release_hashes = {
        path: str(row.get("sha256") or "") for path, row in release_files.items()
    }
    metrics_by_profile: list[dict[str, dict[str, Any]]] = []
    used: set[str] = set()
    profiles: set[str] = set()
    passes: set[int] = set()
    manifest_sha = _one(by_role, "upstream_release_manifest").sha256
    for row in rows:
        reference = row.get("inner_report_artifact")
        structured = row.get("structured_evidence")
        profile = row.get("validation_profile")
        validation_pass = row.get("validation_pass")
        artifact = (
            artifacts.get(str(reference.get("sha256") or ""))
            if isinstance(reference, dict)
            else None
        )
        if (
            artifact is None
            or not isinstance(structured, dict)
            or not isinstance(profile, dict)
            or type(validation_pass) is not int
        ):
            raise ValueError("resource/performance campaign inner reference is invalid")
        inner = _json_artifact(artifact)
        if inner != structured.get("report"):
            raise ValueError("resource/performance inner report differs from campaign row")
        try:
            metrics = summarize_resource_performance_report(
                inner,
                release_files=release_hashes,
                python_runtime_sha256=runtime_sha,
                expected_profile=profile,
                expected_release_id=str(release.get("release_id") or ""),
                expected_release_manifest_sha256=manifest_sha,
            )
        except ResourcePerformanceEvidenceError as exc:
            raise ValueError(f"resource/performance inner report failed: {exc}") from exc
        profile_id = str(profile.get("profile_id") or "")
        if (
            not profile_id
            or profile_id in profiles
            or artifact.sha256 in used
            or structured.get("measurements") != metrics
        ):
            raise ValueError("resource/performance profile/report is duplicated or drifted")
        metrics_by_profile.append(metrics)
        profiles.add(profile_id)
        used.add(artifact.sha256)
        passes.add(validation_pass)
    if len(profiles) != 7 or passes != set(range(1, 8)) or used != set(artifacts):
        raise ValueError("resource/performance campaign lacks seven independent reports")

    performance_rows = [row[RESOURCE_PERFORMANCE_GATE_IDS[0]] for row in metrics_by_profile]
    resource_rows = [row[RESOURCE_PERFORMANCE_GATE_IDS[1]] for row in metrics_by_profile]
    preemption_rows = [row[RESOURCE_PERFORMANCE_GATE_IDS[2]] for row in metrics_by_profile]
    worker_rows = [row[RESOURCE_PERFORMANCE_GATE_IDS[3]] for row in metrics_by_profile]
    soak_rows = _campaign_structured_rows(days, "hundred_cycle_worker_reap_soak")
    soak_measurements = [row["structured_evidence"]["measurements"] for row in soak_rows]
    return {
        RESOURCE_PERFORMANCE_GATE_IDS[0]: {
            "matched_disposable_dependencies": all(
                row["matched_disposable_dependencies"] is True
                for row in performance_rows
            ),
            "matched_production_dependencies": all(
                row["matched_production_dependencies"] is True
                for row in performance_rows
            ),
            "warm_and_cold_measured": all(
                row["warm_and_cold_measured"] is True for row in performance_rows
            ),
            "maximum_p95_regression_ratio": max(
                row["maximum_p95_regression_ratio"] for row in performance_rows
            ),
            "model_tokens_per_second_measured": all(
                row["model_tokens_per_second_measured"] is True
                for row in performance_rows
            ),
            "minimum_model_tokens_per_second_ratio": min(
                float(row.get("minimum_model_tokens_per_second_ratio", 0.0))
                for row in performance_rows
            ),
            "missing_requirements": sorted(
                {item for row in performance_rows for item in row["missing_requirements"]}
            ),
        },
        RESOURCE_PERFORMANCE_GATE_IDS[1]: {
            "all_budgets_passed": all(
                row["all_budgets_passed"] is True for row in resource_rows
            ),
            "idle_swapout_growth_mb": max(
                row["idle_swapout_growth_mb"] for row in resource_rows
            ),
            "observation_seconds": max(row["observation_seconds"] for row in resource_rows),
            "required_idle_observation_seconds": 1800,
            "application_plane_footprint_reduction_ratio": min(
                float(row.get("application_plane_footprint_reduction_ratio", 0.0))
                for row in resource_rows
            ),
            "missing_budget_profiles": sorted(
                {item for row in resource_rows for item in row["missing_budget_profiles"]}
            ),
        },
        RESOURCE_PERFORMANCE_GATE_IDS[2]: {
            "preemption_passed": all(
                row["preemption_passed"] is True for row in preemption_rows
            ),
            "automatic_preemption_observed": all(
                row["automatic_preemption_observed"] is True
                for row in preemption_rows
            ),
            "independent_passes": len(preemption_rows),
            "independent_samples": sum(
                row["independent_samples"] for row in preemption_rows
            ),
            "p0_p1_deadline_misses": sum(
                row["p0_p1_deadline_misses"] for row in preemption_rows
            ),
            "interactive_queue_p95_ms": max(
                row["interactive_queue_p95_ms"] for row in preemption_rows
            ),
            "interactive_queue_p95_seconds": max(
                row["interactive_queue_p95_seconds"]
                for row in preemption_rows
            ),
            "p1_browser_queue_p95_seconds": max(
                row["p1_browser_queue_p95_seconds"] for row in preemption_rows
            ),
            "orphan_process_groups": sum(
                row["orphan_process_groups"] for row in preemption_rows
            ),
            "duplicate_completions": sum(
                row["duplicate_completions"] for row in preemption_rows
            ),
            "lost_jobs": sum(row["lost_jobs"] for row in preemption_rows),
            "preempted_jobs_requeued": sum(
                row["preempted_jobs_requeued"] for row in preemption_rows
            ),
            "attempt_two_unique_completions": sum(
                row["attempt_two_unique_completions"] for row in preemption_rows
            ),
            "missing_requirements": sorted(
                {item for row in preemption_rows for item in row["missing_requirements"]}
            ),
        },
        RESOURCE_PERFORMANCE_GATE_IDS[3]: {
            "cycles": sum(
                int(row.get("cycles_completed", 0))
                for row in soak_measurements
                if type(row.get("cycles_completed")) is int
            ),
            "orphan_process_groups": sum(
                max(
                    0,
                    int(row.get("cycles_completed", 0))
                    - int(row.get("process_groups_gone", 0)),
                )
                for row in soak_measurements
                if type(row.get("cycles_completed")) is int
                and type(row.get("process_groups_gone")) is int
            ),
            "rss_returned_to_baseline": all(
                row["rss_returned_to_baseline"] is True for row in worker_rows
            ),
            "physical_footprint_returned_to_baseline": all(
                row["physical_footprint_returned_to_baseline"] is True
                for row in worker_rows
            ),
            "metal_measurement_available": all(
                row["metal_measurement_available"] is True for row in worker_rows
            ),
            "metal_returned_to_baseline": all(
                row["metal_returned_to_baseline"] is True for row in worker_rows
            ),
            "rss_return_window_measured": all(
                row["rss_return_window_measured"] is True for row in worker_rows
            ),
            "physical_footprint_return_window_measured": all(
                row["physical_footprint_return_window_measured"] is True
                for row in worker_rows
            ),
            "independent_passes": len(worker_rows),
            "independent_samples": sum(
                row["independent_samples"] for row in worker_rows
            ),
            "return_p95_seconds": max(
                row["return_p95_seconds"] for row in worker_rows
            ),
            "return_budget_seconds": 30.0,
            "missing_requirements": sorted(
                {item for row in worker_rows for item in row["missing_requirements"]}
            ),
        },
    }


def _recompute_campaign_metrics(
    evidence_id: str,
    by_role: dict[str, list[BoundArtifact]],
    expected_context: dict[str, str],
) -> dict[str, Any]:
    report, days = _verify_campaign_ledger(by_role, expected_context)
    required_passes = _required_campaign_passes(report)
    if evidence_id == "notification_storm_and_dlq_faults_passed":
        rows = _campaign_structured_rows(
            days, "fault_injection", required_passes=required_passes
        )
        notifications: list[dict[str, Any]] = []
        for row in rows:
            matrix = row["structured_evidence"]["measurements"].get("matrix")
            if not isinstance(matrix, list):
                raise ValueError("fault campaign matrix is missing")
            matches = [item for item in matrix if isinstance(item, dict) and item.get("fault") == "notification_storm_dlq"]
            if len(matches) != 1:
                raise ValueError("fault campaign notification row is missing or duplicated")
            notifications.extend(matches)
        for row_index, row in enumerate(notifications):
            for field in ("duplicate", "committed", "dead_lettered", "recovered"):
                _nonnegative_int(row.get(field), f"notification row {row_index} {field}")
        return {
            "notification_storm_passed": all(row.get("status") == "passed" for row in notifications),
            "dlq_recovery_passed": all(
                row.get("dead_lettered") == row.get("committed")
                and row.get("recovered") == row.get("committed")
                for row in notifications
            ),
            "duplicate_side_effects": sum(row["duplicate"] for row in notifications),
            "unbounded_queue_growth": sum(
                max(0, row["committed"] - row["dead_lettered"])
                for row in notifications
            ),
        }
    if evidence_id == "hundred_cycle_worker_reap_soak_passed":
        from scripts.v3_validation.worker_soak_evidence import (
            summarize_worker_soak_measurements,
        )

        rows = _campaign_structured_rows(
            days,
            "hundred_cycle_worker_reap_soak",
            required_passes=required_passes,
        )
        measurements = [row["structured_evidence"]["measurements"] for row in rows]
        return summarize_worker_soak_measurements(measurements)
    raise ValueError("campaign evidence id has no authoritative recomputation")


def _validate_health_inner_report(
    report: dict[str, Any],
    *,
    expected_profile: dict[str, Any],
    release_files: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Independently recompute the strict health result from a hashed inner report."""

    supplied_hash = report.get("evidence_sha256")
    unhashed = dict(report)
    unhashed.pop("evidence_sha256", None)
    observed_hash = hashlib.sha256(
        json.dumps(
            unhashed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        report.get("schema") != "magi.v3.health-probe-certification/v1"
        or report.get("status") != "certified"
        or report.get("probe") != "production_health_service_liveness"
        or report.get("validation_profile") != expected_profile
        or supplied_hash != observed_hash
    ):
        raise ValueError("health inner report identity/profile/hash is invalid")
    generated_at = report.get("generated_at")
    try:
        generated = datetime.fromisoformat(str(generated_at))
    except ValueError as exc:
        raise ValueError("health inner report timestamp is invalid") from exc
    if generated.tzinfo is None:
        raise ValueError("health inner report timestamp must include a timezone")
    measurements = report.get("measurements")
    exact = {
        "probe_count": 1_000,
        "successful_probes": 1_000,
        "failed_probes": 0,
        "model_imports": 0,
        "models_loaded": 0,
        "model_probe_flags": 0,
        "newly_loaded_heavy_modules": [],
        "state_mutations": [],
    }
    if not isinstance(measurements, dict) or any(
        measurements.get(key) != value for key, value in exact.items()
    ):
        raise ValueError("health inner report is not strict 1000/1000 zero-model read-only")
    for field in ("total_duration_us", "maximum_probe_us"):
        value = measurements.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"health inner report timing is invalid: {field}")
    safety = report.get("safety")
    if not isinstance(safety, dict) or any(
        safety.get(field) is not False
        for field in (
            "network_access_performed",
            "service_start_performed",
            "production_port_access_performed",
            "launchctl_performed",
            "runtime_initialized",
        )
    ):
        raise ValueError("health inner report safety proof failed")
    source_binding = report.get("release_binding")
    certifier_sha = release_files.get(
        "scripts/v3_validation/health_certification.py", {}
    ).get("sha256")
    health_sha = release_files.get("magi_v3/health.py", {}).get("sha256")
    if (
        not SHA256_RE.fullmatch(str(certifier_sha or ""))
        or not SHA256_RE.fullmatch(str(health_sha or ""))
        or not isinstance(source_binding, dict)
        or source_binding
        != {
            "certifier_script_sha256": certifier_sha,
            "health_module_sha256": health_sha,
        }
    ):
        raise ValueError("health inner report source hashes are not release-bound")
    return measurements


def _recompute_health_metrics(
    by_role: dict[str, list[BoundArtifact]], expected_context: dict[str, str]
) -> dict[str, Any]:
    health_role = "upstream_health_certification_report"
    campaign_config_role = "upstream_campaign_config"
    base_roles = {
        "upstream_campaign_report",
        "upstream_campaign_day",
        "upstream_campaign_cron_snapshot",
        "upstream_release_marker",
        "upstream_release_manifest",
    }
    if (
        set(by_role) != base_roles | {health_role, campaign_config_role}
        or len(by_role[campaign_config_role]) != 1
    ):
        raise ValueError("health normalized evidence source roles are not exact")
    base = {
        role: rows
        for role, rows in by_role.items()
        if role not in {health_role, campaign_config_role}
    }
    campaign, days = _verify_campaign_ledger(base, expected_context)
    required_passes = _required_campaign_passes(campaign)
    if len(by_role[health_role]) != required_passes:
        raise ValueError("health report count is not campaign-bound")
    _marker, _release, release_files = _verify_release_control_sources(
        base, expected_context
    )
    artifacts = {item.sha256: item for item in by_role[health_role]}
    if len(artifacts) != required_passes:
        raise ValueError("health certification inner reports are duplicated")
    campaign_config_artifact = _one(by_role, campaign_config_role)
    if (
        release_files.get("config/v3_validation_campaign.json", {}).get("sha256")
        != campaign_config_artifact.sha256
        or campaign.get("campaign_config_sha256") != campaign_config_artifact.sha256
    ):
        raise ValueError("health campaign config hash is not release/report-bound")
    campaign_config = _json_artifact(campaign_config_artifact)
    offline = campaign_config.get("offline_campaign")
    expected_profiles = (
        offline.get("validation_pass_profiles") if isinstance(offline, dict) else None
    )
    if (
        not isinstance(expected_profiles, list)
        or len(expected_profiles) != required_passes
        or not isinstance(offline, dict)
        or offline.get("required_independent_passes") != required_passes
        or "health_1000_model_free" not in offline.get("workloads", [])
    ):
        raise ValueError("health campaign config lacks the required profiles")
    used_hashes: set[str] = set()
    profiles: set[str] = set()
    passes: set[int] = set()
    measurements: list[dict[str, Any]] = []
    for day in days:
        workloads = day.get("workloads")
        if not isinstance(workloads, list):
            raise ValueError("health campaign workloads are invalid")
        for outcome in workloads:
            if (
                not isinstance(outcome, dict)
                or outcome.get("workload") != "health_1000_model_free"
            ):
                continue
            reference = outcome.get("inner_report_artifact")
            profile = outcome.get("validation_profile")
            structured = outcome.get("structured_evidence")
            artifact = (
                artifacts.get(str(reference.get("sha256") or ""))
                if isinstance(reference, dict)
                else None
            )
            if (
                outcome.get("status") != "offline_passed"
                or type(outcome.get("returncode")) is not int
                or outcome.get("returncode") != 0
                or type(outcome.get("validation_pass")) is not int
                or not 1 <= outcome["validation_pass"] <= required_passes
                or not isinstance(profile, dict)
                or set(profile) != {"profile_id", "replay_start_local", "fault_seed"}
                or profile != expected_profiles[outcome["validation_pass"] - 1]
                or not isinstance(structured, dict)
                or not isinstance(reference, dict)
                or set(reference) != {"path", "sha256"}
                or artifact is None
            ):
                raise ValueError("health campaign outcome/report reference is invalid")
            report = _json_artifact(artifact)
            if structured.get("report") != report:
                raise ValueError("health inner report differs from campaign outcome")
            profile_id = profile.get("profile_id")
            validation_pass = outcome["validation_pass"]
            if (
                not isinstance(profile_id, str)
                or not profile_id
                or profile_id in profiles
                or validation_pass in passes
                or artifact.sha256 in used_hashes
            ):
                raise ValueError("health campaign profile/pass/report is duplicated")
            measurements.append(
                _validate_health_inner_report(
                    report,
                    expected_profile=profile,
                    release_files=release_files,
                )
            )
            profiles.add(profile_id)
            passes.add(validation_pass)
            used_hashes.add(artifact.sha256)
    if (
        len(measurements) != required_passes
        or len(profiles) != required_passes
        or passes != set(range(1, required_passes + 1))
        or used_hashes != set(artifacts)
    ):
        raise ValueError("health campaign lacks the required profile reports")
    return {
        "profile_count": required_passes,
        "probe_count": 1_000,
        "successful_probes": 1_000,
        "total_probe_count": sum(item["probe_count"] for item in measurements),
        "failed_probes": sum(item["failed_probes"] for item in measurements),
        "model_imports": sum(item["model_imports"] for item in measurements),
        "models_loaded": sum(item["models_loaded"] for item in measurements),
        "state_mutations": sum(len(item["state_mutations"]) for item in measurements),
    }


def _validate_fault_inner_report(
    report: dict[str, Any],
    *,
    expected_profile: dict[str, Any],
    release_files: dict[str, dict[str, Any]],
    python_runtime_sha256: str,
) -> dict[str, Any]:
    """Recompute the approved offline controlled-restart fault layer."""

    supplied_hash = report.get("evidence_sha256")
    unsigned = dict(report)
    unsigned.pop("evidence_sha256", None)
    observed_hash = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        report.get("schema") != "magi.v3.fault-certification/v2"
        or report.get("status") != "certified_controlled_restart_fault_layer"
        or report.get("validation_profile") != expected_profile
        or supplied_hash != observed_hash
    ):
        raise ValueError("fault inner report identity/profile/hash is invalid")
    from scripts.v3_validation.fault_certification import build_fault_stimulus_plan

    stimulus_plan = report.get("stimulus_plan")
    if (
        not isinstance(stimulus_plan, dict)
        or stimulus_plan != build_fault_stimulus_plan(expected_profile)
    ):
        raise ValueError("fault inner report stimulus plan is invalid")
    try:
        generated = datetime.fromisoformat(str(report.get("generated_at")))
    except ValueError as exc:
        raise ValueError("fault inner report timestamp is invalid") from exc
    if generated.tzinfo is None:
        raise ValueError("fault inner report timestamp must include a timezone")
    decision = report.get("decision")
    residual = report.get("residual_risk")
    required_residuals = [
        "controlled cold restart with boot-session change",
        "V2 readiness and single-owner restoration after restart",
    ]
    if (
        not isinstance(decision, dict)
        or decision.get("blocker_code")
        != "FAULT_CAMPAIGN_CONTROLLED_RESTART_DEFERRED"
        or decision.get("required_evidence_id")
        != "sqlite_wal_disk_full_fsync_faults_passed"
        or decision.get("eligible_to_clear_fault_campaign_realism_blocker") is not True
        or decision.get("software_equivalent_layer_certified") is not True
        or decision.get("transaction_stage_sigkill_certified") is not True
        or decision.get("external_device_disconnect_required") is not False
        or decision.get("physical_power_cut_required") is not False
        or decision.get("controlled_cold_restart_required_at_cutover") is not True
        or decision.get("hard_gate_blocked") is not False
        or not isinstance(residual, dict)
        or residual.get("accepted_by_equivalent_layer") is not True
        or residual.get("hard_gate_blocking") is not False
        or residual.get("deferred_gate")
        != "atomic_release_switch_and_cold_rollback_drill_passed"
        or residual.get("required_before_final_replacement") != required_residuals
    ):
        raise ValueError("fault inner report controlled-restart deferral is invalid")
    safety = report.get("safety")
    if (
        not isinstance(safety, dict)
        or any(
            safety.get(field) is not False
            for field in (
                "live_magi_state_accessed",
                "live_business_database_accessed",
                "production_service_started",
                "production_port_accessed",
                "launchctl_invoked",
                "network_accessed",
            )
        )
        or safety.get("signals_sent_only_to_owned_children") is not True
        or safety.get("apfs_mount_was_disposable_sparse_image") is not True
        or safety.get("apfs_image_detached_and_removed") is not True
        or not SHA256_RE.fullmatch(str(safety.get("sandbox_path_sha256") or ""))
    ):
        raise ValueError("fault inner report safety proof failed")
    binding = report.get("release_binding")
    certifier_sha = release_files.get(
        "scripts/v3_validation/fault_certification.py", {}
    ).get("sha256")
    realism_sha = release_files.get("scripts/v3_validation/fault_realism.py", {}).get(
        "sha256"
    )
    helper = binding.get("mach_helper") if isinstance(binding, dict) else None
    if (
        not SHA256_RE.fullmatch(str(certifier_sha or ""))
        or not SHA256_RE.fullmatch(str(realism_sha or ""))
        or not isinstance(binding, dict)
        or binding.get("certifier_script_sha256") != certifier_sha
        or binding.get("fault_probe_script_sha256") != realism_sha
        or binding.get("python_executable_sha256") != python_runtime_sha256
        or not isinstance(helper, dict)
        or any(
            not SHA256_RE.fullmatch(str(helper.get(field) or ""))
            for field in ("source_sha256", "executable_sha256")
        )
    ):
        raise ValueError("fault inner report source/runtime hashes are not release-bound")
    measurements = report.get("measurements")
    if not isinstance(measurements, dict):
        raise ValueError("fault inner report measurements are missing")
    apfs = measurements.get("apfs_enospc")
    if (
        not isinstance(apfs, dict)
        or apfs.get("status") != "passed"
        or apfs.get("filesystem") != "apfs"
        or apfs.get("image_type") != "sparsebundle"
        or apfs.get("image_capacity_bytes") != 33_554_432
        or apfs.get("recovery_reserve_bytes") != 4_194_304
        or apfs.get("sqlite_overhead_reserve_bytes") != 1_048_576
        or apfs.get("sqlite_full_attempt_isolated_to_owned_child") is not True
        or apfs.get("sqlite_recovery_isolated_to_owned_child") is not True
        or apfs.get("fault_filler_removed_before_recovery") is not True
        or type(apfs.get("filler_bytes_before_enospc")) is not int
        or not 0
        < apfs["filler_bytes_before_enospc"]
        < apfs["image_capacity_bytes"]
        or apfs.get("filesystem_enospc_observed") is not True
        or apfs.get("filesystem_enospc_operation") not in {"write", "fsync"}
        or apfs.get("sqlite_full_observed") is not True
        or apfs.get("sqlite_error_code") != 13
        or apfs.get("sqlite_error_name") != "SQLITE_FULL"
        or apfs.get("committed_rows_preserved") != 1
        or apfs.get("partial_rows_visible") != 0
        or apfs.get("final_jobs") != 2
        or apfs.get("integrity_check") != "ok"
    ):
        raise ValueError("fault APFS sparse-image ENOSPC evidence failed")
    vfs = measurements.get("sqlite_wal_fsync_io_error")
    if (
        not isinstance(vfs, dict)
        or vfs.get("status") != "passed"
        or vfs.get("injection_boundary") != "custom SQLite VFS xSync"
        or vfs.get("injected_error") != "SQLITE_IOERR_FSYNC"
        or vfs.get("injected_file_role") != "wal"
        or vfs.get("commit_rc") != 1034
        or vfs.get("extended_rc") != 1034
        or vfs.get("expected_extended_rc") != 1034
        or vfs.get("injected") != 1
        or type(vfs.get("sync_calls_after_arm")) is not int
        or vfs["sync_calls_after_arm"] < 1
        or vfs.get("baseline_rows") != 1
        or vfs.get("partial_rows") != 0
        or vfs.get("recovery_rc") != 0
        or vfs.get("final_rows") != 2
        or vfs.get("integrity_ok") != 1
        or vfs.get("journal_mode") != "wal"
        or vfs.get("synchronous") != "FULL"
        or vfs.get("power_loss_simulated") is not False
        or not SHA256_RE.fullmatch(str(vfs.get("source_sha256") or ""))
        or not SHA256_RE.fullmatch(str(vfs.get("executable_sha256") or ""))
    ):
        raise ValueError("fault SQLite WAL xSync evidence failed")
    stages = [
        "READY",
        "BEGIN",
        "JOB_INSERT",
        *(f"PAYLOAD_{index:02d}" for index in range(32)),
        "COMMIT_STARTED",
        "COMMIT_ACK",
    ]
    logical = measurements.get("logical_transaction_boundary_sweep")
    logical_cycles = logical.get("cycles") if isinstance(logical, dict) else None
    if (
        not isinstance(logical, dict)
        or logical.get("stages_requested") != 37
        or logical.get("stages_completed") != 37
        or logical.get("stage_markers") != stages
        or logical.get("acknowledged_commits_lost") != 0
        or logical.get("partially_visible_transactions") != 0
        or logical.get("final_job_rows") != 37
        or logical.get("final_unique_jobs") != 37
        or logical.get("final_payload_rows") != 1_184
        or logical.get("duplicate_jobs") != 0
        or logical.get("lost_jobs_after_recovery") != 0
        or logical.get("integrity_check") != "ok"
        or not isinstance(logical_cycles, list)
        or len(logical_cycles) != 37
        or any(not isinstance(row, dict) for row in logical_cycles)
        or [row.get("target_stage") for row in logical_cycles] != stages
        or any(
            row.get("signal") != "SIGKILL"
            or row.get("final_job_rows") != 1
            or row.get("final_payload_rows") != 32
            or row.get("integrity_check") != "ok"
            for row in logical_cycles
        )
    ):
        raise ValueError("fault logical transaction-boundary evidence failed")
    mach = measurements.get("mach_clock_sigkill")
    offsets = stimulus_plan.get("mach_kill_offsets_us")
    mach_cycles = mach.get("cycles") if isinstance(mach, dict) else None
    if (
        not isinstance(offsets, list)
        or len(offsets) != 6
        or any(type(offset) is not int or not 0 <= offset <= 20_000 for offset in offsets)
        or len(set(offsets)) != 6
        or not isinstance(mach, dict)
        or mach.get("clock") != "mach_absolute_time"
        or mach.get("wait") != "mach_wait_until"
        or mach.get("offsets_us") != offsets
        or mach.get("cycles_completed") != 6
        or mach.get("acknowledged_commits_lost") != 0
        or mach.get("partially_visible_transactions") != 0
        or mach.get("duplicate_jobs") != 0
        or mach.get("lost_jobs_after_recovery") != 0
        or mach.get("final_job_rows") != 6
        or mach.get("final_payload_rows") != 192
        or mach.get("integrity_check") != "ok"
        or not isinstance(mach_cycles, list)
        or len(mach_cycles) != 6
    ):
        raise ValueError("fault Mach-clock aggregate evidence failed")
    for index, row in enumerate(mach_cycles):
        timing = row.get("timing") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or not isinstance(timing, dict)
            or timing.get("clock") != "mach_absolute_time"
            or timing.get("wait") != "mach_wait_until"
            or timing.get("signal") != "SIGKILL"
            or timing.get("scheduled_delay_ns") != offsets[index] * 1_000
            or type(timing.get("target_pid")) is not int
            or timing["target_pid"] <= 1
            or row.get("final_job_rows") != 1
            or row.get("final_payload_rows") != 32
            or row.get("integrity_check") != "ok"
        ):
            raise ValueError("fault Mach-clock cycle evidence failed")
    return measurements


def _recompute_fault_metrics(
    by_role: dict[str, list[BoundArtifact]], expected_context: dict[str, str]
) -> dict[str, Any]:
    fault_role = "upstream_fault_certification_report"
    config_role = "upstream_campaign_config"
    runtime_role = "upstream_python_runtime_manifest"
    physical_roles = {
        "upstream_physical_fault_drill_report",
        "upstream_physical_fault_drill_plan",
        "upstream_physical_fault_authorization",
        "upstream_physical_fault_certifier",
    }
    base_roles = {
        "upstream_campaign_report",
        "upstream_campaign_day",
        "upstream_campaign_cron_snapshot",
        "upstream_release_marker",
        "upstream_release_manifest",
    }
    supplied_physical_roles = set(by_role) & physical_roles
    if supplied_physical_roles:
        raise ValueError(
            "external-device or hard-power evidence is outside the controlled-restart model"
        )
    allowed_roles = base_roles | {fault_role, config_role, runtime_role}
    if (
        set(by_role) != allowed_roles
        or len(by_role[config_role]) != 1
        or len(by_role[runtime_role]) != 1
    ):
        raise ValueError("fault normalized evidence source roles are not exact")
    base = {
        role: rows
        for role, rows in by_role.items()
        if role not in {fault_role, config_role, runtime_role} | physical_roles
    }
    campaign, days = _verify_campaign_ledger(base, expected_context)
    required_passes = _required_campaign_passes(campaign)
    if len(by_role[fault_role]) != required_passes:
        raise ValueError("fault report count is not campaign-bound")
    _marker, _release, release_files = _verify_release_control_sources(
        base, expected_context
    )
    config_artifact = _one(by_role, config_role)
    if (
        release_files.get("config/v3_validation_campaign.json", {}).get("sha256")
        != config_artifact.sha256
        or campaign.get("campaign_config_sha256") != config_artifact.sha256
    ):
        raise ValueError("fault campaign config is not release/report-bound")
    config = _json_artifact(config_artifact)
    offline = config.get("offline_campaign")
    profiles = offline.get("validation_pass_profiles") if isinstance(offline, dict) else None
    workloads = offline.get("workloads") if isinstance(offline, dict) else None
    if (
        not isinstance(profiles, list)
        or len(profiles) != required_passes
        or not isinstance(workloads, list)
        or "fault_recovery_certification" not in workloads
        or offline.get("required_independent_passes") != required_passes
    ):
        raise ValueError("fault campaign config lacks the required profiles")
    runtime_artifact = _one(by_role, runtime_role)
    runtime = _json_artifact(runtime_artifact)
    python_sha = campaign.get("python_runtime_sha256")
    if (
        campaign.get("python_runtime_manifest_sha256") != runtime_artifact.sha256
        or runtime.get("schema_version") != 1
        or runtime.get("python_runtime_sha256") != python_sha
        or runtime.get("tree_sha256") != campaign.get("python_runtime_tree_sha256")
        or not SHA256_RE.fullmatch(str(python_sha or ""))
    ):
        raise ValueError("fault campaign Python runtime is not manifest-bound")
    artifacts = {item.sha256: item for item in by_role[fault_role]}
    if len(artifacts) != required_passes:
        raise ValueError("fault certification inner reports are duplicated")
    measurements: list[dict[str, Any]] = []
    profiles_seen: set[str] = set()
    passes: set[int] = set()
    used_hashes: set[str] = set()
    stimulus_plan_hashes: set[str] = set()
    for day in days:
        workloads_rows = day.get("workloads")
        if not isinstance(workloads_rows, list):
            raise ValueError("fault campaign workloads are invalid")
        for outcome in workloads_rows:
            if (
                not isinstance(outcome, dict)
                or outcome.get("workload") != "fault_recovery_certification"
            ):
                continue
            reference = outcome.get("inner_report_artifact")
            profile = outcome.get("validation_profile")
            structured = outcome.get("structured_evidence")
            validation_pass = outcome.get("validation_pass")
            artifact = (
                artifacts.get(str(reference.get("sha256") or ""))
                if isinstance(reference, dict)
                else None
            )
            if (
                outcome.get("status") != "offline_passed"
                or type(outcome.get("returncode")) is not int
                or outcome.get("returncode") != 0
                or type(validation_pass) is not int
                or not 1 <= validation_pass <= required_passes
                or not isinstance(profile, dict)
                or profile != profiles[validation_pass - 1]
                or not isinstance(structured, dict)
                or not isinstance(reference, dict)
                or set(reference) != {"path", "sha256"}
                or artifact is None
            ):
                raise ValueError("fault campaign outcome/report reference is invalid")
            inner = _json_artifact(artifact)
            if structured.get("report") != inner:
                raise ValueError("fault inner report differs from campaign outcome")
            profile_id = profile.get("profile_id")
            stimulus_plan = inner.get("stimulus_plan")
            stimulus_plan_sha = (
                stimulus_plan.get("stimulus_plan_sha256")
                if isinstance(stimulus_plan, dict)
                else None
            )
            if (
                not isinstance(profile_id, str)
                or not profile_id
                or profile_id in profiles_seen
                or validation_pass in passes
                or artifact.sha256 in used_hashes
                or not SHA256_RE.fullmatch(str(stimulus_plan_sha or ""))
                or stimulus_plan_sha in stimulus_plan_hashes
            ):
                raise ValueError("fault campaign profile/pass/report/stimulus is duplicated")
            measurements.append(
                _validate_fault_inner_report(
                    inner,
                    expected_profile=profile,
                    release_files=release_files,
                    python_runtime_sha256=str(python_sha),
                )
            )
            profiles_seen.add(profile_id)
            passes.add(validation_pass)
            used_hashes.add(artifact.sha256)
            stimulus_plan_hashes.add(str(stimulus_plan_sha))
    if (
        len(measurements) != required_passes
        or len(profiles_seen) != required_passes
        or passes != set(range(1, required_passes + 1))
        or used_hashes != set(artifacts)
        or len(stimulus_plan_hashes) != required_passes
    ):
        raise ValueError("fault campaign lacks the required profile reports")
    logical = [row["logical_transaction_boundary_sweep"] for row in measurements]
    mach = [row["mach_clock_sigkill"] for row in measurements]
    apfs = [row["apfs_enospc"] for row in measurements]
    vfs = [row["sqlite_wal_fsync_io_error"] for row in measurements]
    return {
        "profile_count": required_passes,
        "unique_stimulus_plan_count": len(stimulus_plan_hashes),
        "software_equivalent_layer_passed": True,
        "sqlite_wal_fault_passed": True,
        "apfs_sparse_image_enospc_passed": True,
        "fsync_fault_passed": True,
        "logical_transaction_sweep_passed": True,
        "mach_clock_offset_sigkill_passed": True,
        "transaction_stage_sigkill_passed": True,
        "controlled_cold_restart_deferred_to_cutover": True,
        "external_device_disconnect_required": False,
        "physical_power_cut_required": False,
        "acknowledged_commits_lost": sum(
            row["acknowledged_commits_lost"] for row in logical + mach
        ),
        "partially_visible_transactions": sum(
            row["partially_visible_transactions"] for row in logical + mach
        )
        + sum(row["partial_rows_visible"] for row in apfs)
        + sum(row["partial_rows"] for row in vfs),
        "duplicate_jobs": sum(row["duplicate_jobs"] for row in logical + mach),
        "lost_jobs_after_recovery": sum(
            row["lost_jobs_after_recovery"] for row in logical + mach
        ),
        "unreconciled_ambiguous_commits": 0,
        "residual_hard_gate_blocked": False,
    }


def _authoritative_normalized_metrics(
    evidence_id: str,
    artifacts: Sequence[BoundArtifact],
    expected_context: dict[str, str],
    config: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    by_role = _artifacts_by_role([item for item in artifacts if item.role != "producer_report"])
    if evidence_id == "atomic_release_switch_and_cold_rollback_drill_passed":
        source_contract = config.get("source_contract")
        if (
            isinstance(source_contract, dict)
            and source_contract.get("legacy_v2_validation") == "disabled"
        ):
            from scripts.v3_validation.v3_rotation_drill import (
                V3RotationDrillBlocked,
                derive_v3_rotation_metrics,
            )

            expected_roles = {"upstream_v3_rotation_drill_report"}
            if set(by_role) != expected_roles or any(
                len(rows) != 1 for rows in by_role.values()
            ):
                raise ValueError(
                    "G27 V3-only rotation normalized evidence source roles are not exact"
                )
            artifact = _one(by_role, "upstream_v3_rotation_drill_report")
            _json_artifact(artifact)
            with tempfile.TemporaryDirectory(prefix="magi-v3-rotation-gate-") as temporary:
                report_path = Path(temporary).resolve() / "v3-rotation-report.json"
                report_path.write_bytes(artifact.data)
                try:
                    return derive_v3_rotation_metrics(
                        report_path,
                        expected_context=expected_context,
                        gate_config=config,
                    )
                except V3RotationDrillBlocked as exc:
                    raise ValueError(
                        f"G27 authoritative V3 rotation evidence blocked: {exc}"
                    ) from exc
        from scripts.v3_validation.cutover_evidence import (
            CutoverEvidenceBlocked,
            RawPair,
            derive_cutover_metrics,
        )

        expected_roles = {
            "upstream_controlled_restart_plan",
            "upstream_controlled_restart_report",
            "upstream_controlled_restart_sentinel",
            *(f"upstream_cutover_plan_{index}" for index in range(1, 4)),
            *(f"upstream_cutover_report_{index}" for index in range(1, 4)),
            *(f"upstream_rollback_plan_{index}" for index in range(1, 4)),
            *(f"upstream_rollback_report_{index}" for index in range(1, 4)),
        }
        if set(by_role) != expected_roles or any(len(rows) != 1 for rows in by_role.values()):
            raise ValueError("G27 atomic normalized evidence source roles are not exact")
        with tempfile.TemporaryDirectory(prefix="magi-v3-atomic-gate-") as temporary:
            root = Path(temporary).resolve()
            restart_plan_artifact = _one(
                by_role, "upstream_controlled_restart_plan"
            )
            restart_report_artifact = _one(
                by_role, "upstream_controlled_restart_report"
            )
            restart_sentinel_artifact = _one(
                by_role, "upstream_controlled_restart_sentinel"
            )
            restart_plan_path = root / "controlled-restart-plan.json"
            restart_report_path = root / "controlled-restart-report.json"
            restart_sentinel_path = root / "controlled-restart-sentinel.sqlite3"
            restart_plan_path.write_bytes(restart_plan_artifact.data)
            restart_report_path.write_bytes(restart_report_artifact.data)
            restart_sentinel_path.write_bytes(restart_sentinel_artifact.data)
            pairs = []
            for index in range(1, 4):
                artifacts_by_name = {
                    name: _one(by_role, f"upstream_{name}_{index}")
                    for name in (
                        "cutover_plan",
                        "cutover_report",
                        "rollback_plan",
                        "rollback_report",
                    )
                }
                paths = {
                    name: root / f"{name.replace('_', '-')}-{index}.json"
                    for name in artifacts_by_name
                }
                for name, path in paths.items():
                    path.write_bytes(artifacts_by_name[name].data)
                pairs.append(
                    RawPair(
                        cutover_plan_path=paths["cutover_plan"],
                        cutover_plan_sha256=artifacts_by_name["cutover_plan"].sha256,
                        cutover_report_path=paths["cutover_report"],
                        rollback_plan_path=paths["rollback_plan"],
                        rollback_plan_sha256=artifacts_by_name["rollback_plan"].sha256,
                        rollback_report_path=paths["rollback_report"],
                    )
                )
            try:
                return derive_cutover_metrics(
                    pairs=pairs,
                    controlled_restart_plan_path=restart_plan_path,
                    controlled_restart_plan_sha256=restart_plan_artifact.sha256,
                    controlled_restart_report_path=restart_report_path,
                    controlled_restart_sentinel_path=restart_sentinel_path,
                    expected_context=expected_context,
                    gate_config=config,
                )
            except CutoverEvidenceBlocked as exc:
                raise ValueError(f"G27 authoritative atomic evidence blocked: {exc}") from exc
    if evidence_id == "human_go_approval_recorded":
        import inspect

        from scripts.v3_validation.human_approval import (
            FrozenSource,
            HumanApprovalBlocked,
            derive_conditional_human_approval_metrics,
            derive_human_approval_metrics,
        )

        legacy_fixed_roles = {
            "upstream_approval_request",
            "upstream_approval_receipt",
            "upstream_approval_gate_report",
            "upstream_approval_gate_config",
        }
        conditional_fixed_roles = {
            "upstream_conditional_request",
            "upstream_conditional_receipt",
            "upstream_conditional_consumption",
            "upstream_approval_gate_report",
            "upstream_approval_gate_config",
        }
        if conditional_fixed_roles.issubset(by_role):
            fixed_roles = conditional_fixed_roles
            request_role = "upstream_conditional_request"
            receipt_role = "upstream_conditional_receipt"
            conditional = True
        else:
            fixed_roles = legacy_fixed_roles
            request_role = "upstream_approval_request"
            receipt_role = "upstream_approval_receipt"
            conditional = False
        if not fixed_roles.issubset(by_role) or any(
            len(by_role.get(role, ())) != 1 for role in fixed_roles
        ):
            raise ValueError("G28 approval fixed source roles are not exact")
        request_artifact = _one(by_role, request_role)
        request = _json_artifact(request_artifact)
        consumption_artifact = (
            _one(by_role, "upstream_conditional_consumption") if conditional else None
        )
        consumption = (
            _json_artifact(consumption_artifact) if consumption_artifact is not None else None
        )
        rows = consumption.get("machine_evidence") if conditional and consumption else request.get("machine_evidence")
        if not isinstance(rows, list):
            raise ValueError("G28 approval request machine source manifest is missing")
        machine_roles: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("artifacts"), list):
                raise ValueError("G28 approval request machine source row is invalid")
            machine_roles.add(str(row.get("envelope_source_role") or ""))
            machine_roles.update(
                str(item.get("source_role") or "")
                for item in row["artifacts"]
                if isinstance(item, dict)
            )
        expected_roles = fixed_roles | machine_roles
        if (
            "" in machine_roles
            or set(by_role) != expected_roles
            or any(len(rows_for_role) != 1 for rows_for_role in by_role.values())
        ):
            raise ValueError("G28 approval normalized source roles are not exact")
        receipt_artifact = _one(by_role, receipt_role)
        receipt = _json_artifact(receipt_artifact)
        gate_artifact = _one(by_role, "upstream_approval_gate_report")
        config_artifact = _one(by_role, "upstream_approval_gate_config")
        gate = _json_artifact(gate_artifact)
        approval_config = _json_artifact(config_artifact)
        machine_sources = {
            role: FrozenSource(
                role=role,
                path=Path(artifact.path),
                data=artifact.data,
                sha256=artifact.sha256,
            )
            for role in machine_roles
            for artifact in (_one(by_role, role),)
        }
        arguments: dict[str, Any] = {
            "request": request,
            "request_sha256": request_artifact.sha256,
            "receipt": receipt,
            "receipt_sha256": receipt_artifact.sha256,
            "gate_report": gate,
            "gate_report_sha256": gate_artifact.sha256,
            "gate_config": approval_config,
            "gate_config_sha256": config_artifact.sha256,
            "machine_sources": machine_sources,
            "expected_context": expected_context,
        }
        if conditional:
            assert consumption_artifact is not None and consumption is not None
            arguments.update(
                {
                    "consumption": consumption,
                    "consumption_sha256": consumption_artifact.sha256,
                }
            )
            # Conditional approval must never silently fall through to the
            # legacy verifier.  The authoritative verifier owns the exact
            # request/receipt/one-time-consumption binding.
            if request.get("schema") != "magi.v3.conditional-human-approval-request/v2":
                raise ValueError("G28 conditional request schema is not exact")
        try:
            verifier = derive_conditional_human_approval_metrics if conditional else derive_human_approval_metrics
            parameters = inspect.signature(verifier).parameters
            if conditional and not {"consumption", "consumption_sha256"}.issubset(parameters):
                raise ValueError("G28 conditional verifier does not bind consumption")
            return verifier(
                **{key: value for key, value in arguments.items() if key in parameters}
            )
        except HumanApprovalBlocked as exc:
            raise ValueError(f"G28 authoritative human approval blocked: {exc}") from exc
    if evidence_id == "seven_day_schedule_10x_arrival_2x_duration_replay_passed":
        from scripts.v3_validation.schedule_evidence import (
            derive_schedule_gate_metrics,
            enabled_job_ids_from_cron,
        )

        campaign_roles = {
            "upstream_campaign_report",
            "upstream_campaign_day",
            "upstream_campaign_cron_snapshot",
            "upstream_release_marker",
            "upstream_release_manifest",
        }
        release_sources = {
            "upstream_schedule_dispatch_policy",
            "upstream_schedule_capacity_certifier",
            "upstream_schedule_body_registry_script",
            "upstream_schedule_body_registry_config",
            "upstream_schedule_duration_baseline",
        }
        if (
            not campaign_roles.issubset(by_role)
            or any(not by_role.get(role) for role in campaign_roles)
        ):
            raise ValueError("G11 normalized evidence source roles are not exact")
        campaign_sources = {role: by_role[role] for role in campaign_roles}
        campaign, days = _verify_campaign_ledger(campaign_sources, expected_context)
        required_passes = _required_campaign_passes(campaign)
        expected_roles = {
            *campaign_roles,
            *release_sources,
            *(
                f"upstream_schedule_capacity_report_{index}"
                for index in range(1, required_passes + 1)
            ),
            *(
                f"upstream_schedule_body_evidence_{index}"
                for index in range(1, required_passes + 1)
            ),
        }
        if (
            set(by_role) != expected_roles
            or any(
                len(rows) != 1
                for role, rows in by_role.items()
                if role != "upstream_campaign_day"
            )
            or not by_role.get("upstream_campaign_day")
        ):
            raise ValueError("G11 normalized evidence source roles are not exact")
        schedule_rows = sorted(
            _campaign_structured_rows(
                days,
                "seven_day_schedule_10x_arrival_2x_duration_replay",
                required_passes=required_passes,
            ),
            key=lambda row: row["validation_pass"],
        )
        _marker, release, release_files = _verify_release_control_sources(
            campaign_sources, expected_context
        )
        source_paths = {
            "upstream_schedule_dispatch_policy": "config/v3_schedule_dispatch_policy.json",
            "upstream_schedule_capacity_certifier": (
                "scripts/v3_validation/schedule_capacity_certification.py"
            ),
            "upstream_schedule_body_registry_script": (
                "scripts/v3_validation/schedule_body_registry.py"
            ),
            "upstream_schedule_body_registry_config": (
                "config/v3_schedule_body_adapter_registry.json"
            ),
            "upstream_schedule_duration_baseline": (
                "config/v3_schedule_realism_baseline.json"
            ),
        }
        for role, path in source_paths.items():
            if release_files.get(path, {}).get("sha256") != _one(by_role, role).sha256:
                raise ValueError(f"G11 schedule source is not manifest-bound: {path}")
        reports = [
            _json_artifact(_one(by_role, f"upstream_schedule_capacity_report_{index}"))
            for index in range(1, required_passes + 1)
        ]
        body_reports = [
            _json_artifact(_one(by_role, f"upstream_schedule_body_evidence_{index}"))
            for index in range(1, required_passes + 1)
        ]
        for index, (row, capacity, body) in enumerate(
            zip(schedule_rows, reports, body_reports, strict=True), 1
        ):
            structured = row.get("structured_evidence")
            capacity_ref = row.get("inner_report_artifact")
            body_ref = row.get("body_evidence_artifact")
            profile = row.get("validation_profile")
            if (
                not isinstance(structured, dict)
                or structured.get("report") != capacity
                or structured.get("body_evidence") != body
                or not isinstance(capacity_ref, dict)
                or capacity_ref.get("sha256")
                != _one(by_role, f"upstream_schedule_capacity_report_{index}").sha256
                or not isinstance(body_ref, dict)
                or body_ref.get("sha256")
                != _one(by_role, f"upstream_schedule_body_evidence_{index}").sha256
                or not isinstance(profile, dict)
                or capacity.get("validation_profile_id") != profile.get("profile_id")
            ):
                raise ValueError(f"G11 campaign/raw report binding failed at pass {index}")
        cron_artifact = _one(by_role, "upstream_campaign_cron_snapshot")
        if cron_artifact.media_type != "application/json":
            raise ValueError("G11 campaign cron snapshot must be application/json")
        enabled_job_ids = enabled_job_ids_from_cron(
            _load_json_value_bytes(cron_artifact.data, cron_artifact.path)
        )
        return derive_schedule_gate_metrics(
            reports,
            body_reports,
            enabled_job_ids=enabled_job_ids,
            cron_jobs_sha256=cron_artifact.sha256,
            dispatch_policy_sha256=_one(
                by_role, "upstream_schedule_dispatch_policy"
            ).sha256,
            certifier_sha256=_one(
                by_role, "upstream_schedule_capacity_certifier"
            ).sha256,
            registry_script_sha256=_one(
                by_role, "upstream_schedule_body_registry_script"
            ).sha256,
            registry_config_sha256=_one(
                by_role, "upstream_schedule_body_registry_config"
            ).sha256,
            duration_baseline_sha256=_one(
                by_role, "upstream_schedule_duration_baseline"
            ).sha256,
            release_id=str(release.get("release_id") or ""),
            release_manifest_sha256=_one(
                by_role, "upstream_release_manifest"
            ).sha256,
        )
    if evidence_id in {
        "offline_replay_and_isolated_live_validation_satisfied",
        "isolated_live_validation_single_active_handoff_verified",
        "v2_fully_stopped_before_v3_start_verified",
        "single_scheduler_consumer_writer_ownership_verified",
        "single_active_handoff_test_passed",
        "v3_fully_stopped_before_v2_rollback_verified",
        "input_method_candidate_window_probe_passed",
    }:
        from scripts.v3_validation.isolated_live_evidence import (
            RawRun,
            derive_isolated_live_metrics,
        )

        expected_roles = {
            "upstream_validation_campaign_config",
            "upstream_offline_campaign_day",
            *(f"upstream_isolated_live_plan_{index}" for index in range(1, 4)),
            *(f"upstream_isolated_live_report_{index}" for index in range(1, 4)),
        }
        if set(by_role) != expected_roles or any(len(rows) != 1 for rows in by_role.values()):
            raise ValueError("isolated LIVE normalized evidence source roles are not exact")
        campaign_artifact = _one(by_role, "upstream_validation_campaign_config")
        day_artifact = _one(by_role, "upstream_offline_campaign_day")
        campaign_config = _json_artifact(campaign_artifact)
        offline_day = _json_artifact(day_artifact)
        with tempfile.TemporaryDirectory(prefix="magi-v3-live-gate-") as temporary:
            root = Path(temporary).resolve()
            runs = []
            for index in range(1, 4):
                plan_artifact = _one(by_role, f"upstream_isolated_live_plan_{index}")
                report_artifact = _one(by_role, f"upstream_isolated_live_report_{index}")
                plan_path = root / f"plan-{index}.json"
                report_path = root / f"report-{index}.json"
                plan_path.write_bytes(plan_artifact.data)
                report_path.write_bytes(report_artifact.data)
                runs.append(
                    RawRun(
                        plan_path=plan_path,
                        plan_sha256=plan_artifact.sha256,
                        report_path=report_path,
                    )
                )
            metrics = derive_isolated_live_metrics(
                offline_campaign_day=offline_day,
                campaign_config=campaign_config,
                campaign_config_sha256=campaign_artifact.sha256,
                runs=runs,
                expected_context=expected_context,
                gate_config=config,
            )
        return metrics[evidence_id]
    if evidence_id in {"database_backup_restore_drill_passed", "runtime_state_snapshot_verified"}:
        contract = _source_contract(config)
        if report.get("source_contract") != contract:
            raise ValueError("backup producer report source_contract is not gate-config-bound")
        return _recompute_backup_metrics(by_role, expected_context, contract)[evidence_id]
    if evidence_id == "rendered_launchagent_manifest_checksums_saved":
        return _recompute_deploy_metrics(by_role, expected_context)
    if evidence_id == "runtime_route_inventory_current":
        return _recompute_route_metrics(by_role, expected_context)
    if evidence_id == "portable_source_inventory_current":
        return _recompute_portable_inventory_metrics(by_role, expected_context)
    if evidence_id in {
        "notification_storm_and_dlq_faults_passed",
        "hundred_cycle_worker_reap_soak_passed",
    }:
        return _recompute_campaign_metrics(evidence_id, by_role, expected_context)
    if evidence_id in {
        "v2_regression_passed_in_release_venv",
        "v3_unit_contract_integration_e2e_passed",
        "interaction_agent_kernel_memory_quality_contracts_passed",
        "context_memory_tool_plan_answer_golden_sets_passed",
        "golden_side_effect_diff_approved",
    }:
        return _recompute_release_quality_metrics(by_role, expected_context)[evidence_id]
    if evidence_id in RESOURCE_PERFORMANCE_GATE_IDS:
        return _recompute_resource_performance_partial_metrics(
            by_role, expected_context
        )[evidence_id]
    if evidence_id == "health_1000_probes_loaded_zero_models":
        return _recompute_health_metrics(by_role, expected_context)
    if evidence_id == "sqlite_wal_disk_full_fsync_faults_passed":
        return _recompute_fault_metrics(by_role, expected_context)
    raise ValueError("normalized evidence id is not code-owned or allowed")


def validate_evidence_semantics(
    document: dict[str, Any],
    evidence_id: str,
    *,
    config: dict[str, Any],
    bound_artifacts: Sequence[BoundArtifact],
    expected_context: dict[str, str],
) -> list[str]:
    """Recompute trusted evidence; never accept producer assertions alone."""

    spec = EVIDENCE_SPECS.get(evidence_id)
    if spec is None:
        return ["no code-owned semantic evidence specification is registered"]
    errors: list[str] = []
    if document.get("producer") != spec.producer:
        errors.append(f"producer must equal the registered producer {spec.producer!r}")

    artifacts = document.get("artifacts")
    primary = [item for item in bound_artifacts if item.role == "producer_report"]
    if len(primary) != 1:
        errors.append("artifacts must contain exactly one producer_report")
        return errors
    producer_artifact = primary[0]
    if producer_artifact.media_type != "application/json":
        errors.append("producer_report media_type must equal application/json")
        return errors
    try:
        report = _json_artifact(producer_artifact)
    except ValueError as exc:
        return errors + [f"producer_report is not a readable JSON object: {exc}"]

    expected_report_fields = {
        "schema_version": 1,
        "report_schema": spec.report_schema,
        "evidence_id": evidence_id,
        "status": document.get("status"),
        "producer": spec.producer,
        "generated_at": document.get("generated_at"),
        **expected_context,
    }
    for field, expected in expected_report_fields.items():
        observed = report.get(field)
        if (
            observed != expected
            or (type(expected) is int and type(observed) is not int)
        ):
            errors.append(f"producer_report {field} does not match {expected!r}")

    normalized_by = report.get("normalized_by")
    if normalized_by is None:
        errors.append(
            "direct producer evidence has no code-owned authoritative verifier; "
            "producer/schema/metrics assertions are insufficient"
        )
    else:
        if evidence_id not in NORMALIZED_EVIDENCE_WHITELIST:
            errors.append("evidence id is forbidden for normalized evidence")
        if normalized_by != TRUSTED_NORMALIZER:
            errors.append("producer_report normalized_by is not a trusted normalizer")
        if report.get("normalizer_schema") != TRUSTED_NORMALIZER_SCHEMA:
            errors.append("producer_report normalizer_schema is invalid")
        declared_sources = report.get("source_artifacts")
        envelope_sources = [
            {
                "role": item.role,
                "media_type": item.media_type,
                "path": item.path,
                "sha256": item.sha256,
            }
            for item in bound_artifacts
            if item.role != "producer_report"
        ]
        if not isinstance(declared_sources, list) or not declared_sources:
            errors.append("normalized producer_report must bind source_artifacts")
        elif declared_sources != envelope_sources:
            errors.append(
                "producer_report source_artifacts do not match the normalized evidence envelope"
            )
        elif (
            len({item.get("path") for item in declared_sources}) != len(declared_sources)
            or any(
                not isinstance(item.get("role"), str)
                or not item["role"].startswith("upstream_")
                or not isinstance(item.get("media_type"), str)
                or not item["media_type"]
                for item in declared_sources
                if isinstance(item, dict)
            )
        ):
            errors.append("normalized producer_report source_artifacts are invalid or duplicated")
        normalization = report.get("normalization")
        if (
            not isinstance(normalization, dict)
            or normalization.get("defaults_used") is not False
            or normalization.get("live_state_accessed") is not False
            or normalization.get("service_start_performed") is not False
        ):
            errors.append("normalized producer_report safety attestation is invalid")

    run_context = report.get("run_context")
    if not isinstance(run_context, dict):
        errors.append("producer_report run_context must be an object")
    else:
        for field, expected in expected_context.items():
            if run_context.get(field) != expected:
                errors.append(
                    f"producer_report run_context.{field} does not match release context"
                )
        if run_context.get("execution_mode") != spec.execution_mode:
            errors.append(
                "producer_report run_context.execution_mode must equal "
                f"{spec.execution_mode!r}"
            )
        if not isinstance(run_context.get("run_id"), str) or not run_context["run_id"].strip():
            errors.append("producer_report run_context.run_id must be a non-empty string")
        started, started_error = _parse_timestamp(run_context.get("started_at"))
        completed, completed_error = _parse_timestamp(run_context.get("completed_at"))
        generated, generated_error = _parse_timestamp(document.get("generated_at"))
        if started_error:
            errors.append("producer_report run_context.started_at must be timezone-aware ISO-8601")
        if completed_error:
            errors.append(
                "producer_report run_context.completed_at must be timezone-aware ISO-8601"
            )
        timestamps_valid = (
            not generated_error
            and started is not None
            and completed is not None
            and generated is not None
        )
        if timestamps_valid:
            if completed < started:
                errors.append("producer_report run_context.completed_at precedes started_at")
            if completed > generated:
                errors.append("producer_report run_context.completed_at is after generated_at")

    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("producer_report metrics must be an object")
        return errors
    metrics_digest = hashlib.sha256(_canonical_json_bytes(metrics)).hexdigest()
    if report.get("metrics_sha256") != metrics_digest:
        errors.append("producer_report metrics_sha256 does not match canonical metrics")
    if document.get("metrics_sha256") != metrics_digest:
        errors.append("evidence metrics_sha256 does not match producer_report metrics")

    if normalized_by is not None and evidence_id in NORMALIZED_EVIDENCE_WHITELIST:
        try:
            authoritative_metrics = _authoritative_normalized_metrics(
                evidence_id, bound_artifacts, expected_context, config, report
            )
        except (ValueError, OSError, sqlite3.Error, tarfile.TarError) as exc:
            errors.append(f"authoritative evidence verification failed: {exc}")
        else:
            if metrics != authoritative_metrics:
                errors.append(
                    "producer report metrics do not match code-owned authoritative recomputation"
                )

    if document.get("status") == "passed":
        for rule in spec.rules:
            error = _validate_metric_rule(metrics, rule, config)
            if error:
                errors.append(error)
    return errors


def evaluate_evidence(
    config: dict[str, Any],
    evidence_dir: Path,
    *,
    expected_context: dict[str, str],
    now: datetime | None = None,
    max_age_hours: float = DEFAULT_MAX_EVIDENCE_AGE_HOURS,
) -> dict[str, Any]:
    required = config.get("required_evidence")
    if not isinstance(required, list) or not all(
        isinstance(item, str) and item.strip() and EVIDENCE_ID_RE.fullmatch(item) for item in required
    ):
        raise ValueError("cutover config required_evidence must be a list of non-empty strings")
    if not required:
        raise ValueError("cutover config required_evidence must not be empty")
    if len(required) != len(set(required)):
        raise ValueError("cutover config required_evidence contains duplicates")
    if {
        "database_backup_restore_drill_passed",
        "runtime_state_snapshot_verified",
    } & set(required):
        _source_contract(config)
    canonical_required = list(EVIDENCE_SPECS)
    if required != canonical_required:
        missing_specs = sorted(set(required) - set(EVIDENCE_SPECS))
        omitted_contracts = sorted(set(EVIDENCE_SPECS) - set(required))
        details = []
        if missing_specs:
            details.append(f"unregistered={missing_specs}")
        if omitted_contracts:
            details.append(f"omitted={omitted_contracts}")
        if not details:
            details.append("order does not match the code-owned semantic contract")
        raise ValueError(
            "cutover config required_evidence must exactly match semantic evidence specs: "
            + "; ".join(details)
        )
    if (
        not isinstance(max_age_hours, (int, float))
        or isinstance(max_age_hours, bool)
        or not math.isfinite(max_age_hours)
        or max_age_hours <= 0
    ):
        raise ValueError("max_age_hours must be finite and greater than zero")
    context_errors = [
        field
        for field in CONTEXT_FIELDS
        if not isinstance(expected_context.get(field), str) or not expected_context[field].strip()
    ]
    if context_errors:
        raise ValueError(f"expected release context is missing: {', '.join(context_errors)}")
    if not SHA256_RE.fullmatch(expected_context["gate_config_sha256"]):
        raise ValueError("expected gate_config_sha256 must be lowercase SHA-256")

    missing: list[str] = []
    failed: list[str] = []
    invalid: dict[str, list[str]] = {}
    passed: list[str] = []
    # The final executor is deliberately not allowed to trust a stale GO
    # report alone for unattended cutover.  Keep a hash-bound pointer to the
    # already-authoritatively-verified G28 envelope and only expose the small
    # conditional authorization projection it needs to recheck at mutation
    # time.  Legacy approvals leave this field absent.
    conditional_authorization: dict[str, Any] | None = None
    for evidence_id in required:
        path = evidence_dir / f"{evidence_id}.json"
        if not path.is_file():
            missing.append(evidence_id)
            continue
        try:
            document = load_json(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            invalid[evidence_id] = [str(exc)]
            continue
        errors = validate_evidence(
            document,
            evidence_id,
            expected_context=expected_context,
            now=now,
            max_age_hours=float(max_age_hours),
        )
        bound_artifacts: list[BoundArtifact] = []
        if not errors:
            bound_artifacts, artifact_errors = freeze_artifacts(document, evidence_dir)
            errors.extend(artifact_errors)
        if not errors:
            errors.extend(
                validate_evidence_semantics(
                    document,
                    evidence_id,
                    config=config,
                    bound_artifacts=bound_artifacts,
                    expected_context=expected_context,
                )
            )
        if errors:
            invalid[evidence_id] = errors
        elif document["status"] != "passed":
            failed.append(evidence_id)
        else:
            passed.append(evidence_id)
            if evidence_id == "human_go_approval_recorded":
                producer = _json_artifact(_one(_artifacts_by_role(bound_artifacts), "producer_report"))
                metrics = producer.get("metrics")
                if isinstance(metrics, dict) and metrics.get("authorization_mode") == "conditional_daytime_window":
                    window = metrics.get("conditional_daytime_window")
                    digests = {
                        key: metrics.get(key)
                        for key in (
                            "conditional_request_sha256",
                            "conditional_receipt_sha256",
                            "conditional_consumption_sha256",
                        )
                    }
                    if not (
                        isinstance(window, dict)
                        and all(isinstance(value, str) and SHA256_RE.fullmatch(value) for value in digests.values())
                    ):
                        invalid[evidence_id] = ["conditional G28 metrics are incomplete"]
                        passed.pop()
                        continue
                    conditional_authorization = {
                        "evidence_path": str(path.resolve()),
                        "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "metrics_sha256": hashlib.sha256(_canonical_json_bytes(metrics)).hexdigest(),
                        "conditional_daytime_window": window,
                        **digests,
                    }

    no_go_reasons = []
    if missing:
        no_go_reasons.append("required_evidence_missing")
    if failed:
        no_go_reasons.append("required_evidence_failed")
    if invalid:
        no_go_reasons.append("required_evidence_invalid")
    decision = "GO" if not no_go_reasons else "NO_GO"
    result = {
        "schema_version": 1,
        "semantic_contract_version": 1,
        "semantic_contract_count": len(EVIDENCE_SPECS),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "fail_closed": True,
        "expected_context": dict(expected_context),
        "max_evidence_age_hours": float(max_age_hours),
        "required_count": len(required),
        "passed_count": len(passed),
        "passed": passed,
        "missing": missing,
        "failed": failed,
        "invalid": invalid,
        "no_go_reasons": no_go_reasons,
    }
    if conditional_authorization is not None:
        result["conditional_authorization"] = conditional_authorization
    return result


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=root / "config" / "v3_cutover_gates.json")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    parser.add_argument("--max-evidence-age-hours", type=float, default=DEFAULT_MAX_EVIDENCE_AGE_HOURS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        actual_gate_digest = hashlib.sha256(args.config.read_bytes()).hexdigest()
        if actual_gate_digest != args.gate_config_sha256:
            raise ValueError("--gate-config-sha256 does not match the selected gate config")
        expected_context = {field: str(getattr(args, field)) for field in CONTEXT_FIELDS}
        report = evaluate_evidence(
            load_json(args.config),
            args.evidence_dir,
            expected_context=expected_context,
            max_age_hours=args.max_evidence_age_hours,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "decision": "NO_GO",
            "fail_closed": True,
            "fatal_error": str(exc),
        }

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["decision"] == "GO" else 2


if __name__ == "__main__":
    raise SystemExit(main())
