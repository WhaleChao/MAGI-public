from __future__ import annotations

import copy
import json
import socket

import pytest

from scripts.v3_campaign.runner import CampaignSafetyError, _structured_workload_evidence
from scripts.v3_validation.inventory import EXPECTED_FINGERPRINT
from scripts.v3_validation.perf_compat import (
    BLOCKER_CODE,
    BUSINESS_ROUTE,
    FIXTURE_VERSION,
    PINNED_ROUTE,
    PerfEvidenceError,
    SyntheticOscDatabase,
    build_actual_osc_cases_app,
    _network_blocked,
    build_actual_livez_app,
    build_offline_app,
    measure_mode,
    run_benchmark,
    validate_and_compare,
    verify_evidence_hash,
)


EVIDENCE_PREFIX = "MAGI_V3_OFFLINE_EVIDENCE="


def test_legacy_perf_envelope_cannot_impersonate_release_bound_partial_evidence() -> None:
    report = run_benchmark(warmup=100, iterations=1000, repeats=3)
    evidence = {
        "schema_version": 1,
        "workload": "matched_v2_v3_performance",
        "probe": "sequential_actual_v2_livez_vs_native_v3_gateway_livez",
        "status": "passed",
        "measurements": {
            "repeats": 3,
            "requests_per_arm_per_repeat": 1000,
            "response_drift": 0,
            "gateway_thresholds_passed": report["gateway_threshold_evaluation"]["passed"],
            "native_v3_handler_measured": True,
            "production_business_workload_measured": False,
            "synthetic_business_corpus_defined": True,
            "native_business_handler_available": True,
            "synthetic_business_get_measured": True,
            "synthetic_business_post_measured": True,
        },
        "report": report,
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
        "live_state_access_performed": False,
    }
    encoded = EVIDENCE_PREFIX + json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    with pytest.raises(CampaignSafetyError, match="partial probe is invalid"):
        _structured_workload_evidence("matched_v2_v3_performance", encoded)
    assert report["gate"]["decision"] == "blocker_retained"
    print(encoded)


def _inventory_evidence() -> dict[str, str]:
    return {"fingerprint": EXPECTED_FINGERPRINT}


def test_fixture_preserves_the_pinned_route_identity_and_deterministic_response() -> None:
    app = build_offline_app()
    rules = [rule for rule in app.url_map.iter_rules() if rule.endpoint != "static"]

    assert len(rules) == 1
    assert str(rules[0].rule) == PINNED_ROUTE.rule
    assert rules[0].endpoint == PINNED_ROUTE.endpoint
    assert "GET" in rules[0].methods

    response = app.test_client().get(
        "/livez?case=proof",
        headers={"X-MAGI-Perf-Case": "trace-proof"},
    )
    assert response.status_code == 200
    assert response.headers["X-MAGI-Perf-Fixture"] == FIXTURE_VERSION
    assert response.get_json() == {
        "case": "proof",
        "fixture": FIXTURE_VERSION,
        "method": "GET",
        "path": "/livez",
        "trace": "trace-proof",
    }


def test_direct_and_compat_arms_prove_identical_workload_and_responses() -> None:
    direct = measure_mode("v2_direct_wsgi", warmup=3, iterations=12)
    compat = measure_mode("v3_compat_wsgi", warmup=3, iterations=12)

    validation = validate_and_compare(
        {"v2_direct_wsgi": [direct], "v3_compat_wsgi": [compat]},
        inventory_evidence=_inventory_evidence(),
    )

    assert validation["comparison_valid"] is True
    assert validation["workload_equivalent"] is True
    assert validation["responses_correct"] is True
    assert direct["request_plan_sha256"] == compat["request_plan_sha256"]
    assert direct["response_sequence_sha256"] == compat["response_sequence_sha256"]
    assert direct["workload_source_sha256"] == compat["workload_source_sha256"]
    assert direct["runtime"]["executable_sha256"] == compat["runtime"]["executable_sha256"]


