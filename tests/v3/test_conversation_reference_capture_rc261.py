from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock

from api.pipelines.chat_pipeline import append_history
from api.session.store import SessionStore


class _HistoryOrchestrator:
    def __init__(self):
        self.user_history = defaultdict(lambda: deque(maxlen=40))
        self._session_store = SessionStore()
        self._history_summaries = {}
        self._history_summaries_lock = Lock()

    def _ensure_runtime_foundations(self):
        return None


def test_user_case_identifier_becomes_recent_reference() -> None:
    orch = _HistoryOrchestrator()
    append_history(orch, "u1", "user", "請查詢2026-0062案件")
    refs = orch._session_store.list_recent("u1", kind="case")
    assert [ref.item_id for ref in refs] == ["2026-0062"]


def test_assistant_case_identifier_never_becomes_authoritative_reference() -> None:
    orch = _HistoryOrchestrator()
    append_history(orch, "u1", "assistant", "我猜可能是2026-9999")
    assert orch._session_store.list_recent("u1", kind="case") == []
