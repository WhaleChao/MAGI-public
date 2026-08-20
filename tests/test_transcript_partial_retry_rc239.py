from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "skills" / "transcript-downloader" / "action.py"


def _load_action():
    spec = importlib.util.spec_from_file_location("transcript_partial_retry_rc239", ACTION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Downloader:
    def __init__(self, cases, errors):
        self._cases = cases
        self._errors = iter(errors)
        self._last_download_error = ""
        self._last_no_new_files_reason = ""

    def cleanup_download_folder(self):
        return {}

    def login(self):
        return True

    def get_cases_from_db(self):
        return self._cases

    def download_record(self, _case):
        self._last_download_error = next(self._errors)
        return []


def _case(number):
    return SimpleNamespace(
        case_number=number,
        court_name="臺灣花蓮地方法院",
        court_case_number=f"115年度訴字第{number[-1]}號",
        case_type="民事",
        client_name=f"當事人{number[-1]}",
        folder_path=f"/cases/{number}",
    )


def _run(monkeypatch, tmp_path, errors):
    module = _load_action()
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_LOCK_PATH", tmp_path / "lock.json")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_CASE_DELAY_SEC", 0)
    cases = [_case(f"2026-000{i + 1}") for i in range(len(errors))]
    return module._download_sync_batch(
        _Downloader(cases, errors),
        batch_size=len(cases),
        notify=False,
    )


def test_minor_inconclusive_results_are_retained_for_retry_without_failing_job(monkeypatch, tmp_path):
    retryable = "unverified_no_pdf_links: no verifiable PDF or empty state"
    result = _run(monkeypatch, tmp_path, ["", "", "", retryable])

    assert result["success"] is True
    assert result["partial"] is True
    assert result["status"] == "partial_retry_pending"
    assert result["retry_pending_count"] == 1
    failed = [row for row in result["cases"] if not row["success"]]
    assert len(failed) == 1
    assert failed[0]["status"] == "search_failed"


def test_nine_inconclusive_results_in_twenty_four_cases_do_not_raise_false_batch_failure(monkeypatch, tmp_path):
    """Regression for the 2026-08-02 21:00 LIVE batch (15 completed / 9 retryable)."""
    retryable = "unverified_no_pdf_links: no verifiable PDF or empty state"
    result = _run(monkeypatch, tmp_path, ([""] * 15) + ([retryable] * 9))

    assert result["success"] is True
    assert result["partial"] is True
    assert result["status"] == "partial_retry_pending"
    assert result["retry_pending_count"] == 9
    assert "error" not in result


def test_systemic_inconclusive_results_still_fail_closed(monkeypatch, tmp_path):
    retryable = "unverified_no_pdf_links: no verifiable PDF or empty state"
    result = _run(monkeypatch, tmp_path, ["", retryable, retryable])

    assert result["success"] is True
    assert result["partial"] is True
    assert result["status"] == "partial_retry_pending"
    assert result["retry_pending_count"] == 2
    assert "error" not in result


def test_fourteen_inconclusive_results_never_emit_a_false_batch_failure(monkeypatch, tmp_path):
    retryable = "unverified_no_pdf_links: no verifiable PDF or empty state"
    result = _run(monkeypatch, tmp_path, ([""] * 10) + ([retryable] * 14))

    assert result["success"] is True
    assert result["partial"] is True
    assert result["status"] == "partial_retry_pending"
    assert result["retry_pending_count"] == 14
    assert "error" not in result


def test_transcript_report_receipt_requires_a_persisted_report(monkeypatch, tmp_path):
    module = _load_action()
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_RUNTIME_DIR", tmp_path / "runtime")
    results = {
        "success": False,
        "batched": True,
        "selected_cases": 1,
        "eligible_cases": 1,
        "cases": [],
        "sync_status": {},
    }
    report_path = module._write_transcript_sync_report(
        results,
        {"failed_cases_count": 1},
        "failure without public tracking code",
    )
    receipt = module._transcript_report_receipt(report_path)

    assert receipt["persisted"] is True
    assert len(receipt["sha256"]) == 64
    assert Path(receipt["path"]).is_file()


def test_retry_pending_query_counts_toward_full_cycle_coverage(monkeypatch, tmp_path):
    retryable = "unverified_no_pdf_links: no verifiable PDF or empty state"
    result = _run(monkeypatch, tmp_path, ["", "", retryable])

    assert result["success"] is True
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["last_cycle_scanned_cases"] == 3
    assert state["cycle_scanned_cases"] == 3
    assert state["last_cycle_completed_at"]
    retry_rows = [
        row for row in state["cases"].values()
        if row.get("last_status") == "search_failed"
    ]
    assert len(retry_rows) == 1
    assert "last_success_at" not in retry_rows[0]
    assert retry_rows[0]["last_error"].startswith("unverified_no_pdf_links:")


