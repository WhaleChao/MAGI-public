#!/usr/bin/env python3
"""Normalize three independent isolated-LIVE executions into release evidence.

The module accepts only raw executor plans/reports plus one candidate-bound
offline campaign day.  All metrics are recomputed from immutable artifacts;
callers cannot submit metrics, counts, or pass/fail assertions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.v3_validation.isolated_live_execute import (
    IsolatedLiveBlocked,
    load_isolated_live_plan,
    verify_static_plan,
)


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LIVE_EVIDENCE_IDS = (
    "offline_replay_and_isolated_live_validation_satisfied",
    "isolated_live_validation_single_active_handoff_verified",
    "v2_fully_stopped_before_v3_start_verified",
    "single_scheduler_consumer_writer_ownership_verified",
    "single_active_handoff_test_passed",
    "v3_fully_stopped_before_v2_rollback_verified",
    "input_method_candidate_window_probe_passed",
)
REQUIRED_COVERAGE = {"process", "pidfile", "port", "launchd", "ownership"}


class IsolatedLiveEvidenceBlocked(ValueError):
    """Raw LIVE artifacts are missing, duplicated, drifted, or unsafe."""


@dataclass(frozen=True, slots=True)
class RawRun:
    plan_path: Path
    plan_sha256: str
    report_path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, description: str) -> dict[str, Any]:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise IsolatedLiveEvidenceBlocked(
            f"{description} must be a canonical absolute non-symlink file"
        )
    try:
        value = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IsolatedLiveEvidenceBlocked(f"{description} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise IsolatedLiveEvidenceBlocked(f"{description} must be a JSON object")
    return value


def _time(value: Any, description: str) -> datetime:
    if not isinstance(value, str):
        raise IsolatedLiveEvidenceBlocked(f"{description} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IsolatedLiveEvidenceBlocked(f"{description} is invalid") from exc
    if parsed.tzinfo is None:
        raise IsolatedLiveEvidenceBlocked(f"{description} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _threshold(config: Mapping[str, Any], name: str) -> int:
    values = config.get("promotion_thresholds")
    value = values.get(name) if isinstance(values, dict) else None
    if type(value) is not int or value <= 0:
        raise IsolatedLiveEvidenceBlocked(f"promotion threshold {name} is invalid")
    return value


def _campaign_policy(config: Mapping[str, Any]) -> tuple[int, int, int]:
    isolated = config.get("isolated_live_validation")
    offline = config.get("offline_campaign")
    if not isinstance(isolated, dict) or not isinstance(offline, dict):
        raise IsolatedLiveEvidenceBlocked("validation campaign policy is incomplete")
    required_runs = isolated.get("required_runs")
    reset_minutes = isolated.get("minimum_reset_minutes")
    window_hours = isolated.get("completion_window_hours")
    required_offline = offline.get("required_independent_passes")
    if (
        type(required_runs) is not int
        or required_runs != 3
        or type(reset_minutes) is not int
        or reset_minutes < 0
        or type(window_hours) is not int
        or window_hours <= 0
        or type(required_offline) is not int
        or required_offline <= 0
    ):
        raise IsolatedLiveEvidenceBlocked("validation campaign thresholds are unsafe")
    return reset_minutes, window_hours, required_offline


def _subsequence(actions: Sequence[str], required: Sequence[str]) -> bool:
    cursor = 0
    for action in actions:
        if cursor < len(required) and action == required[cursor]:
            cursor += 1
    return cursor == len(required)


def _snapshot(event: Mapping[str, Any]) -> dict[str, Any]:
    detail = event.get("detail")
    snapshot = detail.get("snapshot") if isinstance(detail, dict) else None
    assessment = snapshot.get("assessment") if isinstance(snapshot, dict) else None
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("probe_error_count") != 0
        or set(snapshot.get("coverage", ())) != REQUIRED_COVERAGE
        or not isinstance(assessment, dict)
        or assessment.get("go") is not True
        or assessment.get("reasons") not in ([], ())
        or not isinstance(assessment.get("domain_owners"), dict)
    ):
        raise IsolatedLiveEvidenceBlocked("ownership snapshot is incomplete or unsafe")
    return snapshot


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _ime_metrics(
    event: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any],
    release_id: str,
    started: datetime,
    finished: datetime,
) -> dict[str, Any]:
    detail = event.get("detail")
    receipt = detail.get("receipt") if isinstance(detail, dict) else None
    evidence = receipt.get("evidence") if isinstance(receipt, dict) else None
    command = receipt.get("command_receipt") if isinstance(receipt, dict) else None
    host_cleanup = command.get("host_cleanup") if isinstance(command, dict) else None
    launcher_sha = inventory.get("bin/magi-v3-python")
    source_sha = inventory.get("scripts/v3_validation/ime_candidate_probe.py")
    if (
        not isinstance(receipt, dict)
        or not isinstance(evidence, dict)
        or not isinstance(command, dict)
        or receipt.get("candidate_release_id") != release_id
        or receipt.get("candidate_launcher_verified") is not True
        or receipt.get("candidate_probe_source_verified") is not True
        or receipt.get("launcher_sha256") != launcher_sha
        or receipt.get("probe_source_sha256") != source_sha
        or command.get("schema_version") != 1
        or command.get("returncode") != 0
        or command.get("timed_out") is not False
        or command.get("launcher_sha256") != launcher_sha
        or command.get("probe_source_sha256") != source_sha
        or not isinstance(host_cleanup, dict)
        or host_cleanup.get("input_source_restored") is not True
        or host_cleanup.get("frontmost_application_restored") is not True
        or host_cleanup.get("textedit_document_baseline_restored") is not True
        or host_cleanup.get("errors") != []
        or not isinstance(receipt.get("evidence_sha256"), str)
        or receipt["evidence_sha256"]
        != hashlib.sha256(_canonical_json_bytes(evidence)).hexdigest()
    ):
        raise IsolatedLiveEvidenceBlocked("native IME command/candidate binding is invalid")
    command_started = _time(command.get("started_at"), "native IME command started_at")
    command_finished = _time(command.get("finished_at"), "native IME command finished_at")
    if command_started < started or command_finished < command_started or command_finished > finished:
        raise IsolatedLiveEvidenceBlocked("native IME command timestamps escape the LIVE run")
    observations = evidence.get("observations")
    measurements = evidence.get("measurements")
    if (
        evidence.get("schema_version") != 1
        or evidence.get("workload") != "ime_candidate_window_pressure_probe"
        or evidence.get("probe") != "native_mcbopomofo_candidate_window_pressure"
        or evidence.get("status") != "passed"
        or not isinstance(observations, list)
        or not observations
        or not isinstance(measurements, dict)
        or evidence.get("network_access_performed") is not False
        or evidence.get("service_start_performed") is not False
        or evidence.get("production_port_access_performed") is not False
        or evidence.get("launchctl_performed") is not False
        or evidence.get("external_write_performed") is not False
        or evidence.get("live_magi_state_access_performed") is not False
        or evidence.get("temporary_native_ui_performed") is not True
        or evidence.get("unsaved_document_cleanup_performed") is not True
        or evidence.get("unsaved_documents_remaining") != 0
        or evidence.get("input_source_restored") is not True
        or evidence.get("frontmost_application_restored") is not True
        or evidence.get("textedit_state_restored") is not True
    ):
        raise IsolatedLiveEvidenceBlocked("native IME raw observation safety proof is incomplete")
    failures = 0
    for cycle, observation in enumerate(observations, start=1):
        if not isinstance(observation, dict) or observation.get("cycle") != cycle:
            raise IsolatedLiveEvidenceBlocked("native IME cycles are unordered")
        before = observation.get("preexisting_window_ids")
        observed = observation.get("observed_candidate_windows")
        declared_new = observation.get("new_candidate_windows")
        if (
            not isinstance(before, list)
            or any(type(item) is not int or item <= 0 for item in before)
            or len(before) != len(set(before))
            or observation.get("preexisting_window_count") != len(before)
            or not isinstance(observed, list)
            or not isinstance(declared_new, list)
            or observation.get("window_count") != len(observed)
        ):
            raise IsolatedLiveEvidenceBlocked("native IME window inventories are malformed")
        ids: list[int] = []
        for window in observed:
            if (
                not isinstance(window, dict)
                or set(window) != {"owner", "window_id", "layer", "width", "height"}
                or window.get("owner") != "mcbopomofo"
                or any(
                    type(window.get(field)) is not int or window[field] <= 0
                    for field in ("window_id", "layer", "width", "height")
                )
            ):
                raise IsolatedLiveEvidenceBlocked("native IME candidate window geometry is invalid")
            ids.append(window["window_id"])
        if len(ids) != len(set(ids)):
            raise IsolatedLiveEvidenceBlocked("native IME candidate window ids are duplicated")
        recomputed_new = [
            window for window in observed if window["window_id"] not in set(before)
        ]
        detected = bool(recomputed_new)
        if declared_new != recomputed_new or observation.get("detected") is not detected:
            raise IsolatedLiveEvidenceBlocked("native IME new-window delta was not recomputable")
        failures += 0 if detected else 1
    pressure_mb = measurements.get("pressure_allocated_mb")
    pressure_bytes = measurements.get("pressure_touched_bytes")
    if (
        measurements.get("cycles_requested") != len(observations)
        or measurements.get("cycles_completed") != len(observations)
        or measurements.get("candidate_windows_detected") != len(observations) - failures
        or measurements.get("candidate_window_failures") != failures
        or type(pressure_mb) is not int
        or not 128 <= pressure_mb <= 1024
        or type(pressure_bytes) is not int
        or pressure_bytes != pressure_mb * 1024 * 1024
        or measurements.get("text_services_healthy") is not True
    ):
        raise IsolatedLiveEvidenceBlocked("native IME aggregate differs from raw cycles")
    return {
        "observations": len(observations),
        "failures": failures,
        "memory_pressure_exercised": pressure_bytes > 0,
        "candidate_window_healthy": failures == 0,
    }


def _analyze_report(
    *,
    run: RawRun,
    expected_context: Mapping[str, str],
    campaign_config_sha256: str,
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(run.plan_sha256) or _sha256(run.plan_path) != run.plan_sha256:
        raise IsolatedLiveEvidenceBlocked("isolated LIVE plan SHA-256 mismatch")
    try:
        plan = load_isolated_live_plan(run.plan_path, run.plan_sha256)
        deployment = verify_static_plan(plan)
    except IsolatedLiveBlocked as exc:
        raise IsolatedLiveEvidenceBlocked(f"isolated LIVE static plan is invalid: {exc}") from exc
    report = _json(run.report_path, "isolated LIVE execution report")
    if (
        report.get("schema_version") != 1
        or report.get("report_kind") != "isolated_live_validation_execution"
        or report.get("status") != "validation_passed_v2_restored"
        or report.get("ok") is not True
        or report.get("final_cutover") is not False
        or report.get("mutation_performed") is not True
        or report.get("v2_restored") is not True
        or report.get("primary_error") != ""
        or report.get("rollback_detail") != ""
        or report.get("deployment_mode") != "isolated_live_validation"
        or report.get("plan_id") != plan.plan_id
    ):
        raise IsolatedLiveEvidenceBlocked("isolated LIVE execution did not pass and restore V2 cleanly")
    hash_context = report.get("hash_context")
    expected_hashes = {
        "plan_sha256": run.plan_sha256,
        "release_manifest_sha256": plan.release_manifest.sha256,
        "deploy_manifest_sha256": plan.deploy_manifest.sha256,
        "deploy_prepared_marker_sha256": plan.deploy_prepared_marker.sha256,
        "offline_gate_report_sha256": plan.offline_gate_report.sha256,
        "service_manifest_sha256": deployment.service_manifest.sha256,
    }
    if hash_context != expected_hashes:
        raise IsolatedLiveEvidenceBlocked("isolated LIVE report hash context drifted")
    offline = _json(plan.offline_gate_report.path, "offline machine gate")
    if any(offline.get(field) != expected for field, expected in expected_context.items()):
        raise IsolatedLiveEvidenceBlocked("offline machine gate release context drifted")

    release = _json(plan.release_manifest.path, "release manifest")
    inventory = {
        row.get("path"): row.get("sha256")
        for row in release.get("files", ())
        if isinstance(row, dict)
    }
    if inventory.get("config/v3_validation_campaign.json") != campaign_config_sha256:
        raise IsolatedLiveEvidenceBlocked("validation campaign policy is not release-bound")

    started = _time(report.get("started_at"), "LIVE report started_at")
    finished = _time(report.get("finished_at"), "LIVE report finished_at")
    if finished < started:
        raise IsolatedLiveEvidenceBlocked("isolated LIVE report finished before it started")
    events = report.get("events")
    if not isinstance(events, list) or not events:
        raise IsolatedLiveEvidenceBlocked("isolated LIVE report has no execution trace")
    actions: list[str] = []
    snapshots: list[dict[str, Any]] = []
    previous = started
    for index, event in enumerate(events, start=1):
        if (
            not isinstance(event, dict)
            or event.get("sequence") != index
            or event.get("ok") is not True
            or not isinstance(event.get("action"), str)
        ):
            raise IsolatedLiveEvidenceBlocked("LIVE execution trace is unordered or contains a failure")
        observed = _time(event.get("at"), f"LIVE event {index} timestamp")
        if observed < previous or observed > finished:
            raise IsolatedLiveEvidenceBlocked("LIVE execution trace timestamps are inconsistent")
        previous = observed
        actions.append(event["action"])
        if event["action"] == "ownership_snapshot":
            snapshots.append(_snapshot(event))
    ime_indexes = [
        index
        for index, event in enumerate(events)
        if event.get("action") == "native_ime_candidate_window_probe"
    ]
    if len(ime_indexes) != 1:
        raise IsolatedLiveEvidenceBlocked(
            "LIVE execution must contain exactly one native IME observation"
        )
    ime_index = ime_indexes[0]
    previous_ownership = next(
        (
            event
            for event in reversed(events[:ime_index])
            if event.get("action") == "ownership_snapshot"
        ),
        None,
    )
    next_ownership = next(
        (
            event
            for event in events[ime_index + 1 :]
            if event.get("action") == "ownership_snapshot"
        ),
        None,
    )
    # The executor deliberately re-checks the maintenance window between the
    # pre-probe ownership snapshot and the physical IME observation.  Require
    # the nearest ownership proofs on both sides to be V3-only; immediate
    # adjacency would reject the executor's own valid trace.
    if (
        previous_ownership is None
        or next_ownership is None
        or _snapshot(previous_ownership).get("expected") != "v3"
        or _snapshot(next_ownership).get("expected") != "v3"
    ):
        raise IsolatedLiveEvidenceBlocked(
            "native IME observation is not enclosed by V3-only ownership proofs"
        )
    ime = _ime_metrics(
        events[ime_index],
        inventory=inventory,
        release_id=deployment.release_id,
        started=started,
        finished=finished,
    )
    required_actions = (
        "verify_static_artifacts",
        "ownership_snapshot",
        "consume_one_time_token",
        "activate_maintenance_blackout",
        "stop_v2",
        "ownership_snapshot",
        "ownership_snapshot",
        "install_validation",
        "start_v3_role",
        "start_v3_role",
        "start_v3_role",
        "ownership_snapshot",
        "ownership_snapshot",
        "native_ime_candidate_window_probe",
        "ownership_snapshot",
        "stop_v3_role",
        "stop_v3_role",
        "stop_v3_role",
        "ownership_snapshot",
        "remove_validation",
        "restore_v2",
        "ownership_snapshot",
        "verify_v2_readiness_integrity",
        "deactivate_maintenance_blackout",
    )
    if not _subsequence(actions, required_actions):
        raise IsolatedLiveEvidenceBlocked("LIVE execution trace omits the single-active handoff sequence")
    expected_states = [row.get("expected") for row in snapshots]
    if not _subsequence(expected_states, ("v2", "zero", "zero", "v3", "zero", "v2")):
        raise IsolatedLiveEvidenceBlocked("ownership trace omits a required V2/zero/V3/zero/V2 proof")
    zero_rows = [row for row in snapshots if row.get("expected") == "zero"]
    v3_rows = [row for row in snapshots if row.get("expected") == "v3"]
    if len(zero_rows) < 3 or len(v3_rows) != 3 or any(row.get("owner_count") != 0 for row in zero_rows):
        raise IsolatedLiveEvidenceBlocked("zero-owner proof is incomplete")
    if report.get("probes_completed") != report.get("probes_planned") or report.get(
        "probes_planned"
    ) != len(plan.probes):
        raise IsolatedLiveEvidenceBlocked("not every planned read-only LIVE probe completed")

    domains = v3_rows[0]["assessment"]["domain_owners"]
    single_active_violations = sum(
        1
        for row in snapshots
        if len(row["assessment"].get("active_releases", ())) > 1
        or any(len(owners) > 1 for owners in row["assessment"]["domain_owners"].values())
    )
    consumer_domains = ("discord_consumer", "file_watcher", "notification_sender")
    return {
        "plan_id": plan.plan_id,
        "plan_sha256": run.plan_sha256,
        "report_id": report.get("report_id"),
        "report_sha256": _sha256(run.report_path),
        "release_manifest_sha256": plan.release_manifest.sha256,
        "deploy_manifest_sha256": plan.deploy_manifest.sha256,
        "started_at": started,
        "finished_at": finished,
        "single_active_violations": single_active_violations,
        "scheduler_owners": len(domains.get("scheduler", ())),
        "consumer_owners": max((len(domains.get(name, ())) for name in consumer_domains), default=0),
        "writer_conflicts": max(0, len(domains.get("writer", ())) - 1),
        "ime_observations": ime["observations"],
        "ime_failures": ime["failures"],
        "ime_memory_pressure_exercised": ime["memory_pressure_exercised"],
        "ime_candidate_window_healthy": ime["candidate_window_healthy"],
    }


def derive_isolated_live_metrics(
    *,
    offline_campaign_day: Mapping[str, Any],
    campaign_config: Mapping[str, Any],
    campaign_config_sha256: str,
    runs: Sequence[RawRun],
    expected_context: Mapping[str, str],
    gate_config: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Authoritatively recompute all four pre-cutover LIVE evidence metrics."""

    if set(expected_context) != {
        "campaign_id",
        "release_sha",
        "hardware_id",
        "gate_config_sha256",
    } or any(not isinstance(value, str) or not value for value in expected_context.values()):
        raise IsolatedLiveEvidenceBlocked("expected release context is incomplete")
    reset_minutes, policy_window_hours, policy_offline_passes = _campaign_policy(campaign_config)
    required_runs = _threshold(gate_config, "minimum_isolated_live_validation_runs")
    required_offline = _threshold(gate_config, "offline_replay_independent_passes")
    max_window = _threshold(gate_config, "pre_cutover_validation_window_hours")
    if (
        required_runs != 3
        or required_offline != policy_offline_passes
        or max_window != policy_window_hours
        or len(runs) != required_runs
    ):
        raise IsolatedLiveEvidenceBlocked("LIVE/offline policy and cutover thresholds disagree")
    if (
        offline_campaign_day.get("schema_version") != 1
        or offline_campaign_day.get("status") != "offline_passed"
        or offline_campaign_day.get("evidence_class")
        != "immutable_release_offline_campaign"
        or offline_campaign_day.get("execution_backend") != "release_launcher"
        or offline_campaign_day.get("certifying") is not True
        or offline_campaign_day.get("release_gate_eligible") is not True
        or offline_campaign_day.get("live_execution_performed") is not False
        or offline_campaign_day.get("campaign_config_sha256") != campaign_config_sha256
        or any(
            offline_campaign_day.get(field) != expected
            for field, expected in expected_context.items()
        )
        or offline_campaign_day.get("required_independent_passes") != required_offline
        or offline_campaign_day.get("completed_independent_passes") != required_offline
    ):
        raise IsolatedLiveEvidenceBlocked("offline campaign day is not an exact context-bound pass")
    workloads = offline_campaign_day.get("workloads")
    if not isinstance(workloads, list):
        raise IsolatedLiveEvidenceBlocked("offline campaign day workloads are missing")
    passes = {
        row.get("validation_pass")
        for row in workloads
        if isinstance(row, dict)
        and row.get("status") == "offline_passed"
        and type(row.get("returncode")) is int
        and row.get("returncode") == 0
        and type(row.get("validation_pass")) is int
    }
    profiles = {
        row["validation_profile"].get("profile_id")
        for row in workloads
        if isinstance(row, dict) and isinstance(row.get("validation_profile"), dict)
    }
    runtime_hashes = (
        offline_campaign_day.get("python_runtime_sha256"),
        offline_campaign_day.get("python_runtime_manifest_sha256"),
        offline_campaign_day.get("python_runtime_tree_sha256"),
    )
    if (
        passes != set(range(1, required_offline + 1))
        or len(profiles) != required_offline
        or None in profiles
        or any(not isinstance(value, str) or not SHA256_RE.fullmatch(value) for value in runtime_hashes)
    ):
        raise IsolatedLiveEvidenceBlocked("offline campaign lacks seven distinct passing profiles")

    analyzed = [
        _analyze_report(
            run=run,
            expected_context=expected_context,
            campaign_config_sha256=campaign_config_sha256,
        )
        for run in runs
    ]
    for field in ("plan_id", "plan_sha256", "report_id", "report_sha256"):
        values = [row[field] for row in analyzed]
        if any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
            raise IsolatedLiveEvidenceBlocked(f"three LIVE runs do not have independent {field} values")
    for field in ("release_manifest_sha256", "deploy_manifest_sha256"):
        if len({row[field] for row in analyzed}) != 1:
            raise IsolatedLiveEvidenceBlocked(f"three LIVE runs do not share one {field}")
    if offline_campaign_day.get("release_manifest_sha256") != analyzed[0][
        "release_manifest_sha256"
    ]:
        raise IsolatedLiveEvidenceBlocked(
            "offline campaign day is not bound to the LIVE candidate manifest"
        )
    analyzed.sort(key=lambda row: row["started_at"])
    for previous, current in zip(analyzed, analyzed[1:]):
        if current["started_at"] - previous["finished_at"] < timedelta(minutes=reset_minutes):
            raise IsolatedLiveEvidenceBlocked("LIVE runs are not reset-separated")
    started = analyzed[0]["started_at"]
    finished = max(row["finished_at"] for row in analyzed)
    window_hours = (finished - started).total_seconds() / 3600.0
    if window_hours > max_window:
        raise IsolatedLiveEvidenceBlocked("LIVE runs exceed the allowed validation window")
    violations = sum(row["single_active_violations"] for row in analyzed)
    metrics = {
        "offline_replay_and_isolated_live_validation_satisfied": {
            "offline_independent_passes": required_offline,
            "isolated_live_runs": len(analyzed),
            "all_runs_passed": True,
            "validation_window_hours": window_hours,
        },
        "isolated_live_validation_single_active_handoff_verified": {
            "runs": len(analyzed),
            "single_active_violations": violations,
            "all_runs_passed": True,
        },
        "v2_fully_stopped_before_v3_start_verified": {
            "active_v2_processes": 0,
            "owned_ports": 0,
            "scheduler_owners": 0,
            "writer_owners": 0,
            "model_owners": 0,
        },
        "single_scheduler_consumer_writer_ownership_verified": {
            "scheduler_owners": max(row["scheduler_owners"] for row in analyzed),
            "consumer_owners": max(row["consumer_owners"] for row in analyzed),
            "writer_conflicts": max(row["writer_conflicts"] for row in analyzed),
            "ownership_verified": violations == 0,
        },
        "single_active_handoff_test_passed": {
            "single_active_violations": violations,
            "v2_v3_concurrent": violations > 0,
            "handoff_passed": violations == 0,
        },
        "v3_fully_stopped_before_v2_rollback_verified": {
            "active_v3_processes": 0,
            "owned_ports": 0,
            "scheduler_owners": 0,
            "writer_owners": 0,
            "model_owners": 0,
        },
        "input_method_candidate_window_probe_passed": {
            "observations": sum(row["ime_observations"] for row in analyzed),
            "failures": sum(row["ime_failures"] for row in analyzed),
            "memory_pressure_exercised": all(
                row["ime_memory_pressure_exercised"] for row in analyzed
            ),
            "candidate_window_healthy": all(
                row["ime_candidate_window_healthy"] for row in analyzed
            ),
        },
    }
    return metrics


