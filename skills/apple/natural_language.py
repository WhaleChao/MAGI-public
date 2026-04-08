# -*- coding: utf-8 -*-
"""
natural_language.py
===================
macOS NaturalLanguage.framework 輕量 NLP 模組。

提供語言偵測、中文分詞、命名實體辨識（NER），完全不消耗 GPU。
用於分流簡單文字處理任務，減少 LLM 呼叫次數。

整合點：
- pipelines/message_pipeline.py：訊息進來時先做語言偵測和實體擷取
- skills/ops/spotlight_search.py：搜尋前用分詞改善搜尋品質
- 路由決策：NL framework 能處理的不送 LLM
"""
from __future__ import annotations

import logging
import platform
import subprocess
from typing import Optional

logger = logging.getLogger("NaturalLanguage")

# ---------------------------------------------------------------------------
# PyObjC NaturalLanguage framework (macOS 12+)
# ---------------------------------------------------------------------------
_NL_AVAILABLE = False
_NL = None

if platform.system() == "Darwin":
    try:
        import NaturalLanguage as _NL_mod
        _NL = _NL_mod
        _NL_AVAILABLE = True
    except ImportError:
        logger.info("NaturalLanguage framework not available — install pyobjc-framework-NaturalLanguage")
    except Exception as e:
        logger.debug("NaturalLanguage import error: %s", e)

# Language code mapping
_LANG_MAP = {
    "zh-Hans": "zh-CN",
    "zh-Hant": "zh-TW",
    "ja": "ja",
    "ko": "ko",
    "en": "en",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "pt": "pt",
    "vi": "vi",
    "th": "th",
    "id": "id",
}

# NER tag mapping
_NER_TAG_MAP = {}


def is_available() -> bool:
    """Check if NaturalLanguage framework is available."""
    return _NL_AVAILABLE


# ---------------------------------------------------------------------------
# 語言偵測
# ---------------------------------------------------------------------------

def detect_language(text: str) -> str:
    """
    語言偵測，零 GPU 消耗。

    Args:
        text: 輸入文字

    Returns:
        語言代碼（如 "zh-TW", "en", "ja"），未知回傳 "unknown"
    """
    if not text or not text.strip():
        return "unknown"

    if _NL_AVAILABLE:
        return _detect_language_native(text)
    return _detect_language_heuristic(text)


def _detect_language_native(text: str) -> str:
    """使用 NaturalLanguage.framework 偵測語言。"""
    try:
        recognizer = _NL.NLLanguageRecognizer.alloc().init()
        recognizer.processString_(text)
        lang = recognizer.dominantLanguage()
        if lang:
            lang_str = str(lang)
            return _LANG_MAP.get(lang_str, lang_str)
        return "unknown"
    except Exception as e:
        logger.debug("NL language detection failed: %s", e)
        return _detect_language_heuristic(text)


def _detect_language_heuristic(text: str) -> str:
    """簡易啟發式語言偵測（fallback）。"""
    # Count character types
    cjk = 0
    latin = 0
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            cjk += 1
        elif 0x0041 <= cp <= 0x007A:
            latin += 1

    total = len(text.replace(" ", ""))
    if total == 0:
        return "unknown"

    cjk_ratio = cjk / total
    if cjk_ratio > 0.3:
        # Distinguish Traditional vs Simplified Chinese
        # Traditional-specific characters sampling
        trad_chars = set("的個這們點經過說對樣裡實開關還進體點讓義個經過對給話進過問")
        simp_chars = set("个这们经过说对样里实开关还进体点让义个经过对给话进过问")
        text_chars = set(text)
        trad_hits = len(text_chars & trad_chars)
        simp_hits = len(text_chars & simp_chars)
        if trad_hits >= simp_hits:
            return "zh-TW"
        return "zh-CN"
    elif latin / max(total, 1) > 0.5:
        return "en"
    return "unknown"


