"""共用案號解析工具。

從以下檔案抽取重複邏輯：
- skills/pdf-namer/action.py   → RE_CASE_NUMBER, _extract_case_number
- skills/legal/judicial.py     → _parse_case_number
- skills/legal/laf.py          → LAF 案號 regex
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# 嚴格 ROC 案號格式：114年度訴字第123號
# ---------------------------------------------------------------------------
RE_CASE_NUMBER = re.compile(
    r"(\d{2,3})\s*年度?\s*(\S{1,6}字)\s*第?\s*(\d+)\s*號"
)

# ---------------------------------------------------------------------------
# 法扶案號格式：1141121-E-005
# ---------------------------------------------------------------------------
RE_LAF_CASE_NUMBER = re.compile(r"(\d{7}-[A-Z]-\d{3})")


def extract_case_number(text: str) -> str:
    """擷取嚴格 ROC 格式案號（如 115年度司促字第1781號）。

    原始位置：``pdf-namer/action.py``。
    """
    m = RE_CASE_NUMBER.search(text or "")
    if not m:
        return ""
    return f"{m.group(1)}年度{m.group(2)}第{m.group(3)}號"


def parse_case_number_flexible(
    case_number: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """寬鬆解析案號，支援多種格式。

    支援：
    - 114年度訴字第123號
    - 114年度宜簡字第299號
    - 114.訴.000123

    回傳 ``(year, word, number)`` 或 ``(None, None, None)``。

    原始位置：``judicial.py:_parse_case_number``。
    """
    if not case_number:
        return (None, None, None)

    # 格式1: 114年度訴字第123號
    match = re.search(r"(\d+)年度?(.+?)(?:字|)?第?(\d+)號?", case_number)
    if match:
        return match.groups()  # type: ignore[return-value]

    # 格式2: 114.訴.000123
    match = re.search(r"(\d+)[.\-](.+?)[.\-](\d+)", case_number)
    if match:
        year, word, number = match.groups()
        return (year, word, str(int(number)))

    return (None, None, None)


def extract_laf_case_number(text: str) -> str:
    """擷取法扶格式案號（如 1141121-E-005）。

    原始位置：``laf.py:1754``。
    """
    m = RE_LAF_CASE_NUMBER.search(text or "")
    return m.group(1) if m else ""
