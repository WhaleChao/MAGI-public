# -*- coding: utf-8 -*-
"""
coreml_classifier.py
====================
Core ML 文件分類模組。

使用 Core ML 在 Neural Engine 上做文件分類，不佔 GPU。
高信心度（>0.8）直接採用，低信心度才送 Gemma-4 LLM。

整合點：
- skills/pdf-namer/：PDF 命名前先用 Core ML 分類
- pipelines/attachment_pipeline.py：附件上傳時自動分類
- 定期用新資料 retrain（nightly_distill_train）
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("CoreMLClassifier")

# ---------------------------------------------------------------------------
# Core ML 載入（可選依賴）
# ---------------------------------------------------------------------------
_COREML_AVAILABLE = False
_ct = None

try:
    import coremltools as ct_mod
    _ct = ct_mod
    _COREML_AVAILABLE = True
except ImportError:
    logger.info("coremltools not installed — Core ML classifier disabled. Install: pip install coremltools")

# ---------------------------------------------------------------------------
# 文件分類類別
# ---------------------------------------------------------------------------

CATEGORIES = [
    "判決書", "裁定", "起訴書", "答辯狀", "準備書狀",
    "筆錄", "傳票", "調解通知", "支付命令", "聲請狀",
    "委任狀", "證據清單", "函文", "庭通知書", "書狀存底",
    "郵件回執", "其他",
]

# 關鍵詞快速分類（信心度極高的情況直接用 regex）
_KEYWORD_RULES = [
    (r"判\s*決\s*書|判\s*決\s*主\s*文", "判決書"),
    (r"裁\s*定", "裁定"),
    (r"起\s*訴\s*書|起\s*訴\s*狀", "起訴書"),
    (r"答\s*辯\s*狀", "答辯狀"),
    (r"準\s*備\s*書\s*狀|準\s*備\s*[（(]?一?[）)]?\s*狀", "準備書狀"),
    (r"(?:審判|準備程序|言詞辯論)\s*筆\s*錄", "筆錄"),
    (r"傳\s*票|開庭通知", "傳票"),
    (r"調\s*解\s*(?:通知|期日)", "調解通知"),
    (r"支\s*付\s*命\s*令", "支付命令"),
    (r"聲\s*請\s*狀", "聲請狀"),
    (r"委\s*任\s*狀", "委任狀"),
    (r"證\s*據\s*清\s*單", "證據清單"),
    (r"庭\s*通\s*知\s*書", "庭通知書"),
]

_COMPILED_RULES = [(re.compile(pattern), category) for pattern, category in _KEYWORD_RULES]


def is_available() -> bool:
    """Check if Core ML classifier is available."""
    return _COREML_AVAILABLE


class DocumentClassifier:
    """
    Core ML 文件分類器。

    在 Neural Engine 上執行，不佔 GPU 記憶體。
    """

    def __init__(self, model_path: Optional[str] = None):
        """
        初始化分類器。

        Args:
            model_path: .mlmodel 或 .mlpackage 路徑。
                        None 則只使用 keyword rules。
        """
        self.model = None
        self.model_path = model_path

        if model_path and _COREML_AVAILABLE:
            try:
                self.model = _ct.models.MLModel(model_path)
                logger.info("Core ML model loaded: %s", model_path)
            except Exception as e:
                logger.warning("Failed to load Core ML model: %s", e)

    def classify(
        self,
        text: str,
        threshold: float = 0.8,
    ) -> tuple[str, float]:
        """
        分類文件。

        先嘗試 keyword rules（快速、高��心度），
        再嘗試 Core ML model，
        最後回傳 unknown。

        Args:
            text: 文件文字（前 500 字足夠）
            threshold: 信心度門檻

        Returns:
            (category, confidence) — 信心度低於 threshold 時回傳 ("unknown", confidence)
        """
        if not text or not text.strip():
            return ("unknown", 0.0)

        # Phase 1: Keyword rules（最快，信心度 0.95）
        category = self._classify_by_keywords(text)
        if category:
            return (category, 0.95)

        # Phase 2: Core ML model
        if self.model is not None:
            try:
                prediction = self.model.predict({"text": text[:500]})
                label = prediction.get("label", "unknown")
                probs = prediction.get("labelProbability", {})
                confidence = max(probs.values()) if probs else 0.0

                if confidence >= threshold and label in CATEGORIES:
                    return (label, confidence)
                return ("unknown", confidence)
            except Exception as e:
                logger.debug("Core ML prediction failed: %s", e)

        return ("unknown", 0.0)

    def _classify_by_keywords(self, text: str) -> Optional[str]:
        """使用關鍵詞規則快速分類。"""
        # 取前 500 字
        sample = text[:500]
        for pattern, category in _COMPILED_RULES:
            if pattern.search(sample):
                return category
        return None

    def classify_batch(
        self,
        texts: list[str],
        threshold: float = 0.8,
    ) -> list[tuple[str, float]]:
        """
        批次分類。

        Args:
            texts: 文件文字列表
            threshold: 信心度門檻

        Returns:
            [(category, confidence), ...]
        """
        return [self.classify(t, threshold) for t in texts]


# ---------------------------------------------------------------------------
# 訓練資料匯出（給 Create ML 使用）
# ---------------------------------------------------------------------------

def export_training_data(
    output_path: str,
    db_host: str = "127.0.0.1",
    db_port: int = 3306,
    db_user: str = "root",
    db_password: str = "",
    db_name: str = "magi_brain",
    limit: int = 5000,
) -> int:
    """
    從 magi_brain.case_documents 匯出已分類的訓練資料。

    輸出 JSON 格式，可直接匯入 Create ML。

    Args:
        output_path: 輸出 JSON 檔案路徑
        db_*: 資料庫連線參數
        limit: 最大筆數

    Returns:
        匯出的筆數
    """
    try:
        import mysql.connector
    except ImportError:
        logger.error("mysql-connector-python not installed")
        return 0

    try:
        conn = mysql.connector.connect(
            host=db_host, port=db_port,
            user=db_user, password=db_password,
            database=db_name,
        )
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT doc_type, title, content_preview
            FROM case_documents
            WHERE doc_type IS NOT NULL AND doc_type != ''
            AND (title IS NOT NULL OR content_preview IS NOT NULL)
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))

        rows = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error("DB export failed: %s", e)
        return 0

    # Format for Create ML
    training_data = []
    for row in rows:
        text = (row.get("title", "") or "") + " " + (row.get("content_preview", "") or "")
        text = text.strip()
        if text and row.get("doc_type") in CATEGORIES:
            training_data.append({
                "text": text[:500],
                "label": row["doc_type"],
            })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(training_data, f, ensure_ascii=False, indent=2)

    logger.info("Exported %d training samples to %s", len(training_data), output_path)
    return len(training_data)


# ---------------------------------------------------------------------------
# Singleton default classifier
# ---------------------------------------------------------------------------

_default_classifier: Optional[DocumentClassifier] = None


def get_classifier() -> DocumentClassifier:
    """取得預設分類器實例。"""
    global _default_classifier
    if _default_classifier is None:
        # 嘗試載入已訓練的模型
        model_path = os.path.join(
            os.path.expanduser("~/Library/Application Support/MAGI"),
            "models", "document_classifier.mlmodel",
        )
        if os.path.exists(model_path):
            _default_classifier = DocumentClassifier(model_path)
        else:
            _default_classifier = DocumentClassifier()  # keyword-only mode
    return _default_classifier


def classify_document(text: str, threshold: float = 0.8) -> tuple[str, float]:
    """便捷函式：使用預設分類器分類文件。"""
    return get_classifier().classify(text, threshold)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    test_texts = [
        "臺灣臺北地方法院刑事判決書 113年度易字第888號",
        "裁定 案號：113年度勞訴字第19號 主文：准許假執行",
        "起訴書 被告：黃語玲 犯罪事實：...",
        "準備程序筆錄 案號：113年度訴字第100號",
        "臺灣臺北地方法院庭通知書",
        "這是一份普通的文件",
    ]

    print(f"Core ML available: {_COREML_AVAILABLE}")
    print(f"Keyword rules: {len(_COMPILED_RULES)}\n")

    classifier = get_classifier()
    for text in test_texts:
        cat, conf = classifier.classify(text)
        print(f"  [{conf:.2f}] {cat:8s} ← {text[:50]}")