def detect_language_with_confidence(text: str) -> tuple[str, float]:
    """
    語言偵測含信心度。

    Returns:
        (language_code, confidence) — confidence 0.0~1.0
    """
    if not text or not text.strip():
        return ("unknown", 0.0)

    if _NL_AVAILABLE:
        try:
            recognizer = _NL.NLLanguageRecognizer.alloc().init()
            recognizer.processString_(text)
            hypotheses = recognizer.languageHypothesesWithMaximum_(3)
            if hypotheses:
                # hypotheses is NSDictionary {NLLanguage: NSNumber}
                best_lang = None
                best_conf = 0.0
                for lang_key in hypotheses:
                    conf = float(hypotheses[lang_key])
                    if conf > best_conf:
                        best_conf = conf
                        best_lang = str(lang_key)
                if best_lang:
                    mapped = _LANG_MAP.get(best_lang, best_lang)
                    return (mapped, best_conf)
        except Exception as e:
            logger.debug("NL confidence detection failed: %s", e)

    lang = _detect_language_heuristic(text)
    return (lang, 0.7 if lang != "unknown" else 0.0)


# ---------------------------------------------------------------------------
# 中文分詞
# ---------------------------------------------------------------------------

def tokenize(text: str, language: str = "zh-Hant") -> list[str]:
    """
    中文分詞，用於搜尋前處理。零 GPU 消耗。

    Args:
        text: 輸入文字
        language: 語言提示（預設繁體中文）

    Returns:
        分詞結果列表
    """
    if not text or not text.strip():
        return []

    if _NL_AVAILABLE:
        return _tokenize_native(text)
    return _tokenize_simple(text)


def _tokenize_native(text: str) -> list[str]:
    """使用 NaturalLanguage.framework 分詞。"""
    try:
        tokenizer = _NL.NLTokenizer.alloc().initWithUnit_(_NL.NLTokenUnitWord)
        tokenizer.setString_(text)

        tokens = []
        ns_range = _NL.NSRange(0, len(text))

        def _callback(token_range, attrs, stop):
            loc = token_range.location
            length = token_range.length
            token = text[loc:loc + length]
            if token.strip():
                tokens.append(token)

        tokenizer.enumerateTokensInRange_usingBlock_(ns_range, _callback)
        return tokens
    except Exception as e:
        logger.debug("NL tokenization failed: %s", e)
        return _tokenize_simple(text)


def _tokenize_simple(text: str) -> list[str]:
    """簡易分詞（fallback）：按空白和標點切割。"""
    import re
    # Split on whitespace and common CJK punctuation
    tokens = re.split(r'[\s，。、；：！？「」『』（）\[\]【】\(\)]+', text)
    return [t for t in tokens if t.strip()]


# ---------------------------------------------------------------------------
# 命名實體辨識 (NER)
# ---------------------------------------------------------------------------

def extract_entities(text: str) -> dict[str, list[str]]:
    """
    命名實體辨識（NER）。零 GPU 消耗。

    辨識：人名 (person)、地名 (place)、組織名 (organization)。
    用途：案件中自動標記當事人、法院名稱、地址。

    Args:
        text: 輸入文字

    Returns:
        {"person": [...], "place": [...], "organization": [...]}
    """
    entities: dict[str, list[str]] = {
        "person": [],
        "place": [],
        "organization": [],
    }

    if not text or not text.strip():
        return entities

    if _NL_AVAILABLE:
        return _extract_entities_native(text)
    return _extract_entities_regex(text)


