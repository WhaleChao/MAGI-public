# -*- coding: utf-8 -*-
"""
pdf-namer / naming_validator.py
================================
Format guard for generated PDF filenames.
Called by generate_name_proposal() before returning; adds warnings but does NOT block.
"""
import re
from typing import List, Tuple

# Document types that require bracket supplemental info
_TYPES_REQUIRING_BRACKETS = frozenset([
    "判決", "裁定", "函文", "函", "庭通知書", "起訴書",
    "不起訴處分書", "聲請書", "再議聲請書",
])

_DATE_RE = re.compile(r"^\d{8}")
_BRACKET_RE = re.compile(r"[（(].+[）)]")


def validate_filename(name: str) -> Tuple[bool, List[str]]:
    """Validate a proposed PDF filename against naming rules.

    Rules:
      1. Must not be empty
      2. Must start with 8-digit YYYYMMDD
      3. Character after date must be a single space (not underscore/dash)
      4. Extension must be .pdf (case-insensitive)
      5. Judgment/ruling/letter types must have bracket supplemental info

    Returns:
        (is_valid, warnings) — warnings is empty when is_valid is True.
        Non-blocking: caller logs warnings but still returns the filename.
    """
    warnings: List[str] = []

    if not name or not name.strip():
        return False, ["檔名不得為空字串"]

    stem = name
    if name.lower().endswith(".pdf"):
        stem = name[:-4]
    else:
        warnings.append("副檔名不是 .pdf")

    if not _DATE_RE.match(stem):
        warnings.append("檔名未以 8 位西元日期 (YYYYMMDD) 開頭")
    else:
        date_part = stem[:8]
        try:
            y, m, d = int(date_part[:4]), int(date_part[4:6]), int(date_part[6:8])
            if not (2000 <= y <= 2099 and 1 <= m <= 12 and 1 <= d <= 31):
                warnings.append(f"日期 {date_part} 超出合法範圍")
        except ValueError:
            warnings.append(f"日期 {date_part} 無法解析")

        if len(stem) > 8 and stem[8] != " ":
            sep = repr(stem[8])
            warnings.append(f"日期後分隔符應為空格，實際為 {sep}")

    # Check for required bracket info in judgment/ruling/letter types
    for doc_type in _TYPES_REQUIRING_BRACKETS:
        if doc_type in stem:
            if not _BRACKET_RE.search(stem):
                warnings.append(
                    f"文件類型「{doc_type}」應包含括號補充資訊，例如（當事人；主文摘要）"
                )
            break

    is_valid = len(warnings) == 0
    return is_valid, warnings
