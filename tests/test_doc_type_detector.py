# -*- coding: utf-8 -*-
"""Tests for shared skills.engine.doc_type_detector."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from skills.engine.doc_type_detector import detect_doc_type


def test_detect_by_regex_judgment():
    result = detect_doc_type("臺灣高等法院刑事判決書\n主文如下")
    assert result.doc_type == "判決"
    assert result.source == "regex"
    assert result.confidence >= 0.85


def test_detect_by_regex_reply_brief():
    result = detect_doc_type("民事答辯狀\n被告答辯如下")
    assert result.doc_type == "答辯狀"
    assert result.source == "regex"


def test_empty_text_returns_default():
    result = detect_doc_type("")
    assert result.doc_type == "其他"
    assert result.source == "default"


def test_unknown_text_returns_default_without_vision(monkeypatch):
    monkeypatch.setenv("MAGI_BOOKMARKER_VISION_FALLBACK", "0")
    result = detect_doc_type("這是一段沒有任何法院文件關鍵字的普通敘述。")
    assert result.doc_type == "其他"
    assert result.source == "default"
