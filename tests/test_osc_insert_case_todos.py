# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "osc-orchestrator"))

from osc_headless.db import insert_case_todos, list_unsynced_todos_with_case_info


class _FakeCursor:
    def __init__(self, mode):
        self.mode = mode
        self.rowcount = 0
        self.executed = []
        self._fetchone = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        normalized = " ".join(sql.split())
        self.rowcount = 0
        if "AND `description`=%s" in normalized:
            self._fetchone = None
        elif "AND `source_file`=%s AND (status IS NULL" in normalized and "`todo_type`=%s" not in normalized:
            if self.mode == "same_datetime":
                self._fetchone = (42,)
            elif self.mode == "same_datetime_needs_share_refresh":
                self._fetchone = (42, "⚖️ 3月4日 下午3時00分 開庭", "余秋菊")
            elif self.mode == "same_datetime_share_host_changed":
                self._fetchone = (
                    42,
                    "⚖️ 3月4日 下午3時00分 開庭\nMAGI分享連結：https://old-share.example/s/old\n連結有效至：2026-06-01T00:00:00",
                    "余秋菊",
                )
            else:
                self._fetchone = None
        elif "AND `todo_type`=%s" in normalized and "AND `source_file`=%s" not in normalized:
            if self.mode == "cross_source_hearing":
                if "source_file NOT LIKE 'gcal_import%%'" in normalized:
                    self._fetchone = None
                else:
                    self._fetchone = (88, "陳建華案開庭@台東地檢", "陳建華", "gcal_import:whalelawyer@gmail.com")
            elif self.mode == "cross_source_non_gcal_hearing":
                self._fetchone = (89, "⚖️ 5月27日 早上10時40分 開庭", "陳建華", "other_notice.pdf")
            else:
                self._fetchone = None
        elif "AND `todo_type`=%s AND `source_file`=%s" in normalized:
            self._fetchone = (77,) if self.mode == "stale_pending" else None
        elif normalized.startswith("UPDATE `case_todos`"):
            self.rowcount = 1
            self._fetchone = None
        elif normalized.startswith("INSERT INTO `case_todos`"):
            self.rowcount = 1
            self._fetchone = None

    def fetchone(self):
        return self._fetchone

    def close(self):
        pass


class _FakeConn:
    def __init__(self, mode):
        self.cursor_obj = _FakeCursor(mode)
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True


def _todo():
    return {
        "type": "開庭",
        "date": "2025-03-04",
        "time": "15:00",
        "description": "⚖️ 3月4日 下午3時00分 開庭",
    }


def test_insert_case_todos_skips_same_source_datetime_even_if_description_differs():
    conn = _FakeConn("same_datetime")
    result = insert_case_todos(
        conn,
        case_number="2025-0088",
        client_name="余秋菊",
        todos=[_todo()],
        source_file="notice.pdf",
    )

    assert result == {"inserted": 0, "skipped": 1, "updated": 0}
    assert not any("INSERT INTO `case_todos`" in sql for sql, _ in conn.cursor_obj.executed)


def test_insert_case_todos_refreshes_same_source_datetime_when_share_link_added():
    conn = _FakeConn("same_datetime_needs_share_refresh")
    todo = _todo()
    todo["description"] += "\nMAGI分享連結：https://share.example/s/token"
    result = insert_case_todos(
        conn,
        case_number="2025-0088",
        client_name="余秋菊",
        todos=[todo],
        source_file="notice.pdf",
    )

    assert result == {"inserted": 0, "skipped": 0, "updated": 1}
    assert any("UPDATE `case_todos`" in sql for sql, _ in conn.cursor_obj.executed)


def test_insert_case_todos_refreshes_stale_share_host():
    conn = _FakeConn("same_datetime_share_host_changed")
    todo = _todo()
    todo["description"] += "\nMAGI分享連結：https://new-share.example/s/new\n連結有效至：2026-06-16T00:00:00"
    result = insert_case_todos(
        conn,
        case_number="2025-0088",
        client_name="余秋菊",
        todos=[todo],
        source_file="notice.pdf",
    )

    assert result == {"inserted": 0, "skipped": 0, "updated": 1}
    update_params = [params for sql, params in conn.cursor_obj.executed if "UPDATE `case_todos`" in sql][0]
    assert "https://new-share.example/s/new" in update_params[1]


def test_insert_case_todos_updates_stale_pending_same_source_type():
    conn = _FakeConn("stale_pending")
    result = insert_case_todos(
        conn,
        case_number="2025-0088",
        client_name="余秋菊",
        todos=[_todo()],
        source_file="notice.pdf",
    )

    assert result == {"inserted": 0, "skipped": 0, "updated": 1}
    assert any("UPDATE `case_todos`" in sql for sql, _ in conn.cursor_obj.executed)


def test_insert_case_todos_does_not_let_imported_calendar_block_pdf_todo():
    conn = _FakeConn("cross_source_hearing")
    result = insert_case_todos(
        conn,
        case_number="2026-0038",
        client_name="陳建華",
        todos=[
            {
                "type": "開庭",
                "date": "2026-05-27",
                "time": "10:40",
                "description": "⚖️ 5月27日 早上10時40分 開庭",
            }
        ],
        source_file="20260514 臺東地方檢察署115年度偵字第9號開庭通知.pdf",
    )

    assert result == {"inserted": 1, "skipped": 0, "updated": 0}
    assert any("INSERT INTO `case_todos`" in sql for sql, _ in conn.cursor_obj.executed)


def test_insert_case_todos_still_skips_same_hearing_from_non_gcal_source():
    conn = _FakeConn("cross_source_non_gcal_hearing")
    result = insert_case_todos(
        conn,
        case_number="2026-0038",
        client_name="陳建華",
        todos=[
            {
                "type": "開庭",
                "date": "2026-05-27",
                "time": "10:40",
                "description": "⚖️ 5月27日 早上10時40分 開庭",
            }
        ],
        source_file="20260514 臺東地方檢察署115年度偵字第9號開庭通知.pdf",
    )

    assert result == {"inserted": 0, "skipped": 1, "updated": 0}
    assert not any("INSERT INTO `case_todos`" in sql for sql, _ in conn.cursor_obj.executed)


def test_list_unsynced_todos_only_returns_today_or_future_items():
    class Cursor:
        def __init__(self):
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append((sql, params))

        def fetchall(self):
            return []

        def close(self):
            pass

    class Conn:
        def __init__(self):
            self.cur = Cursor()

        def cursor(self, dictionary=False):
            assert dictionary is True
            return self.cur

    conn = Conn()
    assert list_unsynced_todos_with_case_info(conn, limit=10) == []
    sql = " ".join(conn.cur.executed[0][0].split())
    assert "ct.todo_date >= CURDATE()" in sql
    assert "ct.todo_date <= DATE_ADD(CURDATE(), INTERVAL 2 YEAR)" in sql
    assert "ct.source_file NOT LIKE 'gcal_import%%'" in sql
