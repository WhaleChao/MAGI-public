# -*- coding: utf-8 -*-
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GCAL_SYNC_PATH = ROOT / "skills" / "osc-orchestrator" / "gcal_sync.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_gcal_sync_module():
    spec = importlib.util.spec_from_file_location("osc_gcal_sync_import_test", GCAL_SYNC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class _FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeCalendarListApi:
    def list(self, **kwargs):
        return _FakeRequest(
            {
                "items": [
                    {"id": "primary", "summary": "主日曆"},
                    {"id": "whalelawyer@gmail.com", "summary": "WHALELAWYER"},
                ]
            }
        )


class _FakeEventsApi:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        calendar_id = kwargs.get("calendarId")
        if calendar_id == "whalelawyer@gmail.com":
            return _FakeRequest(
                {
                    "items": [
                        {
                            "id": "whale-event-1",
                            "summary": "法扶 2026-0001 開庭",
                            "description": "WHALELAWYER shared calendar",
                            "start": {"dateTime": "2026-05-20T10:00:00+08:00"},
                        }
                    ]
                }
            )
        return _FakeRequest({"items": []})


class _FakeService:
    def __init__(self):
        self.events_api = _FakeEventsApi()
        self.calendar_list_api = _FakeCalendarListApi()

    def events(self):
        return self.events_api

    def calendarList(self):
        return self.calendar_list_api


def test_import_gcal_events_reads_all_visible_calendars(monkeypatch):
    module = _load_gcal_sync_module()
    writes = []

    def fake_osc_exec(sql, params=(), fetch="all"):
        if "SELECT value FROM settings" in sql:
            return None, []
        if "SELECT google_calendar_id" in sql:
            return [], []
        if "INSERT INTO case_todos" in sql:
            writes.append(params)
            return {"lastrowid": len(writes)}, []
        raise AssertionError(sql)

    monkeypatch.setattr(module, "_osc_exec_sql", fake_osc_exec)

    stats = module.import_gcal_events_to_todos(_FakeService(), dry_run=False)

    assert stats["imported"] == 1
    assert "whalelawyer@gmail.com" in stats["import_calendars"]
    assert writes[0][2] == "開庭"
    assert writes[0][6] == "gcal_import:whalelawyer@gmail.com"
    assert writes[0][7] == "whale-event-1"


def test_run_sync_accepts_dict_rows_from_osc_exec(monkeypatch):
    module = _load_gcal_sync_module()

    monkeypatch.setattr(module, "_load_creds", lambda: type("Creds", (), {"valid": True})())
    monkeypatch.setattr(module, "_build_service", lambda creds: _FakeService())
    monkeypatch.setattr(module, "import_gcal_events_to_todos", lambda service, dry_run=False: {"imported": 0, "import_errors": []})

    def fake_osc_exec(sql, params=(), fetch="all"):
        if "SELECT value FROM settings" in sql:
            return {"value": "primary"}, []
        if "FROM case_todos" in sql:
            return [
                {
                    "id": 99,
                    "case_number": "2026-0001",
                    "client_name": "測試",
                    "description": "開庭",
                    "todo_date": "2026-05-20",
                    "google_calendar_id": "",
                }
            ], []
        return [], []

    monkeypatch.setattr(module, "_osc_exec_sql", fake_osc_exec)

    stats = module.run_sync(dry_run=True)

    assert stats["pushed"] == 1
    assert stats["errors"] == []
