#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Embedding-based Skill Router
=============================
Uses ModernBERT embeddings (via oMLX) to match user messages to skills
by cosine similarity, replacing fragile regex/if-elif dispatch.

Each skill has MULTIPLE embeddings (description + each phrase hint separately)
and routing uses MAX similarity across all vectors for each skill.

Startup:
  1. Loads skill descriptors from definitions.json
  2. Augments with phrase hints from semantic_router._PHRASE_HINTS
  3. Pre-computes embeddings for each text independently
  4. Caches embeddings to disk for fast restart

Runtime:
  route(message) → (skill_name, confidence, tier)
  Tiers: "DIRECT" (≥0.75), "GUIDED" (0.55-0.74), "LOW" (<0.55)

Environment variables:
  EMBEDDING_ROUTER_OMLX_URL      oMLX base URL          (default http://127.0.0.1:8081)
  EMBEDDING_ROUTER_MODEL         embedding model name    (default modernbert-embed-4bit)
  EMBEDDING_ROUTER_DIRECT_THRESH min score for direct    (default 0.75)
  EMBEDDING_ROUTER_GUIDED_THRESH min score for guided    (default 0.55)
  EMBEDDING_ROUTER_CACHE_DIR     cache directory         (default skills/bridge/.embed_cache)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
try:
    from skills.bridge.http_pool import get_session as _get_session
    _http = _get_session()
except ImportError:
    _http = requests.Session()

logger = logging.getLogger("EmbeddingRouter")

# --- Configuration ---
_OMLX_BASE = (
    os.environ.get("EMBEDDING_ROUTER_OMLX_URL")
    or os.environ.get("MAGI_OMLX_EMBED_URL")
    or "http://127.0.0.1:8081"
)
_OMLX_EMBED_URL = _OMLX_BASE.rstrip("/") + "/v1/embeddings"
_EMBED_MODEL = os.environ.get("EMBEDDING_ROUTER_MODEL", os.environ.get("MAGI_OMLX_EMBED_MODEL", ""))
_DIRECT_THRESH = float(os.environ.get("EMBEDDING_ROUTER_DIRECT_THRESH", "0.75") or "0.75")
_GUIDED_THRESH = float(os.environ.get("EMBEDDING_ROUTER_GUIDED_THRESH", "0.55") or "0.55")
_EMBED_TIMEOUT = 4  # seconds per embedding request
_BATCH_SIZE = 8  # max texts per embedding API call

_DEFINITIONS_PATH = os.path.join(os.path.dirname(__file__), "..", "definitions.json")
_CACHE_DIR = os.environ.get(
    "EMBEDDING_ROUTER_CACHE_DIR",
    os.path.join(os.path.dirname(__file__), ".embed_cache"),
)

# Skills that should never be auto-dispatched
_BLACKLISTED_SKILLS = {
    "iron_dome_scan",
    "drop_table",
}

# Skip auto-generated fallback skills
_SKIP_NAME_PATTERNS = ["run_auto_user_clarification_", "auto_user_clarification_"]

# Supplementary phrase hints for skills not covered by semantic_router._PHRASE_HINTS
_EXTRA_PHRASES: Dict[str, List[str]] = {
    "deep_research": [
        "深入研究", "網路研究", "執行網路研究", "進行研究", "研究一下",
        "comprehensive research", "in-depth research", "research this topic",
    ],
    "run_pdf_namer": [
        "PDF重新命名", "PDF命名", "重新命名PDF", "rename PDF", "PDF rename",
        "自動命名檔案", "rename this file",
    ],
    "run_magi_doctor": [
        "系統狀態", "系統檢查", "系統診斷", "健康檢查", "MAGI狀態",
        "system status", "health check", "diagnostics", "system check",
    ],
    "run_magi_autopilot": [
        "夜間巡檢", "夜間任務", "巡檢", "night patrol", "nightly check",
        "自動巡檢", "跑夜間任務", "巡檢報告", "執行夜間巡檢", "開始巡檢",
        "自動駕駛", "autopilot", "自動執行",
    ],
    "run_file_review_orchestrator": [
        "閱卷", "卷宗審閱", "案件審閱", "file review", "review files",
        "閱卷下載", "同步筆錄",
    ],
    "run_laf_portal_automation": [
        "法扶", "法律扶助", "法扶申請", "legal aid", "LAF",
    ],
    "tri_sage_translate": [
        "翻譯這段文字", "翻譯這個", "翻譯以下內容", "translate this text",
        "translate the following", "中翻英", "英翻中", "日翻中",
    ],
    "tri_sage_transcribe": [
        "音訊轉文字", "語音辨識", "speech to text", "audio transcription",
    ],
    "fetch_url": [
        "讀取網頁", "抓取網頁", "fetch URL", "read webpage", "open URL",
    ],
    "analyze_image": [
        "分析圖片", "看圖", "圖片辨識", "OCR", "文字辨識", "analyze image",
        "describe image", "看這張圖",
    ],
    "image_generate": [
        "生成圖片", "畫圖", "畫一張", "generate image", "create image", "draw",
    ],
    "rss_subscribe": [
        "RSS訂閱", "新增訂閱", "取消訂閱", "RSS feed", "subscribe feed",
    ],
    "run_evidence_admissibility": [
        "卷證索引", "證據能力", "傳聞法則", "證據意見表", "證據分類",
        "hearsay", "admissibility", "刑事訴訟法第159條",
        "偵訊筆錄證據能力", "警詢筆錄傳聞", "證據能力意見",
    ],
    "run_crawler_targets": [
        "爬蟲目標", "爬蟲清單", "crawler targets", "新增爬蟲", "移除爬蟲",
        "管理爬蟲", "爬蟲設定",
    ],
    "query_clients": [
        "查詢當事人", "查當事人", "案件當事人", "找客戶資料", "客戶查詢",
        "search client", "find client", "client info", "當事人資料",
    ],
    "run_db_dual_sync": [
        "資料庫同步", "DB同步", "database sync", "sync database",
    ],
    "run_judicial_tools": [
        "規費", "裁判費", "司法規費", "judicial fee", "court fee",
        "上訴期間", "抗告期間", "再審期間", "appeal period", "appeal deadline",
        "經過時間", "elapsed time", "期間計算",
        "折舊", "折舊試算", "depreciation",
        "霍夫曼", "Hoffman", "一次給付", "lump sum",
        "利息", "違約金", "interest", "penalty",
        "刑度", "加重", "減輕", "sentence", "刑度試算",
        "土地分割", "共有物分割", "land division",
        "土地合併", "應有部分", "land merge",
        "不當得利", "相當租金", "unjust enrichment",
        "繼承", "繼承系統表", "inheritance", "應繼分",
        "共有人", "持分", "co-owner share",
        "資遣費", "特休", "severance", "annual leave",
    ],
    "run_judgment_trend": [
        "判決趨勢", "趨勢分析", "案由分析", "案由統計",
        "判決統計", "見解趨勢", "裁判趨勢", "判決分析",
        "judgment trend", "case trend analysis",
    ],
    # 2026-03-29: Added phrases for skills that previously relied on
    # _try_conversational_intent guide messages (channel-aware routing migration)
    "browser_automation": [
        "開網頁", "開網站", "截圖網頁", "截圖", "幫我開這個網站",
        "browse", "open URL", "screenshot", "navigate",
    ],
    "file_manager": [
        "找檔案", "搜尋檔案", "列出資料夾", "列出檔案", "檔案在哪",
        "search file", "list files", "find file",
    ],
    "github_search": [
        "搜尋github", "找套件", "找repo", "github趨勢", "open source",
        "github search", "trending repos",
    ],
    "deep_think": [
        "深度思考", "仔細分析", "用大腦想", "深入分析", "詳細分析",
        "deep think", "think hard", "analyze deeply",
    ],
    "run_obsidian": [
        "搜尋筆記", "查筆記", "obsidian", "知識庫", "筆記本",
        "obsidian search", "notebook", "vault",
    ],
    "run_legal_attest": [
        "寫存證信函", "草擬存證信函", "發存證信函", "存證信函",
        "律師函", "催告書", "legal attest",
    ],
    "poa_generator": [
        "做委任狀", "開委任狀", "製作委託書", "委任狀", "委託書",
        "power of attorney",
    ],
    "contract_generator": [
        "做委任契約", "草擬契約", "委任契約書", "委任合約",
        "engagement agreement",
    ],
    "receipt_generator": [
        "開收據", "製作收據", "收據", "律師費收據", "receipt",
    ],
    "run_court_hearing_reminder": [
        "開庭提醒", "庭期提醒", "開庭排程", "庭前準備",
        "最近有什麼庭", "明天開庭嗎", "今天有庭嗎",
        "下次開庭", "庭期", "開庭日期",
        "補正期限", "補正提醒", "繳費期限", "繳費提醒",
        "繳了", "交了", "補正了", "已繳", "已補正",
        "關掉提醒", "取消提醒",
        "準備清單", "應備文件", "庭前清單",
        "案件時程", "時程總覽", "全部排程", "所有案件排程",
        "court hearing", "hearing reminder",
    ],
    "screenshot_sorter": [
        "截圖排序", "對話截圖", "排序截圖", "截圖重新命名", "對話排序",
        "LINE截圖", "對話紀錄截圖", "截圖整理", "截圖編號", "幫我排截圖",
        "按照時間排", "重新命名截圖", "截圖浮水印", "iMessage截圖",
        "sort screenshots", "screenshot sort", "chat screenshots",
    ],
    # 2026-04-06: Added missing user-facing skills
    "run_contract_review": [
        "審閱契約", "合約審查", "契約審閱", "合約風險", "審閱合約",
        "合約檢查", "契約檢視", "合約分析", "contract review", "review contract",
        "檢視契約", "看合約", "看契約",
    ],
    # pdf-annotator / pdf-bookmarker: 工具型技能，需指定路徑，由自動化流程觸發，不適合語意路由
    "run_transcript_indexer": [
        "筆錄索引", "建立筆錄索引", "筆錄搜尋", "索引筆錄",
        "transcript index", "index transcript",
    ],
    "run_market_briefing": [
        "股市晨報", "股市分析", "股票追蹤", "市場報告", "股市報告",
        "stock briefing", "market briefing", "今日股市",
    ],
    "run_statutes_vdb": [
        "法規搜尋", "查法規", "查法條", "法規查詢", "搜尋法規",
        "法條搜尋", "法律查詢", "statutes search", "search statutes",
    ],
}


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class EmbeddingRouter:
    def __init__(self):
        import threading as _th
        # skill_name → list of embedding vectors (one per phrase/description)
        self._skill_vectors: Dict[str, List[List[float]]] = {}
        self._skill_descriptions: Dict[str, str] = {}
        self._ready = False
        self._init_error = ""
        self._last_embed_error = 0.0
        self._cooldown_sec = 30.0
        self._lock = _th.Lock()
        self._definitions_mtime = 0.0  # last mtime of definitions.json at init
        self._mtime_check_interval = 120.0  # check every 2 min
        self._last_mtime_check = 0.0
        # Message embedding cache (in-memory LRU)
        self._msg_cache: Dict[str, List[float]] = {}
        self._msg_cache_order: list = []
        self._msg_cache_size = 128

    def initialize(self) -> bool:
        """
        Load skills, compute/load cached embeddings. Returns True if ready.
        """
        try:
            skills = self._load_skills()
            if not skills:
                self._init_error = "no skills loaded"
                logger.warning("⚠️ EmbeddingRouter: no skills found in definitions.json")
                return False

            phrase_map = self._load_phrase_hints()
            cache = self._load_cache()
            if "vectors" not in cache:
                cache["vectors"] = {}
            updated = False
            total_vectors = 0

            # Fix #5: Detect skill set changes → invalidate stale cache entries
            current_skill_set_hash = hashlib.md5(
                json.dumps(sorted(skills.keys()), ensure_ascii=False).encode()
            ).hexdigest()
            if cache.get("_skill_set_hash") != current_skill_set_hash:
                # Prune cache entries for skills no longer in definitions
                stale_keys = [k for k in cache["vectors"] if k.split(":")[0] not in skills]
                if stale_keys:
                    for k in stale_keys:
                        del cache["vectors"][k]
                    logger.info(f"🔄 Pruned {len(stale_keys)} stale cache entries (skill set changed)")
                    updated = True
                cache["_skill_set_hash"] = current_skill_set_hash
                updated = True

            for name, desc in skills.items():
                self._skill_descriptions[name] = desc
                # Build list of texts to embed: description + each phrase hint
                texts = [desc]
                phrases = phrase_map.get(name, [])
                texts.extend(phrases)

                vectors = []
                texts_to_embed = []
                text_keys = []

                for txt in texts:
                    ck = _content_hash(txt)
                    cache_key = f"{name}:{ck}"
                    if cache_key in cache["vectors"]:
                        vectors.append(cache["vectors"][cache_key])
                    else:
                        texts_to_embed.append(txt)
                        text_keys.append(cache_key)

                # Batch embed uncached texts
                if texts_to_embed:
                    new_embs = self._get_embeddings_batch(texts_to_embed)
                    for i, emb in enumerate(new_embs):
                        if emb:
                            vectors.append(emb)
                            cache["vectors"][text_keys[i]] = emb
                            updated = True

                if vectors:
                    self._skill_vectors[name] = vectors
                    total_vectors += len(vectors)

            if updated:
                self._save_cache(cache)

            self._ready = len(self._skill_vectors) > 0
            # Record definitions.json mtime for auto-rebuild detection
            try:
                self._definitions_mtime = os.path.getmtime(_DEFINITIONS_PATH)
            except OSError:
                pass
            self._last_mtime_check = time.monotonic()
            logger.info(
                f"🧭 EmbeddingRouter: {len(self._skill_vectors)} skills, "
                f"{total_vectors} vectors total"
            )
            return self._ready

        except Exception as e:
            self._init_error = str(e)
            logger.error(f"❌ EmbeddingRouter init failed: {e}")
            return False

    def _check_definitions_changed(self):
        """Auto-rebuild if definitions.json was modified since last init."""
        now = time.monotonic()
        if now - self._last_mtime_check < self._mtime_check_interval:
            return
        self._last_mtime_check = now
        try:
            current_mtime = os.path.getmtime(_DEFINITIONS_PATH)
            if current_mtime > self._definitions_mtime:
                logger.info("🔄 definitions.json changed, auto-rebuilding embedding cache")
                self.rebuild_cache()
        except OSError:
            pass

    def route(self, message: str) -> Optional[Tuple[str, float, str]]:
        """
        Route a user message to the best matching skill.
        Uses MAX similarity across all vectors per skill.

        Returns: (skill_name, confidence, tier) or None.
        Tiers: "DIRECT" (≥0.75), "GUIDED" (0.55-0.74), "LOW" (<0.55)
        """
        if not self._ready:
            return None
        self._check_definitions_changed()

        msg = (message or "").strip()
        if not msg:
            return None

        if self._last_embed_error and time.monotonic() - self._last_embed_error < self._cooldown_sec:
            logger.debug(f"EmbeddingRouter in cooldown ({self._cooldown_sec}s after oMLX error), skipping")
            return None

        msg_embedding = self._get_embedding_cached(msg)
        if not msg_embedding:
            self._last_embed_error = time.monotonic()
            return None

        best_skill = ""
        best_score = -1.0

        for skill_name, vectors in self._skill_vectors.items():
            # Max similarity across all vectors for this skill
            max_sim = max(_cosine_similarity(msg_embedding, v) for v in vectors)
            if max_sim > best_score:
                best_score = max_sim
                best_skill = skill_name

        if best_score < 0:
            return None

        # Compute gap between #1 and #2 for logging / disambiguation
        second_best = -1.0
        for skill_name, vectors in self._skill_vectors.items():
            if skill_name == best_skill:
                continue
            max_sim = max(_cosine_similarity(msg_embedding, v) for v in vectors)
            if max_sim > second_best:
                second_best = max_sim

        gap = best_score - second_best if second_best >= 0 else best_score

        # Tier assignment:
        # - DIRECT: high confidence, clear winner
        # - GUIDED: moderate confidence, use as hint
        # - LOW: weak match, probably not a skill command
        # Note: CHAT vs non-CHAT is IntentionClassifier's job, not ours
        if best_score >= _DIRECT_THRESH and gap >= 0.02:
            tier = "DIRECT"
        elif best_score >= _GUIDED_THRESH:
            tier = "GUIDED"
        else:
            tier = "LOW"

        logger.info(
            f"🧭 Route: '{msg[:40]}' → {best_skill} "
            f"({best_score:.3f} gap={gap:.3f} {tier})"
        )
        return (best_skill, best_score, tier)

    def route_top_n(self, message: str, n: int = 3) -> List[Tuple[str, float]]:
        """Return top N skills with max-similarity scores."""
        if not self._ready:
            return []

        msg = (message or "").strip()
        if not msg:
            return []

        msg_embedding = self._get_embedding_cached(msg)
        if not msg_embedding:
            return []

        scores = []
        for skill_name, vectors in self._skill_vectors.items():
            max_sim = max(_cosine_similarity(msg_embedding, v) for v in vectors)
            scores.append((skill_name, max_sim))

        scores.sort(key=lambda x: -x[1])
        return scores[:n]

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def skill_count(self) -> int:
        return len(self._skill_vectors)

    # --- Internal methods ---

    def _load_skills(self) -> Dict[str, str]:
        """Load skill name → description from definitions.json."""
        p = Path(_DEFINITIONS_PATH)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        tools = data.get("tools") or []
        skills = {}
        for t in tools:
            name = str(t.get("name") or "").strip()
            if not name or name in _BLACKLISTED_SKILLS:
                continue
            if any(name.startswith(pat) for pat in _SKIP_NAME_PATTERNS):
                continue
            desc = str(t.get("description") or "").strip()
            if not desc:
                continue
            skills[name] = desc
        return skills

    def _load_phrase_hints(self) -> Dict[str, List[str]]:
        """Load phrase hints from semantic_router + supplementary _EXTRA_PHRASES."""
        phrase_map: Dict[str, List[str]] = {}

        # From semantic_router
        try:
            from skills.bridge.semantic_router import _PHRASE_HINTS
        except ImportError:
            try:
                from .semantic_router import _PHRASE_HINTS
            except ImportError:
                _PHRASE_HINTS = []
                logger.debug("Could not load _PHRASE_HINTS from semantic_router")

        for phrase, skill_name, _score in _PHRASE_HINTS:
            if skill_name not in phrase_map:
                phrase_map[skill_name] = []
            phrase_map[skill_name].append(phrase)

        # Merge supplementary phrases
        for skill_name, phrases in _EXTRA_PHRASES.items():
            if skill_name not in phrase_map:
                phrase_map[skill_name] = []
            for p in phrases:
                if p not in phrase_map[skill_name]:
                    phrase_map[skill_name].append(p)

        return phrase_map

    def _get_embedding_cached(self, text: str) -> Optional[List[float]]:
        """Get embedding with in-memory cache."""
        key = _content_hash(text)
        if key in self._msg_cache:
            return self._msg_cache[key]

        emb = self._get_embedding(text)
        if emb:
            self._msg_cache[key] = emb
            self._msg_cache_order.append(key)
            if len(self._msg_cache_order) > self._msg_cache_size:
                old = self._msg_cache_order.pop(0)
                self._msg_cache.pop(old, None)
        return emb

    def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Get single embedding vector from oMLX."""
        try:
            resp = _http.post(
                _OMLX_EMBED_URL,
                json={"model": _EMBED_MODEL, "input": text},
                timeout=_EMBED_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                emb_data = data.get("data") or []
                if emb_data:
                    return emb_data[0].get("embedding")
            else:
                logger.warning(f"⚠️ Embedding API returned {resp.status_code}")
        except Exception as e:
            logger.debug(f"Embedding API error: {e}")
        return None

    def _get_embeddings_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Get embeddings for multiple texts, batching API calls."""
        results: List[Optional[List[float]]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            try:
                resp = _http.post(
                    _OMLX_EMBED_URL,
                    json={"model": _EMBED_MODEL, "input": batch},
                    timeout=_EMBED_TIMEOUT * 2,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    emb_list = data.get("data") or []
                    # Sort by index to ensure order matches input
                    emb_list.sort(key=lambda x: x.get("index", 0))
                    for item in emb_list:
                        results.append(item.get("embedding"))
                    # Pad if fewer results than inputs
                    while len(results) < i + len(batch):
                        results.append(None)
                else:
                    logger.warning(f"⚠️ Batch embedding returned {resp.status_code}")
                    results.extend([None] * len(batch))
            except Exception as e:
                logger.debug(f"Batch embedding error: {e}")
                results.extend([None] * len(batch))
        return results

    def _load_cache(self) -> dict:
        """Load cached embeddings from disk."""
        cache_path = os.path.join(_CACHE_DIR, "skill_vectors.json")
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.debug(f"Cache load error: {e}")
        return {"vectors": {}}

    def _save_cache(self, cache: dict):
        """Save embeddings cache to disk."""
        os.makedirs(_CACHE_DIR, exist_ok=True)
        cache_path = os.path.join(_CACHE_DIR, "skill_vectors.json")
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            logger.info(f"💾 Vector cache saved ({len(cache.get('vectors', {}))} entries)")
        except Exception as e:
            logger.warning(f"⚠️ Cache save error: {e}")

    def rebuild_cache(self) -> bool:
        """Force rebuild all embeddings (thread-safe)."""
        with self._lock:
            cache_path = os.path.join(_CACHE_DIR, "skill_vectors.json")
            try:
                if os.path.exists(cache_path):
                    os.remove(cache_path)
            except OSError:
                pass
            self._skill_vectors.clear()
            self._ready = False
            return self.initialize()


# Module-level singleton
_router: Optional[EmbeddingRouter] = None


def get_router() -> EmbeddingRouter:
    """Get or create the singleton EmbeddingRouter."""
    global _router
    if _router is None:
        _router = EmbeddingRouter()
        _router.initialize()
    return _router


def route(message: str) -> Optional[Tuple[str, float, str]]:
    """Convenience: route a message using the singleton."""
    return get_router().route(message)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    router = EmbeddingRouter()
    ok = router.initialize()
    print(f"Router ready: {ok}, skills: {router.skill_count}")

    tests = [
        "搜尋東京天氣",
        "翻譯這段文字",
        "幫我查最新判決",
        "今天有沒有開庭",
        "你好嗎",
        "加班費怎麼算",
        "幫我記住這件事",
        "What is the meaning of life?",
        "逐字稿",
        "股市晨報",
        "執行網路研究：台灣AI產業",
        "幫我把這個PDF重新命名",
    ]

    for msg in tests:
        result = router.route(msg)
        if result:
            name, score, tier = result
            top3 = router.route_top_n(msg, 3)
            others = ", ".join(f"{n}:{s:.2f}" for n, s in top3[1:])
            print(f"  [{msg}] → {name} ({score:.3f}) [{tier}]  | {others}")
        else:
            print(f"  [{msg}] → no match")
    print()
