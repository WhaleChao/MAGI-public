# -*- coding: utf-8 -*-
import importlib.util
import os
import sys
from datetime import date, timedelta
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
        self.cursors = []
        self.commits = 0

    def cursor(self, dictionary=False):
        cur = _MirrorCursor(self.rows, self.inserts)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1


class _DuplicateCleanupCursor:
    def __init__(self, rows, updates, purge_updates):
        self.rows = rows
        self.updates = updates
        self.purge_updates = purge_updates
        self._selecting = False

    def execute(self, sql, params=None):
        if "SELECT id, case_number" in sql and "FROM case_todos" in sql:
            self._selecting = True
        if "UPDATE case_todos" in sql and "calendar_deduped" in sql:
            self.updates.append(params)
            self.rowcount = 1
        elif "UPDATE case_todos" in sql and "SET google_calendar_id=''" in sql:
            self.purge_updates.append(params)
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
        self.purge_updates = []
        self.commits = 0

    def cursor(self, dictionary=False):
        return _DuplicateCleanupCursor(self.rows, self.updates, self.purge_updates)

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


def test_gcal_imported_lawyer_visit_keeps_human_calendar_title():
    mod = _load_action_module()

    assert mod._classify_gcal_import_todo_type("謝易霖律見", "") == "律見"
    assert mod._classify_gcal_import_todo_type("[2025-0007] 張偉銘 - 準備程序", "") == "準備程序"

    body = mod._todo_to_gcal_event(
        {
            "id": 1002,
            "case_number": "2026-0007",
            "client_name": "謝易霖",
            "todo_type": "行事曆事件",
            "todo_date": "2026-06-26",
            "todo_time": "14:00",
            "description": "謝易霖律見",
            "source_file": "gcal_import:primary",
        },
        tz="Asia/Taipei",
    )

    assert body["summary"] == "謝易霖律見"
    assert "行事曆事件 謝易霖" not in body["summary"]


def test_gcal_mirror_calendar_event_keeps_human_calendar_title():
    mod = _load_action_module()

    body = mod._todo_to_gcal_event(
        {
            "id": 1003,
            "case_number": "2026-0020",
            "client_name": "黃宥茹",
            "todo_type": "行事曆事件",
            "todo_date": "2026-08-03",
            "todo_time": "10:00",
            "description": "黃宥茹轉銜會議",
            "source_file": "gcal_mirror:whalelawyer@gmail.com",
            "court_case_number": "115年度家護字第12號",
        },
        tz="Asia/Taipei",
    )

    assert body["summary"] == "黃宥茹轉銜會議"
    assert "行事曆事件" not in body["summary"]


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


def test_materialize_imported_calendar_mirror_does_not_skip_by_case_status_only():
    mod = _load_action_module()
    conn = _MirrorConn(
        [
            {
                "source_todo_id": 5186,
                "case_number": "2025-0007",
                "client_name": "張偉銘",
                "todo_type": "開庭",
                "todo_date": "2026-07-06",
                "todo_time": "16:10:00",
                "description": "張偉銘開庭＠花蓮地院",
                "source_file": "gcal_import:whalelawyer@gmail.com",
                "case_status": "已結案",
            }
        ]
    )

    out = mod._materialize_imported_calendar_mirrors(conn, limit=10)

    assert out["inserted"] == 1
    assert conn.inserts[0][0] == "2025-0007"


def test_materialize_imported_calendar_mirror_query_respects_manual_delete_tombstone():
    mod = _load_action_module()
    conn = _MirrorConn([])

    mod._materialize_imported_calendar_mirrors(conn, limit=10)

    sql = "\n".join(sql for cur in conn.cursors for sql, _params in cur.executed)
    assert "COALESCE(d.status, '') = 'deleted'" in sql
    assert "LIKE '[人工刪除：%%'" in sql


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


