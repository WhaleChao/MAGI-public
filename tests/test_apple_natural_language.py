# -*- coding: utf-8 -*-
"""Tests for skills/apple/natural_language.py — NaturalLanguage.framework."""

import pytest
from skills.apple.natural_language import (
    detect_language,
    detect_language_with_confidence,
    tokenize,
    extract_entities,
    is_chinese,
    extract_keywords,
    _detect_language_heuristic,
    _tokenize_simple,
    _extract_entities_regex,
)


class TestDetectLanguage:
    def test_chinese_traditional(self):
        lang = detect_language("臺灣臺北地方法院判決書")
        assert lang in ("zh-TW", "zh-Hant")

    def test_english(self):
        lang = detect_language("The Supreme Court ruled today.")
        assert lang == "en"

    def test_empty_string(self):
        assert detect_language("") == "unknown"

    def test_whitespace_only(self):
        assert detect_language("   ") == "unknown"


class TestDetectLanguageHeuristic:
    def test_chinese_text(self):
        lang = _detect_language_heuristic("臺灣臺北地方法院判決書被告黃語玲")
        assert lang in ("zh-TW", "zh-CN")

    def test_english_text(self):
        lang = _detect_language_heuristic("This is an English sentence for testing purposes.")
        assert lang == "en"

    def test_mixed_text(self):
        # Mostly Chinese
        lang = _detect_language_heuristic("臺灣臺北地方法院 113年度")
        assert lang in ("zh-TW", "zh-CN")


class TestDetectLanguageWithConfidence:
    def test_returns_tuple(self):
        lang, conf = detect_language_with_confidence("Hello world")
        assert isinstance(lang, str)
        assert isinstance(conf, float)
        assert 0.0 <= conf <= 1.0

    def test_empty_returns_zero_confidence(self):
        lang, conf = detect_language_with_confidence("")
        assert lang == "unknown"
        assert conf == 0.0


class TestTokenize:
    def test_chinese_text(self):
        tokens = tokenize("臺灣臺北地方法院判決書")
        assert len(tokens) > 0
        # At least some tokens should be present
        joined = "".join(tokens)
        assert "臺" in joined or "法院" in joined

    def test_empty_text(self):
        assert tokenize("") == []

    def test_whitespace(self):
        assert tokenize("   ") == []


class TestTokenizeSimple:
    def test_punctuation_split(self):
        tokens = _tokenize_simple("被告：黃語玲，原告：王小明。")
        assert "被告" in tokens
        assert "黃語玲" in tokens

    def test_space_split(self):
        tokens = _tokenize_simple("hello world test")
        assert "hello" in tokens
        assert "world" in tokens


class TestExtractEntities:
    def test_returns_dict_structure(self):
        entities = extract_entities("臺灣臺北地方法院判決書")
        assert "person" in entities
        assert "place" in entities
        assert "organization" in entities

    def test_empty_text(self):
        entities = extract_entities("")
        assert entities == {"person": [], "place": [], "organization": []}


class TestExtractEntitiesRegex:
    def test_court_name(self):
        entities = _extract_entities_regex("臺灣臺北地方法院113年度勞訴字第19號")
        assert any("臺北" in org or "法院" in org for org in entities["organization"])

    def test_place_name(self):
        entities = _extract_entities_regex("被告住所：臺北市大安區")
        assert "臺北" in entities["place"] or any("臺北" in p for p in entities["place"])

    def test_organization(self):
        entities = _extract_entities_regex("被告為永豐銀行股份有限公司")
        assert any("銀行" in org for org in entities["organization"])


class TestIsChinese:
    def test_chinese(self):
        assert is_chinese("臺灣臺北地方法院") is True

    def test_english(self):
        assert is_chinese("This is English") is False

    def test_empty(self):
        assert is_chinese("") is False


class TestExtractKeywords:
    def test_extracts_keywords(self):
        kw = extract_keywords("臺灣臺北地方法院判決書被告黃語玲應給付原告")
        assert len(kw) > 0

    def test_respects_max_limit(self):
        kw = extract_keywords("臺灣臺北地方法院判決書被告黃語玲應給付原告新臺幣壹佰萬元", max_keywords=3)
        assert len(kw) <= 3

    def test_empty_text(self):
        kw = extract_keywords("")
        assert kw == []
