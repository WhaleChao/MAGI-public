from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.v3_campaign.offline_probes import run_fault_campaign
from scripts.v3_campaign.runner import _structured_workload_evidence
from scripts.v3_validation.fault_realism import (
    BLOCKER_CODE,
    DEFAULT_TIME_OFFSET_KILL_DELAYS_US,
    LIVE_ROOT,
    PAYLOAD_ROWS_PER_JOB,
    TRANSACTION_STAGE_MARKERS,
    FaultEvidenceError,
    _read_owned_marker,
    _sigkill_and_reap_owned,
    _validate_workdir,
    run_fault_realism,
    verify_evidence,
)


EVIDENCE_PREFIX = "MAGI_V3_OFFLINE_EVIDENCE="


def test_bounded_fault_matrix_with_realism_audit_emits_recovery_duplicate_and_loss_evidence(
    tmp_path: Path,
) -> None:
    evidence = run_fault_campaign(tmp_path)
    realism = run_fault_realism(
        tmp_path / "realism-sandbox",
        cycles=12,
        include_apfs_sparse_image=True,
    )
    evidence["measurements"]["realism_audit"] = realism

    assert evidence["status"] == "passed"
    assert evidence["measurements"]["faults_passed"] == 6
    assert evidence["measurements"]["duplicate_total"] == 0
    assert evidence["measurements"]["loss_total"] == 0
    matrix = {row["fault"]: row for row in evidence["measurements"]["matrix"]}
    assert matrix["sqlite_wal_concurrent_reopen"]["writer_synchronous_policy"].startswith("NORMAL")
    assert matrix["sqlite_bounded_disk_full"]["real_filesystem_enospc"] is False
    assert matrix["atomic_fsync_failure"]["sqlite_vfs_fsync_failure"] is False
    assert realism["blocker"]["decision"] == "blocker_retained"
    assert realism["coverage"]["physical_apfs_enospc"] is False
    assert realism["coverage"]["physical_power_interruption"] is False
    assert realism["coverage"]["sqlite_vfs_fsync_io_error_injection"] is True
    assert realism["coverage"]["custom_sqlite_vfs_power_loss"] is False
    assert realism["coverage"]["sandbox_apfs_sparse_image_enospc"] is True
    assert realism["measurements"]["apfs_sparse_image"]["filesystem_enospc_operation"] in {
        "write",
        "fsync",
    }
    encoded = EVIDENCE_PREFIX + json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    parsed = _structured_workload_evidence("fault_injection", encoded)
    assert parsed is not None
    parsed_realism = parsed["measurements"]["realism_audit"]
    assert parsed_realism["blocker"]["decision"] == "blocker_retained"
    assert parsed_realism["coverage"]["physical_apfs_enospc"] is False
    assert parsed_realism["coverage"]["physical_power_interruption"] is False
    print(encoded)


def test_owned_sigkill_commit_window_sweep_emits_exact_partial_evidence(tmp_path: Path) -> None:
    cycles = 6
    evidence = run_fault_realism(tmp_path / "sandbox", cycles=cycles)
    measurements = evidence["measurements"]

    assert evidence["status"] == "passed_partial_evidence"
    assert measurements["cycles_requested"] == cycles
    assert measurements["cycles_completed"] == cycles
    assert measurements["acknowledged_commits_lost"] == 0
    assert measurements["partially_visible_transactions"] == 0
    assert measurements["final_job_rows"] == cycles
    assert measurements["final_unique_jobs"] == cycles
    assert measurements["final_payload_rows"] == cycles * PAYLOAD_ROWS_PER_JOB
    assert measurements["duplicate_jobs"] == 0
    assert measurements["lost_jobs_after_recovery"] == 0
    assert measurements["integrity_check"] == "ok"
    vfs_fsync = measurements["sqlite_vfs_fsync_io_error"]
    assert vfs_fsync["status"] == "passed"
    assert vfs_fsync["injection_boundary"] == "custom SQLite VFS xSync"
    assert vfs_fsync["injected_error"] == "SQLITE_IOERR_FSYNC"
    assert vfs_fsync["injected_file_role"] == "wal"
    assert vfs_fsync["injected_open_flags"] & 0x00080000
    assert vfs_fsync["extended_rc"] == vfs_fsync["expected_extended_rc"] == 1034
    assert vfs_fsync["sync_calls_after_arm"] >= 1
    assert vfs_fsync["baseline_rows"] == 1
    assert vfs_fsync["partial_rows"] == 0
    assert vfs_fsync["final_rows"] == 2
    assert vfs_fsync["integrity_ok"] == 1
    assert vfs_fsync["power_loss_simulated"] is False
    assert measurements["machine_instruction_offset_sigkill"]["status"] == "blocked"
    assert (
        measurements["machine_instruction_offset_sigkill"][
            "logical_transaction_boundary_sweep_substituted"
        ]
        is False
    )
    assert all(row["signal"] == "SIGKILL" for row in measurements["cycles"])
    assert all(row["final_job_rows"] == 1 for row in measurements["cycles"])
    assert all(
        row["final_payload_rows"] == PAYLOAD_ROWS_PER_JOB for row in measurements["cycles"]
    )
    instruction = measurements["transaction_instruction_boundary_sweep"]
    assert instruction["stages_completed"] == len(TRANSACTION_STAGE_MARKERS)
    assert instruction["stage_markers"] == list(TRANSACTION_STAGE_MARKERS)
    assert instruction["acknowledged_commits_lost"] == 0
    assert instruction["partially_visible_transactions"] == 0
    assert instruction["final_job_rows"] == len(TRANSACTION_STAGE_MARKERS)
    assert instruction["final_unique_jobs"] == len(TRANSACTION_STAGE_MARKERS)
    assert instruction["final_payload_rows"] == len(TRANSACTION_STAGE_MARKERS) * PAYLOAD_ROWS_PER_JOB
    assert instruction["duplicate_jobs"] == 0
    assert instruction["lost_jobs_after_recovery"] == 0
    assert instruction["integrity_check"] == "ok"
    time_offsets = measurements["bounded_time_offset_sigkill_sweep"]
    assert time_offsets["offsets_completed"] == len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US)
    assert time_offsets["scheduled_offsets_us"] == list(DEFAULT_TIME_OFFSET_KILL_DELAYS_US)
    assert time_offsets["acknowledged_commits_lost"] == 0
    assert time_offsets["partially_visible_transactions"] == 0
    assert time_offsets["final_job_rows"] == len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US)
    assert time_offsets["final_unique_jobs"] == len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US)
    assert time_offsets["final_payload_rows"] == (
        len(DEFAULT_TIME_OFFSET_KILL_DELAYS_US) * PAYLOAD_ROWS_PER_JOB
    )
    assert time_offsets["duplicate_jobs"] == 0
    assert time_offsets["lost_jobs_after_recovery"] == 0
    assert time_offsets["integrity_check"] == "ok"
    assert all(row["signal"] == "SIGKILL" for row in time_offsets["cycles"])
    verify_evidence(evidence)


