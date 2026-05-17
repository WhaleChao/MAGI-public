# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "osc-orchestrator"))

from osc_headless.db import insert_case_todos


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
