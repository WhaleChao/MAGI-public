# -*- coding: utf-8 -*-
"""
Multi-language → zh-Hant translation bridge for research-brief.

Priority:
    1. Source already zh-Hant/zh-Hans → pass through (no translation)
    2. Apple Translation sidecar (ja/ko/de/fr/es/en/ru/ar/th/vi/it/pt/nl/pl/tr/...)
    3. NIM 405B fallback (degraded=true, Plan A)
    4. Give up → return original with degraded flag

Contract:
    translate_to_zh_hant(text, source_lang) -> {"text": str, "provider": str,
                                                 "degraded": bool, "error": Optional[str]}
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, Optional

logger = logging.getLogger("research-brief.translator")

_APPLE_LANGS = {
    "en", "ja", "ko", "de", "fr", "es", "it", "pt", "ru", "ar",
    "th", "vi", "id", "ms", "hi", "tr", "nl", "pl", "uk",
}

_ZH_FAMILY = {"zh", "zh-Hant", "zh-Hans", "zh-TW", "zh-CN", "cmn", "yue"}


def _normalize_lang(lang: Optional[str]) -> str:
    if not lang:
        return ""
    v = lang.strip().replace("_", "-")
    low = v.lower()
    if low.startswith("zh-hant") or low in ("zh-tw", "zh_tw"):
        return "zh-Hant"
    if low.startswith("zh-hans") or low in ("zh-cn", "zh_cn"):
        return "zh-Hans"
    if low == "zh":
        return "zh-Hant"  # Default TW-preference
    # Keep first 2 chars for family
    if "-" in v:
        return v.split("-", 1)[0].lower()
    return low


def _detect_lang(text: str) -> str:
    """Tiny heuristic when source lang is unknown."""
    if not text:
        return "en"
    # CJK Unified Ideographs → Chinese (assume zh-Hant for now; user is TW)
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    kana = len(re.findall(r"[\u3040-\u309f\u30a0-\u30ff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    total = max(len(text), 1)
    if kana / total > 0.02:
        return "ja"
    if hangul / total > 0.02:
        return "ko"
    if han / total > 0.15:
        return "zh-Hant"
    # Rough Latin/Cyrillic detection
    if re.search(r"[\u0400-\u04ff]", text):
        return "ru"
    if re.search(r"[ßäöüÄÖÜ]", text):
        return "de"
    if re.search(r"[àâçéèêëîïôûùüÿ]", text, re.IGNORECASE):
        return "fr"
    return "en"


def translate_to_zh_hant(text: str, source_lang: Optional[str] = None) -> Dict[str, object]:
    """
    Translate `text` to zh-Hant. Returns {"text", "provider", "degraded", "error", "source_lang"}.
    Safe: never raises on normal failures.
    """
    if not text or not text.strip():
        return {"text": "", "provider": "noop", "degraded": False,
                "error": None, "source_lang": source_lang or ""}

    src = _normalize_lang(source_lang) or _detect_lang(text)

    # zh family → no translation
    if src in _ZH_FAMILY or src == "zh-Hant" or src == "zh-Hans":
        return {"text": text, "provider": "passthrough", "degraded": False,
                "error": None, "source_lang": src}

    # 1. Apple Translation
    if src in _APPLE_LANGS or src == "en":
        try:
            from skills.engine.apple_translation import translate as _apple_translate
            r = _apple_translate(text, source_lang=src, target_lang="zh-Hant",
                                 timeout_sec=12.0)
            if r.get("success") and r.get("text"):
                return {"text": str(r["text"]), "provider": "apple_translation",
                        "degraded": False, "error": None, "source_lang": src}
            else:
                logger.debug("apple_translation failed src=%s err=%s",
                             src, r.get("error"))
        except Exception as e:
            logger.debug("apple_translation exception src=%s: %s", src, e)

    # 2. NIM 405B fallback
    try:
        from skills.bridge.nim_heavy import run_nim_chat
        prompt = (f"Translate the following {src} text to Traditional Chinese (繁體中文). "
                  f"Output ONLY the translation, no preamble, no explanation.\n\n{text}")
        r = run_nim_chat(prompt, heavy=False, max_tokens=1024)
        if isinstance(r, dict) and r.get("text"):
            out = str(r["text"]).strip()
            # Strip common preamble even though we told it not to
            out = re.sub(r"^(Translation:|繁體中文：|中文：|譯文：)\s*", "", out)
            if out:
                return {"text": out, "provider": "nim_fallback",
                        "degraded": True, "error": None, "source_lang": src}
    except Exception as e:
        logger.debug("NIM fallback exception src=%s: %s", src, e)

    # 3. Give up
    return {"text": text, "provider": "none", "degraded": True,
            "error": "all_providers_failed", "source_lang": src}