def test_response_drift_is_a_hard_blocker_before_comparison() -> None:
    direct = measure_mode("v2_direct_wsgi", warmup=0, iterations=4)
    compat = copy.deepcopy(direct)
    compat["mode"] = "v3_compat_wsgi"
    compat["response_sequence_sha256"] = "0" * 64

    with pytest.raises(PerfEvidenceError, match="response_sequence_sha256 differs"):
        validate_and_compare(
            {"v2_direct_wsgi": [direct], "v3_compat_wsgi": [compat]},
            inventory_evidence=_inventory_evidence(),
        )


def test_wrong_worker_assignment_is_rejected() -> None:
    direct = measure_mode("v2_direct_wsgi", warmup=0, iterations=4)
    misplaced = copy.deepcopy(direct)

    with pytest.raises(PerfEvidenceError, match="wrong mode"):
        validate_and_compare(
            {"v2_direct_wsgi": [direct], "v3_compat_wsgi": [misplaced]},
            inventory_evidence=_inventory_evidence(),
        )


def test_network_guard_prevents_connections() -> None:
    with _network_blocked(), pytest.raises(PerfEvidenceError, match="network connection"):
        socket.create_connection(("127.0.0.1", 9), timeout=0.01)


def test_actual_production_livez_blueprint_runs_without_service_or_external_dependencies(
    tmp_path,
) -> None:
    app = build_actual_livez_app(tmp_path)
    response = app.test_client().get("/livez")

    assert response.status_code == 200
    assert response.get_json()["probe"] == "liveness"
    rule = next(rule for rule in app.url_map.iter_rules() if str(rule.rule) == "/livez")
    assert rule.endpoint == PINNED_ROUTE.endpoint
    view = app.view_functions[rule.endpoint]
    assert view.__module__ == "api.blueprints.admin_runtime"
    assert view.__name__ == "livez"


def test_actual_handler_direct_and_compat_arms_normalize_only_dynamic_probe_fields() -> None:
    direct = measure_mode(
        "v2_direct_wsgi", warmup=1, iterations=4, workload="actual_v2_livez"
    )
    compat = measure_mode(
        "v3_compat_wsgi", warmup=1, iterations=4, workload="actual_v2_livez"
    )
    validation = validate_and_compare(
        {"v2_direct_wsgi": [direct], "v3_compat_wsgi": [compat]},
        inventory_evidence=_inventory_evidence(),
    )

    assert validation["responses_correct"] is True
    assert direct["workload"] == compat["workload"] == "actual_v2_livez"
    assert direct["response_sequence_sha256"] == compat["response_sequence_sha256"]
    assert direct["safety"]["production_handler_module_imported"] is True
    assert direct["safety"]["production_service_imported"] is False


def test_actual_v2_and_native_v3_gateway_handlers_share_semantic_livez_contract() -> None:
    v2 = measure_mode(
        "v2_actual_livez_wsgi", warmup=1, iterations=4, workload="native_gateway_livez"
    )
    v3 = measure_mode(
        "v3_native_gateway_livez_wsgi",
        warmup=1,
        iterations=4,
        workload="native_gateway_livez",
    )
    validation = validate_and_compare(
        {
            "v2_actual_livez_wsgi": [v2],
            "v3_native_gateway_livez_wsgi": [v3],
        },
        inventory_evidence=_inventory_evidence(),
    )

    assert validation["same_semantic_response_contract"] is True
    assert v2["response_sequence_sha256"] == v3["response_sequence_sha256"]
    assert v2["handler_identity"]["implementation"] == "production_v2"
    assert v3["handler_identity"]["implementation"] == "native_v3"
    assert v2["handler_identity"]["source_sha256"] != v3["handler_identity"]["source_sha256"]
    assert v2["file_descriptors"]["drift"] == 0
    assert v3["file_descriptors"]["drift"] == 0


