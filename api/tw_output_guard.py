"""
台灣用語輸出守門器（LINE / Discord / Telegram / OpenClaw 最終送出前）

目標：
1. 先做 deterministic 台灣詞彙正規化（低延遲、穩定）。
2. 再呼叫本地 LLM 做二次校正（預設全量啟用，可由環境變數關閉）。
3. 任何失敗都要降級回傳原文，不中斷主流程。
"""

from __future__ import annotations

import math
import os
import re
import json
import logging
from collections import Counter
from typing import Dict, Tuple, List


# 常見陸用詞 -> 台灣慣用詞（可持續擴充）
MAINLAND_TO_TW: Dict[str, str] = {
    "台灣": "臺灣",
    "台北": "臺北",
    "台中": "臺中",
    "台南": "臺南",
    "台東": "臺東",
    "台檢": "臺檢",
    "台上": "臺上",
    "台抗": "臺抗",
    "台簡": "臺簡",
    "台易": "臺易",
    "台訴": "臺訴",
    "台聲": "臺聲",
    "人民法院": "法院",
    "檢察院": "檢察署",
    "勞動合同": "勞動契約",
    "勞動者": "勞工",
    "知識產權": "智慧財產權",
    "行政復議": "訴願",
    "實施條例": "施行細則",
    "實施細則": "施行細則",
    "訊問筆錄": "訊問筆錄",
    "调查": "調查",
    "证据": "證據",
    "程序性": "程序性",
    "后续": "後續",
    "优化": "最佳化",
    "優化": "最佳化",
    "信息": "資訊",
    "数据": "資料",
    "數據": "資料",
    "接口": "介面",
    "對接": "串接",
    "适配": "相容",
    "適配": "相容",
    "模块": "模組",
    "模塊": "模組",
    "代码": "程式碼",
    "文件夹": "資料夾",
    "文件夾": "資料夾",
    "线程": "執行緒",
    "异步": "非同步",
    "排查": "檢查",
    "重启": "重啟",
    "运行": "執行",
    "运作": "運作",
    "关键信息": "關鍵資訊",
    "關键信息": "關鍵資訊",
    "关鍵信息": "關鍵資訊",
    "關键信息": "關鍵資訊",
    "關鍵信息": "關鍵資訊",
    "關键": "關鍵",
    "关鍵": "關鍵",
    "关键信息": "關鍵資訊",
    "默認": "預設",
    "默认": "預設",
    "支持": "支援",
    "支援性": "支援性",
    "调度": "調度",
    "調度器": "排程器",
    "排期": "排程",
    "上报": "回報",
    "匯報": "回報",
    "上線": "上線",
    "调优": "最佳化",
    "調優": "最佳化",
    "调用": "呼叫",
    "調用": "呼叫",
    "账号": "帳號",
    "账號": "帳號",
    "短信": "簡訊",
    "视频": "影片",
    "視頻": "影片",
    "屏蔽": "封鎖",
    "屏蔽詞": "封鎖詞",
    "兜底": "降級備援",
    "扩展": "擴充",
    "擴展": "擴充",
    "适用": "適用",
    "适配器": "轉接器",
    "適配器": "轉接器",
    "回滚": "回滾",
    "回滾策略": "回滾策略",
    "节点": "節點",
    "節點間協同": "節點協作",
    "在线": "線上",
    "离线": "離線",
    "离線": "離線",
    "離线": "離線",
    "激活": "啟用",
    "啟動器": "啟動程式",
    "日志": "日誌",
    "日志檔": "日誌檔",
    "覆蓋率": "涵蓋率",
    "复现": "重現",
    "複現": "重現",
    "模板化": "範本化",
    "批量": "批次",
    "批次化": "批次",
    "新增一条": "新增一筆",
    "条目": "項目",
    "一键": "一鍵",
    "告警": "警示",
    "触发": "觸發",
    "觸发": "觸發",
    "觸發器": "觸發條件",
    "软删除": "軟刪除",
    "软體": "軟體",
    "发信": "寄信",
    "投递": "投遞",
    "回执": "回條",
    "收件箱": "收件匣",
    "运维": "維運",
    "運維": "維運",
    "回復": "回覆",
    "回覆用語": "回覆用語",
    "自动": "自動",
    "自動化流程中加入「學習」機制": "在自動化流程中加入「學習」機制",
}

