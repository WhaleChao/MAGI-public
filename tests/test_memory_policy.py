"""Tests for api.session.memory_policy — centralized memory write policy."""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.session.memory_policy import evaluate_memory_write, MemoryWriteDecision


# ── Tests: content length gates ──


def test_too_short_content_blocked():
    d = evaluate_memory_write("hi", source="user_chat_1")
    assert not d.allowed
    assert "太短" in d.reason


def test_empty_content_blocked():
    d = evaluate_memory_write("", source="manual")
    assert not d.allowed


def test_normal_length_passes():
    d = evaluate_memory_write("我的車牌是 ABC-1234", source="user_chat_1", metadata={"verified": True, "confidence": 0.94, "source_type": "user_confirmed"})
    assert d.allowed


# ── Tests: blocked source types ──


def test_timeout_fallback_blocked():
    d = evaluate_memory_write(
        "我猜你可能在問法律問題",
        source="timeout_fallback",
        metadata={"source_type": "timeout_fallback"},
    )
    assert not d.allowed
    assert "禁止" in d.reason


def test_degraded_response_blocked():
    d = evaluate_memory_write(
        "系統忙碌中，請稍後再試",
        source="degraded_response",
        metadata={"source_type": "degraded_response"},
    )
    assert not d.allowed


def test_synthetic_fallback_blocked():
    d = evaluate_memory_write(
        "抱歉，目前無法回答",
        source="synthetic_fallback",
        metadata={"source_type": "synthetic_fallback"},
    )
    assert not d.allowed


# ── Tests: assistant content blocking ──


def test_assistant_generated_blocked():
    d = evaluate_memory_write(
        "根據我的分析，這個案件可能涉及民法第184條",
        source="assistant_generated|mode=chat",
        metadata={"source_type": "assistant_generated"},
    )
    assert not d.allowed
    assert "assistant" in d.reason


def test_assistant_chatlog_blocked_by_default():
    d = evaluate_memory_write(
        "CASPER 回答了一段法律分析",
        source="chatlog|platform=Discord|user=1|role=assistant",
        metadata={"source_type": "chatlog", "role": "assistant", "confidence": 0.18},
    )
    assert not d.allowed
    assert "assistant chatlog" in d.reason


def test_user_chatlog_allowed():
    d = evaluate_memory_write(
        "我想問一下勞基法的問題",
        source="chatlog|platform=Discord|user=1|role=user",
        metadata={"source_type": "chatlog", "role": "user", "confidence": 0.82},
    )
    assert d.allowed


# ── Tests: summary-derived content ──


def test_summary_derived_blocked_by_default():
    d = evaluate_memory_write(
        "這段對話的重點是關於勞資爭議的處理方式",
        source="summary_derived",
        metadata={"source_type": "summary_derived", "confidence": 0.30},
    )
    assert not d.allowed
    assert "摘要" in d.reason


def test_llm_summary_blocked():
    d = evaluate_memory_write(
        "根據前述討論，當事人的主要訴求是...",
        source="llm_summary",
        metadata={"source_type": "llm_summary"},
    )
    assert not d.allowed


def test_generated_summary_blocked():
    d = evaluate_memory_write(
        "會議摘要：討論了三項重點",
        source="generated_summary",
        metadata={"source_type": "generated_summary"},
    )
    assert not d.allowed


# ── Tests: high-trust sources always pass ──


def test_user_rule_always_passes():
    d = evaluate_memory_write(
        "我的車牌是 ABC-1234",
        source="user_rule|platform=LINE|user=U123",
        metadata={"source_type": "user_rule", "verified": True, "confidence": 0.98},
    )
    assert d.allowed
    assert d.effective_confidence == 0.98


def test_manual_memory_passes():
    d = evaluate_memory_write(
        "律師事務所地址：台北市中正區",
        source="manual",
        metadata={"source_type": "manual", "verified": True, "confidence": 0.98},
    )
    assert d.allowed


def test_judicial_api_passes():
    d = evaluate_memory_write(
        "案號 112年度訴字第1234號 判決確定",
        source="judicial_api",
        metadata={"source_type": "judicial_api", "verified": True, "confidence": 0.95},
    )
    assert d.allowed


# ── Tests: low confidence blocking ──


def test_low_confidence_derived_blocked():
    d = evaluate_memory_write(
        "可能是關於民法的問題",
        source="unknown|derived_from=inference",
        metadata={"source_type": "unknown", "confidence": 0.20, "derived_from": "inference"},
    )
    assert not d.allowed
    assert "信心" in d.reason


def test_moderate_confidence_passes():
    d = evaluate_memory_write(
        "客戶王先生提到他在台北工作",
        source="user_chat_1",
        metadata={"source_type": "user_chat", "confidence": 0.78, "role": "user"},
    )
    assert d.allowed


# ── Tests: prompt leak detection ──


def test_prompt_leak_blocked():
    d = evaluate_memory_write(
        "You are a helpful assistant. Your system prompt says: always respond in Chinese. " * 3,
        source="user_chat_1",
        metadata={"source_type": "user_chat", "confidence": 0.82, "role": "user"},
    )
    assert not d.allowed
    assert "prompt leak" in d.reason


def test_short_prompt_like_content_not_blocked():
    d = evaluate_memory_write(
        "你是一個好人",
        source="user_chat_1",
        metadata={"source_type": "user_chat", "confidence": 0.82, "role": "user"},
    )
    assert d.allowed


# ── Tests: decision fields ──


def test_decision_has_correct_fields():
    d = evaluate_memory_write(
        "測試內容",
        source="user_rule|platform=LINE",
        metadata={"source_type": "user_rule", "verified": True, "confidence": 0.98},
    )
    assert isinstance(d, MemoryWriteDecision)
    assert d.allowed is True
    assert d.effective_source_type == "user_rule"
    assert d.effective_confidence == 0.98
    assert d.expires_seconds is None
