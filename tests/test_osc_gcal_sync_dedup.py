# -*- coding: utf-8 -*-
import importlib.util
import os
import sys
from pathlib import Path

import pytest


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
    def __init__(self, existing_event_id="", patch_exc=None, patch_status="confirmed"):
        self.existing_event_id = existing_event_id
        self.patch_exc = patch_exc
        self.patch_status = patch_status
        self.insert_calls = []
        self.list_calls = []
        self.patch_calls = []
        self.delete_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if kwargs.get("privateExtendedProperty") and self.existing_event_id:
            return _FakeReq({"items": [{"id": self.existing_event_id, "summary": "dup", "start": {"dateTime": "2026-06-20T10:00:00+08:00"}}]})
        return _FakeReq({"items": []})

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)
        return _FakeReq({"id": "new-event-id"})

    def patch(self, **kwargs):
        self.patch_calls.append(kwargs)
        if self.patch_exc:
            raise self.patch_exc
        return _FakeReq({"id": kwargs.get("eventId") or "patched-event-id", "status": self.patch_status})

    def delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        return _FakeReq({})


class _FakeService:
    def __init__(self, existing_event_id="", patch_exc=None, patch_status="confirmed"):
        self.events_api = _FakeEventsApi(existing_event_id=existing_event_id, patch_exc=patch_exc, patch_status=patch_status)

    def events(self):
        return self.events_api


class _Resp410:
    status = 410


class _Http410(Exception):
    def __init__(self):
        super().__init__("410 Gone: syncToken expired")
        self.resp = _Resp410()


class _FakeImportEventsApi:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("syncToken"):
            raise _Http410()
        return _FakeReq({"items": [], "nextSyncToken": "fresh-token"})


class _FakeImportService:
    def __init__(self):
        self.events_api = _FakeImportEventsApi()

    def events(self):
        return self.events_api


class _DummyCursor:
    def __init__(self, repair_rows=None):
        self.repair_rows = repair_rows or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return list(self.repair_rows)

    def close(self):
        return None


class _DummyConn:
    repair_rows = []

    def __init__(self):
        self.cursors = []

    def cursor(self, dictionary=False):
        cur = _DummyCursor(self.repair_rows)
        self.cursors.append(cur)
        return cur

    def commit(self):
        return None

    def close(self):
        return None


class _MirrorCursor:
    def __init__(self, rows, inserts):
        self.rows = rows
        self.inserts = inserts
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "INSERT INTO case_todos" in sql:
            self.inserts.append(params)

    def fetchall(self):
        return list(self.rows)

    def close(self):
        return None


class _MirrorConn:
    def __init__(self, rows):
        self.rows = rows
        self.inserts = []
        self.commits = 0

    def cursor(self, dictionary=False):
        return _MirrorCursor(self.rows, self.inserts)

    def commit(self):
        self.commits += 1


class _DuplicateCleanupCursor:
    def __init__(self, rows, updates):
        self.rows = rows
        self.updates = updates
        self._selecting = False

    def execute(self, sql, params=None):
        if "SELECT id, case_number" in sql and "FROM case_todos" in sql:
            self._selecting = True
        if "UPDATE case_todos" in sql and "calendar_deduped" in sql:
            self.updates.append(params)
            self.rowcount = 1
        else:
            self.rowcount = 0

    def fetchall(self):
        return list(self.rows) if self._selecting else []

    def close(self):
        return None


class _DuplicateCleanupConn:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []
        self.commits = 0

    def cursor(self, dictionary=False):
        return _DuplicateCleanupCursor(self.rows, self.updates)

    def commit(self):
        self.commits += 1


def _patch_db_helpers(monkeypatch, todo_rows, set_calls, repair_rows=None):
    import osc_headless.db as dbmod  # type: ignore

    conn = _DummyConn()
    conn.repair_rows = list(repair_rows or [])
    monkeypatch.setattr(dbmod, "db_config_from_env", lambda prefix="OSC_DB_": {"host": "127.0.0.1"})
    monkeypatch.setattr(dbmod, "connect_mysql", lambda cfg: conn)
    monkeypatch.setattr(dbmod, "ensure_osc_min_schema", lambda conn: None)
    monkeypatch.setattr(dbmod, "ensure_cases_schema", lambda conn: None)
    monkeypatch.setattr(dbmod, "list_unsynced_todos_with_case_info", lambda conn, limit=50: list(todo_rows))
    monkeypatch.setattr(
        dbmod,
        "set_todo_google_calendar_id",
        lambda conn, todo_id, google_calendar_id: set_calls.append((todo_id, google_calendar_id)) or {"updated": 1},
    )
    return conn


