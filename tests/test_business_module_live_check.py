from __future__ import annotations

import importlib.util
import json
import plistlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from gui.magi_menubar import _business_readiness_detail
from scripts.ops import business_readiness_snapshot as readiness_snapshot
from scripts.ops import business_module_live_check as live_check
from scripts.ops import audit_operational_hardening as hardening
from magi_v3.file_review_receipts import (
    PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
    portal_download_snapshot,
    portal_snapshot_fingerprint,
    signature_set_hash,
)


_PROCESS_HYGIENE_PATH = (
    Path(__file__).resolve().parents[1] / "skills/process-hygiene/action.py"
)
_PROCESS_HYGIENE_SPEC = importlib.util.spec_from_file_location(
    "magi_process_hygiene_test_module", _PROCESS_HYGIENE_PATH
)
assert _PROCESS_HYGIENE_SPEC is not None and _PROCESS_HYGIENE_SPEC.loader is not None
process_hygiene = importlib.util.module_from_spec(_PROCESS_HYGIENE_SPEC)
_PROCESS_HYGIENE_SPEC.loader.exec_module(process_hygiene)


def _coverage_portal_receipt(batch: str, count: int) -> dict:
    return portal_download_snapshot(
        [
            {
                "status": "downloadable",
                "rowid": f"{batch}-{index}",
                "upddt": "20260818190000",
            }
            for index in range(count)
        ],
        observed_at="2026-08-18T19:20:00+08:00",
    )


def _coverage_download_receipt(expected: dict, handled: list[str]) -> dict:
    expected_hashes = expected["portal_download_signature_hashes"]
    handled = sorted(set(handled))
    return {
        "success": True,
        "portal_download_receipt_schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
        "expected_portal_downloadable_count": len(expected_hashes),
        "accounted_portal_downloadable_count": len(handled),
        "download_reconciliation_verified": True,
        "expected_portal_signature_hashes": expected_hashes,
        "expected_portal_signature_set_hash": signature_set_hash(expected_hashes),
        "handled_portal_signature_hashes": handled,
        "handled_portal_signature_set_hash": signature_set_hash(handled),
        "mismatch_deferred_portal_signature_hashes": [],
        "mismatch_deferred_portal_signature_set_hash": signature_set_hash([]),
        "accounted_portal_signature_hashes": handled,
        "accounted_portal_signature_set_hash": signature_set_hash(handled),
        "reconciled_probe_snapshot_fingerprint": expected[
            "portal_probe_snapshot_fingerprint"
        ],
        "reconciled_probe_observed_at": expected["portal_probe_observed_at"],
    }


def test_drive_chunk_receipt_is_operational_wait_not_full_cycle_success():
    payload = {
        "worker_kind": "all_files",
        "status": "chunk_completed",
        "ok": True,
        "success": True,
        "chunk_completed": True,
        "cycle_completed": False,
        "all_case_offset_before": 0,
        "all_case_offset_after": 1,
        "all_case_total": 221,
        "finished_at": "2026-08-18T12:00:00+08:00",
    }
    evaluated = live_check._drive_status_eval(
        {"payload": payload, "age_seconds": 0}, max_age_hours=8
    )
    assert evaluated["healthy"] is True
    assert payload["cycle_completed"] is False


def test_drive_chunk_deadline_needs_scheduler_retry_and_never_advances_cursor():
    payload = {
        "worker_kind": "all_files",
        "status": "chunk_deadline_deferred",
        "deferred": True,
        "action_required": False,
        "finished_at": "2026-08-18T12:00:00+08:00",
        "all_case_offset_before": 12,
        "all_case_offset_after": 12,
        "cycle_completed": False,
        "_scheduler_retry_pending": True,
    }
    evaluated = live_check._drive_status_eval(
        {"payload": payload, "age_seconds": 0}, max_age_hours=8
    )
    assert evaluated["waiting"] is True
    assert evaluated["waiting_reason"] == "chunk_deadline_retry_scheduled"
    payload["all_case_offset_after"] = 13
    assert live_check._drive_status_eval(
        {"payload": payload, "age_seconds": 0}, max_age_hours=8
    )["healthy"] is False


def test_drive_chunk_with_mixed_unverified_pending_stays_blocking():
    payload = {
        "worker_kind": "all_files",
        "status": "chunk_completed",
        "ok": True,
        "success": True,
        "chunk_completed": True,
        "cycle_completed": False,
        "finished_at": "2026-08-18T12:00:00+08:00",
        "file_sync_summary": {
            "pending_unverified_files": 1,
            "pending_existing_checksum_missing_conflict": 0,
        },
    }
    evaluated = live_check._drive_status_eval(
        {"payload": payload, "age_seconds": 0}, max_age_hours=8
    )
    assert evaluated["hard_failure_count"] == 1
    assert evaluated["healthy"] is False


def test_business_live_check_redacts_sensitive_tails_and_samples():
    private_root = Path("/").joinpath("Users", "example")
    text = (
        "case_number=2025-0134 "
        "court_case_number=115年度消債更字第000071號 "
        "email=person@example.com phone=0912345678 "
        f"path={private_root}/private/case.pdf token='abc123'"
    )

    redacted = live_check._redact_text(text)

    assert "2025-0134" not in redacted
    assert "115年度消債更字第000071號" not in redacted
    assert "person@example.com" not in redacted
    assert "0912345678" not in redacted
    assert str(private_root) not in redacted
    assert "abc123" not in redacted
    assert "<CASE_ID>" in redacted
    assert "<COURT_CASE_NO>" in redacted


def test_laf_retry_snapshot_hides_expired_history_and_translates_active_reasons():
    details = readiness_snapshot._laf_retry_details(
        [
            {
                "laf_case_number": "115-A",
                "status": "pending_retry",
                "reason": "review_result_download",
            },
            {
                "laf_case_number": "115-B",
                "status": "pending_retry",
                "reason": "startup_backfill_missing_closing_docs",
            },
            {
                "laf_case_number": "115-OLD",
                "status": "expired",
                "last_error": "portal_attachment_retention_expired",
            },
            {
                "laf_case_number": "115-ARCHIVE",
                "status": "archived",
                "last_error": "portal_attachment_retention_expired",
            },
        ]
    )

    assert [row["laf_case_number"] for row in details] == ["115-A", "115-B"]
    assert details[0]["reason"] == "已收到明確的官網下載通知，等待附件可下載"
    assert details[1]["reason"] == "結案附件尚未歸檔，下載期限內自動補抓"


def test_laf_menubar_separates_automatic_retry_from_manual_action():
    detail = _business_readiness_detail(
        "法扶附件",
        {
            "missing": 0,
            "pending_retry": 1,
            "manual_review": 1,
            "retry_items": [
                {
                    "case_number": "2026-A",
                    "laf_case_number": "115-A",
                    "client_name": "測試甲",
                    "status": "自動重試中",
                    "reason": "等待官網附件",
                },
                {
                    "case_number": "2026-B",
                    "laf_case_number": "115-B",
                    "client_name": "測試乙",
                    "status": "需人工確認",
                    "reason": "案件資料無法唯一比對",
                },
            ],
            "missing_items": [],
        },
    )

    assert "期限內自動補抓（1 件）" in detail
    assert "需要處理（1 件）" in detail