def test_actual_v2_and_native_v3_osc_handlers_match_get_post_and_side_effects() -> None:
    v2 = measure_mode(
        "v2_actual_osc_cases_wsgi",
        warmup=1,
        iterations=4,
        workload="synthetic_osc_cases",
    )
    v3 = measure_mode(
        "v3_native_osc_cases_wsgi",
        warmup=1,
        iterations=4,
        workload="synthetic_osc_cases",
    )
    validation = validate_and_compare(
        {
            "v2_actual_osc_cases_wsgi": [v2],
            "v3_native_osc_cases_wsgi": [v3],
        },
        inventory_evidence=_inventory_evidence(),
    )

    assert validation["same_semantic_response_contract"] is True
    assert v2["response_sequence_sha256"] == v3["response_sequence_sha256"]
    assert v2["pinned_route"] == v3["pinned_route"] == {
        "service": "5002",
        "rule": "/api/osc/cases",
        "methods": ["GET", "POST"],
        "endpoint": "osc_cases.osc_cases_api",
    }
    assert v2["handler_identity"]["implementation"] == "production_v2"
    assert v3["handler_identity"]["implementation"] == "native_v3"
    for key in (
        "database",
        "row_count",
        "corpus_sha256",
        "read_only",
        "disposable",
        "measured_methods",
        "unmeasured_methods",
    ):
        assert v2["synthetic_corpus"][key] == v3["synthetic_corpus"][key]
    assert v2["synthetic_corpus"]["measured_methods"] == ["GET", "POST"]
    assert v2["synthetic_corpus"]["unmeasured_methods"] == []
    assert v2["synthetic_corpus"]["read_only"] is False
    assert v2["synthetic_corpus"]["disposable"] is True
    assert (
        v2["synthetic_corpus"]["side_effect_transcript"]
        == v3["synthetic_corpus"]["side_effect_transcript"]
    )
    transcript = v2["synthetic_corpus"]["side_effect_transcript"]
    assert transcript["balanced_transactions"] is True
    assert transcript["post_transaction_count"] == 2
    assert transcript["transaction_event_counts"] == {
        "begin": 2,
        "insert": 0,
        "update": 2,
        "commit": 2,
        "rollback": 0,
    }
    assert transcript["target_state"]["notes"] == "production-shaped-post-fixture"
    assert transcript["external_writes"] is False
    assert transcript["production_state_accessed"] is False
    assert transcript["nas_accessed"] is False
    assert v2["cold_start"]["latency_us"] > 0
    assert v3["cold_start"]["latency_us"] > 0
    assert v2["file_descriptors"]["drift"] == v3["file_descriptors"]["drift"] == 0


def test_v2_synthetic_database_boundary_rejects_unbounded_mutation() -> None:
    database = SyntheticOscDatabase()
    with pytest.raises(PerfEvidenceError, match="bounded SELECT/INSERT/UPDATE"):
        database.v2_exec("UPDATE cases SET client_name='forbidden'", fetch="none")


def test_actual_v2_osc_app_retains_production_route_identity() -> None:
    target = build_actual_osc_cases_app()
    rule = next(
        rule
        for rule in target.app.url_map.iter_rules()
        if str(rule.rule) == BUSINESS_ROUTE.rule
    )
    view = target.app.view_functions[rule.endpoint]
    assert rule.endpoint == BUSINESS_ROUTE.endpoint
    assert {"GET", "POST"}.issubset(rule.methods)
    assert view.__module__ == "api.blueprints.osc_cases"
    assert view.__name__ == "osc_cases_api"


