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
                    {"id": "team-calendar@example.com", "summary": "TEAM_CALENDAR"},
                ]
            }
        )


class _FakeEventsApi:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        calendar_id = kwargs.get("calendarId")
        if calendar_id == "team-calendar@example.com":
            return _FakeRequest(
                {
                    "items": [
                        {
                            "id": "whale-event-1",
                            "summary": "[2026-0001] 法扶開庭",
                            "description": "team shared calendar",
                            "start": {"dateTime": "2026-05-20T10:00:00+08:00"},
                        }
                    ]
                }
            )
        return _FakeRequest(
            {
                "items": [
                    {
                        "id": "manual-event-1",
                        "summary": "王心怡閱卷",
                        "description": "同事手動登錄",
                        "start": {"date": "2026-05-21"},
                    },
                    {
                        "id": "manual-event-2",
                        "summary": "法扶 2026-0001 開庭",
                        "description": "OSC 編號不在前面，視為手動事件",
                        "start": {"date": "2026-05-22"},
                    },
                ]
            }
        )


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
        if "SELECT case_number, client_name FROM cases WHERE case_number=%s" in sql:
            return {"case_number": params[0], "client_name": "測試"}, []
        if "INSERT INTO case_todos" in sql:
            writes.append(params)
            return {"lastrowid": len(writes)}, []
        raise AssertionError(sql)

    monkeypatch.setattr(module, "_osc_exec_sql", fake_osc_exec)

    stats = module.import_gcal_events_to_todos(_FakeService(), dry_run=False)

    assert stats["imported"] == 1
    assert stats["import_skipped"] == 2
    assert "team-calendar@example.com" in stats["import_calendars"]
    assert writes[0][2] == "開庭"
    assert writes[0][6] == "gcal_import:team-calendar@example.com"
    assert writes[0][7] == "whale-event-1"
    assert writes[0][0] == "2026-0001"
    assert writes[0][1] == "測試"


def test_import_gcal_events_dry_run_counts_only_osc_owned_events(monkeypatch):
    module = _load_gcal_sync_module()

    def fake_osc_exec(sql, params=(), fetch="all"):
        if "SELECT value FROM settings" in sql:
            return None, []
        if "SELECT google_calendar_id" in sql:
            return [], []
        if "SELECT case_number, client_name FROM cases WHERE case_number=%s" in sql:
            return {"case_number": params[0], "client_name": "測試"}, []
        if "INSERT INTO case_todos" in sql:
            raise AssertionError("dry_run should not insert")
        raise AssertionError(sql)

    monkeypatch.setattr(module, "_osc_exec_sql", fake_osc_exec)

    stats = module.import_gcal_events_to_todos(_FakeService(), dry_run=True)

    assert stats["imported"] == 1
    assert stats["import_skipped"] == 2


def test_import_gcal_events_keeps_laf_reportable_manual_events(monkeypatch):
    module = _load_gcal_sync_module()
    writes = []
    module._LAF_IDENTITY_CACHE = [
        {
            "case_number": "2026-0035",
            "client_name": "陳鏈棠",
            "laf_case_no": "1150409-I-004",
            "start_date": "2026-04-09",
            "case_reason": "消債",
        }
    ]

    class OneCalendarEvents(_FakeEventsApi):
        def list(self, **kwargs):
            return _FakeRequest(
                {
                    "items": [
                        {
                            "id": "laf-manual-1",
                            "summary": "陳鏈棠來所面談",
                            "description": "法扶進度回報用",
                            "start": {"dateTime": "2026-05-20T14:00:00+08:00"},
                        },
                        {
                            "id": "nonlaf-manual-1",
                            "summary": "買影印紙",
                            "description": "行政事項",
                            "start": {"date": "2026-05-21"},
                        },
                    ]
                }
            )

    class OneCalendarService(_FakeService):
        def __init__(self):
            super().__init__()
            self.events_api = OneCalendarEvents()

    def fake_osc_exec(sql, params=(), fetch="all"):
        if "SELECT value FROM settings" in sql:
            return {"value": "primary"}, []
        if "SELECT google_calendar_id" in sql:
            return [], []
        if "INSERT INTO case_todos" in sql:
            writes.append(params)
            return {"lastrowid": len(writes)}, []
        raise AssertionError(sql)

    monkeypatch.setattr(module, "_osc_exec_sql", fake_osc_exec)

    stats = module.import_gcal_events_to_todos(OneCalendarService(), dry_run=False)

    assert stats["imported"] == 1
    assert stats["import_skipped"] == 1
    assert writes[0][0] == "2026-0035"
    assert writes[0][1] == "陳鏈棠"
    assert writes[0][5] == "陳鏈棠來所面談"


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
