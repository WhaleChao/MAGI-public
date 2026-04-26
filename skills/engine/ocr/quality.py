# -*- coding: utf-8 -*-
"""
OCR 文字品質評分（rule-based，無 LLM）。

`compute_quality_score(text)` 評估 OCR 輸出的法律文字品質，回傳 0.0~1.0。

評分維度：
  1. 繁體中文字元密度（比例，0~0.4）
  2. 亂碼比例（替換字元、非常見字元，0~0.2 penalty）
  3. 可見 ASCII 比例（正常排版，0~0.2）
  4. 法律術語命中（加分，上限 0.2）

Python 3.9 + 3.14 相容。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Tuple

# 繁體中文常見 CJK 範圍（基本 + 擴充 A/B 的前段）
_ZH_RANGE_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

# 法律術語關鍵字（命中 1 個加 0.03，上限 0.2）
_LEGAL_TERMS = frozenset({
    "法院", "地方法院", "高等法院", "最高法院",
    "被告", "原告", "被上訴人", "上訴人", "自訴人",
    "案號", "年度", "判決", "裁定", "裁量",
    "檢察官", "辯護", "委任", "律師",
    "起訴", "聲請", "答辯", "書狀",
    "民事", "刑事", "行政", "家事",
    "第幾審", "再審", "非常上訴",
    "判處", "有期徒刑", "無罪", "不受理",
    "法扶", "法律扶助",
})

# 明顯亂碼字元樣式（替換字元、私用區等）
_GARBAGE_PATTERN = re.compile(r"[\ufffd\ue000-\uf8ff\ufffe\uffff]")


@dataclass(frozen=True)
class ScanQualityAssessment:
    effective_dpi: float
    level: str
    ok_for_ocr: bool
    recommendation: str


def assess_page_scan_quality(
    *,
    width_px: int,
    height_px: int,
    page_width_pt: float,
    page_height_pt: float,
) -> ScanQualityAssessment:
    """Assess scan resolution from image pixels and PDF page size.

    PDF points are 1/72 inch. Standard legal text should be at least 300 DPI;
    small print or weak originals should be closer to 400 DPI.
    """
    try:
        w_px = max(0, int(width_px))
        h_px = max(0, int(height_px))
        w_in = max(0.01, float(page_width_pt) / 72.0)
        h_in = max(0.01, float(page_height_pt) / 72.0)
        dpi = min(w_px / w_in, h_px / h_in)
    except Exception:
        dpi = 0.0

    if dpi >= 395:
        return ScanQualityAssessment(round(dpi, 1), "excellent", True, "400 DPI 以上，適合小字或品質較差原稿。")
    if dpi >= 295:
        return ScanQualityAssessment(round(dpi, 1), "good", True, "300 DPI 以上，適合一般法律文件 OCR。")
    if dpi >= 200:
        return ScanQualityAssessment(round(dpi, 1), "borderline", False, "低於 300 DPI，建議重新掃描；若是小字或淡色原稿請用 400 DPI。")
    return ScanQualityAssessment(round(dpi, 1), "poor", False, "低於 200 DPI，OCR 容易漏字或誤判，應重新掃描。")


def compute_quality_score(text: str) -> float:
    """評估 OCR 輸出的法律文字品質，回傳 0.0~1.0。

    空字串直接回 0.0；非常短文字（<10 chars）以寬鬆閾值處理。
    """
    if not text or not isinstance(text, str):
        return 0.0

    clean = text.strip()
    total = len(clean)
    if total == 0:
        return 0.0

    # --- 1. 繁體中文字元密度（主要信號）---
    zh_count = len(_ZH_RANGE_PATTERN.findall(clean))
    zh_ratio = zh_count / total
    zh_score = min(zh_ratio * 0.8, 0.4)   # 最高 0.4

    # --- 2. 亂碼 penalty ---
    garbage_count = len(_GARBAGE_PATTERN.findall(clean))
    garbage_ratio = garbage_count / total
    garbage_penalty = min(garbage_ratio * 2.0, 0.2)  # 最多扣 0.2

    # --- 3. 可見 ASCII 比例（正常文書含英數標點）---
    ascii_printable = sum(
        1 for c in clean if "\x20" <= c <= "\x7e"
    )
    ascii_ratio = ascii_printable / total
    # 純中文約 0.1~0.3，英數混排可達 0.5；超過 0.8 則可能不是中文文書
    if ascii_ratio > 0.8:
        ascii_score = 0.05     # 幾乎全 ASCII，可能是純英文或亂碼
    else:
        ascii_score = min(ascii_ratio * 0.4, 0.2)

    # --- 4. 法律術語命中加分 ---
    legal_bonus = 0.0
    for term in _LEGAL_TERMS:
        if term in clean:
            legal_bonus += 0.03
            if legal_bonus >= 0.2:
                break

    score = zh_score + ascii_score + legal_bonus - garbage_penalty
    return max(0.0, min(1.0, round(score, 4)))


def is_likely_legal_text(text: str, threshold: float = 0.10) -> bool:
    """快速判斷是否為法律文字（quality_score >= threshold）。

    用於 consensus 決策，不作為最終輸出依據。
    """
    return compute_quality_score(text) >= threshold


def score_pair(text_a: str, text_b: str) -> Tuple[float, float]:
    """同時評分兩段文字，回傳 (score_a, score_b)。"""
    return compute_quality_score(text_a), compute_quality_score(text_b)
