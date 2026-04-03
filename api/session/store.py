from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any

from api.session.models import SessionMessage, SessionPendingState, SessionSummary, utcnow


class SessionStore:
    """Thread-safe in-memory session store."""

    def __init__(self) -> None:
        self._history: dict[str, list[SessionMessage]] = {}
        self._summaries: dict[str, list[SessionSummary]] = {}
        self._pending: dict[str, SessionPendingState] = {}
        self._lock = threading.RLock()

    def _clone(self, value):
        return deepcopy(value)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        source: str = "raw",
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessage:
        with self._lock:
            message = SessionMessage(role=role, content=content, source=source, metadata=dict(metadata or {}))
            self._history.setdefault(session_id, []).append(message)
            return self._clone(message)

    def list_messages(self, session_id: str) -> list[SessionMessage]:
        with self._lock:
            return self._clone(self._history.get(session_id, []))

    def add_summary(
        self,
        session_id: str,
        text: str,
        *,
        source: str = "derived",
        authoritative: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSummary:
        with self._lock:
            summary = SessionSummary(
                text=text,
                source=source,
                authoritative=authoritative,
                metadata=dict(metadata or {}),
            )
            self._summaries.setdefault(session_id, []).append(summary)
            return self._clone(summary)

    def list_summaries(self, session_id: str) -> list[SessionSummary]:
        with self._lock:
            return self._clone(self._summaries.get(session_id, []))

    def set_pending_state(self, session_id: str, values: dict[str, Any]) -> SessionPendingState:
        with self._lock:
            pending = SessionPendingState(values=dict(values), updated_at=utcnow())
            self._pending[session_id] = pending
            return self._clone(pending)

    def update_pending_state(self, session_id: str, **updates: Any) -> SessionPendingState:
        with self._lock:
            pending = self._pending.get(session_id)
            if pending is None:
                pending = SessionPendingState()
                self._pending[session_id] = pending
            pending.values.update(updates)
            pending.updated_at = utcnow()
            return self._clone(pending)

    def get_pending_state(self, session_id: str) -> SessionPendingState | None:
        with self._lock:
            pending = self._pending.get(session_id)
            return self._clone(pending) if pending else None

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            self._history.pop(session_id, None)
            self._summaries.pop(session_id, None)
            self._pending.pop(session_id, None)

    def clear_pending_state(self, session_id: str) -> None:
        with self._lock:
            self._pending.pop(session_id, None)
