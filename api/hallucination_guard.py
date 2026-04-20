"""
api/hallucination_guard.py
==========================
全域防幻覺守門員（Hallucination Guard）

設計原則
--------
1. LLM = 推論引擎，外部來源 = 知識來源
   LLM 不可作為事實來源；事實必須來自 DB / 向量記憶 / 即時 API。
   不確定時明說不確定，提供官方查詢連結，不猜測。

2. 風險分級（classify_risk）
   HIGH   : 具體法條號碼（民法第N條）、程序時效數字、法院案號
            → 強制 COMPLEX tier，走完整三哲人審查
   MEDIUM : 法律程序詞彙（上訴要件、舉證責任等），無具體引用數字
            → 建議 COMPLEX tier（內嵌向量記憶引用）
   SAFE   : 閒聊、意見、分析請求
            → 允許 SIMPLE tier

3. 事實溯源檢查（check_fact_grounding）
   偵測答案中的法條引用（如「民法第184條」），確認是否出現在提供的 context。
   未溯源的引用 → 納入 veto 理由，在 ensemble 中觸發否決。
   設計保守：只抓「第N條」格式，不誤傷模糊表達。

4. 模糊歸因 rewrite（rewrite_ungrounded_attribution）
   「根據我的了解」「據我所知」→ 軟性 rewrite 加不確定標記，保留原文內容。

2026-04-20 初始版本
"""

from __future__ import annotations

import re
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 風險模式定義
# ---------------------------------------------------------------------------

# HIGH 風險：具體法條號碼 / 時效數字 / 法院案號
_HIGH_RISK_PATTERNS = [
    # 法律名稱 + 第N條
    r"(?:民法|刑法|刑事訴訟法|民事訴訟法|行政訴訟法|勞動基準法|公司法"
    r"|消費者保護法|著作權法|商標法|專利法|家事事件法|法律扶助法"
    r"|強制執行法|破產法|保險法|票據法|土地法|所得稅法"
    r"|遺產及贈與稅法|稅捐稽徵法|性別工作平等法|勞資爭議處理法"
    r"|個人資料保護法|通訊保障及監察法|行政程序法|國家賠償法"
    r"|消費者債務清理條例|動產擔保交易法"
    r")\s*第\s*\d+",
    # 獨立「第N條」引用（長度限制避免誤傷「第一條街道」）
    r"第\s*[1-9]\d{0,3}\s*條(?:\s*第\s*\d+\s*項)?",
    # 法院案號格式
    r"\d{2,3}\s*年度?\s*\w{1,8}\s*字第\s*\d+\s*號",
    # 時效/期間的具體年月數字
    r"(?:訴訟時效|除斥期間|請求權時效|起訴期間|上訴期間|抗告期間)\s*(?:為|是|有)\s*\d+\s*(?:年|月|日)",
]

# MEDIUM 風險：法律程序詞彙（無具體引用）
_MEDIUM_RISK_PATTERNS = [
    r"(?:上訴|抗告|再審|非常上訴)\s*(?:期間|程序|要件|方式|條件)",
    r"(?:損害賠償|懲罰性賠償)\s*(?:請求|計算|範圍|要件)",
    r"(?:舉證責任|舉證方式|舉證原則)",
    r"(?:訴訟|審判|庭審)\s*(?:程序|流程|步驟|要件)",
    r"(?:聲請|聲請書|聲請狀)\s*(?:要件|規定|方式|條件)",
    r"(?:告訴乃論|非告訴乃論|公訴罪|自訴罪)",
]

