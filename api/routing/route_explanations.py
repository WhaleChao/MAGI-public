"""Route explanation collector.

Records routing decisions for debugging, auditing, and tracing.
Each decision captures why a route was chosen or rejected.

Usage::

    from api.routing.route_explanations import RouteExplanationCollector

    collector = RouteExplanationCollector()
    collector.record(skill="pdf-namer", confidence=0.82, dispatched=True, reason="DIRECT tier")
    trace = collector.as_trace()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RouteExplanation:
    skill: str
    confidence: float
    dispatched: bool
    reason: str
    intent: str = ""
    method: str = ""
    min_required: float = 0.0
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "confidence": round(self.confidence, 4),
            "dispatched": self.dispatched,
            "reason": self.reason,
            "intent": self.intent,
            "method": self.method,
            "min_required": self.min_required,
            "alternatives": self.alternatives,
        }


class RouteExplanationCollector:
    """Collects route explanations during a single message processing cycle."""

    def __init__(self) -> None:
        self._entries: list[RouteExplanation] = []

    def record(
        self,
        *,
        skill: str,
        confidence: float,
        dispatched: bool,
        reason: str,
        intent: str = "",
        method: str = "",
        min_required: float = 0.0,
        alternatives: list[dict[str, Any]] | None = None,
    ) -> None:
        self._entries.append(
            RouteExplanation(
                skill=skill,
                confidence=confidence,
                dispatched=dispatched,
                reason=reason,
                intent=intent,
                method=method,
                min_required=min_required,
                alternatives=alternatives or [],
            )
        )

    def record_rejection(
        self,
        *,
        skill: str,
        confidence: float,
        reason: str,
        intent: str = "",
        method: str = "",
        min_required: float = 0.0,
    ) -> None:
        """Shortcut for recording a rejected route."""
        self.record(
            skill=skill,
            confidence=confidence,
            dispatched=False,
            reason=reason,
            intent=intent,
            method=method,
            min_required=min_required,
        )

    def as_trace(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self._entries]

    @property
    def dispatched_skill(self) -> str | None:
        """Return the skill that was actually dispatched, if any."""
        for e in self._entries:
            if e.dispatched:
                return e.skill
        return None

    @property
    def had_dispatch(self) -> bool:
        return any(e.dispatched for e in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def summary(self) -> str:
        """Human-readable summary of the routing decision."""
        if not self._entries:
            return "無路由記錄"
        dispatched = [e for e in self._entries if e.dispatched]
        rejected = [e for e in self._entries if not e.dispatched]
        parts: list[str] = []
        if dispatched:
            e = dispatched[0]
            parts.append(f"已派送 → {e.skill} (信心={e.confidence:.2f}, 原因={e.reason})")
        if rejected:
            for e in rejected[:3]:
                parts.append(f"已拒絕 → {e.skill} (信心={e.confidence:.2f}, 原因={e.reason})")
        return " | ".join(parts)