def test_cleanup_duplicate_calendar_todos_merges_response_deadline_type_drift():
    mod = _load_action_module()
    rows = [
        {
            "id": 4123,
            "case_number": "2025-0059",
            "client_name": "吳美蓮",
            "todo_type": "陳報",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "📝 10日內陳報 (05/29文到)",
            "source_file": "20260529 宜蘭地方法院函（吳美蓮；如有異議10日內提出）.pdf",
            "google_calendar_id": "keep-event-id",
            "status": "pending",
        },
        {
            "id": 4134,
            "case_number": "2025-0059",
            "client_name": "吳美蓮",
            "todo_type": "提出資料",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "提出資料 (2026-06-08 00:00) - 20260529 宜蘭地方法院函（吳美蓮；如有異議，請於收受後10日內提出）.pdf",
            "source_file": "20260529 宜蘭地方法院函（吳美蓮；如有異議10日內提出）.pdf",
            "google_calendar_id": "duplicate-event-id",
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
    assert conn.updates == [(4134,)]
    assert service.events_api.delete_calls == [{"calendarId": "primary", "eventId": "duplicate-event-id"}]


def test_cleanup_duplicate_calendar_todos_marks_db_only_duplicate_before_push():
    mod = _load_action_module()
    rows = [
        {
            "id": 4123,
            "case_number": "2025-0059",
            "client_name": "吳美蓮",
            "todo_type": "陳報",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "📝 10日內陳報 (05/29文到)",
            "source_file": "20260529 宜蘭地方法院函（吳美蓮）.pdf",
            "google_calendar_id": "keep-event-id",
            "status": "pending",
        },
        {
            "id": 4134,
            "case_number": "2025-0059",
            "client_name": "吳美蓮",
            "todo_type": "提出資料",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "提出資料 - 10日內提出異議",
            "source_file": "20260529 宜蘭地方法院函（吳美蓮）.pdf",
            "google_calendar_id": "",
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
    assert out["db_only_marked"] == 1
    assert out["deleted_events"] == 0
    assert conn.updates == [(4134,)]
    assert service.events_api.delete_calls == []


def test_cleanup_duplicate_calendar_todos_keeps_more_specific_db_only_type():
    mod = _load_action_module()
    rows = [
        {
            "id": 501,
            "case_number": "2026-0001",
            "client_name": "王小明",
            "todo_type": "提出資料",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "10日內提出資料",
            "source_file": "notice.pdf",
            "google_calendar_id": "",
            "status": "pending",
        },
        {
            "id": 502,
            "case_number": "2026-0001",
            "client_name": "王小明",
            "todo_type": "陳報",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "10日內陳報",
            "source_file": "notice.pdf",
            "google_calendar_id": "",
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
    assert out["db_only_marked"] == 1
    assert conn.updates == [(501,)]
    assert out["items"][0]["kept_id"] == 502


def test_cleanup_duplicate_calendar_todos_merges_same_judgment_short_drive_filename():
    mod = _load_action_module()
    rows = [
        {
            "id": 3977,
            "case_number": "2025-0002",
            "client_name": "游秀鈴",
            "todo_type": "上訴",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "判決送達後 20 日內上訴",
            "source_file": "20260518 臺北地方法院114年度訴字第972號刑事判決(游秀鈴；主文：共同犯傷害致人於死罪，處有期徒刑拾年).pdf",
            "google_calendar_id": "official-appeal-event",
            "status": "pending",
        },
        {
            "id": 3976,
            "case_number": "2025-0002",
            "client_name": "游秀鈴",
            "todo_type": "上訴",
            "todo_date": "2026-06-09",
            "todo_time": None,
            "description": "游秀鈴_台北地院刑事判決.pdf",
            "source_file": "游秀鈴_台北地院刑事判決.pdf",
            "google_calendar_id": "short-drive-event",
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
        lookback_days=14,
    )

    assert out["groups"] == 1
    assert out["marked"] == 1
    assert conn.updates == [(3976,)]
    assert out["items"][0]["kept_id"] == 3977
    assert service.events_api.delete_calls == [{"calendarId": "primary", "eventId": "short-drive-event"}]


def test_cleanup_duplicate_calendar_todos_merges_same_source_hearing_without_deleting_shared_gid():
    mod = _load_action_module()
    source = "20260528 臺灣臺東地方法院刑事庭通知（林建豐；訂115年6月9日早上10時40分行準備程序）.pdf"
    rows = [
        {
            "id": 4048,
            "case_number": "2026-0050",
            "client_name": "林建豐",
            "todo_type": "準備程序",
            "todo_date": "2026-06-09",
            "todo_time": "10:40:00",
            "description": "⚖️ 6月9日 早上10時40分 準備程序",
            "source_file": source,
            "google_calendar_id": "shared-event-id",
            "status": "pending",
        },
        {
            "id": 4053,
            "case_number": "2026-0050",
            "client_name": "林建豐",
            "todo_type": "準備程序",
            "todo_date": "2026-06-09",
            "todo_time": "9:00:00",
            "description": "準備程序 (2026-06-09 09:00)",
            "source_file": source,
            "google_calendar_id": "shared-event-id",
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
        lookback_days=14,
    )

    assert out["groups"] == 1
    assert out["marked"] == 1
    assert out["db_only_marked"] == 1
    assert conn.updates == [(4053,)]
    assert out["items"][0]["kept_id"] == 4048
    assert service.events_api.delete_calls == []


def test_cleanup_duplicate_calendar_todos_resolves_no_case_import_shadow_by_client():
    mod = _load_action_module()
    rows = [
        {
            "id": 3977,
            "case_number": "2025-0002",
            "client_name": "游秀鈴",
            "todo_type": "上訴",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "⚖️ 游秀鈴 114年度訴字第000972號 上訴",
            "source_file": "20260521 臺北地方法院裁定（游秀鈴；上訴期間）.pdf",
            "google_calendar_id": "official-event-id",
            "status": "pending",
        },
        {
            "id": 3849,
            "case_number": "",
            "client_name": "",
            "todo_type": "行事曆事件",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "游秀鈴案上訴末日",
            "source_file": "gcal_import:whalelawyer@gmail.com",
            "google_calendar_id": "import-shadow-id",
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
    assert conn.updates == [(3849,)]
    assert service.events_api.delete_calls == [{"calendarId": "whalelawyer@gmail.com", "eventId": "import-shadow-id"}]
    assert out["items"][0]["kept_id"] == 3977
    assert out["items"][0]["resolved_case_number"] == "2025-0002"


def test_cleanup_duplicate_calendar_todos_deletes_already_deduped_event_only_when_duplicate():
    mod = _load_action_module()
    rows = [
        {
            "id": 3775,
            "case_number": "2025-0127",
            "client_name": "曾昌義",
            "todo_type": "調解",
            "todo_date": "2026-06-01",
            "todo_time": "15:50:00",
            "description": "調解 - 花蓮地方法院通知",
            "source_file": "20260512 花蓮地方法院通知書.pdf",
            "google_calendar_id": "official-event-id",
            "status": "pending",
        },
        {
            "id": 3834,
            "case_number": "2025-0127",
            "client_name": "曾昌義",
            "todo_type": "調解",
            "todo_date": "2026-06-01",
            "todo_time": "15:50:00",
            "description": "曾昌義案調解@花蓮地院",
            "source_file": "gcal_import:whalelawyer@gmail.com",
            "google_calendar_id": "import-shadow-id",
            "status": "calendar_deduped",
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
        lookback_days=14,
    )

    assert out["groups"] == 1
    assert out["marked"] == 1
    assert out["deleted_events"] == 1
    assert conn.updates == [(3834,)]
    assert conn.purge_updates == []
    assert conn.commits == 1
    assert service.events_api.delete_calls == [{"calendarId": "whalelawyer@gmail.com", "eventId": "import-shadow-id"}]
    assert out["items"][0]["action"] == "calendar_deduped"


def test_cleanup_duplicate_calendar_todos_keeps_single_completed_or_deduped_history_event():
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
            "google_calendar_id": "history-event-id",
            "status": "calendar_deduped",
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
        lookback_days=14,
    )

    assert out["groups"] == 0
    assert out["marked"] == 0
    assert conn.updates == []
    assert service.events_api.delete_calls == []


def test_cleanup_duplicate_calendar_todos_purges_deleted_status_google_event():
    mod = _load_action_module()
    rows = [
        {
            "id": 3778,
            "case_number": "2025-0127",
            "client_name": "曾昌義",
            "todo_type": "調解",
            "todo_date": "2026-06-01",
            "todo_time": "15:50:00",
            "description": "⚖️ 6月1日 下午3時50分 調解",
            "source_file": "20260512 花蓮地方法院通知書.pdf",
            "google_calendar_id": "deleted-event-id",
            "status": "deleted",
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
        lookback_days=14,
    )

    assert out["purged_deleted_events"] == 1
    assert out["groups"] == 0
    assert out["marked"] == 0
    assert conn.purge_updates == [(3778,)]
    assert conn.updates == []
    assert conn.commits == 1
    assert service.events_api.delete_calls == [{"calendarId": "primary", "eventId": "deleted-event-id"}]
    assert out["items"][0]["action"] == "deleted_event_purged"


def test_cleanup_duplicate_calendar_todos_does_not_resolve_no_case_import_without_unique_match():
    mod = _load_action_module()
    rows = [
        {
            "id": 3977,
            "case_number": "2025-0002",
            "client_name": "游秀鈴",
            "todo_type": "上訴",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "游秀鈴上訴",
            "source_file": "notice.pdf",
            "google_calendar_id": "official-event-id",
            "status": "pending",
        },
        {
            "id": 3988,
            "case_number": "2026-0055",
            "client_name": "游秀鈴",
            "todo_type": "上訴",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "游秀鈴另案上訴",
            "source_file": "notice2.pdf",
            "google_calendar_id": "official-event-id-2",
            "status": "pending",
        },
        {
            "id": 3849,
            "case_number": "",
            "client_name": "",
            "todo_type": "行事曆事件",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "游秀鈴案上訴末日",
            "source_file": "gcal_import:whalelawyer@gmail.com",
            "google_calendar_id": "import-shadow-id",
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

    assert out["groups"] == 0
    assert out["marked"] == 0
    assert conn.updates == []
    assert service.events_api.delete_calls == []


def test_cleanup_duplicate_calendar_todos_does_not_merge_payment_and_correction_same_day():
    mod = _load_action_module()
    rows = [
        {
            "id": 501,
            "case_number": "2026-0001",
            "client_name": "王小明",
            "todo_type": "補正",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "補正委任狀",
            "source_file": "notice.pdf",
            "google_calendar_id": "correction-event-id",
            "status": "pending",
        },
        {
            "id": 502,
            "case_number": "2026-0001",
            "client_name": "王小明",
            "todo_type": "繳費",
            "todo_date": "2026-06-08",
            "todo_time": None,
            "description": "繳納裁判費",
            "source_file": "notice.pdf",
            "google_calendar_id": "payment-event-id",
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

    assert out["groups"] == 0
    assert out["marked"] == 0
    assert service.events_api.delete_calls == []


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
        "todo_date": (date.today() + timedelta(days=30)).isoformat(),
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


def test_gcal_sync_does_not_skip_by_case_status_only(monkeypatch):
    mod = _load_action_module()
    monkeypatch.setenv("MAGI_GCAL_DEDUP_ENABLED", "0")

    fake_service = _FakeService(existing_event_id="")
    monkeypatch.setattr(mod, "_build_google_calendar_service", lambda *a, **k: {"ok": True, "service": fake_service})

    _patch_db_helpers(
        monkeypatch,
        todo_rows=[
            {
                "id": 5170,
                "case_number": "2025-0007",
                "client_name": "張偉銘",
                "todo_type": "開庭",
                "todo_date": (date.today() + timedelta(days=7)).isoformat(),
                "todo_time": "16:10:00",
                "description": "張偉銘開庭＠花蓮地院",
                "source_file": "gcal_mirror:whalelawyer@gmail.com",
                "case_status": "已結案",
                "court_case_number": "114年度原訴字第24號",
                "court_name": "臺灣花蓮地方法院",
            }
        ],
        set_calls=[],
    )

    out = mod.task_gcal_sync({"limit": 10, "calendar_id": "primary", "time_zone": "Asia/Taipei"})

    assert out.get("ok") is True
    assert out.get("inserted") == 1
    assert out.get("patched") == 0
    assert fake_service.events_api.insert_calls
    assert fake_service.events_api.patch_calls == []


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


def test_gcal_import_resolves_manual_event_by_unique_client_name():
    mod = _load_action_module()

    class Cursor:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if "FROM cases" in self.sql:
                return [
                    {
                        "case_number": "2025-0064",
                        "client_name": "賴麗卿Sera Cang",
                        "case_reason": "清算",
                        "case_type": "消費者債務清理",
                        "case_category": "法律扶助案件",
                        "court_case_no": "",
                        "court_case_number": "",
                        "laf_case_no": "1131227-E-001",
                        "application_no": "",
                        "status": "進行中",
                        "start_date": "2025-01-01",
                        "approval_date": None,
                    }
                ]

        def close(self):
            return None

    class Conn:
        def cursor(self, dictionary=False):
            return Cursor()

    assert mod._resolve_gcal_event_case_identity(Conn(), "賴麗卿案陳報末日", "", "2026-06-11") == (
        "2025-0064",
        "賴麗卿Sera Cang",
    )


def test_gcal_import_resolves_manual_event_without_filtering_by_case_status():
    mod = _load_action_module()

    class Cursor:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if "FROM cases" in self.sql:
                return [
                    {
                        "case_number": "2025-0007",
                        "client_name": "張偉銘",
                        "case_reason": "傷害致死",
                        "case_type": "刑事",
                        "case_category": "法律扶助案件",
                        "court_case_no": "114年度原訴字第24號",
                        "court_case_number": "114年度原訴字第24號",
                        "laf_case_no": "",
                        "application_no": "",
                        "status": "已結案",
                        "start_date": "2025-01-01",
                        "approval_date": None,
                    }
                ]
            return []

        def close(self):
            return None

    class Conn:
        def cursor(self, dictionary=False):
            return Cursor()

    assert mod._resolve_gcal_event_case_identity(Conn(), "張偉銘閱卷", "", "2026-06-11") == (
        "2025-0007",
        "張偉銘",
    )


def test_gcal_import_prefers_non_final_case_when_name_only_event_matches_closed_case_too():
    mod = _load_action_module()

    class Cursor:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if "FROM cases" in self.sql:
                return [
                    {
                        "case_number": "2025-0007",
                        "client_name": "張偉銘",
                        "case_reason": "傷害致死",
                        "case_type": "刑事",
                        "case_category": "法律扶助案件",
                        "court_case_no": "",
                        "court_case_number": "",
                        "laf_case_no": "",
                        "application_no": "",
                        "status": "已結案",
                        "start_date": "2025-01-01",
                        "approval_date": None,
                    },
                    {
                        "case_number": "2026-0100",
                        "client_name": "張偉銘",
                        "case_reason": "詐欺",
                        "case_type": "刑事",
                        "case_category": "一般案件",
                        "court_case_no": "",
                        "court_case_number": "",
                        "laf_case_no": "",
                        "application_no": "",
                        "status": "進行中",
                        "start_date": "2026-01-01",
                        "approval_date": None,
                    },
                ]
            return []

        def close(self):
            return None

    class Conn:
        def cursor(self, dictionary=False):
            return Cursor()

    assert mod._resolve_gcal_event_case_identity(Conn(), "張偉銘閱卷", "", "2026-06-11") == (
        "2026-0100",
        "張偉銘",
    )


def test_gcal_import_leaves_ambiguous_manual_event_unassigned():
    mod = _load_action_module()

    class Cursor:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            if "FROM cases" in self.sql:
                return [
                    {
                        "case_number": "2026-0002",
                        "client_name": "測試人",
                        "case_reason": "清算",
                        "case_type": "消費者債務清理",
                        "case_category": "法律扶助案件",
                        "court_case_no": "",
                        "court_case_number": "",
                        "laf_case_no": "",
                        "application_no": "",
                        "status": "進行中",
                        "start_date": "2026-01-01",
                        "approval_date": None,
                    },
                    {
                        "case_number": "2026-0001",
                        "client_name": "測試人",
                        "case_reason": "監護",
                        "case_type": "民事",
                        "case_category": "一般案件",
                        "court_case_no": "",
                        "court_case_number": "",
                        "laf_case_no": "",
                        "application_no": "",
                        "status": "進行中",
                        "start_date": "2026-01-01",
                        "approval_date": None,
                    },
                ]

        def close(self):
            return None

    class Conn:
        def cursor(self, dictionary=False):
            return Cursor()

    assert mod._resolve_gcal_event_case_identity(Conn(), "測試人案開庭", "", "2026-06-11") == ("", "")
