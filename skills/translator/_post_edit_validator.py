"""
Post-edit validator for APE (Automatic Post-Editing) translation pipeline.

Given (source, baseline, edited) triples, decides whether `edited` should be
trusted or rejected in favour of `baseline`. Rejection reasons are reported
back so callers can surface them for debugging.

Designed to be pure and dependency-free — safe to call in inner loops.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set

# --- Tunables (env-overridable via caller) ----------------------------------

DEFAULT_LENGTH_DELTA_MAX = 0.35  # |len(edited) - len(baseline)| / len(baseline)
# Cross-language APE legitimately rewrites 20-30% of chars (legal-term
# substitution, clause re-ordering). Only flag runaway length changes.
DEFAULT_REPETITION_MAX = 3       # max consecutive identical short-phrase repetitions

# Simplified-Chinese-only characters. Reuses the curated frozenset from
# ensemble_inference to keep a single source of truth. Lazy-loaded to avoid
# import cycles at module load time.
_SIMPLIFIED_ONLY_CHARS: Set[str] = set()

def _load_sc_chars() -> Set[str]:
    global _SIMPLIFIED_ONLY_CHARS
    if _SIMPLIFIED_ONLY_CHARS:
        return _SIMPLIFIED_ONLY_CHARS
    try:
        from skills.bridge.ensemble_inference import _SC_LEGAL_CHARS  # type: ignore
        _SIMPLIFIED_ONLY_CHARS = set(_SC_LEGAL_CHARS)
    except Exception:
        # Fallback minimal set if ensemble module unavailable.
        _SIMPLIFIED_ONLY_CHARS = set(
            "损权责证诉处规进对认时问说来过义务类协签举书审长会还为从发开关应现给让边单实续区动结请们"
        )
    return _SIMPLIFIED_ONLY_CHARS

# Number / money / percentage pattern. Digits with optional commas and
# optional decimal fraction; trailing punctuation not captured.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Capture TW currency + amount tokens ("新臺幣", "NT$200,000", "20萬元") so validator
# can track legal amounts specifically.
_AMOUNT_RE = re.compile(r"(NT\$[\d,]+|新臺幣[\d,]+元?|\d+萬元|\d+[,\d]*元)")

# Case / docket numbers common in TW judgments ("114年度原訴字第000024號").
_CASE_NUMBER_RE = re.compile(r"\d+年度\S+?字第\d+號")

# Very simple proper-noun extraction from source (CJK only): sequences flanked
# by 原告/被告/上訴人/被上訴人/告訴人 etc. before the name.
_PARTY_PREFIX_RE = re.compile(
    r"(?:原告|被告|上訴人|被上訴人|告訴人|聲請人|相對人|抗告人)"
    r"([\u4e00-\u9fffA-Za-z]{2,6})"
)


def _extract_numbers(text: str) -> List[str]:
    return _NUMBER_RE.findall(text or "")


def _extract_case_numbers(text: str) -> List[str]:
    return _CASE_NUMBER_RE.findall(text or "")


def _extract_parties(text: str) -> List[str]:
    return _PARTY_PREFIX_RE.findall(text or "")


def _detect_simplified_chinese(text: str) -> List[str]:
    """Return up to 8 simplified-only characters found in text."""
    sc_chars = _load_sc_chars()
    hits: List[str] = []
    for ch in text or "":
        if ch in sc_chars and ch not in hits:
            hits.append(ch)
            if len(hits) >= 8:
                break
    return hits


def _detect_repetition(text: str, max_run: int) -> bool:
    """
    Detect runaway repetition (LLM crash symptom): same 4-char window repeats
    more than `max_run` times in a row. Returns True if repetition detected.
    """
    if not text or max_run <= 0:
        return False
    window = 4
    if len(text) < window * (max_run + 1):
        return False
    for i in range(len(text) - window * (max_run + 1)):
        token = text[i:i + window]
        if not token.strip():
            continue
        run = 1
        j = i + window
        while j + window <= len(text) and text[j:j + window] == token:
            run += 1
            j += window
            if run > max_run:
                return True
    return False


def validate_post_edit(
    source: str,
    baseline: str,
    edited: str,
    target_lang: str = "en",
    length_delta_max: float = DEFAULT_LENGTH_DELTA_MAX,
    repetition_max: int = DEFAULT_REPETITION_MAX,
) -> Dict[str, object]:
    """
    Decide whether `edited` is acceptable as a post-edit of `baseline`.

    Returns:
      {
        "valid": bool,
        "reasons": [ "length_delta_too_high", "numbers_missing", ... ],
        "stats": {
            "baseline_len": int,
            "edited_len": int,
            "length_delta": float,
            "baseline_numbers": [str],
            "missing_numbers": [str],
            "source_case_numbers": [str],
            "missing_case_numbers": [str],
            "source_parties": [str],
            "missing_parties": [str],
            "simplified_chars": [str],
        },
      }
    """
    reasons: List[str] = []
    stats: Dict[str, object] = {}

    # --- empty / trivially-bad edits -------------------------------------------------
    if not edited or not edited.strip():
        return {
            "valid": False,
            "reasons": ["edited_empty"],
            "stats": {
                "baseline_len": len(baseline or ""),
                "edited_len": 0,
            },
        }

    baseline_len = max(len(baseline or ""), 1)
    edited_len = len(edited)
    length_delta = abs(edited_len - baseline_len) / baseline_len
    stats["baseline_len"] = baseline_len
    stats["edited_len"] = edited_len
    stats["length_delta"] = round(length_delta, 3)

    # Short baselines swing hard on small absolute edits (e.g. 50ch baseline,
    # 10ch legal-term rewrite = 0.20 delta). Relax threshold so a <=12-char
    # absolute swing on baselines <80 chars is not flagged by itself.
    effective_max = length_delta_max
    if baseline_len < 80:
        effective_max = max(length_delta_max, 12.0 / baseline_len)
    if length_delta > effective_max:
        reasons.append("length_delta_too_high")

    # --- numbers preserved ---------------------------------------------------------
    baseline_numbers = _extract_numbers(baseline)
    edited_numbers = _extract_numbers(edited)
    missing_numbers = [n for n in baseline_numbers if n not in edited_numbers]
    stats["baseline_numbers"] = baseline_numbers
    stats["missing_numbers"] = missing_numbers
    if missing_numbers:
        reasons.append("numbers_missing")

    # --- case numbers preserved (pull from SOURCE, not baseline, since Apple may
    # swap format) -----------------------------------------------------------------
    source_case_numbers = _extract_case_numbers(source)
    missing_case_numbers = [
        cn for cn in source_case_numbers if cn not in edited
    ]
    stats["source_case_numbers"] = source_case_numbers
    stats["missing_case_numbers"] = missing_case_numbers
    if missing_case_numbers:
        reasons.append("case_numbers_missing")

    # --- party names preserved (source-based, works for zh-Hant → en where the
    # LLM should keep the name romanized or copied) --------------------------------
    source_parties = _extract_parties(source)
    missing_parties: List[str] = []
    for name in source_parties:
        if name in edited:
            continue
        # For zh → non-zh, the name is acceptable if a plausible transliteration
        # appears; we can't verify transliteration cheaply, so only flag if the
        # target is Chinese (transliteration not required).
        if target_lang.startswith("zh"):
            missing_parties.append(name)
    stats["source_parties"] = source_parties
    stats["missing_parties"] = missing_parties
    if missing_parties:
        reasons.append("parties_missing")

    # --- simplified-chinese guard (only when target is zh-Hant) --------------------
    if target_lang.startswith("zh-Hant") or target_lang in ("繁體中文", "繁中"):
        simplified_chars = _detect_simplified_chinese(edited)
        stats["simplified_chars"] = simplified_chars
        if simplified_chars:
            reasons.append("simplified_chinese_detected")
    else:
        stats["simplified_chars"] = []

    # --- repetition (LLM runaway) ---------------------------------------------------
    if _detect_repetition(edited, repetition_max):
        reasons.append("repetition_runaway")

    return {
        "valid": not reasons,
        "reasons": reasons,
        "stats": stats,
    }


__all__ = ["validate_post_edit"]
