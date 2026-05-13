# -*- coding: utf-8 -*-
"""Non-blocking format validator for generated PDF bookmarks."""

import re
from typing import List, Tuple

_ROC_PREFIX_RE = re.compile(r"^(?:\d{3}\.\d{2}\.\d{2}|\d{8}) ")
_GROUP_RE = re.compile(r"（共\s*(\d+)\s*份）")

_KNOWN_DOC_TYPES = frozenset([
    "卷宗封面",
    "審判筆錄",
    "準備程序筆錄",
    "訊問筆錄",
    "調查筆錄",
    "勘驗筆錄",
    "調解/和解筆錄",
    "言詞辯論筆錄",
    "判決",
    "裁定",
    "起訴書",
    "追加起訴書",
    "不起訴處分書",
    "緩起訴處分書",
    "聲請簡易判決處刑書",
    "論告書",
    "答辯狀",
    "上訴/抗告狀",
    "陳報狀",
    "聲請狀",
    "補充理由狀",
    "量刑辯論意旨狀",
    "辯護意旨狀",
    "委任狀",
    "法院函",
    "警察機關函",
    "移送書",
    "送達證書",
    "傳票",
    "提票",
    "拘票",
    "押票",
    "搜索票",
    "通緝書",
    "鑑定報告",
    "精神鑑定報告",
    "法醫報告",
    "驗傷診斷書",
    "相驗屍體證明書",
    "診斷證明書",
    "扣押物品目錄表",
    "調取扣押物條",
    "搜索扣押筆錄",
    "勘查報告",
    "前案紀錄表",
    "在監在押資料",
    "刑案資料",
    "審理單",
    "報到單",
    "財產所得資料",
    "債權人清冊",
    "更生/清算方案",
    "調解/和解",
    "戶籍謄本",
    "土地/建物謄本",
    "金融交易明細",
    "照片/截圖",
    "票據/契約",
    "收發文",
    "通訊監察",
    "監視器畫面",
])


def validate_bookmark(label: str) -> Tuple[bool, List[str]]:
    """Validate bookmark labels without blocking generation."""
    warnings = []
    text = str(label or "").strip()
    if not text:
        return False, ["bookmark label 不得為空字串"]

    body = text
    if _ROC_PREFIX_RE.match(text):
        body = text.split(" ", 1)[1].strip()
    elif re.match(r"^\d{3}\.\d{2}\.\d{2}$", text) or re.match(r"^\d{8}$", text):
        warnings.append("日期後必須保留一個空格再接文件類型")
        body = ""

    found_type = None
    for doc_type in sorted(_KNOWN_DOC_TYPES, key=len, reverse=True):
        if doc_type in body:
            found_type = doc_type
            break

    if body and not found_type:
        warnings.append("文件類型不在已知 bookmark 類型清單內")

    group_match = _GROUP_RE.search(text)
    if group_match:
        try:
            count = int(group_match.group(1))
        except ValueError:
            count = 0
        if count < 2:
            warnings.append("分組標籤的份數必須 >= 2")

    return len(warnings) == 0, warnings
