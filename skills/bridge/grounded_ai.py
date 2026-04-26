import logging
import os
import re
import sys

import requests

from api.model_config import TEXT_PRIMARY_MODEL
from api.session.context_labels import classify_trust_tier, build_trust_system_instruction
from api.session.provenance import parse_source_provenance, render_provenance_badge
from api.verification import format_verification_footer, run_tri_agent_verification, should_trigger_tri_agent, verify_answer
from skills.bridge.http_pool import get_session as _get_session

# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from skills.memory.mem_bridge import (
    recall,
    remember,
    _embedding_cache,
    _cosine_similarity,
    _source_trust_weight,
)
from skills.bridge import melchior_client

# Configuration
try:
    from api.routing.service_registry import get_service_url as _get_svc_url
    _omlx_chat_default = _get_svc_url("omlx_inference") + "/v1/chat/completions"
except Exception:
    _omlx_chat_default = "http://127.0.0.1:8080/v1/chat/completions"
LOCAL_OLLAMA_GENERATE_URL = os.environ.get("CASPER_LOCAL_OLLAMA_URL", _omlx_chat_default)
LOCAL_MODEL_NAME = os.environ.get("CASPER_LOCAL_MODEL", TEXT_PRIMARY_MODEL)
DISTRIBUTED_MODEL_NAME = os.environ.get("CASPER_DISTRIBUTED_MODEL", TEXT_PRIMARY_MODEL)
ENABLE_SELF_CHECK = os.environ.get("CASPER_SELF_CHECK", "0") != "0"
ENABLE_CHAT_SELF_CHECK = os.environ.get("CASPER_CHAT_SELF_CHECK", "0") != "0"
CASPER_LOCAL_FIRST_DEFAULT = os.environ.get("CASPER_LOCAL_FIRST_DEFAULT", "1").strip().lower() in {"1", "true", "yes", "on"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GroundedAI")

# Auto-memorize config
# Default off: assistant replies should not silently become long-term memory.
ENABLE_AUTO_MEMORIZE = os.environ.get("CASPER_AUTO_MEMORIZE", "0") != "0"
_AUTO_MEM_ALLOWED_MODES = {"manual", "explicit"}
_AUTO_MEM_MAX_LEN = 800  # max chars per memory entry (compress long answers)

# ── 閒聊風格提示 ──────────────────────────────────────────────────
_SMALL_TALK_STYLE_HINT = (
    "回答閒聊時，不要用「我沒有味覺」「我是 AI 沒有感受」這類制式否認開頭。"
    "改用自然的轉換語氣，例如：「綠茶確實很受歡迎，清爽回甘的口感讓人放鬆。」"
    "不要假裝有感受，但也不要每次都先聲明自己沒有。"
    "直接回應對方的話題，像朋友聊天一樣自然。"
)

# ── 角色設定幻覺偵測 ──────────────────────────────────────────────
# LLM 有時不回答問題，反而自行生成一大段「CASPER 角色描述」。
# 這些內容若存入記憶會形成正回饋循環，必須攔截。
_PERSONA_HALLUCINATION_KEYWORDS: list[str] = [
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
    "安全第一",
    "法律規範及服務",
    # ── prompt leak patterns: LLM 原封不動吐出 system prompt ──
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
    # ── task hallucination: LLM 假裝正在執行任務而非真正回答 ──
    "正在為",
    "正在尋求",
    "尋求法律援助",
    "法律緊急服務",
    "正在搜尋相關",
    "正在查詢相關",
    "正在連接",
    "正在聯繫",
]
_PERSONA_HALLUCINATION_THRESHOLD = 2  # 命中 >= 2 個即判定（降低門檻防止 prompt 洩漏）
_INTERNAL_BADGE_LEAK_PATTERNS: list[str] = [
    r"\[(?:使用者陳述|已驗證事實|檢索線索|衍生推論)\]",
    r"根據(?:您|你)的?\s*\[(?:使用者陳述|已驗證事實|檢索線索|衍生推論)\]",
    r"關於(?:您|你)的問題，?\s*身為\s*CAS(?:PER)?\b",
    r"身為\s*CAS(?:PER)?\b",
]


def _has_internal_badge_leak(text: str) -> bool:
    s = text or ""
    if not s:
        return False
    return any(re.search(pat, s) for pat in _INTERNAL_BADGE_LEAK_PATTERNS)


def _is_persona_hallucination(text: str) -> bool:
    """偵測 LLM 回覆是否為角色設定幻覺（非正常回答）。"""
    if not text:
        return False
    if _has_internal_badge_leak(text):
        return True
    hits = sum(1 for kw in _PERSONA_HALLUCINATION_KEYWORDS if kw in text)
    return hits >= _PERSONA_HALLUCINATION_THRESHOLD


def _is_garbage_output(text: str) -> bool:
    """偵測 LLM 回覆是否為亂碼/垃圾輸出。
    典型特徵：大量無意義數字、重複標點、極低中文/英文字元比例。
    """
    if not text or len(text) < 20:
        return False
    # 移除空白後檢查
    stripped = text.replace(" ", "").replace("\n", "")
    if not stripped:
        return True
    # 計算有意義字元比例（中文 + ASCII 字母）
    meaningful = sum(1 for c in stripped if '\u4e00' <= c <= '\u9fff' or c.isalpha())
    ratio = meaningful / len(stripped)
    # 正常中文回覆 meaningful ratio 通常 > 0.4
    # 亂碼如 "03. - 032. - 0034) 0。-00。" ratio 極低
    if ratio < 0.15 and len(stripped) > 30:
        return True
    # 重複片段偵測：將文本分成 10 字元的 chunk，若超過 60% 重複則為垃圾
    if len(stripped) > 60:
        chunks = [stripped[i:i+10] for i in range(0, len(stripped)-9, 10)]
        if chunks and len(set(chunks)) / len(chunks) < 0.4:
            return True
    return False


def _is_parrot_response(query: str, answer: str) -> bool:
    """偵測 LLM 是否只是複述問題而非真正回答（鸚鵡式回覆）。
    只攔截明顯的「照抄問題 + 空殼模板」，避免誤殺正常對話。
    """
    if not query or not answer:
        return False
    q = query.strip()
    a = answer.strip()
    # 回答夠長就不是鸚鵡（超過問題長度 3 倍以上）
    if len(a) > len(q) * 3:
        return False
    # 回答必須幾乎完全由問題原文組成才算鸚鵡
    # 移除問題原文後，檢查剩餘是否只剩模板碎片
    if q not in a:
        return False
    remainder = a.replace(q, "", 1).strip()
    # 移除常見模板碎片
    for frag in ("記憶依據：", "記憶依據：...", "...", "？", "。", "！", "\n", "："):
        remainder = remainder.replace(frag, "")
    remainder = remainder.strip()
    # 只有剩餘內容極少（幾乎沒有新資訊）才判定為鸚鵡
    if len(remainder) < 10:
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════
# 三層反幻覺系統 (Three-Layer Anti-Hallucination)
# Layer 3: Query Tier Classification
# Layer 1: Statute / Context Noise Filtering
# Layer 2: Semantic Coherence Check
# ══════════════════════════════════════════════════════════════════════════

_GREETING_PATTERNS: list[str] = [
    "你好", "早安", "午安", "晚安", "嗨", "hi", "hello", "hey",
    "good morning", "good afternoon", "good evening", "good night",
    "哈囉", "安安", "嘿", "yo", "哈嘍", "您好",
]

_GREETING_RESPONSES: list[str] = [
    "你好！有什麼我能幫你的嗎？",
    "嗨！今天過得如何？有什麼需要協助的嗎？",
    "你好！我是 CASPER，隨時為你服務。",
    "哈囉！需要我幫忙什麼嗎？",
]

# ── Embedding-based tier classification ──────────────────────────────
# Anchor sentences for each tier. The classifier embeds the query and
# computes cosine similarity against these anchors, picking the tier
# with the highest max-similarity. This avoids keyword false positives
# like "解釋為什麼天空是藍的" being misclassified as COMPLEX.
_TIER_ANCHORS: dict[str, list[str]] = {
    "COMPLEX": [
        # 法律實體
        "民法第184條侵權行為損害賠償",
        "刑法詐欺罪構成要件",
        "請分析這份判決的法律見解",
        "這個案件的訴訟策略是什麼",
        "法扶開辦結案報結流程",
        "強制執行聲請裁定抗告",
        "契約違約損害賠償請求權",
        "被告原告上訴理由",
        "法條適用與法院實務見解",
        "律師委任狀收文遞狀",
        "消費者債務清理更生方案",
        "離婚監護權扶養費",
    ],
    "SIMPLE": [
        # 日常對話、生活問題
        "今天天氣如何",
        "晚餐吃什麼好",
        "幫我比較這兩家餐廳",
        "解釋為什麼天空是藍的",
        "推薦一部好看的電影",
        "這個週末有什麼活動",
        "我的行程是什麼",
        "幫我翻譯這段英文",
        "你覺得怎麼樣",
        "提醒我明天開會",
        "最近有什麼新聞",
        "幫我算一下這個數字",
        "今天有什麼行程安排",
        "這個文件幫我翻譯一下",
        "明天幾點要開會",
        "幫我查一下地址",
    ],
}

# Lazy cache for tier anchor embeddings (computed once on first call)
_tier_anchor_embeddings: dict[str, list[tuple]] = {}
_TIER_ANCHOR_LOCK = __import__("threading").Lock()

# Threshold: query must score >= this against COMPLEX anchors to be COMPLEX.
# Below this → SIMPLE. This prevents borderline queries from triggering full pipeline.
_COMPLEX_TIER_THRESHOLD = 0.55
# Margin: COMPLEX max-similarity must exceed SIMPLE max-similarity by this amount.
# Prevents borderline queries like "幫我翻譯" (C=0.64 vs S=0.63) from being COMPLEX.
_COMPLEX_MARGIN = 0.05


def _get_tier_anchor_embeddings() -> dict[str, list[tuple]]:
    """Lazy-init: embed all tier anchors once and cache."""
    if _tier_anchor_embeddings:
        return _tier_anchor_embeddings
    with _TIER_ANCHOR_LOCK:
        if _tier_anchor_embeddings:
            return _tier_anchor_embeddings
        for tier, anchors in _TIER_ANCHORS.items():
            embs = []
            for anchor in anchors:
                try:
                    emb = _embedding_cache(anchor)
                    if any(v != 0.0 for v in emb[:5]):
                        embs.append(emb)
                except Exception:
                    pass
            _tier_anchor_embeddings[tier] = embs
        logger.info("🏷️ Tier anchor embeddings cached: %s",
                     {k: len(v) for k, v in _tier_anchor_embeddings.items()})
        return _tier_anchor_embeddings


def _classify_query_tier(message: str) -> str:
    """Layer 3: 將查詢分類為 GREETING / SIMPLE / COMPLEX 三級。
    GREETING → 模板回覆（不呼叫 LLM）
    SIMPLE   → 輕量推理（top_k=1, temp=0.3, 不載入法條記憶）
    COMPLEX  → 完整管線（top_k=4, temp=0.25, 法條需 score≥0.65）

    分級策略：
    1. GREETING: 短文 + 招呼詞命中 → 直接判定（不需 embedding）
    2. COMPLEX vs SIMPLE: embedding cosine similarity 比對 anchor vectors
       取 max similarity，COMPLEX 需 ≥ _COMPLEX_TIER_THRESHOLD 才成立
    """
    msg = (message or "").strip().lower()

    # ── 強制三哲人模式：使用者加上「三哲人」「深度」「驗證」觸發詞 → COMPLEX ──
    _force_complex_triggers = ("三哲人", "深度分析", "深度驗證", "完整驗證", "仔細想")
    if any(t in msg for t in _force_complex_triggers):
        logger.info("🏷️ Tier: COMPLEX (forced by trigger word)")
        return "COMPLEX"

    # ── Fast path: GREETING（短問候不需 embedding）──
    if len(msg) < 15 and any(g in msg for g in _GREETING_PATTERNS):
        return "GREETING"

    # ── Embedding-based classification ──
    try:
        q_emb = _embedding_cache(msg[:200])
        # Check if embedding is valid (non-zero)
        if not any(v != 0.0 for v in q_emb[:5]):
            # Embedding server down — fallback to SIMPLE (safe default)
            logger.debug("Tier classification: embedding zero, defaulting to SIMPLE")
            return "SIMPLE"

        anchors = _get_tier_anchor_embeddings()
        # Compute max similarity for each tier
        tier_scores: dict[str, float] = {}
        for tier, embs in anchors.items():
            tier_scores[tier] = max(
                (_cosine_similarity(q_emb, emb) for emb in embs),
                default=0.0,
            )

        c_score = tier_scores.get("COMPLEX", 0.0)
        s_score = tier_scores.get("SIMPLE", 0.0)

        # COMPLEX must: (1) clear absolute threshold, (2) win by margin over SIMPLE
        if c_score >= _COMPLEX_TIER_THRESHOLD and c_score > s_score + _COMPLEX_MARGIN:
            logger.debug("🏷️ Tier: COMPLEX (C=%.3f, S=%.3f, margin=%.3f)",
                         c_score, s_score, c_score - s_score)
            return "COMPLEX"

        logger.debug("🏷️ Tier: SIMPLE (C=%.3f, S=%.3f)", c_score, s_score)
        # ── 防幻覺升級：HIGH-risk 查詢強制走 COMPLEX ──
        # 即使 embedding 判定為 SIMPLE，含具體法條引用或程序時效的查詢
        # 必須走完整三哲人審查，不允許跳過驗證直接回傳 LLM 輸出。
        try:
            from api.hallucination_guard import classify_risk as _classify_risk
            _risk = _classify_risk(message)
            if _risk == "HIGH":
                logger.info(
                    "🛡️ Tier override: SIMPLE→COMPLEX "
                    "(HIGH-risk factual query, hallucination guard)"
                )
                return "COMPLEX"
            if _risk == "MEDIUM":
                logger.info(
                    "🛡️ Tier upgrade: SIMPLE→COMPLEX "
                    "(MEDIUM-risk legal query, upgrading for verification)"
                )
                return "COMPLEX"
        except Exception as _hg_err:
            logger.debug("[HG] risk classify skipped: %s", _hg_err)
        return "SIMPLE"

    except Exception as e:
        logger.warning("Tier classification failed, defaulting to SIMPLE: %s", e)
        return "SIMPLE"


def _filter_statute_memories(memories: list[dict], tier: str) -> list[dict]:
    """Layer 1: 依查詢等級過濾法條 / 噪音記憶。
    - GREETING/SIMPLE: 移除所有 statute 來源
    - COMPLEX: 保留 statute 但要求 score ≥ 0.65
    """
    if tier in ("GREETING", "SIMPLE"):
        return [m for m in memories
                if "statute" not in str(m.get("source") or "").lower()]
    # COMPLEX: keep statute only if score is high enough
    filtered = []
    for m in memories:
        src = str(m.get("source") or "").lower()
        if "statute" in src:
            if (m.get("score") or 0) >= 0.65:
                filtered.append(m)
        else:
            filtered.append(m)
    return filtered


def _is_incoherent_response(query: str, answer: str, threshold: float = 0.25) -> bool:
    """Layer 2: 語義一致性檢查 — 用 cosine similarity 比對 query↔answer。
    若相似度低於 threshold，判定為不連貫回覆（LLM 跑題）。
    """
    if not query or not answer or len(answer.strip()) < 20:
        return False
    try:
        q_emb = _embedding_cache(query[:200])
        a_emb = _embedding_cache(answer[:500])
        sim = _cosine_similarity(q_emb, a_emb)
        if sim < threshold:
            logger.warning("⚠️ Incoherent response (cosine=%.3f < %.2f)", sim, threshold)
            return True
        return False
    except Exception as e:
        logger.debug("Coherence check skipped: %s", e)
        return False


# ── 實體幻覺偵測（Entity Hallucination Detection）──────────────────
# 檢查 LLM 回覆中提到的法條編號、案號是否真的出現在輸入 context 裡。
# 如果回覆憑空捏造法條，這裡會攔截。
_RE_LAW_ARTICLE = re.compile(
    r"(?:民法|刑法|民事訴訟法|刑事訴訟法|行政訴訟法|公司法|勞基法|勞動基準法|"
    r"消費者保護法|消保法|家事事件法|強制執行法|土地法|所得稅法|"
    r"道路交通管理處罰條例|社會救助法|性別平等工作法|個人資料保護法|個資法)"
    r"第\s*(\d+(?:-\d+)?)\s*條",
)
_RE_CASE_NUMBER = re.compile(
    r"\d{2,3}\s*年度?\s*[\u4e00-\u9fff]{1,4}\s*字\s*第?\s*\d+\s*號",
)


def _has_entity_hallucination(answer: str, context: str) -> bool:
    """檢查回覆中的法條/案號是否存在於 context（記憶 + query）。
    只在回覆中出現法律實體但 context 完全沒有對應時才判定幻覺。
    """
    if not answer:
        return False
    # 抽取回覆中的法條引用
    answer_articles = set(_RE_LAW_ARTICLE.findall(answer))
    answer_cases = set(_RE_CASE_NUMBER.findall(answer))

    # 沒有引用法律實體 → 不需檢查
    if not answer_articles and not answer_cases:
        return False

    # context = query + memory_context 合併
    ctx = context or ""

    # 檢查法條：回覆提到的法條是否至少部分出現在 context 裡
    if answer_articles:
        ctx_articles = set(_RE_LAW_ARTICLE.findall(ctx))
        fabricated = answer_articles - ctx_articles
        if fabricated and not ctx_articles:
            # context 完全沒有法條，但回覆憑空引用 → 高度疑似幻覺
            logger.warning("⚠️ Entity hallucination: fabricated articles %s (no articles in context)", fabricated)
            return True
        # context 有一些法條但回覆多引了 → 只在比例很高時才判定
        if fabricated and len(fabricated) > len(ctx_articles) + 1:
            logger.warning("⚠️ Entity hallucination: fabricated articles %s exceeds context %s", fabricated, ctx_articles)
            return True

    # 檢查案號：回覆提到案號但 context 完全沒有
    if answer_cases:
        ctx_cases = set(_RE_CASE_NUMBER.findall(ctx))
        fabricated_cases = answer_cases - ctx_cases
        if fabricated_cases and not ctx_cases:
            logger.warning("⚠️ Entity hallucination: fabricated case numbers %s", fabricated_cases)
            return True

    return False


def _auto_remember(query: str, answer: str, mode: str = "chat"):
    """
    Asynchronously store a Q+A pair into the vector DB.
    Runs in a background thread to avoid blocking the response.
    """
    if not ENABLE_AUTO_MEMORIZE:
        return
    if mode not in _AUTO_MEM_ALLOWED_MODES:
        return
    if not query or not answer or len(answer.strip()) < 10:
        return
    # 攔截角色設定幻覺，避免污染記憶庫
    if _is_persona_hallucination(answer):
        logger.warning("⛔ Auto-memorize blocked: persona hallucination detected")
        return
    # 攔截亂碼/垃圾輸出，避免污染記憶庫形成回饋迴圈
    if _is_garbage_output(answer):
        logger.warning("⛔ Auto-memorize blocked: garbage output detected")
        return
    # 攔截鸚鵡式回覆：LLM 只是複述問題而非真正回答
    if _is_parrot_response(query, answer):
        logger.warning("⛔ Auto-memorize blocked: parrot response detected")
        return
    # 攔截 codebase-ingest 殘留污染：回答中包含自動內化標記
    if "codebase-ingest" in answer or "[CODE內化]" in answer:
        logger.warning("⛔ Auto-memorize blocked: codebase-ingest leak in answer")
        return
    # 攔截系統通知/制式輸出：這些不是有意義的對話，不需存入記憶
    _sys_noise = ["殭屍巡邏報告", "Zombie Patrol", "Image Generated", "已輸出排版良好的翻譯",
                  "已輸出 PDF 摘要", "已輸出雙語對照", "重試摘要佇列"]
    if any(n in answer for n in _sys_noise):
        logger.debug("Auto-memorize skipped: system notification output")
        return

    import threading
    from datetime import datetime

    def _do_remember():
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            # Compress: keep query + truncated answer
            q_short = query[:200].strip()
            a_short = answer[:_AUTO_MEM_MAX_LEN].strip()
            content = f"[Q] {q_short}\n[A] {a_short}"
            source = f"assistant_generated|mode={mode}|ts={ts}"
            ok = remember(
                content,
                source=source,
                metadata={
                    "verified": False,
                    "confidence": 0.10,
                    "derived_from": "assistant_reply",
                    "role": "assistant",
                },
            )
            if ok:
                logger.debug(f"Auto-memorized: {q_short[:50]}...")
        except Exception as e:
            logger.warning(f"Auto-memorize failed (non-blocking): {e}")

    threading.Thread(target=_do_remember, daemon=True).start()


def _format_memories(memories, *, query: str = ""):
    if not memories:
        return "無相關記憶。"
    lines = []
    for idx, m in enumerate(memories, 1):
        score = m.get("score")
        score_str = f"{score:.4f}" if isinstance(score, (int, float)) else "n/a"
        source = str(m.get("source", "unknown"))
        prov = parse_source_provenance(source)
        tier = classify_trust_tier(
            source_type=prov.source_type,
            verified=prov.verified,
            confidence=prov.confidence,
            derived_from=prov.derived_from,
            role=prov.role,
        )
        lines.append(
            f"{idx}. {tier.badge} {m.get('content', '')} "
            f"(信心: {prov.confidence:.2f}, 相關度: {score_str})"
        )
    return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """
    Estimate token count for mixed CJK / Latin text.
    CJK characters ≈ 1.5 tokens each; Latin ≈ 1 token per ~4 chars.
    """
    if not text:
        return 0
    cjk = 0
    latin_chars = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
                or 0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F
                or 0xFF00 <= cp <= 0xFFEF):
            cjk += 1
        else:
            latin_chars += 1
    return int(cjk * 1.5) + (latin_chars // 4) + 1


def _summarize_memories_if_needed(query: str, memories: list, max_tokens: int = 1200) -> str:
    """
    If the combined memory text exceeds the token budget, truncate by recall
    score order.  Uses token estimation instead of raw character count for
    better accuracy with mixed CJK/Latin text.

    Memories are already sorted by relevance (recall score), so we just take
    the top entries that fit within max_tokens.  No LLM needed — saves ~15s
    and frees the inference slot for actual conversation.
    """
    if not memories:
        return "無相關記憶。"

    raw_text = _format_memories(memories, query=query)
    if _estimate_tokens(raw_text) <= max_tokens:
        return raw_text

    logger.info(
        "Memory context too long (~%d tokens, budget %d). Truncating by relevance...",
        _estimate_tokens(raw_text), max_tokens,
    )
    # Memories are already ranked by recall score — take top N that fit
    truncated = []
    tokens_used = 0
    for idx, m in enumerate(memories, 1):
        content = m.get("content", "")
        score = m.get("score")
        score_str = f"{score:.4f}" if isinstance(score, (int, float)) else "n/a"
        source = str(m.get("source", "unknown"))
        prov = parse_source_provenance(source)
        tier = classify_trust_tier(
            source_type=prov.source_type,
            verified=prov.verified,
            confidence=prov.confidence,
            derived_from=prov.derived_from,
            role=prov.role,
        )
        entry = (
            f"{idx}. {tier.badge} {content} "
            f"(信心: {prov.confidence:.2f}, 相關度: {score_str})"
        )
        entry_tokens = _estimate_tokens(entry)
        if tokens_used + entry_tokens > max_tokens:
            break
        truncated.append(entry)
        tokens_used += entry_tokens
    return "\n".join(truncated) or "無相關記憶。"


def _wants_chatlog(query: str) -> bool:
    q = (query or "").lower()
    markers = [
        "回顧", "回憶", "之前說", "你記得", "對話記錄", "聊天紀錄", "聊天記錄",
        "what did i say", "remember what i said", "conversation log",
    ]
    return any(m.lower() in q for m in markers)


def _filter_chatlog_memories(query: str, memories: list[dict]) -> list[dict]:
    """
    Chatlog is useful only when the user explicitly asks to recall past conversation.
    By default, suppress it to avoid polluting long-term knowledge retrieval.
    """
    mems = [m for m in (memories or []) if isinstance(m, dict)]
    # 過濾垃圾記憶，避免污染 context
    mems = [m for m in mems if not _is_garbage_output(str(m.get("content") or ""))]
    if _wants_chatlog(query):
        return mems
    non_chat = [m for m in mems if "chatlog|" not in str(m.get("source") or "").lower()]
    # Filter out codebase-ingest memories for casual/non-technical queries
    _tech_markers = ["code", "程式", "函數", "function", "class", "module", "import", "bug", "error", "api"]
    _q_lower = query.lower()
    if not any(t in _q_lower for t in _tech_markers):
        non_chat = [m for m in non_chat
                    if "codebase-ingest" not in str(m.get("source") or "")
                    and "codebase-ingest" not in str(m.get("context") or "")]
    trusted = [m for m in non_chat if _source_trust_weight(str(m.get("source") or ""), query=query) >= 0.5]
    return trusted or non_chat or mems


def _should_skip_recall_for_chat(query: str, tier: str) -> bool:
    """
    SIMPLE tier casual chat should not pay the full FAISS/memory cost.
    Keep recall only when the user explicitly asks to remember past messages
    or asks a factual question that may benefit from memory grounding.
    """
    if str(tier or "").upper() != "SIMPLE":
        return False
    if _wants_chatlog(query):
        return False
    if _needs_research(query) or _is_factual_question(query):
        return False
    return True


def is_small_talk_intent(query: str, tier: str) -> bool:
    """
    Check if query is purely casual small talk without tasks or tool requirements.
    Used to skip both semantic routing and memory grounding for maximum latency reduction.
    """
    if str(tier or "").upper() != "SIMPLE":
        return False
        
    query_lower = (query or "").lower()
    
    # Check for task verbs / tool commands
    task_verbs = [
        "翻譯", "幫我", "列出", "搜尋", "查詢", "追蹤", "整理", "分析",
        "總結", "摘要", "找一下", "寫", "生成", "做", "建立"
    ]
    if any(v in query_lower for v in task_verbs):
        return False
        
    # Check for legal entities (law articles / case numbers)
    if _RE_LAW_ARTICLE.search(query) or _RE_CASE_NUMBER.search(query):
        return False
        
    # Check if factual/research
    if _needs_research(query) or _is_factual_question(query):
        return False
        
    return True



def _needs_research(text):
    lowered = (text or "").lower()

    # 使用者明確要求上網查詢 → 一律觸發
    explicit_web = ["上網", "幫我搜", "google", "網路上", "搜尋一下", "查一下最新"]
    if any(k in lowered for k in explicit_web):
        return True

    # Tightened: only trigger web research for clearly external/dynamic queries.
    # Removed overly broad "是什麼", "how to", "查詢" which cause false positives on casual chat.
    dynamic_keywords = [
        "最新", "news", "today", "recent", "價格", "匯率", "股價",
        "誰是", "what is", "who is", "搜尋",
        "最近發生", "最新新聞", "今日",
        "天氣", "氣溫", "溫度", "weather", "forecast",
        "上網", "網路查", "幫我查", "查一下",
        "現在", "目前",
        # 天氣相關補充（2026-04-20：「下雨」等詞不含「天氣」但需搜尋）
        "下雨", "下雪", "颱風", "降雨", "會不會下", "天晴", "陰天", "晴天",
        "氣象", "預報", "降雪", "豪雨", "颳風", "大風", "濕度",
    ]
    return any(k in lowered for k in dynamic_keywords)


def _is_weather_query(text: str) -> bool:
    """偵測是否為天氣/氣象類查詢，用於強制標示資料來源並推薦 CWA。"""
    lowered = (text or "").lower()
    weather_signals = [
        "天氣", "氣溫", "溫度", "weather", "forecast", "下雨", "下雪",
        "颱風", "降雨", "天晴", "陰天", "晴天", "氣象", "預報", "降雪",
        "豪雨", "颳風", "大風", "濕度", "會下", "會不會下",
    ]
    return any(k in lowered for k in weather_signals)


def _is_factual_question(text: str) -> bool:
    """判斷是否為需要事實回答的問題（而非閒聊）。
    用於在記憶不足時決定是否上網搜尋。"""
    factual_signals = [
        "什麼", "多少", "幾", "哪", "怎麼", "如何", "為什麼",
        "是否", "有沒有", "能不能",
        "天氣", "溫度", "新聞", "價格", "時間",
        "誰是", "what", "how", "who", "when", "where", "why",
    ]
    lowered = (text or "").lower()
    return any(k in lowered for k in factual_signals)


def _generate_local(prompt, temperature=0.4, timeout=90, num_ctx=6144, stream=False):
    """Generate text locally via oMLX.

    Args:
        stream: If True, return a generator that yields text chunks instead of
                a single string. Default False preserves original behavior.
    """
    if stream:
        # Streaming path: delegate to melchior_client.chat_stream()
        chat_stream_fn = getattr(melchior_client, "chat_stream", None)
        if callable(chat_stream_fn):
            return chat_stream_fn(prompt, model=LOCAL_MODEL_NAME, timeout=max(10, timeout))
        # If chat_stream not available, fall back to non-streaming and wrap
        result = _generate_local(prompt, temperature=temperature, timeout=timeout, num_ctx=num_ctx, stream=False)
        def _single_yield(text):
            yield text
        return _single_yield(result)

    # Try oMLX first (faster on Apple Silicon, TAIDE-12b primary)
    try:
        omlx_chat = getattr(melchior_client, "_chat_omlx", None)
        omlx_avail = getattr(melchior_client, "_omlx_available", None)
        if callable(omlx_chat) and callable(omlx_avail) and omlx_avail():
            r = omlx_chat(prompt=prompt, temperature=temperature, timeout=max(10, timeout), max_tokens=2048)
            if r.get("success") and r.get("response"):
                return r["response"].strip()
    except Exception as e:
        logger.debug("oMLX fallthrough: %s", e)

    # Fallback: call oMLX /v1/chat/completions directly (OpenAI-compatible format)
    payload = {
        "model": LOCAL_MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 2048,
        "stream": False,
    }
    response = _get_session().post(LOCAL_OLLAMA_GENERATE_URL, json=payload, timeout=timeout)
    if response.status_code != 200:
        logger.warning(f"LLM Error {response.status_code}: {response.text[:200]}")
        raise RuntimeError(f"LLM Error {response.status_code}")
    data = response.json()
    # OpenAI-compatible response format
    choices = data.get("choices") or []
    if choices:
        return (choices[0].get("message") or {}).get("content", "").strip()
    # Legacy Ollama format fallback
    return data.get("response", "").strip()


def _generate_remote(prompt, timeout=90):
    # Dynamic routing: prefer Melchior /v1 (distributed) when reachable, otherwise fall back.
    result = melchior_client.smart_chat(prompt, model_hint=DISTRIBUTED_MODEL_NAME, timeout=max(45, timeout), quality="high")
    if result.get("success") and result.get("response"):
        return result.get("response", "").strip()
    raise RuntimeError(result.get("error") or "Distributed backend returned empty response")


def _generate(prompt, temperature=0.4, timeout=90, num_ctx=6144):
    """
    Hybrid generation path:
    - Default: local first, then remote fallback.
    - If local-first disabled and mode is distributed: remote first.
    """
    mode = "unknown"
    try:
        from skills.brain_manager.action import get_brain_mode
        mode = get_brain_mode()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 313, exc_info=True)

    errors = []
    remote_first = (mode == "distributed") and (not CASPER_LOCAL_FIRST_DEFAULT)

    if remote_first:
        try:
            return _generate_remote(prompt, timeout=timeout)
        except Exception as e:
            errors.append(f"remote={e}")
            logger.warning(f"Distributed backend failed, falling back to local: {e}")

    try:
        return _generate_local(prompt, temperature=temperature, timeout=timeout, num_ctx=num_ctx)
    except Exception as e:
        errors.append(f"local={e}")

    try:
        return _generate_remote(prompt, timeout=timeout)
    except Exception as e:
        errors.append(f"remote={e}")

    raise RuntimeError(" ; ".join(errors) if errors else "No backend available")


