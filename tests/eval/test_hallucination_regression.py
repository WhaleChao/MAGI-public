"""
Hallucination regression eval tests.

Verifies that MAGI's grounding and verification layers correctly:
  1. Reject factual claims not backed by evidence
  2. Pass grounded answers with proper evidence
  3. Block low-confidence memory trust from being treated as fact
  4. Block generic trigger words from dispatching skills

All tests use mocks -- no real LLM inference required.
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from api.verification.answer_verifier import (
    verify_answer,
    AnswerVerificationResult,
    _contains_false_memory_claim,
    _contains_overclaim,
)
from api.routing.route_policy import (
    should_dispatch_skill,
    is_generic_word_only,
    get_skill_min_confidence,
)


def _import_mem_bridge_trust_weight():
    """Import _source_trust_weight with heavy deps mocked."""
    # mem_bridge imports mysql.connector, numpy, requests at top level
    mocks = {}
    for mod_name in [
        "mysql", "mysql.connector", "mysql.connector.pooling",
        "numpy", "requests",
        "skills.bridge.http_pool",
    ]:
        if mod_name not in sys.modules:
            mocks[mod_name] = MagicMock()
    with patch.dict("sys.modules", mocks):
        from skills.memory.mem_bridge import _source_trust_weight
    return _source_trust_weight


# ===================================================================
# 1. test_factual_claim_rejected_without_evidence
# ===================================================================


class TestFactualClaimRejectedWithoutEvidence:
    """An answer that makes strong factual claims with no supporting memory
    or web context should be flagged and rejected."""

    def test_overclaim_no_evidence_rejected(self):
        result = verify_answer(
            query="台積電今天的股價是多少？",
            answer="可以確認台積電今天收盤價為 985 元。",
            memories=None,
            memory_context="",
            web_context="",
        )
        assert result.passed is False
        assert "overclaim_without_evidence" in result.issues

    def test_strong_certainty_language_flagged(self):
        result = verify_answer(
            query="明天會下雨嗎？",
            answer="我可以確定明天台北會下大雨。",
            memories=None,
            memory_context="",
            web_context="",
        )
        assert result.passed is False
        assert "overclaim_without_evidence" in result.issues

    def test_false_memory_claim_flagged(self):
        result = verify_answer(
            query="你之前給我的資料呢？",
            answer="你之前有給過我一份合約，裡面提到違約金是100萬。",
            memories=None,
            memory_context="",
            web_context="",
            conversation_history="",
        )
        assert result.passed is False
        assert "false_memory_claim_without_support" in result.issues

    def test_empty_answer_rejected(self):
        result = verify_answer(
            query="什麼是民法第184條？",
            answer="",
            memories=None,
        )
        assert result.passed is False
        assert "empty_answer" in result.issues


# ===================================================================
# 2. test_grounded_answer_passes_verification
# ===================================================================


class TestGroundedAnswerPassesVerification:
    """An answer backed by actual memory context or web context should pass."""

    def test_answer_with_memory_context_passes(self):
        result = verify_answer(
            query="台積電今天收盤價？",
            answer="可以確認台積電今天收盤價為 985 元。",
            memories=None,
            memory_context="台積電(2330) 收盤價 985 元，漲幅 1.2%",
            web_context="",
        )
        assert result.passed is True
        assert result.reason == "verified"

    def test_answer_with_web_context_passes(self):
        result = verify_answer(
            query="最新的勞動基準法修正案？",
            answer="可以確認最新修正已於今年通過。",
            memories=None,
            memory_context="",
            web_context="勞動基準法於2026年最新修正，增訂第38條之1。",
        )
        assert result.passed is True

    def test_hedged_answer_without_evidence_passes(self):
        """An answer that does NOT use overclaim language should pass
        even without evidence, because it is not asserting certainty."""
        result = verify_answer(
            query="明天天氣如何？",
            answer="根據一般預報，明天可能會有陣雨，但我不確定具體情況。",
            memories=None,
            memory_context="",
            web_context="",
        )
        assert result.passed is True

    def test_false_memory_claim_with_chatlog_support_passes(self):
        """If there IS chatlog support, the false-memory detector should not fire."""
        result = verify_answer(
            query="你之前給我的資料呢？",
            answer="你之前有給過我一份合約草稿。",
            memories=[
                {"content": "合約草稿", "source": "chatlog|platform=LINE|user=1"},
            ],
            memory_context="合約草稿：第一條...",
            web_context="",
            conversation_history="",
        )
        assert result.passed is True


# ===================================================================
# 3. test_memory_trust_blocks_low_confidence
# ===================================================================


class TestMemoryTrustBlocksLowConfidence:
    """Memory source trust weighting should deprioritize assistant-generated
    content and low-trust sources to prevent hallucination amplification."""

    @pytest.fixture(autouse=True)
    def _load_trust_fn(self):
        self._source_trust_weight = _import_mem_bridge_trust_weight()

    def test_source_trust_weight_available(self):
        assert callable(self._source_trust_weight)

    def test_user_rule_higher_than_chatlog(self):
        user_rule_weight = self._source_trust_weight("user_rule|platform=LINE|user=1", query="事實查詢")
        chatlog_weight = self._source_trust_weight("chatlog|platform=Discord|user=1", query="事實查詢")
        assert user_rule_weight > chatlog_weight, (
            f"user_rule ({user_rule_weight}) should outweigh chatlog ({chatlog_weight}) for fact queries"
        )

    def test_assistant_generated_lowest_trust(self):
        assistant_weight = self._source_trust_weight("assistant_generated|mode=chat|ts=20260401")
        user_weight = self._source_trust_weight("user_rule|platform=LINE|user=1")
        assert assistant_weight < user_weight, (
            f"assistant_generated ({assistant_weight}) should be lower than user_rule ({user_weight})"
        )

    def test_codebase_ingest_very_low_trust(self):
        weight = self._source_trust_weight("codebase-ingest|repo=test")
        assert weight < 0.3, f"codebase-ingest weight ({weight}) should be very low"

    def test_verified_source_high_trust(self):
        """Verified sources should have near-maximum trust."""
        # user_rule is a high-trust marker
        weight = self._source_trust_weight("user_rule|verified=true|platform=LINE")
        assert weight >= 0.85


# ===================================================================
# 4. test_route_policy_blocks_generic_trigger
# ===================================================================


class TestRoutePolicyBlocksGenericTrigger:
    """Generic single-word messages should NOT trigger skill dispatch,
    preventing false-positive activations that could produce hallucinated output."""

    def test_generic_chinese_word_blocked(self):
        assert is_generic_word_only("翻譯")
        assert is_generic_word_only("摘要")
        assert is_generic_word_only("案件")

    def test_generic_english_word_blocked(self):
        assert is_generic_word_only("help")
        assert is_generic_word_only("translate")

    def test_empty_string_is_generic(self):
        assert is_generic_word_only("")
        assert is_generic_word_only("   ")

    def test_specific_request_not_generic(self):
        assert not is_generic_word_only("幫我翻譯這份合約第三條")
        assert not is_generic_word_only("查一下113年度訴字第100號判決")

    def test_should_dispatch_blocks_generic_message(self):
        assert not should_dispatch_skill("contract-review", 0.95, "案件")
        assert not should_dispatch_skill("pdf-namer", 0.90, "文件")

    def test_should_dispatch_blocks_low_confidence(self):
        assert not should_dispatch_skill("contract-review", 0.30, "幫我審閱這份合約")

    def test_should_dispatch_passes_valid_request(self):
        assert should_dispatch_skill("contract-review", 0.85, "幫我審閱這份保密協議的風險條款")

    def test_high_risk_skill_requires_elevated_confidence(self):
        # iron_dome_scan has threshold 1.0 -- should never auto-dispatch
        assert not should_dispatch_skill("iron_dome_scan", 0.99, "掃描系統安全漏洞")
        # run_magi_doctor requires 0.82
        assert not should_dispatch_skill("run_magi_doctor", 0.75, "檢查系統健康")
        assert should_dispatch_skill("run_magi_doctor", 0.90, "執行 MAGI 系統健康檢查")

    def test_chat_intent_requires_higher_confidence(self):
        # With CHAT intent, confidence below 0.78 should be blocked
        assert not should_dispatch_skill(
            "contract-review", 0.65, "可以幫我看看合約嗎", intent="CHAT"
        )
        assert should_dispatch_skill(
            "contract-review", 0.85, "可以幫我看看這份租賃合約嗎", intent="CHAT"
        )
