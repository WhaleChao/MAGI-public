from __future__ import annotations

from collections import defaultdict, deque
from threading import Event, Lock
from unittest.mock import patch

from api.events import MemoryWriteEvent, RouteDecisionEvent, TaskLifecycleEvent
from api.tasks import TaskStatus


def _make_partial_orchestrator(tmp_path):
    with patch("api.orchestrator.ThreadPoolExecutor"), \
         patch("api.orchestrator.switch_brain_mode"), \
         patch("api.orchestrator.get_brain_status"):
        from api.orchestrator import Orchestrator

        orc = object.__new__(Orchestrator)
        orc.user_history = defaultdict(lambda: deque(maxlen=40))
        orc._history_summaries = {}
        orc._history_summaries_lock = Lock()
        orc._history_summaries_maxsize = 100
        orc._HISTORY_COMPRESS_AT = 999
        orc._HISTORY_COMPRESS_KEEP = 8
        orc._HISTORY_TOKEN_BUDGET = 9999
        orc._SUMMARY_MAX_TOKENS = 256
        orc._estimate_tokens = lambda text: max(1, len(str(text)))
        orc._agent_dir = str(tmp_path)
        orc._runtime_events_file = str(tmp_path / "runtime_events.jsonl")
        orc._runtime_events_sink_registered = False
        orc._hook_bus = None
        orc._task_runtime = None
        orc._session_store = None
        orc._session_context_builder = None
        orc._permission_enforcer = None
        orc._heavy_task_lock = Lock()
        orc._heavy_tasks = {}
        orc._heavy_task_done_event = Event()
        orc._route_trace_file = str(tmp_path / "route_trace.jsonl")
        orc._route_trace_lock = Lock()
        orc._chatlog_last_write = {}
        orc._chatlog_last_write_maxsize = 5000
        orc._chatlog_last_write_lock = Lock()
        orc._redact_secrets = lambda text: text
        return orc


def test_append_history_mirrors_messages_into_session_store(tmp_path):
    orc = _make_partial_orchestrator(tmp_path)

    orc._append_history("u1", "user", "第一句")
    orc._append_history("u1", "assistant", "第二句")

    stored = orc._session_store.list_messages("u1")
    assert [msg.content for msg in stored] == ["第一句", "第二句"]
    assert [msg.role for msg in stored] == ["user", "assistant"]


def test_route_trace_emits_structured_route_event(tmp_path):
    orc = _make_partial_orchestrator(tmp_path)
    seen: list[RouteDecisionEvent] = []
    orc._ensure_runtime_foundations()
    orc._hook_bus.subscribe(RouteDecisionEvent, lambda event: seen.append(event))

    orc._append_route_trace(
        "u1",
        "LINE",
        "router",
        "summary",
        {"confidence": 0.42, "message": "幫我摘要", "reason": "keyword"},
    )

    assert len(seen) == 1
    assert seen[0].route_name == "summary"
    assert seen[0].confidence == 0.42
    assert seen[0].metadata["stage"] == "router"
    assert seen[0].metadata["user_id"] == "u1"


def test_heavy_task_runtime_mirrors_into_task_store_and_events(tmp_path):
    orc = _make_partial_orchestrator(tmp_path)
    seen: list[TaskLifecycleEvent] = []
    orc._ensure_runtime_foundations()
    orc._hook_bus.subscribe(TaskLifecycleEvent, lambda event: seen.append(event))

    orc.register_heavy_task("task-1", "摘要", "u1")
    task = orc._task_runtime.get("task-1")

    assert task is not None
    assert task.status == TaskStatus.RUNNING
    assert task.metadata["kind"] == "heavy"

    orc.unregister_heavy_task("task-1")
    task = orc._task_runtime.get("task-1")

    assert task is not None
    assert task.status == TaskStatus.COMPLETED
    assert [event.status for event in seen] == ["running", "completed"]


def test_chatlog_capture_emits_memory_write_event(monkeypatch, tmp_path):
    orc = _make_partial_orchestrator(tmp_path)
    seen: list[MemoryWriteEvent] = []
    orc._ensure_runtime_foundations()
    orc._hook_bus.subscribe(MemoryWriteEvent, lambda event: seen.append(event))
    monkeypatch.setenv("MAGI_CAPTURE_CHATLOG", "1")
    monkeypatch.setenv("MAGI_CHATLOG_MIN_INTERVAL_SEC", "0")

    with patch("skills.memory.mem_bridge.remember") as remember, \
         patch("skills.evolution.skill_genesis.validate_skill_safety", return_value=(True, [])):
        orc._maybe_capture_chatlog("u1", "LINE", "user", "這是可以寫入的內容")

    remember.assert_called_once()
    assert len(seen) == 1
    assert seen[0].accepted is True
    assert seen[0].platform == "LINE"
    assert seen[0].memory_kind == "chatlog"
    assert seen[0].source_signature.startswith("chatlog|platform=LINE|user=u1")
