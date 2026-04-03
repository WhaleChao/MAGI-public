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
    if args and not isinstance(args[0], str) and hasattr(args[0], "add_summary") and hasattr(args[0], "list_summaries"):
        return args[0], args[1:]
    return _DEFAULT_SESSION_STORE, args


class SessionSummaryManager:
    def __init__(self, store: SessionStore | None = None) -> None:
        self.store = store or SessionStore()

    def add(
        self,
        session_id: str,
        text: str,
        *,
        source: str = "derived",
        authoritative: bool = False,
        metadata: dict[str, Any] | None = None,
    ):
        return self.store.add_summary(session_id, text, source=source, authoritative=authoritative, metadata=metadata)

    def list(self, session_id: str):
        return self.store.list_summaries(session_id)

    def latest(self, session_id: str):
        summaries = self.list(session_id)
        return summaries[-1] if summaries else None


def add_summary(
    *args: Any,
    text: str | None = None,
    source: str = "derived",
    authoritative: bool = False,
    metadata: dict[str, Any] | None = None,
    store: SessionStore | None = None,
):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("add_summary() missing session_id")
    session_id = remaining[0]
    if len(remaining) >= 2 and text is None:
        text = remaining[1]
    if text is None:
        raise TypeError("add_summary() missing text")
    return resolved_store.add_summary(
        session_id,
        text,
        source=source,
        authoritative=authoritative,
        metadata=metadata,
    )


def list_summaries(*args: Any, store: SessionStore | None = None):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("list_summaries() missing session_id")
    return resolved_store.list_summaries(remaining[0])


def latest_summary(*args: Any, store: SessionStore | None = None):
    resolved_store, remaining = _resolve_store_and_args(args, store)
    if not remaining:
        raise TypeError("latest_summary() missing session_id")
    summaries = resolved_store.list_summaries(remaining[0])
    return summaries[-1] if summaries else None
