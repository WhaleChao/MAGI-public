# -*- coding: utf-8 -*-
"""
skills.engine.ocr — 統一 OCR runtime skeleton.

Feature flags (all env vars):
  MAGI_TESSERACT_ENABLE=1         (binary 存在就可用；預設 1)
  MAGI_OCR_CONSENSUS_ENABLE=0     (Tesseract + Apple Vision 並行 consensus；預設 0)
  MAGI_OCR_CONSENSUS_SHADOW=0     (shadow run，只記錄不切換；預設 0)
  MAGI_OCR_CONSENSUS_TIMEOUT_SEC=60  (整個 consensus 的 wall-clock timeout)
  MAGI_OCR_CACHE_ENABLE=1         (image-hash LRU 磁碟 cache；預設 1，cache.py 下輪實作)

業務紅線：
  - captcha OCR 路徑 (laf_automation_v2 / file_review_automation / judicial_automation_v2)
    絕對不走 legal_corrector，不走 consensus；task_type='captcha' 時必須 bypass
  - pdf-namer 維持自身 multi-OCR consensus，不得被迫接入本 consensus.py
  - OpenClaw 路徑已廢棄，不新增接線

Python 3.9 + 3.14 相容：禁用 str | None / dict[str, Any] / match-case。
"""

from skills.engine.ocr.ocr_schema import (
    OCREntities,
    OCRProviderResult,
    OCRConsensusResult,
)

__all__ = [
    "OCREntities",
    "OCRProviderResult",
    "OCRConsensusResult",
]
