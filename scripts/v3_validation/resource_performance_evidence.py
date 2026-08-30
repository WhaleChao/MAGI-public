"""Pure validation for fail-closed resource/performance partial evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

from scripts.v3_validation.perf_compat import PerfEvidenceError, verify_evidence_hash
from scripts.v3_validation.isolated_resource_window import (
    IsolatedResourceWindowError,
    verify_report as verify_isolated_resource_window,
)
from scripts.v3_validation.g8_isolated_smb import (
    G8SMBBlocked,
    verify_report as verify_g8_smb_report,
)


SCHEMA = "magi.v3.resource-performance-partial/v1"
WORKLOAD = "matched_v2_v3_performance"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GATE_IDS = (
    "matched_v2_warm_cold_performance_baseline_complete",
    "resource_policy_all_budgets_passed",
    "heavy_plus_interactive_preemption_benchmark_passed",
    "worker_process_group_footprint_and_metal_return_to_baseline",
)
EXPECTED_GAPS = (
    "MODEL_TOKENS_PER_SECOND_UNMEASURED",
    "COMPLETE_RESOURCE_BUDGET_PROFILES_UNMEASURED",
    "IDLE_SWAPOUT_30_MINUTE_WINDOW_UNMEASURED",
    "VALIDATED_PER_PROCESS_METAL_BYTES_UNAVAILABLE",
)
# A certified ``perf_certification`` report is deliberately not a production
# deployment: it has no listener and uses a caller-owned filesystem root.  It
# *is*, however, a same-host comparison of the real V2 handler and the native
# V3 handler, with a private MariaDB server, signed session, folder creation,
# and archive move.  Those are the dependencies G8 is intended to compare.
#
# Keep the scope limitation available to reports, but do not turn it into a
# missing dependency after the certifier has proved every scenario.  Doing so
# would make a verified G8 result impossible to promote merely because the
# safety boundary correctly forbids a LIVE SMB share or service listener.
MATCHED_PRODUCTION_SCOPE_LIMITATIONS = (
    "no_live_smb_share_or_production_listener",
)
MATCHED_PRODUCTION_MISSING_REQUIREMENTS = (
    "hash_bound_isolated_remote_smb_transport_and_composition_receipt",
)


class ResourcePerformanceEvidenceError(ValueError):
    """Raised when partial evidence is forged, incomplete, or overclaims readiness."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _finite_nonnegative(value: Any, description: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ResourcePerformanceEvidenceError(
            f"{description} must be finite and non-negative"
        )
    return float(value)


def verify_g8_transport_composition_receipt(
    receipt: Mapping[str, Any] | None,
    *,
    matched_performance_sha256: str,
    release_binding: Mapping[str, Any],
    release_files: Mapping[str, str] | None = None,
) -> bool:
    """Recompute G8 from the authority-bearing raw SMB evidence."""
    if receipt is None:
        return False
    if not isinstance(receipt, Mapping):
        raise ResourcePerformanceEvidenceError("G8 transport receipt is not an object")
    try:
        verify_g8_smb_report(
            receipt,
            expected_release_id=str(release_binding.get("release_id") or ""),
            expected_release_manifest_sha256=str(
                release_binding.get("release_manifest_sha256") or ""
            ),
            expected_matched_performance_sha256=matched_performance_sha256,
        )
    except G8SMBBlocked as exc:
        raise ResourcePerformanceEvidenceError(f"G8 raw SMB evidence failed: {exc}") from exc
    return True