def test_isolated_end_to_end_report_retains_the_full_performance_blocker() -> None:
    report = run_benchmark(warmup=3, iterations=16, repeats=2)

    assert report["offline"] is True
    assert report["equivalence_proof"]["comparison_valid"] is True
    assert report["equivalence_proof"]["workload_equivalent"] is True
    assert report["equivalence_proof"]["responses_correct"] is True
    assert report["execution_order"] == [
        "v2_actual_livez_wsgi",
        "v3_native_gateway_livez_wsgi",
        "v3_native_gateway_livez_wsgi",
        "v2_actual_livez_wsgi",
        "v2_actual_osc_cases_wsgi",
        "v3_native_osc_cases_wsgi",
        "v3_native_osc_cases_wsgi",
        "v2_actual_osc_cases_wsgi",
    ]
    assert report["comparison"]["latency_p50_direct_us"] > 0
    assert report["comparison"]["latency_p50_compat_us"] > 0
    assert report["comparison"]["latency_p99_direct_us"] > 0
    assert report["comparison"]["latency_p99_compat_us"] > 0
    assert report["comparison"]["cold_latency_direct_us"] > 0
    assert report["comparison"]["cold_latency_compat_us"] > 0
    assert report["workload"] == "native_gateway_livez"
    assert report["gateway_threshold_evaluation"]["passed"] is True
    assert all(
        row["passed"] for row in report["gateway_threshold_evaluation"]["checks"].values()
    )
    assert report["gate"]["blocker_code"] == BLOCKER_CODE
    assert report["gate"]["decision"] == "blocker_retained"
    assert report["gate"]["threshold_scope"] == "gateway_livez_native_v2_v3_only"
    assert report["claim_coverage"]["production_v2_handler"] is True
    assert report["claim_coverage"]["native_v3_handler"] is True
    assert report["claim_coverage"]["native_v3_gateway_probe"] is True
    assert report["claim_coverage"]["production_business_workload"] is False
    assert report["claim_coverage"]["matched_native_business_handler"] is True
    assert report["claim_coverage"]["synthetic_business_get_measured"] is True
    assert report["claim_coverage"]["synthetic_business_post_measured"] is True
    assert report["claim_coverage"]["representative_synthetic_business_corpus_defined"] is True
    assert report["claim_coverage"]["rss_evidence"] is True
    assert report["claim_coverage"]["file_descriptor_evidence"] is True
    corpus = report["representative_business_corpus"]
    assert corpus["synthetic_only"] is True
    assert corpus["request_count"] == 2
    assert corpus["v2_handler"]["route"]["endpoint"] == BUSINESS_ROUTE.endpoint
    assert corpus["v3_native_handler"]["composed_in_service_manifest"] is False
    assert corpus["matched_measurement_status"] == "matched_synthetic_get_and_post_measured"
    assert corpus["measured_methods"] == ["GET", "POST"]
    assert corpus["unmeasured_methods"] == []
    assert corpus["requests"][1]["body"]["auto_create_folder"] is False
    assert corpus["architecture_gap"]["code"] == (
        "NATIVE_OSC_CASES_NOT_COMPOSED_IN_SERVICE_MANIFEST"
    )
    assert corpus["architecture_gap"]["gateway_application_factories"] == {
        "main_http": "magi_v3.compat:create_main_app",
        "tools_http": "magi_v3.compat:create_tools_app",
    }
    assert len(report["evidence_sha256"]) == 64
    business = report["synthetic_business_benchmark"]
    assert business["production_business_workload"] is False
    assert business["release_thresholds_applied"] is False
    assert business["response_projection_equivalent"] is True
    assert business["same_python_runtime"] is True
    assert business["measured_methods"] == ["GET", "POST"]
    assert business["unmeasured_methods"] == []
    assert (
        business["side_effect_transcript"]["v2_actual_osc_cases_wsgi"]
        == business["side_effect_transcript"]["v3_native_osc_cases_wsgi"]
    )
    assert business["comparison"]["latency_p50_v2_us"] > 0
    assert business["comparison"]["latency_p95_native_v3_us"] > 0
    assert business["comparison"]["latency_p99_native_v3_us"] > 0
    assert business["comparison"]["cold_start_v2_us"] > 0
    assert business["comparison"]["cold_start_native_v3_us"] > 0
    verify_evidence_hash(report)
    for mode_runs in report["runs"].values():
        for run in mode_runs:
            assert run["safety"] == {
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
            }
            assert run["file_descriptors"]["drift"] == 0


def test_performance_evidence_hash_fails_closed_after_tampering() -> None:
    report = run_benchmark(warmup=1, iterations=4, repeats=1)
    report["claim_coverage"]["native_v3_handler"] = False

    with pytest.raises(PerfEvidenceError, match="does not match"):
        verify_evidence_hash(report)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"warmup": -1, "iterations": 1, "repeats": 1}, "warmup"),
        ({"warmup": 0, "iterations": 0, "repeats": 1}, "iterations"),
        ({"warmup": 0, "iterations": 1, "repeats": 0}, "repeats"),
    ],
)
def test_invalid_bounds_are_rejected(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(PerfEvidenceError, match=message):
        run_benchmark(**kwargs)
