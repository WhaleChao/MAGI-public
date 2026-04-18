# -*- coding: utf-8 -*-
"""Tests for skills/pdf-namer/naming_validator.py — 8 patterns."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "pdf-namer"))

from naming_validator import validate_filename


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
