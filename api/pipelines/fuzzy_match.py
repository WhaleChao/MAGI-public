"""
Chinese-aware fuzzy command matching for typo correction.

When users type ``法付`` instead of ``法扶``, or ``閱券`` instead of ``閱卷``,
this module attempts to correct the typo before the command dispatch chain
gives up and falls through to the LLM handler.

Design goals:
  * Direct typo-map hits are instant (O(n) scan of a small dict).
  * Fuzzy matching uses ``difflib.SequenceMatcher`` — no new dependencies.
  * Admin-only commands are NEVER auto-corrected (safety).
  * High-confidence corrections (>= 0.85) are auto-applied with a notice.
  * Medium-confidence suggestions (>= 0.65) are returned as a prompt.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
from typing import List, Optional, Tuple

logger = logging.getLogger("FuzzyMatch")

# ─── Known Chinese legal typo pairs (homophone / visual confusion) ─────────
_TYPO_MAP: dict[str, str] = {
    # Legal terms — homophones
    "法付": "法扶",
    "法復": "法扶",
    "法服": "法扶",
    "閱券": "閱卷",
    "閱眷": "閱卷",
    "閱劵": "閱卷",
    "刪閱": "閱卷",
    "判覺": "判決",
    "判絕": "判決",
    "覆議": "複議",
    "証據": "證據",
    "辨護": "辯護",
    "帳告": "抗告",
    "翻意": "翻譯",
    "翻異": "翻譯",
    "壹任狀": "委任狀",
    "計約書": "契約書",
    "存證信涵": "存證信函",
    "比錄": "筆錄",
    "必錄": "筆錄",
    "庭其": "庭期",
    "庭齊": "庭期",
    "查判覺": "查判決",
    "法規收尋": "法規搜尋",
    "法規受尋": "法規搜尋",
    "加班費用": "加班費",
    "摘要要": "摘要",
    "逐字搞": "逐字稿",
    "逐字槁": "逐字稿",
    "截圖排敘": "截圖排序",
    "截圖排緒": "截圖排序",
    "審閱期約": "審閱契約",
    "審閱器約": "審閱契約",
    "系統狀況": "系統狀態",
    "節點狀況": "節點狀態",
    "法付監控": "法扶監控",
    "法復監控": "法扶監控",
    "法付回報": "法扶回報",
    "法復回報": "法扶回報",
    "法付指令": "法扶指令",
    "回報指另": "回報指令",
    "閱券查核": "閱卷查核",
    "閱眷查核": "閱卷查核",
    "閱券聲請": "閱卷聲請",
    "閱眷聲請": "閱卷聲請",
    "下載閱券": "下載閱卷",
    "下載閱眷": "下載閱卷",
    "下載比錄": "下載筆錄",
    "下載必錄": "下載筆錄",
    "同步比錄": "同步筆錄",
    "同步必錄": "同步筆錄",
    "比錄更名": "筆錄更名",
    "必錄更名": "筆錄更名",
    "備份資料褲": "備份資料庫",
    "掃描案件帶辦": "掃描案件待辦",
    "判決趨式": "判決趨勢",
    "判覺趨勢": "判決趨勢",
    "精簡摘要要": "精簡摘要",
    "詳細摘要要": "詳細摘要",
    "重點正理": "重點整理",
    "重點整裡": "重點整理",
    "股市成報": "股市晨報",
    "股市晨抱": "股市晨報",
    "證據能利": "證據能力",
    "證據能里": "證據能力",
    "日厲同步": "日曆同步",
    "日歷同步": "日曆同步",
    "案件時成": "案件時程",
    "模擬側試": "模擬測試",
    "模擬則試": "模擬測試",
    "搜循": "搜尋",
    "收尋": "搜尋",
}

# ─── Load external typo map if available ───────────────────────────────────
_TYPO_MAP_PATH = os.path.join(os.path.dirname(__file__), "typo_map.json")
try:
    if os.path.exists(_TYPO_MAP_PATH):
        with open(_TYPO_MAP_PATH, "r", encoding="utf-8") as _f:
            _ext = json.load(_f)
            if isinstance(_ext, dict):
                _TYPO_MAP.update(_ext)
except Exception:
    pass

# ─── Canonical command keywords (extracted from command_dispatch.py) ────────
# Grouped by category.  Admin-only keywords are in _ADMIN_KEYWORDS below.
_CANONICAL_KEYWORDS: List[str] = [
    # Help
    "指令", "說明", "功能", "幫助", "功能列表", "技能清單",
    "有什麼功能", "可以做什麼",
    # Document generation
    "委任狀", "契約書", "收據", "存證信函",
    "審閱契約", "證據能力", "截圖排序",
    # Legal Aid (法扶)
    "法扶回報指令", "法扶指令", "回報指令", "法扶監控",
    "自動報結掃描", "二階段批次",
    "已開辦", "已報結",
    # File Review (閱卷)
    "閱卷查核", "查核閱卷", "卷宗查核", "閱卷聲請",
    "聲請閱卷", "申請閱卷",
    "檢查閱卷信箱", "下載閱卷", "閱卷下載",
    "閱卷到期檢查", "閱卷到期", "閱卷期限",
    # Transcripts (筆錄)
    "下載筆錄", "筆錄下載", "調閱筆錄", "筆錄調閱",
    "筆錄同步", "同步筆錄", "筆錄全同步",
    "筆錄更名", "更名筆錄",
    # Payments
    "已繳費", "繳費",
    # Legal tools
    "查判決", "找判決", "判決搜尋", "搜尋判決",
    "判決趨勢",
    "法規搜尋",
    "加班費",
    "庭期", "最近有什麼庭",
    "案件時程總覽",
    "司法工具",
    # Search / Fetch
    "搜尋", "查一下", "找一下", "搜一下",
    "抓取", "讀取網頁",
    # Translation / Summary
    "翻譯", "摘要", "精簡摘要", "詳細摘要",
    "短摘要", "長摘要", "重點整理",
    # Image
    "畫圖", "畫一", "畫個", "畫張",
    # Memory
    "記住", "忘記", "刪除記憶",
    # Status
    "系統狀態", "運作狀態", "節點狀態", "機器狀態", "大腦狀態",
    # Schedule / Assistant
    "今天行程", "明天行程", "本週行程",
    "日曆同步",
    "掃描案件待辦", "待辦佇列狀態",
    "股市晨報",
    # Music
    "製作音樂", "生成音樂",
    # Teaching
    "教學檔案", "教學",
    # PDF naming
    "單檔命名", "批次命名",
    # Mock test
    "模擬測試",
    # Misc
    "逐字稿", "備份資料庫",
    "亂碼", "亂碼回報",
    # RSS / Crawler
    "爬蟲目標",
]

# ─── Admin-only keywords — NEVER auto-correct these ───────────────────────
_ADMIN_KEYWORDS: set[str] = {
    # Brain / model switching
    "big brain", "distributed", "分散式", "最強模式",
    "local mode", "本地模式", "切回本地",
    "修理大腦", "修復大腦", "修理melchior", "修復melchior",
    "校準ngl", "自動校準ngl",
    # Night talk
    "夜議", "開始夜議",
    # Skill genesis / evolution
    "學會", "學習", "製作技能", "寫工具",
    "技能版本", "回滾技能", "技能ci", "技能事件",
    "標記穩定版", "開始canary", "停止canary",
    "同步技能到melchior",
    # Code operations
    "自動修復code", "修復程式碼",
    "內化code", "code技能化",
    "自動巡檢", "流程自動化",
    "讀取程式碼", "連動模式", "改善建議",
    "內化技能",
    # Iron Dome
    "鐵穹規則", "加入鐵穹規則", "自動加固鐵穹", "供應鏈掃描",
    # Core changes
    "核心變更待審", "批准核心變更", "拒絕核心變更",
    # Release
    "melchior狀態", "發布狀態",
    # System management
    "系統監控", "健康檢查", "檢查分身", "殭屍巡邏",
}


def _is_admin_keyword(text: str) -> bool:
    """Return True if the text contains any admin-only keyword."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in _ADMIN_KEYWORDS)


