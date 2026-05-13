from __future__ import annotations

from api.session import (
    SessionContextBuilder,
    SessionHistory,
    SessionPendingManager,
    SessionStore,
    SessionSummaryManager,
    append_message,
    build_session_context,
    clear_pending_state,
    get_pending_state,
    last_message,
    list_messages,
    set_pending_state,
    tail_messages,
    update_pending_state,
)
from api.tasks import TaskExecution, TaskStatus, create_task, start_task


def test_session_facades_share_store_and_keep_history_summary_pending_separate():
    store = SessionStore()
    history = SessionHistory(store)
    summaries = SessionSummaryManager(store)
    pending = SessionPendingManager(store)

    history.append("s-1", "user", "hello")
    history.append("s-1", "assistant", "hi")
    summaries.add("s-1", "summary text", authoritative=False)
    pending.update("s-1", case_id="CASE-1")

    builder = SessionContextBuilder(store)
    context = builder.build("s-1", system_prompt="system prompt")

    assert [msg.content for msg in history.list("s-1")] == ["hello", "hi"]
    assert summaries.latest("s-1").text == "summary text"
    assert pending.snapshot("s-1")["case_id"] == "CASE-1"
    assert context.assembled_messages[0]["content"] == "system prompt"
    assert "Derived summary (non-authoritative)" in context.rendered_text


def test_module_level_session_helpers_use_default_facade():
    session_id = "s-helpers"
    append_message(session_id, "user", "one")
    append_message(session_id, "assistant", "two")
    set_pending_state(session_id, {"draft": "pending"})

    assert [msg.content for msg in list_messages(session_id)] == ["one", "two"]
    assert tail_messages(session_id, 1)[0].content == "two"
    assert last_message(session_id).content == "two"
    assert get_pending_state(session_id).values["draft"] == "pending"

    update_pending_state(session_id, draft="ready")
    assert get_pending_state(session_id).values["draft"] == "ready"
    clear_pending_state(session_id)
    assert get_pending_state(session_id) is None


def test_session_context_builder_module_helper_round_trips_store():
    store = SessionStore()
    store.append_message("s-ctx", "user", "hi")
    store.add_summary("s-ctx", "summary one")
    store.update_pending_state("s-ctx", step="collect")

    context = build_session_context("s-ctx", store=store, system_prompt="system")

    assert context.session_id == "s-ctx"
    assert context.raw_history[0].content == "hi"
    assert context.summaries[0].text == "summary one"
    assert context.pending_state["step"] == "collect"
    assert "summary one" in context.rendered_text


def test_task_execution_facade_create_start_and_complete():
    execution = TaskExecution()

    created = execution.create("task-1", "Nightly", description="run")
    running = execution.start("task-1", "Nightly", description="run")
    completed = execution.complete("task-1", result={"ok": True})

    assert created.status == TaskStatus.PENDING
    assert running.status == TaskStatus.RUNNING
    assert completed.status == TaskStatus.COMPLETED
    assert execution.list()[0].task_id == "task-1"


def test_module_level_task_helpers_reuse_default_execution():
    created = create_task("task-helpers", "Helper task")
    running = start_task("task-helpers", "Helper task")

    assert created.status == TaskStatus.PENDING
    assert running.status == TaskStatus.RUNNING
