from __future__ import annotations

from typing import Any

from api.session.store import SessionStore


_DEFAULT_SESSION_STORE = SessionStore()


def _resolve_store_and_args(
    args: tuple[Any, ...],
    store: Optional[SessionStore],
) -> tuple[SessionStore, tuple[Any, ...]]:
    if store is not None:
        return store, args
    if args and not isinstance(args[0], str) and hasattr(args[0], "set_pending_state") and hasattr(args[0], "get_pending_state"):
        return args[0], args[1:]
    return _DEFAULT_SESSION_STORE, args


class SessionPendingManager:
    def __init__(self, store: Optional[SessionStore] = None) -> None:
        self.store = store or SessionStore()

    def set(self, session_id: str, values: dict[str, Any]):
        return self.store.set_pending_state(session_id, values)

    def update(self, session_id: str, **updates: Any):
        return self.store.update_pending_state(session_id, **updates)

    def get(self, session_id: str):
        return self.store.get_pending_state(session_id)

    def snapshot(self, session_id: str) -> dict[str, Any]:
        pending = self.get(session_id)
        return dict(pending.values) if pending else {}

    def clear(self, session_id: str) -> None:
        self.store.clear_pending_state(session_id)


def set_pending_state(*args: Any, values: dict[str, Any] | None = None, store: Optional[SessionStore] = None):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("set_pending_state() missing session_id")
    session_id = remaining[0]
    if len(remaining) >= 2 and values is None:
        values = remaining[1]
    if values is None:
        raise TypeError("set_pending_state() missing values")
    return resolved_store.set_pending_state(session_id, values)


def update_pending_state(*args: Any, store: Optional[SessionStore] = None, **updates: Any):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("update_pending_state() missing session_id")
    return resolved_store.update_pending_state(remaining[0], **updates)


def get_pending_state(*args: Any, store: Optional[SessionStore] = None):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("get_pending_state() missing session_id")
    return resolved_store.get_pending_state(remaining[0])


def clear_pending_state(*args: Any, store: Optional[SessionStore] = None) -> None:
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("clear_pending_state() missing session_id")
    resolved_store.clear_pending_state(remaining[0])
