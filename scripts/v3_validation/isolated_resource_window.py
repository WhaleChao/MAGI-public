#!/usr/bin/env python3
"""Fail-closed verification for the V2-stopped resource/Metal window.

macOS does not expose a documented, non-privileged per-process Metal
allocation-byte counter.  This verifier therefore accepts system-wide AGX
bytes only when the measurement window proves exclusive attribution: V2 is
fully stopped, no non-candidate user Metal owner exists, a stable negative
control precedes the candidate, and the post-exit value returns to that
control baseline.  System-wide bytes are never relabelled as per-process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from magi_v3.external_inputs import (
    NAMED_MUTABLE_STATE_BINDINGS,
    named_mutable_state_paths,
)

SCHEMA = "magi.v3.isolated-resource-window/v1"
ATTRIBUTION_METHOD = "ioreg_agx_plus_powermetrics_per_process_gpu_v2_stopped_window"
AGX_SOURCE = "/usr/sbin/ioreg AGXAccelerator PerformanceStatistics In use system memory"
PER_PROCESS_GPU_SOURCE = "/usr/bin/powermetrics --show-process-gpu"
RAW_SOURCE_COMMANDS = {
    "ps": "/bin/ps",
    "lsof": "/usr/sbin/lsof",
    "ioreg": "/usr/sbin/ioreg",
    "powermetrics": "/usr/bin/powermetrics",
}
REQUIRED_STOPPED_LABELS = (
    "com.magi.daemon",
    "com.magi.omlx",
    "com.magi.omlx-embed",
    "com.magi.omlx-phi4",
    "com.magi.omlx-smol",
    "com.magi.mlx-mtp",
    "com.magi.omlx-nemotron-parse",
)
REQUIRED_OBSERVED_PORTS = (5002, 5003, 5014, 8080, 8081, 8088, 18080)
SHA256_FIELDS = (
    "release_manifest_sha256",
    "release_snapshot_sha256",
    "python_runtime_sha256",
    "resource_policy_sha256",
    "model_tree_sha256",
    "model_backend_sha256",
    "prompt_sha256",
    "sandbox_profile_sha256",
)


class IsolatedResourceWindowError(ValueError):
    pass


def parse_powermetrics_process_gpu(data: bytes) -> tuple[dict[str, int], ...]:
    documents: list[Any] = []
    for chunk in data.split(b"\0"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            documents.append(plistlib.loads(chunk))
        except Exception as exc:
            raise IsolatedResourceWindowError(
                "powermetrics plist output is malformed"
            ) from exc
    if not documents:
        raise IsolatedResourceWindowError("powermetrics returned no plist samples")
    rows: dict[int, int] = {}
    gpu_field_seen = False

    def walk(value: Any) -> None:
        nonlocal gpu_field_seen
        if isinstance(value, dict):
            pid = value.get("pid", value.get("process_id", value.get("process-id")))
            for key, raw in value.items():
                normalized = re.sub(r"[^a-z]", "", str(key).lower())
                if "gpu" in normalized and "time" in normalized:
                    gpu_field_seen = True
                    if type(pid) is int and pid > 0 and isinstance(raw, (int, float)) and raw >= 0:
                        if isinstance(raw, float) and not raw.is_integer():
                            raise IsolatedResourceWindowError(
                                "per-process GPU time is fractional/ambiguous"
                            )
                        rows[pid] = max(rows.get(pid, 0), int(raw))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for document in documents:
        walk(document)
    if not gpu_field_seen:
        raise IsolatedResourceWindowError(
            "powermetrics omitted per-process GPU fields"
        )
    return tuple({"pid": pid, "gpu_time_ns": rows[pid]} for pid in sorted(rows))


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value <= 0)
    ):
        raise IsolatedResourceWindowError(f"{name} is not a valid measurement")
    return float(value)


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise IsolatedResourceWindowError(f"{name} is not a valid integer")
    return value


def _sha(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IsolatedResourceWindowError(f"{name} is not a SHA-256 digest")
    return value


def _verify_identity(
    report: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    supplied = report.get("evidence_sha256")
    unsigned = dict(report)
    unsigned.pop("evidence_sha256", None)
    if supplied != sha256_json(unsigned):
        raise IsolatedResourceWindowError("isolated window evidence hash mismatch")
    if (
        report.get("schema") != SCHEMA
        or report.get("status") != "passed"
        or report.get("mode") != "v2_fully_stopped_isolated_window"
    ):
        raise IsolatedResourceWindowError("isolated window identity is invalid")
    binding = report.get("release_binding")
    execution = report.get("execution_binding")
    thresholds = report.get("thresholds")
    policy = report.get("policy_binding")
    if (
        not isinstance(binding, dict)
        or not isinstance(execution, dict)
        or not isinstance(thresholds, dict)
        or not isinstance(policy, dict)
    ):
        raise IsolatedResourceWindowError("release binding/policy/thresholds are missing")
    release_id = binding.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise IsolatedResourceWindowError("release_id is missing")
    for field in SHA256_FIELDS:
        _sha(binding.get(field), field)
    for field in (
        "plan_sha256",
        "approval_token_sha256",
        "collector_source_sha256",
        "owned_workdir_marker_sha256",
        "token_consumption_receipt_sha256",
        "provisional_gate_sha256",
        "outer_plan_semantic_sha256",
    ):
        _sha(execution.get(field), field)
    _sha(execution.get("outer_plan_sha256"), "outer isolated-LIVE plan")
    if (
        execution.get("plan_consumed_once") is not True
        or execution.get("outer_executor")
        != "scripts.v3_validation.isolated_live_execute"
        or execution.get("outer_executor_phase")
        != "resource_window_after_v2_zero_owner"
        or execution.get("v2_restore_owner")
        != "outer_isolated_live_executor_finally"
        or execution.get("provisional_gate_status") != "provisional_16_of_19_passed"
        or execution.get("provisional_gate_counts")
        != {"required": 16, "passed": 16, "failed": 0, "missing": 0, "invalid": 0}
        or execution.get("formal_live_eligible_before_window") is not False
        or execution.get("observed_listener_ports") != list(REQUIRED_OBSERVED_PORTS)
    ):
        raise IsolatedResourceWindowError("immutable resource-window plan was not consumed once")
    consumption = execution.get("token_consumption_receipt")
    if not isinstance(consumption, dict):
        raise IsolatedResourceWindowError("raw one-time consumption receipt is missing")
    consumption_bytes = (
        json.dumps(consumption, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if (
        hashlib.sha256(consumption_bytes).hexdigest()
        != execution["token_consumption_receipt_sha256"]
        or consumption.get("schema")
        != "magi.v3.resource-window-plan-consumption/v1"
        or consumption.get("plan_sha256") != execution["plan_sha256"]
        or consumption.get("approval_token_sha256")
        != execution["approval_token_sha256"]
        or consumption.get("outer_plan_sha256") != execution["outer_plan_sha256"]
        or consumption.get("outer_plan_semantic_sha256")
        != execution["outer_plan_semantic_sha256"]
        or consumption.get("provisional_gate_sha256")
        != execution["provisional_gate_sha256"]
        or type(consumption.get("consumer_pid")) is not int
        or consumption["consumer_pid"] <= 0
        or type(consumption.get("consumed_monotonic_ns")) is not int
        or consumption["consumed_monotonic_ns"] <= 0
    ):
        raise IsolatedResourceWindowError("one-time consumption receipt is invalid")
    composition_receipt = execution.get("production_composition_receipt")
    external = report.get("external_inputs")
    receipt_runtime_raw = (
        composition_receipt.get("runtime_root")
        if isinstance(composition_receipt, dict)
        else None
    )
    receipt_runtime_path = (
        Path(receipt_runtime_raw) if isinstance(receipt_runtime_raw, str) else Path()
    )
    receipt_runtime = (
        receipt_runtime_path.resolve(strict=False)
        if isinstance(receipt_runtime_raw, str)
        else Path()
    )
    expected_named_paths = named_mutable_state_paths(receipt_runtime)
    expected_named_environment = {
        env_name: expected_named_paths[binding_name]
        for env_name, (binding_name, _relative) in NAMED_MUTABLE_STATE_BINDINGS.items()
    }
    if (
        not isinstance(composition_receipt, dict)
        or not isinstance(external, dict)
            or set(external)
            != {
                "website_root",
                "website_admin_sha256",
                "laf_config_file",
                "laf_config_sha256",
                "laf_config_mode",
                "google_credentials_file",
                "google_credentials_sha256",
                "google_credentials_mode",
                "google_calendar_token_source_file",
                "google_calendar_token_source_sha256",
                "laf_gmail_token_source_file",
                "laf_gmail_token_source_sha256",
                "file_review_token_source_file",
                "file_review_token_source_sha256",
            }
        or composition_receipt.get("website_root") != external.get("website_root")
        or composition_receipt.get("website_admin_sha256")
        != external.get("website_admin_sha256")
        or not isinstance(external.get("website_root"), str)
        or not Path(external["website_root"]).is_absolute()
        or _sha(external.get("website_admin_sha256"), "Website Admin source")
        != external.get("website_admin_sha256")
        or composition_receipt.get("schema")
        != "magi.v3.resource-window-production-environment/v1"
        or composition_receipt.get("receipt_sha256")
        != sha256_json(
            {
                key: value
                for key, value in composition_receipt.items()
                if key != "receipt_sha256"
            }
        )
        or composition_receipt.get("release_id") != binding["release_id"]
            or not isinstance(receipt_runtime_raw, str)
            or not receipt_runtime_path.is_absolute()
            or receipt_runtime_path != receipt_runtime
            or composition_receipt.get("named_mutable_state_bindings")
            != expected_named_environment
            or composition_receipt.get("python_runtime_sha256")
            != binding["python_runtime_sha256"]
            or external.get("laf_config_mode") != "0600"
            or external.get("google_credentials_mode") != "0600"
            or any(
                _sha(external.get(name), name) != external.get(name)
                for name in (
                    "laf_config_sha256",
                    "google_credentials_sha256",
                    "google_calendar_token_source_sha256",
                    "laf_gmail_token_source_sha256",
                    "file_review_token_source_sha256",
                )
            )
            or not isinstance(composition_receipt.get("mutable_token_handoff"), list)
            or len(composition_receipt["mutable_token_handoff"]) != 3
            or any(
                not isinstance(row, dict)
                or row.get("source_sha256") != row.get("target_sha256")
                for row in composition_receipt["mutable_token_handoff"]
            )
        ):
        raise IsolatedResourceWindowError("production composition receipt is invalid")
    outer_owner = execution.get("outer_owner_contract")
    if (
        not isinstance(outer_owner, dict)
        or outer_owner.get("required_stopped_launchd_labels")
        != list(REQUIRED_STOPPED_LABELS)
        or outer_owner.get("zero_owner_snapshot_required_coverage")
        != ["launchd", "ownership", "pidfile", "port", "process"]
        or outer_owner.get("outer_must_capture_initial_label_state") is not True
        or outer_owner.get("outer_finally_restore_initial_label_state_exactly") is not True
        or outer_owner.get("restore_proof_owner")
        != "outer_isolated_live_executor_finally"
    ):
        raise IsolatedResourceWindowError("outer stop/restore contract is missing")
    raw_policy = policy.get("policy_raw_json")
    resolved = policy.get("resolved_thresholds")
    if not isinstance(raw_policy, str) or not isinstance(resolved, dict):
        raise IsolatedResourceWindowError("raw resource policy binding is missing")
    if hashlib.sha256(raw_policy.encode()).hexdigest() != binding["resource_policy_sha256"]:
        raise IsolatedResourceWindowError("raw resource policy hash mismatch")
    try:
        parsed_policy = json.loads(raw_policy)
    except json.JSONDecodeError as exc:
        raise IsolatedResourceWindowError("raw resource policy is invalid JSON") from exc
    authoritative = parsed_policy.get("authoritative_resource_window")
    if (
        not isinstance(authoritative, dict)
        or authoritative.get("schema")
        != "magi.v3.authoritative-resource-window-policy/v1"
        or authoritative.get("per_process_gpu_permission_required") is not True
        or authoritative.get("seatbelt_network_and_live_state_denial_required") is not True
        or authoritative.get("production_composition_required") is not True
        or authoritative.get("shared_direct_model_backend_forbidden") is not True
        or authoritative.get("one_time_plan_consumption_required") is not True
        or policy.get("resolved_thresholds_sha256") != sha256_json(resolved)
        or dict(thresholds) != dict(resolved)
    ):
        raise IsolatedResourceWindowError("policy-derived thresholds are not authoritative")
    required_pairs = {
        "minimum_model_tokens_per_second_ratio": authoritative.get(
            "minimum_model_tokens_per_second_ratio"
        ),
        "minimum_application_plane_footprint_reduction_ratio": authoritative.get(
            "minimum_application_plane_footprint_reduction_ratio"
        ),
        "agx_drift_tolerance_bytes": authoritative.get("agx_drift_tolerance_bytes"),
    }
    if any(resolved.get(key) != value for key, value in required_pairs.items()):
        raise IsolatedResourceWindowError("plan thresholds drifted from raw policy")
    return binding, thresholds, policy


def _verify_preflight(report: Mapping[str, Any]) -> None:
    preflight = report.get("preflight")
    if not isinstance(preflight, dict):
        raise IsolatedResourceWindowError("isolated window preflight is missing")
    empty_fields = (
        "v2_owner_pids",
        "v3_owner_pids_before_start",
        "production_port_owner_pids",
        "noncandidate_user_metal_processes",
    )
    if (
        preflight.get("v2_fully_stopped") is not True
        or preflight.get("candidate_not_started_at_baseline") is not True
        or preflight.get("production_ingress_quiesced") is not True
        or any(preflight.get(field) != [] for field in empty_fields)
        or not isinstance(preflight.get("process_inventory_sha256"), str)
        or preflight.get("per_process_gpu_permission") is not True
        or preflight.get("raw_source_coverage") != sorted(RAW_SOURCE_COMMANDS)
        or preflight.get("required_stopped_launchd_labels")
        != list(REQUIRED_STOPPED_LABELS)
        or not isinstance(preflight.get("stopped_launchd_states"), list)
        or len(preflight["stopped_launchd_states"]) != len(REQUIRED_STOPPED_LABELS)
        or any(
            not isinstance(row, dict)
            or row.get("label") != REQUIRED_STOPPED_LABELS[index]
            or row.get("loaded") is not False
            or type(row.get("returncode")) is not int
            or row["returncode"] == 0
            for index, row in enumerate(preflight.get("stopped_launchd_states", []))
        )
    ):
        raise IsolatedResourceWindowError(
            "V2/port/Metal exclusivity preflight did not pass"
        )
    _sha(preflight["process_inventory_sha256"], "preflight process inventory")
    raw_sources = preflight.get("raw_sources")
    if not isinstance(raw_sources, dict):
        raise IsolatedResourceWindowError("preflight raw host sources are missing")
    for source in RAW_SOURCE_COMMANDS:
        _raw_source(raw_sources, source)
    if preflight["process_inventory_sha256"] != raw_sources["ps"]["stdout_sha256"]:
        raise IsolatedResourceWindowError("preflight process inventory is self-asserted")


def _raw_source(row: Mapping[str, Any], source: str) -> None:
    payload = row.get(source)
    if not isinstance(payload, dict):
        raise IsolatedResourceWindowError(f"raw {source} evidence is missing")
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    argv = payload.get("argv")
    if (
        not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or not isinstance(argv, list)
        or not argv
        or argv[0] != RAW_SOURCE_COMMANDS[source]
        or payload.get("returncode") not in ({0, 1} if source == "lsof" else {0})
        or payload.get("stdout_sha256")
        != hashlib.sha256(stdout.encode()).hexdigest()
        or payload.get("stderr_sha256")
        != hashlib.sha256(stderr.encode()).hexdigest()
    ):
        raise IsolatedResourceWindowError(f"raw {source} evidence is invalid")
    if source == "powermetrics":
        receipt = payload.get("privilege_receipt")
        invoker = payload.get("invoker_argv")
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema") != "magi.v3.fixed-powermetrics-privilege/v1"
            or receipt.get("collector_ran_as_root") is not False
            or receipt.get("collector_euid") in {None, 0}
            or receipt.get("invoker") != "/usr/bin/sudo"
            or receipt.get("noninteractive") is not True
            or not isinstance(invoker, list)
            or invoker[:3] != ["/usr/bin/sudo", "-n", "--"]
            or invoker[3:] != argv
            or receipt.get("fixed_measurement_argv_sha256")
            != hashlib.sha256(
                json.dumps(argv, separators=(",", ":")).encode()
            ).hexdigest()
        ):
            raise IsolatedResourceWindowError(
                "powermetrics privilege was not constrained to the fixed read-only argv"
            )


def _verify_raw_host_samples(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = report.get("raw_host_samples")
    if not isinstance(rows, list) or len(rows) < 190:
        raise IsolatedResourceWindowError("full raw host sample series is missing")
    previous = 0
    phases: set[str] = set()
    execution = report.get("execution_binding")
    if not isinstance(execution, dict):
        raise IsolatedResourceWindowError("execution binding is missing")
    observed_ports = set(execution.get("observed_listener_ports", []))
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise IsolatedResourceWindowError("raw host sample row is invalid")
        timestamp = _integer(row.get("monotonic_ns"), "raw sample monotonic time", minimum=1)
        phase = row.get("phase")
        phases.add(str(phase))
        if (
            row.get("sequence") != index
            or timestamp <= previous
            or phase
            not in {
                "baseline",
                "negative_control",
                "v2_reference",
                "v3_deep_idle",
                "v2_model",
                "v3_model",
                "returned",
            }
            or row.get("live_v2_owner_pids") != []
            or row.get("unexpected_listener_pids") != []
            or row.get("per_process_gpu_permission") is not True
            or not isinstance(row.get("candidate_gpu_processes"), list)
            or not isinstance(row.get("noncandidate_gpu_processes"), list)
        ):
            raise IsolatedResourceWindowError("raw host ownership/GPU sample failed closed")
        previous = timestamp
        for source in RAW_SOURCE_COMMANDS:
            _raw_source(row, source)
        # Raw output must actually contain the evidence identities summarized by the row.
        ps_out = row["ps"]["stdout"]
        lsof_out = row["lsof"]["stdout"]
        ioreg_out = row["ioreg"]["stdout"]
        power_out = row["powermetrics"]["stdout"]
        ps_rows = _parse_raw_ps(ps_out)
        ps_pids = {item["pid"] for item in ps_rows}
        owned_pids = row.get("owned_process_pids")
        if (
            not isinstance(owned_pids, list)
            or any(type(pid) is not int or pid <= 0 for pid in owned_pids)
            or len(owned_pids) != len(set(owned_pids))
            or not set(owned_pids) <= ps_pids
        ):
            raise IsolatedResourceWindowError("owned process inventory is not bound to raw ps")
        raw_v2 = sorted(
            item["pid"]
            for item in ps_rows
            if "Library/Application Support/MAGI/runtime/MAGI_v2" in item["command"]
        )
        raw_listeners = sorted(_parse_raw_lsof(lsof_out, observed_ports))
        if (
            row["live_v2_owner_pids"] != raw_v2
            or row.get("production_listener_pids") != raw_listeners
            or row["unexpected_listener_pids"]
            != sorted(set(raw_listeners) - set(owned_pids))
        ):
            raise IsolatedResourceWindowError("raw ps/lsof ownership summary differs")
        agx_values = [
            int(value)
            for value in re.findall(r'"In use system memory"\s*=\s*(\d+)', ioreg_out)
        ]
        if len(agx_values) != 1 or row.get("system_agx_bytes") != agx_values[0]:
            raise IsolatedResourceWindowError("raw ioreg AGX summary differs")
        parsed_gpu = {
            item["pid"]: item["gpu_time_ns"]
            for item in parse_powermetrics_process_gpu(power_out.encode())
        }
        candidate_gpu = row["candidate_gpu_processes"]
        noncandidate_gpu = row["noncandidate_gpu_processes"]
        if any(
            not isinstance(item, dict)
            or item.get("candidate") is not True
            or item.get("pid") not in set(owned_pids)
            or parsed_gpu.get(item.get("pid")) != item.get("gpu_time_ns")
            for item in candidate_gpu
        ) or any(
            not isinstance(item, dict)
            or item.get("candidate") is not False
            or item.get("pid") in set(owned_pids)
            or item.get("gpu_time_ns", 0) <= 0
            or parsed_gpu.get(item.get("pid")) != item.get("gpu_time_ns")
            for item in noncandidate_gpu
        ):
            raise IsolatedResourceWindowError("raw per-process GPU summary differs")
        candidate_ids = {item["pid"] for item in candidate_gpu}
        noncandidate_ids = {item["pid"] for item in noncandidate_gpu}
        if (
            len(candidate_ids) != len(candidate_gpu)
            or len(noncandidate_ids) != len(noncandidate_gpu)
            or candidate_ids != set(owned_pids).intersection(parsed_gpu)
            or noncandidate_ids
            != {
                pid
                for pid, gpu_time in parsed_gpu.items()
                if pid not in set(owned_pids) and gpu_time > 0
            }
        ):
            raise IsolatedResourceWindowError("raw per-process GPU coverage is incomplete")
        if (
            not ps_out.strip()
            or "In use system memory" not in ioreg_out
            or not power_out.strip()
            or row.get("ps_inventory_sha256")
            != hashlib.sha256(ps_out.encode()).hexdigest()
            or row.get("listener_inventory_sha256")
            != hashlib.sha256(lsof_out.encode()).hexdigest()
            or row.get("ioreg_inventory_sha256")
            != hashlib.sha256(ioreg_out.encode()).hexdigest()
            or row.get("powermetrics_inventory_sha256")
            != hashlib.sha256(power_out.encode()).hexdigest()
        ):
            raise IsolatedResourceWindowError("raw host sample summary is self-asserted")
    required_phases = {
        "baseline",
        "negative_control",
        "v2_reference",
        "v3_deep_idle",
        "v2_model",
        "v3_model",
        "returned",
    }
    if not required_phases <= phases:
        raise IsolatedResourceWindowError("raw host phase coverage is incomplete")
    return rows


def _parse_raw_ps(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([0-9.]+)\s+(.+)$", line)
        if match:
            rows.append(
                {
                    "pid": int(match.group(1)),
                    "uid": int(match.group(2)),
                    "ppid": int(match.group(3)),
                    "pgid": int(match.group(4)),
                    "command": match.group(6),
                }
            )
    if not rows:
        raise IsolatedResourceWindowError("raw ps inventory is empty")
    return rows


def _parse_raw_lsof(text: str, ports: set[int]) -> set[int]:
    current: int | None = None
    result: set[int] = set()
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
        elif line.startswith("n") and current is not None:
            match = re.search(r":(\d+)(?:\s|$)", line[1:])
            if match and int(match.group(1)) in ports:
                result.add(current)
    return result


def _verify_model(
    report: Mapping[str, Any], binding: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> dict[str, Any]:
    model = report.get("model_benchmark")
    if not isinstance(model, dict):
        raise IsolatedResourceWindowError("model benchmark is missing")
    arms = model.get("arms")
    if not isinstance(arms, list) or len(arms) < 6:
        raise IsolatedResourceWindowError("three matched model repeats per arm are required")
    workload = report.get("workload_binding")
    if not isinstance(workload, dict):
        raise IsolatedResourceWindowError("matched workload binding is missing")
    request = workload.get("request")
    composition = workload.get("composition")
    external = report.get("external_inputs")
    if (
        not isinstance(request, dict)
        or not isinstance(composition, dict)
        or workload.get("request_sha256") != sha256_json(request)
        or request.get("corpus_sha256") != binding["prompt_sha256"]
        or request.get("model_tree_sha256") != binding["model_tree_sha256"]
        or request.get("repeats_per_arm") != 3
        or workload.get("same_corpus_model_request_required") is not True
        or composition.get("schema")
        != "magi.v3.resource-window-production-composition/v1"
        or composition.get("arm_transport") != "arm_owned_production_process"
        or composition.get("shared_direct_backend") is not False
        or composition.get("external_inputs") != external
        or composition.get("composition_sha256")
        != sha256_json({key: value for key, value in composition.items() if key != "composition_sha256"})
        or not isinstance(composition.get("v2"), dict)
        or not isinstance(composition.get("v3"), dict)
    ):
        raise IsolatedResourceWindowError(
            "V2/V3 model arms are not exact matched production compositions"
        )
    composition_sha = str(composition["composition_sha256"])
    request_sha = str(workload["request_sha256"])
    http_request_sha = workload.get("http_request_sha256")
    _sha(http_request_sha, "matched HTTP request")
    by_arm: dict[str, list[float]] = {"v2_reference": [], "v3_candidate": []}
    identities: set[tuple[int, int, int]] = set()
    intervals: list[tuple[int, int]] = []
    for row in arms:
        if not isinstance(row, dict) or row.get("arm") not in by_arm:
            raise IsolatedResourceWindowError("model arm identity is invalid")
        tokens = _integer(row.get("generated_tokens"), "generated tokens", minimum=128)
        seconds = _number(row.get("generation_seconds"), "generation seconds", positive=True)
        observed_tps = _number(row.get("tokens_per_second"), "tokens per second", positive=True)
        expected_tps = tokens / seconds
        pid = _integer(row.get("pid"), "model pid", minimum=1)
        pgid = _integer(row.get("pgid"), "model pgid", minimum=1)
        started = _integer(row.get("started_monotonic_ns"), "model start", minimum=1)
        completed = _integer(row.get("completed_monotonic_ns"), "model completion", minimum=1)
        identity = (pid, pgid, _integer(row.get("proc_start_abstime"), "model proc start", minimum=1))
        if (
            abs(observed_tps - expected_tps) > max(1e-9, expected_tps * 1e-9)
            or completed <= started
            or identity in identities
            or row.get("returncode") != 0
            or row.get("timed_out") is not False
            or row.get("process_group_gone") is not True
            or row.get("network_accessed") is not False
            or row.get("production_state_accessed") is not False
            or row.get("prompt_sha256") != binding["prompt_sha256"]
            or row.get("model_tree_sha256") != binding["model_tree_sha256"]
            or row.get("model_backend_sha256") != binding["model_backend_sha256"]
            or row.get("python_runtime_sha256") != binding["python_runtime_sha256"]
            or row.get("request_sha256") != request_sha
            or row.get("http_request_sha256") != http_request_sha
            or not isinstance(row.get("response_sha256"), str)
            or len(row["response_sha256"]) != 64
            or type(row.get("owned_model_server_pid")) is not int
            or row["owned_model_server_pid"] <= 0
            or row.get("composition_sha256") != composition_sha
            or row.get("transport") != "arm_owned_production_process_http"
            or row.get("shared_direct_backend") is not False
            or row.get("seatbelt_network_denied") is not True
            or row.get("seatbelt_live_state_denied") is not True
        ):
            raise IsolatedResourceWindowError("model arm provenance/result is invalid")
        identities.add(identity)
        intervals.append((started, completed))
        by_arm[str(row["arm"])].append(observed_tps)
    intervals.sort()
    if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
        raise IsolatedResourceWindowError("matched model arms overlapped")
    if any(len(values) < 3 for values in by_arm.values()):
        raise IsolatedResourceWindowError("matched model arm coverage is incomplete")
    minimum_ratio = _number(
        thresholds.get("minimum_model_tokens_per_second_ratio"),
        "minimum model TPS ratio",
        positive=True,
    )
    ratio = min(by_arm["v3_candidate"]) / max(by_arm["v2_reference"])
    if (
        model.get("same_model_prompt_backend_runtime") is not True
        or model.get("same_corpus_model_request") is not True
        or model.get("separate_arm_owned_production_compositions") is not True
        or model.get("shared_direct_backend") is not False
        or model.get("maximum_simultaneous_arms") != 1
        or model.get("minimum_v3_over_v2_ratio") != ratio
        or ratio < minimum_ratio
    ):
        raise IsolatedResourceWindowError("matched model throughput gate failed")
    return {
        "matched_production_dependencies": True,
        "model_tokens_per_second_measured": True,
        "minimum_model_tokens_per_second_ratio": ratio,
    }


def _verify_resources(report: Mapping[str, Any], thresholds: Mapping[str, Any]) -> dict[str, Any]:
    profiles = report.get("resource_profiles")
    if not isinstance(profiles, dict):
        raise IsolatedResourceWindowError("resource profiles are missing")
    core = profiles.get("release_core_idle")
    idle = profiles.get("total_magi_deep_idle")
    interactive = profiles.get("interactive_session")
    active = profiles.get("total_magi_active")
    if not all(isinstance(row, dict) for row in (core, idle, interactive, active)):
        raise IsolatedResourceWindowError("required resource profile is missing")
    assert isinstance(core, dict) and isinstance(idle, dict)
    assert isinstance(interactive, dict) and isinstance(active, dict)
    observation = _number(idle.get("observation_seconds"), "deep-idle observation")
    swapout = _number(idle.get("swapout_growth_mb"), "deep-idle swapout")
    max_swapout = _number(
        thresholds.get("maximum_idle_swapout_growth_mb"), "maximum idle swapout"
    )
    v2_plane = _number(active.get("matched_v2_application_plane_footprint_mb"), "V2 footprint", positive=True)
    v3_plane = _number(active.get("v3_application_plane_footprint_mb"), "V3 footprint", positive=True)
    reduction = 1.0 - v3_plane / v2_plane
    required_reduction = _number(
        thresholds.get("minimum_application_plane_footprint_reduction_ratio"),
        "minimum footprint reduction",
    )
    if (
        observation < 1800
        or swapout > max_swapout
        or _number(core.get("max_footprint_mb"), "core footprint")
        > _number(thresholds.get("core_max_footprint_mb"), "policy core footprint")
        or _number(core.get("average_cpu_percent"), "core average CPU")
        > _number(
            thresholds.get("core_max_average_cpu_percent"), "policy core average CPU"
        )
        or _number(core.get("p95_cpu_percent"), "core p95 CPU")
        > _number(thresholds.get("core_max_p95_cpu_percent"), "policy core p95 CPU")
        or core.get("heavy_framework_imports")
        != thresholds.get("core_max_heavy_framework_imports")
        or _number(idle.get("max_footprint_mb"), "deep-idle footprint")
        > _number(thresholds.get("idle_max_footprint_mb"), "policy deep-idle footprint")
        or idle.get("loaded_models") != thresholds.get("idle_max_loaded_models")
        or not isinstance(idle.get("python_service_processes"), int)
        or idle.get("python_service_processes")
        > thresholds.get("idle_max_python_service_processes")
        or idle.get("background_heavy_workers")
        != thresholds.get("idle_max_background_heavy_workers")
        or interactive.get("loaded_primary_models")
        != thresholds.get("interactive_max_loaded_primary_models")
        or interactive.get("background_heavy_workers")
        != thresholds.get("interactive_max_background_heavy_workers")
        or interactive.get("browser_workers")
        != thresholds.get("interactive_max_browser_workers")
        or _number(interactive.get("foreground_memory_reserve_mb"), "interactive reserve")
        < _number(
            thresholds.get("interactive_min_foreground_memory_reserve_mb"),
            "policy interactive reserve",
        )
        or _number(interactive.get("attributed_metal_mb"), "interactive Metal")
        > _number(
            thresholds.get("interactive_max_magi_metal_footprint_mb"),
            "policy interactive Metal",
        )
        or _number(active.get("physical_footprint_mb"), "active footprint")
        > _number(thresholds.get("active_hard_footprint_mb"), "policy active footprint")
        or _number(active.get("attributed_metal_mb"), "active Metal")
        > _number(thresholds.get("active_hard_metal_mb"), "policy active Metal")
        or active.get("matched_workload") is not True
        or reduction < required_reduction
    ):
        raise IsolatedResourceWindowError("one or more resource budgets failed")
    return {
        "all_budgets_passed": True,
        "application_plane_footprint_reduction_ratio": reduction,
        "idle_swapout_growth_mb": swapout,
        "observation_seconds": observation,
    }


def _verify_metal(report: Mapping[str, Any], thresholds: Mapping[str, Any]) -> dict[str, Any]:
    metal = report.get("metal_attribution")
    if not isinstance(metal, dict):
        raise IsolatedResourceWindowError("Metal attribution is missing")
    baseline = _integer(metal.get("baseline_system_agx_bytes"), "Metal baseline")
    control = _integer(metal.get("negative_control_system_agx_bytes"), "Metal control")
    peak = _integer(metal.get("candidate_peak_system_agx_bytes"), "Metal peak")
    returned = _integer(metal.get("returned_system_agx_bytes"), "Metal returned")
    tolerance = _integer(metal.get("drift_tolerance_bytes"), "Metal drift tolerance")
    return_seconds = _number(metal.get("return_seconds"), "Metal return seconds")
    return_budget = _number(
        thresholds.get("worker_metal_return_to_baseline_seconds"),
        "Metal return budget",
        positive=True,
    )
    candidate_pids = metal.get("candidate_processes")
    samples = metal.get("raw_samples")
    per_process_rows = metal.get("per_process_gpu_samples")
    control_other_gpu = _integer(
        metal.get("negative_control_noncandidate_gpu_time_ns"),
        "negative-control noncandidate GPU time",
    )
    peak_other_gpu = _integer(
        metal.get("candidate_peak_noncandidate_gpu_time_ns"),
        "candidate-peak noncandidate GPU time",
    )
    other_gpu_tolerance = _integer(
        metal.get("noncandidate_gpu_time_drift_tolerance_ns"),
        "noncandidate GPU drift tolerance",
    )
    if (
        metal.get("source") != AGX_SOURCE
        or metal.get("per_process_gpu_source") != PER_PROCESS_GPU_SOURCE
        or metal.get("attribution_method") != ATTRIBUTION_METHOD
        or metal.get("per_process_gpu_permission") is not True
        or metal.get("per_process_gpu_available") is not True
        or metal.get("per_process_metal_bytes_available") is not False
        or metal.get("system_wide_bytes_relabelled_as_per_process") is not False
        or metal.get("v2_fully_stopped_for_all_samples") is not True
        or metal.get("production_ingress_quiesced_for_all_samples") is not True
        or metal.get("noncandidate_user_metal_processes") != []
        or metal.get("candidate_process_group_gone") is not True
        or metal.get("negative_control_passed") is not True
        or not isinstance(candidate_pids, list)
        or not candidate_pids
        or any(type(pid) is not int or pid <= 0 for pid in candidate_pids)
        or len(candidate_pids) != len(set(candidate_pids))
        or not isinstance(samples, list)
        or not isinstance(per_process_rows, list)
        or not per_process_rows
        or any(
            not isinstance(row, dict)
            or type(row.get("pid")) is not int
            or type(row.get("gpu_time_ns")) is not int
            or row.get("gpu_time_ns") < 0
            or row.get("raw_powermetrics_sha256") is None
            for row in per_process_rows
        )
        or not set(candidate_pids)
        <= {int(row["pid"]) for row in per_process_rows if row.get("candidate") is True}
        or [row.get("phase") for row in samples if isinstance(row, dict)]
        != ["baseline", "negative_control", "candidate_peak", "returned"]
        or any(
            not isinstance(row, dict)
            or row.get("v2_owner_pids") != []
            or row.get("noncandidate_user_metal_processes") != []
            or row.get("source") != AGX_SOURCE
            or type(row.get("system_agx_bytes")) is not int
            for row in samples
        )
        or [row["system_agx_bytes"] for row in samples]
        != [baseline, control, peak, returned]
        or abs(control - baseline) > tolerance
        or peak <= max(baseline, control) + tolerance
        or abs(returned - control) > tolerance
        or return_seconds > return_budget
        or peak_other_gpu > control_other_gpu + other_gpu_tolerance
    ):
        raise IsolatedResourceWindowError(
            "system-wide AGX bytes are not uniquely attributable to the candidate"
        )
    return {
        "metal_measurement_scope": ATTRIBUTION_METHOD,
        "per_process_metal_bytes_available": False,
        "per_process_gpu_permission": True,
        "per_process_gpu_available": True,
        "metal_returned_to_baseline": True,
        "return_seconds": return_seconds,
        "return_budget_seconds": return_budget,
        "candidate_peak_attributed_bytes": peak - control,
    }


def _verify_isolation(report: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    isolation = report.get("seatbelt_isolation")
    if not isinstance(isolation, dict):
        raise IsolatedResourceWindowError("Seatbelt isolation proof is missing")
    profile = isolation.get("profile_raw")
    network_probe = isolation.get("network_probe")
    live_probe = isolation.get("live_state_probe")
    if (
        not isinstance(profile, str)
        or hashlib.sha256(profile.encode()).hexdigest()
        != binding["sandbox_profile_sha256"]
        or isolation.get("sandbox_exec") != "/usr/bin/sandbox-exec"
        or isolation.get("sandbox_applied_to_every_owned_command") is not True
        or not isinstance(network_probe, dict)
        or not isinstance(live_probe, dict)
        or network_probe.get("attempted") is not True
        or network_probe.get("denied_by_seatbelt") is not True
        or network_probe.get("errno") not in {1, 13}
        or live_probe.get("attempted") is not True
        or live_probe.get("denied_by_seatbelt") is not True
        or live_probe.get("errno") not in {1, 13}
        or isolation.get("network_accessed") is not False
        or isolation.get("live_state_accessed") is not False
    ):
        raise IsolatedResourceWindowError("Seatbelt network/live-state denial is not proven")


def verify_report(
    report: Mapping[str, Any],
    *,
    expected_release_id: str | None = None,
    expected_release_manifest_sha256: str | None = None,
) -> dict[str, dict[str, Any]]:
    binding, thresholds, _policy = _verify_identity(report)
    if expected_release_id is not None and binding["release_id"] != expected_release_id:
        raise IsolatedResourceWindowError("isolated window release_id mismatch")
    if (
        expected_release_manifest_sha256 is not None
        and binding["release_manifest_sha256"] != expected_release_manifest_sha256
    ):
        raise IsolatedResourceWindowError("isolated window release manifest mismatch")
    _verify_preflight(report)
    _verify_isolation(report, binding)
    _verify_raw_host_samples(report)
    return {
        "g8": _verify_model(report, binding, thresholds),
        "g9": _verify_resources(report, thresholds),
        "g25": _verify_metal(report, thresholds),
    }


def live_preflight() -> dict[str, Any]:
    """Read-only readiness check; it never stops or starts a service."""

    process = subprocess.run(
        ("/bin/ps", "-axo", "pid=,pgid=,command="),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if process.returncode != 0:
        raise IsolatedResourceWindowError("process inventory failed")
    rows = [line.strip() for line in process.stdout.splitlines() if line.strip()]
    v2 = [line for line in rows if "Application Support/MAGI/runtime/MAGI_v2" in line]
    model = [
        line
        for line in rows
        if any(token in line.lower() for token in ("omlx serve", "mlx_lm", "whisper"))
    ]
    return {
        "eligible_to_start_isolated_window": not v2 and not model,
        "v2_owner_count": len(v2),
        "existing_model_or_metal_worker_count": len(model),
        "process_inventory_sha256": hashlib.sha256(process.stdout.encode()).hexdigest(),
        "mutations_performed": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--release-id")
    parser.add_argument("--release-manifest-sha256")
    args = parser.parse_args(argv)
    try:
        if args.preflight == (args.verify is not None):
            raise IsolatedResourceWindowError("choose exactly one of --preflight or --verify")
        if args.preflight:
            payload = live_preflight()
        else:
            value = json.loads(args.verify.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise IsolatedResourceWindowError("report must be a JSON object")
            payload = verify_report(
                value,
                expected_release_id=args.release_id,
                expected_release_manifest_sha256=args.release_manifest_sha256,
            )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps({"ok": True, "result": payload}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
