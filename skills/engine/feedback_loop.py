"""
回饋學習系統 — 從使用者行為學習路由和回覆品質
=============================================
Phase 4: 隱式回饋偵測 + 路由權重調整 + Prompt A/B test
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("FeedbackLoop")

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2])))
FEEDBACK_PATH = MAGI_ROOT / ".agent" / "routing_feedback.json"
PROMPT_STATS_PATH = MAGI_ROOT / ".agent" / "prompt_stats.json"
MAX_ENTRIES = 1000  # 最多保留的回饋紀錄


class RoutingFeedback:
    """從使用者反應學習路由品質。"""

    def __init__(self):
        self._entries: list[dict] = []
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        try:
            if FEEDBACK_PATH.exists():
                self._entries = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
        except Exception:
            self._entries = []
        self._loaded = True

    def _save(self):
        try:
            FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
            # 只保留最近的紀錄
            if len(self._entries) > MAX_ENTRIES:
                self._entries = self._entries[-MAX_ENTRIES:]
            FEEDBACK_PATH.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save routing feedback: %s", e)

    def record(self, query: str, routed_to: str, outcome: str, details: str = ""):
        """
        記錄一次路由回饋。

        Args:
            query: 使用者原始訊息
            routed_to: 被路由到的目標（技能名/chat/react）
            outcome: "correct" | "wrong" | "partial" | "no_response"
            details: 額外說明
        """
        self._ensure_loaded()
        self._entries.append({
            "query": query[:200],
            "routed_to": routed_to,
            "outcome": outcome,
            "details": details[:200],
            "ts": time.time(),
        })
        self._save()

    def get_skill_accuracy(self, min_samples: int = 5) -> dict[str, dict[str, Any]]:
        """計算每個路由目標的準確率。"""
        self._ensure_loaded()
        stats: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "wrong": 0, "total": 0})

        for e in self._entries[-500:]:
            skill = e.get("routed_to", "")
            if not skill:
                continue
            stats[skill]["total"] += 1
            if e.get("outcome") == "correct":
                stats[skill]["correct"] += 1
            elif e.get("outcome") in ("wrong", "no_response"):
                stats[skill]["wrong"] += 1

        result = {}
        for skill, s in stats.items():
            if s["total"] >= min_samples:
                accuracy = s["correct"] / s["total"] if s["total"] > 0 else 0
                result[skill] = {
                    "accuracy": round(accuracy, 3),
                    "total": s["total"],
                    "correct": s["correct"],
                    "wrong": s["wrong"],
                }
        return result

    def compute_threshold_adjustments(self) -> dict[str, float]:
        """根據準確率計算 embedding router 的閾值調整。"""
        accuracies = self.get_skill_accuracy(min_samples=5)
        adjustments = {}
        for skill, data in accuracies.items():
            # 準確率 > 0.8 → 降低閾值（更容易觸發）
            # 準確率 < 0.5 → 提高閾值（更難觸發）
            adj = (data["accuracy"] - 0.7) * 0.05  # ±0.015 範圍
            adjustments[skill] = round(adj, 4)
        return adjustments


class ImplicitFeedbackDetector:
    """從使用者行為隱式推斷回饋。"""

    NEGATIVE_PATTERNS = [
        "不是", "不對", "我是問", "我說的是", "你搞錯",
        "重新", "再試", "換一個", "不是這個", "答非所問",
        "聽不懂", "看不懂", "太長了",
    ]
    POSITIVE_PATTERNS = [
        "好", "謝謝", "對", "收到", "了解", "感謝",
        "太好了", "完美", "正確", "沒錯",
    ]

    def detect(self, current_msg: str, last_query: str = "", last_response: str = "") -> str | None:
        """
        從使用者的後續訊息推斷前一次回覆的品質。

        Returns:
            "correct" | "wrong" | None (無法判斷)
        """
        msg = current_msg.strip()
        if not msg:
            return None

        # 負面關鍵字優先檢查（「不對」包含「對」，所以負面要先判）
        if any(p in msg for p in self.NEGATIVE_PATTERNS):
            return "wrong"

        # 短訊息 + 正面關鍵字 → 正確（>10 字的訊息可能是新問題，不算回饋）
        if len(msg) <= 10 and any(p in msg for p in self.POSITIVE_PATTERNS):
            return "correct"

        # 很快重新問類似問題 → 前一次可能沒回答好
        if last_query and len(msg) > 15:
            overlap = sum(1 for c in msg if c in last_query)
            ratio = overlap / max(len(msg), 1)
            if ratio > 0.5 and len(msg) > len(last_query) * 0.8:
                return "wrong"

        return None


# 全域單例
_routing_feedback = RoutingFeedback()
_implicit_detector = ImplicitFeedbackDetector()


def record_feedback(query: str, routed_to: str, outcome: str, details: str = ""):
    """記錄路由回饋（全域入口）。"""
    _routing_feedback.record(query, routed_to, outcome, details)


def detect_implicit_feedback(current_msg: str, last_query: str = "", last_response: str = "") -> str | None:
    """偵測隱式回饋（全域入口）。"""
    return _implicit_detector.detect(current_msg, last_query, last_response)


def get_accuracy_report(min_samples: int = 5) -> dict:
    """取得路由準確率報告。"""
    return _routing_feedback.get_skill_accuracy(min_samples=min_samples)


def get_threshold_adjustments() -> dict[str, float]:
    """取得閾值調整建議。"""
    return _routing_feedback.compute_threshold_adjustments()
