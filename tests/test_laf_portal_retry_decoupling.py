from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def test_portal_retry_once_writes_terminal_heartbeat(monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import (
        LAFOrchestrator,
    )

    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    heartbeats = []
    closed = []
    monkeypatch.setattr(
        orchestrator,
        "_write_portal_retry_heartbeat",
        lambda **payload: heartbeats.append(payload),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_pending_portal_retry_cycle_with_watchdog",
        lambda **_kwargs: {"ok": True, "scanned": 7, "processed": 2},
    )
    monkeypatch.setattr(orchestrator, "close", lambda: closed.append(True))

    result = orchestrator.run_portal_retry_once(
        max_items=2,
        timeout_sec=90,
        interval_sec=3600,
    )

    assert result["ok"] is True
    assert heartbeats[0]["status"] == "starting"
    assert heartbeats[-1] == {
        "status": "ok",
        "interval_sec": 3600,
        "pending_count": 7,
        "processed_count": 2,
        "error_type": "",
    }
    assert closed == [True]


def test_download_listing_finds_table_inside_frame():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import (
        LAFWebAutomation,
    )

    class Switch:
        def __init__(self, driver):
            self.driver = driver

        def frame(self, _frame):
            self.driver.depth += 1

        def parent_frame(self):
            self.driver.depth = max(0, self.driver.depth - 1)

        def default_content(self):
            self.driver.depth = 0

    class Driver:
        def __init__(self):
            self.depth = 0
            self.switch_to = Switch(self)

        def find_elements(self, _by, selector):
            if selector == "table":
                return [object()] if self.depth == 1 else []
            if selector == "table tbody tr":
                return [object(), object()] if self.depth == 1 else []
            if selector == "frame, iframe":
                return [object()] if self.depth == 0 else []
            return []

        def find_element(self, _by, _selector):
            return type("Body", (), {"text": ""})()

    automation = LAFWebAutomation.__new__(LAFWebAutomation)
    automation.driver = Driver()

    result = automation._scan_download_listing_context()

    assert result["status"] == "table"
    assert result["row_count"] == 2
    assert result["frame_depth"] == 1
    assert automation.driver.depth == 1


def test_download_listing_empty_is_a_valid_observation(monkeypatch):
    from casper_ecosystem.law_firm_orchestrators import laf_automation_v2

    automation = laf_automation_v2.LAFWebAutomation.__new__(
        laf_automation_v2.LAFWebAutomation
    )
    automation.driver = object()
    automation.log = lambda _message: None
    automation.last_downloadable_cases_scan = {}
    monkeypatch.setattr(
        automation,
        "_enter_download_listing",
        lambda **_kwargs: {"status": "empty", "row_count": 0, "frame_depth": 1},
    )

    result = automation.get_downloadable_cases()
    automation.driver = None

    assert result == []
    assert automation.last_downloadable_cases_scan["ok"] is True
    assert automation.last_downloadable_cases_scan["empty"] is True


def test_portal_retry_fixture_download_is_confined(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import (
        _FixtureWorkflowAutomation,
    )

    automation = _FixtureWorkflowAutomation(
        tmp_path,
        {"allowed_workflows": ["attachment_retry"]},
    )

    paths = automation.download_case_files("1990101-T-001")
    automation.close()

    assert len(paths) == 1
    assert paths[0].startswith(str(tmp_path))
    assert open(paths[0], "rb").read().startswith(b"%PDF-1.4")
    transcript = json.loads(
        (tmp_path / "workflow_provider_transcript.json").read_text(encoding="utf-8")
    )
    assert [row["action"] for row in transcript] == [
        "download_case_files",
        "close",
    ]


def test_portal_retry_write_authority_is_narrowly_confined_to_marked_fixture(
    tmp_path, monkeypatch
):
    from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import (
        _resolve_schedule_fixture_case_folder_for_write,
    )

    fixture = tmp_path / "fixture"
    case = fixture / "cases" / "2099-0003-test"
    outside = tmp_path / "outside"
    case.mkdir(parents=True)
    outside.mkdir()
    (fixture / ".magi-v3-schedule-fixture").write_text(
        "job_laf_portal_retry_once\n", encoding="utf-8"
    )
    provider = fixture / "workflow-provider.json"
    provider.write_text(
        json.dumps({"allowed_workflows": ["attachment_retry"]}), encoding="utf-8"
    )
    monkeypatch.setenv("MAGI_V3_REALISM_SANDBOX", "1")
    monkeypatch.setenv("MAGI_V3_SCHEDULE_FIXTURE_ROOT", str(fixture))
    monkeypatch.setenv("MAGI_LAF_WORKFLOW_PROVIDER_FIXTURE", str(provider))

    assert _resolve_schedule_fixture_case_folder_for_write(str(case)) == str(case)
    assert _resolve_schedule_fixture_case_folder_for_write(str(outside)) == ""

    (fixture / ".magi-v3-schedule-fixture").write_text("wrong-job\n", encoding="utf-8")
    assert _resolve_schedule_fixture_case_folder_for_write(str(case)) == ""


def test_portal_retry_real_job_body_runs_in_disposable_fixture(tmp_path):
    from scripts.v3_validation.schedule_body_registry import _prepare_fixture

    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / "fixture"
    _prepare_fixture(
        "laf_portal_retry_provider",
        fixture,
        "job_laf_portal_retry_once",
    )
    env = dict(os.environ)
    config_path = fixture / "magi-root" / "json" / "config.json"
    env.update(
        {
            "MAGI_ROOT_DIR": str(root),
            "MAGI_CONFIG_PATH": str(config_path),
            "MAGI_CONFIG_SHA256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "MAGI_V3_SHARED_STATE_DIR": str(fixture),
            "MAGI_RUNTIME_DIR": str(fixture / "runtime"),
            "MAGI_MUTABLE_STATIC_DIR": str(fixture / "static"),
            "MAGI_AGENT_DIR": str(fixture / "agent"),
            "MAGI_LAF_WORKFLOW_PROVIDER_FIXTURE": str(
                fixture / "workflow-provider.json"
            ),
            "MAGI_LAF_USERNAME": "fixture-user",
            "MAGI_LAF_PASSWORD": "fixture-password",
            "MAGI_NAS_ENABLE_WRITE": "1",
            "MAGI_V3_REALISM_SANDBOX": "1",
            "MAGI_V3_SCHEDULE_FIXTURE_ROOT": str(fixture),
            "PYTHONPATH": str(root),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(
                root
                / "casper_ecosystem"
                / "law_firm_orchestrators"
                / "laf_orchestrator.py"
            ),
            "--mode",
            "portal-retry-once",
            "--max-items",
            "6",
            "--timeout-sec",
            "120",
            "--no-notify",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Connected to DB" not in completed.stderr
    assert "Connected to local DB" not in completed.stderr
    queue = json.loads(
        (fixture / "agent" / "laf_pending_portal_downloads.json").read_text(
            encoding="utf-8"
        )
    )
    heartbeat = json.loads(
        (fixture / "static" / "laf_portal_retry_state.json").read_text(
            encoding="utf-8"
        )
    )
    archived = list((fixture / "cases").glob("**/*.pdf"))
    assert queue["items"] == []
    assert heartbeat["status"] == "ok"
    assert heartbeat["processed_count"] == 1
    assert len(archived) == 1
    assert not list(fixture.glob("**/*red_phone*"))
