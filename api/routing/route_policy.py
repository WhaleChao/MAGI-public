"""Centralized routing policy.

Defines confidence thresholds, high-risk skill gates, and generic-word
detection to prevent false-positive skill dispatch.

Usage::

    from api.routing.route_policy import (
        should_dispatch_skill,
        is_generic_word_only,
        get_skill_min_confidence,
    )

    if not should_dispatch_skill(skill_name, score, message):
        # fall back to conversational
        ...
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# High-risk skills: require elevated confidence to trigger
# ---------------------------------------------------------------------------

_HIGH_RISK_SKILLS: dict[str, float] = {
    "run_magi_doctor": 0.82,
    "magi-doctor": 0.82,
    "skill_interview": 0.80,
    "doctor": 0.82,
    "calendar": 0.78,
    "manage_meetings": 0.78,
    "laf_pending": 0.80,
    "laf-go-live": 0.80,
    "iron_dome_scan": 1.0,   # never auto-dispatch
    "drop_table": 1.0,       # never auto-dispatch
    "system_test": 0.85,
    "admin_panel": 0.85,
}

# Default minimum confidence for any skill dispatch
_DEFAULT_MIN_CONFIDENCE = 0.55

# ---------------------------------------------------------------------------
# Generic words that should NOT alone trigger a skill
# ---------------------------------------------------------------------------

_GENERIC_WORDS = frozenset({
    # Chinese
    "記得", "記憶", "摘要", "翻譯", "案件", "文件", "報告", "查詢",
    "幫我", "我要", "可以", "請問", "什麼", "怎麼", "為什麼",
    "這個", "那個", "一下", "看看", "好嗎", "嗎",
    # English
    "remember", "summary", "translate", "case", "document", "report",
    "query", "help", "please", "what", "how", "why",
})

# Pattern: message is <= 2 meaningful tokens and all are generic
_SHORT_MSG_THRESHOLD = 15  # chars


def get_skill_min_confidence(skill_name: str) -> float:
    """Return the minimum confidence required to auto-dispatch *skill_name*."""
    return _HIGH_RISK_SKILLS.get(skill_name, _DEFAULT_MIN_CONFIDENCE)


def is_generic_word_only(message: str) -> bool:
    """Check if *message* consists only of generic words that should not trigger skills.

    Returns True if the message is short and contains only generic vocabulary.
    """
    text = (message or "").strip()
    if not text:
        return True
    if len(text) > _SHORT_MSG_THRESHOLD:
        return False

    # Tokenize: split on whitespace and CJK character boundaries
    tokens = _tokenize(text)
    if not tokens:
        return True

    meaningful = [t for t in tokens if t not in _GENERIC_WORDS and len(t) > 1]
    return len(meaningful) == 0


def should_dispatch_skill(
    skill_name: str,
    confidence: float,
    message: str,
    *,
    intent: str = "",
    method: str = "",
) -> bool:
    """Decide whether to dispatch *skill_name* given the routing confidence.

    Returns False if:
    - Confidence is below the skill's minimum threshold
    - The message consists only of generic words
    - The intent is CHAT and confidence is below the CHAT override threshold
    """
    min_conf = get_skill_min_confidence(skill_name)

    # Generic word check: short generic messages should never trigger skills
    if is_generic_word_only(message):
        return False

    # Confidence gate
    if confidence < min_conf:
        return False

    # CHAT intent requires higher confidence to override
    if intent == "CHAT" and confidence < 0.78:
        return False

    return True


def build_route_explanation(
    *,
    skill_name: str,
    confidence: float,
    dispatched: bool,
    reason: str,
    intent: str = "",
    method: str = "",
    alternatives: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a structured route explanation for debugging and tracing."""
    return {
        "skill": skill_name,
        "confidence": round(confidence, 4),
        "dispatched": dispatched,
        "reason": reason,
        "intent": intent,
        "method": method,
        "min_required": get_skill_min_confidence(skill_name),
        "alternatives": alternatives or [],
    }


# ---------------------------------------------------------------------------
# Intent cache policy
# ---------------------------------------------------------------------------

_CACHE_MIN_CONFIDENCE = 0.85


def should_cache_intent(intent: str, confidence: float) -> bool:
    """Return True if this intent classification is confident enough to cache.

    Low-confidence classifications should not pollute the cache to prevent
    persistent misrouting.
    """
    if confidence < _CACHE_MIN_CONFIDENCE:
        return False
    # Only cache safe intents
    if intent in {"CHAT", "QUERY"}:
        return True
    # CMD and DANGER are context-dependent; don't cache
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CJK_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f]"
)


def _tokenize(text: str) -> list[str]:
    """Simple tokenizer for mixed CJK/Latin text."""
    tokens: list[str] = []
    # Split Latin words
    latin_parts = _CJK_PATTERN.sub(" ", text).split()
    tokens.extend(p.lower().strip() for p in latin_parts if p.strip())
    # Extract CJK bigrams
    cjk_chars = _CJK_PATTERN.findall(text)
    for i in range(len(cjk_chars)):
        tokens.append(cjk_chars[i])
        if i + 1 < len(cjk_chars):
            tokens.append(cjk_chars[i] + cjk_chars[i + 1])
    return tokens