def test_todo_to_gcal_event_embeds_dedup_metadata():
    mod = _load_action_module()
    body = mod._todo_to_gcal_event(
        {
            "id": 1001,
            "case_number": "2025-0081",
            "client_name": "王大明",
            "todo_type": "開庭",
            "todo_date": "2026-06-20",
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


def test_materialize_imported_calendar_mirror_rows():
    mod = _load_action_module()
    conn = _MirrorConn(
        [
            {
                "source_todo_id": 1023,
                "case_number": "2025-0084",
                "client_name": "王台銘",
                "todo_type": "行事曆事件",
                "todo_date": "2026-05-29",
                "todo_time": "10:25:00",
                "description": "王台銘案調查@北院民事庭",
                "source_file": "gcal_import:whalelawyer@gmail.com",
            }
        ]
    )

    out = mod._materialize_imported_calendar_mirrors(conn, limit=10)

    assert out["inserted"] == 1
    assert conn.commits == 1
    assert conn.inserts[0][0] == "2025-0084"
    assert conn.inserts[0][1] == "王台銘"
    assert conn.inserts[0][6] == "gcal_mirror:whalelawyer@gmail.com"


def test_cleanup_duplicate_calendar_todos_treats_import_as_dedup_candidate():
    mod = _load_action_module()
    rows = [
        {
            "id": 3834,
            "case_number": "2025-0127",
            "client_name": "曾昌義",
            "todo_type": "調解",
            "todo_date": "2026-06-01",
            "todo_time": "15:50:00",
            "description": "曾昌義案調解@花蓮地院",
            "source_file": "gcal_import:whalelawyer@gmail.com",
            "google_calendar_id": "import-event-id",
            "status": "pending",
        },
        {
            "id": 3775,
            "case_number": "2025-0127",
            "client_name": "曾昌義",
            "todo_type": "調解",
            "todo_date": "2026-06-01",
            "todo_time": "15:50:00",
            "description": "⚖️ 6月1日 下午3時50分 調解",
            "source_file": "20260512 花蓮地方法院通知書.pdf",
            "google_calendar_id": "primary-duplicate-id",
            "status": "pending",
        },
    ]
    conn = _DuplicateCleanupConn(rows)
    service = _FakeService()

    out = mod._cleanup_duplicate_calendar_todos(
        conn,
        service,
        calendar_id="primary",
        target_calendar_ids={"primary", "zl.hualien@gmail.com"},
        limit=10,
    )

    assert out["groups"] == 1
    assert out["marked"] == 1
    assert out["deleted_events"] == 1
    assert conn.updates == [(3834,)]
    assert conn.commits == 1
    assert service.events_api.delete_calls == [{"calendarId": "whalelawyer@gmail.com", "eventId": "import-event-id"}]
    assert out["items"][0]["kept_id"] == 3775


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
                "todo_date": "2026-06-20",
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
                "todo_date": "2026-06-20",
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


def test_gcal_sync_repairs_existing_calendar_event_by_patching(monkeypatch):
    mod = _load_action_module()
    monkeypatch.setenv("MAGI_GCAL_DEDUP_ENABLED", "1")
    monkeypatch.setenv("MAGI_GCAL_DEDUP_DRY_RUN", "0")

    fake_service = _FakeService(existing_event_id="")
    monkeypatch.setattr(mod, "_build_google_calendar_service", lambda *a, **k: {"ok": True, "service": fake_service})

    set_calls = []
    repair_row = {
        "id": 3,
        "case_number": "2025-0121",
        "client_name": "高弘軒",
        "todo_type": "調解",
        "todo_date": "2026-06-01",
        "todo_time": "16:00:00",
        "description": "高弘軒調解",
        "source_file": "notice.pdf",
        "google_calendar_id": "existing-google-id",
        "court_case_number": "",
        "court_name": "臺灣花蓮地方法院",
    }
    _patch_db_helpers(monkeypatch, todo_rows=[], set_calls=set_calls, repair_rows=[repair_row])

    out = mod.task_gcal_sync({"limit": 10, "calendar_id": "primary", "time_zone": "Asia/Taipei", "repair_existing": True})

    assert out.get("ok") is True
    assert out.get("repair_existing") is True
    assert out.get("patched") == 1
    assert out.get("inserted") == 0
    assert fake_service.events_api.patch_calls[0]["eventId"] == "existing-google-id"


def test_gcal_sync_repair_query_includes_calendar_deduped_rows(monkeypatch):
    mod = _load_action_module()
    monkeypatch.setenv("MAGI_GCAL_DEDUP_ENABLED", "1")
    monkeypatch.setenv("MAGI_GCAL_DEDUP_DRY_RUN", "1")

    fake_service = _FakeService(existing_event_id="")
    monkeypatch.setattr(mod, "_build_google_calendar_service", lambda *a, **k: {"ok": True, "service": fake_service})

    repair_row = {
        "id": 33,
        "case_number": "2026-0038",
        "client_name": "陳建華",
        "todo_type": "開庭",
        "todo_date": "2026-06-27",
        "todo_time": "10:40:00",
        "description": "陳建華開庭",
        "source_file": "notice.pdf",
        "google_calendar_id": "deduped-but-needs-repair",
        "court_case_number": "",
        "court_name": "臺灣臺東地方檢察署",
    }
    conn = _patch_db_helpers(monkeypatch, todo_rows=[], set_calls=[], repair_rows=[repair_row])

    out = mod.task_gcal_sync({"limit": 10, "calendar_id": "primary", "time_zone": "Asia/Taipei", "repair_existing": True})

    sql = "\n".join(sql for cur in conn.cursors for sql, _ in cur.executed)
    assert "calendar_deduped" in sql
    assert out.get("ok") is True


def test_gcal_sync_skips_implausible_far_future_todo(monkeypatch):
    mod = _load_action_module()
    monkeypatch.setenv("MAGI_GCAL_DEDUP_ENABLED", "0")

    fake_service = _FakeService(existing_event_id="")
    monkeypatch.setattr(mod, "_build_google_calendar_service", lambda *a, **k: {"ok": True, "service": fake_service})

    _patch_db_helpers(
        monkeypatch,
        todo_rows=[
            {
                "id": 99,
                "case_number": "2025-0077",
                "client_name": "蘇建和",
                "todo_type": "調查",
                "todo_date": "2317-11-08",
                "todo_time": "",
                "description": "OCR 誤讀遠未來日期",
                "source_file": "transcript.pdf",
                "court_case_number": "",
                "court_name": "臺灣高等法院",
            }
        ],
        set_calls=[],
    )

    out = mod.task_gcal_sync({"limit": 10, "calendar_id": "primary", "time_zone": "Asia/Taipei"})

    assert out.get("ok") is True
    assert out.get("skipped_implausible") == 1
    assert out.get("inserted") == 0
    assert fake_service.events_api.insert_calls == []


def test_gcal_sync_replaces_stale_existing_calendar_event(monkeypatch):
    mod = _load_action_module()
    monkeypatch.setenv("MAGI_GCAL_DEDUP_ENABLED", "0")
    errors = pytest.importorskip("googleapiclient.errors")

    class Resp:
        status = 404
        reason = "Not Found"

    fake_service = _FakeService(existing_event_id="", patch_exc=errors.HttpError(Resp(), b"{}"))
    monkeypatch.setattr(mod, "_build_google_calendar_service", lambda *a, **k: {"ok": True, "service": fake_service})

    repair_row = {
        "id": 4,
        "case_number": "2026-0038",
        "client_name": "陳建華",
        "todo_type": "開庭",
        "todo_date": "2026-06-27",
        "todo_time": "10:40:00",
        "description": "陳建華開庭",
        "source_file": "notice.pdf",
        "google_calendar_id": "wrong-calendar-event-id",
        "court_case_number": "",
        "court_name": "臺灣臺東地方檢察署",
    }
    _patch_db_helpers(monkeypatch, todo_rows=[], set_calls=[], repair_rows=[repair_row])

    out = mod.task_gcal_sync({"limit": 10, "calendar_id": "primary", "time_zone": "Asia/Taipei", "repair_existing": True})

    assert out.get("ok") is True
    assert out.get("patched") == 0
    assert out.get("inserted") == 1
    assert out.get("replaced_stale") == 1
    assert fake_service.events_api.patch_calls[0]["eventId"] == "wrong-calendar-event-id"
    assert fake_service.events_api.insert_calls


def test_gcal_sync_replaces_cancelled_existing_calendar_event(monkeypatch):
    mod = _load_action_module()
    monkeypatch.setenv("MAGI_GCAL_DEDUP_ENABLED", "0")

    fake_service = _FakeService(existing_event_id="", patch_status="cancelled")
    monkeypatch.setattr(mod, "_build_google_calendar_service", lambda *a, **k: {"ok": True, "service": fake_service})

    repair_row = {
        "id": 5,
        "case_number": "2025-0124",
        "client_name": "楊志杰",
        "todo_type": "開庭",
        "todo_date": "2026-06-22",
        "todo_time": "16:30:00",
        "description": "楊志杰審理程序",
        "source_file": "notice.pdf",
        "google_calendar_id": "cancelled-google-event-id",
        "court_case_number": "",
        "court_name": "臺灣高等法院花蓮分院",
    }
    _patch_db_helpers(monkeypatch, todo_rows=[], set_calls=[], repair_rows=[repair_row])

    out = mod.task_gcal_sync({"limit": 10, "calendar_id": "primary", "time_zone": "Asia/Taipei", "repair_existing": True})

    assert out.get("ok") is True
    assert out.get("patched") == 0
    assert out.get("inserted") == 1
    assert out.get("replaced_stale") == 1
    assert fake_service.events_api.insert_calls


def test_gcal_import_incremental_410_resets_token_and_full_syncs(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    from api.platforms import runtime_dir

    mod = _load_action_module()
    state_path = runtime_dir.root() / "gcal_import_sync_tokens.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"primary":"expired-token"}', encoding="utf-8")

    fake_service = _FakeImportService()
    monkeypatch.setattr(mod, "_build_google_calendar_service", lambda *a, **k: {"ok": True, "service": fake_service})

    out = mod.task_gcal_import({"calendar_id": "primary", "incremental": True, "limit": 10})

    assert out["ok"] is True
    assert out["sync_token_resets"] == 1
    assert fake_service.events_api.calls[0]["syncToken"] == "expired-token"
    assert "syncToken" not in fake_service.events_api.calls[1]
    assert "timeMin" in fake_service.events_api.calls[1]
    assert "fresh-token" in state_path.read_text(encoding="utf-8")
