from __future__ import annotations

from api.tasks import TaskRuntime, TaskStatus


def test_task_runtime_register_update_complete_and_list():
    runtime = TaskRuntime()

    created = runtime.register("task-1", "Nightly regression", description="Run checks")
    assert created.task_id == "task-1"
    assert created.status == TaskStatus.PENDING
    assert created.description == "Run checks"

    running = runtime.update("task-1", status=TaskStatus.RUNNING, progress=0.25, metadata={"phase": "collect"})
    assert running.status == TaskStatus.RUNNING
    assert running.started_at is not None
    assert running.progress == 0.25
    assert running.metadata["phase"] == "collect"

    completed = runtime.complete("task-1", result={"ok": True})
    assert completed.status == TaskStatus.COMPLETED
    assert completed.ended_at is not None
    assert completed.result == {"ok": True}

    records = runtime.list()
    assert [record.task_id for record in records] == ["task-1"]
    assert records[0].status == TaskStatus.COMPLETED


def test_task_runtime_fail_and_filter_by_status():
    runtime = TaskRuntime()
    runtime.register("task-a", "Alpha")
    runtime.register("task-b", "Beta")
    runtime.fail("task-b", error="boom")

    failed = runtime.list(status=TaskStatus.FAILED)
    pending = runtime.list(status="pending")

    assert [record.task_id for record in failed] == ["task-b"]
    assert [record.task_id for record in pending] == ["task-a"]
