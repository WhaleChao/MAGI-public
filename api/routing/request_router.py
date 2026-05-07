"""Unified request router.

Provides a single entry point that merges the outputs of all routing
subsystems (orchestrator, channel, semantic, embedding) into one
:class:`~api.routing.models.RoutingDecision` with a full trace.

The router does **not** replace the existing subsystems; it orchestrates
them and applies the :class:`~api.routing.policy_engine.PolicyEngine`
before returning a decision.

Usage::

    from api.routing.request_router import RequestRouter
    from api.routing.context import RoutingContext

    router = RequestRouter()
    ctx = RoutingContext(
        user_id="u-abc",
        platform="line",
        message="查詢案件進度",
    )
    decision = router.route(ctx)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Protocol

from api.routing.context import RoutingContext
from api.routing.models import RoutingDecision
from api.routing.policy_engine import PolicyEngine

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing stage protocol
# ---------------------------------------------------------------------------

class RoutingStage(Protocol):
    """Interface for pluggable routing stages.

    Each stage receives the current context and the accumulated candidates,
    and returns an updated list of candidates.  A candidate is a dict with
    at least ``skill``, ``confidence``, and ``method`` keys.
    """

    def __call__(
        self,
        ctx: RoutingContext,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ...


# ---------------------------------------------------------------------------
# Built-in stage: keyword matching
# ---------------------------------------------------------------------------

def _keyword_stage(
    ctx: RoutingContext,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Placeholder keyword matching stage.

    In production this would delegate to the keyword/alias tables defined
    in the orchestrator configuration.  Here it simply passes through any
    pre-matched skill in the context.
    """
    if ctx.matched_skill and ctx.method == "keyword":
        candidates.append({
            "skill": ctx.matched_skill,
            "confidence": ctx.confidence,
            "method": "keyword",
        })
    return candidates


def _semantic_stage(
    ctx: RoutingContext,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Placeholder semantic matching stage.

    In production this calls the embedding / semantic similarity service.
    Here it passes through any pre-matched skill tagged as semantic.
    """
    if ctx.matched_skill and ctx.method == "semantic":
        candidates.append({
            "skill": ctx.matched_skill,
            "confidence": ctx.confidence,
            "method": "semantic",
        })
    return candidates


# ---------------------------------------------------------------------------
# RequestRouter
# ---------------------------------------------------------------------------

class RequestRouter:
    """Unified request router.

    Runs an ordered list of routing stages, picks the best candidate, and
    sends it through the :class:`PolicyEngine` for a final accept/reject.

    Parameters:
        policy_engine:  Pre-configured policy engine.  If ``None``, a fresh
                        default engine is created.
        stages:         Ordered list of routing stage callables.  If ``None``,
                        the built-in keyword and semantic stages are used.
    """

    def __init__(
        self,
        *,
        policy_engine: Optional[PolicyEngine] = None,
        stages: list[RoutingStage] | None = None,
    ) -> None:
        self._engine = policy_engine or PolicyEngine()
        self._stages: list[RoutingStage] = stages if stages is not None else [
            _keyword_stage,
            _semantic_stage,
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(self, ctx: RoutingContext) -> RoutingDecision:
        """Route a request through all stages and return a decision.

        Steps:
        1. Run each stage to collect candidates.
        2. Pick the best candidate (highest confidence).
        3. Apply the policy engine for final gate.
        4. Attach full trace to the decision.
        """
        t0 = time.monotonic()
        candidates: list[dict[str, Any]] = []
        stage_trace: list[dict[str, Any]] = []

        # --- Run stages ---
        for stage in self._stages:
            stage_name = getattr(stage, "__name__", type(stage).__name__)
            try:
                candidates = stage(ctx, candidates)
                stage_trace.append({
                    "stage": stage_name,
                    "candidates_after": len(candidates),
                })
            except Exception:
                _log.exception("Routing stage %s failed", stage_name)
                stage_trace.append({
                    "stage": stage_name,
                    "error": True,
                })

        # --- Pick best candidate ---
        best = self._pick_best(candidates)

        # --- Build enriched context for policy engine ---
        if best:
            enriched = ctx.with_overrides(
                matched_skill=best["skill"],
                confidence=best["confidence"],
                method=best.get("method", ""),
            )
        else:
            enriched = ctx

        # --- Apply policy engine ---
        decision = self._engine.evaluate(enriched)

        # --- Attach routing metadata ---
        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        full_trace = list(decision.trace) + stage_trace + [{
            "router": "RequestRouter",
            "elapsed_ms": elapsed_ms,
            "total_candidates": len(candidates),
        }]

        return RoutingDecision(
            action=decision.action,
            matched=decision.matched,
            handler=decision.handler,
            confidence=decision.confidence,
            reason=decision.reason,
            intent=decision.intent,
            candidates=tuple(candidates),
            route_context=decision.route_context,
            fallback_plan=decision.fallback_plan,
            trace=tuple(full_trace),
        )

    def add_stage(self, stage: RoutingStage) -> None:
        """Append a routing stage at the end of the pipeline."""
        self._stages.append(stage)

    def insert_stage(self, index: int, stage: RoutingStage) -> None:
        """Insert a routing stage at a specific position."""
        self._stages.insert(index, stage)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_best(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return the candidate with the highest confidence, or None."""
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.get("confidence", 0.0))
