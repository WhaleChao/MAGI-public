"""共用文字正規化工具。

從以下檔案抽取重複邏輯：
- skills/legal/laf.py             → normalize_spaces
- skills/documents/pdf_bridge.py  → normalize_spaces, normalize_segment_fragment
- skills/judicial-web-search/action.py → clean_text
- skills/research/web_research.py → strip_zero_width
- skills/iron-dome/core.py        → strip_zero_width
"""

from __future__ import annotations

import re

# -- zero-width / BOM 字元 ---------------------------------------------------
_ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\ufeff"


def normalize_spaces(s: str, strip_chars: str = "") -> str:
    """摺疊連續空白為單一空格，可選擇額外 strip 指定字元。

    相容 laf.py ``_normalize_spaces`` 與 pdf_bridge.py 行為。
    """
    result = re.sub(r"\s+", " ", (s or "").strip())
    if strip_chars:
        result = result.strip(strip_chars)
    return result


def normalize_segment_fragment(text: str) -> str:
    """去除編號、符號、多餘空白，回傳乾淨的文字片段。

    原始位置：``pdf_bridge.py:966``。
    """
    frag = re.sub(r"\s+", " ", str(text or "")).strip(" -：:;，,。")
    frag = re.sub(r"^[\-•]+\s*", "", frag).strip()
    frag = re.sub(
        r"^(?:[0-9]+[.)、]\s*|[A-Z][.)]\s*|[（(]?[一二三四五六七八九十0-9]+[）).、]\s*)",
        "",
        frag,
    ).strip()
    return frag


def clean_text(s: str) -> str:
    """統一換行符並摺疊過多空行。

    原始位置：``judicial-web-search/action.py:76``。
    """
    if not s:
        return ""
    s = re.sub(r"\r\n?", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def strip_zero_width(s: str) -> str:
    """移除零寬字元 (ZWSP / ZWNJ / ZWJ / BOM)。

    原始位置：``web_research.py:46`` / ``iron-dome/core.py:22``。
    """
    if not s:
        return s or ""
    table = {ord(ch): None for ch in _ZERO_WIDTH_CHARS}
    return s.translate(table)


def normalize_court_char(s: str) -> str:
    """臺/台 統一：將常見的「台」字地名替換為「臺」。"""
    if not s:
        return s or ""
    return (
        s.replace("台灣", "臺灣")
        .replace("台中", "臺中")
        .replace("台南", "臺南")
        .replace("台東", "臺東")
        .replace("台北", "臺北")
    )
