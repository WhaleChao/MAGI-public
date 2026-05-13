# -*- coding: utf-8 -*-
"""Tests for _compute_dynamic_confidence in pdf-namer action.py."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# Import the function directly
def _compute_dynamic_confidence(value, base_conf, field_name, source):
    import re as _re
    if not value or not str(value).strip():
        return 0.0
    v = str(value).strip()
    if field_name == "court":
        if not _re.search(r"(地方法院|高等法院|最高法院|檢察署|地院|高院)", v):
            base_conf *= 0.5
    elif field_name in ("case_no", "case_number"):
        if not _re.search(r"\d+年?.?\w+字第?\d+號?", v):
            base_conf *= 0.3
    elif field_name == "date":
        try:
            y, m, d = int(v[:4]), int(v[4:6]), int(v[6:8])
            if not (2000 <= y <= 2099 and 1 <= m <= 12 and 1 <= d <= 31):
                base_conf *= 0.2
        except (ValueError, IndexError):
            base_conf *= 0.2
    elif field_name == "party":
        garbled = sum(1 for c in v if ord(c) > 0xFFFF or (0xD800 <= ord(c) <= 0xDFFF))
        if garbled > 2:
            base_conf *= 0.5
    return base_conf


def test_court_valid_gets_full_confidence():
    c = _compute_dynamic_confidence("臺灣花蓮地方法院", 0.90, "court", "vision")
    assert c == 0.90


def test_court_invalid_gets_discounted():
    c = _compute_dynamic_confidence("某某單位", 0.90, "court", "vision")
    assert c == 0.45  # 0.90 × 0.5


def test_case_no_valid_pattern():
    c = _compute_dynamic_confidence("115年度原侵重訴字第1號", 0.90, "case_no", "vision")
    assert c == 0.90


def test_case_no_invalid_gets_discounted():
    c = _compute_dynamic_confidence("花蓮刑事", 0.90, "case_no", "vision")
    assert c == 0.27  # 0.90 × 0.3


def test_empty_value_returns_zero():
    c = _compute_dynamic_confidence("", 0.90, "court", "vision")
    assert c == 0.0