def fuzzy_correct(text: str) -> Tuple[Optional[str], float]:
    """
    Attempt to correct Chinese typos in the input text.

    Returns
    -------
    (corrected_text, confidence)
        corrected_text is ``None`` if no correction found.
        confidence: 1.0 for exact typo-map match, 0.0-1.0 for fuzzy match.
    """
    if not text or not text.strip():
        return None, 0.0

    # Never auto-correct admin commands
    if _is_admin_keyword(text):
        return None, 0.0

    # Step 1: Direct typo-map lookup (highest confidence)
    for typo, correct in _TYPO_MAP.items():
        if typo in text:
            corrected = text.replace(typo, correct, 1)
            logger.info("Typo map hit: '%s' -> '%s' (in: %r)", typo, correct, text[:40])
            return corrected, 1.0

    # Step 2: Fuzzy match against canonical keywords
    # Extract the command portion (first ~15 chars, enough for most Chinese commands)
    cmd_portion = text.strip()[:15]

    best_match: Optional[str] = None
    best_ratio: float = 0.0
    best_start: int = 0
    best_length: int = 0

    for keyword in _CANONICAL_KEYWORDS:
        kw_len = len(keyword)
        if kw_len < 2:
            continue
        # Slide a window of keyword length over the command portion
        for i in range(max(1, len(cmd_portion) - kw_len + 2)):
            end = min(i + kw_len, len(cmd_portion))
            substr = cmd_portion[i:end]
            if not substr or len(substr) < 2:
                continue
            ratio = difflib.SequenceMatcher(None, substr, keyword).ratio()
            if ratio > best_ratio and ratio >= 0.60:
                best_ratio = ratio
                best_match = keyword
                best_start = i
                best_length = end - i

    if best_match and best_ratio >= 0.60:
        # Build corrected text
        original_portion = text.strip()[:15]
        corrected_portion = original_portion[:best_start] + best_match + original_portion[best_start + best_length:]
        corrected = corrected_portion + text.strip()[15:]
        logger.info(
            "Fuzzy match: '%s' -> '%s' (ratio=%.2f, keyword='%s')",
            text[:30], corrected[:30], best_ratio, best_match,
        )
        return corrected, best_ratio

    return None, 0.0


def suggest_correction(text: str) -> Optional[str]:
    """
    High-level API: returns a user-facing suggestion string, or None.

    * confidence >= 0.85: auto-correct notice (caller should re-dispatch)
    * confidence >= 0.65: suggestion message (do NOT auto-execute)
    * below 0.65: None
    """
    corrected, confidence = fuzzy_correct(text)
    if not corrected:
        return None

    if confidence >= 0.85:
        return f"\U0001f4a1 已自動修正「{text[:20]}」\u2192「{corrected[:20]}」"
    elif confidence >= 0.65:
        return f"\U0001f914 你是不是要輸入「{corrected[:30]}」？請確認後重新輸入。"

    return None
