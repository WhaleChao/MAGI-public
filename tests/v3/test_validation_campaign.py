from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN = ROOT / "config" / "v3_validation_campaign.json"


def test_campaign_is_armed_for_certifying_offline_only_and_single_active() -> None:
    campaign = json.loads(CAMPAIGN.read_text(encoding="utf-8"))

    assert campaign["campaign_state"] == "certifying_offline"
    assert campaign["armed"] is True
    assert campaign["production_release"] == "v2"
    assert campaign["maximum_simultaneously_active_magi_releases"] == 1
    offline = campaign["offline_campaign"]
    assert offline["validation_strategy"] == "accelerated_24h_event_coverage"
    assert offline["maximum_completion_hours"] == 24
    assert offline["minimum_consecutive_days"] == 1
    assert offline["required_independent_passes"] >= 7
    profiles = offline["validation_pass_profiles"]
    assert len(profiles) == offline["required_independent_passes"]
    assert len({item["profile_id"] for item in profiles}) == len(profiles)
    assert len({item["replay_start_local"] for item in profiles}) == len(profiles)
    assert len({item["fault_seed"] for item in profiles}) == len(profiles)
    live = campaign["isolated_live_validation"]
    assert live["required_runs"] >= 3
    assert live["completion_window_hours"] <= 24
    assert live["minimum_reset_minutes"] >= 10
    assert live["allowed_window"] == {"start": "00:00", "end": "23:59"}
    assert live["allowed_local_dates"] == ["2026-07-22"]
    assert live["external_writes_enabled"] is False
    assert live["abort_if_any_owner_overlap"] is True
    sequence = live["same_host_sequence"]
    assert sequence.index("stop_v2_completely") < sequence.index("start_v3_validation_once")
    assert sequence.index("stop_v3_completely") < sequence.index("restore_v2_once")
    replacement = campaign["final_replacement"]
    assert replacement["schedule_state"] == "not_scheduled_until_all_hard_gates_pass"
    assert replacement["requires_decision"] == "GO"
    commands = {
        "campaign": replacement["campaign_runner_command"],
        "compiler": replacement["evidence_compiler_command"],
        "gate": replacement["release_gate_command"],
    }
    for command in commands.values():
        assert command.startswith("<candidate>/bin/magi-v3-python <candidate>/")
        assert not command.startswith("python ")
        for required_flag in (
            "--campaign-id",
            "--release-sha",
            "--hardware-id",
            "--gate-config-sha256",
        ):
            assert required_flag in command
    assert "<candidate>/scripts/v3_campaign/runner.py" in commands["campaign"]
    assert "--release-root <candidate>" in commands["campaign"]
    assert "<candidate>/scripts/v3_evidence_compiler.py" in commands["compiler"]
    assert "--release-root <candidate>" in commands["compiler"]
    assert "--campaign-report <campaign-report>" in commands["compiler"]
    assert "<candidate>/scripts/v3_release_gate.py" in commands["gate"]
    assert "--config <candidate>/config/v3_cutover_gates.json" in commands["gate"]


