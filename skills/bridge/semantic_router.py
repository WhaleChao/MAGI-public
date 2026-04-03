#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Semantic Skill Router
=====================
Complements the regex/heuristic IntentionClassifier with skill-level dispatch.
When rigid if/elif chains in the orchestrator produce no match, this module
scores all skills in definitions.json against the user's message and returns
the best-matching skill name (if confidence is sufficient).

Algorithm:
  1. Load skill descriptors (name + description + keywords) from definitions.json
  2. Normalise message and skill text to token sets
  3. Compute weighted Jaccard + trigram overlap score for each skill
  4. If top score >= ROUTE_THRESHOLD, return that skill's name + confidence
  5. Optionally use LLM dispatch for ambiguous scores

Environment variables:
  SEMANTIC_ROUTER_THRESHOLD   float 0-1, min confidence to fire  (default 0.22)
  SEMANTIC_ROUTER_LLM_THRESH  float 0-1, threshold to use LLM    (default 0.18)
  SEMANTIC_ROUTER_LLM_ENABLED 0/1 enable LLM fallback            (default 1)
  SEMANTIC_ROUTER_MAX_LLM_SEC timeout for LLM skill dispatch      (default 8)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("SemanticRouter")

_DEFINITIONS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "definitions.json"
)

# Tunable thresholds
_ROUTE_THRESHOLD = float(os.environ.get("SEMANTIC_ROUTER_THRESHOLD", "0.22") or "0.22")
_LLM_THRESHOLD   = float(os.environ.get("SEMANTIC_ROUTER_LLM_THRESH", "0.18") or "0.18")
_LLM_ENABLED     = os.environ.get("SEMANTIC_ROUTER_LLM_ENABLED", "1").strip() in {"1", "true", "yes"}
_LLM_TIMEOUT     = int(os.environ.get("SEMANTIC_ROUTER_MAX_LLM_SEC", "8") or "8")
_PHRASE_BONUS_SCALE = float(os.environ.get("SEMANTIC_ROUTER_PHRASE_BONUS_SCALE", "0.25") or "0.25")

# Chinese/English stopwords to down-weight
_STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "們", "一", "不", "也",
    "就", "都", "和", "有", "大", "来", "这", "会", "好", "要", "做", "说",
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "up",
    "and", "or", "not", "but", "that", "this", "it", "its",
    "i", "you", "he", "she", "we", "they", "me", "him", "her", "us", "them",
}

# Skills that should NEVER be auto-dispatched (they require explicit trigger)
_BLACKLISTED_SKILLS = {
    "iron_dome_scan",   # safety-critical
    "drop_table",       # should never be routed
}

# Generated clarification/fallback skills are useful for recovery flows, but they
# should not participate in normal semantic routing. They add noise and can steal
# matches from real product capabilities.
_SKIP_NAME_PATTERNS = [
    r"^run_auto_user_clarification_",
    r"^auto_user_clarification_",
]
_SKIP_DESC_PATTERNS = [
    r"^Run generated fallback skill for:",
    r"\[User Clarification\]",
]

