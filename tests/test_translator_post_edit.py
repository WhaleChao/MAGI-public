"""Tests for APE (Apple Translation + LLM Post-Editing) pipeline.

Covers:
  - _post_edit_validator.validate_post_edit — all rejection reasons
  - _apple_post_edit.is_legal_text — heuristic routing
  - _apple_post_edit.translate_with_ape — pipeline flow (mocked Apple + LLM)
  - translate_core routing — APE opted-in/out correctly
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from skills.translator._post_edit_validator import validate_post_edit


def test_valid_clean_zh_to_en():
    src = "原告訴之聲明：被告應給付原告新臺幣200,000元整。"
    baseline = "The plaintiff's original statement: The defendant shall pay the plaintiff NT$200,000 in full."
    edited = "Prayer for relief: The defendant shall pay the plaintiff NT$200,000."
    r = validate_post_edit(src, baseline, edited, target_lang="en")
    assert r["valid"] is True, r


def test_flags_missing_number():
    src = "被告應給付原告新臺幣200,000元整。"
    baseline = "The defendant shall pay NT$200,000."
    edited = "The defendant shall pay the plaintiff."
    r = validate_post_edit(src, baseline, edited, target_lang="en")
    assert "numbers_missing" in r["reasons"]


def test_flags_runaway_length():
    src = "原告訴之聲明：被告應給付原告新臺幣200,000元整。"
    baseline = "The defendant shall pay the plaintiff NT$200,000."
    edited = "Pay."
    r = validate_post_edit(src, baseline, edited, target_lang="en")
    assert "length_delta_too_high" in r["reasons"]


def test_flags_simplified_chinese_when_target_hant():
    # "明天开庭" contains 开 (SC) instead of 開 (TC)
    r = validate_post_edit("明天開庭", "明天開庭", "明天开庭", target_lang="zh-Hant")
    assert "simplified_chinese_detected" in r["reasons"]
    assert "开" in r["stats"]["simplified_chars"]


def test_simplified_check_skipped_for_english_target():
    r = validate_post_edit("明天開庭", "trial tomorrow", "the trial is held tomorrow",
                           target_lang="en")
    assert "simplified_chinese_detected" not in r["reasons"]


def test_preserves_case_number_from_source():
    src = "本院114年度原訴字第000024號案審理中"
    baseline = "Case 114-Yuan-Su-24 is pending"
    edited_ok = "Case 114年度原訴字第000024號 is pending before this court"
    r_ok = validate_post_edit(src, baseline, edited_ok, target_lang="en")
    assert "case_numbers_missing" not in r_ok["reasons"]
    edited_bad = "The case is pending"
    r_bad = validate_post_edit(src, baseline, edited_bad, target_lang="en")
    assert "case_numbers_missing" in r_bad["reasons"]


def test_detects_repetition_runaway():
    src = "A"
    baseline = "pay the plaintiff"
    edited = "pay pay pay pay pay pay pay pay pay pay pay"
    r = validate_post_edit(src, baseline, edited, target_lang="en")
    assert "repetition_runaway" in r["reasons"]


def test_empty_edit_rejected():
    r = validate_post_edit("source", "baseline", "", target_lang="en")
    assert r["valid"] is False
    assert "edited_empty" in r["reasons"]


def test_short_baseline_gets_relaxed_threshold():
    # Short baseline where a small absolute rewrite would otherwise blow
    # past the 0.20 default delta.
    r = validate_post_edit(
        "明天開庭",
        "trial tomorrow",  # 14 chars
        "the hearing is tomorrow",  # 23 chars: delta 0.64 but abs swing 9
        target_lang="en",
    )
    # The test here is that short baselines don't instantly flag —
    # we accept that 9-char swing on a 14-char baseline is OK via the
    # short-text floor (effective_max = max(default, 12/14) = 0.857).
    assert "length_delta_too_high" not in r["reasons"]


# ── is_legal_text heuristic ──────────────────────────────────────────────────

def test_is_legal_text_detects_prayer_for_relief():
    from skills.translator._apple_post_edit import is_legal_text
    assert is_legal_text("原告訴之聲明：被告應給付原告新臺幣200,000元整。")


def test_is_legal_text_detects_case_number():
    from skills.translator._apple_post_edit import is_legal_text
    assert is_legal_text("本院114年度原訴字第000024號案現正審理中。")


def test_is_legal_text_detects_english_legal():
    from skills.translator._apple_post_edit import is_legal_text
    assert is_legal_text("The plaintiff moves the court for a prayer for relief pursuant to Article 184.")


def test_is_legal_text_rejects_casual():
    from skills.translator._apple_post_edit import is_legal_text
    assert not is_legal_text("今天天氣很好，我們去吃個飯吧。")


def test_is_legal_text_empty():
    from skills.translator._apple_post_edit import is_legal_text
    assert not is_legal_text("")


# ── translate_with_ape pipeline ──────────────────────────────────────────────

def _make_apple_success(text="The defendant shall pay NT$200,000."):
    return {"success": True, "text": text, "provider": "apple_translation", "elapsed_ms": 50}


def test_translate_with_ape_success_path():
    """Happy path: Apple succeeds, LLM polishes, validator accepts.

    Baseline ~60 chars; polished replaces 'statement' → 'prayer for relief'
    keeping total length within ±35%, so validator should accept.
    """
    from skills.translator._apple_post_edit import translate_with_ape
    baseline = "The plaintiff's statement: the defendant shall pay NT$200,000."  # 62 chars
    polished = "Prayer for relief: the defendant shall pay NT$200,000."          # 53 chars — ~14% shorter, within threshold
    with patch("skills.translator._apple_post_edit._apple_translate",
               return_value=_make_apple_success(baseline)):
        with patch("skills.translator._apple_post_edit._run_local_llm", return_value=polished):
            r = translate_with_ape(
                "原告訴之聲明：被告應給付原告新臺幣200,000元整。",
                source_lang="zh-Hant", target_lang="en",
            )
    assert r["success"] is True
    assert "prayer for relief" in r["text"].lower()
    assert r["provider"] == "apple_translation_ape"
    assert r["degraded"] is False


def test_translate_with_ape_apple_failure_returns_error():
    """Apple translation fails → return error dict, not success."""
    from skills.translator._apple_post_edit import translate_with_ape
    fail = {"success": False, "text": "", "error": "sidecar_binary_missing", "stderr": None}
    with patch("skills.translator._apple_post_edit._apple_translate", return_value=fail):
        r = translate_with_ape("原告訴之聲明", source_lang="zh-Hant", target_lang="en")
    assert r["success"] is False
    assert "apple" in r["provider"]


def test_translate_with_ape_llm_unavailable_returns_baseline():
    """LLM returns empty string → fall back to Apple baseline."""
    from skills.translator._apple_post_edit import translate_with_ape
    with patch("skills.translator._apple_post_edit._apple_translate",
               return_value=_make_apple_success("The defendant shall pay.")):
        with patch("skills.translator._apple_post_edit._run_local_llm", return_value=""):
            r = translate_with_ape("被告應給付", source_lang="zh-Hant", target_lang="en")
    assert r["success"] is True
    assert r["text"] == "The defendant shall pay."
    assert r["provider"] == "apple_translation_baseline"
    assert r["degraded"] is True
    assert r.get("reason") == "llm_unavailable"


def test_translate_with_ape_post_edit_rejected_falls_back_to_baseline():
    """Validator rejects the LLM edit (numbers missing) → return baseline."""
    from skills.translator._apple_post_edit import translate_with_ape
    baseline = "The defendant shall pay NT$200,000."
    # LLM drops the number → validator should reject
    bad_edit = "The defendant shall pay the plaintiff."
    with patch("skills.translator._apple_post_edit._apple_translate",
               return_value=_make_apple_success(baseline)):
        with patch("skills.translator._apple_post_edit._run_local_llm", return_value=bad_edit):
            r = translate_with_ape(
                "被告應給付原告新臺幣200,000元整。",
                source_lang="zh-Hant", target_lang="en",
            )
    assert r["success"] is True
    assert r["text"] == baseline
    assert r["provider"] == "apple_translation_baseline"
    assert r["degraded"] is True
    assert r.get("reason") == "post_edit_rejected"
    assert r.get("rejected_edit") == bad_edit


def test_translate_with_ape_simplified_chinese_rejected():
    """LLM outputs simplified Chinese when target is zh-Hant → validator rejects."""
    from skills.translator._apple_post_edit import translate_with_ape
    baseline_hant = "被告應給付原告新臺幣二十萬元。"
    sc_edit = "被告应给付原告新台币二十万元。"  # simplified
    apple_zh = {"success": True, "text": baseline_hant, "provider": "apple_translation", "elapsed_ms": 50}
    with patch("skills.translator._apple_post_edit._apple_translate", return_value=apple_zh):
        with patch("skills.translator._apple_post_edit._run_local_llm", return_value=sc_edit):
            r = translate_with_ape(
                "被告應給付原告新臺幣二十萬元。",
                source_lang="zh-Hant", target_lang="zh-Hant",
            )
    assert r["success"] is True
    # Should fall back to baseline because SC detected
    assert r["provider"] == "apple_translation_baseline"
    assert r["degraded"] is True


# ── translate_core APE routing ───────────────────────────────────────────────

def test_translate_core_ape_skipped_when_disabled():
    """MAGI_TRANSLATOR_APE=0 must skip APE even for legal text."""
    import os
    from skills.translator.action import translate_core
    with patch.dict(os.environ, {"MAGI_TRANSLATOR_APE": "0",
                                  "MAGI_TRANSLATOR_STABLE_PRIMARY": "0"}):
        with patch("skills.translator._apple_post_edit.translate_with_ape") as mock_ape:
            # translate_core will go to subprocess / GTX path; APE must not be called
            try:
                translate_core("明天開庭", target_lang="en")
            except Exception:
                pass  # subprocess may fail in test env; that's ok
            mock_ape.assert_not_called()


def test_translate_core_ape_skipped_for_non_legal_text():
    """Even with MAGI_TRANSLATOR_APE=1, non-legal text skips APE."""
    import os
    from skills.translator.action import translate_core
    with patch.dict(os.environ, {"MAGI_TRANSLATOR_APE": "1"}):
        with patch("skills.engine.apple_translation.is_available", return_value=(True, "")):
            with patch("skills.translator._apple_post_edit.is_legal_text", return_value=False) as mock_il:
                with patch("skills.translator._apple_post_edit.translate_with_ape") as mock_ape:
                    try:
                        translate_core("今天天氣很好。", target_lang="en")
                    except Exception:
                        pass
                    mock_ape.assert_not_called()


def test_translate_core_ape_used_for_legal_text_when_apple_available():
    """With APE=1, Apple available, legal text → translate_with_ape called."""
    import os
    from skills.translator.action import translate_core
    sentinel = {
        "success": True, "text": "prayer for relief", "provider": "apple_translation_ape",
        "degraded": False, "elapsed_ms": 500, "baseline": "statement of lawsuit",
        "validator": {"valid": True, "reasons": [], "stats": {}},
    }
    legal = "原告訴之聲明：被告應給付原告新臺幣200,000元整。"
    with patch.dict(os.environ, {"MAGI_TRANSLATOR_APE": "1",
                                  "MAGI_TRANSLATOR_STABLE_PRIMARY": "0",
                                  "MAGI_TRANSLATOR_APE_MAX_CHARS": "5000"}):
        with patch("skills.engine.apple_translation.is_available", return_value=(True, "")):
            with patch("skills.translator._apple_post_edit.is_legal_text", return_value=True):
                with patch("skills.translator._apple_post_edit.translate_with_ape",
                           return_value=sentinel) as mock_ape:
                    result = translate_core(legal, target_lang="en",
                                           source_lang="zh-Hant")
    mock_ape.assert_called_once()
    assert result.get("success") is True
    assert result.get("provider") == "apple_translation_ape"
