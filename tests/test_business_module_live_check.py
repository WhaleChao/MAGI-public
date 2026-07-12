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


def test_business_live_check_redacts_party_case_rows_and_items():
    payload = {
        "party": "王小明",
        "court_case_no": "115年度消債更字第000071號",
        "row_text": "115年度消債更字第000071號 王小明",
        "items": [{"client_name": "王小明", "case_number": "2025-0134"}],
        "results": [{"name": "laf_self_test", "ok": True}],
    }

    redacted = live_check._redact_obj(payload)

    assert redacted["party"] == "<REDACTED>"
    assert redacted["court_case_no"] == "<REDACTED>"
    assert redacted["row_text"] == "<REDACTED>"
    assert redacted["items"] == "<REDACTED>"
    assert redacted["results"][0]["name"] == "laf_self_test"


def test_result_name_exception_does_not_leak_nested_names():
    redacted = live_check._redact_obj(
        {
            "results": [
                {
                    "name": "laf_self_test",
                    "parsed": {"name": "王小明", "client_name": "王小明"},
                }
            ]
        }
    )

    assert redacted["results"][0]["name"] == "laf_self_test"
    assert redacted["results"][0]["parsed"]["name"] == "<REDACTED>"
    assert redacted["results"][0]["parsed"]["client_name"] == "<REDACTED>"


def test_laf_portal_live_redacts_portal_errors(monkeypatch):
    class FakeAudit:
        calls = []

        @staticmethod
        def scan_portal_pending_drafts(db=None, *, read_only=False):
            FakeAudit.calls.append({"db": db, "read_only": read_only})
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
    assert FakeAudit.calls == [{"db": None, "read_only": True}]


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


