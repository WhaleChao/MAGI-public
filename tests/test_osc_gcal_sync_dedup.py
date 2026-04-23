# -*- coding: utf-8 -*-
import importlib.util
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OSC_SKILL_DIR = ROOT / "skills" / "osc-orchestrator"
ACTION_PATH = OSC_SKILL_DIR / "action.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(OSC_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(OSC_SKILL_DIR))


def _load_action_module():
    mod_name = "osc_action_test_gcal_sync_dedup"
    spec = importlib.util.spec_from_file_location(mod_name, ACTION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _FakeReq:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeEventsApi:
    def __init__(self, existing_event_id=""):
        self.existing_event_id = existing_event_id
        self.insert_calls = []
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if kwargs.get("privateExtendedProperty") and self.existing_event_id:
            return _FakeReq({"items": [{"id": self.existing_event_id, "summary": "dup", "start": {"dateTime": "2026-05-20T10:00:00+08:00"}}]})
        return _FakeReq({"items": []})

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        return _FakeReq({"id": "new-event-id"})


class _FakeService:
    def __init__(self, existing_event_id=""):
        self.events_api = _FakeEventsApi(existing_event_id=existing_event_id)

    def events(self):
        return self.events_api


class _DummyConn:
    def close(self):
        return None


def _patch_db_helpers(monkeypatch, todo_rows, set_calls):
    import osc_headless.db as dbmod  # type: ignore

    monkeypatch.setattr(dbmod, "db_config_from_env", lambda prefix="OSC_DB_": {"host": "127.0.0.1"})
    monkeypatch.setattr(dbmod, "connect_mysql", lambda cfg: _DummyConn())
    monkeypatch.setattr(dbmod, "ensure_osc_min_schema", lambda conn: None)
    monkeypatch.setattr(dbmod, "ensure_cases_schema", lambda conn: None)
    monkeypatch.setattr(dbmod, "list_unsynced_todos_with_case_info", lambda conn, limit=50: list(todo_rows))
    monkeypatch.setattr(
        dbmod,
        "set_todo_google_calendar_id",
        lambda conn, todo_id, google_calendar_id: set_calls.append((todo_id, google_calendar_id)) or {"updated": 1},
    )


def test_todo_to_gcal_event_embeds_dedup_metadata():
    mod = _load_action_module()
    body = mod._todo_to_gcal_event(
        {
            "id": 1001,
            "case_number": "2025-0081",
            "client_name": "王大明",
            "todo_type": "開庭",
            "todo_date": "2026-05-20",
            "todo_time": "10:00:00",
            "description": "開庭 2025-0081 — 花蓮地院",
        },
        tz="Asia/Taipei",
    )
    private = (((body or {}).get("extendedProperties") or {}).get("private") or {})
    assert private.get("magi_case_number") == "2025-0081"
    assert private.get("magi_todo_id") == "1001"
    assert private.get("magi_todo_type") == "開庭"
    assert private.get("magi_dedup_key")


def test_gcal_sync_dedup_dry_run_avoids_insert(monkeypatch):
    mod = _load_action_module()
    monkeypatch.setenv("MAGI_GCAL_DEDUP_ENABLED", "1")
    monkeypatch.setenv("MAGI_GCAL_DEDUP_DRY_RUN", "1")

    fake_service = _FakeService(existing_event_id="")
    monkeypatch.setattr(mod, "_build_google_calendar_service", lambda *a, **k: {"ok": True, "service": fake_service})

    set_calls = []
    _patch_db_helpers(
        monkeypatch,
        todo_rows=[
            {
                "id": 1,
                "case_number": "2025-0081",
                "client_name": "王大明",
                "todo_type": "開庭",
                "todo_date": "2026-05-20",
                "todo_time": "10:00:00",
                "description": "開庭 2025-0081 — 花蓮地院",
                "source_file": "manual_input",
                "court_case_number": "",
                "court_name": "臺灣花蓮地方法院",
            }
        ],
        set_calls=set_calls,
    )

    out = mod.task_gcal_sync({"limit": 10, "calendar_id": "primary", "time_zone": "Asia/Taipei"})
    assert out.get("ok") is True
    assert out.get("dedup_enabled") is True
    assert out.get("dedup_dry_run") is True
    assert out.get("inserted") == 0
    assert out.get("would_insert") == 1
    assert fake_service.events_api.insert_calls == []
    assert set_calls == []


def test_gcal_sync_dedup_matches_existing_and_updates_db(monkeypatch):
    mod = _load_action_module()
    monkeypatch.setenv("MAGI_GCAL_DEDUP_ENABLED", "1")
    monkeypatch.setenv("MAGI_GCAL_DEDUP_DRY_RUN", "0")

    fake_service = _FakeService(existing_event_id="existing-123")
    monkeypatch.setattr(mod, "_build_google_calendar_service", lambda *a, **k: {"ok": True, "service": fake_service})

    set_calls = []
    _patch_db_helpers(
        monkeypatch,
        todo_rows=[
            {
                "id": 2,
                "case_number": "2025-0081",
                "client_name": "王大明",
                "todo_type": "開庭",
                "todo_date": "2026-05-20",
                "todo_time": "10:00:00",
                "description": "開庭 2025-0081 — 花蓮地院",
                "source_file": "manual_input",
                "court_case_number": "",
                "court_name": "臺灣花蓮地方法院",
            }
        ],
        set_calls=set_calls,
    )

    out = mod.task_gcal_sync({"limit": 10, "calendar_id": "primary", "time_zone": "Asia/Taipei"})
    assert out.get("ok") is True
    assert out.get("inserted") == 0
    assert out.get("dedup_matched") == 1
    assert fake_service.events_api.insert_calls == []
    assert set_calls == [(2, "existing-123")]