def _extract_entities_native(text: str) -> dict[str, list[str]]:
    """使用 NaturalLanguage.framework NER。"""
    entities: dict[str, list[str]] = {
        "person": [],
        "place": [],
        "organization": [],
    }

    try:
        tagger = _NL.NLTagger.alloc().initWithTagSchemes_([
            _NL.NLTagSchemeNameType
        ])
        tagger.setString_(text)

        options = (_NL.NLTaggerOmitWhitespace | _NL.NLTaggerOmitPunctuation)
        ns_range = _NL.NSRange(0, len(text))

        def _callback(tag, token_range, stop):
            if tag is None:
                return
            tag_str = str(tag)
            loc = token_range.location
            length = token_range.length
            token = text[loc:loc + length]

            if "PersonalName" in tag_str:
                if token not in entities["person"]:
                    entities["person"].append(token)
            elif "PlaceName" in tag_str:
                if token not in entities["place"]:
                    entities["place"].append(token)
            elif "OrganizationName" in tag_str:
                if token not in entities["organization"]:
                    entities["organization"].append(token)

        tagger.enumerateTagsInRange_unit_scheme_options_usingBlock_(
            ns_range,
            _NL.NLTokenUnitWord,
            _NL.NLTagSchemeNameType,
            options,
            _callback,
        )
        return entities
    except Exception as e:
        logger.debug("NL NER failed: %s", e)
        return _extract_entities_regex(text)


def _extract_entities_regex(text: str) -> dict[str, list[str]]:
    """簡易 regex NER（fallback）。"""
    import re

    entities: dict[str, list[str]] = {
        "person": [],
        "place": [],
        "organization": [],
    }

    # 法院名稱
    courts = re.findall(r'(?:臺灣|台灣)?[\u4e00-\u9fff]{2,4}(?:地方|高等|最高)(?:法院|行政法院)', text)
    entities["organization"].extend(list(set(courts)))

    # 常見組織後綴
    orgs = re.findall(r'[\u4e00-\u9fff]{2,8}(?:公司|事務所|基金會|協會|委員會|銀行)', text)
    entities["organization"].extend([o for o in set(orgs) if o not in entities["organization"]])

    # 地名（縣市）
    places = re.findall(r'(?:臺北|台北|新北|桃園|臺中|台中|臺南|台南|高雄|基隆|新竹|嘉義|屏東|宜蘭|花蓮|臺東|台東|澎湖|金門|連江|苗栗|彰化|南投|雲林)(?:市|縣)?', text)
    entities["place"] = list(set(places))

    return entities


# ---------------------------------------------------------------------------
# 便捷函式
# ---------------------------------------------------------------------------

def is_chinese(text: str) -> bool:
    """快速判斷文字是否為中文。"""
    lang = detect_language(text)
    return lang in ("zh-TW", "zh-CN", "zh-Hant", "zh-Hans")


def extract_keywords(text: str, max_keywords: int = 10) -> list[str]:
    """
    擷取關鍵詞（分詞 + 去停用詞）。

    用於搜尋查詢預處理。
    """
    _STOP_WORDS = {
        "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
        "一個", "上", "也", "很", "到", "說", "要", "去", "你", "會", "著",
        "沒有", "看", "好", "自己", "這", "他", "她", "它", "們",
        "那", "裡", "之", "與", "及", "或", "等", "被", "從", "把", "對",
    }

    tokens = tokenize(text)
    keywords = []
    seen = set()
    for t in tokens:
        if t in _STOP_WORDS or len(t) < 2:
            continue
        if t not in seen:
            seen.add(t)
            keywords.append(t)
        if len(keywords) >= max_keywords:
            break
    return keywords


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    test_texts = [
        "���灣臺北地方法院113年度勞訴字第19號判決書，被告黃語玲應給付原告新臺幣壹佰萬元。",
        "The Supreme Court of the United States ruled today on the case.",
        "東京地方裁判所は本日、判決を下した。",
    ]

    print(f"NaturalLanguage.framework available: {_NL_AVAILABLE}\n")

    for text in test_texts:
        print(f"Text: {text[:50]}...")
        lang, conf = detect_language_with_confidence(text)
        print(f"  Language: {lang} (confidence: {conf:.2f})")
        tokens = tokenize(text)
        print(f"  Tokens: {tokens[:10]}")
        entities = extract_entities(text)
        print(f"  Entities: {entities}")
        keywords = extract_keywords(text)
        print(f"  Keywords: {keywords}")
        print()
