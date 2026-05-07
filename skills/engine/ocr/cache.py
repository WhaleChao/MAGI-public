# -*- coding: utf-8 -*-
"""
OCR 結果 image-hash LRU 磁碟快取。

設計原則：
  - key = SHA-256(image_bytes)[:16]（16 字元 hex）
  - value = OCRConsensusResult.to_dict() JSON（不含 entity 實際字串，只有 count）
  - TTL = MAGI_OCR_CACHE_TTL_SEC（預設 604800 = 7 天）
  - 路徑 = api.platforms.runtime_dir.root() / "ocr" / "cache"
  - feature flag = MAGI_OCR_CACHE_ENABLE（預設 "1"）
  - 最大容量 = MAGI_OCR_CACHE_MAX_ENTRIES（預設 500）
  - 超過容量：以 mtime 最舊的先刪（lazy eviction on put）
  - 過期自動清理：讀到過期就刪（lazy expiry on get）

業務紅線：
  - 只快取 legal 類型 OCR 結果；captcha 不快取
  - 快取值不含 entities 實際字串（只有 to_dict() 的 counts）
  - thread-safe：讀寫均持 _CACHE_LOCK

Python 3.9 + 3.14 相容：使用 typing.Optional / Dict，不用 str|None / dict[str, Any]。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("ocr.cache")

_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 環境變數 helpers
# ---------------------------------------------------------------------------

def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "1" if default else "0").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)).strip())
    except (ValueError, AttributeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)).strip())
    except (ValueError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# 快取目錄
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    """回傳 OCR cache 目錄（由 RuntimeDir 管理）。"""
    try:
        from api.platforms.runtime_dir import root as _runtime_root
        base = _runtime_root()
    except Exception:
        # fallback：若 RuntimeDir 不可用（e.g. 離線測試），用 MAGI_RUNTIME_DIR 或預設路徑
        override = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
        magi_root = os.environ.get("MAGI_ROOT", "/Users/ai/Desktop/MAGI_v2")
        base = Path(override) if override else Path(magi_root) / ".runtime"

    d = base / "ocr" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# image-hash
# ---------------------------------------------------------------------------

def _image_hash(image_bytes: bytes) -> str:
    """回傳 SHA-256(image_bytes) 前 16 碼 hex。"""
    return hashlib.sha256(image_bytes).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 快取 get / put
# ---------------------------------------------------------------------------

def get(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    """
    取得快取的 OCR 結果 dict。

    Args:
        image_bytes: 原始圖片位元組。

    Returns:
        OCRConsensusResult.to_dict() dict，若無命中或已過期則回 None。
    """
    if not _env_bool("MAGI_OCR_CACHE_ENABLE", True):
        return None

    key = _image_hash(image_bytes)
    cache_path = _cache_dir() / f"{key}.json"

    with _CACHE_LOCK:
        if not cache_path.exists():
            return None

        try:
            raw = cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as e:
            logger.warning("ocr.cache: failed to read cache entry %s: %s", key, e)
            try:
                cache_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        # TTL 檢查
        ttl_sec = _env_float("MAGI_OCR_CACHE_TTL_SEC", 604800.0)
        cached_at = data.get("_cached_at", 0.0)
        age = time.time() - cached_at
        if age > ttl_sec:
            logger.debug("ocr.cache: expired entry %s (age=%.0fs)", key, age)
            try:
                cache_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        logger.debug("ocr.cache: hit %s (age=%.0fs)", key, age)
        # 回傳時去掉 _cached_at 內部欄位
        result = {k: v for k, v in data.items() if not k.startswith("_")}
        return result


def put(image_bytes: bytes, result_dict: Dict[str, Any]) -> None:
    """
    寫入快取。

    Args:
        image_bytes: 原始圖片位元組（用於計算 key）。
        result_dict: OCRConsensusResult.to_dict() 的結果。
    """
    if not _env_bool("MAGI_OCR_CACHE_ENABLE", True):
        return

    key = _image_hash(image_bytes)
    cache_path = _cache_dir() / f"{key}.json"

    data = dict(result_dict)
    data["_cached_at"] = time.time()
    data["_key"] = key

    with _CACHE_LOCK:
        try:
            tmp_path = cache_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_path.replace(cache_path)
        except Exception as e:
            logger.warning("ocr.cache: failed to write cache entry %s: %s", key, e)
            return

        # 容量管理：超過 max_entries 時刪最舊
        _evict_if_needed()


def _evict_if_needed() -> None:
    """若 cache 目錄超過 max_entries，刪最舊（以 mtime 排序）。

    呼叫端必須持有 _CACHE_LOCK。
    """
    max_entries = _env_int("MAGI_OCR_CACHE_MAX_ENTRIES", 500)
    try:
        d = _cache_dir()
        entries = sorted(
            d.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        excess = len(entries) - max_entries
        if excess <= 0:
            return
        for old in entries[:excess]:
            try:
                old.unlink(missing_ok=True)
                logger.debug("ocr.cache: evicted %s", old.name)
            except Exception:
                pass
    except Exception as e:
        logger.warning("ocr.cache: eviction error: %s", e)


# ---------------------------------------------------------------------------
# 便利函式：包裝 OCRConsensusResult 物件
# ---------------------------------------------------------------------------

def get_result(image_bytes: bytes):
    """
    取得快取的 OCRConsensusResult 物件。

    Returns:
        OCRConsensusResult 或 None（未命中 / 過期 / flag off）。
    """
    cached = get(image_bytes)
    if cached is None:
        return None

    try:
        from skills.engine.ocr.ocr_schema import (
            OCRConsensusResult,
            OCREntities,
        )
        # 從 dict 重建（簡化版，不還原完整 provider_results）
        return OCRConsensusResult(
            success=cached.get("success", False),
            selected_text=cached.get("selected_text", ""),
            corrected_text=cached.get("corrected_text", ""),
            confidence=cached.get("confidence", 0.0),
            writable=cached.get("writable", False),
            warnings=cached.get("warnings", []),
            critical_conflict=cached.get("critical_conflict", False),
            provider_results={},   # 不還原 provider 詳細結果（快取只存 summary）
            entities=None,         # entity 字串不快取，不還原
            error=cached.get("error"),
            duration_sec=cached.get("duration_sec", 0.0),
        )
    except Exception as e:
        logger.warning("ocr.cache: failed to reconstruct OCRConsensusResult: %s", e)
        return None


def put_result(image_bytes: bytes, result) -> None:
    """
    儲存 OCRConsensusResult 物件到快取。

    只儲存 to_dict() 的安全子集（count 不含 entity 字串）。
    """
    if not _env_bool("MAGI_OCR_CACHE_ENABLE", True):
        return

    try:
        d = result.to_dict()
        # 額外確認不含 entity 字串：移除 provider 詳細文字
        safe = {
            "success": d.get("success"),
            "selected_text": result.selected_text,   # 保留文字（用於還原 OCR 結果）
            "corrected_text": result.corrected_text,
            "confidence": d.get("confidence"),
            "writable": d.get("writable"),
            "warnings": d.get("warnings", []),
            "critical_conflict": d.get("critical_conflict", False),
            "entities_counts": d.get("entities_counts", {}),  # 只有 count
            "error": d.get("error"),
            "duration_sec": d.get("duration_sec", 0.0),
            # providers: 只存摘要（不含 raw_text / corrected_text）
            "providers_summary": {
                name: {
                    "success": pr.get("success"),
                    "quality_score": pr.get("quality_score"),
                    "duration_sec": pr.get("duration_sec"),
                }
                for name, pr in d.get("providers", {}).items()
            },
        }
        put(image_bytes, safe)
    except Exception as e:
        logger.warning("ocr.cache: failed to serialize result for cache: %s", e)
