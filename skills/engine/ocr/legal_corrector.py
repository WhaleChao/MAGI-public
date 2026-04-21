# -*- coding: utf-8 -*-
"""
法律文字 OCR 修正（deterministic 字元替換，無 LLM）。

只做：
  1. 常見 OCR 字元誤識修正（繁體法律文書場景）
  2. 格式正規化（全形數字、空格清理）
  3. 保留 correction_trace（可稽核哪些替換被套用）

絕對不做：
  - 呼叫任何 LLM / 外部 API
  - 套用在 captcha 文字上（captcha bypass 由 caller 保證 task_type != 'captcha'）
  - 猜測或補全被遮蓋的文字

Python 3.9 + 3.14 相容。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# 字元級替換規則（OCR 常見誤識，繁體中文法律場景）
# ---------------------------------------------------------------------------

# (wrong, correct, description)
_CHAR_RULES: List[Tuple[str, str, str]] = [
    # OCR 誤識：阿拉伯字元替換
    ("〇", "0", "zero-like CJK → 0"),
    ("Ｏ", "O", "fullwidth O → halfwidth"),
    ("Ｉ", "I", "fullwidth I → halfwidth"),
    ("１", "1", "fullwidth 1"),
    ("２", "2", "fullwidth 2"),
    ("３", "3", "fullwidth 3"),
    ("４", "4", "fullwidth 4"),
    ("５", "5", "fullwidth 5"),
    ("６", "6", "fullwidth 6"),
    ("７", "7", "fullwidth 7"),
    ("８", "8", "fullwidth 8"),
    ("９", "9", "fullwidth 9"),
    ("０", "0", "fullwidth 0"),
    # 標點正規化
    ("\uff08", "(", "fullwidth left paren"),
    ("\uff09", ")", "fullwidth right paren"),
    ("\uff0c", ",", "fullwidth comma in numeric context"),
    # 替換字元清理（亂碼）
    ("\ufffd", "", "replacement char → remove"),
    # 繁簡 OCR 混淆（常見單字）
    ("湾", "灣", "simplified 湾 → 繁 灣"),
    ("东", "東", "simplified 东 → 繁 東"),
    ("仅", "僅", "simplified 仅 → 繁 僅"),
]

# 全形英數字母 → 半形
_FULLWIDTH_RE = re.compile(r"[\uff01-\uff5e]")

def _fullwidth_to_halfwidth(c: str) -> str:
    code = ord(c)
    if 0xFF01 <= code <= 0xFF5E:
        return chr(code - 0xFEE0)
    return c


# ---------------------------------------------------------------------------
# pattern 級替換（regex，法律術語修正）
# ---------------------------------------------------------------------------

_PATTERN_RULES: List[Tuple[re.Pattern, str, str]] = [
    # 「年度」後多餘空格
    (re.compile(r"年\s+度"), "年度", "年 度 → 年度"),
    # 案號格式修復：「第 123 號」→「第123號」（數字前後空格）
    (re.compile(r"第\s+(\d+)\s+號"), r"第\1號", "第 N 號 → 第N號"),
    # 月、日前多餘空格（日期）
    (re.compile(r"(\d+)\s+年\s+(\d+)\s+月\s+(\d+)\s+日"),
     r"\1年\2月\3日", "date spacing normalization"),
    # 連續多個空格 → 單一空格
    (re.compile(r"[ \t]{2,}"), " ", "collapse multiple spaces"),
    # 行首行尾多餘空白（不移除換行）
    (re.compile(r"^[ \t]+|[ \t]+$", re.MULTILINE), "", "trim line whitespace"),
]


# ---------------------------------------------------------------------------
# 主函式
# ---------------------------------------------------------------------------

@dataclass
class CorrectionResult:
    """deterministic 修正結果。"""
    corrected_text: str
    correction_trace: List[Dict[str, str]] = field(default_factory=list)
    char_replacements: int = 0
    pattern_replacements: int = 0


def correct_legal_text(text: str, task_type: str = "legal") -> CorrectionResult:
    """套用 deterministic OCR 修正規則。

    Args:
        text: OCR 原始輸出文字。
        task_type: 若為 "captcha"，立即 bypass 並回傳原始文字，不做任何修正。

    Returns:
        CorrectionResult with corrected_text and correction_trace.
    """
    # CAPTCHA 保護：絕不修正驗證碼文字（否則 l→1, O→0 會破壞登入）
    if task_type == "captcha":
        return CorrectionResult(
            corrected_text=text,
            correction_trace=[{"bypass": "captcha task_type, no corrections applied"}],
        )

    if not text or not isinstance(text, str):
        return CorrectionResult(corrected_text=text or "")

    result = text
    trace: List[Dict[str, str]] = []
    char_count = 0
    pat_count = 0

    # --- 1. 字元級替換 ---
    for wrong, correct, desc in _CHAR_RULES:
        if wrong in result:
            count = result.count(wrong)
            result = result.replace(wrong, correct)
            trace.append({"rule": desc, "count": str(count)})
            char_count += count

    # --- 2. 全形英數 → 半形（批次處理）---
    fullwidth_replaced = 0
    new_chars = []
    for c in result:
        h = _fullwidth_to_halfwidth(c)
        if h != c:
            fullwidth_replaced += 1
        new_chars.append(h)
    if fullwidth_replaced:
        result = "".join(new_chars)
        trace.append({"rule": "fullwidth→halfwidth", "count": str(fullwidth_replaced)})
        char_count += fullwidth_replaced

    # --- 3. 替換字元移除（Unicode category Cs = surrogate, Cn = unassigned）---
    control_removed = 0
    filtered = []
    for c in result:
        cat = unicodedata.category(c)
        if cat in ("Cs", "Cn") and c != "\n" and c != "\t":
            control_removed += 1
        else:
            filtered.append(c)
    if control_removed:
        result = "".join(filtered)
        trace.append({"rule": "remove_control_chars", "count": str(control_removed)})
        char_count += control_removed

    # --- 4. Pattern 級替換 ---
    for pat, repl, desc in _PATTERN_RULES:
        new_result, n = pat.subn(repl, result)
        if n:
            result = new_result
            trace.append({"rule": desc, "count": str(n)})
            pat_count += n

    return CorrectionResult(
        corrected_text=result,
        correction_trace=trace,
        char_replacements=char_count,
        pattern_replacements=pat_count,
    )
