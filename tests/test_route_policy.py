"""Tests for api.routing.route_policy — centralized routing policy."""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.routing.route_policy import (
    get_skill_min_confidence,
    is_generic_word_only,
    should_cache_intent,
    should_dispatch_skill,
    build_route_explanation,
)


# ── get_skill_min_confidence ──


def test_high_risk_skill_elevated_threshold():
    assert get_skill_min_confidence("run_magi_doctor") == 0.82
    assert get_skill_min_confidence("calendar") == 0.78
    assert get_skill_min_confidence("laf_pending") == 0.80
    assert get_skill_min_confidence("iron_dome_scan") == 1.0


def test_normal_skill_default_threshold():
    assert get_skill_min_confidence("pdf-namer") == 0.55
    assert get_skill_min_confidence("contract-review") == 0.55
    assert get_skill_min_confidence("worldmonitor-intel") == 0.55


# ── is_generic_word_only ──


def test_single_generic_word():
    assert is_generic_word_only("翻譯")
    assert is_generic_word_only("摘要")
    assert is_generic_word_only("記得")
    assert is_generic_word_only("案件")


def test_multiple_generic_words():
    assert is_generic_word_only("請問")
    assert is_generic_word_only("什麼")


def test_specific_content_not_generic():
    assert not is_generic_word_only("幫我翻譯這份合約的第三條")
    assert not is_generic_word_only("查一下112年度訴字第1234號判決")


def test_empty_is_generic():
    assert is_generic_word_only("")
    assert is_generic_word_only("  ")


def test_english_generic():
    assert is_generic_word_only("translate")
    assert is_generic_word_only("help")


def test_long_message_never_generic():
    assert not is_generic_word_only("這是一段超過十五個字元的訊息內容")


# ── should_dispatch_skill ──


def test_high_confidence_dispatch():
    assert should_dispatch_skill("pdf-namer", 0.80, "幫我命名這份PDF判決書")


def test_low_confidence_blocked():
    assert not should_dispatch_skill("pdf-namer", 0.40, "幫我命名這份PDF判決書")


def test_high_risk_skill_needs_elevated_confidence():
    assert not should_dispatch_skill("run_magi_doctor", 0.60, "系統檢查一下")
    assert should_dispatch_skill("run_magi_doctor", 0.85, "系統檢查一下")


def test_generic_word_blocks_dispatch():
    assert not should_dispatch_skill("pdf-namer", 0.90, "翻譯")
    assert not should_dispatch_skill("contract-review", 0.85, "案件")


def test_chat_intent_needs_higher_confidence():
    assert not should_dispatch_skill("pdf-namer", 0.60, "幫我命名PDF", intent="CHAT")
    assert should_dispatch_skill("pdf-namer", 0.80, "幫我命名PDF", intent="CHAT")


def test_cmd_intent_normal_threshold():
    assert should_dispatch_skill("pdf-namer", 0.60, "幫我命名PDF", intent="CMD")


def test_iron_dome_never_dispatches():
    assert not should_dispatch_skill("iron_dome_scan", 0.99, "掃描系統安全")


# ── should_cache_intent ──


def test_cache_high_confidence_chat():
    assert should_cache_intent("CHAT", 0.90)


def test_cache_high_confidence_query():
    assert should_cache_intent("QUERY", 0.90)


def test_no_cache_low_confidence():
    assert not should_cache_intent("CHAT", 0.55)
    assert not should_cache_intent("QUERY", 0.60)


def test_no_cache_cmd():
    assert not should_cache_intent("CMD", 0.95)


def test_no_cache_danger():
    assert not should_cache_intent("DANGER", 0.99)


# ── build_route_explanation ──


def test_route_explanation_structure():
    exp = build_route_explanation(
        skill_name="pdf-namer",
        confidence=0.82,
        dispatched=True,
        reason="DIRECT tier match",
        intent="CMD",
        method="embedding",
    )
    assert exp["skill"] == "pdf-namer"
    assert exp["dispatched"] is True
    assert exp["confidence"] == 0.82
    assert exp["min_required"] == 0.55


def test_route_explanation_high_risk():
    exp = build_route_explanation(
        skill_name="run_magi_doctor",
        confidence=0.60,
        dispatched=False,
        reason="below threshold",
        intent="CHAT",
    )
    assert exp["dispatched"] is False
    assert exp["min_required"] == 0.82
