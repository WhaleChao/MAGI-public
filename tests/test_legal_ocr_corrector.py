# -*- coding: utf-8 -*-
"""
Tests for skills.engine.ocr.legal_corrector.

禁止在 module level import api.server / api.tools_api / daemon。
"""

from __future__ import annotations

import pytest
from skills.engine.ocr.legal_corrector import correct_legal_text, CorrectionResult


class TestCaptchaBypass:
    def test_captcha_task_bypasses_all_corrections(self):
        # captcha text with l and O which would otherwise be "corrected"
        text = "l1O0aB"
        result = correct_legal_text(text, task_type="captcha")
        assert result.corrected_text == text, "captcha text must not be modified"

    def test_captcha_trace_indicates_bypass(self):
        result = correct_legal_text("anything", task_type="captcha")
        assert any("captcha" in str(entry).lower() for entry in result.correction_trace)

    def test_captcha_bypass_empty_string(self):
        result = correct_legal_text("", task_type="captcha")
        assert result.corrected_text == ""


class TestCharacterReplacement:
    def test_fullwidth_numbers_converted(self):
        result = correct_legal_text("案號：１１４年度訴字第２３號")
        assert "114" in result.corrected_text
        assert "23" in result.corrected_text
        # Fullwidth chars should be gone
        assert "１" not in result.corrected_text

    def test_replacement_char_removed(self):
        result = correct_legal_text("法院\ufffd判決")
        assert "\ufffd" not in result.corrected_text
        assert "法院" in result.corrected_text
        assert "判決" in result.corrected_text

    def test_cjk_zero_converted(self):
        result = correct_legal_text("第〇條")
        assert "〇" not in result.corrected_text
        assert "0" in result.corrected_text

    def test_simplified_湾_corrected(self):
        result = correct_legal_text("台湾高等法院")
        assert "灣" in result.corrected_text

    def test_fullwidth_parens_corrected(self):
        result = correct_legal_text("\uff08民事\uff09")
        assert "(" in result.corrected_text
        assert ")" in result.corrected_text


class TestPatternReplacement:
    def test_年度_space_removed(self):
        result = correct_legal_text("114年 度訴字第1號")
        assert "年度" in result.corrected_text

    def test_case_number_space_normalized(self):
        result = correct_legal_text("第 123 號")
        assert "第123號" in result.corrected_text

    def test_date_spacing_normalized(self):
        result = correct_legal_text("114 年 3 月 15 日")
        assert "114年3月15日" in result.corrected_text

    def test_multiple_spaces_collapsed(self):
        result = correct_legal_text("法院   判決   書")
        assert "   " not in result.corrected_text


class TestCorrectionTrace:
    def test_trace_is_list(self):
        result = correct_legal_text("法院１２３")
        assert isinstance(result.correction_trace, list)

    def test_trace_has_entries_when_corrections_made(self):
        result = correct_legal_text("案號：１１４年度訴字第２３號")
        assert len(result.correction_trace) > 0

    def test_no_corrections_on_clean_text(self):
        text = "臺灣花蓮地方法院\n114年度訴字第123號"
        result = correct_legal_text(text)
        # No corrections needed — trace may be empty or minimal
        assert isinstance(result.char_replacements, int)
        assert isinstance(result.pattern_replacements, int)

    def test_char_replacement_count_accurate(self):
        result = correct_legal_text("１２３４５")
        assert result.char_replacements >= 5  # at least 5 fullwidth digits


class TestReturnType:
    def test_returns_correction_result(self):
        result = correct_legal_text("法院")
        assert isinstance(result, CorrectionResult)
        assert hasattr(result, "corrected_text")
        assert hasattr(result, "correction_trace")
        assert hasattr(result, "char_replacements")
        assert hasattr(result, "pattern_replacements")

    def test_empty_input_returns_empty(self):
        result = correct_legal_text("")
        assert result.corrected_text == ""

    def test_none_input_handled(self):
        result = correct_legal_text(None)  # type: ignore[arg-type]
        assert result.corrected_text is not None
