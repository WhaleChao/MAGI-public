from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts.ops import business_module_live_check as live_check


def test_business_live_check_redacts_sensitive_tails_and_samples():
    text = (
        "case_number=2025-0134 "
        "court_case_number=115年度消債更字第000071號 "
        "email=person@example.com phone=0912345678 "
        "path=/Users/example/private/case.pdf token='abc123'"
    )

    redacted = live_check._redact_text(text)

    assert "2025-0134" not in redacted
    assert "115年度消債更字第000071號" not in redacted
    assert "person@example.com" not in redacted
    assert "0912345678" not in redacted
    assert "/Users/example" not in redacted
    assert "abc123" not in redacted
    assert "<CASE_ID>" in redacted
    assert "<COURT_CASE_NO>" in redacted


def test_business_live_check_redacts_parsed_samples():
    payload = {
        "success": True,
        "eligible_cases": 2,
        "sample": [
            {
                "case_number": "2025-0134",
                "court_case_number": "115年度消債更字第000071號",
                "client_name": "測試姓名",
            }
        ],
    }

    redacted = live_check._redact_obj(payload)

    assert redacted["eligible_cases"] == 2
    assert redacted["sample"] == "<REDACTED:1 item(s)>"


def test_laf_portal_live_redacts_portal_errors(monkeypatch):
    class FakeAudit:
        @staticmethod
        def scan_portal_pending_drafts(db=None):
            return {
                "error": "case 2025-0134 for person@example.com at /Users/example/private",
                "closing_drafts": [],
                "case_status_drafts": [],
                "condition_pending": [],
                "go_live_pending": [],
                "progress_pending": [],
            }

    monkeypatch.setitem(__import__("sys").modules, "scripts.laf_nightly_audit", FakeAudit)

    result = live_check._laf_portal_live()

    assert result["ok"] is False
    assert "2025-0134" not in result["parsed"]["error"]
    assert "person@example.com" not in result["parsed"]["error"]
    assert "/Users/example" not in result["parsed"]["error"]


def test_drive_sync_status_flags_running_without_live_pid(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({"ok": True, "status": "direct_all_case_sync_running", "pid": 999999}),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: False)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    assert result["parsed"]["running_without_pid"] is True
    assert "running_without_live_pid" in result["parsed"]["reason"]


def test_drive_sync_status_flags_stale_completed_status(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    status_file = runtime / "drive_case_sync_worker_status_latest.json"
    status_file.write_text(
        json.dumps({"ok": True, "status": "ok", "pid": 1234, "summary": {"matched_case_folders": 21}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_age_seconds", lambda path: (live_check.DRIVE_SYNC_STATUS_SLA_HOURS + 1) * 3600)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    assert "stale_status" in result["parsed"]["reason"]
    assert result["parsed"]["sla_hours"] == live_check.DRIVE_SYNC_STATUS_SLA_HOURS
    assert "drive_case_sync_worker.py" in result["parsed"]["next_action"]


def test_drive_sync_status_accepts_active_running_pid(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({"status": "direct_all_case_sync_running", "pid": 1234, "worker_kind": "all_files"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(live_check, "_age_seconds", lambda path: 30)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["active_running"] is True


def test_calendar_todo_status_accepts_recent_ok_report(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "osc_events_refresh_latest.json").write_text(
        json.dumps(
            {
                "calendar_audit": {"ok": True, "summary": {"checked_primary_events": 3}},
                "calendar_import": {"ok": True, "imported": 0, "skipped": 2},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)

    result = live_check._calendar_todo_status_live()

    assert result["ok"] is True
    assert result["parsed"]["calendar_audit_ok"] is True


def test_calendar_todo_status_flags_stale_report_with_sla_next_action(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "osc_events_refresh_latest.json").write_text(
        json.dumps(
            {
                "calendar_audit": {"ok": True, "summary": {"checked_primary_events": 3}},
                "calendar_import": {"ok": True, "imported": 1, "skipped": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_age_seconds", lambda path: (live_check.CALENDAR_TODO_STATUS_SLA_HOURS + 1) * 3600)

    result = live_check._calendar_todo_status_live()

    assert result["ok"] is False
    assert "stale_status" in result["parsed"]["reason"]
    assert result["parsed"]["sla_hours"] == live_check.CALENDAR_TODO_STATUS_SLA_HOURS
    assert "osc_events_refresh.py" in result["parsed"]["next_action"]


def test_run_requires_success_or_ok_contract(monkeypatch):
    monkeypatch.setattr(
        live_check.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='{"message":"done"}\n', stderr=""),
    )

    result = live_check._run("contractless", ["python", "fake.py"], timeout=1)

    assert result["ok"] is False
    assert result["contract_error"] == "missing_success_or_ok_contract"


def test_command_script_keys_accepts_quoted_runtime_path_with_spaces():
    command = (
        "'/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2/venv/bin/python3' "
        "'/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2/scripts/ops/run_after_token_refresh.py' "
        "-- '/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2/venv/bin/python3' "
        "'/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2/scripts/ops/osc_events_refresh.py'"
    )

    assert live_check._command_script_keys(command) == {
        "scripts/ops/run_after_token_refresh.py",
        "scripts/ops/osc_events_refresh.py",
    }


def test_live_runtime_root_fingerprint_detects_cron_semantic_drift(tmp_path, monkeypatch):
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(live_check, "REPO_ROOT", source)
    monkeypatch.setattr(live_check, "DEFAULT_LIVE_RUNTIME_ROOT", runtime)
    monkeypatch.setattr(live_check, "_LIVE_ROOT_FINGERPRINT_FILES", ("api/server.py",))
    monkeypatch.setattr(live_check, "_LIVE_ROOT_GOOGLE_CRON_JOBS", {"job_osc_events_refresh"})

    for root in (source, runtime):
        path = root / "api" / "server.py"
        path.parent.mkdir(parents=True)
        path.write_text("print('same')\n", encoding="utf-8")

    (source / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_osc_events_refresh",
                    "enabled": True,
                    "cron": "35 */6 * * *",
                    "command": "python scripts/ops/run_after_token_refresh.py -- python scripts/ops/osc_events_refresh.py",
                }
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_osc_events_refresh",
                    "enabled": True,
                    "cron": "35 */6 * * *",
                    "command": "python scripts/ops/osc_events_refresh.py",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = live_check._live_runtime_root_live()

    assert result["ok"] is False
    assert result["parsed"]["cron_mismatches"][0]["id"] == "job_osc_events_refresh"


def test_main_json_writes_default_business_health_latest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "audit_live_conflicts", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(live_check, "_live_runtime_root_live", lambda: {"name": "live_runtime_root_fingerprint", "ok": True})
    monkeypatch.setattr(live_check, "_token_health_live", lambda: {"name": "token_health_refresh", "ok": True})
    monkeypatch.setattr(live_check, "_nas_mounts_live", lambda: {"name": "nas_mounts_live", "ok": True})
    monkeypatch.setattr(live_check, "_drive_sync_status_live", lambda: {"name": "drive_sync_status_live", "ok": True})
    monkeypatch.setattr(live_check, "_calendar_todo_status_live", lambda: {"name": "calendar_todo_status_live", "ok": True})
    monkeypatch.setattr(live_check, "_run", lambda name, *_args, **_kwargs: {"name": name, "ok": True})

    rc = live_check.main(["--json", "--skip-conflict-audit", "--skip-laf-live"])

    assert rc == 0
    report_path = tmp_path / ".runtime" / "business_module_live_check_latest.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert "business_module_live_check_latest.json" in json.loads(capsys.readouterr().out)["json_out"]
