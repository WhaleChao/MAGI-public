from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    name = "court_hearing_reminder_completion_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "skills" / "court-hearing-reminder" / "action.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _todo(todo_id: int, *, case_number: str = "2026-0032", todo_date: str = "2026-09-01") -> dict:
    return {
        "id": todo_id,
        "case_number": case_number,
        "client_name": "陳玉梅",
        "court_name": "臺灣花蓮地方法院",
        "court_case_number": "115年度司消債調字第000099號",
        "case_reason": "更生",
        "case_type": "民事",
        "todo_type": "補正",
        "todo_date": todo_date,
        "todo_time": "",
        "description": "補正期限",
        "source_file": f"source-{todo_id}.pdf",
    }


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_structured_case_number_reason_and_client_are_and_terms():
    module = _load_module()
    row = _todo(6245)

    assert module._todo_matches_query(
        row,
        "115年度司消債調字第000099號（更生） | 陳玉梅",
    )
    assert module._todo_matches_query(
        row,
        "115年度司消債調字第99號｜陳玉梅",
    )
    assert not module._todo_matches_query(
        row,
        "115年度司消債調字第99號（清算） | 陳玉梅",
    )


def test_discord_fast_path_extracts_both_supported_completion_forms():
    from api.pipelines.message_router import extract_osc_todo_completion_target

    assert extract_osc_todo_completion_target("陳玉梅補正了") == "陳玉梅"
    assert extract_osc_todo_completion_target(
        "115年度司消債調字第000099號（更生） | 陳玉梅補正了"
    ) == "115年度司消債調字第000099號（更生） | 陳玉梅"


def test_discord_fast_path_preserves_completion_type_for_db_filter(monkeypatch):
    from api.pipelines import message_router

    seen = {}

    def run(query, *, todo_type_hint=""):
        seen["query"] = query
        seen["todo_type_hint"] = todo_type_hint
        return "✅ 已標記完成"

    monkeypatch.setattr(message_router, "_run_court_hearing_done", run)

    result = message_router.handle_osc_todo_completion_message(
        "admin",
        "115年度司消債調字第000099號（更生） | 陳玉梅補正了",
        platform="discord",
    )

    assert result == "✅ 已標記完成"
    assert seen == {
        "query": "115年度司消債調字第000099號（更生） | 陳玉梅",
        "todo_type_hint": "補正",
    }


def test_name_completion_closes_identical_duplicate_reminders_atomically(monkeypatch):
    module = _load_module()
    duplicates = [_todo(6245), _todo(6278)]
    seen = {}
    conn = _Connection()

    def match(query, *, todo_types=None):
        seen["query"] = query
        seen["todo_types"] = todo_types
        return duplicates

    def complete(actual_conn, todo_ids):
        assert actual_conn is conn
        seen["todo_ids"] = list(todo_ids)
        return 2

    monkeypatch.setattr(module, "_match_pending_todos", match)
    monkeypatch.setattr(module, "_get_conn", lambda: conn)
    monkeypatch.setattr(module, "_mark_todos_completed", complete)

    result = module.task_done("陳玉梅補正了", notify=False)

    assert seen == {
        "query": "陳玉梅",
        "todo_types": ("補正",),
        "todo_ids": [6245, 6278],
    }
    assert conn.closed is True
    assert "✅ 已標記完成：陳玉梅（補正，期限 2026-09-01）" in result
    assert "已同步關閉 2 筆重複提醒" in result


def test_full_discord_reply_preserves_structured_target_and_completion_type(monkeypatch):
    module = _load_module()
    seen = {}
    conn = _Connection()

    def match(query, *, todo_types=None):
        seen["query"] = query
        seen["todo_types"] = todo_types
        return [_todo(6245), _todo(6278)]

    monkeypatch.setattr(module, "_match_pending_todos", match)
    monkeypatch.setattr(module, "_get_conn", lambda: conn)
    monkeypatch.setattr(
        module,
        "_mark_todos_completed",
        lambda actual_conn, todo_ids: len(list(todo_ids)),
    )

    result = module.task_done(
        "115年度司消債調字第000099號（更生） | 陳玉梅補正了",
        notify=False,
    )

    assert seen == {
        "query": "115年度司消債調字第000099號（更生） | 陳玉梅",
        "todo_types": ("補正",),
    }
    assert result.startswith("✅ 已標記完成")
    assert "2 筆重複提醒" in result


def test_distinct_logical_todos_remain_ambiguous_and_are_not_completed(monkeypatch):
    module = _load_module()
    writes = []
    second = _todo(6300, case_number="2026-0041", todo_date="2026-09-08")
    second["court_case_number"] = "115年度司消債調字第000101號"

    monkeypatch.setattr(
        module,
        "_match_pending_todos",
        lambda query, *, todo_types=None: [_todo(6245), _todo(6278), second],
    )
    monkeypatch.setattr(module, "_get_conn", lambda: writes.append("opened"))

    result = module.task_done("陳玉梅補正了", notify=False)

    assert writes == []
    assert "匹配到 2 組待辦" in result
    assert "115年度司消債調字第000099號" in result
    assert "2 筆重複提醒" in result
    assert "115年度司消債調字第000101號" in result


def test_batch_completion_uses_one_update_and_rolls_back_on_failure():
    module = _load_module()
    calls = []

    class Cursor:
        rowcount = 2

        def execute(self, sql, params):
            calls.append((sql, params))

        def close(self):
            calls.append("cursor_closed")

    class Conn:
        def cursor(self):
            return Cursor()

        def commit(self):
            calls.append("committed")

    count = module._mark_todos_completed(Conn(), [6278, 6245, 6278, True, 0])

    assert count == 2
    assert calls[0][1] == (6278, 6245)
    assert "WHERE id IN (%s, %s)" in calls[0][0]
    assert calls[1:] == ["committed", "cursor_closed"]