# 模糊歸因句式 → 替換字串（tuple 形式，依序比對，找到即替換）
_UNGROUNDED_ATTRIBUTION_REWRITES = [
    # (regex_pattern, replacement)
    (r"根據我的了解[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
    (r"據我所知[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
    (r"以我的理解[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
    (r"我了解(?:的是)?[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
    (r"我知道(?:的是|這個)?[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
    (r"我記得(?:好像|應該)?[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
    (r"根據我的記憶[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
    (r"依我的認識[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
    (r"照我的理解[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
    (r"就我所了解[，,]?\s*", "（以下為 AI 推論，非官方見解，建議查證）"),
]

# 用於從答案中抽取法條引用的 pattern
_LAW_ARTICLE_REF_RE = re.compile(
    r"第\s*([1-9]\d{0,3})\s*條"
)


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def classify_risk(text: str) -> str:
    """
    回傳查詢的幻覺風險等級：'HIGH' / 'MEDIUM' / 'SAFE'

    HIGH   → 強制 COMPLEX tier + 三哲人完整審查
    MEDIUM → 建議走 COMPLEX（含向量記憶引用）
    SAFE   → 允許 SIMPLE tier（閒聊、意見、分析）

    使用方式（在 _classify_query_tier 中）：
        risk = classify_risk(message)
        if risk == "HIGH":
            return "COMPLEX"  # 強制升級
    """
    for pat in _HIGH_RISK_PATTERNS:
        if re.search(pat, text):
            logger.debug("[HallucinationGuard] HIGH risk pattern matched: %s", pat[:40])
            return "HIGH"
    for pat in _MEDIUM_RISK_PATTERNS:
        if re.search(pat, text):
            logger.debug("[HallucinationGuard] MEDIUM risk pattern matched: %s", pat[:40])
            return "MEDIUM"
    return "SAFE"


def check_fact_grounding(
    answer: str,
    context_texts: List[str],
) -> Tuple[bool, List[str]]:
    """
    檢查 answer 中的法條引用是否有在 context_texts 中出現。

    返回：(is_grounded, ungrounded_refs)
    - is_grounded     : True = 所有法條都有溯源，或沒有法條引用
    - ungrounded_refs : 未溯源的法條引用列表（如 ["第184條", "第195條"]）

    設計保守：
    - 只偵測「第N條」格式的直接引用，不包括模糊的「相關法條」
    - 若 context 有此條號（任何法律）即視為溯源，不過度嚴格
    - 空 answer / 空 context → 視為溯源（不觸發誤判）
    """
    if not answer:
        return True, []

    matches = _LAW_ARTICLE_REF_RE.findall(answer)
    if not matches:
        return True, []

    combined_context = " ".join(context_texts) if context_texts else ""
    if not combined_context:
        # 無 context 但有法條引用 → 全部算未溯源
        return False, ["第{}條".format(n) for n in sorted(set(matches), key=int)]

    ungrounded = []
    for article_num in sorted(set(matches), key=int):
        if not re.search(r"第\s*{}\s*條".format(re.escape(article_num)), combined_context):
            ungrounded.append("第{}條".format(article_num))

    return len(ungrounded) == 0, ungrounded


def needs_grounding_check(text: str) -> bool:
    """是否需要做事實溯源檢查（含法條引用時才需要）。"""
    return bool(_LAW_ARTICLE_REF_RE.search(text))


def rewrite_ungrounded_attribution(text: str) -> str:
    """
    軟性 rewrite：把「根據我的了解」等模糊歸因句式替換為不確定標記。
    保留原文其餘內容不變。

    只替換第一個命中（避免多次替換讓句子怪異）。
    """
    out = text
    for pat, replacement in _UNGROUNDED_ATTRIBUTION_REWRITES:
        new_out = re.sub(pat, replacement, out, count=1)
        if new_out != out:
            logger.debug("[HallucinationGuard] Rewrote ungrounded attribution: %s", pat[:40])
            return new_out  # 找到就停，避免過多替換
    return out


def build_anti_hallucination_prompt_rules() -> str:
    """
    回傳應注入 LLM system prompt 的防幻覺規則字串。
    在 chat_casper / ask_casper 的 prompt 中呼叫。
    """
    return (
        "- 【防幻覺規則】若回答包含具體法條號碼（如民法第184條），"
        "必須來自上方[長期記憶]或[網路研究]提供的內容；不可憑 AI 訓練知識引用。\n"
        "- 若不確定某個法律事實，直接說「這個問題需要查證，建議查閱官方來源」，不要猜測。\n"
        "- 禁用模糊歸因句式：「根據我的了解」「據我所知」「我知道」「我記得」。"
        "若要引用資料，必須明說來源（如「根據上方記憶提到的...」「根據 [來源名] ...」）。"
    )
