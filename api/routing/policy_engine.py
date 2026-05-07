"""Unified policy engine for routing decisions.

Wraps the lower-level helpers in :mod:`api.routing.route_policy` into a
stateful engine that:

1. Accepts a :class:`~api.routing.context.RoutingContext`.
2. Applies confidence thresholds, generic-word detection, and high-risk
   skill gates from the base policy.
3. Optionally loads runtime overrides from
   ``.agent/routing_runtime.json``.
4. Returns a :class:`~api.routing.models.RoutingDecision`.

Usage::

    from api.routing.policy_engine import PolicyEngine
    from api.routing.context import RoutingContext

    engine = PolicyEngine()
    ctx = RoutingContext(message="幫我查案件", matched_skill="case_query", confidence=0.72)
    decision = engine.evaluate(ctx)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from api.routing.context import RoutingContext
from api.routing.models import RoutingDecision
from api.routing.route_policy import (
    get_skill_min_confidence,
    is_generic_word_only,
    should_dispatch_skill,
    build_route_explanation,
)

_log = logging.getLogger(__name__)

# Default path for runtime overrides
_RUNTIME_OVERRIDE_PATH = Path(".agent") / "routing_runtime.json"


class PolicyEngine:
    """Evaluate routing policy for a single request.

    Parameters:
        runtime_override_path:  Path to a JSON file with runtime overrides.
            If ``None``, uses the default ``.agent/routing_runtime.json``.
            Pass an explicit ``Path`` for testing.
    """

    def __init__(
        self,
        *,
        runtime_override_path: Optional[Path] = None,
    ) -> None:
        self._override_path = runtime_override_path or _RUNTIME_OVERRIDE_PATH
        self._overrides: dict[str, Any] = {}
        self._load_overrides()

    # ------------------------------------------------------------------
    # Runtime overrides
    # ------------------------------------------------------------------

    def _load_overrides(self) -> None:
        """Load runtime overrides from disk (best-effort)."""
        path = self._override_path
        if not path.exists():
            self._overrides = {}
            return
        try:
            self._overrides = json.loads(path.read_text(encoding="utf-8"))
            _log.debug("Loaded routing overrides from %s", path)
        except Exception:
            _log.warning("Failed to load routing overrides from %s", path, exc_info=True)
            self._overrides = {}

    def reload_overrides(self) -> None:
        """Force-reload runtime overrides from disk."""
        self._load_overrides()

    def get_override(self, key: str, default: Any = None) -> Any:
        """Return a runtime override value, or *default*."""
        return self._overrides.get(key, default)

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate(self, ctx: RoutingContext) -> RoutingDecision:
        """Apply policy rules and return a routing decision.

        The engine checks (in order):
        1. Forced overrides (skill pinned in runtime config).
        2. Generic-word gate (reject if message is only generic words).
        3. Confidence threshold gate (per-skill minimum).
        4. CHAT-intent dampening.

        If all gates pass the matched skill is dispatched; otherwise the
        decision falls back to conversation.
        """
        skill = ctx.matched_skill
        confidence = ctx.confidence
        message = ctx.message
        intent = ctx.intent
        method = ctx.method

        trace_entries: list[dict[str, Any]] = []

        # --- 1. Forced skill override ---
        forced_skill = self._overrides.get("force_skill")
        if forced_skill and isinstance(forced_skill, str):
            _log.info("Policy override: forcing skill=%s", forced_skill)
            trace_entries.append(
                build_route_explanation(
                    skill_name=forced_skill,
                    confidence=1.0,
                    dispatched=True,
                    reason="runtime_override",
                    intent=intent,
                    method="override",
                )
            )
            return RoutingDecision(
                action="dispatch",
                matched=forced_skill,
                handler=forced_skill,
                confidence=1.0,
                reason="runtime_override",
                intent=intent,
                route_context=ctx,
                trace=tuple(trace_entries),
            )

        # --- 2. No skill matched → conversation ---
        if not skill:
            trace_entries.append(
                build_route_explanation(
                    skill_name="(none)",
                    confidence=confidence,
                    dispatched=False,
                    reason="no_skill_matched",
                    intent=intent,
                    method=method,
                )
            )
            return RoutingDecision(
                action="conversation",
                matched="",
                confidence=confidence,
                reason="no_skill_matched",
                intent=intent,
                route_context=ctx,
                trace=tuple(trace_entries),
            )

        # --- 3. Apply override thresholds ---
        override_thresholds: dict[str, float] = self._overrides.get("thresholds", {})
        if skill in override_thresholds:
            min_conf = override_thresholds[skill]
            if confidence < min_conf:
                trace_entries.append(
                    build_route_explanation(
                        skill_name=skill,
                        confidence=confidence,
                        dispatched=False,
                        reason=f"override_threshold ({min_conf})",
                        intent=intent,
                        method=method,
                    )
                )
                return self._fallback(ctx, trace_entries, reason=f"override_threshold ({min_conf})")

        # --- 4. Delegate to route_policy.should_dispatch_skill ---
        dispatched = should_dispatch_skill(
            skill, confidence, message, intent=intent, method=method,
        )

        if dispatched:
            trace_entries.append(
                build_route_explanation(
                    skill_name=skill,
                    confidence=confidence,
                    dispatched=True,
                    reason="policy_pass",
                    intent=intent,
                    method=method,
                )
            )
            return RoutingDecision(
                action="dispatch",
                matched=skill,
                handler=skill,
                confidence=confidence,
                reason="policy_pass",
                intent=intent,
                route_context=ctx,
                trace=tuple(trace_entries),
            )

        # --- 5. Determine rejection reason ---
        if is_generic_word_only(message):
            reject_reason = "generic_word_only"
        elif confidence < get_skill_min_confidence(skill):
            reject_reason = f"below_threshold ({get_skill_min_confidence(skill)})"
        elif intent == "CHAT" and confidence < 0.78:
            reject_reason = "chat_intent_dampening"
        else:
            reject_reason = "policy_reject"

        trace_entries.append(
            build_route_explanation(
                skill_name=skill,
                confidence=confidence,
                dispatched=False,
                reason=reject_reason,
                intent=intent,
                method=method,
            )
        )
        return self._fallback(ctx, trace_entries, reason=reject_reason)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback(
        ctx: RoutingContext,
        trace: list[dict[str, Any]],
        *,
        reason: str,
    ) -> RoutingDecision:
        """Produce a conversation-fallback decision."""
        return RoutingDecision(
            action="conversation",
            matched="",
            confidence=ctx.confidence,
            reason=reason,
            intent=ctx.intent,
            route_context=ctx,
            trace=tuple(trace),
        )