def _self_check_answer(query, answer, memory_context, web_context, conversation_history=""):
    """
    Lightweight second-pass validator for factual coherence.
    Returns revised answer if needed, otherwise original.
    """
    if not ENABLE_SELF_CHECK:
        return answer

    # Skip self-check for short, low-risk answers
    if not answer or len(answer) < 80:
        return answer

    # Compress contexts to save tokens — self-check only needs key facts
    # Token-aware truncation: ~250 tokens for memory, ~150 for web
    _mem = memory_context or "無"
    _web = web_context or "無"
    mem_brief = _mem[:500] if _estimate_tokens(_mem) <= 250 else _mem[:350]
    web_brief = _web[:300] if _estimate_tokens(_web) <= 150 else _web[:200]

    check_prompt = f"""檢查回答是否超出可用上下文。

[記憶] {mem_brief}
[研究] {web_brief}
[問題] {query[:200]}
[回答] {answer[:400]}

一致 → OK。有幻覺 → REVISE + 修正版（繁體中文純文字）。
"""
    try:
        verdict = _generate(check_prompt, temperature=0.1, timeout=35, num_ctx=4096)
        if verdict.strip().upper().startswith("OK"):
            return answer
        if verdict.strip().upper().startswith("REVISE"):
            parts = verdict.split("\n", 1)
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()
        return answer
    except Exception as e:
        logger.warning(f"Self-check skipped due to error: {e}")
        return answer


