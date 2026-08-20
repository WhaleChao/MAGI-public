from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def test_policy_is_narrow_and_preserves_real_deadlines():
    from api.domains.calendar_sync_policy import (
        is_osc_only_calendar_review,
        is_osc_only_overdue_confirmation,
        osc_only_calendar_review_sql,
    )

    assert is_osc_only_overdue_confirmation({"todo_type": "逾期確認"})
    assert is_osc_only_overdue_confirmation(
        {"todo_type": "抗告", "description": "【MAGI逾期治理：原待辦#6194】\n原類型：抗告"}
    )
    assert not is_osc_only_overdue_confirmation(
        {"todo_type": "抗告", "description": "10日內抗告 (03/17文到)"}
    )
    assert not is_osc_only_overdue_confirmation(
        {"todo_type": "準備程序", "description": "2026-09-08 16:30開庭"}
    )
    assert is_osc_only_calendar_review({"todo_type": "確認"})
    assert is_osc_only_calendar_review({"todo_type": "案號確認"})
    assert is_osc_only_calendar_review({"todo_type": "逾期確認"})
    assert not is_osc_only_calendar_review({"todo_type": "補正"})
    assert not is_osc_only_calendar_review({"todo_type": "上訴"})
    assert not is_osc_only_calendar_review({"todo_type": "準備程序"})
    sql = osc_only_calendar_review_sql("ct")
    assert "ct.todo_type" in sql and "確認" in sql and "案號確認" in sql


def test_both_calendar_event_builders_fail_closed_for_osc_only_review():
    legacy = _load(ROOT / "skills/osc-orchestrator/gcal_sync.py", "gcal_sync_overdue_policy_test")
    action = _load(ROOT / "skills/osc-orchestrator/action.py", "osc_action_overdue_policy_test")
    row = {
        "id": 6194,
        "case_number": "2025-0003",
        "todo_type": "抗告",
        "todo_date": "2026-08-12",
        "description": "【MAGI逾期治理：原待辦#6194】",
    }

    with pytest.raises(ValueError, match="OSC-only"):
        legacy._make_todo_event(row)
    with pytest.raises(ValueError, match="OSC-only"):
        action._todo_to_gcal_event(row, tz="Asia/Taipei")

    manual_review = {
        "id": 7001,
        "case_number": "2026-0001",
        "todo_type": "確認",
        "todo_date": "2026-08-20",
        "description": "PDF 擷取後待人工確認",
    }
    with pytest.raises(ValueError, match="OSC-only"):
        legacy._make_todo_event(manual_review)
    with pytest.raises(ValueError, match="OSC-only"):
        action._todo_to_gcal_event(manual_review, tz="Asia/Taipei")


class _DeleteCall:
    def execute(self):
        return {}


class _Events:
    def __init__(self):
        self.deleted = []

    def delete(self, **kwargs):
        self.deleted.append(kwargs)
        return _DeleteCall()


class _Service:
    def __init__(self):
        self.events_api = _Events()

    def events(self):
        return self.events_api


class _Cursor:
    def __init__(self):
        self.queries = []
        self._mode = ""
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        self.rowcount = 0
        if "SHOW COLUMNS" in sql:
            self._mode = "column"
        elif "SELECT id, todo_type" in sql:
            self._mode = "rows"
        elif "UPDATE case_todos" in sql:
            self._mode = "update"
            self.rowcount = 1
        elif "DELETE FROM calendar_events" in sql:
            self._mode = "cache"
            self.rowcount = 1

    def fetchone(self):
        return {"Field": "google_calendar_event_id"} if self._mode == "column" else None

    def fetchall(self):
        if self._mode != "rows":
            return []
        return [
            {
                "id": 6194,
                "todo_type": "抗告",
                "description": "【MAGI逾期治理：原待辦#42】",
                "source_file": "20260317 裁定.pdf",
                "google_calendar_id": "opaque-event",
            }
        ]

    def close(self):
        return None


class _Conn:
    def __init__(self):
        self.cur = _Cursor()
        self.commits = 0

    def cursor(self, **_kwargs):
        return self.cur

    def commit(self):
        self.commits += 1


def test_cleanup_deletes_only_calendar_copy_and_keeps_osc_row_active():
    action = _load(ROOT / "skills/osc-orchestrator/action.py", "osc_action_overdue_cleanup_test")
    conn = _Conn()
    service = _Service()

    result = action._remove_osc_only_overdue_calendar_events(
        conn,
        service,
        calendar_id="primary",
    )

    assert result == {
        "matched": 1,
        "would_remove": 1,
        "removed": 1,
        "already_gone": 0,
        "db_unlinked": 1,
        "cache_deleted": 1,
        "failed": 0,
        "items": [],
    }
    assert service.events_api.deleted == [
        {"calendarId": "primary", "eventId": "opaque-event"}
    ]
    update_sql = next(sql for sql, _ in conn.cur.queries if "UPDATE case_todos" in sql)
    assert "google_calendar_id=''" in update_sql
    assert "status=" not in update_sql
    assert "completed_date" not in update_sql
    assert conn.commits == 1
