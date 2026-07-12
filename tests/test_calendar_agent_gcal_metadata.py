from datetime import datetime
import importlib.util
from pathlib import Path

from api.domains.calendar_metadata import decode_calendar_source, encode_calendar_source


ROOT = Path(__file__).resolve().parents[1]


def _load_gcal_sync():
    path = ROOT / "skills" / "osc-orchestrator" / "gcal_sync.py"
    spec = importlib.util.spec_from_file_location("test_calendar_agent_gcal_sync", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_source_metadata_round_trip_contains_only_calendar_shape():
    source = encode_calendar_source({
        "end": "2026-07-07T17:30:00",
        "rrule": "RRULE:FREQ=WEEKLY;BYDAY=TU",
        "all_day": False,
        "private_note": "must not be encoded",
    })

    assert decode_calendar_source(source) == {
        "end": "2026-07-07T17:30:00",
        "rrule": "RRULE:FREQ=WEEKLY;BYDAY=TU",
        "all_day": False,
    }
    assert "private_note" not in source


def test_gcal_builder_preserves_end_time_and_recurrence_without_exposing_metadata():
    module = _load_gcal_sync()
    source = encode_calendar_source({
        "end": "2026-07-07T17:30:00",
        "rrule": "RRULE:FREQ=WEEKLY;BYDAY=TU",
        "all_day": False,
    })

    body = module._make_todo_event({
        "id": 7,
        "case_number": "非案件行程",
        "todo_type": "行事曆事件",
        "todo_date": "2026-07-07",
        "todo_time": "15:00:00",
        "description": "客戶會議",
        "source_file": source,
    })

    assert body["summary"] == "客戶會議"
    assert body["end"]["dateTime"] == datetime(2026, 7, 7, 17, 30).isoformat()
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU"]
    assert source not in body["description"]