def _verify_and_repair_answer(
    *,
    query: str,
    answer: str,
    prompt: str,
    memories: list[dict],
    memory_context: str,
    web_context: str,
    conversation_history: str,
    entity_context: str,
) -> str:
    report = run_tri_agent_verification(
        query=query,
        draft_answer=answer,
        memories=memories,
        memory_context=memory_context,
        web_context=web_context,
        conversation_history=conversation_history,
        generate=lambda repair_prompt: _generate(repair_prompt, temperature=0.12, timeout=90, num_ctx=6144),
    )
    if report.passed:
        return report.final_answer

    logger.warning("⛔ verification rejected answer: %s", report.critic_reason)
    repair_prompt = (
        f"{prompt}\n\n"
        "[回答修正要求]\n"
        f"- 上一版回答已被判定不可靠，原因：{report.critic_reason}\n"
        "- 若無法驗證，直接說目前沒有可驗證結果。\n"
        "- 不要聲稱使用者以前給過、說過、提供過任何目前上下文裡不存在的內容。\n"
        "- 不要把未驗證記憶或摘要寫成確定事實。\n"
        "- 直接輸出修正版繁體中文回答。\n"
    )
    try:
        repaired = _generate(repair_prompt, temperature=0.12, timeout=90, num_ctx=6144)
        repaired = str(repaired or "").strip()
        if repaired and not _is_parrot_response(query, repaired) and not _is_persona_hallucination(repaired):
            if not _has_entity_hallucination(repaired, entity_context):
                repaired_check = verify_answer(
                    query=query,
                    answer=repaired,
                    memories=memories,
                    memory_context=memory_context,
                    web_context=web_context,
                    conversation_history=conversation_history,
                )
                if repaired_check.passed:
                    return repaired
    except Exception as exc:
        logger.warning("Repair pass skipped: %s", exc)

    return report.final_answer