def test_hard_failure_is_never_downgraded_to_retry_pending(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path, ["", "", "browser crashed"])

    assert result["success"] is False
    assert "failed for 1 case" in result["error"]


def test_broken_pipe_is_retained_for_retry_instead_of_false_module_failure(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path, ([""] * 26) + (["[Errno 32] Broken pipe"] * 4))

    assert result["success"] is True
    assert result["partial"] is True
    assert result["status"] == "partial_retry_pending"
    assert result["retry_pending_count"] == 4
    assert "error" not in result


def test_navigation_session_failures_are_retry_pending_and_open_circuit(monkeypatch, tmp_path):
    result = _run(
        monkeypatch,
        tmp_path,
        ([""] * 2) + (["search_navigate_failed: portal session expired"] * 8),
    )

    assert result["success"] is True
    assert result["status"] == "partial_retry_pending"
    assert result["retry_pending_count"] == 3
    assert result["upstream_deferred"] is True
    assert result["upstream_reason"] == "transcript_portal_session_unavailable"
    assert result["skipped_remaining_cases"] == 5
    assert len(result["cases"]) == 5


def test_navigation_retry_is_reported_as_waiting_not_terminal_failure():
    module = _load_action()
    text, summary = module._summarize_download_results(
        {
            "cases": [
                {
                    "success": False,
                    "status": "search_failed",
                    "error": "search_navigate_failed: portal session expired",
                    "case_number": "2026-0099",
                }
            ]
        }
    )

    assert "入口結果待復核：1 案" in text
    assert summary["retry_pending_cases_count"] == 1
    assert summary["failed_cases_count"] == 0


def test_sync_defers_when_shared_court_portal_is_busy(monkeypatch, tmp_path):
    module = _load_action()
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_LOCK_PATH", tmp_path / "transcript.lock")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_STATE_PATH", tmp_path / "state.json")

    class _BusyPortalLock:
        acquired = False
        active_owner = {"pid": 4321, "owner": "file-review:scheduled-check"}

    monkeypatch.setattr(module, "acquire_lock", lambda *args, **kwargs: _BusyPortalLock())
    result = module.cmd_sync(notify=False)

    assert result["success"] is False
    assert result["ok"] is True
    assert result["retryable"] is True
    assert result["status"] == "deferred"
    assert result["deferred"] is True
    assert result["reason"] == "file_review_portal_busy"
    assert not (tmp_path / "transcript.lock").exists()
    report = json.loads(
        (tmp_path / "runtime" / "transcript_sync_latest.json").read_text(encoding="utf-8")
    )
    assert report["ok"] is True
    assert report["success"] is False
    assert report["retryable"] is True
    assert report["status"] == "deferred"
    assert report["reason"] == "file_review_portal_busy"