# ---------------------------------------------------------------------------
# Phrase hints: direct substring → (skill_name, bonus_score) mapping.
# Applied before token scoring to bridge the Chinese ↔ English vocabulary gap.
# Phrases are checked longest-first; first match per skill wins.
# ---------------------------------------------------------------------------
_PHRASE_HINTS: List[Tuple[str, str, float]] = [
    # Chinese legal / document phrases (longest first for specificity)
    ("法院判決",       "run_judgment_collector", 0.50),
    ("判決書",         "run_judgment_collector", 0.48),
    ("判決",           "run_judgment_collector", 0.40),
    ("幫我摘要",       "summarize_text",         0.46),
    ("幫我翻譯",       "tri_sage_translate",     0.46),
    ("開庭時間",       "list_meetings",          0.50),
    ("今天開庭",       "list_meetings",          0.50),
    ("有沒有開庭",     "list_meetings",          0.48),
    ("開庭",           "list_meetings",          0.42),
    ("行程",           "list_meetings",          0.38),
    ("逐字稿",         "tri_sage_transcribe",    0.50),
    ("語音轉文字",     "tri_sage_transcribe",    0.48),
    ("轉錄",           "tri_sage_transcribe",    0.44),
    ("文章摘要",       "summarize_text",         0.50),
    ("文件摘要",       "summarize_text",         0.48),
    ("摘要",           "summarize_text",         0.42),
    ("搜尋",           "web_search",             0.42),
    ("搜索",           "web_search",             0.42),
    ("查詢",           "web_search",             0.38),
    ("翻譯",           "tri_sage_translate",     0.50),
    ("生成圖片",       "image_generate",         0.52),
    ("圖片生成",       "image_generate",         0.50),
    ("畫一張",         "image_generate",         0.48),
    ("記憶搜尋",       "recall_memory",          0.50),
    ("記得",           "recall_memory",          0.36),
    ("記住",           "save_memory",            0.38),
    ("客戶",           "query_clients",          0.38),
    ("當事人",         "query_clients",          0.36),
    ("案件",           "query_clients",          0.32),
    ("訂閱",           "rss_subscribe",          0.42),
    # Transcript search
    ("筆錄查詢",       "transcript_query",        0.52),
    ("審判筆錄",       "transcript_query",        0.50),
    ("訊問筆錄",       "transcript_query",        0.50),
    ("準備程序筆錄",   "transcript_query",        0.50),
    ("筆錄",           "transcript_query",        0.42),
    ("證詞",           "transcript_query",        0.40),
    ("在法庭上說",     "transcript_query",        0.46),
    ("被告說",         "transcript_query",        0.44),
    ("證人說",         "transcript_query",        0.44),
    # PDF annotation
    ("PDF標籤",        "pdf_annotate",            0.52),
    ("卷宗標記",       "pdf_annotate",            0.50),
    ("自動標籤",       "pdf_annotate",            0.48),
    # Stock briefing — watchlist management
    ("追蹤以下股票",   "stock_briefing",          0.56),
    ("追蹤股票",       "stock_briefing",          0.54),
    ("新增追蹤",       "stock_briefing",          0.50),
    ("設定追蹤",       "stock_briefing",          0.50),
    ("移除追蹤",       "stock_briefing",          0.50),
    ("目前追蹤",       "stock_briefing",          0.52),
    ("追蹤清單",       "stock_briefing",          0.52),
    ("股市預測",       "stock_briefing",          0.52),
    # Stock briefing — technical modes
    ("技術分析",       "stock_briefing",          0.46),
    ("股市晨報",       "stock_briefing",          0.52),
    ("布林通道",       "stock_briefing",          0.46),
    ("MACD",           "stock_briefing",          0.48),
    ("RSI",            "stock_briefing",          0.48),
    ("macd",           "stock_briefing",          0.48),
    ("rsi",            "stock_briefing",          0.48),
    # English keyword phrases (bridges pure English queries to skills)
    ("court judgment",    "run_judgment_collector", 0.52),
    ("court hearing",     "list_meetings",          0.50),
    ("transcribe",        "tri_sage_transcribe",    0.48),
    ("translate",         "tri_sage_translate",     0.48),
    ("summarize",         "summarize_text",         0.46),
    ("summarise",         "summarize_text",         0.46),
    ("search for",        "web_search",             0.42),
    ("look up",           "web_search",             0.40),
    ("generate image",    "image_generate",         0.50),
    ("draw a",            "image_generate",         0.44),
    ("subscribe",         "rss_subscribe",          0.42),
    ("remember this",     "save_memory",            0.44),
    ("recall",            "recall_memory",          0.40),
    # 勞動基準法計算
    ("勞動基準法",        "labor_law_calc",          0.56),
    ("勞基法",            "labor_law_calc",          0.54),
    ("加班費",            "labor_law_calc",          0.54),
    ("加班計算",          "labor_law_calc",          0.56),
    ("特別休假",          "labor_law_calc",          0.52),
    ("特休假",            "labor_law_calc",          0.52),
    ("特休天數",          "labor_law_calc",          0.54),
    ("資遣費",            "labor_law_calc",          0.56),
    ("一例一休",          "labor_law_calc",          0.52),
    ("例假日加班",        "labor_law_calc",          0.58),
    ("休息日加班",        "labor_law_calc",          0.58),
    ("平日加班",          "labor_law_calc",          0.56),
    ("overtime",          "labor_law_calc",          0.42),
    ("severance",         "labor_law_calc",          0.44),
]
# Sort by phrase length descending so longer (more specific) phrases are checked first
_PHRASE_HINTS.sort(key=lambda x: -len(x[0]))
_SOFT_PHRASE_HINTS = {
    # Broad terms should influence scoring only; they must not hard-dispatch.
    "摘要",
    "翻譯",
    "記得",
    "記住",
    "案件",
    "客戶",
    "當事人",
    "搜尋",
    "搜索",
    "查詢",
    "行程",
    "開庭",
    "筆錄",
    "證詞",
    "訂閱",
    "translate",
    "summarize",
    "summarise",
    "search for",
    "look up",
    "recall",
    "remember this",
}


