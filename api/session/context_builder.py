from __future__ import annotations

from typing import Any

from api.session.history import SessionHistory
from api.session.models import SessionContext, SessionMessage, SessionPendingState, SessionSummary
from api.session.pending import SessionPendingManager
from api.session.summary import SessionSummaryManager
from api.session.store import SessionStore


class SessionContextBuilder:
    """Builds prompt-ready context while keeping raw, derived, and pending data separate."""

    def __init__(
        self,
        store: SessionStore | None = None,
        *,
        history: SessionHistory | None = None,
        summaries: SessionSummaryManager | None = None,
        pending: SessionPendingManager | None = None,
    ) -> None:
        base_store = store
        if base_store is None:
            if history is not None:
                base_store = history.store
            elif summaries is not None:
                base_store = summaries.store
            elif pending is not None:
                base_store = pending.store
            else:
                base_store = SessionStore()
        self.store = base_store
        self.history = history or SessionHistory(self.store)
        self.summaries = summaries or SessionSummaryManager(self.store)
        self.pending = pending or SessionPendingManager(self.store)

    def build(
        self,
        session_id: str,
        *,
        system_prompt: str = "",
        history_limit: int | None = None,
        summary_limit: int | None = None,
    ) -> SessionContext:
        raw_history = self.history.list(session_id)
        summaries = self.summaries.list(session_id)
        pending = self.pending.get(session_id)

        assembled_messages = self.assemble(
            raw_history,
            summaries,
            pending,
            system_prompt=system_prompt,
            history_limit=history_limit,
            summary_limit=summary_limit,
        )
        rendered_text = self.render_text(assembled_messages)
        return SessionContext(
            session_id=session_id,
            raw_history=raw_history,
            summaries=summaries,
            pending_state=dict(pending.values) if pending else {},
            assembled_messages=assembled_messages,
            rendered_text=rendered_text,
        )

    def assemble(
        self,
        raw_history: list[SessionMessage],
        summaries: list[SessionSummary],
        pending: SessionPendingState | None,
        *,
        system_prompt: str = "",
        history_limit: int | None = None,
        summary_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system_prompt.strip():
            messages.append(
                {
                    "role": "system",
                    "content": system_prompt.strip(),
                    "origin": "system_prompt",
                    "authoritative": True,
                }
            )

        selected_summaries = summaries if summary_limit is None else summaries[-summary_limit:]
        for summary in selected_summaries:
            content = f"Derived summary (non-authoritative)\n{summary.text.strip()}"
            messages.append(
                {
                    "role": "system",
                    "content": content,
                    "origin": "summary",
                    "authoritative": summary.authoritative,
                    "source": summary.source,
                }
            )

        if pending and pending.values:
            pending_lines = "\n".join(f"{key}: {value}" for key, value in pending.values.items())
            messages.append(
                {
                    "role": "system",
                    "content": "Pending state (derived)\n" + pending_lines,
                    "origin": "pending_state",
                    "authoritative": False,
                }
            )

        selected_history = raw_history if history_limit is None else raw_history[-history_limit:]
        for message in selected_history:
            messages.append(
                {
                    "role": message.role,
                    "content": message.content,
                    "origin": message.source,
                    "metadata": dict(message.metadata),
                }
            )

        return messages

    @staticmethod
    def render_text(messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for message in messages:
            role = message.get("role", "unknown")
            content = str(message.get("content", "")).strip()
            parts.append(f"[{role}] {content}")
        return "\n\n".join(parts)

    def build_prompt(
        self,
        session_id: str,
        *,
        system_prompt: str = "",
        history_limit: int | None = None,
        summary_limit: int | None = None,
    ) -> str:
        return self.build(
            session_id,
            system_prompt=system_prompt,
            history_limit=history_limit,
            summary_limit=summary_limit,
        ).rendered_text


def build_session_context(
    session_id: str,
    *,
    store: SessionStore | None = None,
    system_prompt: str = "",
    history_limit: int | None = None,
    summary_limit: int | None = None,
) -> SessionContext:
    return SessionContextBuilder(store).build(
        session_id,
        system_prompt=system_prompt,
        history_limit=history_limit,
        summary_limit=summary_limit,
    )


def assemble_session_messages(
    raw_history: list[SessionMessage],
    summaries: list[SessionSummary],
    pending: SessionPendingState | None,
    *,
    system_prompt: str = "",
    history_limit: int | None = None,
    summary_limit: int | None = None,
) -> list[dict[str, Any]]:
    return SessionContextBuilder().assemble(
        raw_history,
        summaries,
        pending,
        system_prompt=system_prompt,
        history_limit=history_limit,
        summary_limit=summary_limit,
    )