def test_sync_yields_bounded_priority_to_unverified_file_review_owner(monkeypatch, tmp_path):
    module = _load_action()
    state = tmp_path / "file-review.json"
    state.write_text(
        json.dumps(
            {
                "phase": "cycle_complete",
                "result": {"portal_verified": False, "portal_probe_deferred": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_FILE_REVIEW_AUTO_STATE", str(state))
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_LOCK_PATH", tmp_path / "transcript.lock")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_STATE_PATH", tmp_path / "state.json")

    result = module.cmd_sync(notify=False)

    assert result["status"] == "deferred"
    assert result["reason"] == "file_review_priority_pending"
    assert not (tmp_path / "transcript.lock").exists()


def test_verified_file_review_receipt_does_not_starve_transcript(monkeypatch, tmp_path):
    module = _load_action()
    state = tmp_path / "file-review.json"
    state.write_text(
        json.dumps(
            {
                "phase": "cycle_complete",
                "result": {"portal_verified": True, "portal_probe_deferred": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_FILE_REVIEW_AUTO_STATE", str(state))
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_LOCK_PATH", tmp_path / "transcript.lock")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_STATE_PATH", tmp_path / "state.json")

    class _BusyPortalLock:
        acquired = False
        active_owner = {"pid": 4321, "owner": "another-owner"}

    monkeypatch.setattr(module, "acquire_lock", lambda *args, **kwargs: _BusyPortalLock())
    result = module.cmd_sync(notify=False)

    assert result["reason"] == "file_review_portal_busy"


def test_deferred_report_marks_completed_rename_observation_stale(monkeypatch, tmp_path):
    module = _load_action()
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_STATE_PATH", tmp_path / "state.json")

    module._write_transcript_sync_report(
        {
            "success": True,
            "ok": True,
            "status": "success",
            "rename": {"success": True, "renamed_count": 31, "retry_pending_count": 0},
        },
        {
            "renamed_count": 31,
            "rename_metadata_pending_count": 0,
            "rename_parse_failed_count": 0,
            "rename_file_operation_failed_count": 0,
            "rename_retry_pending_count": 0,
        },
        "completed repair",
    )
    completed = json.loads((tmp_path / "runtime" / "transcript_sync_latest.json").read_text(encoding="utf-8"))

    module._write_transcript_sync_deferred_report(
        {"reason": "file_review_portal_busy", "message": "deferred"}
    )
    report = json.loads((tmp_path / "runtime" / "transcript_sync_latest.json").read_text(encoding="utf-8"))

    assert report["status"] == "deferred"
    assert report["rename_state_stale"] is True
    assert report["rename_observation_at"] == completed["created_at"]
    assert report["summary"]["renamed_count"] == 0
    assert report["summary"]["rename_metadata_pending_count"] == 0
    assert report["summary"]["rename_retry_pending_count"] == 0


def test_deferred_report_never_relabels_prior_rename_pending_as_new(monkeypatch, tmp_path):
    module = _load_action()
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_STATE_PATH", tmp_path / "state.json")

    module._write_transcript_sync_report(
        {"success": True, "ok": True, "status": "partial_retry_pending"},
        {"rename_metadata_pending_count": 31, "rename_retry_pending_count": 31},
        "prior observation",
    )

    module._write_transcript_sync_deferred_report({"reason": "file_review_portal_busy"})
    report = json.loads((tmp_path / "runtime" / "transcript_sync_latest.json").read_text(encoding="utf-8"))

    assert report["rename_state_stale"] is True
    assert report["summary"]["rename_metadata_pending_count"] == 0
    assert report["summary"]["rename_retry_pending_count"] == 0


def test_shared_court_portal_lock_error_defers_and_releases_transcript_lock(monkeypatch, tmp_path):
    module = _load_action()
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_LOCK_PATH", tmp_path / "transcript.lock")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(module, "TRANSCRIPT_SYNC_STATE_PATH", tmp_path / "state.json")

    def _raise_lock_error(*_args, **_kwargs):
        raise OSError("lock metadata temporarily unavailable")

    monkeypatch.setattr(module, "acquire_lock", _raise_lock_error)
    result = module.cmd_sync(notify=False)

    assert result["success"] is False
    assert result["ok"] is True
    assert result["retryable"] is True
    assert result["status"] == "deferred"
    assert result["reason"] == "court_portal_lock_unavailable"
    assert not (tmp_path / "transcript.lock").exists()
    report = json.loads(
        (tmp_path / "runtime" / "transcript_sync_latest.json").read_text(encoding="utf-8")
    )
    assert report["ok"] is True
    assert report["success"] is False
    assert report["retryable"] is True
    assert report["status"] == "deferred"
    assert report["reason"] == "court_portal_lock_unavailable"


def test_venv_reexec_uses_resolved_executable_identity_for_symlink(monkeypatch):
    module = _load_action()
    target = "/fixture/venv/bin/python3"
    resolved = "/central/runtime/bin/python3.14"
    calls = []
    monkeypatch.setattr(module, "VENV_PY", target)
    monkeypatch.setattr(module.sys, "executable", resolved)
    monkeypatch.delenv("MAGI_TRANSCRIPT_NO_VENV", raising=False)
    monkeypatch.setattr(module.os.path, "exists", lambda path: path == target)
    monkeypatch.setattr(
        module.os.path,
        "realpath",
        lambda path: resolved if str(path) in {target, resolved} else str(path),
    )
    monkeypatch.setattr(module.os, "execv", lambda *args: calls.append(args))

    module._maybe_reexec_venv()

    assert calls == []


def test_summary_separates_retry_pending_from_real_failures():
    module = _load_action()
    text, summary = module._summarize_download_results(
        {
            "cases": [
                {"success": True, "files": [], "case_number": "2026-0001"},
                {
                    "success": False,
                    "status": "search_failed",
                    "error": "unverified_no_pdf_links: no verifiable PDF or empty state",
                    "case_number": "2026-0002",
                },
            ]
        }
    )

    assert "入口結果待復核：1 案" in text
    assert summary["retry_pending_cases_count"] == 1
    assert summary["failed_cases_count"] == 0


def test_deferred_cli_contract_uses_tempfail_for_durable_retry(capsys):
    module = _load_action()

    rc = module._ok(
        {
            "success": False,
            "ok": True,
            "status": "deferred",
            "deferred": True,
            "retryable": True,
            "reason": "file_review_portal_busy",
        }
    )

    assert rc == 75
    printed = json.loads(capsys.readouterr().out)
    assert printed["success"] is False
    assert printed["ok"] is True


def test_partial_retry_cli_contract_uses_scheduled_continuation(capsys):
    module = _load_action()

    rc = module._ok(
        {
            "success": True,
            "status": "partial_retry_pending",
            "partial": True,
            "retryable": True,
            "retry_pending_count": 1,
        }
    )

    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "partial_retry_pending"
    assert printed["retry_pending_count"] == 1


def test_rename_quality_is_merged_into_durable_partial_retry():
    module = _load_action()
    results = {"success": True, "status": "success", "retry_pending_count": 2}
    summary = {}

    module._merge_transcript_rename_quality(
        results,
        summary,
        {
            "ok": True,
            "success": True,
            "status": "partial_retry_pending",
            "retryable": True,
            "renamed_count": 3,
            "parse_failed_count": 1,
            "metadata_pending_count": 2,
            "file_operation_failed_count": 0,
            "retry_pending_count": 3,
            "timed_out": False,
            "failure_receipts": [
                {"file_token": "a" * 16, "category": "pdf_unreadable"}
            ],
        },
    )

    assert results["success"] is True
    assert results["status"] == "partial_retry_pending"
    assert results["retry_pending_count"] == 5
    assert results["rename_retry_pending_count"] == 3
    assert summary["renamed_count"] == 3
    assert summary["rename_parse_failed_count"] == 1
    assert summary["rename_metadata_pending_count"] == 2


def test_missing_rename_receipt_fails_closed_instead_of_false_success():
    module = _load_action()
    results = {"success": True}
    summary = {}

    module._merge_transcript_rename_quality(results, summary, None)

    assert results["success"] is False
    assert results["status"] == "failed"
    assert "品質檢查未完成" in results["error"]


def test_rename_inventory_exception_is_private_fail_closed_and_retryable():
    module = _load_action()
    results = {"success": True, "retry_pending_count": 2}
    summary = {}

    module._merge_transcript_rename_quality(
        results,
        summary,
        {
            "ok": False,
            "success": False,
            "status": "failed",
            "reason": "rename_inventory_failed",
            "exception_category": "rename_inventory_exception",
            "retryable": True,
            "retry_pending_count": 1,
            "raw_error": "/private/case/secret.pdf",
        },
    )

    assert results["success"] is False
    assert results["status"] == "failed"
    assert results["retryable"] is True
    assert results["rename_retry_pending_count"] == 1
    assert results["retry_pending_count"] == 3
    assert "/private/" not in results["error"]


def test_portal_failure_message_never_exposes_raw_detail_or_path():
    module = _load_action()
    downloader = SimpleNamespace(
        last_login_error_code="search_page_unavailable",
        last_login_error_detail="trace=secret /Users/private/case.pdf",
        _last_download_error="popup_nested_frame_timeout",
    )

    code, message, reason = module._portal_failure_from_downloader(downloader)

    assert code == "search_page_unavailable"
    assert reason == "search_page_unavailable"
    assert "自動重試" in message
    assert "secret" not in message
    assert "/Users/" not in message
    assert "popup_nested_frame_timeout" not in message


def test_sync_public_result_preserves_partial_retry_for_cli_contract(capsys):
    module = _load_action()
    result = module._transcript_sync_public_result(
        {
            "success": True,
            "status": "partial_retry_pending",
            "partial": True,
            "retryable": True,
            "retry_pending_count": 9,
        },
        message="batch complete with retry work",
        report_path="/private/report.json",
        notify_topic="quiet_cron",
        notified=False,
        notify_suppressed_reason="no_new_transcripts",
    )

    assert result["status"] == "partial_retry_pending"
    assert result["retryable"] is True
    assert result["retry_pending_count"] == 9
    assert module._ok(result) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "partial_retry_pending"


def test_partial_retry_cli_contract_allows_explicit_immediate_retry(capsys):
    module = _load_action()

    rc = module._ok(
        {
            "success": True,
            "status": "partial_retry_pending",
            "retryable": True,
            "immediate_retry_required": True,
        }
    )

    assert rc == 75
