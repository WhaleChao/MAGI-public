"""Tests ensuring assistant-generated content never enters long-term memory.

These tests verify that the memory policy correctly blocks:
1. Direct assistant_generated writes
2. Assistant chatlog captures (default off)
3. Summary-derived content promotion to verified_fact
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.session.memory_policy import evaluate_memory_write


# ── assistant_generated must never persist ──


def test_assistant_reply_never_persisted():
    """An assistant's own answer must not enter long-term memory."""
    d = evaluate_memory_write(
        "根據民法第184條，侵權行為的構成要件包括...",
        source="assistant_generated|mode=chat|ts=20260405",
        metadata={"source_type": "assistant_generated", "confidence": 0.90},
    )
    assert not d.allowed
    assert "assistant" in d.reason.lower()


def test_assistant_chatlog_default_off():
    """Chatlog with role=assistant should be blocked by default."""
    d = evaluate_memory_write(
        "CASPER 回覆：法扶案件需要準備委任狀",
        source="chatlog|platform=LINE|user=U1|role=assistant|ts=20260405",
        metadata={"source_type": "chatlog", "role": "assistant", "confidence": 0.18},
    )
    assert not d.allowed


def test_user_chatlog_still_works():
    """User chatlog should still be persisted normally."""
    d = evaluate_memory_write(
        "我想問一下勞基法第24條加班費的計算方式",
        source="chatlog|platform=LINE|user=U1|role=user|ts=20260405",
        metadata={"source_type": "chatlog", "role": "user", "confidence": 0.82},
    )
    assert d.allowed


# ── summary_derived must never upgrade to verified_fact ──


def test_summary_cannot_become_fact():
    """Summary content must not enter long-term memory as fact."""
    d = evaluate_memory_write(
        "對話摘要：當事人主要訴求是請求給付資遣費新台幣15萬元",
        source="summary_derived|ts=20260405",
        metadata={"source_type": "summary_derived", "confidence": 0.30},
    )
    assert not d.allowed
    assert "摘要" in d.reason


def test_summary_with_high_confidence_still_blocked():
    """Even high-confidence summaries must not enter long-term memory."""
    d = evaluate_memory_write(
        "摘要：雙方合意終止勞動契約",
        source="generated_summary",
        metadata={"source_type": "generated_summary", "confidence": 0.95, "verified": True},
    )
    assert not d.allowed


def test_llm_summary_blocked():
    """LLM-generated summary must not become long-term memory."""
    d = evaluate_memory_write(
        "法院認為原告主張有理由",
        source="llm_summary|model=gemma4",
        metadata={"source_type": "llm_summary", "confidence": 0.40},
    )
    assert not d.allowed


# ── timeout/degraded fallback must never persist ──


def test_timeout_fallback_never_persisted():
    """Responses generated when the system times out must not enter memory."""
    d = evaluate_memory_write(
        "抱歉，我目前無法確認這個問題的答案，請稍後再試",
        source="timeout_fallback",
        metadata={"source_type": "timeout_fallback", "confidence": 0.10},
    )
    assert not d.allowed


def test_degraded_response_never_persisted():
    """Synthetic degraded responses must not enter memory."""
    d = evaluate_memory_write(
        "系統忙碌中，暫時無法完成推理",
        source="degraded_response",
        metadata={"source_type": "degraded_response", "confidence": 0.05},
    )
    assert not d.allowed


# ── verified user content still works ──


def test_user_explicit_remember_passes():
    """User-initiated 記住 command should always pass policy."""
    d = evaluate_memory_write(
        "我的車牌是 ABC-1234",
        source="user_chat_U123",
        metadata={"source_type": "user_confirmed", "verified": True, "confidence": 0.94, "role": "user"},
    )
    assert d.allowed
    assert d.effective_confidence == 0.94


def test_user_rule_passes():
    """User rules should always pass policy."""
    d = evaluate_memory_write(
        "繳費單通知要附 PDF",
        source="user_rule|platform=LINE|user=U123",
        metadata={"source_type": "user_rule", "verified": True, "confidence": 0.98, "role": "user"},
    )
    assert d.allowed
