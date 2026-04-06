"""共用法院名稱映射與查詢工具。

從以下檔案抽取重複邏輯：
- skills/legal/judicial.py      → COURT_OPTIONS, SIMPLE_COURT_MAPPING, get_court_code
- skills/pdf-namer/action.py    → RE_COURT_NAME, _extract_court_name
"""

from __future__ import annotations

import re
from typing import Optional

from skills.bridge.shared_utils.text_utils import normalize_court_char

# ---------------------------------------------------------------------------
# 法院全名 → 代碼
# ---------------------------------------------------------------------------
COURT_OPTIONS: dict[str, str] = {
    # 最高法院
    "最高法院": "TPS",
    # 最高行政法院
    "最高行政法院": "TPA",
    # 高等法院
    "臺灣高等法院": "TPH",
    "臺灣高等法院臺中分院": "TCH",
    "臺灣高等法院臺南分院": "TNH",
    "臺灣高等法院高雄分院": "KSH",
    "臺灣高等法院花蓮分院": "HLH",
    # 高等行政法院
    "臺北高等行政法院": "TPAA",
    "臺中高等行政法院": "TCAA",
    "高雄高等行政法院": "KSAA",
    # 專業法院
    "智慧財產及商業法院": "IPC",
    "臺灣高雄少年及家事法院": "KJF",
    # 地方法院
    "臺灣臺北地方法院": "TPD",
    "臺灣士林地方法院": "SLD",
    "臺灣新北地方法院": "PCD",
    "臺灣桃園地方法院": "TYD",
    "臺灣新竹地方法院": "SCD",
    "臺灣苗栗地方法院": "MLD",
    "臺灣臺中地方法院": "TCD",
    "臺灣南投地方法院": "NTD",
    "臺灣彰化地方法院": "CHD",
    "臺灣雲林地方法院": "ULD",
    "臺灣嘉義地方法院": "CYD",
    "臺灣臺南地方法院": "TND",
    "臺灣高雄地方法院": "KSD",
    "臺灣橋頭地方法院": "CTD",
    "臺灣屏東地方法院": "PTD",
    "臺灣臺東地方法院": "TTD",
    "臺灣花蓮地方法院": "HLD",
    "臺灣宜蘭地方法院": "ILD",
    "臺灣基隆地方法院": "KLD",
    "臺灣澎湖地方法院": "PHD",
    "福建金門地方法院": "KMD",
    "福建連江地方法院": "LCD",
}

# ---------------------------------------------------------------------------
# 簡易庭代碼 → (全名, 代碼)
# ---------------------------------------------------------------------------
SIMPLE_COURT_MAPPING: dict[str, tuple[str, str]] = {
    # 宜蘭地院
    "宜簡": ("宜蘭簡易庭", "ILS"),
    "羅簡": ("羅東簡易庭", "LTS"),
    # 新北地院
    "板簡": ("板橋簡易庭", "PCS"),
    "三簡": ("三重簡易庭", "SJS"),
    # 臺北地院
    "北簡": ("臺北簡易庭", "TPS"),
    # 桃園地院
    "桃簡": ("桃園簡易庭", "TYS"),
    "壢簡": ("中壢簡易庭", "CLS"),
    # 新竹地院
    "竹簡": ("新竹簡易庭", "SCS"),
    "竹北簡": ("竹北簡易庭", "CBS"),
    # 苗栗地院
    "苗簡": ("苗栗簡易庭", "MLS"),
    # 臺中地院
    "中簡": ("臺中簡易庭", "TCS"),
    "沙簡": ("沙鹿簡易庭", "SLS"),
    "豐簡": ("豐原簡易庭", "FYS"),
    # 彰化地院
    "彰簡": ("彰化簡易庭", "CHS"),
    "員簡": ("員林簡易庭", "YLS"),
    # 南投地院
    "投簡": ("南投簡易庭", "NTS"),
    "埔簡": ("埔里簡易庭", "PLS"),
    # 雲林地院
    "雲簡": ("斗六簡易庭", "TLS"),
    "虎簡": ("虎尾簡易庭", "HWS"),
    # 嘉義地院
    "嘉簡": ("嘉義簡易庭", "CYS"),
    "朴簡": ("朴子簡易庭", "PZS"),
    # 臺南地院
    "南簡": ("臺南簡易庭", "TNS"),
    "新簡": ("新營簡易庭", "SYS"),
    "柳簡": ("柳營簡易庭", "LYS"),
    # 高雄地院
    "雄簡": ("高雄簡易庭", "KSS"),
    "鳳簡": ("鳳山簡易庭", "FSS"),
    "岡簡": ("岡山簡易庭", "GSS"),
    # 橋頭地院
    "橋簡": ("橋頭簡易庭", "CTS"),
    "旗簡": ("旗山簡易庭", "CSS"),
    # 屏東地院
    "屏簡": ("屏東簡易庭", "PTS"),
    "潮簡": ("潮州簡易庭", "CZS"),
    # 臺東地院
    "東簡": ("臺東簡易庭", "TTS"),
    # 花蓮地院
    "花簡": ("花蓮簡易庭", "HLS"),
    "玉簡": ("玉里簡易庭", "YUS"),
    # 基隆地院
    "基簡": ("基隆簡易庭", "KLS"),
    # 澎湖地院
    "澎簡": ("澎湖簡易庭", "PHS"),
    # 金門地院
    "金簡": ("金城簡易庭", "KMS"),
    # 連江地院
    "連簡": ("連江簡易庭", "LCS"),
}

# ---------------------------------------------------------------------------
# Regex：從文本中擷取法院名稱
# ---------------------------------------------------------------------------
RE_COURT_NAME = re.compile(
    r"("
    r"(?:臺灣|台灣)?[\u4e00-\u9fff]{1,3}(?:地方法院|少年及家事法院)"
    r"|(?:臺灣|台灣)?高等法院(?:[\u4e00-\u9fff]{1,3}分院)?"
    r"|(?:臺北|臺中|高雄)高等行政法院"
    r"|智慧財產及商業法院"
    r"|最高(?:行政)?法院"
    r")"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_court_name(name: str) -> str:
    """臺/台 統一 + 為地方法院補齊「臺灣」前綴。"""
    if not name:
        return ""
    name = normalize_court_char(name)
    if "地方法院" in name and not name.startswith(("臺灣", "福建", "最高")):
        name = "臺灣" + name
    return name


def get_court_code(court_name: str) -> Optional[str]:
    """查詢法院代碼。完全匹配優先，再做子字串 fallback。"""
    if not court_name:
        return None
    court_name = normalize_court_char(court_name)
    if court_name in COURT_OPTIONS:
        return COURT_OPTIONS[court_name]
    for name, code in COURT_OPTIONS.items():
        if name in court_name or court_name in name:
            return code
    return None


def extract_court_name(text: str) -> str:
    """從文本中 regex 擷取法院全名並正規化。"""
    m = RE_COURT_NAME.search(text or "")
    if not m:
        return ""
    court = m.group(1)
    court = normalize_court_char(court)
    if "地方法院" in court and not court.startswith(("臺灣", "臺北", "最高")):
        court = "臺灣" + court
    return court
