import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "skills" / "court-hearing-reminder" / "action.py"
    spec = importlib.util.spec_from_file_location("court_hearing_reminder_action_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_completion_type_hint_splits_client_and_todo_type():
    mod = _load_module()

    assert mod._extract_completion_type_hint("高弘軒調解") == ("高弘軒", ("調解",))
    assert mod._extract_completion_type_hint("吳美蓮陳報") == ("吳美蓮", ("陳報",))


def test_task_done_can_complete_non_payment_todo(monkeypatch):
    mod = _load_module()
    calls = []

    def fake_match(query, todo_types=None):
        calls.append((query, todo_types))
        if query == "高弘軒" and todo_types == ("調解",):
            return [
                {
                    "id": 123,
                    "client_name": "高弘軒",
                    "todo_type": "調解",
                    "court_case_number": "115年度司消債調字第73號",
                    "todo_date": "2026-06-01",
                }
            ]
        return []

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(mod, "_match_pending_todos", fake_match)
    monkeypatch.setattr(mod, "_get_conn", lambda: FakeConn())
    monkeypatch.setattr(mod, "_mark_todo_completed", lambda conn, todo_id: todo_id == 123)

    out = mod.task_done("高弘軒調解已完成", notify=False)

    assert calls[0] == ("高弘軒", ("調解",))
    assert "已標記完成" in out
