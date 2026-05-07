"""Unified routing context.

Captures all information needed to make a routing decision in a single
immutable data class.  Every routing subsystem (request routing, inference
routing, policy engine) takes a ``RoutingContext`` as its primary input so
that the full decision trace is always available.

Usage::

    from api.routing.context import RoutingContext

    ctx = RoutingContext(
        user_id="u-abc",
        platform="line",
        message="幫我查一下案件進度",
    )
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RoutingContext:
    """Immutable snapshot of everything needed to route a single request.

    Attributes:
        user_id:          Unique identifier for the requesting user.
        platform:         Originating platform (``"line"``, ``"web"``, ``"api"``...).
        role:             User role (``"user"``, ``"admin"``, ``"system"``).
        message:          Raw message text from the user.
        intent:           Classified intent (``"CHAT"``, ``"QUERY"``, ``"CMD"``...).
        correlation_id:   Unique ID tying all trace entries for one request.
        confidence:       Confidence of the intent classification (0.0--1.0).
        matched_skill:    Skill name matched by an earlier routing stage, if any.
        method:           How the skill was matched (``"semantic"``, ``"keyword"``...).
        requires_admin:   Whether the resolved action needs admin privileges.
        channel_context:  Opaque per-channel metadata (group id, thread id...).
        attachment_type:  MIME-like hint for attachments (``"image"``, ``"pdf"``...).
        extra:            Escape hatch for additional metadata.
    """

    # --- identity ---
    user_id: str = ""
    platform: str = ""
    role: str = "user"

    # --- message ---
    message: str = ""
    intent: str = ""
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # --- routing hints ---
    confidence: float = 0.0
    matched_skill: str = ""
    method: str = ""

    # --- access control ---
    requires_admin: bool = False

    # --- channel / attachment ---
    channel_context: dict[str, Any] = field(default_factory=dict)
    attachment_type: str = ""

    # --- extensible ---
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def has_attachment(self) -> bool:
        """Return True if this request carries an attachment."""
        return bool(self.attachment_type)

    @property
    def is_admin(self) -> bool:
        """Shortcut: user has admin role."""
        return self.role == "admin"

    def with_overrides(self, **kwargs: Any) -> RoutingContext:
        """Return a *new* context with selected fields replaced.

        Because the dataclass is frozen we cannot mutate in place.  This
        helper creates a shallow copy with the given overrides applied.
        """
        current = {f.name: getattr(self, f.name) for f in self.__dataclass_fields__.values()}
        current.update(kwargs)
        return RoutingContext(**current)

    def as_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (JSON-friendly)."""
        return {
            "user_id": self.user_id,
            "platform": self.platform,
            "role": self.role,
            "message": self.message,
            "intent": self.intent,
            "correlation_id": self.correlation_id,
            "confidence": self.confidence,
            "matched_skill": self.matched_skill,
            "method": self.method,
            "requires_admin": self.requires_admin,
            "channel_context": self.channel_context,
            "attachment_type": self.attachment_type,
            "extra": self.extra,
        }
