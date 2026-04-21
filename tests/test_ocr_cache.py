# -*- coding: utf-8 -*-
"""
Tests for skills.engine.ocr.cache.

守則：
- 禁止在 module level import api.server / api.tools_api / daemon（SIGCHLD 守則）
- 使用 tmpdir fixture 隔離 cache 目錄
- 不跑真實 OCR；只測 cache 讀寫、TTL、容量管理
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_env(monkeypatch, tmp_path):
    """每個 test 隔離 cache 目錄、TTL、max_entries。"""
    cache_dir = tmp_path / "ocr_cache"
    cache_dir.mkdir()
    # 讓 runtime_dir.root() → tmp_path，使 cache 目錄為 tmp_path/ocr/cache
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    monkeypatch.setenv("MAGI_OCR_CACHE_ENABLE", "1")
    monkeypatch.setenv("MAGI_OCR_CACHE_TTL_SEC", "3600")   # 預設 1 小時
    monkeypatch.setenv("MAGI_OCR_CACHE_MAX_ENTRIES", "500")
    # 每次 test 都 reload cache module，避免 module-level 狀態殘留
    import importlib
    import skills.engine.ocr.cache as _cache_mod
    importlib.reload(_cache_mod)
    yield _cache_mod


def _make_dummy_result() -> Dict[str, Any]:
    """建立符合 OCRConsensusResult.to_dict() 子集的 dict。"""
    return {
        "success": True,
        "selected_text": "花蓮地方法院判決書",
        "corrected_text": "花蓮地方法院判決書（修正）",
        "confidence": 0.85,
        "writable": True,
        "warnings": [],
        "critical_conflict": False,
        "entities_counts": {
            "case_numbers_found": 1,
            "roc_dates_found": 2,
            "courts_found": 1,
            "parties_found": 3,
            "laf_case_numbers_found": 0,
        },
        "error": None,
        "duration_sec": 1.23,
    }


def _make_image_bytes(seed: int = 1) -> bytes:
    """建立虛擬圖片位元組（不同 seed 產生不同 hash）。"""
    return b"FAKE_IMAGE_BYTES_" + str(seed).encode()


# ---------------------------------------------------------------------------
# test: 基本 put / get
# ---------------------------------------------------------------------------

def test_put_then_get_same_key(_reset_env):
    """put 後以同一 image_bytes get，應取得相同結果。"""
    cache = _reset_env
    img = _make_image_bytes(1)
    result = _make_dummy_result()

    cache.put(img, result)
    got = cache.get(img)

    assert got is not None
    assert got["success"] is True
    assert got["confidence"] == 0.85
    assert got["corrected_text"] == "花蓮地方法院判決書（修正）"
    assert got["entities_counts"]["case_numbers_found"] == 1


def test_get_different_key_returns_none(_reset_env):
    """不同 image_bytes → get 回 None（未命中）。"""
    cache = _reset_env
    img1 = _make_image_bytes(1)
    img2 = _make_image_bytes(2)

    cache.put(img1, _make_dummy_result())
    got = cache.get(img2)

    assert got is None


def test_get_nonexistent_key(_reset_env):
    """未 put 直接 get → 回 None。"""
    cache = _reset_env
    img = _make_image_bytes(99)
    assert cache.get(img) is None


# ---------------------------------------------------------------------------
# test: TTL 過期自動清理
# ---------------------------------------------------------------------------

def test_ttl_expired_returns_none(_reset_env, monkeypatch):
    """TTL 設 1 秒，1.5 秒後 get 應回 None。"""
    cache = _reset_env
    monkeypatch.setenv("MAGI_OCR_CACHE_TTL_SEC", "1")
    import importlib
    importlib.reload(cache)

    img = _make_image_bytes(42)
    cache.put(img, _make_dummy_result())

    # 偽造時間：直接修改快取檔的 _cached_at 到過去
    key = hashlib.sha256(img).hexdigest()[:16]
    from api.platforms.runtime_dir import root as _rt_root
    cache_file = _rt_root() / "ocr" / "cache" / f"{key}.json"

    data = json.loads(cache_file.read_text(encoding="utf-8"))
    data["_cached_at"] = time.time() - 10  # 10 秒前 → 過期
    cache_file.write_text(json.dumps(data), encoding="utf-8")

    got = cache.get(img)
    assert got is None
    # 過期後自動清除
    assert not cache_file.exists()


def test_ttl_not_expired_returns_result(_reset_env):
    """TTL 1 小時，剛 put 後 get 應命中。"""
    cache = _reset_env
    img = _make_image_bytes(7)
    cache.put(img, _make_dummy_result())
    got = cache.get(img)
    assert got is not None


# ---------------------------------------------------------------------------
# test: 容量管理（超過 max_entries → 最舊先刪）
# ---------------------------------------------------------------------------

def test_max_entries_evicts_oldest(_reset_env, monkeypatch):
    """max_entries=3，put 第 4 個時，最舊的應被刪除。"""
    cache = _reset_env
    monkeypatch.setenv("MAGI_OCR_CACHE_MAX_ENTRIES", "3")
    import importlib
    importlib.reload(cache)

    imgs = [_make_image_bytes(i) for i in range(4)]
    for i, img in enumerate(imgs):
        result = _make_dummy_result()
        cache.put(img, result)
        # 確保 mtime 有差異（某些 FS 精度為 1 秒）
        time.sleep(0.01)

    from api.platforms.runtime_dir import root as _rt_root
    cache_dir = _rt_root() / "ocr" / "cache"
    remaining = list(cache_dir.glob("*.json"))
    # 最多 3 個（max_entries）
    assert len(remaining) <= 3

    # imgs[0] 是最舊的，應已被刪除
    oldest_key = hashlib.sha256(imgs[0]).hexdigest()[:16]
    oldest_file = cache_dir / f"{oldest_key}.json"
    assert not oldest_file.exists(), "最舊的 cache entry 應已被 evict"

    # imgs[3] 是最新的，應存在
    newest_key = hashlib.sha256(imgs[3]).hexdigest()[:16]
    newest_file = cache_dir / f"{newest_key}.json"
    assert newest_file.exists(), "最新的 cache entry 應仍存在"


# ---------------------------------------------------------------------------
# test: cache disable flag
# ---------------------------------------------------------------------------

def test_cache_disabled_get_returns_none(_reset_env, monkeypatch):
    """MAGI_OCR_CACHE_ENABLE=0 時，get 永遠回 None。"""
    cache = _reset_env
    monkeypatch.setenv("MAGI_OCR_CACHE_ENABLE", "0")
    import importlib
    importlib.reload(cache)

    img = _make_image_bytes(1)
    # 先寫一筆（即使寫進去也不應讀回）
    cache.put(img, _make_dummy_result())
    got = cache.get(img)
    assert got is None


def test_cache_disabled_put_is_noop(_reset_env, monkeypatch, tmp_path):
    """MAGI_OCR_CACHE_ENABLE=0 時，put 不寫任何檔案。"""
    cache = _reset_env
    monkeypatch.setenv("MAGI_OCR_CACHE_ENABLE", "0")
    import importlib
    importlib.reload(cache)

    img = _make_image_bytes(2)
    cache.put(img, _make_dummy_result())

    from api.platforms.runtime_dir import root as _rt_root
    cache_dir = _rt_root() / "ocr" / "cache"
    files = list(cache_dir.glob("*.json"))
    assert len(files) == 0, "disable 時 put 不應寫入任何檔案"


# ---------------------------------------------------------------------------
# test: runtime_dir 路徑正確
# ---------------------------------------------------------------------------

def test_cache_dir_uses_runtime_dir(_reset_env, tmp_path):
    """cache 目錄應在 MAGI_RUNTIME_DIR / ocr / cache 下。"""
    cache = _reset_env
    img = _make_image_bytes(5)
    cache.put(img, _make_dummy_result())

    expected_dir = tmp_path / "ocr" / "cache"
    assert expected_dir.exists()
    files = list(expected_dir.glob("*.json"))
    assert len(files) == 1


def test_cache_key_is_sha256_16chars(_reset_env, tmp_path):
    """cache key 應為 sha256(image_bytes)[:16] hex。"""
    cache = _reset_env
    img = _make_image_bytes(6)
    expected_key = hashlib.sha256(img).hexdigest()[:16]

    cache.put(img, _make_dummy_result())

    expected_dir = tmp_path / "ocr" / "cache"
    files = list(expected_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].stem == expected_key


# ---------------------------------------------------------------------------
# test: put_result / get_result 包裝（OCRConsensusResult 物件）
# ---------------------------------------------------------------------------

def test_put_result_and_get_result(_reset_env):
    """put_result / get_result 以 OCRConsensusResult 物件為介面。"""
    cache = _reset_env
    from skills.engine.ocr.ocr_schema import OCRConsensusResult

    result = OCRConsensusResult(
        success=True,
        selected_text="法院判決",
        corrected_text="法院判決（修正）",
        confidence=0.90,
        writable=True,
        warnings=[],
        critical_conflict=False,
        provider_results={},
        entities=None,
        error=None,
        duration_sec=2.0,
    )

    img = _make_image_bytes(10)
    cache.put_result(img, result)
    got = cache.get_result(img)

    assert got is not None
    assert got.success is True
    assert got.corrected_text == "法院判決（修正）"
    assert got.confidence == 0.90


def test_get_result_miss_returns_none(_reset_env):
    """未命中時 get_result 回 None。"""
    cache = _reset_env
    img = _make_image_bytes(11)
    assert cache.get_result(img) is None