def test_fault_evidence_retains_realism_blocker_and_names_unproven_claims(tmp_path: Path) -> None:
    evidence = run_fault_realism(tmp_path / "sandbox", cycles=2)

    assert evidence["blocker"]["code"] == BLOCKER_CODE
    assert evidence["blocker"]["eligible_to_clear"] is False
    assert evidence["blocker"]["decision"] == "blocker_retained"
    assert evidence["coverage"] == {
        "owned_process_sigkill_at_commit_boundary": True,
        "owned_process_sigkill_at_bounded_time_offsets": True,
        "sqlite_wal_full_synchronous_sigkill": True,
        "sqlite_wal_reopen_and_integrity_check": True,
        "idempotent_recovery_from_known_input_plan": True,
        "all_logical_transaction_boundaries_sigkill": True,
        "sandbox_apfs_sparse_image_enospc": False,
        "physical_apfs_enospc": False,
        "physical_power_interruption": False,
        "custom_sqlite_vfs_power_loss": False,
        "sqlite_vfs_fsync_io_error_injection": True,
        "arbitrary_instruction_offset_sigkill": False,
    }
    assert "SQLite VFS-boundary fsync I/O-error injection" not in evidence["blocker"][
        "unproven_requirements"
    ]
    assert any(
        "machine-instruction-level SIGKILL" in requirement
        for requirement in evidence["blocker"]["unproven_requirements"]
    )
    assert evidence["safety"]["live_magi_state_accessed"] is False
    assert evidence["safety"]["signals_sent_only_to_owned_children"] is True
    assert evidence["safety"]["owned_custom_vfs_compiled_and_executed"] is True
    assert evidence["safety"]["compiler_network_access"] is False


def test_evidence_hash_fails_closed_after_tampering(tmp_path: Path) -> None:
    evidence = run_fault_realism(tmp_path / "sandbox", cycles=1)
    tampered = copy.deepcopy(evidence)
    tampered["measurements"]["lost_jobs_after_recovery"] = 1

    with pytest.raises(FaultEvidenceError, match="does not match"):
        verify_evidence(tampered)


def test_owned_marker_timeout_path_sigkills_and_reaps_child() -> None:
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        with pytest.raises(FaultEvidenceError, match="did not emit marker"):
            _read_owned_marker(process, expected="NEVER", timeout=0.05)
    finally:
        _stdout, stderr = _sigkill_and_reap_owned(process)
    assert process.returncode == -9
    assert process.poll() is not None
    assert stderr == ""


def test_live_tree_and_source_tree_are_rejected_before_sandbox_creation() -> None:
    with pytest.raises(FaultEvidenceError, match="live MAGI"):
        _validate_workdir(LIVE_ROOT)
    source_root = Path(__file__).resolve().parents[2]
    with pytest.raises(FaultEvidenceError, match="source tree"):
        _validate_workdir(source_root / "forbidden-fault-sandbox")


def test_nonempty_or_symlink_sandbox_is_rejected(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "unrelated.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(FaultEvidenceError, match="empty and dedicated"):
        _validate_workdir(occupied)
    assert (occupied / "unrelated.txt").read_text(encoding="utf-8") == "preserve"

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FaultEvidenceError, match="symlink"):
        _validate_workdir(link)


@pytest.mark.parametrize("cycles", [0, 101])
def test_invalid_cycle_count_is_rejected(tmp_path: Path, cycles: int) -> None:
    with pytest.raises(FaultEvidenceError, match="cycles"):
        run_fault_realism(tmp_path / "sandbox", cycles=cycles)