def _load_skills() -> List[Dict]:
    """Load and cache skill descriptors from definitions.json."""
    try:
        p = Path(_DEFINITIONS_PATH)
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        tools = data.get("tools") or []
        skills = []
        for t in tools:
            name = str(t.get("name") or "").strip()
            if not name or name in _BLACKLISTED_SKILLS:
                continue
            desc = str(t.get("description") or "").strip()
            default_skill = str(
                t.get("default_skill")
                or t.get("default skill")
                or t.get("skill")
                or ""
            ).strip()
            if any(re.search(pat, name, flags=re.IGNORECASE) for pat in _SKIP_NAME_PATTERNS):
                continue
            if any(re.search(pat, desc, flags=re.IGNORECASE | re.DOTALL) for pat in _SKIP_DESC_PATTERNS):
                continue
            if "auto-user-clarification" in default_skill.lower():
                continue
            # Build extra keyword hints from parameter descriptions
            param_text = ""
            for p_name, p_info in (
                (t.get("parameters") or {}).get("properties") or {}
            ).items():
                param_text += f" {p_info.get('description','')}"
            keywords = t.get("keywords") or []
            full_text = f"{name} {desc} {param_text} {' '.join(keywords)}"
            skills.append({"name": name, "text": full_text, "tokens": _tokenize(full_text)})
        return skills
    except Exception as e:
        logger.warning(f"SemanticRouter: failed to load definitions: {e}")
        return []


_SKILLS_CACHE: Optional[List[Dict]] = None
_SKILLS_CACHE_TS: float = 0.0
_SKILLS_CACHE_TTL: float = 300.0  # reload every 5 min


def _get_skills() -> List[Dict]:
    global _SKILLS_CACHE, _SKILLS_CACHE_TS
    now = time.monotonic()
    if _SKILLS_CACHE is None or (now - _SKILLS_CACHE_TS) > _SKILLS_CACHE_TTL:
        _SKILLS_CACHE = _load_skills()
        _SKILLS_CACHE_TS = now
    return _SKILLS_CACHE


