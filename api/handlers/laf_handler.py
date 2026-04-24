"""
Legal Aid Foundation (LAF) report parsing utilities extracted from Orchestrator.

Pure functions — no instance state.
"""

import logging
import re
from typing import Optional, Union, Any

logger = logging.getLogger("LAFHandler")


def laf_report_command_help() -> str:
    """Return help text for LAF report natural language commands."""
    return (
        "⚖️ **法扶回報自然語言指令**（預設：只存檔、不送出）\n\n"
        "可直接說：\n"
        "1. `幫我做蕭仁俊開辦回報`\n"
        "2. `幫[當事人F]做疑義回報 原因資力不合標準`\n"
        "3. `[當事人E]訴訟中費用支付回報 說明裁判費2000`\n"
        "4. `[當事人L]二階段回報`\n"
        "5. `蔡旭欽結案回報 原因本案無閱卷必要`\n\n"
        "6. `[當事人F]受扶助人撤回回報 原因申請人撤回`\n\n"
        "也可用結構化寫法：\n"
        "- `法扶回報 <開辦|疑義|費用|二階段|結案|撤回> <姓名或案號> [原因/說明 ...]`\n"
        "- `法扶回報 疑義 1140728-K-002 原因 資力不合標準`\n\n"
        "可帶目標：\n"
        "- 姓名（如：蕭仁俊）\n"
        "- 法扶案號（如：1140728-K-002）\n"
        "- 案件系統編號（如：2026-0013）\n\n"
        "必要條件：\n"
        "- 開辦：需能找到「開辦通知/准予扶助證明」與「委任狀」，並完成日期判讀\n"
        "  送出確認：系統會給確認碼，回覆 `正確送出 <確認碼>` 才實施送出。\n"
        "\n"
        "💡 **Discord 頻道感知自動補全 (Autocomplete)**\n"
        "若您已加入 Discord，可直接在對應頻道發送受扶助人姓名或案號：\n"
        "- **#法扶-費用**：輸入 謝千億 -> 自動觸發 `謝千億 費用支付` 回報\n"
        "- **#法扶-疑義**：輸入 謝千億 -> 自動觸發 `謝千億 疑義` 回報\n"
        "- **#法扶-二階段**：輸入 謝千億 -> 自動觸發 `謝千億 二階段` 回報\n"
        "- **#法扶-結案**：輸入 謝千億 -> 自動觸發 `謝千億 結案` 回報\n"
        "\n"
        "• `法扶監控` — 現有案件狀態總覽\n"
        "- 訴訟中費用：需找到法院收據（粉紅收據/裁判費收據）\n"
        "- 結案：若統計欄位 <= 0，會先要求你補原因\n\n"
        "🔄 **手動更新法扶狀態**\n"
        "直接說：\n"
        "- `[當事人E] 已開辦` → 更新狀態為「進行中」\n"
        "- `[當事人N] 改定子女 已報結` → 同名多案時用案由消歧義\n"
        "- `[當事人N] 消債 已報結` → 支援簡寫（消債＝消費者債務清理）\n"
        "- `1150320-E-014 已開辦`（用法扶案號）\n"
        "- `2026-0028 已結案`（用案件編號）"
    )


def detect_laf_report_action(text: str) -> tuple[str, str]:
    """Detect LAF report action type from text. Returns (action_code, label) or ('', '')."""
    s = (text or "").strip()
    low = s.lower()
    rules = [
        (("訴訟中費用支付", "訴訟中費用", "費用支付"), ("fee", "費用支付")),
        (("二階段", "附條件"), ("condition", "二階段")),
        (("遵期開辦", "開辦", "開案"), ("go_live", "開辦")),
        (("疑義", "异议", "異議"), ("inquiry", "疑義")),
        (("結案", "報結"), ("closing", "結案")),
        (("撤回",), ("withdrawal", "撤回")),
    ]
    for kws, mapped in rules:
        if any((k in s) or (k.lower() in low) for k in kws):
            return mapped
    return "", ""


def _clean_client_name(raw_name: str) -> str:
    """Clean a client name extracted from natural language."""
    s = (raw_name or "").strip()
    if not s:
        return ""
    for token in (
        "幫我", "幫", "請", "處理", "回報", "法扶", "做", "先", "暫存", "存檔",
        "姓名", "名字", "受扶助人", "當事人", "原因", "說明", "備註", "理由",
        "我的", "案件", "案號",
    ):
        s = s.replace(token, "")
    s = s.strip(" ：:，,。;；")
    s = re.sub(r"\s+", " ", s).strip()
    if re.fullmatch(r"[一-龥A-Za-z0-9_.\- ]{2,60}", s):
        return s
    return ""


_ACTION_NAME_TOKENS = {
    "開辦",
    "開案",
    "遵期開辦",
    "疑義",
    "異議",
    "异议",
    "訴訟中費用支付",
    "訴訟中費用",
    "費用支付",
    "二階段",
    "附條件",
    "附條件審查",
    "結案",
    "報結",
    "撤回",
}


