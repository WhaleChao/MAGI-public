from __future__ import annotations

import json
import os
from types import SimpleNamespace
from datetime import date, datetime, timedelta, timezone

from scripts.ops import osc_events_refresh


def test_active_pdf_todos_filters_past_and_implausible_dates():
    active, past_skipped, implausible_skipped = osc_events_refresh._active_pdf_todos(
        [
            {"type": "補正", "date": "2026-05-26", "description": "old"},
            {"type": "開庭", "date": "2026-05-27", "description": "today"},
            {"type": "調解", "date": "2028-05-25", "description": "edge"},
            {"type": "異議", "date": "2028-05-27", "description": "too far"},
            {"type": "待確認", "date": "", "description": "missing"},
        ],
        today=date(2026, 5, 27),
    )

    assert [x["description"] for x in active] == ["today", "edge"]
    assert past_skipped == 1
    assert implausible_skipped == 2


def test_pdf_calendar_scan_writes_only_active_todos(monkeypatch, tmp_path):
    from api.blueprints import osc_pdf

    pdf = tmp_path / "notice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    today = date.today()
    captured: list[dict] = []

    monkeypatch.setattr(osc_events_refresh, "PDF_SCAN_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(osc_events_refresh, "PDF_SCAN_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(
        osc_pdf,
        "_iter_all_case_pdf_targets",
        lambda limit: [(pdf, "2026-0001", "測試")],
    )
    monkeypatch.setattr(
        osc_pdf,
        "_scan_pdf_for_calendar",
        lambda path, **kwargs: {
            "case_number": kwargs["case_number"],
            "client_name": kwargs["client_name"],
            "todos": [
                {"type": "補正", "date": (today - timedelta(days=1)).isoformat(), "description": "old"},
                {"type": "補正", "date": (today + timedelta(days=3)).isoformat(), "description": "future"},
            ],
            "events": [{}, {}],
        },
    )
    monkeypatch.setattr(
        osc_pdf,
        "_insert_todos_single_machine",
        lambda todos, **_kwargs: captured.extend(todos) or {"inserted": len(todos), "updated": 0, "skipped": 0},
    )

    args = SimpleNamespace(pdf_limit=1, pdf_max_pages=8, dry_run=False, force_rebuild=True)
    result = osc_events_refresh._run_pdf_calendar_scan(args)

    assert result["ok"] is True
    assert result["todo_count"] == 1
    assert result["past_todo_count"] == 1
    assert result["write_result"]["inserted"] == 1
    assert [x["description"] for x in captured] == ["future"]


def test_write_latest_serializes_datetime_nested_payload(tmp_path):
    out = tmp_path / "osc_events_refresh_latest.json"
    payload = {
        "ok": True,
        "scan": {
            "results": [
                {
                    "items": [
                        {
                            "todos": [
                                {
                                    "datetime": datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    }

    osc_events_refresh._write_latest(payload, out)

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["scan"]["results"][0]["items"][0]["todos"][0]["datetime"] == "2026-05-13T12:00:00+00:00"


def test_calendar_source_audit_only_flags_pdf_backed_business_events():
    assert osc_events_refresh._calendar_row_likely_needs_pdf_source(
        {"todo_type": "行事曆事件", "description": "賴麗卿案陳報末日"}
    )
    assert osc_events_refresh._calendar_row_likely_needs_pdf_source(
        {"todo_type": "調解", "description": "陳文明調解與審理@宜蘭地院"}
    )
    assert not osc_events_refresh._calendar_row_likely_needs_pdf_source(
        {"todo_type": "行事曆事件", "description": "謝易霖律見"}
    )
    assert not osc_events_refresh._calendar_row_likely_needs_pdf_source(
        {"todo_type": "行事曆事件", "description": "郭麗卿未結案件進度回報末日"}
    )
    assert not osc_events_refresh._calendar_row_likely_needs_pdf_source(
        {"todo_type": "行事曆事件", "description": "【法扶開辦末日】2026-0045 李秀英"}
    )


def test_calendar_gap_drive_remediation_respects_skip_drive_sync():
    out = osc_events_refresh._run_calendar_gap_drive_remediation(
        [{"case_number": "2025-0001", "description": "補正末日"}],
        args=SimpleNamespace(dry_run=False, skip_drive_sync=True),
    )

    assert out == {"ok": True, "skipped": True, "reason": "drive_sync_skipped_by_args"}


def test_refresh_pushes_osc_created_todos_to_gcal(monkeypatch, tmp_path):
    out = tmp_path / "osc_events_refresh_latest.json"
    calls = []

    class FakeOscAction:
        @staticmethod
        def task_scan_cases(payload):
            calls.append(("scan", payload))
            return {"ok": True, "inserted": 1}

        @staticmethod
        def task_gcal_import(payload):
            calls.append(("import", payload))
            return {"ok": True, "imported": 0}

        @staticmethod
        def task_gcal_sync(payload):
            calls.append(("push", payload))
            return {"ok": True, "inserted": 1, "failed": 0}

        @staticmethod
        def task_gcal_integrity_audit(payload):
            calls.append(("audit", payload))
            return {"ok": True, "summary": {"missing_google_id": 0}}

    class FakeTranscriptTodo:
        @staticmethod
        def _iter_pdf_targets(raw_path, *, limit):
            calls.append(("transcript_targets", {"raw_path": raw_path, "limit": limit}))
            return ["transcript-a.pdf"]

        @staticmethod
        def scan_targets(paths, *, tail_pages):
            calls.append(("transcript_scan", {"paths": paths, "tail_pages": tail_pages}))
            return {
                "ok": True,
                "scanned": 1,
                "high_count": 1,
                "review_count": 0,
                "errors_count": 0,
                "items": [{"confidence": "high", "type": "追蹤"}],
                "errors": [],
            }

        @staticmethod
        def apply_high_confidence(items):
            calls.append(("transcript_apply", {"items": items}))
            return {"inserted": 1, "updated": 0, "skipped": 0, "past_skipped": 0}

    monkeypatch.setattr(osc_events_refresh, "_load_osc_action_module", lambda: FakeOscAction)
    monkeypatch.setattr(osc_events_refresh, "_load_transcript_todo_module", lambda: FakeTranscriptTodo)
    monkeypatch.setattr(
        osc_events_refresh,
        "_run_pdf_calendar_scan",
        lambda args: (
            calls.append(("pdf_scan", {"limit": args.pdf_limit, "max_pages": args.pdf_max_pages}))
            or {"ok": True, "scanned": 1, "write_result": {"inserted": 1, "updated": 0, "skipped": 0}}
        ),
    )
    monkeypatch.setattr(
        osc_events_refresh,
        "_run_drive_case_sync_before_pdf",
        lambda args: calls.append(("drive_sync", {})) or {"ok": True, "status": "ok"},
    )
    monkeypatch.setattr(
        osc_events_refresh,
        "_run_calendar_source_audit",
        lambda args: {"ok": True, "calendar_import_only_count": 0, "sample_items": []},
    )
    monkeypatch.delenv("MAGI_GCAL_DEDUP_DRY_RUN", raising=False)

    args = SimpleNamespace(
        calendar_only=False,
        scan_only=False,
        max_cases=5,
        max_files_per_case=10,
        scan_time_budget_sec=30,
        force_rebuild=False,
        lookback_days=30,
        lookahead_days=180,
        calendar_limit=25,
        gcal_push_limit=7,
        pdf_limit=11,
        pdf_max_pages=8,
        skip_pdf_todos=False,
        transcript_limit=9,
        transcript_tail_pages=3,
        skip_transcript_todos=False,
        skip_calendar_audit=False,
        skip_drive_sync=False,
        json_out=str(out),
        dry_run=False,
    )

    result = osc_events_refresh.run_refresh(args)

    assert result["ok"] is True
    assert [name for name, _ in calls] == [
        "drive_sync",
        "pdf_scan",
        "import",
        "push",
        "audit",
        "transcript_targets",
        "transcript_scan",
        "transcript_apply",
        "push",
    ]
    assert result["scan"] == {
        "ok": True,
        "skipped": True,
        "reason": "legacy_scan_disabled; pdf_calendar_scan is the unified bounded todo scanner",
    }
    assert result["pdf_calendar_scan"]["write_result"]["inserted"] == 1
    assert calls[3][1]["limit"] == 7
    assert calls[-1][1]["limit"] == 20
    assert result["transcript_todos"]["write_result"]["inserted"] == 1
    assert result["calendar_push"]["inserted"] == 1
    assert result["calendar_push_after_transcript"]["inserted"] == 1
    assert result["calendar_audit"]["ok"] is True
    assert result["calendar_source_audit"]["calendar_import_only_count"] == 0
    assert os.environ["MAGI_GCAL_DEDUP_DRY_RUN"] == "0"


def test_refresh_can_run_legacy_scan_only_when_explicitly_enabled(monkeypatch, tmp_path):
    out = tmp_path / "osc_events_refresh_latest.json"
    calls = []

    class FakeOscAction:
        @staticmethod
        def task_scan_cases(payload):
            calls.append(("scan", payload))
            return {"ok": True, "inserted": 1}

        @staticmethod
        def task_gcal_import(payload):
            calls.append(("import", payload))
            return {"ok": True, "imported": 0}

        @staticmethod
        def task_gcal_sync(payload):
            calls.append(("push", payload))
            return {"ok": True, "inserted": 0, "failed": 0}

        @staticmethod
        def task_gcal_integrity_audit(payload):
            calls.append(("audit", payload))
            return {"ok": True, "summary": {"missing_google_id": 0}}

    monkeypatch.setattr(osc_events_refresh, "_load_osc_action_module", lambda: FakeOscAction)
    monkeypatch.setattr(
        osc_events_refresh,
        "_run_pdf_calendar_scan",
        lambda args: calls.append(("pdf_scan", {"limit": args.pdf_limit})) or {"ok": True, "scanned": 0},
    )
    monkeypatch.setattr(
        osc_events_refresh,
        "_run_drive_case_sync_before_pdf",
        lambda args: calls.append(("drive_sync", {})) or {"ok": True, "status": "ok"},
    )

    args = SimpleNamespace(
        calendar_only=False,
        scan_only=True,
        legacy_scan=True,
        max_cases=5,
        max_files_per_case=10,
        scan_time_budget_sec=30,
        force_rebuild=False,
        lookback_days=30,
        lookahead_days=180,
        calendar_limit=25,
        gcal_push_limit=7,
        pdf_limit=11,
        pdf_max_pages=8,
        skip_pdf_todos=False,
        transcript_limit=9,
        transcript_tail_pages=3,
        skip_transcript_todos=True,
        skip_calendar_audit=False,
        skip_drive_sync=False,
        json_out=str(out),
        dry_run=False,
    )

    result = osc_events_refresh.run_refresh(args)

    assert result["ok"] is True
    assert [name for name, _ in calls] == ["scan", "drive_sync", "pdf_scan"]
    assert result["scan"]["inserted"] == 1


def test_refresh_skips_transcript_dry_run_when_remaining_budget_too_small(monkeypatch, tmp_path):
    out = tmp_path / "osc_events_refresh_latest.json"

    class FakeOscAction:
        pass

    class FakeTranscriptTodo:
        @staticmethod
        def _iter_pdf_targets(raw_path, *, limit):
            raise AssertionError("dry-run with insufficient remaining budget should not start transcript scan")

    monkeypatch.setattr(osc_events_refresh, "_load_osc_action_module", lambda: FakeOscAction)
    monkeypatch.setattr(osc_events_refresh, "_load_transcript_todo_module", lambda: FakeTranscriptTodo)
    monkeypatch.setattr(
        osc_events_refresh,
        "_run_pdf_calendar_scan",
        lambda args: {"ok": True, "scanned": 0, "write_result": {"inserted": 0, "updated": 0, "skipped": 0}},
    )

    args = SimpleNamespace(
        calendar_only=False,
        scan_only=True,
        legacy_scan=False,
        max_cases=5,
        max_files_per_case=10,
        scan_time_budget_sec=30,
        force_rebuild=False,
        lookback_days=30,
        lookahead_days=180,
        calendar_limit=25,
        gcal_push_limit=7,
        pdf_limit=11,
        pdf_max_pages=8,
        skip_pdf_todos=False,
        transcript_limit=9,
        transcript_tail_pages=3,
        skip_transcript_todos=False,
        skip_calendar_audit=False,
        json_out=str(out),
        dry_run=True,
    )

    result = osc_events_refresh.run_refresh(args)

    assert result["ok"] is True
    assert "transcript_todo_timeout" not in result["warnings"]
    assert result["transcript_todos"]["ok"] is True
    assert result["transcript_todos"]["skipped"] is True
    assert result["transcript_todos"]["reason"] == "transcript_todo_dry_run_budget_too_small"


def test_refresh_marks_transcript_timeout_as_warning_when_scan_had_budget(monkeypatch, tmp_path):
    out = tmp_path / "osc_events_refresh_latest.json"

    class FakeOscAction:
        pass

    class FakeTranscriptTodo:
        @staticmethod
        def _iter_pdf_targets(raw_path, *, limit):
            raise osc_events_refresh._PdfScanTimeout("transcript_todo_timeout:1s")

    monkeypatch.setattr(osc_events_refresh, "_load_osc_action_module", lambda: FakeOscAction)
    monkeypatch.setattr(osc_events_refresh, "_load_transcript_todo_module", lambda: FakeTranscriptTodo)
    monkeypatch.setattr(
        osc_events_refresh,
        "_run_pdf_calendar_scan",
        lambda args: {"ok": True, "scanned": 0, "write_result": {"inserted": 0, "updated": 0, "skipped": 0}},
    )

    args = SimpleNamespace(
        calendar_only=False,
        scan_only=True,
        legacy_scan=False,
        max_cases=5,
        max_files_per_case=10,
        scan_time_budget_sec=300,
        force_rebuild=False,
        lookback_days=30,
        lookahead_days=180,
        calendar_limit=25,
        gcal_push_limit=7,
        pdf_limit=11,
        pdf_max_pages=8,
        skip_pdf_todos=False,
        transcript_limit=9,
        transcript_tail_pages=3,
        skip_transcript_todos=False,
        skip_calendar_audit=False,
        json_out=str(out),
        dry_run=True,
    )

    result = osc_events_refresh.run_refresh(args)

    assert result["ok"] is True
    assert "transcript_todo_timeout" in result["warnings"]
    assert result["transcript_todos"]["ok"] is False
    assert result["transcript_todos"]["skipped"] is True


def test_pdf_calendar_scan_uses_filename_first_in_bulk(monkeypatch):
    from api.blueprints import osc_pdf

    calls = []

    monkeypatch.setattr(
        osc_pdf,
        "_iter_all_case_pdf_targets",
        lambda limit: [(SimpleNamespace(name="notice.pdf"), "2026-0001", "王小明")],
    )
    monkeypatch.setattr(
        osc_pdf,
        "_scan_pdf_for_calendar",
        lambda path, **kwargs: calls.append(kwargs)
        or {
            "case_number": kwargs["case_number"],
            "client_name": kwargs["client_name"],
            "todos": [{"type": "開庭", "date": "2026-06-01", "time": "10:00", "description": "測試"}],
            "events": [{}],
        },
    )
    monkeypatch.setattr(
        osc_pdf,
        "_insert_todos_single_machine",
        lambda *_args, **_kwargs: {"inserted": 1, "updated": 0, "skipped": 0},
    )
    monkeypatch.delenv("OSC_PDF_CALENDAR_BULK_TEXT_ENABLE", raising=False)
    monkeypatch.delenv("OSC_PDF_CALENDAR_BULK_TEXT_WHEN_FILENAME", raising=False)
    args = SimpleNamespace(pdf_limit=1, pdf_max_pages=8, dry_run=False)

    result = osc_events_refresh._run_pdf_calendar_scan(args)

    assert result["ok"] is True
    assert calls[0]["scan_text"] is True
    assert calls[0]["text_when_filename"] is False
    assert calls[0]["include_share_link"] is True


def test_pdf_calendar_scan_rescans_when_rule_version_changes(monkeypatch, tmp_path):
    from api.blueprints import osc_pdf

    pdf = tmp_path / "notice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    calls = []
    old_cache = {
        "version": 1,
        "files": {
            str(pdf): {
                "mtime": int(pdf.stat().st_mtime),
                "size": int(pdf.stat().st_size),
                "todo_count": 0,
                "rule_version": "old-rule",
                "scanned_at": "2026-05-26T00:00:00+00:00",
            }
        },
    }
    cache_path = tmp_path / "pdf_calendar_scan_cache.json"
    cache_path.write_text(json.dumps(old_cache), encoding="utf-8")

    monkeypatch.setattr(osc_events_refresh, "PDF_SCAN_CACHE_PATH", cache_path)
    monkeypatch.setattr(osc_events_refresh, "PDF_SCAN_RULE_VERSION", "new-rule")
    monkeypatch.setattr(
        osc_pdf,
        "_iter_all_case_pdf_targets",
        lambda limit: [(pdf, "2026-0001", "測試")],
    )
    monkeypatch.setattr(
        osc_pdf,
        "_scan_pdf_for_calendar",
        lambda path, **kwargs: calls.append(str(path))
        or {
            "case_number": kwargs["case_number"],
            "client_name": kwargs["client_name"],
            "todos": [],
            "events": [],
        },
    )
    args = SimpleNamespace(pdf_limit=1, pdf_max_pages=8, dry_run=True)

    result = osc_events_refresh._run_pdf_calendar_scan(args)

    assert result["scanned"] == 1
    assert calls == [str(pdf)]
    updated = json.loads(cache_path.read_text(encoding="utf-8"))
    assert updated["files"][str(pdf)]["rule_version"] == "new-rule"


def test_pdf_calendar_scan_rescans_cached_no_todo_when_text_error_exists(monkeypatch, tmp_path):
    from api.blueprints import osc_pdf

    pdf = tmp_path / "notice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    cache_path = tmp_path / "pdf_calendar_scan_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {
                    str(pdf): {
                        "mtime": int(pdf.stat().st_mtime),
                        "size": int(pdf.stat().st_size),
                        "todo_count": 0,
                        "rule_version": osc_events_refresh.PDF_SCAN_RULE_VERSION,
                        "text_error": "pdf_scan_timeout:12s",
                        "scanned_at": "2026-06-01T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    monkeypatch.setattr(osc_events_refresh, "PDF_SCAN_CACHE_PATH", cache_path)
    monkeypatch.setattr(
        osc_pdf,
        "_iter_all_case_pdf_targets",
        lambda limit: [(pdf, "2026-0001", "測試")],
    )
    monkeypatch.setattr(
        osc_pdf,
        "_scan_pdf_for_calendar",
        lambda path, **kwargs: calls.append(str(path))
        or {
            "case_number": kwargs["case_number"],
            "client_name": kwargs["client_name"],
            "todos": [{"type": "補正", "date": "2026-06-10", "time": "", "description": "補正"}],
            "events": [{}],
        },
    )
    monkeypatch.setattr(
        osc_pdf,
        "_insert_todos_single_machine",
        lambda *_args, **_kwargs: {"inserted": 1, "updated": 0, "skipped": 0},
    )
    args = SimpleNamespace(pdf_limit=1, pdf_max_pages=8, dry_run=False)

    result = osc_events_refresh._run_pdf_calendar_scan(args)

    assert result["scanned"] == 1
    assert result["cache_skipped"] == 0
    assert calls == [str(pdf)]


def test_pdf_calendar_scan_full_filename_sweep_uses_all_cases_before_bounded_text(monkeypatch, tmp_path):
    from api.blueprints import osc_pdf

    pdf = tmp_path / "notice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    target_calls = []
    scan_calls = []

    monkeypatch.setattr(osc_events_refresh, "PDF_SCAN_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(osc_events_refresh, "PDF_SCAN_CURSOR_PATH", tmp_path / "cursor.json")
    monkeypatch.setattr(osc_pdf, "_count_all_case_pdf_case_rows", lambda: 123)
    monkeypatch.setenv("OSC_PDF_CALENDAR_FILENAME_SWEEP_LIMIT", "50")

    def fake_targets(*, limit, case_offset=0, case_batch=40, filename_only=False):
        target_calls.append({"limit": limit, "case_offset": case_offset, "case_batch": case_batch, "filename_only": filename_only})
        if filename_only and case_offset == 0 and case_batch == 123:
            return [(pdf, "2026-0001", "測試")]
        return []

    monkeypatch.setattr(osc_pdf, "_iter_all_case_pdf_targets", fake_targets)
    monkeypatch.setattr(
        osc_pdf,
        "_scan_pdf_for_calendar",
        lambda path, **kwargs: scan_calls.append(kwargs)
        or {
            "case_number": kwargs["case_number"],
            "client_name": kwargs["client_name"],
            "todos": [{"type": "補正", "date": "2026-06-10", "time": "", "description": "補正"}],
            "events": [{}],
        },
    )
    monkeypatch.setattr(
        osc_pdf,
        "_insert_todos_single_machine",
        lambda *_args, **_kwargs: {"inserted": 1, "updated": 0, "skipped": 0},
    )

    args = SimpleNamespace(pdf_limit=1, pdf_max_pages=8, dry_run=False)
    result = osc_events_refresh._run_pdf_calendar_scan(args)

    assert target_calls[0] == {"limit": 50, "case_offset": 0, "case_batch": 123, "filename_only": True}
    assert result["filename_sweep_targets"] == 1
    assert result["filename_sweep_scanned"] == 1
    assert scan_calls[0]["scan_text"] is False