def _tokenize(text: str) -> Dict[str, float]:
    """
    Returns a token → weight dict.
    Chinese: individual characters + bigrams (2-char) to capture compound words.
    English: whitespace/punctuation-split words.
    CJK bigrams are up-weighted because they represent meaningful vocabulary units.
    """
    text = (text or "").lower()

    # Split into CJK runs and Latin runs
    tokens: List[str] = []
    buf = ""
    cjk_run: List[str] = []

    def _flush_cjk(run: List[str]) -> List[str]:
        """Emit single chars + bigrams from a CJK run."""
        out = list(run)  # single chars
        for i in range(len(run) - 1):
            out.append(run[i] + run[i + 1])  # bigrams
        return out

    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            if buf.strip():
                tokens.extend(t for t in re.split(r"\W+", buf.strip()) if t)
                buf = ""
            cjk_run.append(ch)
        else:
            if cjk_run:
                tokens.extend(_flush_cjk(cjk_run))
                cjk_run = []
            buf += ch
    if cjk_run:
        tokens.extend(_flush_cjk(cjk_run))
    if buf.strip():
        tokens.extend(t for t in re.split(r"\W+", buf.strip()) if t)

    tokens = [t for t in tokens if t and t not in _STOPWORDS]

    # Build TF-like weights — bigrams (len 2 CJK) get higher weight than single chars
    weights: Dict[str, float] = {}
    for tok in tokens:
        is_cjk_bigram = len(tok) == 2 and all("\u4e00" <= c <= "\u9fff" for c in tok)
        if is_cjk_bigram:
            w = 2.5  # bigrams carry strong signal
        else:
            w = 1.0 + math.log(1 + len(tok))
        weights[tok] = weights.get(tok, 0.0) + w
    return weights


def _trigrams(text: str) -> set:
    t = re.sub(r"\s+", "", (text or "").lower())
    return {t[i:i+3] for i in range(len(t) - 2)} if len(t) >= 3 else set()


def _score(msg_tokens: Dict[str, float], skill_tokens: Dict[str, float],
           msg_text: str, skill_text: str) -> float:
    """Combined weighted overlap + trigram similarity (0..1)."""
    if not msg_tokens or not skill_tokens:
        return 0.0

    # Weighted intersection
    inter = sum(
        min(msg_tokens[k], skill_tokens[k])
        for k in msg_tokens
        if k in skill_tokens
    )
    union = sum(msg_tokens.values()) + sum(skill_tokens.values()) - inter
    jaccard = inter / union if union > 0 else 0.0

    # Trigram overlap on raw text
    mt = _trigrams(msg_text[:200])
    st = _trigrams(skill_text[:300])
    tg = len(mt & st) / (len(mt | st) + 1e-9) if (mt and st) else 0.0

    # Exact skill name match bonus
    name_bonus = 0.0

    return 0.65 * jaccard + 0.25 * tg + 0.10 * name_bonus


def route(message: str) -> Optional[Dict]:
    """
    Route a user message to the best-matching skill.

    Returns:
        {
            "skill": "web_search",
            "confidence": 0.27,
            "method": "phrase" | "semantic" | "llm",
        }
        or None if confidence < threshold.
    """
    skills = _get_skills()
    if not skills:
        return None

    msg_text = (message or "").lower()
    msg_tokens = _tokenize(message)
    if not msg_tokens:
        return None

    # --- Phase 1: Phrase hint matching (bridges Chinese ↔ English vocabulary gap) ---
    phrase_bonus: Dict[str, float] = {}  # skill_name → best bonus score
    hard_phrase_bonus: Dict[str, float] = {}
    for phrase, skill_name, bonus in _PHRASE_HINTS:
        if skill_name in _BLACKLISTED_SKILLS:
            continue
        if phrase in msg_text:
            # Only record the best (longest/highest) bonus per skill
            if bonus > phrase_bonus.get(skill_name, 0.0):
                phrase_bonus[skill_name] = bonus
            if phrase not in _SOFT_PHRASE_HINTS and bonus > hard_phrase_bonus.get(skill_name, 0.0):
                hard_phrase_bonus[skill_name] = bonus

    # Only hard phrases may short-circuit directly. Broad phrases must go through
    # the token scorer so they do not hijack generic chat or ambiguous requests.
    if hard_phrase_bonus:
        best_phrase_skill = max(hard_phrase_bonus, key=lambda k: hard_phrase_bonus[k])
        best_phrase_conf = hard_phrase_bonus[best_phrase_skill]
        if best_phrase_conf >= _ROUTE_THRESHOLD:
            logger.debug(
                f"SemanticRouter[phrase]: '{message[:60]}' → {best_phrase_skill} ({best_phrase_conf:.3f})"
            )
            return {"skill": best_phrase_skill, "confidence": round(best_phrase_conf, 3), "method": "phrase"}

    # --- Phase 2: Token overlap scoring ---
    scores: List[Tuple[float, str]] = []
    for s in skills:
        sc = _score(msg_tokens, s["tokens"], msg_text, s["text"])
        # Blend in any phrase bonus for this skill
        sc += phrase_bonus.get(s["name"], 0.0) * _PHRASE_BONUS_SCALE
        scores.append((sc, s["name"]))

    scores.sort(reverse=True)
    best_score, best_name = scores[0]
    second_score = scores[1][0] if len(scores) > 1 else 0.0

    # Only route if the top score is meaningfully above second place
    gap = best_score - second_score
    effective_score = best_score * (1 + 0.5 * min(gap / (best_score + 1e-9), 1.0))

    if effective_score >= _ROUTE_THRESHOLD:
        logger.debug(f"SemanticRouter: '{message[:60]}' → {best_name} ({effective_score:.3f})")
        return {"skill": best_name, "confidence": round(effective_score, 3), "method": "semantic"}

    # --- Phase 3: LLM fallback for ambiguous mid-range scores ---
    if _LLM_ENABLED and best_score >= _LLM_THRESHOLD:
        result = _llm_route(message, [s["name"] for s in skills])
        if result:
            return result

    return None


