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
        transcript_limit=9,
        transcript_tail_pages=3,
        skip_transcript_todos=False,
        json_out=str(out),
    )

    result = osc_events_refresh.run_refresh(args)

    assert result["ok"] is True
    assert [name for name, _ in calls] == ["scan", "transcript_targets", "transcript_scan", "transcript_apply", "import", "push"]
    assert calls[-1][1]["limit"] == 7
    assert result["transcript_todos"]["write_result"]["inserted"] == 1
    assert result["calendar_push"]["inserted"] == 1
    assert os.environ["MAGI_GCAL_DEDUP_DRY_RUN"] == "0"