def _performance_metrics(
    report: Mapping[str, Any],
    matched_production_report: Mapping[str, Any],
    isolated_window_report: Mapping[str, Any] | None,
    transport_composition_receipt: Mapping[str, Any] | None = None,
    release_binding: Mapping[str, Any] | None = None,
    release_files: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    try:
        verify_evidence_hash(report)
    except PerfEvidenceError as exc:
        raise ResourcePerformanceEvidenceError(
            f"matched performance report hash failed: {exc}"
        ) from exc
    claim = report.get("claim_coverage")
    gate = report.get("gate")
    comparison = report.get("comparison")
    business = report.get("synthetic_business_benchmark")
    if (
        report.get("workload") != "native_gateway_livez"
        or report.get("offline") is not True
        or not isinstance(claim, dict)
        or claim.get("production_business_workload") is not False
        or claim.get("native_v3_handler") is not True
        or claim.get("warm_latency_p50_p95_p99") is not True
        or claim.get("cold_first_request_latency") is not True
        or not isinstance(gate, dict)
        or gate.get("decision") != "blocker_retained"
        or gate.get("eligible_to_clear_full_v2_v3_performance_blocker") is not False
        or not isinstance(comparison, dict)
        or not isinstance(business, dict)
    ):
        raise ResourcePerformanceEvidenceError(
            "matched performance report does not retain its production blocker"
        )
    v2_p95 = _finite_nonnegative(comparison.get("latency_p95_v2_us"), "V2 p95")
    v3_p95 = _finite_nonnegative(
        comparison.get("latency_p95_native_v3_us"), "V3 p95"
    )
    business_comparison = business.get("comparison")
    if not isinstance(business_comparison, dict):
        raise ResourcePerformanceEvidenceError("business comparison is missing")
    business_v2 = _finite_nonnegative(
        business_comparison.get("latency_p95_v2_us"), "business V2 p95"
    )
    business_v3 = _finite_nonnegative(
        business_comparison.get("latency_p95_native_v3_us"), "business V3 p95"
    )
    if min(v2_p95, business_v2) <= 0:
        raise ResourcePerformanceEvidenceError("performance baseline p95 must be positive")
    matched = matched_production_report
    if not isinstance(matched, dict):
        raise ResourcePerformanceEvidenceError(
            "matched production MariaDB/session/NAS report is missing"
        )
    try:
        from scripts.v3_validation.perf_certification import (
            PerformanceCertificationError,
            verify_performance_certification,
        )

        verify_performance_certification(matched)
    except (PerformanceCertificationError, ImportError) as exc:
        raise ResourcePerformanceEvidenceError(
            f"matched production report failed: {exc}"
        ) from exc
    matched_comparison = matched.get("comparison")
    matched_gate = matched.get("gate")
    matched_safety = matched.get("safety")
    matched_performance = (
        matched_comparison.get("performance")
        if isinstance(matched_comparison, dict)
        else None
    )
    matched_ratio = _finite_nonnegative(
        matched_performance.get("v3_over_v2_ratio")
        if isinstance(matched_performance, dict)
        else None,
        "matched production V3/V2 p95 ratio",
    )
    if (
        matched.get("schema") != "magi.v3.matched-production-performance/v1"
        or matched.get("status") != "certified"
        or matched.get("workload") != WORKLOAD
        or not isinstance(matched_gate, dict)
        or matched_gate.get("decision") != "clear"
        or matched_gate.get("eligible_to_clear_full_v2_v3_performance_blocker")
        is not True
        or not isinstance(matched_comparison, dict)
        or matched_comparison.get("semantic_equivalence_passed") is not True
        or not isinstance(matched_performance, dict)
        or matched_performance.get("passed") is not True
        or not isinstance(matched_safety, dict)
        or matched_safety.get("live_state_accessed") is not False
        or matched_safety.get("live_business_database_accessed") is not False
        or matched_safety.get("production_service_started") is not False
        or matched_safety.get("production_port_accessed") is not False
        or matched_safety.get("version_arms_ran_concurrently") is not False
        or matched_safety.get("mariadb_tcp_networking_disabled") is not True
        or matched_safety.get("mariadb_unix_socket_removed_after_shutdown") is not True
    ):
        raise ResourcePerformanceEvidenceError(
            "matched production report did not prove a safe complete comparison"
        )
    regression = max(
        0.0,
        v3_p95 / v2_p95 - 1.0,
        business_v3 / business_v2 - 1.0,
        matched_ratio - 1.0,
    )
    model_window: dict[str, Any] | None = None
    if isolated_window_report is not None:
        try:
            model_window = verify_isolated_resource_window(isolated_window_report)["g8"]
        except IsolatedResourceWindowError as exc:
            raise ResourcePerformanceEvidenceError(
                f"isolated model window failed: {exc}"
            ) from exc
    model_measured = bool(
        model_window is not None
        and model_window.get("model_tokens_per_second_measured") is True
    )
    transport_composition_verified = (
        verify_g8_transport_composition_receipt(
            transport_composition_receipt,
            matched_performance_sha256=str(matched.get("evidence_sha256") or ""),
            release_binding=release_binding,
            release_files=release_files,
        )
        if release_binding is not None
        else False
    )
    result = {
        # This is not inferred from a producer boolean: the preceding
        # verification replays hashes, arm reports, MariaDB/socket settings,
        # session results, filesystem transcript, DB state, and thresholds.
        # It therefore remains fail-closed when a V3 folder/archive path is
        # absent, a request plan drifts, or either arm is not sequential.
        "matched_disposable_dependencies": True,
        "matched_production_dependencies": transport_composition_verified,
        "matched_dependency_evidence": {
            "real_mariadb_unix_socket": True,
            "signed_session_authorization": True,
            "v2_v3_folder_and_archive_transcript": True,
            "same_host_sequential_arms": True,
            "live_state_accessed": False,
        },
        "scope_limitations": list(MATCHED_PRODUCTION_SCOPE_LIMITATIONS),
        "warm_and_cold_measured": True,
        "maximum_p95_regression_ratio": round(regression, 9),
        "model_tokens_per_second_measured": model_measured,
        "measured_scope": [
            "actual_v2_vs_native_v3_livez",
            "disposable_sqlite_osc_get_post",
            "private_mariadb_session_folder_archive",
        ],
        "missing_requirements": [
            *(
                []
                if transport_composition_verified
                else MATCHED_PRODUCTION_MISSING_REQUIREMENTS
            ),
            *(
                []
                if model_measured
                else ["hash_bound_local_model_tokens_per_second"]
            ),
        ],
    }
    if model_window is not None:
        result["minimum_model_tokens_per_second_ratio"] = model_window[
            "minimum_model_tokens_per_second_ratio"
        ]
    return result


def _resource_metrics(
    probe: Mapping[str, Any], isolated_window_report: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    footprint = probe.get("owned_probe_physical_footprint_mb")
    if footprint is not None:
        _finite_nonnegative(footprint, "owned probe physical footprint")
    swapout = _finite_nonnegative(probe.get("swapout_growth_mb"), "swapout growth")
    observed_seconds = _finite_nonnegative(
        probe.get("observation_seconds"), "resource observation seconds"
    )
    if (
        probe.get("physical_footprint_source")
        != "/usr/bin/footprint --noCategories -f bytes"
        or probe.get("production_state_accessed") is not False
        or probe.get("model_loaded") is not False
        or probe.get("complete_budget_profiles_measured") is not False
        or probe.get("required_idle_observation_seconds") != 1800
        or observed_seconds >= 1800
    ):
        raise ResourcePerformanceEvidenceError("resource probe overclaims budget coverage")
    window: dict[str, Any] | None = None
    if isolated_window_report is not None:
        try:
            window = verify_isolated_resource_window(isolated_window_report)["g9"]
        except IsolatedResourceWindowError as exc:
            raise ResourcePerformanceEvidenceError(
                f"isolated resource window failed: {exc}"
            ) from exc
    result = {
        "all_budgets_passed": window is not None,
        "idle_swapout_growth_mb": round(swapout, 6),
        "observed_probe_physical_footprint_mb": footprint,
        "observation_seconds": round(observed_seconds, 6),
        "required_idle_observation_seconds": 1800,
        "missing_budget_profiles": (
            []
            if window is not None
            else [
                "release_core_deep_idle_30_minute",
                "interactive_model_session",
                "total_magi_active_model_and_metal",
            ]
        ),
    }
    if window is not None:
        result.update(
            application_plane_footprint_reduction_ratio=window[
                "application_plane_footprint_reduction_ratio"
            ],
            idle_swapout_growth_mb=window["idle_swapout_growth_mb"],
            observation_seconds=window["observation_seconds"],
        )
    return result


def _preemption_metrics(probe: Mapping[str, Any]) -> dict[str, Any]:
    samples = probe.get("samples")
    if not isinstance(samples, list) or len(samples) < 4:
        raise ResourcePerformanceEvidenceError(
            "preemption probe requires at least four raw samples"
        )
    queue_values: list[float] = []
    browser_values: list[float] = []
    deadline_misses = orphan_groups = duplicates = lost = 0
    requeued = completed = 0
    seen_cycles: set[int] = set()
    seen_jobs: set[str] = set()
    seen_pids: set[int] = set()
    seen_priorities: set[str] = set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise ResourcePerformanceEvidenceError("preemption sample must be an object")
        cycle = sample.get("cycle")
        priority = sample.get("incoming_priority_class")
        leader = sample.get("heavy_worker_pid")
        descendant = sample.get("heavy_descendant_pid")
        job_ids = (sample.get("heavy_job_id"), sample.get("interactive_job_id"))
        attempts = sample.get("attempts")
        queue_ms = _finite_nonnegative(
            sample.get("interactive_queue_ms"), "interactive queue latency"
        )
        budget_ms = _finite_nonnegative(
            sample.get("deadline_budget_ms"), "interactive deadline budget"
        )
        if (
            type(cycle) is not int
            or cycle < 1
            or cycle in seen_cycles
            or priority not in {"P0", "P1"}
            or sample.get("queue_class") != "interactive_browser"
            or type(leader) is not int
            or type(descendant) is not int
            or leader <= 0
            or descendant <= 0
            or leader in seen_pids
            or descendant in seen_pids
            or any(not isinstance(job_id, str) or not job_id for job_id in job_ids)
            or any(job_id in seen_jobs for job_id in job_ids)
            or sample.get("automatic_preemption_count") != 1
            or sample.get("preemption_source") != "dispatch_handle.preemptions"
            or sample.get("manual_terminate_invoked") is not False
            or sample.get("worker_killed_after_bounded_grace") is not True
            or sample.get("process_group_gone") is not True
            or sample.get("leader_pid_gone") is not True
            or sample.get("descendant_pid_gone") is not True
            or sample.get("heavy_requeued") is not True
            or attempts != [[1, "preempted"], [2, "succeeded"]]
            or sample.get("retry_attempt_number") != 2
            or sample.get("retry_completed_once") is not True
            or sample.get("active_leases_after_completion") != 0
            or type(sample.get("deadline_missed")) is not bool
            or sample.get("deadline_missed") != (queue_ms > budget_ms)
            or sample.get("orphan_process_groups") != 0
            or sample.get("duplicate_completions") != 0
            or sample.get("lost_jobs") != 0
        ):
            raise ResourcePerformanceEvidenceError(
                "preemption raw sample did not prove automatic durable recovery"
            )
        seen_cycles.add(cycle)
        seen_priorities.add(priority)
        seen_jobs.update(str(job_id) for job_id in job_ids)
        seen_pids.update((leader, descendant))
        queue_values.append(queue_ms)
        if priority == "P1":
            browser_values.append(queue_ms / 1000)
        deadline_misses += int(bool(sample["deadline_missed"]))
        orphan_groups += int(sample["orphan_process_groups"])
        duplicates += int(sample["duplicate_completions"])
        lost += int(sample["lost_jobs"])
        requeued += int(bool(sample["heavy_requeued"]))
        completed += int(bool(sample["retry_completed_once"]))
    if seen_priorities != {"P0", "P1"} or not browser_values:
        raise ResourcePerformanceEvidenceError(
            "both P0 and P1 interactive/browser samples are required"
        )

    def p95(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, max(0, (95 * len(ordered) + 99) // 100 - 1))]

    interactive_p95 = p95(queue_values)
    browser_p95 = p95(browser_values)
    if (
        probe.get("probe_version") != "durable_dispatcher_automatic_preemption_v1"
        or not isinstance(probe.get("candidate_launcher"), str)
        or not str(probe.get("candidate_launcher")).startswith("/")
        or probe.get("seatbelt_child") is not True
        or probe.get("sample_count") != len(samples)
        or probe.get("automatic_preemption_observed") is not True
        or probe.get("manual_owned_cleanup_performed") is not False
        or probe.get("p0_p1_deadline_misses") != deadline_misses
        or probe.get("interactive_queue_p95_ms") != interactive_p95
        or probe.get("p1_browser_queue_p95_seconds") != browser_p95
        or probe.get("orphan_process_groups") != orphan_groups
        or probe.get("duplicate_completions") != duplicates
        or probe.get("lost_jobs") != lost
        or probe.get("preempted_jobs_requeued") != requeued
        or probe.get("attempt_two_unique_completions") != completed
        or probe.get("production_state_accessed") is not False
    ):
        raise ResourcePerformanceEvidenceError(
            "preemption aggregate differs from raw automatic-dispatch observations"
        )
    passed = bool(
        deadline_misses == orphan_groups == duplicates == lost == 0
        and requeued == completed == len(samples)
    )
    return {
        "preemption_passed": passed,
        "automatic_preemption_observed": True,
        "independent_samples": len(samples),
        "p0_p1_deadline_misses": deadline_misses,
        "interactive_queue_p95_ms": round(interactive_p95, 6),
        "interactive_queue_p95_seconds": round(interactive_p95 / 1000, 9),
        "p1_browser_queue_p95_seconds": round(browser_p95, 9),
        "orphan_process_groups": orphan_groups,
        "duplicate_completions": duplicates,
        "lost_jobs": lost,
        "preempted_jobs_requeued": requeued,
        "attempt_two_unique_completions": completed,
        "missing_requirements": [],
    }


def _worker_metrics(
    probe: Mapping[str, Any], isolated_window_report: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    samples = probe.get("samples")
    if not isinstance(samples, list) or len(samples) < 3:
        raise ResourcePerformanceEvidenceError(
            "worker footprint probe requires three raw samples"
        )
    cycles: set[int] = set()
    jobs: set[str] = set()
    pids: set[int] = set()
    return_values: list[float] = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ResourcePerformanceEvidenceError(
                "worker footprint sample must be an object"
            )
        cycle = sample.get("cycle")
        job_id = sample.get("job_id")
        leader = sample.get("leader_pid")
        descendant = sample.get("descendant_pid")
        rss = _finite_nonnegative(
            sample.get("observed_group_rss_mb"), "worker group RSS"
        )
        footprint = _finite_nonnegative(
            sample.get("observed_group_physical_footprint_mb"),
            "worker group physical footprint",
        )
        budget = _finite_nonnegative(
            sample.get("return_budget_seconds"), "worker return budget"
        )
        returned = _finite_nonnegative(
            sample.get("return_seconds"), "worker return time"
        )
        if (
            type(cycle) is not int
            or cycle < 1
            or cycle in cycles
            or not isinstance(job_id, str)
            or not job_id
            or job_id in jobs
            or type(leader) is not int
            or type(descendant) is not int
            or leader <= 0
            or descendant <= 0
            or leader in pids
            or descendant in pids
            or leader == descendant
            or rss <= 0
            or footprint <= 0
            or budget != 30.0
            or returned > budget
            or sample.get("rss_source")
            != "libproc.proc_pid_rusage(RUSAGE_INFO_V4).ri_resident_size"
            or sample.get("physical_footprint_source")
            != "libproc.proc_pid_rusage(RUSAGE_INFO_V4).ri_phys_footprint"
            or type(sample.get("leader_proc_start_abstime")) is not int
            or sample.get("leader_proc_start_abstime") <= 0
            or type(sample.get("descendant_proc_start_abstime")) is not int
            or sample.get("descendant_proc_start_abstime") <= 0
            or sample.get("returncode") != 0
            or sample.get("timed_out") is not False
            or sample.get("killed") is not False
            or sample.get("process_group_gone") is not True
            or sample.get("leader_pid_gone") is not True
            or sample.get("descendant_pid_gone") is not True
            or sample.get("rss_returned_to_zero") is not True
            or sample.get("physical_footprint_returned_to_zero") is not True
            or sample.get("production_state_accessed") is not False
            or not isinstance(sample.get("sample_errors"), list)
        ):
            raise ResourcePerformanceEvidenceError(
                "worker footprint sample did not prove bounded process-group return"
            )
        cycles.add(cycle)
        jobs.add(job_id)
        pids.update((leader, descendant))
        return_values.append(returned)

    def p95(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[
            min(len(ordered) - 1, max(0, (95 * len(ordered) + 99) // 100 - 1))
        ]

    return_p95 = p95(return_values)
    if (
        probe.get("probe_version") != "owned_worker_group_footprint_return_v1"
        or not isinstance(probe.get("candidate_launcher"), str)
        or not str(probe.get("candidate_launcher")).startswith("/")
        or probe.get("seatbelt_child") is not True
        or probe.get("sample_count") != len(samples)
        or probe.get("return_budget_seconds") != 30.0
        or probe.get("return_p95_seconds") != return_p95
        or probe.get("metal_measurement_available") is not False
        or probe.get("magi_metal_mb") is not None
        or probe.get("metal_missing_reason")
        != "no validated non-privileged per-process Metal allocation-byte source"
        or probe.get("production_state_accessed") is not False
    ):
        raise ResourcePerformanceEvidenceError(
            "worker footprint/Metal aggregate differs from raw observations"
        )
    metal_window: dict[str, Any] | None = None
    if isolated_window_report is not None:
        try:
            metal_window = verify_isolated_resource_window(isolated_window_report)["g25"]
        except IsolatedResourceWindowError as exc:
            raise ResourcePerformanceEvidenceError(
                f"isolated Metal window failed: {exc}"
            ) from exc
    return {
        "rss_returned_to_baseline": True,
        "physical_footprint_returned_to_baseline": True,
        "metal_measurement_available": metal_window is not None,
        "metal_returned_to_baseline": metal_window is not None,
        "metal_measurement_scope": (
            metal_window.get("metal_measurement_scope")
            if metal_window is not None
            else "unavailable"
        ),
        "per_process_metal_bytes_available": False,
        "rss_return_window_measured": True,
        "physical_footprint_return_window_measured": True,
        "independent_samples": len(samples),
        "return_p95_seconds": round(return_p95, 6),
        "return_budget_seconds": 30.0,
        "missing_requirements": (
            []
            if metal_window is not None
            else ["isolated_v2_stopped_exclusive_agx_attribution_window"]
        ),
    }


def derive_metrics(
    report: Mapping[str, Any], *, release_files: Mapping[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """Derive only from raw observations; producer booleans are never inputs."""

    window = report.get("isolated_resource_window_report")
    isolated_window = window if isinstance(window, dict) else None
    return {
        GATE_IDS[0]: _performance_metrics(
            report.get("performance_report", {}),
            report.get("matched_production_report", {}),
            isolated_window,
            report.get("g8_transport_composition_receipt"),
            report.get("release_binding") if isinstance(report.get("release_binding"), dict) else None,
            release_files,
        ),
        GATE_IDS[1]: _resource_metrics(report.get("resource_probe", {}), isolated_window),
        GATE_IDS[2]: _preemption_metrics(report.get("preemption_probe", {})),
        GATE_IDS[3]: _worker_metrics(
            report.get("worker_capability_probe", {}), isolated_window
        ),
    }


def summarize_report(
    report: Mapping[str, Any],
    *,
    release_files: Mapping[str, str],
    python_runtime_sha256: str,
    expected_profile: Mapping[str, Any] | None = None,
    expected_release_id: str | None = None,
    expected_release_manifest_sha256: str | None = None,
) -> dict[str, dict[str, Any]]:
    claimed = report.get("evidence_sha256")
    unhashed = dict(report)
    unhashed.pop("evidence_sha256", None)
    if (
        report.get("schema") != SCHEMA
        or report.get("workload") != WORKLOAD
        or report.get("status") != "incomplete"
        or claimed != sha256_json(unhashed)
        or (expected_profile is not None and report.get("validation_profile") != expected_profile)
        or tuple(report.get("capability_gaps", ()))
        != (() if isinstance(report.get("isolated_resource_window_report"), dict) else EXPECTED_GAPS)
    ):
        raise ResourcePerformanceEvidenceError("partial report identity/profile/hash is invalid")
    binding = report.get("release_binding")
    window_binding = (
        report.get("isolated_resource_window_report", {}).get("release_binding", {})
        if isinstance(report.get("isolated_resource_window_report"), dict)
        else None
    )
    sources = {
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
    if (
        not isinstance(binding, dict)
        or binding.get("python_runtime_sha256") != python_runtime_sha256
        or binding.get("python_runtime_observed_sha256") != python_runtime_sha256
        or not isinstance(binding.get("python_runtime_realpath"), str)
        or not str(binding.get("python_runtime_realpath")).startswith("/")
        or report.get("preemption_probe", {}).get("candidate_launcher")
        != binding.get("python_runtime_realpath")
        or report.get("matched_production_report", {})
        .get("release_binding", {})
        .get("certifier_script_sha256")
        != binding.get("matched_perf_source_sha256")
        or report.get("matched_production_report", {})
        .get("release_binding", {})
        .get("python_executable_sha256")
        != python_runtime_sha256
        or (
            expected_release_id is not None
            and binding.get("release_id") != expected_release_id
        )
        or (
            expected_release_manifest_sha256 is not None
            and binding.get("release_manifest_sha256")
            != expected_release_manifest_sha256
        )
        or any(binding.get(field) != release_files.get(path) for field, path in sources.items())
        or (
            window_binding is not None
            and (
                window_binding.get("release_id") != binding.get("release_id")
                or window_binding.get("release_manifest_sha256")
                != binding.get("release_manifest_sha256")
                or window_binding.get("python_runtime_sha256") != python_runtime_sha256
                or window_binding.get("resource_policy_sha256")
                != release_files.get("config/v3_resource_policy.json")
                or report.get("isolated_resource_window_report", {})
                .get("execution_binding", {})
                .get("collector_source_sha256")
                != release_files.get(
                    "scripts/v3_validation/isolated_resource_window_collector.py"
                )
            )
        )
    ):
        raise ResourcePerformanceEvidenceError("partial report release/source binding failed")
    if report.get("safety") != {
        "live_state_accessed": False,
        "production_service_started": False,
        "production_port_accessed": False,
        "launchctl_invoked": False,
        "external_writes": False,
        "network_denied_by_seatbelt": True,
        "writes_restricted_to_sandbox": True,
        "models_loaded": False,
    }:
        raise ResourcePerformanceEvidenceError("partial report sandbox safety binding failed")
    metrics = derive_metrics(report, release_files=release_files)
    if report.get("metrics") != metrics:
        raise ResourcePerformanceEvidenceError("partial report metrics differ from recomputation")
    return metrics
