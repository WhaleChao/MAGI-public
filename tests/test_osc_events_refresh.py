from __future__ import annotations

import json
import os
from types import SimpleNamespace
from datetime import datetime, timezone

from scripts.ops import osc_events_refresh


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
        json_out=str(out),
    )

    result = osc_events_refresh.run_refresh(args)

    assert result["ok"] is True
    assert [name for name, _ in calls] == ["scan", "pdf_scan", "transcript_targets", "transcript_scan", "transcript_apply", "import", "push", "audit"]
    assert result["pdf_calendar_scan"]["write_result"]["inserted"] == 1
    assert calls[-1][1]["limit"] == 7
    assert result["transcript_todos"]["write_result"]["inserted"] == 1
    assert result["calendar_push"]["inserted"] == 1
    assert result["calendar_audit"]["ok"] is True
    assert os.environ["MAGI_GCAL_DEDUP_DRY_RUN"] == "0"


def test_pdf_calendar_scan_reads_text_by_default_in_bulk(monkeypatch):
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
    assert calls[0]["text_when_filename"] is True