def ask_casper(query, conversation_history="", force_research=False):
    """
    Grounded response generation with memory-first context + anti-hallucination layers.
    """
    logger.info(f"🤔 Thinking about: {query}")

    # ── Layer 3: Query Classification ──
    tier = _classify_query_tier(query)
    logger.info("🏷️ ask_casper tier: %s", tier)

    _top_k = 2 if tier == "SIMPLE" else 4
    memories = _filter_chatlog_memories(query, recall(query, top_k=_top_k))
    # ── Layer 1: Statute / Noise Filter ──
    memories = _filter_statute_memories(memories, tier)
    # Compress context if too large to avoid LLM context overflow
    memory_context = _summarize_memories_if_needed(query, memories, max_tokens=1200)

    web_context = "無。"
    # 智慧搜尋觸發：明確關鍵字、force、或「記憶不足 + 事實問題」才上網
    memories_insufficient = len(memories) == 0
    should_research = (
        force_research
        or _needs_research(query)
        or (memories_insufficient and _is_factual_question(query))
    )
    if should_research:
        try:
            from skills.research.web_research import research_topic

            reason = "force" if force_research else ("factual+no_mem" if memories_insufficient else "keyword")
            logger.info(f"🌐 ask_casper auto-research ({reason})")
            search_res = research_topic(query, depth=2)
            if search_res.get("sources"):
                _src_titles = [s.get("title", "").split("|")[0].strip() for s in search_res["sources"][:2]]
                _src_note = f"（資料來源：{', '.join(t for t in _src_titles if t)}）"
                if _is_weather_query(query):
                    _src_note += " ⚠️ 天氣資料非即時精確，請以中央氣象署（CWA）為準：https://www.cwa.gov.tw/"
                web_context = _src_note + "\n" + search_res.get("combined_content", "")[:3500]
        except Exception as e:
            logger.warning(f"Web research failed in ask_casper: {e}")

    _ask_weather_rule = ""
    if _is_weather_query(query):
        _ask_weather_rule = (
            "\n7. 【天氣規則】回答天氣時必須明確說明資料來源，"
            "並提醒使用者「以中央氣象署（CWA）為準」。不得憑空給出降雨機率或溫度數字。"
        )

    prompt = f"""
你是 CASPER（MAGI-01），負責穩定、可追溯、低幻覺的回答。

[回答規則]
1. 優先使用「長期記憶」；若不足再使用「網路研究」。
2. 如果資訊不足，直接說「目前記憶與資料不足」，並指出缺口。
3. 禁止編造細節。
4. 回覆語言必須是繁體中文。
5. 若使用者糾正您的錯誤或補充新資訊，請【務必明確總結並覆誦正確的資訊】，這會幫助系統將正確知識寫入長期記憶。
6. 記憶中的「證據等級」若是「已驗證」才能當成事實；「原始對話」與「衍生線索」只能當線索，不可硬寫成確定事實。
7. 若回答引用網路搜尋資料，必須說出資料來源名稱，不可用「根據我目前找到的資訊」等模糊說法。{_ask_weather_rule}

[長期記憶]
{memory_context}

[網路研究]
{web_context}

[近期對話]
{conversation_history or "無"}

[使用者問題]
{query}

[輸出格式]
- 先給 2-6 句直接回答。
- 如有引用記憶，最後補一行「記憶依據：...」。
- 不要使用 Markdown 語法（**粗體**、`程式碼`、### 標題等）。純文字即可。
"""

    try:
        answer = _generate(prompt, temperature=0.25, timeout=180, num_ctx=6144)
        answer = answer or "目前暫時無法產生可靠回覆。"
        # 攔截鸚鵡式回覆
        if _is_parrot_response(query, answer):
            logger.warning("⛔ ask_casper: parrot response detected, retrying once")
            answer = _generate(prompt, temperature=0.3, timeout=120, num_ctx=6144)
            if not answer or _is_parrot_response(query, answer):
                return "目前記憶與資料不足，無法回答這個問題。請提供更多細節。"
        # ── Layer 2: Semantic Coherence Check ──
        if _is_incoherent_response(query, answer):
            logger.warning("⛔ ask_casper: incoherent response, retrying with lower temp")
            answer = _generate(prompt, temperature=0.15, timeout=120, num_ctx=6144)
            if not answer or _is_incoherent_response(query, answer):
                return "目前記憶與資料不足，無法提供與問題相關的回答。請提供更多細節。"
        # ── Entity Hallucination Check ──
        _entity_ctx = f"{query}\n{memory_context}"
        if _has_entity_hallucination(answer, _entity_ctx):
            logger.warning("⛔ ask_casper: entity hallucination (fabricated law articles/case numbers)")
            answer = _generate(prompt, temperature=0.1, timeout=120, num_ctx=6144)
            if not answer or _has_entity_hallucination(answer, _entity_ctx):
                return "我的回覆中引用了無法確認的法條或案號，為避免誤導，請您確認具體法條後再詢問。"
        # SIMPLE tier: skip heavy verification to reduce latency (~60s → ~10s)
        # Only COMPLEX queries (legal analysis, case strategy) need Tri-Agent
        if tier == "SIMPLE":
            return answer
        final = _self_check_answer(
            query=query,
            answer=answer,
            memory_context=memory_context,
            web_context=web_context,
            conversation_history=conversation_history,
        )
        return _verify_and_repair_answer(
            query=query,
            answer=final,
            prompt=prompt,
            memories=memories,
            memory_context=memory_context,
            web_context=web_context,
            conversation_history=conversation_history,
            entity_context=_entity_ctx,
        )
    except Exception as e:
        logger.error(f"❌ LLM Error: {e}")
        return "系統忙碌中，暫時無法完成推理。"


