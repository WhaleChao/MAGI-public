# -*- coding: utf-8 -*-
"""
Apple Vision OCR adapter.

包裝 skills.apple.apple_intelligence.ocr_image(engine='vision')。
functional probe session-cached（5s timeout）。

設計原則：
  - 不可用時回 structured OCRProviderResult(success=False)，禁止 raise
  - probe 必須做真實功能測試（不只看 framework 是否 import）
  - task_type='captcha' 時 bypass legal_corrector

Feature flags:
  MAGI_APPLE_VISION_OCR_ENABLE=1  (預設 1；0 = 完全停用)

Python 3.9 + 3.14 相容。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from typing import Optional, Tuple

from skills.engine.ocr.ocr_schema import OCRProviderResult
from skills.engine.ocr.quality import compute_quality_score
from skills.engine.ocr.legal_entities import extract_entities
from skills.engine.ocr.legal_corrector import correct_legal_text

# --- 環境變數 ---------------------------------------------------------------

def _env_bool(key: str, default: bool = True) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "on", "yes")


# --- 可用性 probe（session-cached）------------------------------------------

_PROBE_LOCK = threading.Lock()
_PROBE_CACHE: Optional[Tuple[bool, str]] = None

# 最小合成 PNG (1x1 白色像素，base64-decoded)
_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def check_available() -> Tuple[bool, str]:
    """session-cached 功能性 probe。

    1. 確認 macOS 平台
    2. 嘗試 import apple_intelligence 與 Vision framework
    3. 5s timeout 功能性 probe：用最小 PNG 呼叫 ocr_image()
    """
    global _PROBE_CACHE
    with _PROBE_LOCK:
        if _PROBE_CACHE is not None:
            return _PROBE_CACHE

        if not _env_bool("MAGI_APPLE_VISION_OCR_ENABLE", default=True):
            _PROBE_CACHE = (False, "MAGI_APPLE_VISION_OCR_ENABLE=0")
            return _PROBE_CACHE

        # macOS 平台檢查
        import platform as _platform
        if _platform.system() != "Darwin":
            _PROBE_CACHE = (False, "Apple Vision only available on macOS")
            return _PROBE_CACHE

        # import probe（Vision framework）
        try:
            import Vision  # type: ignore[import]
        except ImportError:
            try:
                import objc  # type: ignore[import]
            except ImportError:
                _PROBE_CACHE = (False, "pyobjc not installed (pip install pyobjc-framework-Vision)")
                return _PROBE_CACHE
            _PROBE_CACHE = (False, "pyobjc-framework-Vision not installed")
            return _PROBE_CACHE

        # functional probe：用最小 PNG 試跑
        ok, reason = _functional_probe()
        _PROBE_CACHE = (ok, reason)
        return _PROBE_CACHE


def _functional_probe() -> Tuple[bool, str]:
    """最小 1x1 PNG 功能性 probe，5s timeout。"""
    import base64
    import concurrent.futures

    try:
        png_bytes = base64.b64decode(_PROBE_PNG_B64)
    except Exception as e:
        return False, f"probe PNG decode error: {e}"

    def _do_probe() -> Tuple[bool, str]:
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(png_bytes)
                tmp_path = f.name
            result = _call_apple_vision(tmp_path)
            if not isinstance(result, dict):
                return False, "ocr_image returned non-dict"
            # success or text present both count as functional
            if result.get("success") or isinstance(result.get("text"), str):
                return True, ""
            return False, result.get("error", "unknown probe error")
        except Exception as e:
            return False, f"functional probe error: {type(e).__name__}: {e}"
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_do_probe)
        try:
            return future.result(timeout=5.0)
        except concurrent.futures.TimeoutError:
            return False, "Apple Vision functional probe timed out (5s)"
        except Exception as e:
            return False, f"probe executor error: {e}"


def _call_apple_vision(image_path: str) -> dict:
    """呼叫 apple_intelligence.ocr_image(engine='vision')。

    失敗時回傳 dict(success=False, text='', error=...) 而非 raise。
    """
    try:
        from skills.apple.apple_intelligence import ocr_image
        return ocr_image(image_path, engine="vision")
    except Exception as e:
        return {
            "success": False,
            "text": "",
            "error": f"{type(e).__name__}: {e}",
            "engine": "vision",
        }


def reset_probe_cache() -> None:
    """測試用：清除 session-cached probe 結果。"""
    global _PROBE_CACHE
    with _PROBE_LOCK:
        _PROBE_CACHE = None


# --- OCR 執行 ---------------------------------------------------------------

def run(
    image_path: str,
    task_type: str = "legal",
    timeout_sec: float = 30.0,
) -> OCRProviderResult:
    """對 image_path 執行 Apple Vision OCR。

    Args:
        image_path: PNG/JPEG/HEIC 圖片路徑。
        task_type: "captcha" 時 bypass legal_corrector（OCR 文字不修正）。
        timeout_sec: 整體 wall-clock timeout（用 ThreadPoolExecutor 強制）。

    Returns:
        OCRProviderResult，失敗時 success=False + error 說明。
        **禁止 raise**，所有例外封裝在 error 欄位。
    """
    import concurrent.futures

    t0 = time.monotonic()

    if not _env_bool("MAGI_APPLE_VISION_OCR_ENABLE", default=True):
        return OCRProviderResult.failure("apple_vision", "MAGI_APPLE_VISION_OCR_ENABLE=0")

    available, reason = check_available()
    if not available:
        return OCRProviderResult.failure("apple_vision", f"not available: {reason}")

    if not image_path or not os.path.isfile(image_path):
        return OCRProviderResult.failure(
            "apple_vision",
            f"image file not found: {image_path!r}",
        )

    def _do_ocr() -> dict:
        return _call_apple_vision(image_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_do_ocr)
        try:
            raw_result = future.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError:
            return OCRProviderResult.failure(
                "apple_vision",
                f"timeout after {timeout_sec}s",
                timed_out=True,
            )
        except Exception as e:
            return OCRProviderResult.failure(
                "apple_vision",
                f"error: {type(e).__name__}: {e}",
            )

    if not isinstance(raw_result, dict):
        return OCRProviderResult.failure("apple_vision", "unexpected non-dict response")

    if not raw_result.get("success"):
        err = raw_result.get("error") or "Vision OCR returned success=False"
        return OCRProviderResult.failure("apple_vision", err)

    raw_text = str(raw_result.get("text") or "").strip()
    q_score = compute_quality_score(raw_text)

    correction = correct_legal_text(raw_text, task_type=task_type)
    corrected = correction.corrected_text

    entities = extract_entities(corrected) if task_type != "captcha" else None
    duration = time.monotonic() - t0

    return OCRProviderResult(
        success=True,
        provider="apple_vision",
        raw_text=raw_text,
        corrected_text=corrected,
        quality_score=q_score,
        entities=entities,
        duration_sec=duration,
        timed_out=False,
    )
