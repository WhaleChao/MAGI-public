# -*- coding: utf-8 -*-
"""Tests for skills/pdf-namer/naming_validator.py — 8 patterns."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "pdf-namer"))

from naming_validator import sanitize_filename, validate_filename, validate_filename_quality


def test_valid_judgment_with_brackets():
    name = "20241015 判決（王大明；上訴駁回）.pdf"
    valid, warns = validate_filename(name)
    assert valid, warns


def test_valid_ruling_with_brackets():
    name = "20240305 裁定（李小花；應於15日內補正）.pdf"
    valid, warns = validate_filename(name)
    assert valid, warns


def test_empty_string_invalid():
    valid, warns = validate_filename("")
    assert not valid
    assert any("空字串" in w for w in warns)


def test_no_date_prefix_invalid():
    valid, warns = validate_filename("裁定（王大明）.pdf")
    assert not valid
    assert any("YYYYMMDD" in w for w in warns)


def test_wrong_separator_warns():
    name = "20241015_判決（王大明）.pdf"
    valid, warns = validate_filename(name)
    assert not valid
    assert any("空格" in w for w in warns)


def test_missing_pdf_extension_warns():
    name = "20241015 判決（王大明；主文）.doc"
    valid, warns = validate_filename(name)
    assert not valid
    assert any(".pdf" in w for w in warns)


def test_judgment_without_brackets_warns():
    name = "20241015 判決 王大明.pdf"
    valid, warns = validate_filename(name)
    assert not valid
    assert any("括號" in w for w in warns)


def test_non_bracket_type_no_bracket_required():
    """書狀/委任狀 etc. don't require brackets."""
    name = "20241015 委任狀 王大明.pdf"
    valid, warns = validate_filename(name)
    # Only check for date separator; no bracket requirement
    bracket_warns = [w for w in warns if "括號" in w]
    assert not bracket_warns


def test_quality_detects_repeated_unknown_tokens():
    ok, issues, details = validate_filename_quality(
        "20260306 找不到找不到刑事和解條件切結書（黃宥茹）.pdf",
        source_hint="20260306 和解書（黃宥茹）.pdf",
    )
    assert ok is False
    assert any("重複未知詞" in issue for issue in issues)
    assert "repeated_unknown_tokens" in details


def test_quality_detects_party_noise_fragments():
    ok, issues, details = validate_filename_quality(
        "20251218 臺灣高等法院花蓮分院114年度原上易字第30號判決（余秋菊女民國）.pdf",
        source_hint="20251218 ... 判決(余秋菊；上訴駁回).pdf",
    )
    assert ok is False
    assert any("OCR 汙染" in issue for issue in issues)
    assert "party_noise_markers" in details


def test_quality_detects_name_variant_drift():
    ok, issues, details = validate_filename_quality(
        "20250718 無償委任證明書（餘秋菊）.pdf",
        source_hint="/cases/2025-0088-余秋菊-二審-毒品危害防制條例/20250718 無償委任證明書(余秋菊)_已簽名.pdf",
    )
    assert ok is False
    assert any("姓名字形" in issue for issue in issues)
    assert details.get("name_variant_drift")


def test_sanitize_filename_restores_source_variant_and_trims_noise():
    cleaned, fixes = sanitize_filename(
        "20251218 判決（餘秋菊女民國）.pdf",
        source_hint="20251218 判決(余秋菊；上訴駁回).pdf",
    )
    assert cleaned == "20251218 判決（余秋菊）.pdf"
    assert "trim_party_noise" in fixes
