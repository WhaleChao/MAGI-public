from __future__ import annotations

import json
from pathlib import Path

from scripts.v3_release_gate import ACTIVE_V3_EVIDENCE_IDS, EVIDENCE_SPECS


ROOT = Path(__file__).resolve().parents[2]


def test_rc643_promotion_is_single_pass_v3_only() -> None:
    campaign = json.loads(
        (ROOT / "config/v3_validation_campaign.json").read_text(encoding="utf-8")
    )
    offline = campaign["offline_campaign"]

    assert campaign["production_release"] == "v3"
    assert offline["validation_strategy"] == (
        "targeted_v3_once_with_production_observation"
    )
    assert offline["required_independent_passes"] == 1
    assert len(offline["validation_pass_profiles"]) == 1
    assert {
        "matched_v2_v3_performance",
        "ime_candidate_window_pressure_probe",
    }.isdisjoint(offline["workloads"])


def test_rc643_gate_has_no_legacy_v2_or_retired_probe_requirement() -> None:
    gates = json.loads(
        (ROOT / "config/v3_cutover_gates.json").read_text(encoding="utf-8")
    )
    retired = {
        "v2_regression_passed_in_release_venv",
        "matched_v2_warm_cold_performance_baseline_complete",
        "resource_policy_all_budgets_passed",
        "heavy_plus_interactive_preemption_benchmark_passed",
        "v2_fully_stopped_before_v3_start_verified",
        "v3_fully_stopped_before_v2_rollback_verified",
        "input_method_candidate_window_probe_passed",
    }

    assert gates["required_evidence"] == list(ACTIVE_V3_EVIDENCE_IDS)
    assert list(EVIDENCE_SPECS) == list(ACTIVE_V3_EVIDENCE_IDS)
    assert retired.isdisjoint(gates["required_evidence"])
    assert gates["promotion_thresholds"]["offline_replay_independent_passes"] == 1
    assert gates["promotion_thresholds"]["minimum_isolated_live_validation_runs"] == 1


def test_release_quality_suite_does_not_execute_retired_cutover_campaigns() -> None:
    suites = json.loads(
        (ROOT / "config/v3_release_quality_suites.json").read_text(encoding="utf-8")
    )
    targets = {
        target
        for group in suites["v3_suites"].values()
        for target in group
    }
    retired_targets = {
        "tests/v3/test_pre_cutover.py",
        "tests/v3/test_provisional_resource_window_execute.py",
        "tests/v3/test_isolated_live_execute.py",
        "tests/v3/test_release_gate.py",
    }

    assert suites["legacy_v2_validation"]["mode"] == "disabled"
    assert retired_targets.isdisjoint(targets)
