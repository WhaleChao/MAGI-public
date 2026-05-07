"""
持續知識進化 — 從對話中即時擷取可複用知識 + 記憶管理
===================================================
Phase 6: 取代週末批次蒸餾，改為即時擷取 + 品質管理
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("KnowledgeExtractor")

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2])))
EXTRACT_STATS_PATH = MAGI_ROOT / ".agent" / "knowledge_extract_stats.json"

# 最小內容長度（太短的對話不值得擷取）
MIN_CONTENT_LEN = 50
# 擷取的冷卻時間（同一使用者連續對話不要每句都擷取）
EXTRACT_COOLDOWN_SEC = 300
# 擷取的關鍵字（出現這些關鍵字時更有可能包含可複用知識）
KNOWLEDGE_INDICATORS = [
    "法院認為", "法院審酌", "依據", "判決", "裁定",
    "根據", "依照", "第.*條", "應解為",
    "重要", "注意", "不要忘記", "記住", "以後",
    "SOP", "流程", "步驟", "規則",
]

_last_extract_ts: dict[str, float] = {}  # user_id → last extract timestamp
_extract_lock = threading.Lock()


def should_extract(user_id: str, query: str, answer: str) -> bool:
    """判斷這段對話是否值得擷取知識。"""
    # 長度檢查
    if len(answer) < MIN_CONTENT_LEN:
        return False

    # 冷卻檢查
    now = time.time()
    last = _last_extract_ts.get(user_id, 0)
    if now - last < EXTRACT_COOLDOWN_SEC:
        return False

    # 內容品質檢查
    combined = query + answer
    indicator_count = sum(1 for kw in KNOWLEDGE_INDICATORS if kw in combined)
    if indicator_count >= 2:
        return True

    # 長回覆（>200 字）本身就可能有價值
    if len(answer) > 200:
        return True

    return False


def extract_and_store(user_id: str, query: str, answer: str, source: str = "conversation"):
    """
    從對話中擷取知識並存入記憶庫。
    在背景執行緒中運行，不阻塞主流程。
    """
    if not should_extract(user_id, query, answer):
        return

    def _do_extract():
        try:
            from skills.bridge.llm_direct import chat

            # 用 LLM 判斷是否包含可複用知識
            prompt = (
                "以下對話是否包含可供未來參考的知識、法律見解、或實務經驗？\n"
                "如果有，請用一段精簡的繁體中文摘要（不超過 100 字）。\n"
                "如果沒有，只回覆「無」。\n\n"
                f"問：{query[:500]}\n"
                f"答：{answer[:1500]}"
            )

            result = chat(prompt=prompt, feature="intent", max_tokens=150, timeout=30)
            if not result.get("success"):
                return

            text = result.get("text", "").strip()
            if text == "無" or len(text) < 10:
                return

            # 存入記憶
            from skills.memory.mem_bridge import remember
            remember(
                content=text,
                source=f"knowledge_extract:{source}:{user_id}",
            )

            # 更新冷卻時間
            with _extract_lock:
                _last_extract_ts[user_id] = time.time()

            logger.info("Knowledge extracted from conversation with %s: %s", user_id, text[:80])
            _update_stats("extracted")

        except Exception as e:
            logger.debug("Knowledge extraction failed: %s", e)
            _update_stats("failed")

    threading.Thread(target=_do_extract, daemon=True).start()


def _update_stats(outcome: str):
    """更新擷取統計。"""
    try:
        stats = {}
        if EXTRACT_STATS_PATH.exists():
            stats = json.loads(EXTRACT_STATS_PATH.read_text(encoding="utf-8"))
        stats[outcome] = stats.get(outcome, 0) + 1
        stats["last_update"] = time.time()
        EXTRACT_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        EXTRACT_STATS_PATH.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def get_stats() -> dict:
    """取得擷取統計。"""
    try:
        if EXTRACT_STATS_PATH.exists():
            return json.loads(EXTRACT_STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


class MemoryManager:
    """記憶品質管理 — 衰減、合併、驗證。"""

    def decay_old_memories(self, months: int = 6):
        """
        標記超過指定月數未被召回的記憶為低優先。
        不刪除，只是在檢索時降權。

        這個方法設計為由 cron job 定期呼叫（每月一次）。
        """
        try:
            from skills.memory.mem_bridge import _get_conn
            conn = _get_conn()
            cursor = conn.cursor()

            cutoff_ts = time.time() - (months * 30 * 86400)
            cursor.execute(
                """
                UPDATE memories
                SET priority = GREATEST(priority - 1, 0)
                WHERE last_recalled_at < FROM_UNIXTIME(%s)
                  AND priority > 0
                  AND source NOT LIKE 'statute%%'
                """,
                (cutoff_ts,),
            )
            affected = cursor.rowcount
            conn.commit()
            if affected > 0:
                logger.info("Memory decay: %d memories deprioritized", affected)
            return {"decayed": affected}
        except Exception as e:
            logger.warning("Memory decay failed: %s", e)
            return {"error": str(e)}

    def get_memory_stats(self) -> dict:
        """取得記憶庫統計。"""
        try:
            from skills.memory.mem_bridge import _get_conn
            conn = _get_conn()
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM memories")
            total = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(DISTINCT source) FROM memories")
            sources = cursor.fetchone()[0]

            cursor.execute(
                "SELECT source, COUNT(*) as cnt FROM memories GROUP BY source ORDER BY cnt DESC LIMIT 10"
            )
            top_sources = [{"source": r[0], "count": r[1]} for r in cursor.fetchall()]

            return {
                "total_memories": total,
                "distinct_sources": sources,
                "top_sources": top_sources,
            }
        except Exception as e:
            return {"error": str(e)}
