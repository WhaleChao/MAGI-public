# -*- coding: utf-8 -*-
"""
skills/engine/doc_type_detector.py
====================================
Shared document type detection module.

Consolidates DOC_PATTERNS from pdf-bookmarker with cross-reference to
pdf-namer's DOC_CATEGORIES naming_rules, providing a unified
`detect_doc_type(text, ocr_engines=None)` API.

Used by:
  - skills/pdf-bookmarker/action.py  (Vision fallback when regex fails)
  - skills/pdf-namer/action.py       (shared classification layer)
"""
from __future__ import annotations

import re
import logging
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

# ── Dataclass-compatible namedtuple (Python 3.9 safe) ──────────────────────

class DocTypeResult:
    """Result of detect_doc_type()."""
    __slots__ = ("doc_type", "confidence", "source", "label")

    def __init__(self, doc_type: str, confidence: float, source: str, label: str = ""):
        self.doc_type = doc_type
        self.confidence = confidence
        self.source = source  # "regex" | "vision" | "learn"
        self.label = label or doc_type

    def __repr__(self):
        return (f"DocTypeResult(doc_type={self.doc_type!r}, "
                f"confidence={self.confidence:.2f}, source={self.source!r})")


# ── DOC_PATTERNS (shared, mirrors pdf-bookmarker definitions) ────────────────

_RAW_PATTERNS: List[Tuple[str, str, float]] = [
    # (pattern, label, confidence_base)
    # 筆錄
    (r"審判(?:筆錄|程序筆錄)", "審判筆錄", 0.95),
    (r"準備程序筆錄", "準備程序筆錄", 0.95),
    (r"(?:訊問|讯问)筆錄", "訊問筆錄", 0.90),
    (r"言詞辯論筆錄", "言詞辯論筆錄", 0.90),
    (r"(?:調解|和解)(?:程序)?筆錄", "調解筆錄", 0.90),
    # 裁判
    (r"(?:刑事|民事|家事)?判決(?:書)?", "判決", 0.95),
    (r"(?:刑事|民事|家事)?裁定(?:書)?", "裁定", 0.95),
    # 起訴
    (r"起訴書", "起訴書", 0.95),
    (r"追加起訴書", "追加起訴書", 0.95),
    (r"不起訴處分書", "不起訴處分書", 0.95),
    (r"緩起訴處分書", "緩起訴處分書", 0.95),
    (r"聲請簡易判決處刑書", "聲請簡易判決處刑書", 0.90),
    # 書狀
    (r"(?:刑事|民事|家事)?答辯(?:狀|書|意旨)", "答辯狀", 0.90),
    (r"(?:刑事|民事|家事)?(?:上訴|抗告)(?:狀|書|理由)", "上訴抗告狀", 0.90),
    (r"(?:刑事|民事|家事)?陳報(?:狀|書)", "陳報狀", 0.85),
    (r"(?:刑事|民事|家事)?聲請(?:狀|書)", "聲請狀", 0.85),
    (r"辯護(?:意旨|要旨)(?:狀|書)?", "辯護意旨狀", 0.85),
    # 委任
    (r"(?:選任辯護人)?委任(?:狀|書)", "委任狀", 0.95),
    # 函文
    (r"(?:臺灣|最高|智慧財產).*(?:法院|檢察署|地檢署)\s*函", "法院函", 0.90),
    (r"(?:警察局|分局|派出所|調查[處局站])\s*函", "警察機關函", 0.85),
    # 庭通知
    (r"(?:合議審理|審理)?傳票", "傳票", 0.90),
    (r"(?:開庭通知|庭期通知|通知書)", "庭通知書", 0.90),
    # 鑑定
    (r"鑑定(?:報告|書|意見)", "鑑定報告", 0.85),
    (r"(?:法醫|解剖|相驗).*(?:報告|鑑定|證明)", "法醫報告", 0.85),
    # 卷封
    (r"(?:刑事|民事|家事|少年|消債).*卷宗", "卷宗封面", 0.90),
    # 消債
    (r"(?:更生|清算)(?:方案|計畫)", "更生/清算方案", 0.90),
    (r"(?:調解|和解)(?:方案|條件)", "調解/和解方案", 0.85),
]

_COMPILED: List[Tuple[re.Pattern, str, float]] = [
    (re.compile(p, re.IGNORECASE), label, conf)
    for p, label, conf in _RAW_PATTERNS
]


def _detect_by_regex(text: str) -> Optional[DocTypeResult]:
    """Run regex patterns against text; return first match with confidence."""
    for pattern, label, conf in _COMPILED:
        if pattern.search(text):
            return DocTypeResult(doc_type=label, confidence=conf, source="regex", label=label)
    return None


def _detect_by_vision(text: str) -> Optional[DocTypeResult]:
    """Use Vision/LLM as fallback when regex fails.
    Controlled by MAGI_BOOKMARKER_VISION_FALLBACK env var.
    """
    import os
    if not os.environ.get("MAGI_BOOKMARKER_VISION_FALLBACK", "1").strip() in ("1", "true", "yes"):
        return None
    try:
        from skills.bridge import melchior_client as _mc
        _chat_fn = getattr(_mc, "_chat_omlx", None)
        if not callable(_chat_fn):
            return None
        prompt = (
            "根據以下法院文件OCR文字，判斷文件類型。"
            "只回覆一個詞，如：判決、裁定、起訴書、答辯狀、委任狀、傳票、函文、筆錄、其他。\n\n"
            f"文字：{text[:800]}"
        )
        r = _chat_fn(prompt=prompt, model="", timeout=20)
        if r and isinstance(r, dict):
            label = (r.get("content") or r.get("text") or "").strip().split("\n")[0][:20]
            if label:
                return DocTypeResult(doc_type=label, confidence=0.65, source="vision", label=label)
    except Exception as e:
        logger.debug("[doc_type_detector] vision fallback failed: %s", e)
    return None


def detect_doc_type(text: str, ocr_engines: Optional[List[str]] = None) -> DocTypeResult:
    """Detect document type from OCR/native text.

    Strategy:
      1. Regex patterns (fast, high precision) — confidence ≥ 0.85
      2. Vision/LLM fallback (MAGI_BOOKMARKER_VISION_FALLBACK=1) — confidence 0.65

    Args:
        text: OCR or native text extracted from the PDF.
        ocr_engines: (reserved for future multi-engine consensus)

    Returns:
        DocTypeResult with doc_type, confidence, source.
    """
    if not text or not text.strip():
        return DocTypeResult(doc_type="其他", confidence=0.10, source="default")

    result = _detect_by_regex(text)
    if result and result.confidence >= 0.85:
        logger.debug("[doc_type_detector] regex match: %s (%.2f)", result.doc_type, result.confidence)
        return result

    vision_result = _detect_by_vision(text)
    if vision_result:
        logger.info("[doc_type_detector] vision fallback: %s (%.2f)", vision_result.doc_type, vision_result.confidence)
        return vision_result

    if result:
        return result

    return DocTypeResult(doc_type="其他", confidence=0.10, source="default")
