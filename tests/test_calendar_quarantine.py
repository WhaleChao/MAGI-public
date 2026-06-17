from __future__ import annotations

from datetime import date
import sys
import types
from types import SimpleNamespace

from scripts.ops import osc_events_refresh


def test_active_pdf_todos_marks_old_source_as_false_future_event():
    active, past_skipped, implausible_skipped, quarantined = osc_events_refresh._active_pdf_todos(
        [
            {
                "type": "開庭",
                "date": "2026-06-17",
                "description": "⚖️ 6月17日下午2時 審理",
                "source_file": r"K:\\舊案\\2025-0049\\20250517法院通知書.pdf",
            }
        ],
        today=date(2026, 6, 14),
        include_diagnostics=True,
    )

    assert active == []
    assert past_skipped == 0
    assert implausible_skipped == 1
    assert len(quarantined) == 1
    assert quarantined[0]["quarantine_reason"] == osc_events_refresh._PDF_CALENDAR_QUARANTINE_REASON


def test_active_pdf_todos_keeps_explicit_2026_notice():
    active, past_skipped, implausible_skipped, quarantined = osc_events_refresh._active_pdf_todos(
        [
            {
                "type": "開庭",
                "date": "2026-06-17",
                "description": "⚖️ 6月17日下午2時 審理",
                "source_file": r"K:\\2026-案件\\20260614 通知(法務部補正).pdf",
                "case_number": "2026-0045",
            }
        ],
        today=date(2026, 6, 14),
        include_diagnostics=True,
    )

    assert len(active) == 1
    assert past_skipped == 0
    assert implausible_skipped == 0
    assert quarantined == []


def test_run_pdf_calendar_scan_records_quarantine_and_keeps_valid_rows(monkeypatch, tmp_path):
    old_pdf = tmp_path / "old_20250517_notice.pdf"
    new_pdf = tmp_path / "new_20260614_notice.pdf"
    old_pdf.write_bytes(b"%PDF-1.4")
    new_pdf.write_bytes(b"%PDF-1.4")

    captured_todos: list[dict] = []
    monkeypatch.setattr(osc_events_refresh, "PDF_SCAN_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(osc_events_refresh, "PDF_SCAN_CURSOR_PATH", tmp_path / "cursor.json")

    fake_api = types.ModuleType("api")
    fake_blueprints = types.ModuleType("api.blueprints")
    fake_osc_pdf = types.ModuleType("api.blueprints.osc_pdf")

    fake_osc_pdf._count_all_case_pdf_case_rows = lambda: 2
    fake_osc_pdf._iter_all_case_pdf_targets = lambda *args, **kwargs: [
        (old_pdf, "2025-0049", "林洋宇"),
        (new_pdf, "2026-0045", "張明"),
    ]

    def fake_scan_pdf(path, **kwargs):
        if "old_20250517" in str(path):
            return {
                "case_number": kwargs["case_number"],
                "client_name": kwargs["client_name"],
                "todos": [
                    {
                        "type": "開庭",
                        "date": "2026-06-17",
                        "description": "⚖️ 6月17日下午2時 審理",
                    },
                ],
                "events": [{}],
            }
        return {
            "case_number": kwargs["case_number"],
            "client_name": kwargs["client_name"],
            "todos": [
                {
                    "type": "開庭",
                    "date": "2026-06-22",
                    "description": "⚖️ 6月22日下午4時 審理",
                    "source_file": str(path),
                },
            ],
            "events": [{}],
        }

    def fake_insert_todos_single_machine(todos, **_):
        captured_todos.extend(todos)
        return {"inserted": len(todos), "updated": 0, "skipped": 0}

    fake_osc_pdf._scan_pdf_for_calendar = fake_scan_pdf
    fake_osc_pdf._insert_todos_single_machine = fake_insert_todos_single_machine
    fake_blueprints.osc_pdf = fake_osc_pdf
    fake_api.blueprints = fake_blueprints

    monkeypatch.setitem(sys.modules, "api", fake_api)
    monkeypatch.setitem(sys.modules, "api.blueprints", fake_blueprints)
    monkeypatch.setitem(sys.modules, "api.blueprints.osc_pdf", fake_osc_pdf)

    result = osc_events_refresh._run_pdf_calendar_scan(SimpleNamespace(pdf_limit=10, pdf_max_pages=8, dry_run=False))

    assert result["ok"] is True
    assert result["todo_count"] == 1
    assert result["quarantine_todo_count"] == 1
    assert result["implausible_todo_count"] == 1
    assert len(captured_todos) == 1
    assert captured_todos[0]["date"] == "2026-06-22"
    assert result["quarantined_todos"][0]["source_file"].endswith("old_20250517_notice.pdf")
