from __future__ import annotations

from typing import Any

from api.session.store import SessionStore


_DEFAULT_SESSION_STORE = SessionStore()


def _resolve_store_and_args(
    args: tuple[Any, ...],
    store: SessionStore | None,
) -> tuple[SessionStore, tuple[Any, ...]]:
    if store is not None:
        return store, args
    if args and not isinstance(args[0], str) and hasattr(args[0], "append_message") and hasattr(args[0], "list_messages"):
        return args[0], args[1:]
    return _DEFAULT_SESSION_STORE, args


class SessionHistory:
    def __init__(self, store: SessionStore | None = None) -> None:
        self.store = store or SessionStore()

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        source: str = "raw",
        metadata: dict[str, Any] | None = None,
    ):
        return self.store.append_message(session_id, role, content, source=source, metadata=metadata)

    def list(self, session_id: str):
        return self.store.list_messages(session_id)

    def tail(self, session_id: str, count: int):
        return self.store.list_messages(session_id)[-count:]

    def last(self, session_id: str):
        messages = self.store.list_messages(session_id)
        return messages[-1] if messages else None


def append_message(
    *args: Any,
    role: str | None = None,
    content: str | None = None,
    source: str = "raw",
    metadata: dict[str, Any] | None = None,
    store: SessionStore | None = None,
):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("append_message() missing session_id")
    session_id = remaining[0]
    if len(remaining) >= 3 and role is None and content is None:
        role = remaining[1]
        content = remaining[2]
    if role is None or content is None:
        raise TypeError("append_message() missing role or content")
    return resolved_store.append_message(str(session_id), str(role), str(content), source=source, metadata=metadata)


def list_messages(*args: Any, store: SessionStore | None = None):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("list_messages() missing session_id")
    return resolved_store.list_messages(str(remaining[0]))


def tail_messages(*args: Any, store: SessionStore | None = None):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if len(remaining) < 2:
        raise TypeError("tail_messages() missing session_id or count")
    session_id, count = remaining[0], remaining[1]
    return resolved_store.list_messages(str(session_id))[-int(count):]


def last_message(*args: Any, store: SessionStore | None = None):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("last_message() missing session_id")
    messages = resolved_store.list_messages(str(remaining[0]))
    return messages[-1] if messages else None