def chat_casper(message, conversation_history="", heavy: bool = False):
    """
    General conversational interface with 3-layer anti-hallucination.
    Layer 3: Query tier → GREETING / SIMPLE / COMPLEX
    Layer 1: Statute / noise memory filter per tier
    Layer 2: Semantic coherence check on LLM output

    Layer 0 (P1-2, 2026-04-19): @heavy / @重型 前綴 → 直接走 NVIDIA NIM 405B，跳過本地 oMLX
    """
    logger.info(f"💬 Chatting: {message}")

    # ── Layer 0: @heavy opt-in → 直接走 NIM 405B（P1-2 根修 2026-04-19）──
    # 此為 chat_casper 主要入口，處理所有 /osc/external/chat → _handle_chat_async 路徑。
    # 必須在這一層接 @heavy，因為 chat_casper 不會走 inference_gateway._chat_inner 的 heavy fast path。
    _msg_stripped = str(message or "").strip()
    # 2026-04-24：case-insensitive（@HEAVY / @Heavy 都接受）；全形 ＠ 已在 orchestrator sanitize 統一轉半形
    _msg_lower_head = _msg_stripped.lower()
    # 2026-04-24：三保險偵測（prefix / flask.g / explicit kwarg）
    # - prefix：上游尚未剝除時直接命中
    # - flask.g：同 request thread 設定，但 ThreadPoolExecutor 子 thread 讀不到
    # - heavy kwarg：caller（chat_pipeline）在 main thread 讀完 flask.g 後顯式傳入，跨 thread 可靠
    _heavy_via_g = bool(heavy)
    if not _heavy_via_g:
        try:
            from flask import g as _flask_g_head
            _heavy_via_g = bool(getattr(_flask_g_head, "heavy_opt_in", False))
        except Exception:
            _heavy_via_g = False
    _has_prefix = _msg_lower_head.startswith("@heavy ") or _msg_lower_head.startswith("@重型 ")
    if _has_prefix or _heavy_via_g:
        import os as _os
        _nim_enabled = (_os.environ.get("NVIDIA_NIM_ENABLE", "0") or "").strip().lower() in {"1", "true", "yes", "on"}
        if _nim_enabled:
            try:
                from skills.bridge.nim_heavy import run_nim_chat
                # 若 prefix 還在就剝除；若上游已剝除則直接用原文
                if _has_prefix:
                    _clean_msg = _msg_stripped.split(" ", 1)[1] if " " in _msg_stripped else ""
                else:
                    _clean_msg = _msg_stripped
                logger.info("chat_casper: @heavy opt-in → NIM 405B fast path")
                _nim_r = run_nim_chat(
                    prompt=_clean_msg,
                    timeout_sec=int(_os.environ.get("NVIDIA_NIM_TIMEOUT_SEC", "120") or "120"),
                    task_type="legal_analysis",
                    require_pii_scrub=(_os.environ.get("NVIDIA_NIM_REQUIRE_PII_SCRUB", "1") or "").strip() != "0",
                    system_prompt=(
                        "你是 MAGI 系統的 AI 助理，服務對象為台灣的律師事務所。"
                        "請全程使用台灣繁體中文（正體中文）回覆。"
                        "使用台灣慣用的法律術語，例如「被告」而非「被告人」、「起訴書」而非「起诉书」。"
                        "不要使用簡體中文或中國大陸用語。"
                    ),
                    heavy=True,  # 強制 405B
                )
                if _nim_r.get("success") and _nim_r.get("response"):
                    logger.info(
                        "chat_casper: NIM success (model=%s, dur=%dms, pii=%s)",
                        _nim_r.get("model"), _nim_r.get("duration_ms", 0), _nim_r.get("pii_counts", {}),
                    )
                    return str(_nim_r["response"])
                logger.warning(
                    "chat_casper: NIM failed (%s), falling back to oMLX with prefix stripped",
                    _nim_r.get("error", "empty"),
                )
                # NIM 失敗 → 剝除 @heavy 前綴後繼續走 oMLX（降級）
                message = _clean_msg
            except Exception as _nim_err:
                logger.warning("chat_casper: NIM exception (%s), falling back to oMLX", _nim_err)
                if _has_prefix:
                    message = _msg_stripped.split(" ", 1)[1] if " " in _msg_stripped else ""
                else:
                    message = _msg_stripped
        else:
            # NIM 未啟用，剝除 @heavy 前綴正常走 oMLX（避免前綴混進 prompt）
            if _has_prefix:
                message = _msg_stripped.split(" ", 1)[1] if " " in _msg_stripped else ""
            else:
                message = _msg_stripped

    # ── Layer 3: Query Classification ──
    tier = _classify_query_tier(message)
    logger.info("🏷️ Query tier: %s", tier)

    # GREETING → 直接模板回覆，不走 LLM（零延遲、零幻覺風險）
    if tier == "GREETING":
        import random as _rng
        greeting = _rng.choice(_GREETING_RESPONSES)
        return greeting

    # ── Tier-aware recall parameters ──
    _top_k = 1 if tier == "SIMPLE" else 4
    _temperature = 0.3 if tier == "SIMPLE" else 0.55

    if _should_skip_recall_for_chat(message, tier):
        memories = []
        logger.info("💬 SIMPLE chat recall skipped for low-risk casual query")
    else:
        memories = _filter_chatlog_memories(message, recall(message, top_k=_top_k))
    # ── Layer 1: Statute / Noise Filter ──
    memories = _filter_statute_memories(memories, tier)
    # Compress context if too large to avoid LLM context overflow
    # SIMPLE tier: shorter context = faster prefill = lower latency
    _ctx_budget = 600 if tier == "SIMPLE" else 1200
    memory_context = _summarize_memories_if_needed(message, memories, max_tokens=_ctx_budget)

    web_context = "無。"
    memories_insufficient = len(memories) == 0

    # ── Real-time Data Gateway（Layer 0：authoritative API 先行）──────────
    # 天氣/股價/匯率等即時資料，絕不讓 LLM 合成猜測數字；
    # 優先打 authoritative API；若 API+scrape 均失敗，fall through 到 web_research
    # 讓搜尋引擎補一刀，而非直接硬拒。只有 web_research 也失敗時才用拒絕文字。
    _rdg_result = None
    _rdg_refusal_text = None  # 備用：只有 web_research 也撈不到資料時才用
    try:
        from skills.engine.realtime_data_gateway import handle_realtime_query
        _rdg_result = handle_realtime_query(message)
    except Exception as _rdg_err:
        logger.debug("[RDG] import/call error: %s", _rdg_err)

    if _rdg_result is not None:
        if _rdg_result.get("success") and _rdg_result.get("reply"):
            # API 成功 → 直接用 authoritative data，跳過 LLM 合成
            logger.info("[RDG] ✅ authoritative data ready, bypassing LLM synthesis")
            return _rdg_result["reply"]
        elif _rdg_result.get("refusal"):
            # 無法取得即時資料（API key 缺失 / scrape 失敗）→ 先試 web_research
            # 不立即拒絕；把拒絕文字存起來作為最後備份
            logger.info("[RDG] ⚠️ realtime data unavailable, falling through to web_research")
            _rdg_refusal_text = _rdg_result.get("refusal")
            # 繼續執行，不 return
        # else：_rdg_result 但沒有 success/refusal → fall through 到 auto-research

    # 智慧搜尋觸發：明確關鍵字觸發，或「記憶不足 + 事實問題」才上網
    # SIMPLE 級閒聊即使沒記憶也不搜尋，避免無謂延遲
    should_research = (
        _needs_research(message)  # 明確關鍵字（天氣、最新、上網...）
        or (memories_insufficient and _is_factual_question(message))  # 沒記憶 + 事實問題
    )
    if should_research:
        try:
            from skills.research.web_research import research_topic

            reason = "factual+no_mem" if memories_insufficient else "keyword match"
            logger.info(f"🌐 chat_casper auto-research ({reason}): {message}")
            search_res = research_topic(message, depth=2)
            if search_res.get("sources"):
                _src_titles = [s.get("title", "").split("|")[0].strip() for s in search_res["sources"][:2]]
                _src_note = f"（資料來源：{', '.join(t for t in _src_titles if t)}）"
                if _is_weather_query(message):
                    _src_note += " ⚠️ 天氣資料非即時精確，請以中央氣象署（CWA）為準：https://www.cwa.gov.tw/"
                web_context = _src_note + "\n" + search_res.get("combined_content", "")[:2800]
        except Exception as e:
            logger.warning(f"Web research failed in chat_casper: {e}")

    # RDG refusal 安全網：如果 web_research 也完全沒撈到資料，
    # 用 RDG 的明確拒絕文字（含正確的 CWA 連結）作為回覆，
    # 防止 LLM 憑空合成天氣數字
    if _rdg_refusal_text and web_context == "無。":
        logger.info("[RDG] 🚫 web_research also empty, returning rdg refusal")
        return _rdg_refusal_text

    # ── Web-Grounded Synthesis Router（Bug #5 Layer B+C）──────────────────
    # 資訊整合類查詢（評價/路線/營業時間/商品比較/新聞）→ 走 web_research_synthesize，
    # 而非讓 LLM 憑空回答。此路由在 RDG 之後、LLM 之前插入。
    # 只有當 web_context 尚未被前面流程填充時才觸發（避免重複搜尋）。
    if web_context == "無。":
        try:
            from skills.research.web_research import _maybe_route_to_web_grounded, web_research_synthesize
            _wg_category = _maybe_route_to_web_grounded(message)
            if _wg_category:
                logger.info("[WG] Web-grounded synthesis triggered for category: %s", _wg_category)
                _wg_reply = web_research_synthesize(message, max_sources=3)
                if _wg_reply and len(_wg_reply.strip()) > 50:
                    return _wg_reply
        except Exception as _wg_err:
            logger.warning("[WG] web_research_synthesize failed: %s", _wg_err)
            # fall through to LLM

    _trust_rules = build_trust_system_instruction()
    _style_rule = ""
    if tier == "SIMPLE":
        _style_rule = "- 【閒聊模式】回答請簡短（1~3句）、自然、口語化。不要像客服那樣過度道歉或解釋，就像朋友一樣對話即可。"
    if is_small_talk_intent(message, tier):
        _style_rule = _style_rule + "\n" + _SMALL_TALK_STYLE_HINT if _style_rule else _SMALL_TALK_STYLE_HINT

    # 天氣/即時資料規則
    _realtime_rule = ""
    if _is_weather_query(message):
        _realtime_rule = (
            "\n- 【天氣規則】回答天氣時必須明確說明資料來源（如 Weather.com、AccuWeather），"
            "並提醒使用者「以中央氣象署（CWA）為準」。不得憑空給出降雨機率或溫度數字。"
        )

    # 防幻覺規則（注入 prompt）
    try:
        from api.hallucination_guard import build_anti_hallucination_prompt_rules
        _anti_hallucination_rules = "\n" + build_anti_hallucination_prompt_rules()
    except Exception:
        _anti_hallucination_rules = ""

    prompt = f"""你是 CASPER（MAGI-01），請以繁體中文回答。
你需要同時參考記憶與近期對話，保持前後一致。

[規則]
- 記憶優先於網路摘要。
- 若資訊不足，直接承認不足，不可硬猜。
- 若使用者在延續上一題，請承接上下文。
- 若使用者糾正您的錯誤或補充新資訊，請【務必明確總結並覆誦正確的資訊】，這會幫助系統將正確知識寫入長期記憶。
- 不要使用 Markdown 語法（**粗體**、`程式碼`、### 標題等）。純文字即可。
- 內部信任標記僅供你判斷，回答時不得直接說出 [使用者陳述]、[已驗證事實]、[檢索線索]、[衍生推論] 或「身為 CASPER」這類內部提示字樣。
- 若回答引用網路搜尋資料，必須說出資料來源名稱，不可用「根據我目前找到的資訊」等模糊說法。{_realtime_rule}{_anti_hallucination_rules}
{_style_rule}


{_trust_rules}

[長期記憶]
{memory_context}

[網路研究]
{web_context}

[近期對話]
{conversation_history or "無"}

---

{message}
"""
    try:
        answer = _generate(prompt, temperature=_temperature, timeout=180, num_ctx=6144)
        if answer:
            # 攔截亂碼輸出：LLM 產出垃圾時重試一次
            if _is_garbage_output(answer):
                logger.warning("⛔ chat_casper: garbage output detected, retrying once")
                answer = _generate(prompt, temperature=0.3, timeout=120, num_ctx=6144)
                if not answer or _is_garbage_output(answer):
                    return "抱歉，我剛才產生了異常輸出。請再試一次。"
            # 攔截鸚鵡式回覆：LLM 只是複述問題而非真正回答
            if _is_parrot_response(message, answer):
                logger.warning("⛔ chat_casper: parrot response detected, retrying once")
                answer = _generate(prompt, temperature=0.3, timeout=120, num_ctx=6144)
                if not answer or _is_parrot_response(message, answer):
                    return "抱歉，我目前無法提供有意義的回答。請換個方式再問一次。"
            # 攔截角色設定幻覺：LLM 自行生成 CASPER 職責描述而非回答問題
            if _is_persona_hallucination(answer):
                logger.warning("⛔ chat_casper: persona hallucination detected, retrying once")
                answer = _generate(prompt, temperature=0.3, timeout=120, num_ctx=6144)
                if not answer or _is_persona_hallucination(answer):
                    return "抱歉，我剛才跑題了。請再說一次你的問題，我會直接回答。"
            # ── Layer 2: Semantic Coherence Check ──
            if _is_incoherent_response(message, answer):
                logger.warning("⛔ chat_casper: incoherent response, retrying with lower temp")
                answer = _generate(prompt, temperature=0.2, timeout=120, num_ctx=6144)
                if not answer or _is_incoherent_response(message, answer):
                    return "抱歉，我剛才的回覆偏離了你的問題。請再說一次，我會更專注回答。"
            # ── Entity Hallucination Check ──
            _entity_ctx = f"{message}\n{memory_context}"
            if _has_entity_hallucination(answer, _entity_ctx):
                logger.warning("⛔ chat_casper: entity hallucination detected")
                answer = _generate(prompt, temperature=0.15, timeout=120, num_ctx=6144)
                if not answer or _has_entity_hallucination(answer, _entity_ctx):
                    return "我的回覆中引用了無法確認的法條或案號，為避免誤導，建議您查閱原始法規。"
            # Self-check disabled for chat by default (CASPER_CHAT_SELF_CHECK=0)
            # to reduce latency from ~90s to ~25s. Only query mode keeps self-check.
            if ENABLE_CHAT_SELF_CHECK and _needs_research(message):
                answer = _self_check_answer(
                    query=message,
                    answer=answer,
                    memory_context=memory_context,
                    web_context=web_context,
                    conversation_history=conversation_history,
                )
            # SIMPLE tier: skip Tri-Agent verification to reduce latency
            # (recall=1, no research, low hallucination risk → verification overkill)
            # COMPLEX tier: full verification pipeline
            if tier == "SIMPLE":
                return answer
            final_answer = _verify_and_repair_answer(
                query=message,
                answer=answer,
                prompt=prompt,
                memories=memories,
                memory_context=memory_context,
                web_context=web_context,
                conversation_history=conversation_history,
                entity_context=_entity_ctx,
            )
            # ── 溯源頁尾（2026-04-20：來源標記，方便使用者引導修正）──────────
            try:
                from api.answer_provenance import (
                    build_provenance_footer as _build_prov,
                    store_provenance as _store_prov,
                )
                _prov_risk = "SAFE"
                try:
                    from api.hallucination_guard import classify_risk as _cr
                    _prov_risk = _cr(message)
                except Exception:
                    pass
                _footer = _build_prov(
                    memories=memories,
                    web_context=web_context,
                    tier=tier,
                    risk_level=_prov_risk,
                )
                if _footer:
                    final_answer = final_answer + "\n\n" + _footer
                # 背景儲存溯源記錄（用於「這條不對」修正流程）
                import threading as _thr
                _thr.Thread(
                    target=_store_prov,
                    args=("default", memories, web_context, message),
                    daemon=True,
                ).start()
            except Exception as _prov_err:
                logger.debug("[provenance] footer build failed: %s", _prov_err)
            return final_answer
    except Exception as e:
        logger.error(f"Chat Error: {e}")
        return "我目前有點忙碌，請稍後再試一次。"
    return "目前沒有足夠資訊可以穩定回答。"

def analyze_content(prompt, timeout=300):
    """
    Dedicated function for content analysis (Code, Documents) without persona/search overhead.
    """
    try:
        logger.info(f"🧠 Analyzing content (Timeout={timeout}s)...")
        # Use the same hybrid path as CASPER generation:
        # - distributed mode: Melchior first (better quality/speed on GPU), then local fallback
        # - local mode: local first, then Melchior fallback
        return _generate(prompt, temperature=0.2, timeout=timeout, num_ctx=8192)
    except Exception as e:
        logger.error(f"Analysis Error: {e}")
        return f"System functionality limited: {e}"

if __name__ == "__main__":
    if len(sys.argv) > 1:
        q = sys.argv[1]
        print(ask_casper(q))
    else:
        print("Usage: python grounded_ai.py 'Question'")
