# -*- coding: utf-8 -*-
"""
Tests for skills.engine.ocr.quality.

禁止在 module level import api.server / api.tools_api / daemon（SIGCHLD guard 守則）。
"""

from __future__ import annotations

import pytest
from skills.engine.ocr.quality import (
    assess_page_scan_quality,
    compute_quality_score,
    is_likely_legal_text,
    score_pair,
)


class TestComputeQualityScore:
    def test_empty_string_returns_zero(self):
        assert compute_quality_score("") == 0.0

    def test_none_returns_zero(self):
        assert compute_quality_score(None) == 0.0  # type: ignore[arg-type]

    def test_pure_ascii_low_score(self):
        score = compute_quality_score("Hello World test 123 abc")
        # Pure ASCII, no legal terms → low score
        assert 0.0 <= score < 0.35

    def test_legal_chinese_high_score(self):
        text = "臺灣花蓮地方法院\n案號：114年度訴字第123號\n原告：王大明\n被告：李小華\n判決如主文"
        score = compute_quality_score(text)
        assert score >= 0.3, f"Legal TW text should score >= 0.3, got {score}"

    def test_garbage_chars_reduce_score(self):
        clean_text = "法院判決書\n案號：114年度訴字第5號"
        garbage_text = clean_text + "\ufffd" * 50
        clean_score = compute_quality_score(clean_text)
        garbage_score = compute_quality_score(garbage_text)
        assert garbage_score < clean_score, "Garbage should reduce score"

    def test_score_bounded_0_to_1(self):
        samples = [
            "A" * 1000,
            "法院" * 100,
            "\ufffd" * 200,
            "114年度訴字第1號 判決 原告 被告 法院 委任狀",
        ]
        for text in samples:
            s = compute_quality_score(text)
            assert 0.0 <= s <= 1.0, f"Score out of bounds for {text[:30]!r}: {s}"

    def test_legal_term_bonus_applied(self):
        base = "一些中文文字沒有法律術語"
        legal = "法院 案號 判決 被告 原告 委任 律師 刑事"
        s_base = compute_quality_score(base)
        s_legal = compute_quality_score(legal)
        assert s_legal >= s_base, "Legal terms should increase score"

    def test_almost_all_ascii_low_score(self):
        text = "abc def ghi jkl mno pqr stu vwx yz 123"
        score = compute_quality_score(text)
        assert score < 0.35

    def test_mixed_zh_en_score(self):
        text = "This is Case No. 114年度訴字第99號 from Taipei District Court"
        score = compute_quality_score(text)
        assert 0.0 < score <= 1.0


class TestIsLikelyLegalText:
    def test_legal_text_detected(self):
        text = "臺灣高等法院案號：115年度刑字第5號"
        assert is_likely_legal_text(text)

    def test_empty_not_legal(self):
        assert not is_likely_legal_text("")

    def test_pure_english_not_legal(self):
        text = "hello world this is not a legal document"
        assert not is_likely_legal_text(text, threshold=0.15)

    def test_custom_threshold(self):
        text = "法院"  # minimal legal text
        # Very high threshold should fail
        assert not is_likely_legal_text(text, threshold=0.99)
        # Low threshold should pass
        assert is_likely_legal_text(text, threshold=0.01)


class TestScorePair:
    def test_returns_tuple_of_floats(self):
        a, b = score_pair("法院判決", "hello world")
        assert isinstance(a, float)
        assert isinstance(b, float)

    def test_better_text_has_higher_score(self):
        legal = "臺灣花蓮地方法院114年度訴字第123號判決書\n原告：王大明\n被告：李小華"
        garbage = "\ufffd\ufffd\ufffd\ufffd???"
        a, b = score_pair(legal, garbage)
        assert a > b


class TestScanQualityAssessment:
    def test_a4_300dpi_is_good_for_ocr(self):
        # A4 ~= 8.27 x 11.69 inch; at 300 DPI ~= 2481 x 3507 px.
        result = assess_page_scan_quality(
            width_px=2481,
            height_px=3507,
            page_width_pt=595,
            page_height_pt=842,
        )
        assert result.ok_for_ocr is True
        assert result.level == "good"

    def test_low_resolution_is_rejected(self):
        result = assess_page_scan_quality(
            width_px=1000,
            height_px=1414,
            page_width_pt=595,
            page_height_pt=842,
        )
        assert result.ok_for_ocr is False
        assert result.level in {"poor", "borderline"}
        assert "重新掃描" in result.recommendation