def test_all_offline_workloads_are_certified_and_historical_blockers_remain_auditable() -> None:
    campaign = json.loads(CAMPAIGN.read_text(encoding="utf-8"))
    harness = campaign["offline_campaign"]["harness_certification"]
    active_blockers = {item["workload"]: item for item in harness["arming_blockers"]}
    blockers = {
        item["workload"]: item for item in harness["historical_arming_blockers"]
    }
    verified = {item["workload"]: item for item in harness["verified_workloads"]}

    assert harness["status"] == "certified"
    assert active_blockers == {}
    assert set(blockers) == {
        "seven_day_schedule_10x_arrival_2x_duration_replay",
        "fault_injection",
        "matched_v2_v3_performance",
    }
    assert set(verified) == {
        "golden_business_flows",
        "health_1000_model_free",
        "hundred_cycle_worker_reap_soak",
        "ime_candidate_window_pressure_probe",
        "346_route_contract_replay",
        "seven_day_schedule_10x_arrival_2x_duration_replay",
        "fault_injection",
        "fault_recovery_certification",
        "matched_v2_v3_performance",
    }
    route = verified["346_route_contract_replay"]
    assert route["evidence_schema"] == "magi.v3.route-certification/v1"
    assert route["test_target"] == "scripts/v3_validation/route_certification.py"
    assert route["required_measurements"] == [
        "pinned_routes",
        "fully_replayed_routes",
        "remaining_routes",
        "pinned_route_methods",
        "representative_success_path_passed",
        "remaining_route_methods",
        "validation_profile_id",
    ]
    assert verified["hundred_cycle_worker_reap_soak"]["test_target"] == (
        "tests/v3/test_campaign_offline_probes.py"
    )
    assert verified["ime_candidate_window_pressure_probe"]["test_target"] == (
        "tests/v3/test_ime_candidate_native.py"
    )
    assert blockers["seven_day_schedule_10x_arrival_2x_duration_replay"]["partial_evidence"][
        "measured_ledger_arrivals"
    ] == 55470
    schedule_evidence = blockers["seven_day_schedule_10x_arrival_2x_duration_replay"][
        "partial_evidence"
    ]
    assert schedule_evidence["representative_body_coverage_source"] == (
        "bound_schedule_realism_evidence"
    )
    assert schedule_evidence["production_p95_coverage_source"] == (
        "bound_schedule_realism_baseline"
    )
    assert schedule_evidence["global_production_p95_available"] is False
    assert blockers["fault_injection"]["partial_evidence"]["scenarios_passed"] == 6
    assert blockers["fault_injection"]["partial_evidence"]["sigkill_commit_window_cycles"] == 12
    assert blockers["fault_injection"]["partial_evidence"][
        "logical_transaction_boundary_sigkill_stages"
    ] == 37
    assert blockers["fault_injection"]["partial_evidence"][
        "bounded_time_offset_sigkill_offsets_us"
    ] == [0, 50, 250, 1000, 5000, 20000]
    assert blockers["fault_injection"]["partial_evidence"][
        "sqlite_wal_synchronous_full_verified"
    ] is True
    assert blockers["fault_injection"]["partial_evidence"]["physical_apfs_enospc"] is False
    assert blockers["fault_injection"]["partial_evidence"][
        "sqlite_vfs_fsync_io_error_injection"
    ] is True
    assert blockers["fault_injection"]["partial_evidence"]["test_targets"] == [
        "tests/v3/test_fault_realism.py",
        "tests/v3/test_campaign_offline_probes.py",
        "tests/v3/test_fault_certification.py",
        "tests/v3/test_physical_fault_drill.py",
    ]
    assert verified["golden_business_flows"]["test_target"] == (
        "tests/v3/test_actual_route_replay.py"
    )
    assert blockers["matched_v2_v3_performance"]["partial_evidence"]["test_target"] == (
        "tests/v3/test_perf_compat.py"
    )
    assert blockers["matched_v2_v3_performance"]["partial_evidence"][
        "gateway_thresholds_passed"
    ] is True
    assert blockers["matched_v2_v3_performance"]["partial_evidence"][
        "native_v3_handler_measured"
    ] is True
    assert blockers["matched_v2_v3_performance"]["partial_evidence"][
        "native_business_handler_available"
    ] is True
    performance = blockers["matched_v2_v3_performance"]["partial_evidence"]
    assert performance["matched_synthetic_business_get_measured"] is True
    assert performance["synthetic_business_post_measured"] is True
    assert performance["synthetic_business_measured_methods"] == ["GET", "POST"]
    assert performance["synthetic_business_post_transactions_per_arm"] == 550
    assert performance["synthetic_business_post_transcript_equivalent"] is True
    assert performance["synthetic_business_target_state_equivalent"] is True
    assert performance["production_business_workload_measured"] is False
    assert performance["production_business_release_thresholds_applied"] is False
    assert performance["synthetic_business_corpus_rows"] == 32
    assert performance["v2_synthetic_osc_fd_drift"] == 0
    assert performance["native_v3_synthetic_osc_fd_drift"] == 0
