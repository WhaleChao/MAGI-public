# -*- coding: utf-8 -*-
"""Tests for skills/pdf-bookmarker/bookmark_validator.py."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills", "pdf-bookmarker")))

from bookmark_validator import validate_bookmark


def test_valid_roc_prefixed_bookmark():
    ok, warns = validate_bookmark("113.04.15 判決")
    assert ok, warns


def test_valid_ad_prefixed_bookmark():
    ok, warns = validate_bookmark("20240415 起訴書")
    assert ok, warns


def test_unknown_type_warns():
    ok, warns = validate_bookmark("113.04.15 神秘文件")
    assert not ok
    assert any("已知" in w for w in warns)


def test_group_count_must_be_at_least_two():
    ok, warns = validate_bookmark("送達證書（共 1 份）")
    assert not ok
    assert any(">= 2" in w for w in warns)
