from __future__ import annotations

from api.session import (
    SessionContextBuilder,
    SessionHistory,
    SessionPendingManager,
    SessionStore,
    SessionSummaryManager,
    append_message,
    build_session_context,
    latest_summary,
)
from api.tasks import TaskExecution, TaskExecutor, TaskRuntime, TaskStatus, cancel_task, complete_task, create_task, fail_task, start_task, update_task


def test_session_facades_build_context_and_keep_layers_separated():
    store = SessionStore()
    history = SessionHistory(store)
    summary = SessionSummaryManager(store)
    pending = SessionPendingManager(store)

    history.append("s1", "user", "第一句")
    summary.add("s1", "摘要", authoritative=False)
    pending.set("s1", {"case_no": "A123"})

    context = SessionContextBuilder(store).build("s1", system_prompt="system prompt")

    assert context.rendered_text.startswith("[system] system prompt")
    assert context.pending_state["case_no"] == "A123"
    assert latest_summary("s1", store=store).text == "摘要"


def test_task_facades_execute_and_track_state():
    runtime = TaskRuntime()
    execution = TaskExecution(runtime)
    task = execution.create("t1", "demo")
    assert task.status == TaskStatus.PENDING

    task = execution.start("t1", "demo")
    assert task.status == TaskStatus.RUNNING

    task = execution.complete("t1", result={"ok": True})
    assert task.status == TaskStatus.COMPLETED

    task = execution.update("t1", progress=1.0)
    assert task.progress == 1.0

    runtime.register("t2", "demo2")
    task = execution.cancel("t2", reason="stop")
    assert task.status == TaskStatus.CANCELLED

    runtime.register("t3", "demo3")
    task = execution.fail("t3", error="boom")
    assert task.status == TaskStatus.FAILED


def test_task_executor_runs_callable_and_captures_failures():
    executor = TaskExecutor()
    result = executor.run("task-1", "echo", lambda value: {"value": value}, value=123)
    assert result.success is True
    assert result.output == {"value": 123}

    failed = executor.run("task-2", "boom", lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    assert failed.success is False
    assert "nope" in failed.error