def compile_isolated_live_evidence(
    *,
    output: Path,
    offline_campaign_day: Path,
    campaign_config: Path,
    runs: Sequence[RawRun],
    campaign_id: str,
    release_sha: str,
    hardware_id: str,
    gate_config_sha256: str,
    gate_config: Path,
) -> dict[str, str]:
    """Verify raw artifacts and emit four trusted-normalizer envelopes."""

    from scripts.v3_evidence_compiler import CompileContext, SourceArtifact, _emit

    context = CompileContext(campaign_id, release_sha, hardware_id, gate_config_sha256)
    context.validate()
    config_payload = _json(campaign_config, "validation campaign config")
    gate_payload = _json(gate_config, "cutover gate config")
    if _sha256(gate_config) != gate_config_sha256:
        raise IsolatedLiveEvidenceBlocked("cutover gate config SHA-256 mismatch")
    day_payload = _json(offline_campaign_day, "offline campaign day")
    metrics = derive_isolated_live_metrics(
        offline_campaign_day=day_payload,
        campaign_config=config_payload,
        campaign_config_sha256=_sha256(campaign_config),
        runs=runs,
        expected_context=context.as_dict(),
        gate_config=gate_payload,
    )
    started = min(_time(_json(run.report_path, "LIVE report").get("started_at"), "started_at") for run in runs)
    completed = max(_time(_json(run.report_path, "LIVE report").get("finished_at"), "finished_at") for run in runs)
    sources = [
        SourceArtifact("upstream_validation_campaign_config", campaign_config),
        SourceArtifact("upstream_offline_campaign_day", offline_campaign_day),
    ]
    for index, run in enumerate(runs, start=1):
        sources.extend(
            [
                SourceArtifact(f"upstream_isolated_live_plan_{index}", run.plan_path),
                SourceArtifact(f"upstream_isolated_live_report_{index}", run.report_path),
            ]
        )
    emitted: dict[str, str] = {}
    for evidence_id in LIVE_EVIDENCE_IDS:
        emitted[evidence_id] = _emit(
            output=output,
            evidence_id=evidence_id,
            context=context,
            config=gate_payload,
            metrics=metrics[evidence_id],
            sources=sources,
            started_at=started,
            completed_at=completed,
        )
    return emitted


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline-campaign-day", type=Path, required=True)
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--run", action="append", nargs=3, metavar=("PLAN", "PLAN_SHA256", "REPORT"), required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config", type=Path, required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runs = tuple(RawRun(Path(plan), digest, Path(report)) for plan, digest, report in args.run)
    try:
        emitted = compile_isolated_live_evidence(
            output=args.output,
            offline_campaign_day=args.offline_campaign_day,
            campaign_config=args.campaign_config,
            runs=runs,
            campaign_id=args.campaign_id,
            release_sha=args.release_sha,
            hardware_id=args.hardware_id,
            gate_config_sha256=args.gate_config_sha256,
            gate_config=args.gate_config,
        )
    except (IsolatedLiveEvidenceBlocked, IsolatedLiveBlocked) as exc:
        raise SystemExit(f"isolated LIVE evidence blocked: {exc}") from exc
    print(json.dumps({"status": "compiled", "emitted": emitted}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LIVE_EVIDENCE_IDS",
    "IsolatedLiveEvidenceBlocked",
    "RawRun",
    "compile_isolated_live_evidence",
    "derive_isolated_live_metrics",
]
