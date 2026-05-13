"""Tests for the Phase 4 unified routing system.

Covers:
- RoutingContext creation and immutability
- RoutingDecision creation and legacy-dict compatibility
- PolicyEngine threshold / generic-word / override rules
- InferenceRouter producing fallback plans
- RoutingTelemetry recording and reading
- RequestRouter end-to-end
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from api.routing.context import RoutingContext
from api.routing.models import FallbackPlan, RoutingDecision, ServiceTarget
from api.routing.policy_engine import PolicyEngine
from api.routing.inference_router import InferenceRouter
from api.routing.request_router import RequestRouter
from api.routing.telemetry import RoutingTelemetry
from api.routing.route_decision import build_route_decision


# =========================================================================
# RoutingContext
# =========================================================================

class TestRoutingContext:
    def test_defaults(self):
        ctx = RoutingContext()
        assert ctx.user_id == ""
        assert ctx.platform == ""
        assert ctx.role == "user"
        assert ctx.confidence == 0.0
        assert ctx.requires_admin is False
        assert len(ctx.correlation_id) == 12

    def test_creation_with_values(self):
        ctx = RoutingContext(
            user_id="u-1",
            platform="line",
            role="admin",
            message="hello",
            intent="CHAT",
            confidence=0.95,
            matched_skill="pdf-namer",
            method="semantic",
            requires_admin=True,
            attachment_type="image",
        )
        assert ctx.user_id == "u-1"
        assert ctx.platform == "line"
        assert ctx.is_admin is True
        assert ctx.has_attachment is True
        assert ctx.attachment_type == "image"

    def test_immutability(self):
        ctx = RoutingContext(user_id="u-1")
        with pytest.raises(AttributeError):
            ctx.user_id = "u-2"  # type: ignore[misc]

    def test_with_overrides(self):
        ctx = RoutingContext(user_id="u-1", platform="web")
        ctx2 = ctx.with_overrides(platform="line", confidence=0.8)
        assert ctx.platform == "web"  # original unchanged
        assert ctx2.platform == "line"
        assert ctx2.user_id == "u-1"  # carried over
        assert ctx2.confidence == 0.8

    def test_as_dict(self):
        ctx = RoutingContext(user_id="u-1", message="hi")
        d = ctx.as_dict()
        assert d["user_id"] == "u-1"
        assert d["message"] == "hi"
        assert "correlation_id" in d


# =========================================================================
# ServiceTarget / FallbackPlan
# =========================================================================

class TestServiceTarget:
    def test_creation(self):
        t = ServiceTarget(
            service_name="omlx_inference",
            model_role="text_primary",
            provider="omlx",
            model_id="gemma-4-26b",
        )
        assert t.service_name == "omlx_inference"
        assert t.priority == 0

    def test_as_dict(self):
        t = ServiceTarget(service_name="s", model_id="m")
        d = t.as_dict()
        assert d["service_name"] == "s"
        assert d["model_id"] == "m"


class TestFallbackPlan:
    def test_empty(self):
        plan = FallbackPlan()
        assert plan.primary is None
        assert plan.has_fallback is False

    def test_from_targets(self):
        t1 = ServiceTarget(service_name="a", priority=1)
        t2 = ServiceTarget(service_name="b", priority=0)
        plan = FallbackPlan.from_targets([t1, t2], reason="test")
        # sorted by priority: b (0) then a (1)
        assert plan.primary is not None
        assert plan.primary.service_name == "b"
        assert plan.has_fallback is True
        assert plan.reason == "test"

    def test_as_dict(self):
        t = ServiceTarget(service_name="x")
        plan = FallbackPlan.from_targets([t])
        d = plan.as_dict()
        assert len(d["targets"]) == 1
        assert d["max_retries"] == 2


# =========================================================================
# RoutingDecision
# =========================================================================

class TestRoutingDecision:
    def test_success_dispatch(self):
        d = RoutingDecision(action="dispatch", matched="pdf-namer", confidence=0.9)
        assert d.success is True

    def test_success_conversation(self):
        d = RoutingDecision(action="conversation", matched="chat", confidence=0.5)
        assert d.success is True

    def test_no_match(self):
        d = RoutingDecision(action="reject", matched="")
        assert d.success is False

    def test_to_legacy_dict_compatibility(self):
        """RoutingDecision.to_legacy_dict() should produce the same shape
        as build_route_decision()."""
        legacy = build_route_decision(
            action="dispatch",
            matched="pdf-namer",
            handler="pdf_handler",
            confidence=0.91,
            reason="semantic match",
            intent="CMD",
        )
        decision = RoutingDecision(
            action="dispatch",
            matched="pdf-namer",
            handler="pdf_handler",
            confidence=0.91,
            reason="semantic match",
            intent="CMD",
        )
        result = decision.to_legacy_dict()
        # Both should have the same keys
        assert set(legacy.keys()) == set(result.keys())
        assert result["matched"] == legacy["matched"]
        assert result["action"] == legacy["action"]
        assert result["confidence"] == legacy["confidence"]
        assert result["intent"] == legacy["intent"]

    def test_from_legacy_dict(self):
        legacy = build_route_decision(
            action="dispatch",
            matched="case_query",
            confidence=0.85,
            intent="QUERY",
        )
        decision = RoutingDecision.from_legacy_dict(legacy)
        assert decision.action == "dispatch"
        assert decision.matched == "case_query"
        assert decision.confidence == 0.85
        assert decision.intent == "QUERY"

    def test_as_dict_includes_trace(self):
        d = RoutingDecision(
            action="dispatch",
            matched="x",
            trace=({"stage": "test"},),
        )
        full = d.as_dict()
        assert len(full["trace"]) == 1


# =========================================================================
# PolicyEngine
# =========================================================================

class TestPolicyEngine:
    def test_dispatch_passes(self):
        engine = PolicyEngine(runtime_override_path=Path("/nonexistent"))
        ctx = RoutingContext(
            message="幫我處理這個案件的PDF文件",
            matched_skill="pdf-namer",
            confidence=0.85,
            method="semantic",
        )
        decision = engine.evaluate(ctx)
        assert decision.action == "dispatch"
        assert decision.matched == "pdf-namer"

    def test_low_confidence_rejects(self):
        engine = PolicyEngine(runtime_override_path=Path("/nonexistent"))
        ctx = RoutingContext(
            message="幫我處理這個案件的PDF文件",
            matched_skill="pdf-namer",
            confidence=0.30,
            method="semantic",
        )
        decision = engine.evaluate(ctx)
        assert decision.action == "conversation"
        assert "threshold" in decision.reason

    def test_generic_word_rejects(self):
        engine = PolicyEngine(runtime_override_path=Path("/nonexistent"))
        ctx = RoutingContext(
            message="幫我",
            matched_skill="case_query",
            confidence=0.90,
            method="semantic",
        )
        decision = engine.evaluate(ctx)
        assert decision.action == "conversation"
        assert "generic" in decision.reason

    def test_high_risk_skill_threshold(self):
        engine = PolicyEngine(runtime_override_path=Path("/nonexistent"))
        # iron_dome_scan requires 1.0 -- should never auto-dispatch
        ctx = RoutingContext(
            message="掃描系統安全漏洞並產生報告",
            matched_skill="iron_dome_scan",
            confidence=0.95,
            method="semantic",
        )
        decision = engine.evaluate(ctx)
        assert decision.action == "conversation"

    def test_no_skill_falls_to_conversation(self):
        engine = PolicyEngine(runtime_override_path=Path("/nonexistent"))
        ctx = RoutingContext(message="你好", confidence=0.5)
        decision = engine.evaluate(ctx)
        assert decision.action == "conversation"
        assert decision.reason == "no_skill_matched"

    def test_chat_intent_dampening(self):
        engine = PolicyEngine(runtime_override_path=Path("/nonexistent"))
        ctx = RoutingContext(
            message="幫我看一下案件狀態怎麼樣了",
            matched_skill="case_query",
            confidence=0.60,
            intent="CHAT",
            method="semantic",
        )
        decision = engine.evaluate(ctx)
        assert decision.action == "conversation"

    def test_runtime_override_force_skill(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"force_skill": "emergency_handler"}, f)
            f.flush()
            engine = PolicyEngine(runtime_override_path=Path(f.name))

        ctx = RoutingContext(message="anything")
        decision = engine.evaluate(ctx)
        assert decision.action == "dispatch"
        assert decision.matched == "emergency_handler"
        assert decision.reason == "runtime_override"

    def test_runtime_override_threshold(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"thresholds": {"pdf-namer": 0.99}}, f)
            f.flush()
            engine = PolicyEngine(runtime_override_path=Path(f.name))

        ctx = RoutingContext(
            message="幫我處理這個案件的PDF文件",
            matched_skill="pdf-namer",
            confidence=0.90,
            method="semantic",
        )
        decision = engine.evaluate(ctx)
        assert decision.action == "conversation"
        assert "override_threshold" in decision.reason

    def test_trace_is_populated(self):
        engine = PolicyEngine(runtime_override_path=Path("/nonexistent"))
        ctx = RoutingContext(
            message="幫我處理這個案件的PDF文件",
            matched_skill="pdf-namer",
            confidence=0.85,
            method="semantic",
        )
        decision = engine.evaluate(ctx)
        assert len(decision.trace) >= 1


# =========================================================================
# InferenceRouter
# =========================================================================

class TestInferenceRouter:
    def test_resolve_produces_plan(self):
        router = InferenceRouter()
        ctx = RoutingContext(message="test")
        plan = router.resolve(ctx, model_role="text_primary")
        assert isinstance(plan, FallbackPlan)
        assert plan.primary is not None
        assert plan.primary.model_role == "text_primary"
        assert plan.primary.model_id != ""

    def test_resolve_embedding(self):
        router = InferenceRouter()
        ctx = RoutingContext(message="embed this")
        target = router.resolve_embedding(ctx)
        assert isinstance(target, ServiceTarget)
        assert target.model_role == "embedding"

    def test_vision_attachment_prepended(self):
        router = InferenceRouter()
        ctx = RoutingContext(message="看這張圖", attachment_type="image")
        plan = router.resolve(ctx, model_role="text_primary")
        # Vision target should be first if vision model differs from primary
        if plan.primary and plan.primary.model_role == "vision":
            assert "vision" in plan.reason

    def test_fallback_plan_has_reason(self):
        router = InferenceRouter()
        ctx = RoutingContext(message="test")
        plan = router.resolve(ctx)
        assert plan.reason != ""


# =========================================================================
# RoutingTelemetry
# =========================================================================

class TestRoutingTelemetry:
    def test_record_and_summary(self):
        with tempfile.TemporaryDirectory() as td:
            tel = RoutingTelemetry(telemetry_dir=Path(td))
            decision = RoutingDecision(
                action="dispatch",
                matched="pdf-namer",
                confidence=0.9,
                route_context=RoutingContext(user_id="u-1"),
            )
            tel.record(decision)
            tel.record(decision)

            stats = tel.summary()
            assert stats["total"] == 2
            assert stats["by_action"]["dispatch"] == 2

    def test_read_all(self):
        with tempfile.TemporaryDirectory() as td:
            tel = RoutingTelemetry(telemetry_dir=Path(td))
            for i in range(5):
                tel.record(RoutingDecision(
                    action="dispatch" if i % 2 == 0 else "conversation",
                    matched=f"skill-{i}",
                ))
            entries = tel.read_all()
            assert len(entries) == 5
            # newest first
            assert entries[0]["matched"] == "skill-4"

    def test_disabled_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            tel = RoutingTelemetry(telemetry_dir=Path(td), enabled=False)
            tel.record(RoutingDecision(action="dispatch", matched="x"))
            # In-memory counter should still increment
            assert tel.summary()["total"] == 1
            # But no file should exist
            assert not (Path(td) / "routing_telemetry.jsonl").exists()

    def test_record_raw(self):
        with tempfile.TemporaryDirectory() as td:
            tel = RoutingTelemetry(telemetry_dir=Path(td))
            tel.record_raw({"action": "legacy", "skill": "old_handler"})
            entries = tel.read_all()
            assert len(entries) == 1
            assert entries[0]["action"] == "legacy"

    def test_summary_from_disk(self):
        with tempfile.TemporaryDirectory() as td:
            tel = RoutingTelemetry(telemetry_dir=Path(td))
            tel.record(RoutingDecision(action="dispatch", matched="a"))
            tel.record(RoutingDecision(action="conversation", matched=""))
            stats = tel.summary_from_disk()
            assert stats["total"] == 2
            assert stats["by_action"]["dispatch"] == 1
            assert stats["by_action"]["conversation"] == 1


# =========================================================================
# RequestRouter (end-to-end)
# =========================================================================

class TestRequestRouter:
    def test_route_with_keyword_match(self):
        router = RequestRouter(
            policy_engine=PolicyEngine(runtime_override_path=Path("/nonexistent")),
        )
        ctx = RoutingContext(
            message="幫我處理這個案件的PDF文件命名",
            matched_skill="pdf-namer",
            confidence=0.88,
            method="keyword",
        )
        decision = router.route(ctx)
        assert decision.action == "dispatch"
        assert decision.matched == "pdf-namer"
        assert len(decision.trace) >= 1

    def test_route_no_match_falls_to_conversation(self):
        router = RequestRouter(
            policy_engine=PolicyEngine(runtime_override_path=Path("/nonexistent")),
        )
        ctx = RoutingContext(message="你好嗎", confidence=0.3)
        decision = router.route(ctx)
        assert decision.action == "conversation"

    def test_route_custom_stage(self):
        def custom_stage(ctx, candidates):
            candidates.append({
                "skill": "custom_skill",
                "confidence": 0.99,
                "method": "custom",
            })
            return candidates

        router = RequestRouter(
            policy_engine=PolicyEngine(runtime_override_path=Path("/nonexistent")),
            stages=[custom_stage],
        )
        ctx = RoutingContext(message="這是一個自訂路由測試的訊息")
        decision = router.route(ctx)
        assert decision.action == "dispatch"
        assert decision.matched == "custom_skill"

    def test_trace_includes_router_metadata(self):
        router = RequestRouter(
            policy_engine=PolicyEngine(runtime_override_path=Path("/nonexistent")),
        )
        ctx = RoutingContext(
            message="幫我處理這個案件的PDF文件命名",
            matched_skill="pdf-namer",
            confidence=0.88,
            method="keyword",
        )
        decision = router.route(ctx)
        # Last trace entry should be from RequestRouter
        router_entry = [t for t in decision.trace if t.get("router") == "RequestRouter"]
        assert len(router_entry) == 1
        assert "elapsed_ms" in router_entry[0]