# 句型級替換（避免單字誤傷）
MAINLAND_REGEX_TO_TW: List[Tuple[str, str]] = [
    (r"請耐心等待", "請稍候"),
    (r"稍後會給您回復", "稍後會回覆您"),
    (r"正在為您處理中", "正在處理中"),
    (r"如有疑問可隨時聯繫", "如有疑問可隨時聯絡"),
    (r"請您提供", "請提供"),
    (r"為您同步", "為您同步更新"),
    (r"排查原因", "檢查原因"),
    (r"(?i)\bCurrent Brain\b", "目前大腦模式"),
    (r"(?i)\bActive Model\b", "目前模型"),
    (r"(?i)\bModel\b", "模型"),
    (r"(?i)\bStatus\b", "狀態"),
    (r"(?i)\bRole\b", "角色"),
    (r"(?i)\bCommander\b", "指揮節點"),
    (r"(?i)\bOnline\b", "線上"),
    (r"(?i)\bDistributed\b", "分散式"),
    (r"(?i)\bBig Brain\b", "大腦模式"),
]

logger = logging.getLogger("OutputGuard")


# ---------------------------------------------------------------------------
# 客服模板偵測：LLM 偶爾會把司法/法扶入口網站的 HTML 文字
# 重新格式化成「尊敬的客戶」客服信件樣式送出。
# 以下模式命中任一條，視為客服模板洩漏，替換成簡短內部狀態語。
# ---------------------------------------------------------------------------
_CUSTOMER_SERVICE_PATTERNS: List[str] = [
    r"尊敬的.{0,6}客戶",
    r"感謝您使用我們的.{0,10}服務",
    r"竭誠為您服務",
    r"目前尚無待處理的.{0,20}申請",
    r"若您有.{0,20}，請您再行提出申請",
    r"歡迎透過以下方式與我們聯絡",
    # Formal letter sign-off patterns from external portals
    r"電話：\(\d{2}\)\s*\d{4}-\d{4}",
    r"電子郵件：[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    r"地址：\d{3}",
]

# 2026-04-24：索引 6-8（電話/電子郵件/地址）單獨出現不是客服樣板，
# 是文件附錄常見格式（本次撞到 179 頁研究報告附錄的 NGO 名錄誤觸發）。
# STRONG 只保留真正的客服開頭/結尾樣板前 6 條。
_CUSTOMER_SERVICE_STRONG_PATTERNS: List[str] = _CUSTOMER_SERVICE_PATTERNS[:6]

_GENERIC_REFUSAL_PATTERNS: List[str] = [
    r"根據您所提供的對話內容",
    r"我無法直接為您提供",
    r"一般性建議",
]

_TRANSLATION_DRIFT_PATTERNS: List[str] = [
    r"尊敬的.{0,6}客戶",
    r"(?:本公司|貴公司).{0,30}(?:產品|服務|個人資料|隱私權)",
    r"個人資料保護",
    r"隱私權",
    r"privacy policy",
    r"privacy protection",
    r"感謝您對(?:本公司|我們).{0,12}(?:產品|服務)的(?:興趣|支持)",
]

# 內部指令/排程 wrapper 文案，禁止直接對外送出。
INTERNAL_LEAK_PATTERNS: List[str] = [
    r"\[CRON_INTERNAL\]",
    r"A scheduled reminder has been triggered",
    r"Please relay this to the user",
    r"請在本機執行以下指令",
    r"Conversation info \(untrusted metadata\)",
    r'^\s*\{\s*"message_id"\s*:',
    r'^\s*"sender"\s*:\s*".*openclaw.*"\s*$',
    r"\bMAGI_[A-Z0-9_]+=.+\bpython\b.+\baction\.py\b",
    r"/Users/ai/Desktop/.+\baction\.py\b",
    r"^\s*NO_REPLY\s*$",
    r"^\s*assistant\s*$",
    # Internal trust badge leak patterns (2026-04-11)
    r"\[已驗證事實\]",
    r"\[使用者陳述\]",
    r"\[檢索線索\]",
    r"\[衍生推論\]",
]

# 模糊 source 聲明 → 軟性 rewrite（不整行刪除，只換前綴）
# LLM 沒有實際資料時常用此類措辭（2026-04-20）
_VAGUE_SOURCE_REWRITES: List[Tuple[str, str]] = [
    ("根據我目前找到的資訊，", "（以下資料來自網路搜尋，不保證即時準確）"),
    ("根據我目前找到的資訊", "（以下資料來自網路搜尋，不保證即時準確）"),
    ("根據我剛剛搜尋到的資料，", "（以下資料來自網路搜尋，不保證即時準確）"),
    ("根據我的搜尋結果，", "（以下資料來自網路搜尋，不保證即時準確）"),
]