def _llm_route(message: str, skill_names: List[str]) -> Optional[Dict]:
    """Use Casper LLM to pick the best skill from a short list."""
    try:
        import requests as _req
        casper_url = os.environ.get("CASPER_LOCAL_URL", "http://localhost:8080/v1/chat/completions")
        skill_list = "\n".join(f"- {s}" for s in skill_names[:30])
        prompt = (
            f"Available skills:\n{skill_list}\n\n"
            f"User message: \"{message[:300]}\"\n\n"
            "Which ONE skill best matches this message? Reply with ONLY the exact skill name "
            "from the list, or 'none' if none match well."
        )
        resp = _req.post(
            casper_url,
            json={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 30,
            },
            timeout=_LLM_TIMEOUT,
        )
        if resp.status_code == 200:
            answer = (resp.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip().lower()
            for s in skill_names:
                if s.lower() == answer or s.lower() in answer:
                    return {"skill": s, "confidence": 0.55, "method": "llm"}
    except Exception as e:
        logger.debug(f"SemanticRouter LLM failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Orchestrator integration helper
# ---------------------------------------------------------------------------
# Maps skill names (from definitions.json) to orchestrator trigger keywords
# so the router can synthesise a synthetic trigger message.
# ---------------------------------------------------------------------------

_SKILL_TO_TRIGGER: Dict[str, str] = {
    "web_search":           "@MAGI 搜尋 {msg}",
    "translate_document":   "@MAGI 翻譯 {msg}",
    "pdf_summarize":        "@MAGI 摘要 {msg}",
    "audio_transcribe":     "@MAGI 逐字稿 {msg}",
    "image_generate":       "@MAGI 生成圖片 {msg}",
    "calendar_sync":        "@MAGI 日曆同步",
    "case_create":          "@MAGI 建立案件 {msg}",
    "judgment_search":      "收集判決 {msg}",
    "run_judgment_collector":"收集判決 {msg}",
    "rss_subscribe":        "@MAGI 訂閱 {msg}",
    "memory_search":        "@MAGI 記憶搜尋 {msg}",
    "transcript_query":     "@MAGI 筆錄查詢 {msg}",
    "transcript_index":     "@MAGI 索引筆錄",
    "pdf_annotate":         "@MAGI 自動標籤 {msg}",
    "stock_briefing":       "@MAGI 股市晨報 --mode technical {msg}",
    "labor_law_calc":       "@MAGI 加班費計算 {msg}",
}


def suggest_trigger(skill_name: str, original_message: str) -> str:
    """
    Given a matched skill name and the original user message, return a
    suggested canonical trigger string that the orchestrator would recognise.
    """
    tpl = _SKILL_TO_TRIGGER.get(skill_name, "")
    if tpl:
        return tpl.format(msg=original_message[:120])
    return f"@MAGI {skill_name} {original_message[:120]}"
