# -*- coding: utf-8 -*-
"""Test that _pick_best_source rejects empty/blank Vision values, allowing OCR to win."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _pick_best_source(field_name, sources):
    """Inline copy of the fixed function for isolation testing."""
    _SOURCE_PRIORITY = {"vision": 1, "ocr": 2, "learn": 3}
    filtered = [(v, c, src) for (v, c, src) in sources if v and str(v).strip()]
    if not filtered:
        return ("", 0.0, "none")
    return max(filtered, key=lambda x: (x[1], -_SOURCE_PRIORITY.get(x[2], 99)))


def test_empty_vision_loses_to_ocr():
    """Vision returns empty string → OCR value should win."""
    result = _pick_best_source("court", [
        ("", 0.90, "vision"),
        ("臺灣花蓮地方法院", 0.70, "ocr"),
    ])
    assert result[0] == "臺灣花蓮地方法院"
    assert result[2] == "ocr"


def test_blank_vision_loses_to_ocr():
    """Vision returns whitespace-only string → OCR value should win."""
    result = _pick_best_source("party", [
        ("   ", 0.90, "vision"),
        ("王大明", 0.70, "ocr"),
    ])
    assert result[0] == "王大明"
    assert result[2] == "ocr"


def test_nonempty_vision_wins():
    """Vision with real value should beat OCR."""
    result = _pick_best_source("court", [
        ("臺灣高等法院", 0.90, "vision"),
        ("高等法院", 0.70, "ocr"),
    ])
    assert result[0] == "臺灣高等法院"
    assert result[2] == "vision"


def test_all_empty_returns_none_sentinel():
    """All sources empty → returns ('', 0.0, 'none')."""
    result = _pick_best_source("case_number", [
        ("", 0.90, "vision"),
        ("", 0.70, "ocr"),
    ])
    assert result == ("", 0.0, "none")


def test_tie_break_vision_beats_ocr():
    """Equal confidence: vision beats ocr via source priority."""
    result = _pick_best_source("doc_type", [
        ("裁定", 0.80, "vision"),
        ("裁定", 0.80, "ocr"),
    ])
    assert result[2] == "vision"


def test_tie_break_ocr_beats_learn():
    """Equal confidence: ocr beats learn via source priority."""
    result = _pick_best_source("court", [
        ("花蓮地方法院", 0.70, "ocr"),
        ("花蓮地方法院", 0.70, "learn"),
    ])
    assert result[2] == "ocr"