# LLM 角色設定幻覺 / prompt 洩漏：模型自行產出 CASPER 角色描述或原封不動吐出 system prompt。
# 這裡用整段偵測（非逐行），命中 >= 2 個即攔截整段回覆。
_PERSONA_HALLUCINATION_KEYWORDS: List[str] = [
    "你扮演",
    "扮演 CASPER",
    "CASPER 角色",
    "法律事務說明",
    "核心工作項目",
    "夜間巡邏",
    "確保事務所",
    "運作核心",
    "CASPER 法律事務",
    "流程總覽",
    # ── prompt leak patterns ──
    "你是 CASPER（MAGI-01）",
    "記憶優先於網路摘要",
    "[長期記憶]",
    "[網路研究]",
    "[近期對話]",
    "[規則]",
    "不可硬猜",
    "寫入長期記憶",
    "MAGI-01），請以繁體中文",
    "承接上下文",
    "覆誦正確的資訊",
]
_PERSONA_HALLUCINATION_THRESHOLD = 2

# ---------------------------------------------------------------------------
# 統計式亂碼偵測 — 不依賴窮舉 pattern，用字元熵 + n-gram 重複率判斷
# ---------------------------------------------------------------------------

def _char_entropy(text: str) -> float:
    """計算字元級 Shannon entropy（bits）。正常中文約 6-9，亂碼常 < 5 或 > 10。"""
    if not text:
        return 0.0
    freq = Counter(text)
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in freq.values() if c > 0)


def _bigram_repetition_ratio(text: str) -> float:
    """bigram 重複率：unique bigrams / total bigrams。正常文本 > 0.5，亂碼常 < 0.3。"""
    if len(text) < 4:
        return 1.0
    bigrams = [text[i:i+2] for i in range(len(text) - 1)]
    if not bigrams:
        return 1.0
    return len(set(bigrams)) / len(bigrams)


_GIBBERISH_KEYWORDS: list[str] = [
    "rep#", "整合跨語言", "教會畜", "認知自制", "大量教會", "小膠整合",
]


def _has_semantic_breaks(text: str) -> bool:
    """
    偵測「語法正確但語義斷裂」的亂碼 —— 主謂賓搭配不合理。
    用中文常用動賓搭配反向判定：若句子裡有多個「罕見搭配」就很可能是亂碼。
    只處理較長文本（≥30 字元），短文本跳過。
    """
    if len(text) < 30:
        return False
    # 已知的 TAIDE 亂碼語義搭配（動詞+不合理賓語）
    bizarre_collocations = [
        r"訊息反映了.{0,6}行為的行動",
        r"指令應是.{0,8}利用",
        r"更正和對.{0,6}強調",
        r"強調之上.{0,4}每日",
        r"整合.{0,4}預期行為",
        r"勉強.{0,4}整合",
    ]
    hits = sum(1 for p in bizarre_collocations if re.search(p, text))
    return hits >= 2


def _is_gibberish(text: str) -> bool:
    """
    多層亂碼偵測。結合三個信號層：
    Layer 1: 已知亂碼關鍵字（快速路徑，零延遲）
    Layer 2: 統計異常（字元熵 + bigram 重複率 + 標點比例）
    Layer 3: 語義斷裂搭配偵測（LLM 特有的荒謬語義組合）
    純 CPU 計算，延遲 < 1ms。
    """
    s = (text or "").strip()
    if not s or len(s) < 10:
        return False

    # ── Layer 1: 已知亂碼 token（最快路徑）──
    if any(kw in s for kw in _GIBBERISH_KEYWORDS):
        return True

    # ── Layer 2: 統計信號 ──
    punct_chars = set("，。、；：「」『』（）！？…—＊＃｜【】｛｝*#|[]{}~@^&")
    punct_count = sum(1 for c in s if c in punct_chars)
    punct_ratio = punct_count / len(s)

    # 極端標點比例
    if punct_ratio > 0.25:
        return True

    entropy = _char_entropy(s)
    bigram_ratio = _bigram_repetition_ratio(s)

    # 統計信號組合（至少兩個才觸發，避免 false positive）
    signals = 0
    if entropy < 2.5:                # 極低熵（幾乎全是重複字元）
        signals += 1
    if bigram_ratio < 0.30:           # bigram 高度重複
        signals += 1
    if punct_ratio > 0.15:            # 標點明顯偏高
        signals += 1

    if signals >= 2:
        return True

    # ── Layer 3: 語義斷裂偵測 ──
    if _has_semantic_breaks(s):
        return True

    return False