def _looks_like_laf_action_name(candidate: str) -> bool:
    compact = re.sub(r"\s+", "", candidate or "")
    return bool(compact and compact in _ACTION_NAME_TOKENS)


def parse_laf_report_payload(raw_text: str) -> Optional[dict]:
    """Parse a natural-language LAF report command into a structured payload."""
    text = (raw_text or "").strip()
    if not text:
        return None

    # 含法扶案號格式（XXXXXXX-X-XXX）也視為法扶指令
    _has_laf_no = bool(re.search(r"\d{6,8}-[A-Za-z]-\d{3}", text))
    looks_like_laf = _has_laf_no or any(k in text for k in ("法扶", "回報", "報結", "開辦", "疑義", "二階段", "費用支付", "訴訟中費用", "撤回", "結案"))
    if not looks_like_laf:
        return None

    action, action_label = detect_laf_report_action(text)
    if not action:
        return None

    # reason/desc
    reason = ""
    m_reason = re.search(r"(?:原因|說明|備註|理由)\s*(?:是|為|:|：)?\s*(.+)$", text)
    if m_reason:
        reason = (m_reason.group(1) or "").strip()

    # IDs
    laf_case_no = ""
    m_laf = re.search(r"(\d{6,8}-[A-Za-z]-\d{3})", text)
    if m_laf:
        laf_case_no = (m_laf.group(1) or "").strip()

    case_number = ""
    m_case_no = re.search(r"\b(\d{4}-\d{4})\b", text)
    if m_case_no:
        case_number = (m_case_no.group(1) or "").strip()

    # explicit target by labels
    client_name = ""
    m_client = re.search(
        r"(?:受扶助人|當事人|姓名|名字)\s*(?:是|為|:|：)?\s*([^\n,，。；;]+?)\s*(?=(?:原因|說明|備註|理由|$))",
        text,
    )
    if m_client:
        _candidate_name = _clean_client_name(m_client.group(1) or "")
        if _candidate_name and not _looks_like_laf_action_name(_candidate_name):
            client_name = _candidate_name

    # natural phrase fallback: 「蕭仁俊開辦回報」
    if not client_name:
        m_inline = re.search(
            r"(?:幫我(?:做)?|請(?:幫)?|幫)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9_.\- ]{1,60}?)\s*(?:的)?(?:開辦|疑義|訴訟中費用支付|訴訟中費用|費用支付|二階段|結案|報結|撤回)(?:回報)?",
            text,
        )
        if m_inline:
            _candidate_name = _clean_client_name(m_inline.group(1) or "")
            # 排除常見非人名詞彙
            _NOT_NAMES = {"案件", "案號", "法扶", "準備", "請", "幫", "做", "處理", "確認", "正式", "先", "這個", "那個"}
            # 排除含案號片段、純數字、或非人名的候選
            _is_noise = (
                _candidate_name in _NOT_NAMES
                or any(nw in _candidate_name for nw in _NOT_NAMES)
                or re.match(r"^[A-Za-z0-9\-]+$", _candidate_name)  # 純英數 = 案號片段
                or re.search(r"\d{6,}", _candidate_name)  # 含長數字 = 案號
            )
            if _candidate_name and not _is_noise:
                client_name = _candidate_name

    # token fallback for "法扶回報 開辦 蕭仁俊 ..."
    if not client_name and not laf_case_no and not case_number:
        cleaned = text
        for token in ("法扶回報", "回報法扶", "法扶", "回報", "幫我", "請", "做", "處理", "先", "暫存", "存檔", "開辦", "疑義", "訴訟中費用支付", "訴訟中費用", "費用支付", "二階段", "結案", "報結", "撤回", "原因", "說明", "備註", "理由"):
            cleaned = cleaned.replace(token, " ")
        cleaned = re.sub(r"(\d{6,8}-[A-Za-z]-\d{3})", " ", cleaned)
        cleaned = re.sub(r"\b(\d{4}-\d{4})\b", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        m_name = re.search(r"([一-龥A-Za-z][一-龥A-Za-z0-9_.\- ]{1,60})", cleaned)
        if m_name:
            client_name = _clean_client_name(m_name.group(1) or "")

    payload = {
        "action": action,
        "action_label": action_label,
        "laf_case_no": laf_case_no,
        "case_number": case_number,
        "client_name": client_name,
        "reason": reason,
        "fields": {},
    }

    if action == "condition":
        payload["fields"] = {"at_ctype": "附條件審查"}
    if action == "inquiry" and reason:
        payload["fields"] = {"desc": reason}
    if action == "fee" and reason:
        payload["fields"] = {"desc": reason}

    return payload


# ─── 法扶狀態手動更新 ────────────────────────────────────────

_STATUS_UPDATE_PATTERNS = [
    # 「1150320-E-014 已開辦」（法扶案號優先）
    re.compile(
        r"(?P<laf_no>\d{6,8}-[A-Za-z]-\d{3})\s*(?:已經|已)\s*(?P<status>開辦|報結|結案|撤回|撤案)"
    ),
    # 「2026-0028 已開辦」（案件系統編號）
    re.compile(
        r"(?P<case_no>\d{4}-\d{4})\s*(?:已經|已)\s*(?P<status>開辦|報結|結案|撤回|撤案)"
    ),
    # 「標記[當事人E]為已開辦」
    re.compile(
        r"(?:標記|更新|設定)\s*(?P<name>[一-龥A-Za-z][一-龥A-Za-z0-9_.\- ]{1,30}?)\s*(?:為|是|→)\s*(?:已經|已)?\s*(?P<status>開辦|報結|結案|撤回|撤案)"
    ),
    # 「[當事人N] 改定子女 已報結」「[當事人N] 消債 已報結」（姓名+案由+狀態）
    re.compile(
        r"(?:法扶\s*)?(?P<name>[一-龥][一-龥A-Za-z0-9_.\- ]{1,30}?)\s+"
        r"(?P<reason>[一-龥A-Za-z]{2,20})\s+"
        r"(?:已經|已)\s*(?P<status>開辦|報結|結案|撤回|撤案)"
    ),
    # 「[當事人E] 已開辦」「[當事人E]已報結」「[當事人E] 已結案」（姓名最後匹配）
    re.compile(
        r"(?:法扶\s*)?(?P<name>[一-龥][一-龥A-Za-z0-9_.\- ]{1,30}?)\s*"
        r"(?:已經|已)\s*(?P<status>開辦|報結|結案|撤回|撤案)"
    ),
]

_STATUS_MAP = {
    "開辦": "進行中",
    "報結": "已報結",
    "結案": "已報結",
    "撤回": "已報結",
    "撤案": "已報結",
}

# 案由簡寫對照：簡寫 → 正式案由中的關鍵字（用於 LIKE 搜尋）
_CASE_REASON_ALIASES: dict[str, list[str]] = {
    "消債": ["消費者債務清理", "消債"],
    "消費者債務": ["消費者債務清理", "消債"],
    "更生": ["更生", "消費者債務清理"],
    "清算": ["清算", "消費者債務清理"],
    "改定": ["改定", "親權"],
    "改定子女": ["改定", "親權"],
    "改定親權": ["改定", "親權"],
    "監護": ["監護"],
    "扶養": ["扶養", "扶養費"],
    "離婚": ["離婚"],
    "給付": ["給付"],
    "損害賠償": ["損害賠償", "損賠"],
    "損賠": ["損害賠償", "損賠"],
    "車禍": ["損害賠償", "車禍"],
    "勞資": ["勞資", "給付資遣費", "給付工資"],
    "資遣": ["資遣費", "勞資"],
    "資遣費": ["資遣費", "給付資遣費"],
    "給付資遣費": ["給付資遣費", "資遣費"],
    "工資": ["給付工資", "勞資"],
    "給付工資": ["給付工資", "工資"],
    "保護令": ["保護令"],
    "家暴": ["保護令", "家暴"],
    "遷讓": ["遷讓", "房屋"],
    "租賃": ["租賃", "遷讓"],
    "返還": ["返還"],
    "確認": ["確認"],
    "塗銷": ["塗銷"],
    "繼承": ["繼承"],
    "拋棄繼承": ["拋棄繼承"],
    "訴訟救助": ["訴訟救助"],
}


def _expand_reason_keywords(reason_hint: str) -> list[str]:
    """將案由簡寫展開為 DB 搜尋用關鍵字列表。"""
    hint = (reason_hint or "").strip()
    if not hint:
        return []
    # 先查對照表
    if hint in _CASE_REASON_ALIASES:
        return _CASE_REASON_ALIASES[hint]
    # 部分匹配：e.g. 「改定」matches「改定子女」
    for alias, keywords in _CASE_REASON_ALIASES.items():
        if hint in alias or alias in hint:
            return keywords
    # 無匹配 → 直接用原文當關鍵字
    return [hint]


def parse_laf_status_update(raw_text: str) -> Optional[dict]:
    """
    解析法扶狀態手動更新指令。

    Returns:
        {"client_name": ..., "laf_case_no": ..., "case_number": ...,
         "case_reason_hint": ..., "new_status": ..., "status_label": ...}
        or None
    """
    text = (raw_text or "").strip()
    if not text:
        return None
    # 排除法扶回報類指令（避免誤判）
    if any(k in text for k in ("回報", "幫我做", "幫我", "原因", "說明")):
        return None

    for pat in _STATUS_UPDATE_PATTERNS:
        m = pat.search(text)
        if m:
            gd = m.groupdict()
            status_word = gd.get("status", "")
            new_status = _STATUS_MAP.get(status_word, "")
            if not new_status:
                continue
            return {
                "client_name": _clean_client_name(gd.get("name", "")),
                "laf_case_no": gd.get("laf_no", ""),
                "case_number": gd.get("case_no", ""),
                "case_reason_hint": gd.get("reason", ""),
                "new_status": new_status,
                "status_label": f"已{status_word}",
            }
    return None