def test_file_review_worker_failure_snapshot_exposes_only_reconciliation_aggregates(
    tmp_path, monkeypatch
):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "result": {
                    "ok": False,
                    "download": {
                        "reason": "portal_downloadable_not_reconciled",
                        "expected_portal_downloadable_count": 7,
                        "accounted_portal_downloadable_count": 5,
                        "download_reconciliation_verified": False,
                        "error": "private-case /private/review.pdf token=secret",
                    },
                    "scheduled_check": {
                        "parsed": {
                            "steps": {
                                "download": {
                                    "reason": "portal_probe_failed",
                                    "expected_portal_downloadable_count": 99,
                                }
                            }
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness_snapshot, "_operations", lambda _exec_fn: {})
    monkeypatch.setattr(readiness_snapshot, "_latest_file_review_job", lambda *_args: {})
    monkeypatch.setattr(
        readiness_snapshot, "_scheduled_file_review_download_enabled", lambda _root: False
    )

    snapshot = readiness_snapshot.build_snapshot(
        root=tmp_path,
        env={
            "MAGI_MUTABLE_STATIC_DIR": str(static),
            "MAGI_RUNTIME_DIR": str(tmp_path / "runtime"),
            "MAGI_AGENT_DIR": str(tmp_path / "agent"),
            "MAGI_FILE_REVIEW_UNATTENDED_MODE": "1",
        },
        now=datetime(2026, 8, 19, 9, 0, 0),
    )

    item = snapshot["items"]["閱卷下載"]
    assert item == {
        "state": "attention",
        "label": "上輪失敗",
        "auto_download": True,
        "ready_items": [],
        "failure_reason": "portal_downloadable_not_reconciled",
        "expected_portal_downloadable_count": 7,
        "accounted_portal_downloadable_count": 5,
        "download_reconciliation_verified": False,
    }
    serialized = json.dumps(item, ensure_ascii=False)
    assert "private-case" not in serialized
    assert "/private/review.pdf" not in serialized
    assert "secret" not in serialized


def test_file_review_failed_job_snapshot_uses_safe_job_reason(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "result": {
                    "ok": True,
                    "check": {
                        "parsed": {"portal_status_semantics": "ola-current-state-v2"}
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness_snapshot, "_operations", lambda _exec_fn: {})
    monkeypatch.setattr(
        readiness_snapshot,
        "_latest_file_review_job",
        lambda *_args: {
            "status": "failed",
            "success": False,
            "result": {
                "reason": "portal_probe_failed",
                "expected_portal_downloadable_count": 3,
                "accounted_portal_downloadable_count": 0,
                "download_reconciliation_verified": False,
                "error": "token=private-value",
            },
        },
    )
    monkeypatch.setattr(
        readiness_snapshot, "_scheduled_file_review_download_enabled", lambda _root: False
    )

    snapshot = readiness_snapshot.build_snapshot(
        root=tmp_path,
        env={
            "MAGI_MUTABLE_STATIC_DIR": str(static),
            "MAGI_RUNTIME_DIR": str(tmp_path / "runtime"),
            "MAGI_AGENT_DIR": str(tmp_path / "agent"),
            "MAGI_FILE_REVIEW_UNATTENDED_MODE": "1",
        },
        now=datetime(2026, 8, 19, 9, 0, 0),
    )

    item = snapshot["items"]["閱卷下載"]
    assert item["state"] == "attention"
    assert item["label"] == "下載工作失敗"
    assert item["failure_reason"] == "portal_probe_failed"
    assert item["expected_portal_downloadable_count"] == 3
    assert item["accounted_portal_downloadable_count"] == 0
    assert item["download_reconciliation_verified"] is False
    assert "private-value" not in json.dumps(item, ensure_ascii=False)


def test_file_review_attention_explains_reconciliation_failure_with_zero_ready():
    detail = _business_readiness_detail(
        "閱卷下載",
        {
            "state": "attention",
            "label": "上輪失敗",
            "auto_download": True,
            "ready_to_download": 0,
            "failure_reason": "portal_downloadable_not_reconciled",
            "expected_portal_downloadable_count": 7,
            "accounted_portal_downloadable_count": 5,
            "download_reconciliation_verified": False,
        },
    )

    assert detail == (
        "入口偵測7件、已驗證5件，本輪簽章未完全對上，"
        "MAGI將自動重試；未完成前維持紅燈。"
    )


def test_file_review_ok_with_zero_ready_keeps_success_explanation():
    detail = _business_readiness_detail(
        "閱卷下載",
        {
            "state": "ok",
            "auto_download": True,
            "ready_to_download": 0,
            "pending_payment": 0,
        },
    )

    assert detail == "閱卷資料自動掃描與下載功能正常，目前沒有待下載檔案。"


def test_file_review_unknown_attention_reason_uses_safe_generic_explanation():
    unsafe_reason = "case=private /private/review.pdf token=secret"
    detail = _business_readiness_detail(
        "閱卷下載",
        {
            "state": "attention",
            "auto_download": True,
            "ready_to_download": 0,
            "failure_reason": unsafe_reason,
        },
    )

    assert detail == "閱卷下載上輪未完成，MAGI將自動重試；未完成前維持紅燈。"
    assert unsafe_reason not in detail
    assert "功能正常" not in detail


def test_nested_live_probe_binds_current_release_pythonpath(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs["env_extra"])
        return SimpleNamespace(
            returncode=0,
            stdout='{"success":true}',
            stderr="",
            timed_out=False,
        )

    monkeypatch.setenv("PYTHONPATH", "/tmp/existing")
    monkeypatch.setattr(live_check.safe_process, "run", fake_run)

    result = live_check._run("probe", [live_check.PYTHON, "probe.py"])

    assert result["ok"] is True
    parts = captured["env"]["PYTHONPATH"].split(__import__("os").pathsep)
    assert parts[0] == str(live_check.REPO_ROOT)
    assert "/tmp/existing" in parts


def test_business_recovery_contract_live_covers_all_declared_domains():
    result = live_check._business_recovery_contract_live()
    assert result["ok"] is True
    assert result["parsed"]["domain_count"] == 13
    assert result["parsed"]["owner_count"] == 79
    assert result["parsed"]["verifier_count"] == 19
    assert result["parsed"]["errors"] == []


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
    private_root = Path("/").joinpath("Users", "example")

    class FakeAudit:
        calls = []

        @staticmethod
        def scan_portal_pending_drafts(db=None, *, read_only=False):
            FakeAudit.calls.append({"db": db, "read_only": read_only})
            return {
                "error": f"case 2025-0134 for person@example.com at {private_root}/private",
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
    assert str(private_root) not in result["parsed"]["error"]
    assert FakeAudit.calls == [{"db": None, "read_only": True}]


def test_laf_ingestion_accepts_fresh_running_startup_state(tmp_path, monkeypatch):
    monitor = tmp_path / "laf_gmail_monitor_state.json"
    pending = tmp_path / "laf_gmail_dispatch_pending.json"
    portal = tmp_path / "laf_portal_new_files_latest.json"
    monitor.write_text(
        json.dumps(
            {
                "status": "started",
                "running": True,
                "consecutive_errors": 0,
                "updated_at": "fresh",
            }
        ),
        encoding="utf-8",
    )
    pending.write_text(
        json.dumps(
            {"ok": True, "pending_count": 0, "failure_count": 0, "updated_at": "fresh"}
        ),
        encoding="utf-8",
    )
    portal.write_text(
        json.dumps(
            {"ok": True, "status": "ok", "scanned_cases": 147, "checked_at": "fresh"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        live_check,
        "_mutable_static_status_file",
        lambda name: monitor if name == monitor.name else portal,
    )
    monkeypatch.setattr(live_check, "_runtime_status_file", lambda *_parts: pending)
    monkeypatch.setattr(live_check, "_artifact_age_seconds", lambda *_args: 60)

    result = live_check._laf_ingestion_coverage_live()

    assert result["ok"] is True
    assert result["parsed"]["reason"] == ""


def test_laf_ingestion_accepts_fresh_successful_portal_download(tmp_path, monkeypatch):
    monitor = tmp_path / "laf_gmail_monitor_state.json"
    pending = tmp_path / "laf_gmail_dispatch_pending.json"
    portal = tmp_path / "laf_portal_new_files_latest.json"
    monitor.write_text(
        json.dumps(
            {
                "status": "ok",
                "running": False,
                "consecutive_errors": 0,
                "updated_at": "fresh",
            }
        ),
        encoding="utf-8",
    )
    pending.write_text(
        json.dumps(
            {"ok": True, "pending_count": 0, "failure_count": 0, "updated_at": "fresh"}
        ),
        encoding="utf-8",
    )
    portal.write_text(
        json.dumps(
            {
                "ok": True,
                "status": "downloaded",
                "scanned_cases": 150,
                "portal_auto_downloaded": 2,
                "portal_still_missing": 0,
                "checked_at": "fresh",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        live_check,
        "_mutable_static_status_file",
        lambda name: monitor if name == monitor.name else portal,
    )
    monkeypatch.setattr(live_check, "_runtime_status_file", lambda *_parts: pending)
    monkeypatch.setattr(live_check, "_artifact_age_seconds", lambda *_args: 60)

    result = live_check._laf_ingestion_coverage_live()

    assert result["ok"] is True
    assert result["parsed"]["portal_still_missing"] == 0
    assert result["parsed"]["reason"] == ""


def test_laf_ingestion_rejects_unowned_started_state(tmp_path, monkeypatch):
    monitor = tmp_path / "laf_gmail_monitor_state.json"
    pending = tmp_path / "laf_gmail_dispatch_pending.json"
    portal = tmp_path / "laf_portal_new_files_latest.json"
    monitor.write_text(
        json.dumps(
            {
                "status": "started",
                "running": False,
                "consecutive_errors": 0,
                "updated_at": "fresh",
            }
        ),
        encoding="utf-8",
    )
    pending.write_text(
        json.dumps(
            {"ok": True, "pending_count": 0, "failure_count": 0, "updated_at": "fresh"}
        ),
        encoding="utf-8",
    )
    portal.write_text(
        json.dumps(
            {"ok": True, "status": "ok", "scanned_cases": 147, "checked_at": "fresh"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        live_check,
        "_mutable_static_status_file",
        lambda name: monitor if name == monitor.name else portal,
    )
    monkeypatch.setattr(live_check, "_runtime_status_file", lambda *_parts: pending)
    monkeypatch.setattr(live_check, "_artifact_age_seconds", lambda *_args: 60)

    result = live_check._laf_ingestion_coverage_live()

    assert result["ok"] is False
    assert result["parsed"]["reason"] == "laf_gmail_monitor_failed"


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


def test_business_live_check_refreshes_drive_state_after_slow_probes(monkeypatch):
    early = {"name": "drive_sync_status_live", "ok": False, "parsed": {"pid": 100}}
    durable = {"name": "laf_self_test", "ok": True}
    current = {"name": "drive_sync_status_live", "ok": True, "parsed": {"pid": 200}}
    monkeypatch.setattr(live_check, "_drive_sync_status_live", lambda: current)

    refreshed = live_check._refresh_volatile_results([early, durable])

    assert refreshed == [current, durable]
    assert refreshed is not [early, durable]


def test_drive_sync_status_accepts_live_lock_owner_when_contender_pid_exited(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "already_running",
                "pid": 2222,
                "active_worker_pid": 1111,
                "worker_kind": "all_files",
                "finished_at": "fresh",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: pid == 1111)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 30)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["pid"] == 1111
    assert result["parsed"]["pid_alive"] is True
    assert result["parsed"]["active_running"] is True
    assert result["parsed"]["running_without_pid"] is False


def test_drive_sync_status_accepts_completed_overlap_receipt_without_live_owner(tmp_path, monkeypatch):
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
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "status": "direct_all_case_sync_running",
                "pid": 1111,
                "worker_kind": "all_files",
                "status_by_kind": {
                    "all_files": {
                        "status": "direct_all_case_sync_running",
                        "pid": 1111,
                        "worker_kind": "all_files",
                        "finished_at": "fresh",
                    },
                    "priority": {
                        "ok": False,
                        "status": "already_running",
                        "pid": 2222,
                        "worker_kind": "priority",
                        "action_required": False,
                        "finished_at": "fresh",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: pid == 1111)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["blocking_kinds"] == []
    assert result["parsed"]["waiting_kinds"] == ["priority"]
    assert result["parsed"]["status_by_kind"]["priority"]["running_without_pid"] is False
    assert result["parsed"]["status_by_kind"]["priority"]["waiting_reason"] == (
        "singleton_overlap_safely_deferred"
    )


def test_notification_delivery_status_accepts_stale_idle_queue(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "notification_delivery_health_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "idle",
                "remaining": 0,
                "auto_retry_pending": 0,
                "manual_hold_pending": 0,
                "oldest_pending_age_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        live_check,
        "_artifact_age_seconds",
        lambda *args, **kwargs: (live_check.NOTIFICATION_STATUS_SLA_HOURS + 1) * 3600,
    )

    result = live_check._notification_delivery_status_live()

    assert result["ok"] is True
    assert result["parsed"]["reason"] == ""


def test_notification_delivery_status_keeps_stale_pending_queue_blocking(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "notification_delivery_health_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "ok",
                "remaining": 1,
                "auto_retry_pending": 1,
                "manual_hold_pending": 0,
                "oldest_pending_age_seconds": 60,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        live_check,
        "_artifact_age_seconds",
        lambda *args, **kwargs: (live_check.NOTIFICATION_STATUS_SLA_HOURS + 1) * 3600,
    )

    result = live_check._notification_delivery_status_live()

    assert result["ok"] is False
    assert "stale_notification_delivery_evidence" in result["parsed"]["reason"]


def test_notification_delivery_status_fails_closed_for_malformed_receipt(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "notification_delivery_health_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "ok",
                "remaining": "not-a-number",
                "auto_retry_pending": 0,
                "manual_hold_pending": 0,
                "oldest_pending_age_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)

    result = live_check._notification_delivery_status_live()

    assert result["ok"] is False
    assert result["error"] == "invalid_notification_delivery_evidence"
    assert result["parsed"]["reason"] == "invalid_notification_delivery_evidence"


def test_drive_sync_status_treats_controlled_interruption_as_retryable_wait(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "interrupted",
                "action_required": False,
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

    assert result["ok"] is True
    assert result["parsed"]["status"] == "interrupted"
    assert result["parsed"]["worker_kind"] == "all_files"
    assert result["parsed"]["latest_status"] == "interrupted"
    assert result["parsed"]["matched_case_folders"] == 23
    assert result["parsed"]["status_by_kind"]["priority"]["status"] == "ok"
    assert result["parsed"]["status_by_kind"]["all_files"]["status"] == "interrupted"
    assert "all_files" not in result["parsed"]["blocking_kinds"]
    assert result["parsed"]["status_by_kind"]["all_files"]["waiting"] is True
    assert (
        result["parsed"]["status_by_kind"]["all_files"]["waiting_reason"]
        == "controlled_interruption_retry_scheduled"
    )


def test_drive_sync_status_keeps_action_required_interruption_blocking(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "interrupted",
                "action_required": True,
                "pid": 999999,
                "worker_kind": "all_files",
                "finished_at": "2026-07-01T08:12:48+08:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
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


def test_drive_sync_status_treats_semantic_only_collision_as_safe_wait(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_drive_case_sync_all_files",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --direct-all-cases",
                }
            ]
        ),
        encoding="utf-8",
    )
    payload = {
        "ok": False,
        "status": "partial_failure",
        "action_required": True,
        "worker_kind": "all_files",
        "finished_at": "fresh",
        "file_sync_summary": {
            "semantic_collision_files": 125,
            "case_errors": 1,
            "incomplete_case_scans": 0,
        },
        "execution_summary": {"download_failed": 0, "upload_failed": 0},
        "drive_folder_summary": {"failed": 0},
        "drive_imported_folder_repair": {"errors": 0},
    }
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["blocking_kinds"] == []
    assert result["parsed"]["waiting_kinds"] == ["all_files"]
    assert result["parsed"]["status_by_kind"]["all_files"]["waiting"] is True
    assert result["parsed"]["status_by_kind"]["all_files"]["semantic_collision_files"] == 125


def test_drive_sync_status_blocks_unverified_transfer_beside_semantic_collision(
    tmp_path, monkeypatch
):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_drive_case_sync_all_files",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --direct-all-cases",
                }
            ]
        ),
        encoding="utf-8",
    )
    receipt_path = runtime / "drive_case_sync_worker_status_latest.json"
    for pending_field in ("upload_pending_unverified", "download_pending_unverified"):
        receipt_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "status": "partial_failure",
                    "action_required": True,
                    "worker_kind": "all_files",
                    "finished_at": "fresh",
                    "file_sync_summary": {
                        "semantic_collision_files": 8,
                        "incomplete_case_scans": 0,
                    },
                    "execution_summary": {
                        "download_failed": 0,
                        "upload_failed": 0,
                        pending_field: 1,
                    },
                    "drive_folder_summary": {"failed": 0},
                    "drive_imported_folder_repair": {"errors": 0},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

        result = live_check._drive_sync_status_live()

        assert result["ok"] is False
        assert result["parsed"]["blocking_kinds"] == ["all_files"]
        status = result["parsed"]["status_by_kind"]["all_files"]
        assert status["waiting"] is False
        assert status["hard_failure_count"] == 1


def test_drive_sync_status_treats_explicit_deferred_semantic_collision_as_safe_wait(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_drive_case_sync_all_files",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --direct-all-cases",
                }
            ]
        ),
        encoding="utf-8",
    )
    payload = {
        "ok": False,
        "status": "deferred",
        "reason": "semantic_path_collision_requires_human_review",
        "action_required": False,
        "worker_kind": "all_files",
        "finished_at": "fresh",
        "file_sync_summary": {
            "semantic_collision_files": 2,
            "incomplete_case_scans": 0,
        },
        "execution_summary": {"download_failed": 0, "upload_failed": 0},
        "drive_folder_summary": {"failed": 0},
        "drive_imported_folder_repair": {"errors": 0},
    }
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["blocking_kinds"] == []
    assert result["parsed"]["waiting_kinds"] == ["all_files"]
    assert result["parsed"]["status_by_kind"]["all_files"]["waiting_reason"] == (
        "semantic_path_collision_requires_human_review"
    )


def test_drive_sync_status_treats_storage_reconnect_as_safe_wait(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_drive_case_sync_all_files",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --direct-all-cases",
                }
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "deferred",
                "reason": "storage_unavailable",
                "action_required": False,
                "worker_kind": "all_files",
                "finished_at": "fresh",
                "file_sync_summary": {"pending_unverified_files": 83},
                "execution_summary": {
                    "download_failed": 0,
                    "upload_failed": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["blocking_kinds"] == []
    assert result["parsed"]["waiting_kinds"] == ["all_files"]
    assert result["parsed"]["status_by_kind"]["all_files"]["waiting_reason"] == (
        "storage_unavailable"
    )


def test_drive_sync_status_does_not_blanket_accept_unrelated_deferred_state(tmp_path, monkeypatch):
    # A safely cleared stale-running receipt is terminal evidence, not an
    # actionable failure that should remain red forever.
    candidate = {
        "payload": {
            "ok": False,
            "status": "stale_running_cleared",
            "action_required": False,
            "worker_kind": "priority",
            "finished_at": "old-but-terminal",
            "execution_summary": {"download_failed": 0, "upload_failed": 0},
            "file_sync_summary": {"incomplete_case_scans": 0},
        },
        "age_seconds": 99 * 3600,
    }
    cleared = live_check._drive_status_eval(candidate, max_age_hours=6)
    assert cleared["healthy"] is True
    assert cleared["waiting"] is True
    assert cleared["waiting_reason"] == "stale_running_marker_safely_cleared"

    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "deferred",
                "reason": "unknown_wait",
                "action_required": False,
                "worker_kind": "all_files",
                "finished_at": "fresh",
                "file_sync_summary": {"semantic_collision_files": 2, "incomplete_case_scans": 0},
                "execution_summary": {"download_failed": 0, "upload_failed": 0},
                "drive_folder_summary": {"failed": 0},
                "drive_imported_folder_repair": {"errors": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    assert result["parsed"]["blocking_kinds"] == ["all_files"]
    assert result["parsed"]["status_by_kind"]["all_files"]["waiting"] is False


def test_drive_sync_status_keeps_real_io_failure_blocking(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "partial_failure",
                "action_required": True,
                "worker_kind": "all_files",
                "finished_at": "fresh",
                "file_sync_summary": {"semantic_collision_files": 125, "incomplete_case_scans": 0},
                "execution_summary": {"download_failed": 1, "upload_failed": 0},
                "drive_folder_summary": {"failed": 0},
                "drive_imported_folder_repair": {"errors": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    assert result["parsed"]["blocking_kinds"] == ["all_files"]
    assert result["parsed"]["status_by_kind"]["all_files"]["waiting"] is False
    assert result["parsed"]["status_by_kind"]["all_files"]["hard_failure_count"] == 1


def test_drive_sync_status_keeps_file_level_unverified_items_blocking(
    tmp_path, monkeypatch
):
    for field_name in ("pending_unverified_files", "unverified_existing_files"):
        case_root = tmp_path / field_name
        runtime = case_root / ".runtime" / "drive_sync"
        runtime.mkdir(parents=True)
        (case_root / "cron_jobs.json").write_text(
            json.dumps(
                [
                    {
                        "id": "job_drive_case_sync_all_files",
                        "enabled": True,
                        "command": "python scripts/drive_case_sync_worker.py --direct-all-cases",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (runtime / "drive_case_sync_worker_status_latest.json").write_text(
            json.dumps(
                {
                    "ok": False,
                    "status": "partial_failure",
                    "action_required": True,
                    "worker_kind": "all_files",
                    "finished_at": "fresh",
                    "file_sync_summary": {
                        "semantic_collision_files": 8,
                        "incomplete_case_scans": 0,
                        field_name: 1,
                    },
                    "execution_summary": {
                        "download_failed": 0,
                        "upload_failed": 0,
                        "download_pending_unverified": 0,
                        "upload_pending_unverified": 0,
                    },
                    "drive_folder_summary": {"failed": 0},
                    "drive_imported_folder_repair": {"errors": 0},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(live_check, "REPO_ROOT", case_root)
        monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

        result = live_check._drive_sync_status_live()

        assert result["ok"] is False
        assert result["parsed"]["blocking_kinds"] == ["all_files"]
        status = result["parsed"]["status_by_kind"]["all_files"]
        assert status["waiting"] is False
        assert status["hard_failure_count"] == 1


def test_drive_sync_status_treats_only_existing_checksum_missing_conflicts_as_review(
    tmp_path, monkeypatch
):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([{"id": "job_drive_case_sync_all_files", "enabled": True,
                     "command": "python scripts/drive_case_sync_worker.py --direct-all-cases"}]),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({
            "ok": False, "status": "partial_failure", "action_required": True,
            "worker_kind": "all_files", "finished_at": "fresh",
            "file_sync_summary": {"semantic_collision_files": 2, "incomplete_case_scans": 0},
            "execution_summary": {
                "download_failed": 0, "upload_failed": 0,
                "download_pending_unverified": 0, "upload_pending_unverified": 2,
                "upload_pending_existing_checksum_missing_conflict": 2,
            },
            "drive_folder_summary": {"failed": 0},
            "drive_imported_folder_repair": {"errors": 0},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    status = result["parsed"]["status_by_kind"]["all_files"]
    assert status["waiting"] is True
    assert status["waiting_reason"] == "data_integrity_review"
    assert status["action_required"] is True
    assert status["data_integrity_review"] is True
    assert status["hard_failure_count"] == 0


def test_drive_sync_status_keeps_mixed_pending_items_blocking(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([{"id": "job_drive_case_sync_all_files", "enabled": True,
                     "command": "python scripts/drive_case_sync_worker.py --direct-all-cases"}]),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({
            "ok": False, "status": "partial_failure", "action_required": True,
            "worker_kind": "all_files", "finished_at": "fresh",
            "file_sync_summary": {
                "semantic_collision_files": 2, "incomplete_case_scans": 0,
                "pending_unverified_files": 1,
            },
            "execution_summary": {
                "download_failed": 0, "upload_failed": 0,
                "download_pending_unverified": 0, "upload_pending_unverified": 2,
                "upload_pending_existing_checksum_missing_conflict": 2,
            },
            "drive_folder_summary": {"failed": 0},
            "drive_imported_folder_repair": {"errors": 0},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    status = result["parsed"]["status_by_kind"]["all_files"]
    assert status["waiting"] is False
    assert status["data_integrity_review"] is False
    assert status["hard_failure_count"] == 3


def test_drive_sync_status_treats_only_file_plan_checksum_missing_as_review(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([{"id": "job_drive_case_sync_all_files", "enabled": True,
                     "command": "python scripts/drive_case_sync_worker.py --direct-all-cases"}]),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({
            "ok": False, "status": "partial_failure", "action_required": True,
            "worker_kind": "all_files", "finished_at": "fresh",
            "file_sync_summary": {
                "semantic_collision_files": 2, "incomplete_case_scans": 0,
                "pending_unverified_files": 1, "unverified_existing_files": 1,
                "pending_existing_checksum_missing_conflict": 1,
            },
            "execution_summary": {"download_failed": 0, "upload_failed": 0},
            "drive_folder_summary": {"failed": 0},
            "drive_imported_folder_repair": {"errors": 0},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    status = result["parsed"]["status_by_kind"]["all_files"]
    assert status["data_integrity_review"] is True
    assert status["file_plan_pending_existing_checksum_missing_conflict"] == 1


def test_drive_sync_status_rejects_current_local_hash_pending_shape(tmp_path, monkeypatch):
    """A storage/hash pending item must never inherit the safe-review label."""
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([{"id": "job_drive_case_sync_all_files", "enabled": True,
                     "command": "python scripts/drive_case_sync_worker.py --direct-all-cases"}]),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({
            "ok": False, "status": "partial_failure", "action_required": True,
            "worker_kind": "all_files", "finished_at": "fresh",
            "file_sync_summary": {
                "semantic_collision_files": 8, "case_errors": 2,
                "incomplete_case_scans": 0, "pending_unverified_files": 1,
                "unverified_existing_files": 1,
                "pending_existing_checksum_missing_conflict": 0,
            },
            "execution_summary": {
                "download_failed": 0, "upload_failed": 0,
                "download_pending_unverified": 0, "upload_pending_unverified": 0,
                "upload_pending_existing_checksum_missing_conflict": 0,
            },
            "drive_folder_summary": {"failed": 0},
            "drive_imported_folder_repair": {"errors": 0},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    status = result["parsed"]["status_by_kind"]["all_files"]
    assert status["data_integrity_review"] is False
    assert status["waiting"] is False
    assert status["hard_failure_count"] == 1


def test_drive_timeout_with_persisted_bounded_retry_is_waiting_not_red(
    tmp_path, monkeypatch
):
    runtime = tmp_path / ".runtime"
    drive = runtime / "drive_sync"
    drive.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([
            {
                "id": "job_drive_case_sync_all_files",
                "enabled": True,
                "command": "python scripts/drive_case_sync_worker.py --direct-all-cases",
            }
        ]),
        encoding="utf-8",
    )
    (drive / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({
            "ok": False,
            "status": "timeout",
            "action_required": False,
            "worker_kind": "all_files",
            "finished_at": "fresh",
        }),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps({
            "job_drive_case_sync_all_files": {
                "v3_retry": {"status": "queued", "attempt": 1, "max_attempts": 3}
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    status = result["parsed"]["status_by_kind"]["all_files"]
    assert status["waiting"] is True
    assert status["waiting_reason"] == "bounded_timeout_retry_scheduled"
    assert status["blocking_status"] is False


def test_drive_timeout_without_persisted_retry_remains_red(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    drive = runtime / "drive_sync"
    drive.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([
            {
                "id": "job_drive_case_sync_all_files",
                "enabled": True,
                "command": "python scripts/drive_case_sync_worker.py --direct-all-cases",
            }
        ]),
        encoding="utf-8",
    )
    (drive / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({
            "ok": False,
            "status": "timeout",
            "action_required": False,
            "worker_kind": "all_files",
            "finished_at": "fresh",
        }),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    assert result["parsed"]["blocking_kinds"] == ["all_files"]


def test_drive_sync_status_keeps_classified_smb_storage_pending_red_when_unverified(tmp_path, monkeypatch):
    """A retry hint is evidence only; it cannot turn an unverified item green."""
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([{"id": "job_drive_case_sync_all_files", "enabled": True,
                     "command": "python scripts/drive_case_sync_worker.py --direct-all-cases"}]),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps({
            "ok": False, "status": "partial_failure", "action_required": True,
            "worker_kind": "all_files", "finished_at": "fresh",
            "file_sync_summary": {
                "semantic_collision_files": 2, "incomplete_case_scans": 0,
                "pending_unverified_files": 1, "unverified_existing_files": 1,
                "smb_hash_storage_unavailable_files": 1,
            },
            "execution_summary": {"download_failed": 0, "upload_failed": 0},
            "drive_folder_summary": {"failed": 0},
            "drive_imported_folder_repair": {"errors": 0},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is False
    status = result["parsed"]["status_by_kind"]["all_files"]
    assert status["waiting"] is False
    assert status["data_integrity_review"] is False


def test_drive_sync_status_accepts_deferred_smb_storage_with_pure_collision_guard(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(json.dumps([{"id": "job_drive_case_sync_all_files", "enabled": True, "command": "python scripts/drive_case_sync_worker.py --direct-all-cases"}]), encoding="utf-8")
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(json.dumps({
        "ok": False, "status": "deferred", "reason": "storage_unavailable", "action_required": False,
        "worker_kind": "all_files", "finished_at": "fresh",
        "file_sync_summary": {"semantic_collision_files": 8, "case_errors": 2,
                              "pending_unverified_files": 1, "unverified_existing_files": 1,
                              "smb_hash_storage_unavailable_files": 1, "incomplete_case_scans": 0},
        "execution_summary": {"download_failed": 0, "upload_failed": 0,
                              "download_pending_unverified": 0, "upload_pending_unverified": 0},
        "drive_folder_summary": {"failed": 0}, "drive_imported_folder_repair": {"errors": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)
    result = live_check._drive_sync_status_live()
    status = result["parsed"]["status_by_kind"]["all_files"]
    assert result["ok"] is True
    assert status["waiting"] is True
    assert status["waiting_reason"] == "storage_unavailable"


def test_drive_status_kind_map_keeps_detailed_same_run_despite_rounding_jitter():
    detailed = {
        "source": "latest",
        "payload": {"worker_kind": "all_files", "status": "partial_failure"},
    }
    summary_only = {
        "source": "worker_state.last_status",
        "payload": {"worker_kind": "all_files", "status": "partial_failure"},
    }
    status = live_check._drive_status_kind_map(
        [
            (
                detailed,
                {
                    "worker_kind": "all_files",
                    "status": "partial_failure",
                    "age_seconds": 100.6,
                    "healthy": True,
                    "waiting": True,
                    "waiting_reason": "semantic_path_collision_requires_human_review",
                    "semantic_collision_files": 125,
                    "hard_failure_count": 0,
                },
            ),
            (
                summary_only,
                {
                    "worker_kind": "all_files",
                    "status": "partial_failure",
                    "age_seconds": 100.7,
                    "healthy": False,
                    "waiting": False,
                    "waiting_reason": "",
                    "semantic_collision_files": 0,
                    "hard_failure_count": 0,
                },
            ),
        ]
    )

    assert status["all_files"]["source"] == "latest"
    assert status["all_files"]["ok"] is True
    assert status["all_files"]["waiting"] is True
    assert status["all_files"]["semantic_collision_files"] == 125


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


def test_calendar_todo_status_rejects_hidden_pdf_scan_failure(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "osc_events_refresh_latest.json").write_text(
        json.dumps(
            {
                "calendar_audit": {"ok": True},
                "calendar_import": {"ok": True},
                "calendar_push": {"ok": True, "failed": 0},
                "calendar_source_audit": {"ok": True},
                "pdf_calendar_scan": {
                    "ok": True,
                    "targets": 100,
                    "scanned": 60,
                    "error_count": 1,
                    "timeout_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)

    result = live_check._calendar_todo_status_live()

    assert result["ok"] is False
    assert "calendar_pdf_scan_failed" in result["parsed"]["reason"]
    assert result["parsed"]["pdf_coverage_percent"] == 60.0


def test_calendar_todo_status_counts_identity_bound_cache_as_verified_coverage(
    tmp_path, monkeypatch
):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "osc_events_refresh_latest.json").write_text(
        json.dumps(
            {
                "calendar_audit": {"ok": True},
                "calendar_import": {"ok": True},
                "calendar_push": {"ok": True, "failed": 0},
                "calendar_source_audit": {"ok": True},
                "pdf_calendar_scan": {
                    "ok": True,
                    "targets": 100,
                    "scanned": 60,
                    "cache_skipped": 40,
                    "error_count": 0,
                    "timeout_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_age_seconds", lambda _path: 60)

    result = live_check._calendar_todo_status_live()

    assert result["ok"] is True
    assert result["parsed"]["pdf_verified"] == 100
    assert result["parsed"]["pdf_coverage_percent"] == 100.0


def test_calendar_todo_status_fails_closed_on_unaccounted_targets(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "osc_events_refresh_latest.json").write_text(
        json.dumps(
            {
                "calendar_audit": {"ok": True},
                "calendar_import": {"ok": True},
                "calendar_push": {"ok": True, "failed": 0},
                "calendar_source_audit": {"ok": True},
                "pdf_calendar_scan": {
                    "ok": True,
                    "targets": 100,
                    "scanned": 60,
                    "cache_skipped": 39,
                    "error_count": 0,
                    "timeout_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_age_seconds", lambda _path: 60)

    result = live_check._calendar_todo_status_live()

    assert result["ok"] is False
    assert "calendar_pdf_coverage_incomplete" in result["parsed"]["reason"]
    assert result["parsed"]["pdf_verified"] == 99


def test_file_review_coverage_rejects_unverified_status_semantics(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-04T10:00:00",
                "result": {
                    "ok": True,
                    "portal_verified": True,
                    "check": {"parsed": {"portal_probe_ok": True, "scan_errors": 0}},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live()

    assert result["ok"] is False
    assert "unverified_portal_status_semantics" in result["parsed"]["reason"]


def test_file_review_coverage_accepts_fresh_live_worker_phase(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-05T15:20:00",
                "phase": "running_scheduled_check",
                "pid": 1234,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: pid == 1234)

    result = live_check._file_review_ingestion_coverage_live()

    assert result["ok"] is True
    assert result["parsed"]["active_running"] is True
    assert result["parsed"]["phase"] == "running_scheduled_check"


def test_file_review_coverage_accepts_current_verified_portal_probe(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-07T10:00:00",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": False,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": False,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                            "recent_unnotified_count": 0,
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)
    probe = {
        "name": "file_review_downloadable_probe",
        "ok": True,
        "parsed": {
            "success": True,
            "source": "portal",
            "portal": {
                "success": True,
                "raw_count": 275,
                "case_count": 213,
            },
        },
    }

    result = live_check._file_review_ingestion_coverage_live(portal_probe=probe)

    assert result["ok"] is True
    assert result["parsed"]["portal_verified"] is True
    assert result["parsed"]["portal_verified_by_current_live_probe"] is True
    assert result["parsed"]["portal_raw_rows"] == 275
    assert result["parsed"]["portal_cases"] == 213


def test_file_review_coverage_rejects_fresh_downloads_hidden_by_deferred_worker(
    tmp_path, monkeypatch
):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-10T01:15:58",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": False,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": False,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                            "recent_unnotified_count": 0,
                        }
                    },
                    "download": {
                        "success": True,
                        "status": "deferred",
                        "deferred": True,
                        "reason": "portal_probe_deferred",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "source": "portal",
                "portal": {
                    "success": True,
                    "raw_count": 275,
                    "case_count": 213,
                    "downloadable_count": 8,
                },
            },
        }
    )

    assert result["ok"] is False
    assert "portal_downloads_waiting_worker" in result["parsed"]["reason"]
    assert result["parsed"]["portal_downloadable_current"] == 8
    assert result["parsed"]["download_verified"] is False


def test_file_review_coverage_rejects_naked_zero_file_success(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-18T19:21:53",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": True,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": True,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                            "recent_unnotified_count": 0,
                        }
                    },
                    "download": {"success": True, "downloaded_count": 0},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "source": "portal",
                "portal": {"success": True, "downloadable_count": 7},
            },
        }
    )

    assert result["ok"] is False
    assert "portal_downloads_waiting_worker" in result["parsed"]["reason"]
    assert result["parsed"]["download_verified"] is False
    assert result["parsed"]["download_signature_contract"] is False
    assert result["parsed"]["live_signature_contract"] is False


def test_file_review_coverage_accepts_reconciled_existing_files(tmp_path, monkeypatch):
    receipt = _coverage_portal_receipt("existing", 7)
    handled = [
        *receipt["portal_download_signature_hashes"],
        *_coverage_portal_receipt("older-existing", 6)[
            "portal_download_signature_hashes"
        ],
    ]
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-18T19:21:53",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": True,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": True,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                            "recent_unnotified_count": 0,
                        }
                    },
                    "download": {
                        "downloaded_count": 0,
                        "verified_existing_count": 13,
                        **_coverage_download_receipt(receipt, handled),
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "source": "portal",
                "portal": {
                    "success": True,
                    "downloadable_count": 7,
                    **receipt,
                },
            },
        }
    )

    assert result["ok"] is True
    assert result["parsed"]["download_verified"] is True
    assert result["parsed"]["download_reconciled_expected"] == 7
    assert result["parsed"]["download_reconciled_accounted"] == 13
    assert result["parsed"]["download_same_snapshot"] is True
    assert result["parsed"]["live_signature_subset_accounted"] is True


def test_file_review_coverage_accepts_exact_cross_case_cooldown(
    tmp_path, monkeypatch
):
    receipt = _coverage_portal_receipt("cross-case-cooldown", 7)
    expected = receipt["portal_download_signature_hashes"]
    handled = expected[:5]
    deferred = expected[5:]
    download = _coverage_download_receipt(receipt, handled)
    accounted = [*handled, *deferred]
    download.update(
        {
            "status": "deferred",
            "deferred": True,
            "reason": "court_payload_identity_mismatch",
            "accounted_portal_downloadable_count": 7,
            "mismatch_deferred_portal_signature_hashes": deferred,
            "mismatch_deferred_portal_signature_set_hash": signature_set_hash(
                deferred
            ),
            "accounted_portal_signature_hashes": accounted,
            "accounted_portal_signature_set_hash": signature_set_hash(accounted),
        }
    )
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-18T19:21:53",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": True,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": True,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                            "recent_unnotified_count": 0,
                        }
                    },
                    "download": download,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "source": "portal",
                "portal": {
                    "success": True,
                    "downloadable_count": 7,
                    **receipt,
                },
            },
        }
    )

    assert result["ok"] is True
    assert result["parsed"]["download_verified"] is True
    assert result["parsed"]["download_mismatch_deferred"] == 2
    assert result["parsed"]["download_mismatch_deferred_verified"] is True


def test_file_review_coverage_rejects_nonidentity_deferred_signatures(
    tmp_path, monkeypatch
):
    receipt = _coverage_portal_receipt("wrong-deferred-reason", 1)
    expected = receipt["portal_download_signature_hashes"]
    download = _coverage_download_receipt(receipt, [])
    download.update(
        {
            "status": "deferred",
            "deferred": True,
            "reason": "download_time_budget_exhausted",
            "mismatch_deferred_portal_signature_hashes": expected,
            "mismatch_deferred_portal_signature_set_hash": signature_set_hash(
                expected
            ),
            "accounted_portal_signature_hashes": expected,
            "accounted_portal_signature_set_hash": signature_set_hash(expected),
        }
    )
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-18T19:21:53",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": True,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": True,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                            "recent_unnotified_count": 0,
                        }
                    },
                    "download": download,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "source": "portal",
                "portal": {
                    "success": True,
                    "downloadable_count": 1,
                    **receipt,
                },
            },
        }
    )

    assert result["ok"] is False
    assert result["parsed"]["download_verified"] is False
    assert "portal_downloads_waiting_worker" in result["parsed"]["reason"]


def test_file_review_coverage_rejects_noncanonical_mismatch_signature_list(
    tmp_path, monkeypatch
):
    receipt = _coverage_portal_receipt("bad-mismatch-list", 1)
    expected = receipt["portal_download_signature_hashes"]
    download = _coverage_download_receipt(receipt, [])
    download.update(
        {
            "status": "deferred",
            "deferred": True,
            "reason": "court_payload_identity_mismatch",
            "mismatch_deferred_portal_signature_hashes": [
                *expected,
                "not-a-signature",
            ],
            "mismatch_deferred_portal_signature_set_hash": signature_set_hash(
                expected
            ),
            "accounted_portal_signature_hashes": expected,
            "accounted_portal_signature_set_hash": signature_set_hash(expected),
        }
    )
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-18T19:21:53",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": True,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": True,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                            "recent_unnotified_count": 0,
                        }
                    },
                    "download": download,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "source": "portal",
                "portal": {
                    "success": True,
                    "downloadable_count": 1,
                    **receipt,
                },
            },
        }
    )

    assert result["ok"] is False
    assert result["parsed"]["download_signature_contract"] is False
    assert result["parsed"]["download_verified"] is False


def test_business_snapshot_and_menubar_show_safe_cross_case_wait(monkeypatch, tmp_path):
    receipt = _coverage_portal_receipt("menu-cross-case", 2)
    hashes = receipt["portal_download_signature_hashes"]
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "result": {
                    "ok": True,
                    "portal_verified": True,
                    "check": {
                        "parsed": {
                            "portal_status_semantics": "ola-current-state-v2",
                            "ready_to_download_count": 2,
                            "portal_pending_payment_count": 0,
                        }
                    },
                    "download": {
                        "success": True,
                        "status": "deferred",
                        "deferred": True,
                        "reason": "court_payload_identity_mismatch",
                        "download_reconciliation_verified": True,
                        "mismatch_deferred_portal_signature_hashes": hashes,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness_snapshot, "_mutable_static_dir", lambda *_a: static)
    monkeypatch.setattr(readiness_snapshot, "_latest_file_review_job", lambda *_a: {})
    monkeypatch.setattr(
        readiness_snapshot,
        "_scheduled_file_review_download_enabled",
        lambda *_a: True,
    )
    monkeypatch.setattr(readiness_snapshot, "_operations", lambda *_a: {})
    monkeypatch.setattr(
        readiness_snapshot,
        "_recent_report_failures",
        lambda *_a, **_k: {"count": 0, "reasons": {}, "items": []},
    )

    snapshot = readiness_snapshot.build_snapshot(
        root=tmp_path,
        env={"MAGI_FILE_REVIEW_UNATTENDED_MODE": "1"},
        now=datetime(2026, 8, 19, 22, 0, 0),
        mlx_available=True,
        whisper_cli="whisper",
    )
    item = snapshot["items"]["閱卷下載"]

    assert item["state"] == "waiting"
    assert item["court_payload_waiting"] == 2
    assert item["label"] == "2件法院資料待更新"
    detail = _business_readiness_detail("閱卷下載", item)
    assert "2 件下載列曾回傳其他案件卷宗" in detail
    assert "已隔離錯案檔案" in detail


def test_file_review_coverage_rejects_same_count_different_signature_batch(
    tmp_path, monkeypatch
):
    worker_receipt = _coverage_portal_receipt("worker-batch", 7)
    live_receipt = _coverage_portal_receipt("new-live-batch", 7)
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-18T19:21:53",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": True,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": True,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                            "recent_unnotified_count": 0,
                        }
                    },
                    "download": _coverage_download_receipt(
                        worker_receipt,
                        worker_receipt["portal_download_signature_hashes"],
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "source": "portal",
                "portal": {
                    "success": True,
                    "downloadable_count": 7,
                    **live_receipt,
                },
            },
        }
    )

    assert result["ok"] is False
    assert "portal_downloads_waiting_worker" in result["parsed"]["reason"]
    assert result["parsed"]["download_same_snapshot"] is False
    assert result["parsed"]["live_signature_subset_accounted"] is False


def test_file_review_coverage_accepts_live_signature_subset(tmp_path, monkeypatch):
    worker_receipt = _coverage_portal_receipt("worker-subset", 7)
    live_hashes = worker_receipt["portal_download_signature_hashes"][:3]
    live_receipt = {
        "portal_download_receipt_schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
        "portal_download_signature_hashes": live_hashes,
        "portal_download_signature_set_hash": signature_set_hash(live_hashes),
        "portal_probe_snapshot_fingerprint": portal_snapshot_fingerprint(live_hashes),
        "portal_probe_observed_at": "2026-08-18T19:22:00+08:00",
    }
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-18T19:21:53",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": True,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": True,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                            "recent_unnotified_count": 0,
                        }
                    },
                    "download": _coverage_download_receipt(
                        worker_receipt,
                        worker_receipt["portal_download_signature_hashes"],
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "source": "portal",
                "portal": {
                    "success": True,
                    "downloadable_count": 3,
                    **live_receipt,
                },
            },
        }
    )

    assert result["ok"] is True
    assert result["parsed"]["download_same_snapshot"] is False
    assert result["parsed"]["live_signature_subset_accounted"] is True


def test_file_review_coverage_rejects_failed_current_portal_probe(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-07T10:00:00",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": False,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": False,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": False,
            "parsed": {"success": False, "source": "portal", "portal": {"success": False}},
        }
    )

    assert result["ok"] is False
    assert "portal_not_verified" in result["parsed"]["reason"]


def test_file_review_coverage_accepts_verified_busy_live_owner(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-07T10:00:00",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": False,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": False,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: pid == 22355)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "status": "deferred",
                "deferred": True,
                "reason": "file_review_portal_busy",
                "active_pid": 22355,
            },
        }
    )

    assert result["ok"] is True
    assert result["parsed"]["portal_verified"] is False
    assert result["parsed"]["portal_waiting_on_active_owner"] is True
    assert result["parsed"]["portal_active_owner_pid"] == 22355


def test_file_review_coverage_rejects_busy_probe_with_dead_owner(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "file_review_auto_state.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-08-07T10:00:00",
                "phase": "cycle_complete",
                "result": {
                    "ok": True,
                    "portal_verified": False,
                    "check": {
                        "parsed": {
                            "portal_probe_ok": False,
                            "portal_status_semantics": "ola-current-state-v2",
                            "scan_errors": 0,
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: False)

    result = live_check._file_review_ingestion_coverage_live(
        portal_probe={
            "ok": True,
            "parsed": {
                "success": True,
                "status": "deferred",
                "deferred": True,
                "reason": "file_review_portal_busy",
                "active_pid": 22355,
            },
        }
    )

    assert result["ok"] is False
    assert result["parsed"]["portal_waiting_on_active_owner"] is False
    assert "portal_not_verified" in result["parsed"]["reason"]


def test_transcript_coverage_reports_incomplete_cycle_without_false_failure(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "transcript_sync"
    runtime.mkdir(parents=True)
    (runtime / "transcript_sync_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "2026-08-04T10:00:00",
                "summary": {"retry_pending_cases_count": 2, "failed_cases_count": 0},
                "sync_status": {
                    "success": True,
                    "eligible_cases": 79,
                    "cycle_scanned_cases": 53,
                    "last_cycle_completed_at": "2026-08-04T00:00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._transcript_sync_coverage_live()

    assert result["ok"] is True
    assert result["parsed"]["coverage_state"] == "in_progress"
    assert result["parsed"]["remaining_cases"] == 26
    assert result["parsed"]["retry_pending_cases"] == 2


def test_transcript_coverage_accepts_stale_success_while_verified_worker_is_running(
    tmp_path, monkeypatch
):
    runtime = tmp_path / ".runtime" / "transcript_sync"
    agent = tmp_path / ".agent"
    runtime.mkdir(parents=True)
    agent.mkdir()
    (runtime / "transcript_sync_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": "stale",
                "summary": {"retry_pending_cases_count": 2, "failed_cases_count": 0},
                "sync_status": {
                    "success": True,
                    "eligible_cases": 80,
                    "cycle_scanned_cases": 40,
                    "last_cycle_completed_at": "stale",
                },
            }
        ),
        encoding="utf-8",
    )
    (agent / "transcript_sync.lock").write_text(
        json.dumps({"pid": 4321, "started_at": "fresh"}), encoding="utf-8"
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("MAGI_AGENT_DIR", str(agent))
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(
        live_check,
        "_artifact_age_seconds",
        lambda *args, **kwargs: (live_check.TRANSCRIPT_SYNC_STATUS_SLA_HOURS + 1) * 3600,
    )
    monkeypatch.setattr(
        live_check,
        "_iso_age_seconds",
        lambda value: (
            60
            if value == "fresh"
            else (live_check.TRANSCRIPT_FULL_CYCLE_SLA_HOURS + 1) * 3600
        ),
    )

    result = live_check._transcript_sync_coverage_live()

    assert result["ok"] is True
    assert result["parsed"]["active_running"] is True
    assert result["parsed"]["active_pid"] == 4321
    assert result["parsed"]["coverage_state"] == "running"
    assert result["parsed"]["reason"] == ""


def test_transcript_coverage_does_not_hide_recorded_failures_while_worker_runs(
    tmp_path, monkeypatch
):
    runtime = tmp_path / ".runtime" / "transcript_sync"
    agent = tmp_path / ".agent"
    runtime.mkdir(parents=True)
    agent.mkdir()
    (runtime / "transcript_sync_latest.json").write_text(
        json.dumps(
            {
                "ok": False,
                "summary": {"failed_cases_count": 1},
                "sync_status": {"success": False, "eligible_cases": 1},
            }
        ),
        encoding="utf-8",
    )
    (agent / "transcript_sync.lock").write_text(
        json.dumps({"pid": 4321, "started_at": "fresh"}), encoding="utf-8"
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("MAGI_AGENT_DIR", str(agent))
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(live_check, "_iso_age_seconds", lambda value: 60)

    result = live_check._transcript_sync_coverage_live()

    assert result["ok"] is False
    assert result["parsed"]["active_running"] is True
    assert "transcript_sync_failed" in result["parsed"]["reason"]
    assert "transcript_case_failures" in result["parsed"]["reason"]


def test_drive_sync_exposes_full_sweep_throughput(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "status": "ok",
                "worker_kind": "all_files",
                "all_case_offset_before": 24,
                "all_case_offset_after": 28,
                "all_case_total": 216,
                "all_case_numbers": ["a", "b", "c", "d"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "_age_seconds", lambda path: 60)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["all_case_sweep_batch"] == 4
    assert result["parsed"]["all_case_sweep_remaining"] == 188
    assert result["parsed"]["all_case_sweep_throughput_warning"] is False


def test_drive_sync_running_without_committed_offset_is_not_reported_complete(
    tmp_path, monkeypatch
):
    runtime = tmp_path / ".runtime" / "drive_sync"
    runtime.mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_drive_case_sync_all_files",
                    "enabled": True,
                    "command": "python scripts/drive_case_sync_worker.py --direct-all-cases --direct-all-case-limit 4",
                }
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "drive_case_sync_worker_status_latest.json").write_text(
        json.dumps(
            {
                "status": "direct_all_case_sync_running",
                "pid": 1234,
                "worker_kind": "all_files",
                "all_case_total": 217,
                "all_case_numbers": ["case-a", "case-b", "case-c", "case-d"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_check, "RUNTIME_DIR", tmp_path / ".runtime")
    monkeypatch.setattr(
        live_check, "_enabled_drive_sync_worker_kinds", lambda root: {"all_files"}
    )
    monkeypatch.setattr(live_check, "_pid_alive", lambda pid: pid == 1234)
    monkeypatch.setattr(live_check, "_age_seconds", lambda path: 30)

    result = live_check._drive_sync_status_live()

    assert result["ok"] is True
    assert result["parsed"]["all_case_sweep_state"] == "running"
    assert result["parsed"]["all_case_sweep_position"] == 0
    assert result["parsed"]["all_case_sweep_remaining"] == 217
    assert result["parsed"]["all_case_sweep_estimated_cycles"] == 55


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
    runtime_root = Path("/").joinpath(
        "Users", "example", "Library", "Application Support", "MAGI", "runtime", "MAGI_v2"
    )
    command = (
        f"'{runtime_root}/venv/bin/python3' "
        f"'{runtime_root}/scripts/ops/run_after_token_refresh.py' "
        f"-- '{runtime_root}/venv/bin/python3' "
        f"'{runtime_root}/scripts/ops/osc_events_refresh.py'"
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


def test_live_runtime_fingerprint_uses_active_v3_root_without_legacy_env(tmp_path, monkeypatch):
    release = tmp_path / "MAGI" / "releases" / "v3-test"
    release.mkdir(parents=True)
    monkeypatch.delenv("MAGI_LIVE_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-test")
    monkeypatch.setenv("MAGI_ROOT_DIR", str(release))
    monkeypatch.setattr(live_check, "REPO_ROOT", release)

    result = live_check._live_runtime_root_live()

    assert result["ok"] is True
    assert result["parsed"]["runtime_root"] == str(release)
    assert result["parsed"]["same_root"] is True


def test_release_local_environment_overrides_stale_shared_dotenv_paths(tmp_path, monkeypatch):
    release = tmp_path / "MAGI" / "releases" / "v3-current"
    (release / "casper_ecosystem" / "law_firm_orchestrators").mkdir(parents=True)
    (release / "release-manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(live_check, "REPO_ROOT", release)
    for key in live_check._RELEASE_BOUND_ENV:
        monkeypatch.setenv(key, "/old/releases/v3-old")

    bound = live_check._bind_release_local_environment()

    assert bound["MAGI_ROOT_DIR"] == str(release.resolve())
    assert bound["MAGI_ORCH_DIR"] == str(
        (release / "casper_ecosystem" / "law_firm_orchestrators").resolve()
    )
    assert all("v3-old" not in value for value in bound.values())


def test_host_singleton_audit_rejects_older_release_reference(tmp_path, monkeypatch):
    release = tmp_path / "MAGI" / "releases" / "v3-current"
    release.mkdir(parents=True)
    (release / "release-manifest.json").write_text("{}", encoding="utf-8")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    payload = {
        "Label": "com.magi.input-method-watchdog",
        "ProgramArguments": [
            "/usr/bin/python3",
            str(tmp_path / "MAGI" / "releases" / "v3-old" / "scripts" / "ops" / "input_method_watchdog.py"),
        ],
    }
    (agents / "com.magi.input-method-watchdog.plist").write_bytes(
        plistlib.dumps(payload)
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", release)
    monkeypatch.setenv("MAGI_LAUNCHAGENTS_DIR", str(agents))

    result = live_check._host_singleton_release_bindings_live()

    assert result["ok"] is False
    assert result["parsed"]["drift"] == [
        {
            "label": "com.magi.input-method-watchdog",
            "reason": "older_release_reference",
            "release": "v3-old",
        }
    ]


def test_host_singleton_audit_accepts_active_release_root_and_children(tmp_path, monkeypatch):
    release = tmp_path / "MAGI" / "releases" / "v3-current"
    release.mkdir(parents=True)
    (release / "release-manifest.json").write_text("{}", encoding="utf-8")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    payload = {
        "Label": "com.magi.input-method-watchdog",
        "EnvironmentVariables": {"MAGI_ROOT": str(release)},
        "ProgramArguments": [
            "/usr/bin/python3",
            str(release / "scripts" / "ops" / "input_method_watchdog.py"),
        ],
    }
    (agents / "com.magi.input-method-watchdog.plist").write_bytes(
        plistlib.dumps(payload)
    )
    monkeypatch.setattr(live_check, "REPO_ROOT", release)
    monkeypatch.setenv("MAGI_LAUNCHAGENTS_DIR", str(agents))

    result = live_check._host_singleton_release_bindings_live()

    assert result["ok"] is True
    assert result["parsed"]["drift"] == []


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


def test_notify_accepts_red_phone_delivered_shape(monkeypatch):
    monkeypatch.setenv("MAGI_BUSINESS_LIVE_CHECK_NOTIFY", "1")
    import sys

    fake = SimpleNamespace(
        send_telegram_push_with_status=lambda *_args, **_kwargs: {
            "telegram": True,
            "delivered": True,
            "queued": False,
        }
    )
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake)

    result = live_check._notify("健康檢查")

    assert result == {"requested": True, "ok": True, "delivery": "sent", "queued": False}


def test_notification_failure_fails_live_check_contract(tmp_path, monkeypatch):
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
    assert payload["success"] is False
    assert payload["notification_ok"] is False
    assert payload["notification"]["delivery"] == "failed"
    notification = next(item for item in payload["results"] if item["name"] == "notification_delivery")
    assert notification["ok"] is False
    assert notification["business_impact"] is False


def test_process_hygiene_accepts_only_receipt_bound_v3_launchd_role(tmp_path, monkeypatch):
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    release_id = "v3-test-release"
    pid = 8123
    (pid_dir / "control.pid").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "role": "control",
                "pid": pid,
                "process_group": pid,
                "release_id": release_id,
                "release_root": f"/tmp/releases/{release_id}",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_V3_PID_FILE", str(pid_dir / "supervisor.pid"))
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", release_id)
    monkeypatch.setattr(process_hygiene.os, "getpgid", lambda observed: observed)
    process = {
        "pid": pid,
        "ppid": 1,
        "stat": "S",
        "etime": "01:02",
        "command": (
            "/usr/bin/python3 -c \"import runpy; "
            "runpy.run_module('magi_v3.control', run_name='__main__')\" "
            f"/tmp/.magi-{release_id}-owner"
        ),
    }

    assert process_hygiene.scan_orphans([process]) == []
    assert process_hygiene.scan_stuck([{**process, "etime": "03:00:00"}]) == []

    receipt = json.loads((pid_dir / "control.pid").read_text(encoding="utf-8"))
    receipt["pid"] = pid + 1
    (pid_dir / "control.pid").write_text(json.dumps(receipt), encoding="utf-8")
    assert len(process_hygiene.scan_orphans([process])) == 1


def test_unlocked_autopilot_anchor_is_not_a_stale_runtime_lock(tmp_path, monkeypatch):
    lock_path = tmp_path / "_autopilot.lock"
    lock_path.write_text("4031,1700000000", encoding="utf-8")
    monkeypatch.setattr(
        hardening,
        "_legacy_pid_file_paths",
        lambda: [(lock_path, "legacy_autopilot")],
    )
    monkeypatch.setattr(hardening, "_legacy_lock_is_held", lambda _path: False)

    report = hardening._audit_legacy_runtime_pid_files(cleanup=False)

    assert report["ok"] is True
    assert report["stale_count"] == 0
    assert report["inactive_anchor_count"] == 1


def test_held_autopilot_lock_with_dead_owner_remains_actionable(tmp_path, monkeypatch):
    lock_path = tmp_path / "_autopilot.lock"
    lock_path.write_text("4031,1700000000", encoding="utf-8")
    monkeypatch.setattr(
        hardening,
        "_legacy_pid_file_paths",
        lambda: [(lock_path, "legacy_autopilot")],
    )
    monkeypatch.setattr(hardening, "_legacy_lock_is_held", lambda _path: True)
    monkeypatch.setattr(hardening, "_pid_alive", lambda _pid: False)

    report = hardening._audit_legacy_runtime_pid_files(cleanup=False)

    assert report["ok"] is False
    assert report["stale_count"] == 1


def test_judicial_transcript_sweep_is_not_audio_transcription_or_human_work(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    sync_dir = runtime / "transcript_sync"
    sync_dir.mkdir(parents=True)
    now = datetime(2026, 8, 6, 12, 0, 0)
    (sync_dir / "transcript_sync_latest.json").write_text(
        json.dumps(
            {
                "ok": True,
                "created_at": now.isoformat(),
                "sync_status": {
                    "success": True,
                    "eligible_cases": 80,
                    "cycle_scanned_cases": 77,
                    "last_cycle_completed_at": now.isoformat(),
                },
                "summary": {"retry_pending_cases_count": 15, "failed_cases_count": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness_snapshot, "_operations", lambda _exec_fn: {})
    monkeypatch.setattr(readiness_snapshot, "_scheduled_file_review_download_enabled", lambda _root: False)

    result = readiness_snapshot.build_snapshot(
        root=tmp_path,
        env={
            "MAGI_RUNTIME_DIR": str(runtime),
            "MAGI_MUTABLE_STATIC_DIR": str(tmp_path / "static"),
            "MAGI_AGENT_DIR": str(tmp_path / "agent"),
            "MAGI_FILE_REVIEW_UNATTENDED_MODE": "1",
        },
        now=now,
        mlx_available=False,
        whisper_cli=None,
    )

    assert "錄音轉文字" not in result["items"]
    item = result["items"]["筆錄下載"]
    assert item["state"] == "ok"
    assert item["label"] == "背景輪巡中 77/80・15案自動復核"
    assert item["kind"] == "judicial_transcript_download"


def test_judicial_transcript_detail_explains_no_user_action():
    detail = _business_readiness_detail(
        "筆錄下載",
        {
            "eligible_cases": 80,
            "cycle_scanned_cases": 77,
            "remaining_cases": 3,
            "retry_pending_cases": 15,
            "failed_cases": 0,
        },
    )

    assert "不是錄音轉文字作業" in detail
    assert "不需要人工處理" in detail


def test_optional_nvidia_heavy_disabled_is_healthy_and_explains_local_fallback(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(readiness_snapshot, "_operations", lambda _exec_fn: {})
    monkeypatch.setattr(
        readiness_snapshot,
        "_scheduled_file_review_download_enabled",
        lambda _root: False,
    )

    result = readiness_snapshot.build_snapshot(
        root=tmp_path,
        env={
            "MAGI_RUNTIME_DIR": str(tmp_path / "runtime"),
            "MAGI_MUTABLE_STATIC_DIR": str(tmp_path / "static"),
            "MAGI_AGENT_DIR": str(tmp_path / "agent"),
            "NVIDIA_NIM_ENABLE": "0",
        },
        now=datetime(2026, 8, 20, 21, 0, 0),
    )

    item = result["items"]["NVIDIA重型"]
    assert item == {
        "state": "ok",
        "label": "選配未啟用",
        "model": "",
        "enabled": False,
    }
    assert "本機模型與一般業務功能仍正常運作" in _business_readiness_detail(
        "NVIDIA重型", item
    )


def test_enabled_nvidia_heavy_without_model_is_a_real_configuration_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(readiness_snapshot, "_operations", lambda _exec_fn: {})
    monkeypatch.setattr(
        readiness_snapshot,
        "_scheduled_file_review_download_enabled",
        lambda _root: False,
    )

    result = readiness_snapshot.build_snapshot(
        root=tmp_path,
        env={
            "MAGI_RUNTIME_DIR": str(tmp_path / "runtime"),
            "MAGI_MUTABLE_STATIC_DIR": str(tmp_path / "static"),
            "MAGI_AGENT_DIR": str(tmp_path / "agent"),
            "NVIDIA_NIM_ENABLE": "1",
        },
        now=datetime(2026, 8, 20, 21, 0, 0),
    )

    item = result["items"]["NVIDIA重型"]
    assert item == {
        "state": "attention",
        "label": "設定不完整",
        "model": "",
        "enabled": True,
    }
    assert "尚未設定模型名稱" in _business_readiness_detail("NVIDIA重型", item)
