from __future__ import annotations

import copy
import hashlib
import json
import plistlib
from pathlib import Path

import pytest

from magi_v3.external_inputs import (
    NAMED_MUTABLE_STATE_BINDINGS,
    named_mutable_state_paths,
)
from scripts.v3_validation.isolated_resource_window import (
    AGX_SOURCE,
    ATTRIBUTION_METHOD,
    SCHEMA,
    IsolatedResourceWindowError,
    sha256_json,
    verify_report,
)
from scripts.v3_validation.isolated_resource_window_plan_builder import _policy_thresholds


ROOT = Path(__file__).resolve().parents[2]


def _raw_source(name: str, stdout: str, *, rc: int = 0) -> dict:
    argv = {
        "ps": ["/bin/ps", "-axo", "pid=,uid=,ppid=,pgid=,%cpu=,command="],
        "lsof": ["/usr/sbin/lsof", "-b", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-Fpn"],
        "ioreg": ["/usr/sbin/ioreg", "-r", "-c", "AGXAccelerator"],
        "powermetrics": [
            "/usr/bin/powermetrics", "--format", "plist", "--sample-count", "1",
            "--sample-rate", "100", "--samplers", "tasks,gpu_power", "--show-process-gpu",
        ],
    }[name]
    result = {
        "argv": argv,
        "returncode": rc,
        "stdout": stdout,
        "stderr": "",
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
    }
    if name == "powermetrics":
        result.update(
            invoker_argv=["/usr/bin/sudo", "-n", "--", *argv],
            privilege_receipt={
                "schema": "magi.v3.fixed-powermetrics-privilege/v1",
                "collector_euid": 501,
                "collector_ran_as_root": False,
                "invoker": "/usr/bin/sudo",
                "noninteractive": True,
                "fixed_measurement_argv_sha256": hashlib.sha256(
                    json.dumps(argv, separators=(",", ":")).encode()
                ).hexdigest(),
            },
        )
    return result


def _pow_text(rows: list[dict] | None = None) -> str:
    return plistlib.dumps({"tasks": rows or [{"pid": 1, "gpu_time_ns": 0}]}).decode()


def passing_report() -> dict:
    policy_raw = (ROOT / "config/v3_resource_policy.json").read_text(encoding="utf-8")
    thresholds = _policy_thresholds(json.loads(policy_raw))
    profile_raw = (ROOT / "config/v3_resource_window.sb").read_text(encoding="utf-8")
    binding = {
        "release_id": "v3-test",
        "release_manifest_sha256": "1" * 64,
        "release_snapshot_sha256": "2" * 64,
        "python_runtime_sha256": "3" * 64,
        "resource_policy_sha256": hashlib.sha256(policy_raw.encode()).hexdigest(),
        "model_tree_sha256": "5" * 64,
        "model_backend_sha256": "6" * 64,
        "prompt_sha256": "7" * 64,
        "sandbox_profile_sha256": hashlib.sha256(profile_raw.encode()).hexdigest(),
    }
    request = {
        "schema": "magi.v3.resource-window-matched-request/v1",
        "corpus_sha256": binding["prompt_sha256"],
        "model_tree_sha256": binding["model_tree_sha256"],
        "max_tokens": 256,
        "temperature": 0,
        "seed": 63181107,
        "repeats_per_arm": 3,
    }
    inert_root = Path("/tmp/magi-v3-resource-window-inert")
    external_inputs = {
        "website_root": str(inert_root / "website"),
        "website_admin_sha256": "e" * 64,
        "laf_config_file": str(inert_root / "config.json"),
        "laf_config_sha256": "8" * 64,
        "laf_config_mode": "0600",
        "google_credentials_file": str(inert_root / "credentials.json"),
        "google_credentials_sha256": "4" * 64,
        "google_credentials_mode": "0600",
        "google_calendar_token_source_file": str(
            inert_root / "tokens/google_calendar_token.json"
        ),
        "google_calendar_token_source_sha256": "a" * 64,
        "laf_gmail_token_source_file": str(
            inert_root / "tokens/laf_gmail_token.pickle"
        ),
        "laf_gmail_token_source_sha256": "b" * 64,
        "file_review_token_source_file": str(
            inert_root / "tokens/filereview_token.pickle"
        ),
        "file_review_token_source_sha256": "c" * 64,
    }
    mutable_token_handoff = [
        {
            "key": key,
            "status": "materialized",
            "source": external_inputs[source_key],
            "source_sha256": external_inputs[source_sha_key],
            "target": str(inert_root / "v3/shared/secrets" / target_leaf),
            "target_sha256": external_inputs[source_sha_key],
        }
        for key, source_key, source_sha_key, target_leaf in (
            (
                "google_calendar_token",
                "google_calendar_token_source_file",
                "google_calendar_token_source_sha256",
                "google_calendar_token.json",
            ),
            (
                "laf_gmail_token",
                "laf_gmail_token_source_file",
                "laf_gmail_token_source_sha256",
                "laf_gmail_token.pickle",
            ),
            (
                "file_review_token",
                "file_review_token_source_file",
                "file_review_token_source_sha256",
                "filereview_token.pickle",
            ),
        )
    ]
    composition = {
        "schema": "magi.v3.resource-window-production-composition/v1",
        "v2": {"entrypoint": "scripts/ops/run_daemon_no_site.py", "members": {"daemon.py": "a" * 64}},
        "v3": {"entrypoints": {"gateway": "magi_v3.gateway"}, "members": {"magi_v3/gateway.py": "b" * 64}},
        "external_inputs": dict(external_inputs),
        "arm_transport": "arm_owned_production_process",
        "shared_direct_backend": False,
    }
    composition["composition_sha256"] = sha256_json(composition)
    workload = {
        "request": request,
        "request_sha256": sha256_json(request),
        "http_request_sha256": "e" * 64,
        "composition": composition,
        "same_corpus_model_request_required": True,
    }
    arms = []
    for index in range(6):
        arm = "v2_reference" if index % 2 == 0 else "v3_candidate"
        seconds = 10.0 if arm == "v2_reference" else 9.5
        arms.append(
            {
                "arm": arm,
                "generated_tokens": 200,
                "generation_seconds": seconds,
                "tokens_per_second": 200 / seconds,
                "pid": 1000 + index,
                "pgid": 2000 + index,
                "proc_start_abstime": 3000 + index,
                "started_monotonic_ns": 100 + index * 100,
                "completed_monotonic_ns": 150 + index * 100,
                "returncode": 0,
                "timed_out": False,
                "process_group_gone": True,
                "network_accessed": False,
                "production_state_accessed": False,
                "prompt_sha256": binding["prompt_sha256"],
                "model_tree_sha256": binding["model_tree_sha256"],
                "model_backend_sha256": binding["model_backend_sha256"],
                "python_runtime_sha256": binding["python_runtime_sha256"],
                "request_sha256": workload["request_sha256"],
                "http_request_sha256": workload["http_request_sha256"],
                "response_sha256": f"{index + 1:x}" * 64,
                "owned_model_server_pid": 4101 if arm == "v3_candidate" else 4201,
                "composition_sha256": composition["composition_sha256"],
                "transport": "arm_owned_production_process_http",
                "shared_direct_backend": False,
                "seatbelt_network_denied": True,
                "seatbelt_live_state_denied": True,
            }
        )
    baseline = 100_000_000
    control = 102_000_000
    peak = 800_000_000
    returned = 101_000_000
    report = {
        "schema": SCHEMA,
        "status": "passed",
        "mode": "v2_fully_stopped_isolated_window",
        "release_binding": binding,
        "execution_binding": {
            "plan_sha256": "9" * 64,
            "approval_token_sha256": "a" * 64,
            "collector_source_sha256": "b" * 64,
            "owned_workdir_marker_sha256": "c" * 64,
            "token_consumption_receipt_sha256": "f" * 64,
            "token_consumption_receipt": {
                "schema": "magi.v3.resource-window-plan-consumption/v1",
                "plan_sha256": "9" * 64,
                "approval_token_sha256": "a" * 64,
                "outer_plan_sha256": "d" * 64,
                "outer_plan_semantic_sha256": "e" * 64,
                "provisional_gate_sha256": "f" * 64,
                "zero_owner_phase_token_sha256": "0" * 64,
                "consumer_pid": 123,
                "consumed_monotonic_ns": 456,
            },
            "plan_consumed_once": True,
            "outer_plan_sha256": "d" * 64,
            "outer_plan_semantic_sha256": "e" * 64,
            "provisional_gate_sha256": "f" * 64,
            "provisional_gate_status": "provisional_16_of_19_passed",
            "provisional_gate_counts": {"required": 16, "passed": 16, "failed": 0, "missing": 0, "invalid": 0},
            "formal_live_eligible_before_window": False,
            "observed_listener_ports": [5002, 5003, 5014, 8080, 8081, 8088, 18080],
            "outer_executor": "scripts.v3_validation.isolated_live_execute",
            "outer_executor_phase": "resource_window_after_v2_zero_owner",
            "v2_restore_owner": "outer_isolated_live_executor_finally",
            "production_composition_receipt": {
                "schema": "magi.v3.resource-window-production-environment/v1",
                "release_id": "v3-test",
                "runtime_root": str(Path("/tmp/v3").resolve()),
                "service_manifest_sha256": "a" * 64,
                "ownership_manifest_sha256": "b" * 64,
                "environment_file_sha256": "c" * 64,
                "cron_jobs_sha256": "d" * 64,
                "python_runtime_sha256": binding["python_runtime_sha256"],
                "website_root": external_inputs["website_root"],
                "website_admin_sha256": external_inputs[
                    "website_admin_sha256"
                ],
                "mutable_token_handoff": mutable_token_handoff,
                "named_mutable_state_bindings": {
                    env_name: named_mutable_state_paths("/tmp/v3")[binding_name]
                    for env_name, (binding_name, _relative) in (
                        NAMED_MUTABLE_STATE_BINDINGS.items()
                    )
                },
            },
            "outer_owner_contract": {
                "required_stopped_launchd_labels": [
                    "com.magi.daemon", "com.magi.omlx", "com.magi.omlx-embed",
                    "com.magi.omlx-phi4", "com.magi.omlx-smol", "com.magi.mlx-mtp",
                    "com.magi.omlx-nemotron-parse",
                ],
                "zero_owner_snapshot_required_coverage": ["launchd", "ownership", "pidfile", "port", "process"],
                "outer_must_capture_initial_label_state": True,
                "outer_finally_restore_initial_label_state_exactly": True,
                "restore_proof_owner": "outer_isolated_live_executor_finally",
            },
        },
        "external_inputs": dict(external_inputs),
        "thresholds": thresholds,
        "policy_binding": {
            "policy_raw_json": policy_raw,
            "policy_raw_sha256": hashlib.sha256(policy_raw.encode()).hexdigest(),
            "resolved_thresholds": thresholds,
            "resolved_thresholds_sha256": sha256_json(thresholds),
        },
        "workload_binding": workload,
        "seatbelt_isolation": {
            "profile_raw": profile_raw,
            "sandbox_exec": "/usr/bin/sandbox-exec",
            "sandbox_applied_to_every_owned_command": True,
            "network_probe": {"attempted": True, "denied_by_seatbelt": True, "errno": 1},
            "live_state_probe": {"attempted": True, "denied_by_seatbelt": True, "errno": 13},
            "network_accessed": False,
            "live_state_accessed": False,
        },
        "preflight": {
            "v2_fully_stopped": True,
            "candidate_not_started_at_baseline": True,
            "production_ingress_quiesced": True,
            "v2_owner_pids": [],
            "v3_owner_pids_before_start": [],
            "production_port_owner_pids": [],
            "noncandidate_user_metal_processes": [],
            "process_inventory_sha256": "8" * 64,
            "per_process_gpu_permission": True,
            "raw_source_coverage": ["ioreg", "lsof", "powermetrics", "ps"],
            "required_stopped_launchd_labels": [
                "com.magi.daemon", "com.magi.omlx", "com.magi.omlx-embed",
                "com.magi.omlx-phi4", "com.magi.omlx-smol", "com.magi.mlx-mtp",
                "com.magi.omlx-nemotron-parse",
            ],
            "stopped_launchd_states": [
                {"label": label, "loaded": False, "returncode": 113, "stdout_sha256": "0" * 64, "stderr_sha256": "1" * 64}
                for label in [
                    "com.magi.daemon", "com.magi.omlx", "com.magi.omlx-embed",
                    "com.magi.omlx-phi4", "com.magi.omlx-smol", "com.magi.mlx-mtp",
                    "com.magi.omlx-nemotron-parse",
                ]
            ],
        },
        "model_benchmark": {
            "arms": arms,
            "same_model_prompt_backend_runtime": True,
            "same_corpus_model_request": True,
            "separate_arm_owned_production_compositions": True,
            "shared_direct_backend": False,
            "maximum_simultaneous_arms": 1,
            "minimum_v3_over_v2_ratio": (200 / 9.5) / (200 / 10.0),
        },
        "resource_profiles": {
            "release_core_idle": {
                "max_footprint_mb": 200,
                "average_cpu_percent": 0.5,
                "p95_cpu_percent": 2.0,
                "heavy_framework_imports": 0,
            },
            "total_magi_deep_idle": {
                "observation_seconds": 1800,
                "swapout_growth_mb": 0,
                "max_footprint_mb": 400,
                "loaded_models": 0,
                "python_service_processes": 3,
                "background_heavy_workers": 0,
            },
            "interactive_session": {
                "loaded_primary_models": 1,
                "background_heavy_workers": 0,
                "browser_workers": 0,
                "foreground_memory_reserve_mb": 9000,
                "attributed_metal_mb": 6000,
            },
            "total_magi_active": {
                "matched_v2_application_plane_footprint_mb": 1000,
                "v3_application_plane_footprint_mb": 700,
                "physical_footprint_mb": 9000,
                "attributed_metal_mb": 7000,
                "matched_workload": True,
            },
        },
        "metal_attribution": {
            "source": AGX_SOURCE,
            "attribution_method": ATTRIBUTION_METHOD,
            "per_process_metal_bytes_available": False,
            "per_process_gpu_source": "/usr/bin/powermetrics --show-process-gpu",
            "per_process_gpu_permission": True,
            "per_process_gpu_available": True,
            "system_wide_bytes_relabelled_as_per_process": False,
            "v2_fully_stopped_for_all_samples": True,
            "production_ingress_quiesced_for_all_samples": True,
            "noncandidate_user_metal_processes": [],
            "candidate_process_group_gone": True,
            "negative_control_passed": True,
            "candidate_processes": [4101, 4102],
            "per_process_gpu_samples": [
                {"pid": 4101, "gpu_time_ns": 100, "candidate": True, "raw_powermetrics_sha256": "a" * 64},
                {"pid": 4102, "gpu_time_ns": 100, "candidate": True, "raw_powermetrics_sha256": "b" * 64},
            ],
            "negative_control_noncandidate_gpu_time_ns": 100,
            "candidate_peak_noncandidate_gpu_time_ns": 200,
            "noncandidate_gpu_time_drift_tolerance_ns": 5_000_000,
            "baseline_system_agx_bytes": baseline,
            "negative_control_system_agx_bytes": control,
            "candidate_peak_system_agx_bytes": peak,
            "returned_system_agx_bytes": returned,
            "drift_tolerance_bytes": 4_000_000,
            "return_seconds": 30,
            "raw_samples": [
                {
                    "phase": phase,
                    "system_agx_bytes": value,
                    "source": AGX_SOURCE,
                    "v2_owner_pids": [],
                    "noncandidate_user_metal_processes": [],
                }
                for phase, value in (
                    ("baseline", baseline),
                    ("negative_control", control),
                    ("candidate_peak", peak),
                    ("returned", returned),
                )
            ],
        },
    }
    report["execution_binding"]["production_composition_receipt"]["receipt_sha256"] = sha256_json(
        report["execution_binding"]["production_composition_receipt"]
    )
    report["execution_binding"]["token_consumption_receipt_sha256"] = hashlib.sha256(
        (
            json.dumps(
                report["execution_binding"]["token_consumption_receipt"],
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    ).hexdigest()
    report["preflight"]["raw_sources"] = {
        "ps": _raw_source("ps", "1 501 0 1 0.0 /usr/bin/test\n"),
        "lsof": _raw_source("lsof", "", rc=1),
        "ioreg": _raw_source("ioreg", '"In use system memory"=100000000'),
        "powermetrics": _raw_source("powermetrics", _pow_text()),
    }
    report["preflight"]["process_inventory_sha256"] = report["preflight"]["raw_sources"]["ps"]["stdout_sha256"]
    raw_rows = []
    phases = ["baseline", "negative_control"] + ["v2_reference"] * 6 + ["v2_model"] * 3 + ["v3_deep_idle"] * 181 + ["v3_model"] * 3 + ["returned"]
    for index, phase in enumerate(phases, 1):
        ps = _raw_source("ps", "1 501 0 1 0.0 /usr/bin/test\n")
        lsof = _raw_source("lsof", "", rc=1)
        ioreg = _raw_source("ioreg", '"In use system memory"=100000000')
        power = _raw_source("powermetrics", _pow_text())
        raw_rows.append({
            "sequence": index, "phase": phase, "monotonic_ns": index,
            "live_v2_owner_pids": [], "production_listener_pids": [],
            "unexpected_listener_pids": [], "candidate_gpu_processes": [],
            "noncandidate_gpu_processes": [], "per_process_gpu_permission": True,
            "owned_process_pids": [], "system_agx_bytes": 100000000,
            "ps_inventory_sha256": ps["stdout_sha256"],
            "listener_inventory_sha256": lsof["stdout_sha256"],
            "ioreg_inventory_sha256": ioreg["stdout_sha256"],
            "powermetrics_inventory_sha256": power["stdout_sha256"],
            "ps": ps, "lsof": lsof, "ioreg": ioreg, "powermetrics": power,
        })
    report["raw_host_samples"] = raw_rows
    report["evidence_sha256"] = sha256_json(report)
    return report


def rehash(report: dict) -> None:
    report.pop("evidence_sha256", None)
    report["evidence_sha256"] = sha256_json(report)


def test_passing_fixture_has_complete_inert_production_external_contract() -> None:
    report = passing_report()
    external = report["external_inputs"]
    assert set(external) == {
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
    assert report["workload_binding"]["composition"]["external_inputs"] == external
    receipt = report["execution_binding"]["production_composition_receipt"]
    assert len(receipt["mutable_token_handoff"]) == 3
    assert all(
        row["status"] == "materialized"
        and Path(row["source"]).is_absolute()
        and Path(row["target"]).is_absolute()
        and row["source_sha256"] == row["target_sha256"]
        for row in receipt["mutable_token_handoff"]
    )
    assert receipt["receipt_sha256"] == sha256_json(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


def test_resource_receipt_rejects_rehashed_nonexact_named_state_binding() -> None:
    report = passing_report()
    receipt = report["execution_binding"]["production_composition_receipt"]
    receipt["named_mutable_state_bindings"]["MAGI_PAYMENT_REGISTRY_PATH"] = (
        "/tmp/v3/shared/file-review/downloads/wrong.json"
    )
    receipt["receipt_sha256"] = sha256_json(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    rehash(report)

    with pytest.raises(IsolatedResourceWindowError, match="production composition receipt"):
        verify_report(report)


def test_exclusive_stopped_window_can_certify_without_claiming_per_process_metal() -> None:
    report = passing_report()
    metrics = verify_report(
        report,
        expected_release_id="v3-test",
        expected_release_manifest_sha256="1" * 64,
    )

    assert metrics["g8"]["minimum_model_tokens_per_second_ratio"] >= 0.95
    assert metrics["g9"]["all_budgets_passed"] is True
    assert metrics["g25"]["metal_returned_to_baseline"] is True
    assert metrics["g25"]["per_process_metal_bytes_available"] is False
    assert metrics["g25"]["metal_measurement_scope"] == ATTRIBUTION_METHOD


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("preflight", "v2_owner_pids"), [123]),
        (("preflight", "noncandidate_user_metal_processes"), [{"pid": 99}]),
        (("model_benchmark", "maximum_simultaneous_arms"), 2),
        (("resource_profiles", "total_magi_deep_idle", "observation_seconds"), 1799),
        (("resource_profiles", "total_magi_active", "attributed_metal_mb"), 9000),
        (("metal_attribution", "per_process_metal_bytes_available"), True),
        (("metal_attribution", "noncandidate_user_metal_processes"), [{"pid": 88}]),
        (("metal_attribution", "negative_control_system_agx_bytes"), 200_000_000),
        (("metal_attribution", "returned_system_agx_bytes"), 300_000_000),
        (("metal_attribution", "return_seconds"), 61),
    ],
)
def test_window_fails_closed_on_owner_budget_model_or_attribution_drift(
    path: tuple[str, ...], value: object
) -> None:
    report = copy.deepcopy(passing_report())
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    rehash(report)

    with pytest.raises(IsolatedResourceWindowError):
        verify_report(report)


def test_rehash_is_required_and_release_binding_is_exact() -> None:
    report = passing_report()
    report["thresholds"]["minimum_model_tokens_per_second_ratio"] = 0.1
    with pytest.raises(IsolatedResourceWindowError, match="hash mismatch"):
        verify_report(report)

    report = passing_report()
    with pytest.raises(IsolatedResourceWindowError, match="release_id"):
        verify_report(report, expected_release_id="different")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report["raw_host_samples"][10].pop("powermetrics"), "raw powermetrics"),
        (lambda report: report["raw_host_samples"][10].update(live_v2_owner_pids=[99]), "ownership/GPU"),
        (lambda report: report["raw_host_samples"][10].update(unexpected_listener_pids=[88]), "ownership/GPU"),
        (lambda report: report["raw_host_samples"][10].update(per_process_gpu_permission=False), "ownership/GPU"),
        (lambda report: report["seatbelt_isolation"]["network_probe"].update(denied_by_seatbelt=False), "Seatbelt"),
        (lambda report: report["workload_binding"]["composition"].update(shared_direct_backend=True), "production compositions"),
        (lambda report: report.pop("external_inputs"), "production composition receipt"),
        (lambda report: report["model_benchmark"].update(shared_direct_backend=True), "throughput gate"),
        (lambda report: report["execution_binding"].update(plan_consumed_once=False), "consumed once"),
        (lambda report: report["execution_binding"].update(provisional_gate_status="failed"), "consumed once"),
        (lambda report: report["metal_attribution"].update(per_process_gpu_permission=False), "uniquely attributable"),
        (lambda report: report["metal_attribution"].update(candidate_peak_noncandidate_gpu_time_ns=10_000_000), "uniquely attributable"),
        (lambda report: report["raw_host_samples"][10]["powermetrics"]["privilege_receipt"].update(collector_ran_as_root=True), "privilege"),
    ],
)
def test_authoritative_resource_window_red_team_mutations_fail_closed(
    mutation, message: str
) -> None:
    report = passing_report()
    mutation(report)
    rehash(report)
    with pytest.raises(IsolatedResourceWindowError, match=message):
        verify_report(report)


def test_policy_threshold_cannot_be_weakened_even_when_report_is_rehashed() -> None:
    report = passing_report()
    report["thresholds"]["minimum_model_tokens_per_second_ratio"] = 0.01
    report["policy_binding"]["resolved_thresholds"][
        "minimum_model_tokens_per_second_ratio"
    ] = 0.01
    report["policy_binding"]["resolved_thresholds_sha256"] = sha256_json(
        report["policy_binding"]["resolved_thresholds"]
    )
    rehash(report)
    with pytest.raises(IsolatedResourceWindowError, match="drifted from raw policy"):
        verify_report(report)