def _unwrap_json_fence(text: str) -> str:
    """
    If the whole message is wrapped as ```json ... ```, unwrap it.
    """
    s = (text or "").strip()
    if not s:
        return s
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", s, flags=re.IGNORECASE)
    if m:
        return (m.group(1) or "").strip()
    return s


def _machine_output_to_natural_text(text: str) -> Tuple[str, bool]:
    """
    Convert machine-like outputs (NO_REPLY / tool-call JSON / empty JSON shells)
    into natural language chat text.
    """
    src = (text or "").strip()
    if not src:
        return src, False

    s = _unwrap_json_fence(src)
    low = s.lower()
    # Common runtime/model failures -> user-readable, non-JSON fallback.
    if (
        "request was aborted" in low
        or "this operation was aborted" in low
        or "request timed out before a response was generated" in low
        or "all models failed" in low
        or "llm request timed out" in low
        or "gateway timeout after" in low
        or "session file locked" in low
        or "embedded agent failed before reply" in low
        or "agent failed before reply" in low
        or "cannot resolve model id from /v1/models" in low
    ):
        return "⚠️ 目前模型回應逾時，我已收到你的需求。請再給我一次指令，或改用較短描述，我會優先處理。", True
    if "exec host not allowed" in low:
        return "⚠️ 工具執行環境目前設定異常，我正在自動修復後重試。", True
    if "failed to run for the fifth time" in low or "command is not found" in low:
        return "⚠️ 目前工具啟動失敗，我正在切換到可用流程並重試。", True
    if "missing required parameter: newtext" in low or "edit: `in " in low:
        return "⚠️ 目前系統誤觸內部編輯工具，我已忽略該操作並回到正常對話模式。請直接重送你的需求。", True
    if "the tool `edit` requires the `newtext`" in low:
        return "⚠️ 系統剛才誤走內部工具流程，已自動中止。請直接再說一次需求，我會以一般對話回覆。", True
    if low in {"no_reply", "noreply", "none", "null", "{}", "[]"}:
        return "⏳ 已收到你的訊息，正在處理中。完成後我會回覆你結果。", True
    if re.fullmatch(r"[A-Za-z]", s):
        return "⏳ 已收到你的訊息，正在處理中。完成後我會回覆你結果。", True

    # Detect inline tool-call JSON leak without full parsing.
    if re.search(r'^\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*[\{\[]', s):
        return "⏳ 已收到你的指令，正在執行。完成後我會用自然文字回覆結果。", True
    if re.search(r'^\{\s*"tool"\s*:\s*"[^"]+"\s*,\s*"(args|arguments)"\s*:\s*[\{\[]', s):
        return "⏳ 已收到你的指令，正在執行。完成後我會用自然文字回覆結果。", True

    if not (s.startswith("{") or s.startswith("[")):
        return src, False

    try:
        data = json.loads(s)
    except Exception:
        return src, False

    if isinstance(data, dict):
        # Tool-call shaped payload.
        if "name" in data and "arguments" in data and len(data.keys()) <= 5:
            return "⏳ 已收到你的指令，正在執行。完成後我會用自然文字回覆結果。", True

        # Prefer explicit human-facing fields.
        for k in ("reply", "response", "text", "message", "content", "translation", "summary"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip(), True

        if "error" in data:
            err = str(data.get("error") or "").strip() or "未知錯誤"
            return f"⚠️ 系統回報：{err}", True

        if "success" in data:
            if bool(data.get("success")):
                return "✅ 已完成處理。", True
            err = str(data.get("error") or "").strip() or "處理失敗"
            return f"❌ 處理失敗：{err}", True

        return "⏳ 已收到資料，正在整理後回覆。", True

    if isinstance(data, list):
        if not data:
            return "⏳ 已收到資料，內容目前為空。", True
        return "⏳ 已收到多筆資料，正在整理重點後回覆。", True

    return src, False


def _strip_customer_service_template(text: str) -> Tuple[str, bool]:
    """
    偵測 LLM 把入口網站「客服信件」樣式輸出洩漏給管理員的狀況。
    命中任一客服模板關鍵字→替換成簡短內部狀態語，
    並記錄 logger.warning 以便追蹤觸發原因。
    """
    import logging as _logging
    s = text or ""
    if not s:
        return s, False

    hit_patterns = [p for p in _CUSTOMER_SERVICE_PATTERNS if re.search(p, s)]
    if not hit_patterns:
        return s, False

    _logging.getLogger("OutputGuard").warning(
        f"[客服模板洩漏] 偵測到 {len(hit_patterns)} 個客服樣式關鍵字，已攔截輸出。"
        f" 觸發模式：{hit_patterns[0]}"
    )
    # Extract the core status phrase (first content-bearing sentence) if possible.
    # Typical pattern: the 2nd or 3rd sentence describes the actual status.
    status_sentence = ""
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip greeting/sign-off lines
        if re.search(r"尊敬|感謝|竭誠|聯絡|電話|電子郵件|地址", line):
            continue
        if re.search(r"目前尚無|無待處理|查無資料|沒有.{0,10}申請", line):
            status_sentence = line
            break
        if re.search(r"申請|案件|處理", line) and len(line) < 80:
            status_sentence = line
            break

    if status_sentence:
        return f"ℹ️ {status_sentence}", True
    return "ℹ️ 系統查無待處理項目。", True


def _strip_generic_refusal_template(text: str) -> Tuple[str, bool]:
    s = text or ""
    if not s:
        return s, False
    hit_count = sum(1 for p in _GENERIC_REFUSAL_PATTERNS if re.search(p, s))
    if hit_count < 2:
        return s, False
    logger.warning("[拒答模板洩漏] 偵測到通用拒答樣式，已攔截輸出。")
    return "⚠️ 偵測到非任務型通用回覆，已自動略過。請重送原指令，我會回覆實際執行結果。", True


def _strip_internal_leaks(text: str) -> Tuple[str, bool]:
    s = text or ""
    if not s:
        return s, False

    # ── 角色設定幻覺整段攔截 ──
    persona_hits = sum(1 for kw in _PERSONA_HALLUCINATION_KEYWORDS if kw in s)
    if persona_hits >= _PERSONA_HALLUCINATION_THRESHOLD:
        logger.warning(f"[角色幻覺攔截] 命中 {persona_hits} 個關鍵字，整段回覆已攔截。")
        return "抱歉，我剛才跑題了。請再說一次你的問題，我會直接回答。", True

    flagged = any(re.search(p, s, flags=re.IGNORECASE) for p in INTERNAL_LEAK_PATTERNS)
    if not flagged:
        return s, False

    kept: List[str] = []
    for line in s.splitlines():
        ln = line.strip()
        if not ln:
            kept.append(line)
            continue
        hit = any(re.search(p, ln, flags=re.IGNORECASE) for p in INTERNAL_LEAK_PATTERNS)
        # 額外擋掉疑似 shell 命令列（避免洩漏環境變數/本機路徑）。
        if not hit and re.search(r"\b[A-Z_]{3,}=[^\s]+", ln) and re.search(r"\bpython\b", ln):
            hit = True
        if not hit and ("/Users/" in ln and ("--task " in ln or "action.py" in ln)):
            hit = True
        if not hit:
            kept.append(line)

    out = "\n".join(kept).strip()
    if not out:
        out = "抱歉，我剛才把內部判斷文字講出來了。請再說一次你的問題，我會直接自然回答。"
    return out, True


def detect_output_guard_issues(text: str, mode: str = "general") -> List[str]:
    """
    Lightweight validator for downstream workflows that need hard blocking
    instead of best-effort rewriting.
    """
    s = str(text or "").strip()
    if not s:
        return []

    issues: List[str] = []
    mode_l = mode.lower()
    customer_patterns = (
        _CUSTOMER_SERVICE_STRONG_PATTERNS
        if mode_l in {"translation", "summary"}
        else _CUSTOMER_SERVICE_PATTERNS
    )
    checks: List[Tuple[str, List[str]]] = [
        ("customer_service", customer_patterns),
        ("internal_leak", INTERNAL_LEAK_PATTERNS),
    ]
    # generic_refusal: 翻譯/摘要模式不檢查，因為原文可能包含「一般性建議」等合法法律用語
    if mode_l not in {"translation", "summary"}:
        checks.append(("generic_refusal", _GENERIC_REFUSAL_PATTERNS))
    if mode_l in {"translation", "summary"}:
        checks.append(("translation_drift", _TRANSLATION_DRIFT_PATTERNS))

    for label, patterns in checks:
        for pat in patterns:
            if re.search(pat, s, flags=re.IGNORECASE):
                issues.append(label)
                break
    return issues


def mark_non_authoritative_context(text: str, *, label: str = "背景摘要", source: str = "模型壓縮") -> str:
    """
    Prefix derived context so downstream prompts do not treat it as source truth.
    """
    body = str(text or "").strip()
    if not body:
        return ""
    header = f"【{label}｜非原文｜僅供延續上下文】"
    if source:
        header = f"{header} 來源：{source}"
    return f"{header}\n{body}"


def mark_unverified_reply(text: str, *, reason: str = "未驗證") -> str:
    """
    Prefix fallback replies that must not be presented as verified facts.
    """
    body = str(text or "").strip()
    header = f"【未驗證回覆｜{reason}】"
    return f"{header}\n{body}" if body else header


def strip_markdown_for_chat(text: str) -> str:
    """Strip Markdown formatting that doesn't render on LINE/chat platforms.
    Preserves emoji and plain text structure. Saves tokens on LLM output."""
    import re as _re
    s = text or ""
    if not s:
        return s
    # ```code blocks``` → content only (MUST be before inline backtick)
    s = _re.sub(r"```[a-zA-Z]*\n?", "", s)
    # **bold** or __bold__ → content only
    s = _re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = _re.sub(r"__(.+?)__", r"\1", s)
    # *italic* or _italic_ (but not underscores in identifiers like skill_name)
    s = _re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", s)
    # `inline code` → content only
    s = _re.sub(r"`([^`]+)`", r"\1", s)
    # [link text](url) → text url  (keep both for readability)
    s = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", s)
    # ### headings → content only
    s = _re.sub(r"^#{1,6}\s+", "", s, flags=_re.MULTILINE)
    # --- or *** horizontal rules → empty
    s = _re.sub(r"^[\-\*_]{3,}\s*$", "", s, flags=_re.MULTILINE)
    # Collapse triple+ newlines
    s = _re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _limit_message_for_platform(text: str, platform: str = "") -> str:
    s = text or ""
    if not s:
        return s

    p = (platform or "").strip().upper()
    # 留緩衝，避免平台編輯/包裝後超長失敗。
    limits = {
        # Discord: 下游 _split_discord_chunks 會按行分段，這裡不需要截斷
        "DISCORD": 30000,
        "LINE": 4500,
        "TELEGRAM": 3500,
        "JUDGMENT": 6000,
        "WEB": 12000,
    }
    lim = limits.get(p, 6000)
    if len(s) <= lim:
        return s
    suffix = "\n\n（內容較長，已先截斷；若需要完整內容，我可以改用 TXT 檔回傳。）"
    keep = max(200, lim - len(suffix))
    return s[:keep].rstrip() + suffix


def _to_bool(v: str, default: bool = False) -> bool:
    s = str(v or "").strip().lower()
    if not s:
        return default
    return s in {"1", "true", "yes", "on", "y"}


def _opencc_s2twp(text: str) -> str:
    s = text or ""
    if not s:
        return s
    try:
        from opencc import OpenCC

        return OpenCC("s2twp").convert(s)
    except Exception:
        return s


def _replace_mainland_terms(text: str) -> Tuple[str, int]:
    out = text or ""
    hits = 0
    for src, dst in MAINLAND_TO_TW.items():
        if src in out:
            out = out.replace(src, dst)
            hits += 1
    for pat, dst in MAINLAND_REGEX_TO_TW:
        out, n = re.subn(pat, dst, out)
        hits += n
    return out, hits


def _protect_terms(text: str, terms: List[str]) -> Tuple[str, Dict[str, str]]:
    out = text or ""
    protected: Dict[str, str] = {}
    for idx, term in enumerate(terms):
        if not term or term not in out:
            continue
        token = f"__MAGI_PROTECTED_TERM_{idx}__"
        out = out.replace(term, token)
        protected[token] = term
    return out, protected


def _restore_terms(text: str, protected: Dict[str, str]) -> str:
    out = text or ""
    for token, term in (protected or {}).items():
        out = out.replace(token, term)
    return out


def _looks_legal_context(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    markers = [
        "法院",
        "地檢署",
        "判決",
        "裁定",
        "法扶",
        "開庭",
        "書狀",
        "閱卷",
        "筆錄",
        "條",
        "第",
    ]
    return any(m in s for m in markers)


def _contains_mainland_signal(text: str) -> bool:
    s = text or ""
    if not s:
        return False
    if any(k in s for k in MAINLAND_TO_TW.keys()):
        return True
    # 常見簡體字訊號（門檻低，作為 TAIDE 觸發條件之一）
    return bool(re.search(r"[后发为这并对关审证诉务数据国资讯线点复启删优适账号调范拟续结滚]", s))


def _should_run_taide(text: str, force: bool = False) -> bool:
    # 預設所有輸出都經過 TAIDE 二次確認；必要時可用環境變數關閉。
    # Default to selective mode to avoid adding 6s latency on every short notification.
    force_all = _to_bool(os.environ.get("MAGI_TW_REVIEW_FORCE_ALL", "0"), False)
    if force or force_all:
        if not _to_bool(os.environ.get("MAGI_TW_REVIEW_ENABLED", "1"), True):
            return False
        max_chars = int(os.environ.get("MAGI_TW_REVIEW_MAX_CHARS", "12000") or "12000")
        return len(text or "") <= max_chars
    if not _to_bool(os.environ.get("MAGI_TW_REVIEW_ENABLED", "1"), True):
        return False
    # Short acknowledgements should stay fast and deterministic.
    if len((text or "").strip()) < 14:
        return False
    max_chars = int(os.environ.get("MAGI_TW_REVIEW_MAX_CHARS", "12000") or "12000")
    if len(text or "") > max_chars:
        return False
    if _contains_mainland_signal(text):
        return True
    # 法律語境且中長回覆才觸發，避免每句短訊都走模型。
    return _looks_legal_context(text) and len(text or "") >= 180


_SYSTEM_CONTEXT_MARKERS: List[str] = [
    "閱卷",
    "法扶",
    "法院",
    "筆錄",
    "逐字稿",
    "翻譯",
    "摘要",
    "下載",
    "案件",
    "書狀",
]

_OFF_TOPIC_MARKERS: List[str] = [
    "退款",
    "退費",
    "補發證書",
    "補發憑證",
    "雲端儲存空間",
    "可下載狀態",
]

_DRIFT_PATTERNS: List[str] = [
    r"根據您所提供的對話內容",
    r"我無法直接為您提供",
    r"然而，我可以提供一般性建議",
]

_PROTECTED_LEGAL_TERMS: List[str] = [
    "消費者債務清理程序",
]

# 容易被 OpenCC s2twp 誤轉的中文姓氏（余→餘、范→範、于→於、干→幹、谷→穀、云→雲 等）
# 以及其他在法律文書中常見且不應轉換的字
_SURNAME_OPENCC_FIXES: Dict[str, str] = {
    "餘春香": "[當事人H]",  # 具體案例修正
}

# 比對姓氏 pattern：「姓＋1~3字名」出現在人名語境（前後為標點、空白或特定分隔符）
import functools as _ft

@_ft.lru_cache(maxsize=1)
def _surname_fix_pattern():
    """預編譯姓氏修正 regex：偵測被 OpenCC 誤轉的姓氏。"""
    # 常見被 OpenCC 誤轉的姓氏字（轉換後的錯誤形式 → 正確形式）
    _pairs = [
        ("餘", "余"),   # 余 → 餘 (OpenCC s2twp)
        ("範", "范"),   # 范 → 範
        ("於", "于"),   # 于 → 於（僅姓氏時）
    ]
    patterns = []
    for wrong, correct in _pairs:
        # 匹配「錯誤姓氏 + 1~3 字名字」出現在 人名位置
        # 位置：行首/空白/標點/分隔符後面
        patterns.append((
            re.compile(r'(?<=[\s｜|，。、：；\n]|^)' + wrong + r'([\u4e00-\u9fff]{1,3})(?=[\s｜|，。、：；\n]|$)', re.MULTILINE),
            correct,
            wrong,
        ))
    return patterns

def _fix_surname_opencc(text: str) -> str:
    """修正 OpenCC 對姓氏的誤轉換。"""
    out = text or ""
    # 先做精確修正（已知的完整姓名）
    for wrong, correct in _SURNAME_OPENCC_FIXES.items():
        if wrong in out:
            out = out.replace(wrong, correct)
    return out


def _looks_taide_domain_drift(before: str, after: str) -> bool:
    """
    Guard against model drift:
    when legal/system notifications are rewritten into unrelated客服話術.
    """
    b = (before or "").strip()
    a = (after or "").strip()
    if not b or not a:
        return False

    has_system_context = any(k in b for k in _SYSTEM_CONTEXT_MARKERS)
    if not has_system_context:
        return False

    if any(re.search(p, a) for p in _DRIFT_PATTERNS):
        return True

    after_off_topic = any(k in a for k in _OFF_TOPIC_MARKERS)
    before_off_topic = any(k in b for k in _OFF_TOPIC_MARKERS)
    return after_off_topic and not before_off_topic


def normalize_output_text(text: str, platform: str = "", force_taide: bool = False) -> str:
    """
    輸出前的台灣用語守門器。
    回傳可直接送出的文字；任何例外都降級回原文。
    """
    src = (text or "").strip()
    if not src:
        return src

    # 0) 統計式亂碼偵測 — 在任何處理之前先攔截，避免亂碼經過後續步驟變得更難辨識
    if _is_gibberish(src):
        logger.warning("[亂碼偵測] 統計信號觸發（entropy/bigram/punct），已攔截輸出。")
        return "⚠️ 偵測到模型產生異常輸出，已自動攔截。請重新輸入指令。"

    # 0.5) Convert machine-like outputs into natural text first.
    src, _ = _machine_output_to_natural_text(src)
    src, protected_terms = _protect_terms(src, _PROTECTED_LEGAL_TERMS)
    has_protected_legal_terms = bool(protected_terms)

    # 1) 字形/詞彙 deterministic 正規化
    out = _opencc_s2twp(src)
    out = _fix_surname_opencc(out)  # 修正 OpenCC 對姓氏的誤轉
    out, _ = _replace_mainland_terms(out)

    # 1.5a) 客服模板洩漏防護（LLM 把入口網站 HTML 重格式化成客服信件）
    out, _ = _strip_customer_service_template(out)

    # 1.5b-pre) 模糊 source 聲明軟性 rewrite（不整段刪除，只換前綴）
    for _vague_src, _replacement in _VAGUE_SOURCE_REWRITES:
        if _vague_src in out:
            out = out.replace(_vague_src, _replacement, 1)
            break

    # 1.5b-pre2) LLM 訓練知識模糊歸因 rewrite（根據我的了解 / 據我所知 等）
    # 這類句式代表 LLM 以訓練知識回答事實性問題，未提供可查證來源
    try:
        from api.hallucination_guard import rewrite_ungrounded_attribution as _rua
        out = _rua(out)
    except Exception:
        pass

    # 1.5b) 內部命令洩漏防護（LINE / Discord / TG / Web 等對外輸出）
    out, _ = _strip_internal_leaks(out)

    # 1.5c) 通用拒答模板防護（避免離題客服腔/拒答腔）
    out, _ = _strip_generic_refusal_template(out)

    # 1.6) 再跑一次機器輸出轉換，避免清掉 metadata 後殘留 NO_REPLY 或工具 JSON。
    out, _ = _machine_output_to_natural_text(out)
    out = _restore_terms(out, protected_terms)

    # 2) 可選 TAIDE 二次校正（僅在必要條件）
    if (not has_protected_legal_terms) and _should_run_taide(out, force=force_taide):
        try:
            from skills.law_review.tw_legal_review import review_legal_text

            timeout = int(os.environ.get("MAGI_TW_REVIEW_TIMEOUT_SEC", "6") or "6")
            model = (os.environ.get("MAGI_TW_REVIEW_MODEL") or "").strip()
            before_taide = out
            if model:
                corrected = review_legal_text(out, model=model, timeout=timeout)
            else:
                corrected = review_legal_text(out, timeout=timeout)
            if corrected and corrected.strip():
                candidate = corrected.strip()
                candidate = _opencc_s2twp(candidate)
                candidate, _ = _replace_mainland_terms(candidate)
                if _looks_taide_domain_drift(before_taide, candidate):
                    logger.warning("[TAIDE drift] off-topic rewrite detected; keep deterministic output.")
                else:
                    out = candidate
        except Exception:
            # 靜默降級，避免阻塞回覆
            pass

    # 2.5) LINE/chat 平台去除 Markdown 格式（LINE 不渲染 Markdown，**粗體** 只會顯示星號）
    if (platform or "").strip().upper() in ("LINE", "TELEGRAM", ""):
        out = strip_markdown_for_chat(out)

    # 3) 平台長度上限保護，避免訊息送出失敗。
    return _limit_message_for_platform(out, platform=platform)
