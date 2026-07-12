import json
import time
from pathlib import Path

from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import LAFOrchestrator


def test_portal_retry_heartbeat_is_public_safe_and_atomic(tmp_path: Path):
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    orchestrator._portal_retry_heartbeat_path = tmp_path / "static" / "laf_portal_retry_state.json"

    orchestrator._write_portal_retry_heartbeat(
        status="error",
        interval_sec=3600,
        pending_count=15,
        processed_count=0,
        error_type="PortalTimeout",
    )

    payload = json.loads(orchestrator._portal_retry_heartbeat_path.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["pending_count"] == 15
    assert payload["error_type"] == "PortalTimeout"
    assert "items" not in payload
    assert not list((tmp_path / "static").glob(".*.tmp"))


def test_portal_retry_watchdog_keeps_running_heartbeat(tmp_path: Path):
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    orchestrator._portal_retry_heartbeat_path = tmp_path / "static" / "laf_portal_retry_state.json"
    orchestrator._retry_pending_portal_downloads = lambda max_items: (time.sleep(0.05) or {"ok": True})

    result = orchestrator._run_pending_portal_retry_cycle_with_watchdog(
        max_items=1,
        interval_sec=3600,
        timeout_sec=2,
    )

    payload = json.loads(orchestrator._portal_retry_heartbeat_path.read_text(encoding="utf-8"))
    assert result == {"ok": True}
    assert payload["status"] == "running"


def test_portal_retry_watchdog_times_out_stuck_cycle(tmp_path: Path):
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    orchestrator._portal_retry_heartbeat_path = tmp_path / "static" / "laf_portal_retry_state.json"
    orchestrator._retry_pending_portal_downloads = lambda max_items: time.sleep(2)

    try:
        orchestrator._run_pending_portal_retry_cycle_with_watchdog(
            max_items=1,
            interval_sec=3600,
            timeout_sec=1,
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("stuck retry cycle did not time out")


def test_missing_folder_manual_review_is_recoverable():
    assert LAFOrchestrator._portal_retry_item_is_pending(
        {"status": "manual_review", "last_error": "missing_local_case_folder"}
    ) is True
    assert LAFOrchestrator._portal_retry_item_is_pending(
        {"status": "manual_review", "last_error": "identity_ambiguous"}
    ) is False
    assert LAFOrchestrator._portal_retry_item_is_pending({"status": "pending_retry"}) is True