def test_drive_sync_status_uses_recent_success_by_kind_when_latest_interrupted(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "interrupted",
                "pid": 999999,
                "worker_kind": "all_files",
                "finished_at": "2026-07-01T08:12:48+08:00",
                "summary": {"matched_case_folders": 23},
                "status_by_kind": {
                    "priority": {
                        "ok": True,
                        "status": "ok",
                        "pid": 1234,
                        "worker_kind": "priority",
                        "finished_at": "2026-07-01T06:49:48+08:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    assert result["parsed"]["status"] == "ok"
    assert result["parsed"]["worker_kind"] == "priority"
    assert result["parsed"]["latest_status"] == "interrupted"
    assert result["parsed"]["matched_case_folders"] == 23
    assert result["parsed"]["status_by_kind"]["priority"]["status"] == "ok"
    assert result["parsed"]["status_by_kind"]["all_files"]["status"] == "interrupted"
    assert "all_files" in result["parsed"]["blocking_kinds"]


def test_drive_sync_status_ignores_stale_inactive_inventory_kind(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_drive_case_sync_bidirectional",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --matched-case-limit 8",
                },
                {
                    "id": "job_drive_case_sync_all_files",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --direct-all-cases",
                },
                {
                    "id": "job_drive_case_sync_nightly",
                    "enabled": False,
                    "command": "python scripts/drive_case_sync_inventory.py --file-diff",
                },
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "ok",
                "pid": 100,
                "worker_kind": "all_files",
                "finished_at": "fresh",
                "status_by_kind": {
                    "all_files": {"ok": True, "status": "ok", "worker_kind": "all_files", "finished_at": "fresh"},
                    "priority": {"ok": True, "status": "ok", "worker_kind": "priority", "finished_at": "fresh"},
                    "inventory": {"ok": True, "status": "ok", "worker_kind": "inventory", "finished_at": "stale"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60 if value == "fresh" else 25 * 3600)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["blocking_kinds"] == []
    assert result["parsed"]["active_kinds"] == ["all_files", "priority"]
    assert result["parsed"]["inactive_kinds"] == ["inventory"]
    assert result["parsed"]["status_by_kind"]["inventory"]["ok"] is False


def test_drive_sync_status_blocks_stale_active_inventory_kind(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_drive_case_sync_inventory",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --no-direct-priority-sync",
                }
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "ok",
                "worker_kind": "inventory",
                "finished_at": "stale",
                "status_by_kind": {
                    "inventory": {"ok": True, "status": "ok", "worker_kind": "inventory", "finished_at": "stale"}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 25 * 3600)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    assert result["parsed"]["blocking_kinds"] == ["inventory"]


def test_drive_sync_status_reads_kind_file_when_general_status_missing(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_priority_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "ok",
                "worker_kind": "priority",
                "finished_at": "2026-07-01T06:49:48+08:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["worker_kind"] == "priority"
    assert result["parsed"]["selected_source"] == "kind_file.priority"


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
        live_check.safe_process,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='{"message":"done"}\n', stderr="", timed_out=False),
    )

    result = live_check._run("contractless", ["python", "fake.py"], timeout=1)

    assert result["ok"] is False
    assert result["contract_error"] == "missing_success_or_ok_contract"


def test_run_rejects_missing_json_or_non_boolean_success_contract(monkeypatch):
    outputs = iter(("done\n", "[]\n", '{"success":"false"}\n'))
    monkeypatch.setattr(
        live_check.safe_process,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=next(outputs), stderr="", timed_out=False),
    )

    missing_json = live_check._run("missing_json", ["python", "fake.py"], timeout=1)
    array_payload = live_check._run("array_payload", ["python", "fake.py"], timeout=1)
    string_flag = live_check._run("string_flag", ["python", "fake.py"], timeout=1)

    assert missing_json["ok"] is False
    assert missing_json["contract_error"] == "missing_json_object_contract"
    assert array_payload["ok"] is False
    assert array_payload["contract_error"] == "missing_json_object_contract"
    assert string_flag["ok"] is False
    assert string_flag["contract_error"] == "non_boolean_success_or_ok_contract"


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


def test_live_runtime_fingerprint_covers_file_review_manager_core():
    assert (
        "casper_ecosystem/law_firm_orchestrators/file_review_automation.py"
        in live_check._LIVE_ROOT_FINGERPRINT_FILES
    )


def test_live_runtime_fingerprint_covers_laf_automation_core():
    assert (
        "casper_ecosystem/law_firm_orchestrators/laf_automation_v2.py"
        in live_check._LIVE_ROOT_FINGERPRINT_FILES
    )


def test_live_runtime_fingerprint_covers_laf_orchestrator_core():
    assert (
        "casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py"
        in live_check._LIVE_ROOT_FINGERPRINT_FILES
    )
    assert (
        "casper_ecosystem/law_firm_orchestrators/laf_orchestrator_docmixins.py"
        in live_check._LIVE_ROOT_FINGERPRINT_FILES
    )


def test_live_runtime_fingerprint_covers_transcript_indexer_core():
    assert "skills/transcript-indexer/action.py" in live_check._LIVE_ROOT_FINGERPRINT_FILES


def test_live_runtime_fingerprint_covers_acceptance_gate():
    assert "scripts/ops/magi_acceptance_gate.py" in live_check._LIVE_ROOT_FINGERPRINT_FILES


def test_live_runtime_fingerprint_covers_admin_runtime_health_boundary():
    assert "api/blueprints/admin_runtime.py" in live_check._LIVE_ROOT_FINGERPRINT_FILES


def test_live_runtime_fingerprint_covers_business_module_cron_jobs():
    assert {
        "job_laf_pending_scan",
        "job_laf_gmail_dispatch_scan",
        "job_file_review_check",
        "job_file_review_downloadable_probe_dense",
        "job_transcript_sync",
        "job_transcript_indexer",
        "job_business_module_live_check",
    } <= live_check._LIVE_ROOT_CRON_JOBS


def test_live_runtime_root_fingerprint_detects_cron_semantic_drift(tmp_path, monkeypatch):
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(live_check, "REPO_ROOT", source)
    monkeypatch.setattr(live_check, "DEFAULT_LIVE_RUNTIME_ROOT", runtime)
    monkeypatch.setattr(live_check, "_LIVE_ROOT_FINGERPRINT_FILES", ("api/server.py",))
    monkeypatch.setattr(live_check, "_LIVE_ROOT_CRON_JOBS", {"job_osc_events_refresh"})

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

    assert rc == 1
    report_path = tmp_path / ".runtime" / "business_module_live_check_latest.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    laf_result = next(item for item in payload["results"] if item["name"] == "laf_portal_live")
    assert laf_result["skipped"] is True
    assert laf_result["error"] == "skipped_live_verification"
    assert "business_module_live_check_latest.json" in json.loads(capsys.readouterr().out)["json_out"]


def test_write_report_is_atomic_and_redacts_legal_content(tmp_path):
    report_path = tmp_path / "business.json"
    live_check._write_report(
        report_path,
        {
            "ok": True,
            "results": [{"name": "laf_self_test", "ok": True, "parsed": {"row_text": "王小明"}}],
            "items": [{"party": "王小明"}],
        },
    )

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["results"][0]["name"] == "laf_self_test"
    assert payload["results"][0]["parsed"]["row_text"] == "<REDACTED>"
    assert payload["items"] == "<REDACTED>"
    assert not list(tmp_path.glob(".business.json.*.tmp"))


def test_notify_reports_queued_delivery(monkeypatch):
    monkeypatch.setenv("MAGI_BUSINESS_LIVE_CHECK_NOTIFY", "1")
    import sys

    fake = SimpleNamespace(send_telegram_push_with_status=lambda *_args, **_kwargs: {"queued": True})
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake)

    result = live_check._notify("健康檢查")

    assert result == {"requested": True, "ok": True, "delivery": "queued", "queued": True}


def test_notification_failure_is_written_as_business_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "audit_live_conflicts", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(live_check, "_live_runtime_root_live", lambda: {"name": "live_runtime_root_fingerprint", "ok": True})
    monkeypatch.setattr(live_check, "_token_health_live", lambda: {"name": "token_health_refresh", "ok": True})
    monkeypatch.setattr(live_check, "_nas_mounts_live", lambda: {"name": "nas_mounts_live", "ok": True})
    monkeypatch.setattr(live_check, "_drive_sync_status_live", lambda: {"name": "drive_sync_status_live", "ok": True})
    monkeypatch.setattr(live_check, "_calendar_todo_status_live", lambda: {"name": "calendar_todo_status_live", "ok": True})
    monkeypatch.setattr(live_check, "_laf_portal_live", lambda: {"name": "laf_portal_live", "ok": True})
    monkeypatch.setattr(live_check, "_run", lambda name, *_args, **_kwargs: {"name": name, "ok": True})
    monkeypatch.setattr(live_check, "_notify", lambda _text: {"requested": True, "ok": False, "delivery": "failed", "queued": False})

    rc = live_check.main(["--skip-conflict-audit"])

    payload = json.loads((tmp_path / ".runtime" / "business_module_live_check_latest.json").read_text(encoding="utf-8"))
    assert rc == 1
    assert payload["ok"] is False
    assert payload["notification"]["delivery"] == "failed"
    assert any(item["name"] == "notification_delivery" and item["ok"] is False for item in payload["results"])
