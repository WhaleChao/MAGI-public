from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Mapping


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


RECENT_REFERENCE_KINDS = frozenset({"case", "person", "attachment", "schedule", "draft", "plan"})


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _json_object(value: Mapping[str, Any] | None, field_name: str) -> dict[str, Any]:
    data = dict(value or {})
    try:
        return json.loads(json.dumps(data, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-serializable") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc


@dataclass(frozen=True)
class SessionKey:
    """A platform-neutral conversation key.

    ``platform`` is a caller-defined label (for example, ``discord`` or
    ``telegram``); the model does not depend on any platform SDK.
    """

    platform: str
    conversation_id: str
    actor_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform", _required_text(self.platform, "platform"))
        object.__setattr__(self, "conversation_id", _required_text(self.conversation_id, "conversation_id"))
        if self.actor_id is not None:
            object.__setattr__(self, "actor_id", _required_text(self.actor_id, "actor_id"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "platform": self.platform,
            "conversation_id": self.conversation_id,
            "actor_id": self.actor_id,
        }

    def serialize(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SessionKey":
        if not isinstance(payload, Mapping):
            raise ValueError("session key must be an object")
        return cls(
            platform=payload.get("platform", ""),
            conversation_id=payload.get("conversation_id", ""),
            actor_id=payload.get("actor_id"),
        )

    @classmethod
    def deserialize(cls, raw: str) -> "SessionKey":
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("session key must be JSON") from exc
        return cls.from_dict(payload)


@dataclass(frozen=True)
class IdentityBinding:
    """An opt-in local association between a platform account and an identity."""

    identity_id: str
    platform: str
    actor_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "identity_id", _required_text(self.identity_id, "identity_id"))
        object.__setattr__(self, "platform", _required_text(self.platform, "platform"))
        object.__setattr__(self, "actor_id", _required_text(self.actor_id, "actor_id"))

    def to_dict(self) -> dict[str, str]:
        return {
            "identity_id": self.identity_id,
            "platform": self.platform,
            "actor_id": self.actor_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IdentityBinding":
        if not isinstance(payload, Mapping):
            raise ValueError("identity binding must be an object")
        return cls(
            identity_id=payload.get("identity_id", ""),
            platform=payload.get("platform", ""),
            actor_id=payload.get("actor_id", ""),
        )


@dataclass(frozen=True)
class RecentReference:
    """A short-lived, serializable subject available to reference resolution."""

    kind: str
    item_id: str
    label: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        kind = _required_text(self.kind, "kind")
        if kind not in RECENT_REFERENCE_KINDS:
            allowed = ", ".join(sorted(RECENT_REFERENCE_KINDS))
            raise ValueError(f"kind must be one of: {allowed}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "item_id", _required_text(self.item_id, "item_id"))
        object.__setattr__(self, "label", str(self.label or "").strip())
        object.__setattr__(self, "payload", _json_object(self.payload, "payload"))
        created_at = _as_utc(self.created_at)
        expires_at = _as_utc(self.expires_at) if self.expires_at else None
        if expires_at is not None and expires_at < created_at:
            raise ValueError("expires_at cannot be before created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and _as_utc(now or utcnow()) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "item_id": self.item_id,
            "label": self.label,
            "payload": _json_object(self.payload, "payload"),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RecentReference":
        if not isinstance(payload, Mapping):
            raise ValueError("recent reference must be an object")
        expires_raw = payload.get("expires_at")
        raw_payload = payload.get("payload", {})
        if raw_payload is None:
            raw_payload = {}
        if not isinstance(raw_payload, Mapping):
            raise ValueError("payload must be an object")
        return cls(
            kind=payload.get("kind", ""),
            item_id=payload.get("item_id", ""),
            label=payload.get("label", ""),
            payload=raw_payload,
            created_at=_parse_timestamp(payload.get("created_at"), "created_at"),
            expires_at=_parse_timestamp(expires_raw, "expires_at") if expires_raw is not None else None,
        )


@dataclass()
class SessionMessage:
    role: str
    content: str
    created_at: datetime = field(default_factory=utcnow)
    source: str = "raw"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass()
class SessionSummary:
    text: str
    created_at: datetime = field(default_factory=utcnow)
    source: str = "derived"
    authoritative: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass()
class SessionPendingState:
    values: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass()
class SessionContext:
    session_id: str
    raw_history: list[SessionMessage]
    summaries: list[SessionSummary]
    pending_state: dict[str, Any]
    assembled_messages: list[dict[str, Any]]
    rendered_text: str
    recent_references: dict[str, list[RecentReference]] = field(default_factory=dict)
