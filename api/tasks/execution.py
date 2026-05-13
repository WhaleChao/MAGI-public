from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from api.tasks.models import TaskRecord, TaskStatus
from api.tasks.runtime import TaskRuntime


TaskCallable = Callable[..., Any]


def _resolve_runtime_and_args(
    args: tuple[Any, ...],
    runtime: Optional[TaskRuntime],
) -> tuple[TaskRuntime, tuple[Any, ...]]:
    if runtime is not None:
        return runtime, args
    if args and not isinstance(args[0], str):
        candidate = args[0]
        if hasattr(candidate, "register") and hasattr(candidate, "update") and hasattr(candidate, "list"):
            return candidate, args[1:]
        if hasattr(candidate, "runtime") and hasattr(candidate.runtime, "register"):
            return candidate.runtime, args[1:]
    return _DEFAULT_TASK_EXECUTION.runtime, args


@dataclass()
class TaskExecutionResult:
    task: TaskRecord
    output: Any = None
    error: str = ""

    @property
    def success(self) -> bool:
        return self.task.status == TaskStatus.COMPLETED and not self.error


class TaskExecution:
    """Compatibility facade expected by the legacy tests and call sites."""

    def __init__(self, runtime: Optional[TaskRuntime] = None) -> None:
        self.runtime = runtime or TaskRuntime()

    def create(
        self,
        task_id: str,
        name: str,
        *,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        return self.runtime.register(task_id, name, description=description, metadata=metadata)

    def start(
        self,
        task_id: str,
        name: str,
        *,
        description: Optional[str] = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        self.create(task_id, name, description=description or "", metadata=metadata)
        changes: dict[str, Any] = {"name": name, "status": TaskStatus.RUNNING, "metadata": metadata or {}}
        if description is not None:
            changes["description"] = description
        return self.runtime.update(task_id, **changes)

    def update(self, task_id: str, **changes: Any) -> TaskRecord:
        return self.runtime.update(task_id, **changes)

    def complete(self, task_id: str, *, result: Any = None, **changes: Any) -> TaskRecord:
        return self.runtime.complete(task_id, result=result, **changes)

    def fail(self, task_id: str, *, error: str, **changes: Any) -> TaskRecord:
        return self.runtime.fail(task_id, error=error, **changes)

    def cancel(self, task_id: str, *, reason: str = "", **changes: Any) -> TaskRecord:
        return self.runtime.cancel(task_id, reason=reason, **changes)

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self.runtime.get(task_id)

    def list(self, *, status: TaskStatus | Optional[str] = None) -> list[TaskRecord]:
        return self.runtime.list(status=status)

    def active(self) -> list[TaskRecord]:
        return self.runtime.active()


@dataclass()
class TaskExecutor:
    runtime: TaskRuntime = field(default_factory=TaskRuntime)

    def run(
        self,
        task_id: str,
        name: str,
        fn: TaskCallable,
        *,
        description: str = "",
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> TaskExecutionResult:
        record = self.runtime.register(task_id, name, description=description, metadata=metadata)
        self.runtime.update(task_id, status=TaskStatus.RUNNING)
        try:
            output = fn(**kwargs)
        except Exception as exc:
            record = self.runtime.fail(task_id, error=str(exc))
            return TaskExecutionResult(task=record, error=str(exc))
        record = self.runtime.complete(task_id, result=output)
        return TaskExecutionResult(task=record, output=output)


_DEFAULT_TASK_EXECUTION = TaskExecution()


def create_task(
    *args: Any,
    description: str = "",
    metadata: dict[str, Any] | None = None,
    runtime: Optional[TaskRuntime] = None,
) -> TaskRecord:
    resolved_runtime, remaining = _resolve_runtime_and_args(args, runtime)
    if len(remaining) < 2:
        raise TypeError("create_task() missing task_id or name")
    task_id, name = remaining[0], remaining[1]
    return TaskExecution(resolved_runtime).create(task_id, name, description=description, metadata=metadata)


def start_task(
    *args: Any,
    description: str = "",
    metadata: dict[str, Any] | None = None,
    runtime: Optional[TaskRuntime] = None,
) -> TaskRecord:
    resolved_runtime, remaining = _resolve_runtime_and_args(args, runtime)
    if len(remaining) < 2:
        raise TypeError("start_task() missing task_id or name")
    task_id, name = remaining[0], remaining[1]
    return TaskExecution(resolved_runtime).start(task_id, name, description=description, metadata=metadata)


def update_task(*args: Any, runtime: Optional[TaskRuntime] = None, **changes: Any) -> TaskRecord:
    resolved_runtime, remaining = _resolve_runtime_and_args(args, runtime)
    if not remaining:
        raise TypeError("update_task() missing task_id")
    return TaskExecution(resolved_runtime).update(remaining[0], **changes)


def complete_task(*args: Any, result: Any = None, runtime: Optional[TaskRuntime] = None, **changes: Any) -> TaskRecord:
    resolved_runtime, remaining = _resolve_runtime_and_args(args, runtime)
    if not remaining:
        raise TypeError("complete_task() missing task_id")
    return TaskExecution(resolved_runtime).complete(remaining[0], result=result, **changes)


def fail_task(*args: Any, error: str, runtime: Optional[TaskRuntime] = None, **changes: Any) -> TaskRecord:
    resolved_runtime, remaining = _resolve_runtime_and_args(args, runtime)
    if not remaining:
        raise TypeError("fail_task() missing task_id")
    return TaskExecution(resolved_runtime).fail(remaining[0], error=error, **changes)


def cancel_task(*args: Any, reason: str = "", runtime: Optional[TaskRuntime] = None, **changes: Any) -> TaskRecord:
    resolved_runtime, remaining = _resolve_runtime_and_args(args, runtime)
    if not remaining:
        raise TypeError("cancel_task() missing task_id")
    return TaskExecution(resolved_runtime).cancel(remaining[0], reason=reason, **changes)
