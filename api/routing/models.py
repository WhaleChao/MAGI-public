"""Unified routing data models.

Defines the core value objects returned by routing subsystems:

- **RoutingDecision** -- the outcome of request routing (which skill/handler
  to invoke and why).
- **ServiceTarget** -- a concrete inference endpoint (model + provider + URL).
- **FallbackPlan** -- an ordered list of ``ServiceTarget`` entries with retry
  policy so callers can transparently fail over.

All models are frozen dataclasses to guarantee immutability once created.

Usage::

    from api.routing.models import RoutingDecision, ServiceTarget, FallbackPlan

    target = ServiceTarget(service_name="omlx_inference", model_role="text_primary")
    plan = FallbackPlan(targets=[target])
    decision = RoutingDecision(action="dispatch", matched="pdf-namer", confidence=0.91)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from api.routing.context import RoutingContext


# ---------------------------------------------------------------------------
# ServiceTarget
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServiceTarget:
    """A concrete service endpoint to use for inference.

    Attributes:
        service_name:  Logical service name from ``service_registry``
                       (e.g. ``"omlx_inference"``).
        model_role:    Logical model role from ``model_registry``
                       (e.g. ``"text_primary"``, ``"vision"``).
        provider:      Provider tag (``"omlx"``, ``"openai"``, ``"anthropic"``...).
        endpoint:      Fully resolved URL to call.  May be empty if the
                       caller should resolve it lazily.
        model_id:      Concrete model identifier (e.g. ``"gemma-4-26b-a4b-it-4bit"``).
        priority:      Lower is higher priority.  Used for ordering inside
                       a :class:`FallbackPlan`.
        metadata:      Additional provider-specific metadata.
    """

    service_name: str = ""
    model_role: str = ""
    provider: str = ""
    endpoint: str = ""
    model_id: str = ""
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "model_role": self.model_role,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "priority": self.priority,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# FallbackPlan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FallbackPlan:
    """Ordered list of :class:`ServiceTarget` with retry policy.

    Callers iterate ``targets`` in order; if a target fails, they move to the
    next one.  ``max_retries`` controls how many total attempts (across *all*
    targets) are allowed.

    Attributes:
        targets:       Ordered list of service targets (highest priority first).
        max_retries:   Maximum total retry attempts across all targets.
        timeout_sec:   Per-target timeout in seconds.
        reason:        Human-readable explanation of why this plan was chosen.
    """

    targets: tuple[ServiceTarget, ...] = ()
    max_retries: int = 2
    timeout_sec: float = 30.0
    reason: str = ""

    @property
    def primary(self) -> Optional[ServiceTarget]:
        """Return the first (highest priority) target, or None."""
        return self.targets[0] if self.targets else None

    @property
    def has_fallback(self) -> bool:
        return len(self.targets) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "targets": [t.as_dict() for t in self.targets],
            "max_retries": self.max_retries,
            "timeout_sec": self.timeout_sec,
            "reason": self.reason,
        }

    @classmethod
    def from_targets(
        cls,
        targets: list[ServiceTarget],
        *,
        max_retries: int = 2,
        timeout_sec: float = 30.0,
        reason: str = "",
    ) -> FallbackPlan:
        """Convenience constructor accepting a mutable list."""
        sorted_targets = sorted(targets, key=lambda t: t.priority)
        return cls(
            targets=tuple(sorted_targets),
            max_retries=max_retries,
            timeout_sec=timeout_sec,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# RoutingDecision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoutingDecision:
    """The outcome of a routing decision.

    Designed as a structured replacement for the plain-dict returned by
    :func:`api.routing.route_decision.build_route_decision`.  The
    :meth:`to_legacy_dict` method produces the same dict shape for backward
    compatibility.

    Attributes:
        action:         Action verb (``"dispatch"``, ``"fallback"``,
                        ``"reject"``, ``"conversation"``).
        matched:        Matched skill/handler name (empty string if none).
        handler:        Handler function path or identifier.
        confidence:     Confidence of the match (0.0--1.0).
        reason:         Human-readable explanation.
        intent:         Classified intent that led to this decision.
        candidates:     Other skills that were considered.
        route_context:  The :class:`RoutingContext` that produced this decision.
        fallback_plan:  Inference fallback plan, if applicable.
        trace:          Full routing trace entries for debugging.
    """

    action: str = ""
    matched: str = ""
    handler: str = ""
    confidence: float = 0.0
    reason: str = ""
    intent: str = ""
    candidates: tuple[dict[str, Any], ...] = ()
    route_context: Optional[RoutingContext] = None
    fallback_plan: Optional[FallbackPlan] = None
    trace: tuple[dict[str, Any], ...] = ()

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------

    @property
    def success(self) -> bool:
        """True if a skill/handler was successfully matched."""
        return self.action in {"dispatch", "conversation"} and bool(self.matched)

    @property
    def requires_admin(self) -> bool:
        if self.route_context is not None:
            return self.route_context.requires_admin
        return False

    # ------------------------------------------------------------------
    # Backward compatibility
    # ------------------------------------------------------------------

    def to_legacy_dict(self) -> dict[str, Any]:
        """Produce a dict identical to ``build_route_decision()`` output.

        This allows new code to return a ``RoutingDecision`` while old code
        still expects a plain dict.
        """
        payload: dict[str, Any] = {
            "success": self.success,
            "matched": self.matched,
            "action": self.action,
            "requires_admin": self.requires_admin,
            "handler": self.handler,
            "confidence": float(self.confidence),
            "reason": self.reason or self.matched,
            "candidates": list(self.candidates),
        }
        if self.intent:
            payload["intent"] = self.intent
        return payload

    def as_dict(self) -> dict[str, Any]:
        """Full serialisation including trace and fallback plan."""
        d = self.to_legacy_dict()
        d["trace"] = list(self.trace)
        if self.fallback_plan is not None:
            d["fallback_plan"] = self.fallback_plan.as_dict()
        if self.route_context is not None:
            d["correlation_id"] = self.route_context.correlation_id
        return d

    @classmethod
    def from_legacy_dict(cls, data: dict[str, Any]) -> RoutingDecision:
        """Construct from a ``build_route_decision()``-style dict."""
        return cls(
            action=data.get("action", ""),
            matched=data.get("matched", ""),
            handler=data.get("handler", ""),
            confidence=float(data.get("confidence", 0.0)),
            reason=data.get("reason", ""),
            intent=data.get("intent", ""),
            candidates=tuple(data.get("candidates", [])),
        )
