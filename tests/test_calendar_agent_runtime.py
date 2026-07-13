from datetime import date, datetime

from api.domains.calendar_agent import CalendarEvent
from api.domains.calendar_agent_runtime import handle_calendar_message
from api.session import SessionStore


class FakeOrchestrator:
    def __init__(self):
        self._session_store = SessionStore()

    def _ensure_runtime_foundations(self):
        return None


class MemoryCalendarRepository:
    def __init__(self, events=None):
        self.events = list(events or [])
        self.create_calls = 0
        self.update_calls = 0
        self.cancel_calls = 0

    def list_events(self, *, start=None, end=None, hint="", mutable_only=False):
        rows = self.events
        if start:
            rows = [item for item in rows if item.start >= start]
        if end:
            rows = [item for item in rows if item.start < end]
        if hint:
            rows = [item for item in rows if hint in item.title]
        if mutable_only:
            rows = [item for item in rows if item.event_id.startswith("todo:")]
        return list(rows)

    def create_event(self, event):
        self.create_calls += 1
        saved = CalendarEvent(**{**event.__dict__, "event_id": f"todo:{len(self.events) + 1}"})
        self.events.append(saved)
        return saved

    def update_event(self, event_id, event):
        self.update_calls += 1
        self.events = [event if item.event_id == event_id else item for item in self.events]
        return event

    def cancel_event(self, event_id):
        self.cancel_calls += 1
        self.events = [item for item in self.events if item.event_id != event_id]
        return True


REFERENCE = date(2026, 7, 6)


def _turn(orch, repo, text):
    return handle_calendar_message(
        orch,
        text,
        user_id="owner",
        platform="discord",
        repository=repo,
        reference_date=REFERENCE,
    )


def test_create_requires_preview_and_explicit_confirmation_before_write():
    orch = FakeOrchestrator()
    repo = MemoryCalendarRepository()

    preview = _turn(orch, repo, "請新增明天下午3點和客戶開會")

    assert "請確認是否建立" in preview
    assert repo.create_calls == 0
    assert "和客戶開會" in preview

    result = _turn(orch, repo, "確認")

    assert "已建立並通過回查驗證" in result
    assert repo.create_calls == 1


def test_decline_clears_pending_without_write():
    orch = FakeOrchestrator()
    repo = MemoryCalendarRepository()

    _turn(orch, repo, "請新增明天下午3點和客戶開會")
    result = _turn(orch, repo, "先不要")

    assert "資料未異動" in result
    assert repo.create_calls == 0
    assert _turn(orch, repo, "確認") is None


def test_duplicate_is_not_written_and_conflict_is_explained_in_preview():
    duplicate = CalendarEvent("和客戶開會", datetime(2026, 7, 7, 15), datetime(2026, 7, 7, 16), event_id="todo:1")
    orch = FakeOrchestrator()
    repo = MemoryCalendarRepository([duplicate])

    result = _turn(orch, repo, "請新增明天下午3點和客戶開會")

    assert "相同行程已存在" in result
    assert repo.create_calls == 0


def test_query_lists_concrete_items_instead_of_only_a_count():
    event = CalendarEvent("客戶會議", datetime(2026, 7, 7, 15), datetime(2026, 7, 7, 16), event_id="todo:1")
    result = _turn(FakeOrchestrator(), MemoryCalendarRepository([event]), "明天有哪些行程")

    assert "共找到 1 筆" in result
    assert "客戶會議" in result
    assert "2026/07/07 15:00" in result


def test_modify_and_cancel_only_execute_after_target_is_unique_and_confirmed():
    event = CalendarEvent("客戶會議", datetime(2026, 7, 7, 15), datetime(2026, 7, 7, 16), event_id="todo:7")
    orch = FakeOrchestrator()
    repo = MemoryCalendarRepository([event])

    preview = _turn(orch, repo, "把明天的客戶會議改到後天上午10點")
    assert "請確認是否修改" in preview
    assert repo.update_calls == 0
    assert "已修改並通過回查驗證" in _turn(orch, repo, "確認")
    assert repo.update_calls == 1

    preview = _turn(orch, repo, "取消後天的客戶會議")
    assert "請確認是否取消" in preview
    assert "行程已取消" in _turn(orch, repo, "確認")
    assert repo.cancel_calls == 1


def test_pending_change_does_not_treat_unrelated_text_as_confirmation():
    orch = FakeOrchestrator()
    repo = MemoryCalendarRepository()
    _turn(orch, repo, "請新增明天下午3點和客戶開會")

    result = _turn(orch, repo, "順便幫我查案件")

    assert "等待確認" in result
    assert repo.create_calls == 0
