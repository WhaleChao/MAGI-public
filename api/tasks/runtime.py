from __future__ import annotations

from typing import Any

from api.tasks.models import TaskRecord, TaskStatus
from api.tasks.store import TaskStore


class TaskRuntime:
    """Convenience wrapper for the in-memory task store."""

    def __init__(self, store: Optional[TaskStore] = None) -> None:
        self.store = store or TaskStore()

    def register(
        self,
        task_id: str,
        name: str,
        *,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        return self.store.register(task_id, name, description=description, metadata=metadata)

    def update(self, task_id: str, **changes: Any) -> TaskRecord:
        return self.store.update(task_id, **changes)

    def complete(self, task_id: str, *, result: Any = None, **changes: Any) -> TaskRecord:
        return self.store.complete(task_id, result=result, **changes)

    def fail(self, task_id: str, *, error: str, **changes: Any) -> TaskRecord:
        return self.store.fail(task_id, error=error, **changes)

    def cancel(self, task_id: str, *, reason: str = "", **changes: Any) -> TaskRecord:
        return self.store.cancel(task_id, reason=reason, **changes)

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self.store.get(task_id)

    def list(self, *, status: TaskStatus | Optional[str] = None) -> list[TaskRecord]:
        return self.store.list(status=status)

    def active(self) -> list[TaskRecord]:
        return self.store.active()
