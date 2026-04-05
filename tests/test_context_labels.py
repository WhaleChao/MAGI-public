"""Tests for api.session.context_labels — trust tier classification and labeling."""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.session.context_labels import (
    TRUST_TIERS,
    classify_trust_tier,
    label_single_memory,
    label_memory_context,
    build_trust_system_instruction,
)


# ── classify_trust_tier ──


def test_verified_high_confidence():
    tier = classify_trust_tier(verified=True, confidence=0.95, source_type="judicial_api")
    assert tier.name == "verified"
    assert tier.badge == "[已驗證事實]"


def test_user_rule_is_verified():
    tier = classify_trust_tier(source_type="user_rule", confidence=0.98, verified=True)
    assert tier.name == "verified"


def test_user_chat_is_user_stated():
    tier = classify_trust_tier(source_type="user_chat", role="user", confidence=0.82)
    assert tier.name == "user_stated"
    assert tier.badge == "[使用者陳述]"


def test_chatlog_user_is_user_stated():
    tier = classify_trust_tier(source_type="chatlog", role="user", confidence=0.82)
    assert tier.name == "user_stated"


def test_summary_derived_is_derived():
    tier = classify_trust_tier(source_type="summary_derived", confidence=0.18)
    assert tier.name == "derived"
    assert tier.badge == "[衍生推論]"


def test_assistant_generated_is_derived():
    tier = classify_trust_tier(source_type="assistant_generated", confidence=0.18)
    assert tier.name == "derived"


def test_derived_from_field_makes_derived():
    tier = classify_trust_tier(
        source_type="unknown", confidence=0.60, derived_from="summary"
    )
    assert tier.name == "derived"


def test_moderate_confidence_is_retrieved():
    tier = classify_trust_tier(source_type="crawler", confidence=0.72)
    assert tier.name == "retrieved"
    assert tier.badge == "[檢索線索]"


def test_low_confidence_unknown_is_derived():
    tier = classify_trust_tier(source_type="unknown", confidence=0.30)
    assert tier.name == "derived"


# ── label_single_memory ──


def test_label_verified_memory():
    labeled = label_single_memory(
        "案號確認為 112年度訴字第1234號",
        {"source_type": "judicial_api", "verified": True, "confidence": 0.95},
    )
    assert labeled.startswith("[已驗證事實]")
    assert "112年度訴字第1234號" in labeled


def test_label_derived_memory():
    labeled = label_single_memory(
        "可能涉及勞基法",
        {"source_type": "summary_derived", "confidence": 0.18},
    )
    assert labeled.startswith("[衍生推論]")


def test_label_no_provenance_defaults_to_derived():
    labeled = label_single_memory("某段記憶內容")
    assert labeled.startswith("[衍生推論]")


# ── label_memory_context ──


def test_label_memory_context_combines():
    results = [
        {
            "content": "客戶王先生住台北",
            "provenance": {"source_type": "user_chat", "role": "user", "confidence": 0.82, "verified": False},
        },
        {
            "content": "案號 112訴1234",
            "provenance": {"source_type": "judicial_api", "verified": True, "confidence": 0.95},
        },
    ]
    labeled = label_memory_context(results)
    assert "[使用者陳述]" in labeled
    assert "[已驗證事實]" in labeled
    assert "客戶王先生住台北" in labeled
    assert "案號 112訴1234" in labeled


def test_label_memory_context_empty():
    assert label_memory_context([]) == ""


def test_label_memory_context_skips_empty_content():
    results = [
        {"content": "", "provenance": {"source_type": "chatlog"}},
        {"content": "有效記憶", "provenance": {"source_type": "user_confirmed", "verified": True, "confidence": 0.94}},
    ]
    labeled = label_memory_context(results)
    assert "有效記憶" in labeled
    assert labeled.count("\n\n") == 0  # Only one entry


# ── build_trust_system_instruction ──


def test_trust_instruction_contains_all_tiers():
    instruction = build_trust_system_instruction()
    assert "[已驗證事實]" in instruction
    assert "[使用者陳述]" in instruction
    assert "[檢索線索]" in instruction
    assert "[衍生推論]" in instruction
    assert "記憶信任等級" in instruction
    assert "保留語氣" in instruction
