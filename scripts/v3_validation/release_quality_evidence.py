"""Pure validation and metric recomputation for release-quality inner reports."""

from __future__ import annotations

import hashlib
import json
import re
from fnmatch import fnmatch
from typing import Any, Mapping


SCHEMA = "magi.v3.release-quality-certification/v1"
WORKLOAD = "golden_business_flows"
EXPECTED_V3_SUITES = ("unit", "contract", "integration", "e2e")
EXPECTED_QUALITY_GROUPS = ("interaction", "agent_kernel", "memory", "quality")
EXPECTED_GOLDEN_SETS = ("context", "memory", "tool", "plan", "answer")
EXPECTED_FLOW_IDS = ("osc_preview_range_download_v1", "nas_office_provider_session_v1")
GOLDEN_DEPENDENCY_PATHS = (
    "tests/v3/compat/behavior_fixtures/osc-file-content.json",
    "docs/architecture/v3/generated/v2_runtime_routes.json",
    "scripts/v3_validation/route-method-review.json",
    "scripts/v3_validation/route-method-review-supplement.json",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseQualityEvidenceError(ValueError):
    """Raised when a quality transcript is incomplete, forged, or ambiguous."""


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


def _node_path(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _final_outcomes(
    transcript: Mapping[str, Any], *, python_runtime_sha256: str
) -> dict[str, str]:
    if transcript.get("schema_version") != 1:
        raise ReleaseQualityEvidenceError("pytest transcript schema is invalid")
    if (
        not SHA256_RE.fullmatch(python_runtime_sha256)
        or transcript.get("python_runtime_sha256") != python_runtime_sha256
        or transcript.get("python_runtime_realpath_sha256") != python_runtime_sha256
    ):
        raise ReleaseQualityEvidenceError("pytest transcript runtime binding is invalid")
    collected = transcript.get("collected_nodeids")
    reports = transcript.get("phase_reports")
    if (
        not isinstance(collected, list)
        or not collected
        or len(collected) != len(set(collected))
        or any(not isinstance(item, str) or not item for item in collected)
        or not isinstance(reports, list)
    ):
        raise ReleaseQualityEvidenceError("pytest collection is empty, duplicated, or invalid")
    phases: dict[str, dict[str, str]] = {nodeid: {} for nodeid in collected}
    for row in reports:
        if not isinstance(row, dict):
            raise ReleaseQualityEvidenceError("pytest phase report is invalid")
        nodeid = row.get("nodeid")
        when = row.get("when")
        outcome = row.get("outcome")
        if (
            nodeid not in phases
            or when not in {"setup", "call", "teardown"}
            or outcome not in {"passed", "failed", "skipped"}
            or when in phases[nodeid]
            or row.get("wasxfail") is not False
            or not isinstance(row.get("longrepr_sha256"), str)
            or not SHA256_RE.fullmatch(row["longrepr_sha256"])
        ):
            raise ReleaseQualityEvidenceError("pytest phase report identity is invalid")
        phases[nodeid][when] = outcome
    final: dict[str, str] = {}
    for nodeid, observed in phases.items():
        if not observed:
            raise ReleaseQualityEvidenceError(f"pytest node has no execution report: {nodeid}")
        if "failed" in observed.values():
            final[nodeid] = "failed"
        elif observed.get("setup") == "skipped" or observed.get("call") == "skipped":
            final[nodeid] = "skipped"
        elif observed == {"setup": "passed", "call": "passed", "teardown": "passed"}:
            final[nodeid] = "passed"
        else:
            raise ReleaseQualityEvidenceError(
                f"pytest node has no terminal call outcome: {nodeid}"
            )
    # Pytest exits successfully when every collected node either passes or is
    # skipped.  Strict no-skip policy is enforced below by the suite coverage
    # checks; do not misclassify a truthful successful pytest exit as transcript
    # drift before those checks can report the actual skipped-node failure.
    expected_exit = 0 if all(value != "failed" for value in final.values()) else 1
    exitstatus = transcript.get("pytest_exitstatus")
    if type(exitstatus) is not int or (exitstatus == 0) != (expected_exit == 0):
        raise ReleaseQualityEvidenceError("pytest exit status disagrees with node outcomes")
    return final


def _selected_release_paths(
    release_files: Mapping[str, str], manifest: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    v2 = manifest.get("v2_regression")
    suites = manifest.get("v3_suites")
    if not isinstance(v2, dict) or not isinstance(suites, dict):
        raise ReleaseQualityEvidenceError("quality suite manifest is incomplete")
    globs = v2.get("include_globs")
    if not isinstance(globs, list) or not globs or any(not isinstance(row, str) for row in globs):
        raise ReleaseQualityEvidenceError("V2 regression globs are invalid")
    v2_paths = sorted(
        path for path in release_files if any(fnmatch(path, pattern) for pattern in globs)
    )
    if not v2_paths:
        raise ReleaseQualityEvidenceError("V2 regression selection matched no release tests")
    if tuple(suites) != EXPECTED_V3_SUITES:
        raise ReleaseQualityEvidenceError("V3 suite names/order are invalid")
    v3_paths = sorted({path for rows in suites.values() for path in rows})
    declared_v3_paths = [path for rows in suites.values() for path in rows]
    if len(declared_v3_paths) != len(set(declared_v3_paths)):
        raise ReleaseQualityEvidenceError("V3 suite files overlap or are duplicated")
    for path in [*v2_paths, *v3_paths]:
        if path not in release_files or not path.startswith("tests/") or not path.endswith(".py"):
            raise ReleaseQualityEvidenceError(f"selected test is not release-bound: {path}")
    return v2_paths, v3_paths


def _evaluate_selection(
    final: Mapping[str, str], paths: list[str], description: str
) -> dict[str, int | bool]:
    path_set = set(paths)
    selected = {nodeid: outcome for nodeid, outcome in final.items() if _node_path(nodeid) in path_set}
    if not selected or {_node_path(nodeid) for nodeid in selected} != path_set:
        raise ReleaseQualityEvidenceError(f"{description} collection does not cover exact files")
    failed = sum(outcome == "failed" for outcome in selected.values())
    passed = sum(outcome == "passed" for outcome in selected.values())
    skipped = sum(outcome == "skipped" for outcome in selected.values())
    return {
        "collected": len(selected),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "passed_all_required": failed == 0 and skipped == 0 and passed > 0,
    }


def _verify_test_sources(
    report: Mapping[str, Any],
    release_files: Mapping[str, str],
    transcripts: tuple[Mapping[str, Any], Mapping[str, Any]],
) -> None:
    declared = report.get("test_source_sha256")
    observed_paths = sorted(
        {
            _node_path(nodeid)
            for transcript in transcripts
            for nodeid in transcript.get("collected_nodeids", [])
        }
    )
    expected = {path: release_files.get(path) for path in observed_paths}
    if (
        not isinstance(declared, dict)
        or any(value is None for value in expected.values())
        or any(not isinstance(value, str) or not SHA256_RE.fullmatch(value) for value in declared.values())
        or declared != expected
    ):
        raise ReleaseQualityEvidenceError("pytest source hashes are not release-bound")


def _verify_flow(flow: Mapping[str, Any], expected_id: str) -> None:
    claimed = flow.get("evidence_sha256")
    unhashed = dict(flow)
    unhashed.pop("evidence_sha256", None)
    if (
        flow.get("flow_id") != expected_id
        or flow.get("passed") is not True
        or flow.get("network_access_performed") is not False
        or flow.get("external_writes_performed") is not False
        or flow.get("sandbox_writes_only") is not True
        or flow.get("production_state_accessed") is not False
        or flow.get("service_start_performed") is not False
        or flow.get("staged_files_remaining") != 0
        or flow.get("expected_outcomes_sha256") != flow.get("observed_outcomes_sha256")
        or claimed != sha256_json(unhashed)
    ):
        raise ReleaseQualityEvidenceError(f"golden flow is not certifying: {expected_id}")


def _verify_side_effect_snapshot(snapshot: Mapping[str, Any]) -> tuple[int, int]:
    expected_offline = {
        effect: {"allowed": True, "execute": False}
        for effect in ("none", "read_only", "local_draft", "reversible_write", "external_commit", "destructive")
    }
    expected_live = {
        "none": {"allowed": True, "execute": True},
        "read_only": {"allowed": True, "execute": True},
        "local_draft": {"allowed": False, "execute": False},
        "reversible_write": {"allowed": False, "execute": False},
        "external_commit": {"allowed": False, "execute": False},
        "destructive": {"allowed": False, "execute": False},
    }
    expected_sandbox = {
        "local_draft": {"allowed": True, "execute": True},
        "reversible_write": {"allowed": True, "execute": True},
        "external_commit": {"allowed": False, "execute": False},
        "destructive": {"allowed": False, "execute": False},
    }
    if snapshot != {
        "offline": expected_offline,
        "isolated_live_default": expected_live,
        "isolated_live_explicit_sandbox": expected_sandbox,
    }:
        return 1, 0
    return 0, 0


def summarize_report(
    report: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
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
        or report.get("status") != "certified"
        or claimed != sha256_json(unhashed)
        or (expected_profile is not None and report.get("validation_profile") != expected_profile)
    ):
        raise ReleaseQualityEvidenceError("release quality report identity/profile/hash is invalid")
    binding = report.get("release_binding")
    expected_sources = {
        "certifier_script_sha256": "scripts/v3_validation/release_quality_certification.py",
        "evidence_module_sha256": "scripts/v3_validation/release_quality_evidence.py",
        "pytest_plugin_sha256": "scripts/v3_validation/pytest_transcript_plugin.py",
        "suite_manifest_sha256": "config/v3_release_quality_suites.json",
        "golden_flows_sha256": "scripts/v3_validation/golden_flows.py",
        "side_effects_sha256": "scripts/v3_validation/side_effects.py",
    }
    if (
        not isinstance(binding, dict)
        or binding.get("python_runtime_sha256") != python_runtime_sha256
        or binding.get("python_runtime_observed_sha256") != python_runtime_sha256
        or (
            expected_release_id is not None
            and binding.get("release_id") != expected_release_id
        )
        or (
            expected_release_manifest_sha256 is not None
            and binding.get("release_manifest_sha256")
            != expected_release_manifest_sha256
        )
        or any(binding.get(field) != release_files.get(path) for field, path in expected_sources.items())
    ):
        raise ReleaseQualityEvidenceError("release quality source/runtime binding failed")
    dependencies = report.get("golden_dependency_sha256")
    expected_dependencies = {
        path: release_files.get(path) for path in GOLDEN_DEPENDENCY_PATHS
    }
    if (
        any(value is None for value in expected_dependencies.values())
        or dependencies != expected_dependencies
    ):
        raise ReleaseQualityEvidenceError("golden flow dependencies are not release-bound")
    safety = report.get("safety")
    if safety != {
        "live_state_accessed": False,
        "production_service_started": False,
        "production_port_accessed": False,
        "launchctl_invoked": False,
        "external_writes": False,
        "network_denied_by_seatbelt": True,
        "writes_restricted_to_sandbox": True,
        "pytest_home_isolated": True,
    }:
        raise ReleaseQualityEvidenceError("release quality sandbox safety binding failed")
    runs = report.get("pytest_runs")
    if not isinstance(runs, dict) or set(runs) != {"v2_regression", "v3_suites"}:
        raise ReleaseQualityEvidenceError("release quality pytest runs are missing")
    v2_transcript = runs["v2_regression"]
    v3_transcript = runs["v3_suites"]
    if not isinstance(v2_transcript, dict) or not isinstance(v3_transcript, dict):
        raise ReleaseQualityEvidenceError("release quality pytest transcript is invalid")
    v2_definition = manifest.get("v2_regression")
    if not isinstance(v2_definition, dict):
        raise ReleaseQualityEvidenceError("V2 regression definition is invalid")
    v2_mode = str(v2_definition.get("mode", "required"))
    if v2_mode not in {"required", "retired_baseline_v3_compatibility"}:
        raise ReleaseQualityEvidenceError(f"unsupported V2 regression mode: {v2_mode}")
    if (
        v2_mode == "retired_baseline_v3_compatibility"
        and v2_transcript.get("execution_scope")
        != "v3_compatibility_boundary"
    ):
        raise ReleaseQualityEvidenceError(
            "retired V2 regression is not projected from the V3 compatibility boundary"
        )
    v2_final = _final_outcomes(
        v2_transcript, python_runtime_sha256=python_runtime_sha256
    )
    v3_final = _final_outcomes(
        v3_transcript, python_runtime_sha256=python_runtime_sha256
    )
    _verify_test_sources(report, release_files, (v2_transcript, v3_transcript))
    v2_paths, v3_paths = _selected_release_paths(release_files, manifest)
    if {_node_path(item) for item in v2_final} != set(v2_paths):
        raise ReleaseQualityEvidenceError("V2 regression did not collect the exact release test set")
    if {_node_path(item) for item in v3_final} != set(v3_paths):
        raise ReleaseQualityEvidenceError("V3 suites did not collect the exact manifest test set")
    v2 = _evaluate_selection(v2_final, v2_paths, "V2 regression")
    minimum_v2_passed = manifest["v2_regression"].get("minimum_passed")
    if (
        type(minimum_v2_passed) is not int
        or minimum_v2_passed < 1
        or int(v2["passed"]) < minimum_v2_passed
        or v2["passed_all_required"] is not True
    ):
        raise ReleaseQualityEvidenceError("V2 release regression is not strictly passing")
    suite_results = {
        name: _evaluate_selection(v3_final, list(manifest["v3_suites"][name]), f"V3 {name}")
        for name in EXPECTED_V3_SUITES
    }
    quality_manifest = manifest.get("quality_contract_groups")
    golden_manifest = manifest.get("golden_sets")
    if (
        not isinstance(quality_manifest, dict)
        or tuple(quality_manifest) != EXPECTED_QUALITY_GROUPS
        or not isinstance(golden_manifest, dict)
        or tuple(golden_manifest) != EXPECTED_GOLDEN_SETS
    ):
        raise ReleaseQualityEvidenceError("quality/golden manifest groups are invalid")
    if tuple(manifest.get("golden_flow_ids", ())) != EXPECTED_FLOW_IDS:
        raise ReleaseQualityEvidenceError("golden flow manifest IDs are invalid")
    def evaluate_declared_paths(paths: list[str], description: str) -> dict[str, int | bool]:
        path_set = set(paths)
        if path_set <= set(v2_paths):
            return _evaluate_selection(v2_final, paths, description)
        if path_set <= set(v3_paths):
            return _evaluate_selection(v3_final, paths, description)
        raise ReleaseQualityEvidenceError(
            f"{description} paths are not covered by either release transcript"
        )

    quality_results = {
        name: evaluate_declared_paths(list(quality_manifest[name]), f"quality {name}")
        for name in EXPECTED_QUALITY_GROUPS
    }
    golden_results = {
        name: evaluate_declared_paths(list(golden_manifest[name]), f"golden {name}")
        for name in EXPECTED_GOLDEN_SETS
    }
    flows = report.get("golden_flows")
    if not isinstance(flows, list) or len(flows) != 2:
        raise ReleaseQualityEvidenceError("golden flow reports are incomplete")
    for flow, expected_id in zip(flows, EXPECTED_FLOW_IDS, strict=True):
        if not isinstance(flow, dict):
            raise ReleaseQualityEvidenceError("golden flow report is invalid")
        _verify_flow(flow, expected_id)
    if (
        flows[0].get("fixture_sha256")
        != expected_dependencies[GOLDEN_DEPENDENCY_PATHS[0]]
        or not all(
            SHA256_RE.fullmatch(str(flow.get("inventory_fingerprint") or ""))
            for flow in flows
        )
        or len({flow.get("inventory_fingerprint") for flow in flows}) != 1
    ):
        raise ReleaseQualityEvidenceError("golden flow fixture/inventory binding failed")
    diff_count, duplicate_count = _verify_side_effect_snapshot(
        report.get("side_effect_snapshot", {})
    )
    reviewed_keys: set[tuple[str, str, str, str]] = set()
    for flow in flows:
        for row in flow.get("reviewed_routes", []):
            if not isinstance(row, dict):
                raise ReleaseQualityEvidenceError("golden flow route review is invalid")
            key = (
                str(row.get("service") or ""),
                str(row.get("rule") or ""),
                str(row.get("method") or ""),
                str(row.get("endpoint") or ""),
            )
            if key in reviewed_keys:
                duplicate_count += 1
            reviewed_keys.add(key)
    if any(
        row["passed_all_required"] is not True
        for row in (*suite_results.values(), *quality_results.values(), *golden_results.values())
    ):
        raise ReleaseQualityEvidenceError("required release quality tests are not strictly passing")
    if diff_count != 0 or duplicate_count != 0:
        raise ReleaseQualityEvidenceError("golden side-effect diff is not strictly approved")
    return {
        "v2_regression_passed_in_release_venv": {
            "release_venv_verified": True,
            "passed": v2["passed"],
            "failed": int(v2["failed"]) + int(v2["skipped"]),
            "execution_mode": v2_mode,
        },
        "v3_unit_contract_integration_e2e_passed": {
            "failed": sum(
                int(row["failed"]) + int(row["skipped"])
                for row in suite_results.values()
            ),
            "suites": list(EXPECTED_V3_SUITES),
            "all_required_suites_passed": all(
                row["passed_all_required"] is True for row in suite_results.values()
            ),
        },
        "interaction_agent_kernel_memory_quality_contracts_passed": {
            "failed_contracts": sum(
                int(row["failed"]) + int(row["skipped"])
                for row in quality_results.values()
            ),
            "contract_groups": list(EXPECTED_QUALITY_GROUPS),
            "quality_non_regression_passed": all(
                row["passed_all_required"] is True for row in quality_results.values()
            ),
        },
        "context_memory_tool_plan_answer_golden_sets_passed": {
            "failed_cases": sum(
                int(row["failed"]) + int(row["skipped"])
                for row in golden_results.values()
            ),
            "sets": list(EXPECTED_GOLDEN_SETS),
            "all_sets_passed": all(
                row["passed_all_required"] is True for row in golden_results.values()
            ),
        },
        "golden_side_effect_diff_approved": {
            "unapproved_contract_diffs": diff_count,
            "duplicate_side_effects": duplicate_count,
            "golden_diff_completed": True,
        },
    }
