from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any

from api.tasks.models import TaskRecord, TaskStatus, utcnow


class TaskStore:
    """Thread-safe in-memory task store."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.RLock()

    def _clone(self, record: TaskRecord) -> TaskRecord:
        return deepcopy(record)

    def register(
        self,
        task_id: str,
        name: str,
        *,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        now = utcnow()
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                record = TaskRecord(
                    task_id=task_id,
                    name=name,
                    description=description,
                    metadata=dict(metadata or {}),
                    created_at=now,
                    updated_at=now,
                )
                self._tasks[task_id] = record
            else:
                record.name = name or record.name
                if description:
                    record.description = description
                if metadata:
                    record.metadata.update(metadata)
                record.updated_at = now
            return self._clone(record)

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            record = self._tasks.get(task_id)
            return self._clone(record) if record else None

    def update(self, task_id: str, **changes: Any) -> TaskRecord:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError(f"Unknown task_id: {task_id}")
            return self._apply_changes(record, **changes)

    def complete(self, task_id: str, *, result: Any = None, **changes: Any) -> TaskRecord:
        changes = dict(changes)
        changes.update(
            {
                "status": TaskStatus.COMPLETED,
                "result": result,
                "ended_at": changes.get("ended_at", utcnow()),
            }
        )
        return self.update(task_id, **changes)

    def fail(self, task_id: str, *, error: str, **changes: Any) -> TaskRecord:
        changes = dict(changes)
        changes.update(
            {
                "status": TaskStatus.FAILED,
                "error": error,
                "ended_at": changes.get("ended_at", utcnow()),
            }
        )
        return self.update(task_id, **changes)

    def cancel(self, task_id: str, *, reason: str = "", **changes: Any) -> TaskRecord:
        changes = dict(changes)
        changes.update(
            {
                "status": TaskStatus.CANCELLED,
                "error": reason or changes.get("error", ""),
                "ended_at": changes.get("ended_at", utcnow()),
            }
        )
        return self.update(task_id, **changes)

    def list(self, *, status: TaskStatus | Optional[str] = None) -> list[TaskRecord]:
        with self._lock:
            records = list(self._tasks.values())
        if status is not None:
            wanted = TaskStatus(status) if not isinstance(status, TaskStatus) else status
            records = [record for record in records if record.status == wanted]
        records.sort(key=lambda record: (record.created_at, record.task_id))
        return [self._clone(record) for record in records]

    def active(self) -> list[TaskRecord]:
        records = self.list(status=TaskStatus.PENDING) + self.list(status=TaskStatus.RUNNING)
        records.sort(key=lambda record: (record.created_at, record.task_id))
        return records

    def _apply_changes(self, record: TaskRecord, **changes: Any) -> TaskRecord:
        now = utcnow()
        status = changes.pop("status", None)
        metadata = changes.pop("metadata", None)
        if "name" in changes and changes["name"]:
            record.name = changes.pop("name")
        if "description" in changes and changes["description"] is not None:
            record.description = changes.pop("description")
        if "progress" in changes and changes["progress"] is not None:
            record.progress = float(changes.pop("progress"))
        if "result" in changes:
            record.result = changes.pop("result")
        if "error" in changes:
            record.error = str(changes.pop("error") or "")
        if "started_at" in changes and changes["started_at"] is not None:
            record.started_at = changes.pop("started_at")
        if "ended_at" in changes and changes["ended_at"] is not None:
            record.ended_at = changes.pop("ended_at")
        if metadata:
            record.metadata.update(metadata)
        if status is not None:
            record.status = TaskStatus(status) if not isinstance(status, TaskStatus) else status
            if record.status == TaskStatus.RUNNING and record.started_at is None:
                record.started_at = now
            if record.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED} and record.ended_at is None:
                record.ended_at = now
        if record.status == TaskStatus.RUNNING and record.started_at is None:
            record.started_at = now
        record.updated_at = now
        return self._clone(record)
