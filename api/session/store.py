from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import threading
from typing import Any, Mapping

from api.session.models import RECENT_REFERENCE_KINDS, IdentityBinding, RecentReference, SessionKey, SessionMessage, SessionPendingState, SessionSummary, utcnow


DEFAULT_RECENT_REFERENCE_TTL_SECONDS = 60 * 60


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be an ISO-8601 string") from exc
    return _as_utc(parsed)


class SessionStore:
    """Thread-safe in-memory session store."""

    def __init__(self, *, recent_reference_ttl_seconds: float = DEFAULT_RECENT_REFERENCE_TTL_SECONDS) -> None:
        if not math.isfinite(recent_reference_ttl_seconds) or recent_reference_ttl_seconds < 0:
            raise ValueError("recent_reference_ttl_seconds cannot be negative")
        self._history: dict[str, list[SessionMessage]] = {}
        self._summaries: dict[str, list[SessionSummary]] = {}
        self._pending: dict[str, SessionPendingState] = {}
        self._recent: dict[str, list[RecentReference]] = {}
        self._identity_bindings: dict[tuple[str, str], IdentityBinding] = {}
        self.recent_reference_ttl_seconds = float(recent_reference_ttl_seconds)
        self._lock = threading.RLock()

    def _clone(self, value):
        return deepcopy(value)

    def bind_identity(self, identity_id: str, *, platform: str, actor_id: str) -> IdentityBinding:
        binding = IdentityBinding(identity_id=identity_id, platform=platform, actor_id=actor_id)
        with self._lock:
            self._identity_bindings[(binding.platform, binding.actor_id)] = binding
            return self._clone(binding)

    def get_identity_binding(self, *, platform: str, actor_id: str) -> IdentityBinding | None:
        with self._lock:
            binding = self._identity_bindings.get((str(platform).strip(), str(actor_id).strip()))
            return self._clone(binding) if binding else None

    def session_id_for(self, key: str | SessionKey, *, share_identity: bool = False) -> str:
        if not isinstance(key, SessionKey):
            return str(key)
        if share_identity and key.actor_id:
            binding = self.get_identity_binding(platform=key.platform, actor_id=key.actor_id)
            if binding:
                return f"identity:{binding.identity_id}"
        return key.serialize()

    def remember_recent(
        self,
        session_id: str | SessionKey,
        *,
        kind: str,
        item_id: str,
        label: str = "",
        payload: dict[str, Any] | None = None,
        ttl_seconds: float | None = None,
        now: datetime | None = None,
        share_identity: bool = False,
    ) -> RecentReference:
        observed_at = _as_utc(now or utcnow())
        ttl = self.recent_reference_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        if not math.isfinite(ttl) or ttl < 0:
            raise ValueError("ttl_seconds cannot be negative")
        reference = RecentReference(
            kind=kind,
            item_id=item_id,
            label=label,
            payload=dict(payload or {}),
            created_at=observed_at,
            expires_at=observed_at + timedelta(seconds=ttl),
        )
        resolved_id = self.session_id_for(session_id, share_identity=share_identity)
        with self._lock:
            active = [
                item
                for item in self._recent.get(resolved_id, [])
                if not item.is_expired(observed_at) and not (item.kind == reference.kind and item.item_id == reference.item_id)
            ]
            self._recent[resolved_id] = [reference, *active]
            return self._clone(reference)

    def list_recent(
        self,
        session_id: str | SessionKey,
        *,
        kind: str | None = None,
        now: datetime | None = None,
        share_identity: bool = False,
    ) -> list[RecentReference]:
        if kind is not None and kind not in RECENT_REFERENCE_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(RECENT_REFERENCE_KINDS))}")
        observed_at = _as_utc(now or utcnow())
        resolved_id = self.session_id_for(session_id, share_identity=share_identity)
        with self._lock:
            active = [item for item in self._recent.get(resolved_id, []) if not item.is_expired(observed_at)]
            if kind is not None:
                active = [item for item in active if item.kind == kind]
            return self._clone(active)

    def recent_by_kind(
        self,
        session_id: str | SessionKey,
        *,
        now: datetime | None = None,
        share_identity: bool = False,
    ) -> dict[str, list[RecentReference]]:
        grouped: dict[str, list[RecentReference]] = {}
        for item in self.list_recent(session_id, now=now, share_identity=share_identity):
            grouped.setdefault(item.kind, []).append(item)
        return grouped

    def purge_expired_references(self, *, now: datetime | None = None) -> int:
        observed_at = _as_utc(now or utcnow())
        removed = 0
        with self._lock:
            for session_id, items in list(self._recent.items()):
                active = [item for item in items if not item.is_expired(observed_at)]
                removed += len(items) - len(active)
                if active:
                    self._recent[session_id] = active
                else:
                    self._recent.pop(session_id, None)
        return removed

    def clear_recent(self, session_id: str | SessionKey, *, share_identity: bool = False) -> None:
        resolved_id = self.session_id_for(session_id, share_identity=share_identity)
        with self._lock:
            self._recent.pop(resolved_id, None)

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

    def get_pending_state(self, session_id: str) -> Optional[SessionPendingState]:
        with self._lock:
            pending = self._pending.get(session_id)
            return self._clone(pending) if pending else None

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            self._history.pop(session_id, None)
            self._summaries.pop(session_id, None)
            self._pending.pop(session_id, None)
            self._recent.pop(session_id, None)

    def clear_pending_state(self, session_id: str) -> None:
        with self._lock:
            self._pending.pop(session_id, None)

    def to_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        observed_at = _as_utc(now or utcnow())
        with self._lock:
            recent = {
                session_id: [item.to_dict() for item in items if not item.is_expired(observed_at)]
                for session_id, items in self._recent.items()
            }
            return {
                "version": 1,
                "recent_reference_ttl_seconds": self.recent_reference_ttl_seconds,
                "history": {
                    session_id: [
                        {
                            "role": item.role,
                            "content": item.content,
                            "created_at": item.created_at.isoformat(),
                            "source": item.source,
                            "metadata": self._clone(item.metadata),
                        }
                        for item in items
                    ]
                    for session_id, items in self._history.items()
                },
                "summaries": {
                    session_id: [
                        {
                            "text": item.text,
                            "created_at": item.created_at.isoformat(),
                            "source": item.source,
                            "authoritative": item.authoritative,
                            "metadata": self._clone(item.metadata),
                        }
                        for item in items
                    ]
                    for session_id, items in self._summaries.items()
                },
                "pending": {
                    session_id: {
                        "values": self._clone(item.values),
                        "updated_at": item.updated_at.isoformat(),
                    }
                    for session_id, item in self._pending.items()
                },
                "recent": recent,
                "identity_bindings": [item.to_dict() for item in self._identity_bindings.values()],
            }

    def to_json(self, *, now: datetime | None = None) -> str:
        return json.dumps(self.to_dict(now=now), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SessionStore":
        if not isinstance(payload, Mapping) or payload.get("version") != 1:
            raise ValueError("session store payload must be an object")
        store = cls(recent_reference_ttl_seconds=float(payload.get("recent_reference_ttl_seconds", DEFAULT_RECENT_REFERENCE_TTL_SECONDS)))
        history = payload.get("history", {})
        summaries = payload.get("summaries", {})
        pending = payload.get("pending", {})
        recent = payload.get("recent", {})
        bindings = payload.get("identity_bindings", [])
        if not all(isinstance(value, dict) for value in (history, summaries, pending, recent)) or not isinstance(bindings, list):
            raise ValueError("invalid session store payload")
        with store._lock:
            for session_id, rows in history.items():
                if not isinstance(rows, list):
                    raise ValueError("history rows must be lists")
                parsed_rows: list[SessionMessage] = []
                for row in rows:
                    if not isinstance(row, Mapping) or not isinstance(row.get("metadata", {}), Mapping):
                        raise ValueError("history rows must be objects")
                    parsed_rows.append(
                        SessionMessage(
                            role=str(row["role"]),
                            content=str(row["content"]),
                            created_at=_parse_timestamp(row["created_at"]),
                            source=str(row.get("source", "raw")),
                            metadata=dict(row.get("metadata") or {}),
                        )
                    )
                store._history[str(session_id)] = parsed_rows
            for session_id, rows in summaries.items():
                if not isinstance(rows, list):
                    raise ValueError("summary rows must be lists")
                parsed_rows: list[SessionSummary] = []
                for row in rows:
                    if not isinstance(row, Mapping) or not isinstance(row.get("metadata", {}), Mapping):
                        raise ValueError("summary rows must be objects")
                    authoritative = row.get("authoritative", False)
                    if not isinstance(authoritative, bool):
                        raise ValueError("summary authoritative must be boolean")
                    parsed_rows.append(
                        SessionSummary(
                            text=str(row["text"]),
                            created_at=_parse_timestamp(row["created_at"]),
                            source=str(row.get("source", "derived")),
                            authoritative=authoritative,
                            metadata=dict(row.get("metadata") or {}),
                        )
                    )
                store._summaries[str(session_id)] = parsed_rows
            for session_id, row in pending.items():
                if not isinstance(row, Mapping) or not isinstance(row.get("values", {}), Mapping):
                    raise ValueError("pending rows must be objects")
                store._pending[str(session_id)] = SessionPendingState(
                    values=dict(row.get("values") or {}),
                    updated_at=_parse_timestamp(row["updated_at"]),
                )
            for session_id, rows in recent.items():
                if not isinstance(rows, list):
                    raise ValueError("recent rows must be lists")
                if not all(isinstance(row, Mapping) for row in rows):
                    raise ValueError("recent rows must be objects")
                store._recent[str(session_id)] = [RecentReference.from_dict(row) for row in rows]
            for row in bindings:
                binding = IdentityBinding.from_dict(row)
                store._identity_bindings[(binding.platform, binding.actor_id)] = binding
        return store

    @classmethod
    def from_json(cls, raw: str) -> "SessionStore":
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("session store payload must be JSON") from exc
        return cls.from_dict(payload)
