import logging
import os
import re
import time
from collections import deque

import requests
try:
    from skills.bridge.http_pool import get_session as _get_session
    _http = _get_session()
except ImportError:
    _http = requests.Session()

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("IntentionClassifier")

# Inference Configuration (oMLX primary, InferenceGateway fallback)
_OMLX_BASE = os.environ.get("CASPER_CLASSIFIER_OMLX_URL", "http://127.0.0.1:8080")
_OMLX_CHAT_URL = _OMLX_BASE.rstrip("/") + "/v1/chat/completions"
MODEL_NAME = os.environ.get("CASPER_CLASSIFIER_MODEL", "TAIDE-12b-Chat-mlx-4bit")
LLM_TIMEOUT_SEC = max(2, int(os.environ.get("CASPER_CLASSIFIER_TIMEOUT_SEC", "15") or "15"))
LLM_COOLDOWN_SEC = max(5, int(os.environ.get("CASPER_CLASSIFIER_COOLDOWN_SEC", "30") or "30"))


_CACHE_PERSIST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".agent", "intent_classifier_cache.json",
)


class IntentionClassifier:
    def __init__(self, use_llm=None, cache_size=256):
        # Env var CASPER_CLASSIFIER_USE_LLM=1 enables LLM fallback (default: on, using local model)
        if use_llm is None:
            use_llm = os.environ.get("CASPER_CLASSIFIER_USE_LLM", "1") == "1"
        self.use_llm = use_llm
        self.cache_size = cache_size
        self._cache = {}
        self._cache_order = deque()
        self._llm_cooldown_until = 0.0
        self._last_llm_error = ""
        self._cache_dirty_count = 0
        self._load_persistent_cache()
        logger.info(f"🔮 Intention Classifier Initialized (LLM={use_llm}, cached={len(self._cache)})")

    def _load_persistent_cache(self):
        """Load cached intent results from disk on startup."""
        try:
            if os.path.exists(_CACHE_PERSIST_PATH):
                import json
                with open(_CACHE_PERSIST_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    items = data.get("items", {})
                    if isinstance(items, dict):
                        # Only load up to cache_size items
                        for key, value in list(items.items())[:self.cache_size]:
                            self._cache[key] = value
                            self._cache_order.append(key)
        except Exception as e:
            logger.debug("Intent classifier cache load skipped: %s", e)

    def _save_persistent_cache(self):
        """Save cache to disk periodically (every 20 new entries)."""
        self._cache_dirty_count += 1
        if self._cache_dirty_count < 20:
            return
        self._cache_dirty_count = 0
        self._flush_cache()

    def _flush_cache(self):
        """Force write cache to disk."""
        try:
            import json
            os.makedirs(os.path.dirname(_CACHE_PERSIST_PATH), exist_ok=True)
            payload = {
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "items": dict(self._cache),
            }
            import tempfile
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(_CACHE_PERSIST_PATH), suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, _CACHE_PERSIST_PATH)
        except Exception as e:
            logger.debug("Intent classifier cache save failed: %s", e)

    def _cache_get(self, key):
        return self._cache.get(key)

    def _cache_set(self, key, value):
        if key in self._cache:
            self._cache[key] = value
            return
        self._cache[key] = value
        self._cache_order.append(key)
        if len(self._cache_order) > self.cache_size:
            oldest = self._cache_order.popleft()
            self._cache.pop(oldest, None)
        self._save_persistent_cache()

    # Pre-compiled regex patterns for _check_regex_rules (avoid per-message re.compile overhead)
    _RE_CHAT = re.compile(r"(你覺得|你認為|心情|閒聊|聊聊|笑話|開玩笑|早安|晚安|哈囉|你好|你會做什麼|你能做什麼|你可以做什麼)", re.IGNORECASE)
    _RE_DANGER = re.compile(r"(rm\s+-rf|drop\s+table|delete\s+from|truncate\s+table)", re.IGNORECASE)
    _RE_SYS_CTRL = re.compile(r"\b(reboot|restart|shutdown|update|upgrade|deploy|rollback)\b", re.IGNORECASE)
    _RE_SEARCH_CMD = re.compile(r"^(search|find|query|check|lookup|research|fetch|grab)\s", re.IGNORECASE)
    _RE_TRANSLATE_CMD = re.compile(r"^(translate|翻譯)\s", re.IGNORECASE)
    _RE_CN_ACTION = re.compile(r"^(?:執行|進行|開始|啟動)\s*(?:網路研究|網路搜尋|搜尋|研究|爬蟲)")
    _RE_CN_RESEARCH = re.compile(r"^(?:網路研究|網路搜尋)\s*[:：]?\s*\S")
    _RE_HELP_CMD = re.compile(r"(幫我|麻煩|請|我要|我想|我需要).*(同步|下載|新增|移除|設定|關閉|開啟|切換|執行|啟動|停止|重啟|翻譯|摘要|檢查|掃描|找判決|跑夜間任務|夜間任務|校準|修理|逐字稿|轉錄|分析|辨識)", re.IGNORECASE)
    _RE_QUESTION_END = re.compile(r"[嗎呢？?]$")
    _RE_LEGAL_OPS = re.compile(r"(同步筆錄|閱卷|法扶|大腦模式|夜間任務|自動巡檢|新增爬蟲|移除爬蟲|翻譯檔案|完整翻譯|摘要翻譯)", re.IGNORECASE)
    _RE_MODEL_Q = re.compile(r"(目前模型|現在.*模型|用哪個模型|使用.*模型|model status|系統狀態|status)", re.IGNORECASE)
    _RE_QUERY_TERMS = re.compile(r"(查一下|找一下|有沒有|多少|何時|最新|進度|為什麼|怎麼|如何|下週|今天|明天|想知道|我想知道)", re.IGNORECASE)
    _RE_QUERY_PARTICLE = re.compile(r"[？?]|(嗎|呢|為何|怎麼|如何|想知道|我想知道)")
    _RE_IMPLICIT_Q = re.compile(r"(現在|目前|今天|明天|下週|這週).{0,10}(天氣|氣溫|溫度|匯率|價格|股價|指數|新聞|進度)")
    _RE_RULE = re.compile(r"^(規則|rule)\s*[:：]?\s*", re.IGNORECASE)
    _RE_GENERAL_CMD = re.compile(r"(幫我找|幫我查|幫我搜|請搜尋|網路搜尋|網路研究|抓取|讀取網頁|翻譯|translate|執行|啟動|停止|重啟|切換|生成圖片|畫圖)", re.IGNORECASE)
    _RE_MEMORY_CMD = re.compile(r"^記住\s*[:：]?\s*\S")
    _RE_MEMORY_STORE = re.compile(r"(歸入|存入|寫入|加入).*(向量|資料庫|記憶|memory)")
    _RE_VISION_CMD = re.compile(r"(逐字稿|語音轉文字|轉錄音訊|音訊轉文字|分析.*(?:圖|照片|截圖|圖片)|(?:圖|照片|截圖).*(?:分析|辨識|描述)|看圖說話|OCR|文字辨識)", re.IGNORECASE)
    _RE_LEGAL_EN = re.compile(r"\b(meeting|court|schedule|laf|agenda|calendar|hearing)\b", re.IGNORECASE)

    def _check_regex_rules(self, text):
        """
        Fast-path regex rules for obvious commands.
        Returns intent or None.
        """
        text = text.strip()

        if not text:
            return "CHAT"

        if self._RE_CHAT.search(text):
            return "CHAT"
        if self._RE_DANGER.search(text):
            return "DANGER"
        if text.startswith("/") or text.startswith("!") or text.lower().startswith("@magi"):
            return "CMD"
        if self._RE_SYS_CTRL.search(text):
            return "CMD"
        if self._RE_SEARCH_CMD.search(text):
            return "CMD"
        if self._RE_TRANSLATE_CMD.search(text):
            return "CMD"
        if self._RE_CN_ACTION.search(text):
            return "CMD"
        if self._RE_CN_RESEARCH.search(text):
            return "CMD"
        if self._RE_HELP_CMD.search(text):
            if self._RE_QUESTION_END.search(text.strip()):
                return "QUERY"
            return "CMD"
        if self._RE_LEGAL_OPS.search(text):
            return "CMD"
        if self._RE_MODEL_Q.search(text):
            return "QUERY"
        if self._RE_QUERY_TERMS.search(text) and self._RE_QUERY_PARTICLE.search(text):
            return "QUERY"
        if self._RE_IMPLICIT_Q.search(text):
            return "QUERY"
        if self._RE_RULE.search(text):
            return "CHAT"
        if self._RE_GENERAL_CMD.search(text):
            return "CMD"
        if self._RE_MEMORY_CMD.search(text):
            return "CMD"
        if self._RE_MEMORY_STORE.search(text):
            return "CMD"
        if self._RE_VISION_CMD.search(text):
            return "CMD"
        if self._RE_LEGAL_EN.search(text):
            return "CMD"

        return None

    def _heuristic_classify(self, text):
        """
        Lightweight scoring fallback when regex is inconclusive.
        """
        t = text.lower().strip()
        if not t:
            return "CHAT"

        query_terms = [
            "what", "who", "when", "where", "why", "how", "latest", "news", "price",
            "什麼", "誰", "何時", "哪裡", "為何", "怎麼", "最新", "新聞", "多少", "查詢", "介紹",
            "翻譯", "摘要", "查", "分析", "辨識", "檢查", "幫我查",
        ]
        cmd_terms = [
            "please", "run", "execute", "open", "switch", "restart", "generate",
            "執行", "啟動", "打開", "切換", "建立", "修改", "刪除", "畫",
            "同步", "下載", "新增", "移除", "關閉", "開啟", "掃描", "重啟",
        ]
        chat_terms = [
            "hello", "hi", "thanks", "thank you", "lol", "haha",
            "哈囉", "你好", "謝謝", "早安", "晚安", "聊天",
            "還活著", "過得好", "你是誰", "在嗎", "忙嗎",
        ]

        query_score = sum(1 for k in query_terms if k in t)
        cmd_score = sum(1 for k in cmd_terms if k in t)
        chat_score = sum(1 for k in chat_terms if k in t)

        if "?" in t or "？" in t:
            query_score += 1
        if re.search(r"(嗎|呢|如何|怎麼|為何|多少|有沒有|進度|最新)", t):
            query_score += 1
        if re.search(r"(幫我|請|麻煩|我要|我想|我需要).*(同步|下載|新增|移除|設定|關閉|開啟|切換|執行|翻譯|摘要|檢查)", t):
            # Don't boost CMD score if the sentence is a question
            if not re.search(r"[嗎呢？?]", t):
                cmd_score += 2

        if cmd_score >= 2 and cmd_score >= query_score:
            return "CMD"
        if query_score >= 2:
            return "QUERY"
        if chat_score > 0 and cmd_score == 0:
            return "CHAT"
        if query_score == 1 and cmd_score == 0:
            return "QUERY"
        return "CHAT"

    def _embedding_classify(self, text: str) -> tuple:
        """
        Use EmbeddingRouter to infer intent from skill-matching score.
        Returns (intent, confidence) or (None, 0.0) if unavailable.
        Replaces slow LLM fallback — runs in ~0.1s vs 15s.
        """
        try:
            from skills.bridge.embedding_router import route
            result = route(text)
            if result is None:
                return (None, 0.0)
            _skill, score, tier = result
            if tier == "DIRECT":
                return ("CMD", score)
            elif score > 0.5:
                return ("QUERY", score)
            else:
                return ("CHAT", 1.0 - score)
        except Exception as e:
            logger.debug(f"EmbeddingRouter classify fallback: {e}")
            return (None, 0.0)

    def classify(self, text):
        """
        Determines the intent of the message.
        Returns: 'CHAT', 'QUERY', 'CMD', 'DANGER'

        Pipeline (ensemble, no LLM):
          1. Cache hit → return immediately
          2. Regex rules → high confidence, stored as candidate
          3. Embedding Router → fast cosine similarity intent
          4. Ensemble: regex > embedding (high) > heuristic
        """
        key = (text or "").strip().lower()
        cached = self._cache_get(key)
        if cached:
            return cached

        # --- Layer 1: Regex (high confidence but not absolute) ---
        regex_intent = self._check_regex_rules(text)
        if regex_intent:
            # DANGER from regex is always authoritative
            if regex_intent == "DANGER":
                logger.info(f"⚡ Regex Matched: DANGER")
                self._cache_set(key, "DANGER")
                return "DANGER"
            # For non-DANGER, regex is a strong signal but check embedding too
            regex_conf = 0.9
        else:
            regex_conf = 0.0

        # --- Layer 2: LLM 判斷（主要分類器）---
        llm_intent = self._ask_llm(text)

        if llm_intent and llm_intent in ("CHAT", "QUERY", "CMD", "DANGER"):
            # LLM 成功 → 直接採用（LLM 是最聰明的分類器）
            logger.info(f"🧠 LLM Classified: {llm_intent}")
            self._cache_set(key, llm_intent)
            return llm_intent

        # --- Layer 3: Embedding + Heuristic fallback（LLM 失敗時）---
        embed_intent, embed_conf = self._embedding_classify(text)
        heuristic_intent = self._heuristic_classify(text)

        # 高信心 regex
        if regex_conf >= 0.9 and regex_intent:
            logger.info(f"⚡ Regex Matched: {regex_intent}")
            self._cache_set(key, regex_intent)
            return regex_intent

        # 高信心 embedding（≥ 0.8 DIRECT）
        if embed_intent and embed_conf >= 0.8:
            logger.info(f"🧭 Embedding High: {embed_intent} (conf={embed_conf:.2f})")
            self._cache_set(key, embed_intent)
            return embed_intent

        # Heuristic fallback
        logger.info(f"📊 Heuristic Fallback: {heuristic_intent}")
        self._cache_set(key, heuristic_intent)
        return heuristic_intent

    def _ask_llm(self, text):
        """
        Asks oMLX (OpenAI-compatible) to classify the intent.
        Falls back to InferenceGateway if oMLX is unavailable.
        """
        if time.monotonic() < self._llm_cooldown_until:
            return ""

        try:
            from skills.bridge.llm_direct import classify_intent_with_codex, feature_enabled as _codex_feature_enabled

            if _codex_feature_enabled("intent"):
                codex_res = classify_intent_with_codex(
                    text,
                    timeout_sec=int(os.environ.get("MAGI_CODEX_INTENT_TIMEOUT_SEC", "120") or "120"),
                )
                label = str(codex_res.get("intent") or codex_res.get("label") or codex_res.get("text") or "").strip().upper()
                if label in {"CHAT", "QUERY", "CMD", "DANGER"}:
                    return label
                if codex_res.get("error"):
                    logger.warning("Codex intent route failed: %s", codex_res.get("error"))
        except Exception as codex_err:
            logger.warning("Codex intent route skipped: %s", codex_err)

        system_prompt = (
            "Classify the following user message into exactly ONE of these categories:\n"
            "- CHAT: Casual conversation, greetings, jokes, philosophical questions.\n"
            "- QUERY: Asking for factual information, database lookups, news, or specific knowledge.\n"
            "- CMD: Asking the system to perform an action, change settings, or run a tool.\n"
            "- DANGER: Destructive or security-sensitive instructions.\n\n"
            "Reply ONLY with one category name: CHAT, QUERY, CMD, or DANGER."
        )

        def _extract_intent(raw: str) -> str:
            result = (raw or "").strip().upper()
            for valid in ["DANGER", "CMD", "QUERY", "CHAT"]:
                if valid in result:
                    return valid
            return ""

        # --- Primary: oMLX via OpenAI-compatible /v1/chat/completions ---
        try:
            payload = {
                "model": MODEL_NAME,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.1,
                "max_tokens": 8,
                "stream": False,
            }
            response = _http.post(_OMLX_CHAT_URL, json=payload, timeout=LLM_TIMEOUT_SEC)
            if response.status_code == 200:
                data = response.json()
                choices = data.get("choices") or []
                if choices:
                    raw = (choices[0].get("message") or {}).get("content", "")
                    intent = _extract_intent(raw)
                    if intent:
                        return intent
                    logger.warning(f"⚠️ oMLX returned unclear intent: {raw}")
            elif response.status_code in {429, 503}:
                self._last_llm_error = f"busy:{response.status_code}"
            else:
                logger.warning(f"⚠️ oMLX intent error: {response.status_code}")
        except Exception as e:
            logger.debug(f"oMLX intent unavailable: {e}")

        # --- Fallback: InferenceGateway (tries all available routes) ---
        try:
            from skills.bridge.inference_gateway import InferenceGateway
            _gw = InferenceGateway()
            fb = _gw.chat(
                f"{system_prompt}\n\nMessage: \"{text}\"",
                task_type="intent",
                timeout=max(LLM_TIMEOUT_SEC, 8),
            )
            if fb.get("success"):
                intent = _extract_intent(fb.get("response", ""))
                if intent:
                    return intent
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 397, exc_info=True)

        logger.error(f"❌ Classification Failed: oMLX and InferenceGateway both unavailable")
        self._last_llm_error = "all_routes_failed"
        self._llm_cooldown_until = time.monotonic() + LLM_COOLDOWN_SEC
        return ""


if __name__ == "__main__":
    classifier = IntentionClassifier()
    tests = [
        "Hello there!",
        "Check the latest court dates",
        "/restart system",
        "What represents the number 42?",
        "rm -rf /",
    ]

    for phrase in tests:
        print(f"[{phrase}] -> {classifier.classify(phrase)}")
