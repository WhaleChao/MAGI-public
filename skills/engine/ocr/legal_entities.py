# -*- coding: utf-8 -*-
"""
法律實體抽取（OCR 專用，純 deterministic regex，無 LLM）。

優先 import shared_utils 的已驗證 regex；
不足再補 OCR-specific pattern（如 OCR 常見字元誤識修正後的格式）。

Python 3.9 + 3.14 相容。
"""

from __future__ import annotations

import re
from typing import List

from skills.engine.ocr.ocr_schema import OCREntities

# --- 引用共用工具（已驗證的 regex）--------------------------------------------

try:
    from skills.bridge.shared_utils.case_number_utils import (
        RE_CASE_NUMBER,
        RE_LAF_CASE_NUMBER,
        extract_case_number,
        extract_laf_case_number,
    )
    _SHARED_CASE_UTILS = True
except ImportError:
    _SHARED_CASE_UTILS = False
    # Fallback：在 shared_utils 不可用時（如獨立測試環境）自行定義
    RE_CASE_NUMBER = re.compile(r"(\d{2,3})\s*年度?\s*(\S{1,6}字)\s*第?\s*(\d+)\s*號")
    RE_LAF_CASE_NUMBER = re.compile(r"(\d{7}-[A-Z]-\d{3})")

    def extract_case_number(text: str) -> str:
        m = RE_CASE_NUMBER.search(text or "")
        if not m:
            return ""
        return f"{m.group(1)}年度{m.group(2)}第{m.group(3)}號"

    def extract_laf_case_number(text: str) -> str:
        m = RE_LAF_CASE_NUMBER.search(text or "")
        return m.group(1) if m else ""

try:
    from skills.bridge.shared_utils.court_utils import extract_court_name
    _SHARED_COURT_UTILS = True
except ImportError:
    _SHARED_COURT_UTILS = False

    def extract_court_name(text: str) -> str:  # type: ignore[misc]
        m = re.search(
            r"((?:臺灣|台灣)?[\u4e00-\u9fff]{2,8}(?:地方)?法院)",
            text or ""
        )
        return m.group(1) if m else ""

# --- OCR 專用補充 regex -------------------------------------------------------

# ROC 日期（多種 OCR 輸出格式）
_RE_ROC_DATE = re.compile(
    r"(?:中華民國\s*)?(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)

# 寬鬆版案號：允許 OCR 常見誤識後的格式（如「114年 訴字第123號」多餘空格）
_RE_CASE_NUMBER_LOOSE = re.compile(
    r"(\d{2,3})\s*年\s*度?\s*(\S{1,8}字)\s*第\s*(\d+)\s*號"
)

# 當事人姓名候選（2~4 個中文字，preceded by 原告/被告/自訴人/辯護人等角色）
_RE_PARTY = re.compile(
    r"(?:原告|被告|自訴人|辯護人|代理人|委任人|申請人|聲請人|相對人)\s*[:：]\s*([\u4e00-\u9fff]{2,5})"
)

# 多案號掃描（findall 回傳所有匹配）
_RE_CASE_ALL = re.compile(
    r"(\d{2,3})\s*年度?\s*(\S{1,8}字)\s*第?\s*(\d+)\s*號"
)

_RE_LAF_ALL = re.compile(r"\d{7}-[A-Z]-\d{3}")


def extract_entities(text: str) -> OCREntities:
    """從 OCR 文字抽取法律實體。

    所有操作為 deterministic regex，無 LLM 呼叫。
    """
    if not text or not isinstance(text, str):
        return OCREntities()

    # --- 案號（嚴格 + 寬鬆兩輪）---
    case_set: set = set()
    for m in _RE_CASE_ALL.finditer(text):
        normalized = f"{m.group(1)}年度{m.group(2)}第{m.group(3)}號"
        case_set.add(normalized)
    case_numbers = sorted(case_set)

    # --- 法扶案號 ---
    laf_set = set(m.group(0) for m in _RE_LAF_ALL.finditer(text))
    laf_case_numbers = sorted(laf_set)

    # --- ROC 日期 ---
    date_set: set = set()
    for m in _RE_ROC_DATE.finditer(text):
        normalized_date = f"{m.group(1)}年{m.group(2)}月{m.group(3)}日"
        date_set.add(normalized_date)
    roc_dates = sorted(date_set)

    # --- 法院 ---
    court = extract_court_name(text)
    courts = [court] if court else []

    # --- 當事人 ---
    parties = list(dict.fromkeys(m.group(1) for m in _RE_PARTY.finditer(text)))

    return OCREntities(
        case_numbers=case_numbers,
        roc_dates=roc_dates,
        courts=courts,
        parties=parties,
        laf_case_numbers=laf_case_numbers,
    )


def extract_all_case_numbers(text: str) -> List[str]:
    """快速擷取所有案號字串（不含 entity 完整物件）。"""
    return list(set(
        f"{m.group(1)}年度{m.group(2)}第{m.group(3)}號"
        for m in _RE_CASE_ALL.finditer(text or "")
    ))
