# -*- coding: utf-8 -*-
"""
Tesseract OCR provider.

所有 subprocess 呼叫一律走 SafeProcess.run(argv=[...])，不得使用 shell 模式。
功能性 probe 結果 session-cached，避免每次呼叫都啟動 tesseract process。

Feature flags:
  MAGI_TESSERACT_ENABLE=1       (預設 1；0 = 完全停用)
  MAGI_TESSERACT_BIN=tesseract  (binary 路徑，可 override)
  MAGI_TESSERACT_LANGS=chi_tra+eng  (OCR 語言，預設繁中+英)
  MAGI_TESSERACT_PSM=3          (預設 PSM，可被 run() 的 psm 參數覆蓋)

Python 3.9 + 3.14 相容。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
import threading
from typing import Optional, Tuple

from api.platforms.safe_process import run as _safe_run
from skills.engine.ocr.ocr_schema import OCRProviderResult
from skills.engine.ocr.quality import compute_quality_score
from skills.engine.ocr.legal_entities import extract_entities
from skills.engine.ocr.legal_corrector import correct_legal_text

# --- 環境變數讀取 -----------------------------------------------------------

def _env_bool(key: str, default: bool = True) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "on", "yes")


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default).strip() or default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)).strip())
    except (ValueError, AttributeError):
        return default


# --- 可用性 probe（session-cached）------------------------------------------

_PROBE_LOCK = threading.Lock()
_PROBE_CACHE: Optional[Tuple[bool, str]] = None  # (available, reason)

# 最小合成 PNG (1x1 白色像素，base64-decoded)
_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _probe_binary(bin_path: str) -> Tuple[bool, str]:
    """binary 版本 probe（tesseract --version）。"""
    try:
        r = _safe_run([bin_path, "--version"], timeout_sec=5.0)
        if r.returncode != 0:
            return False, f"tesseract --version rc={r.returncode}"
    except Exception as e:
        return False, f"tesseract --version error: {e}"
    return True, ""


def _probe_langs(bin_path: str, required_lang: str = "chi_tra") -> Tuple[bool, str]:
    """語言包 probe（tesseract --list-langs 含 chi_tra）。"""
    try:
        r = _safe_run([bin_path, "--list-langs"], timeout_sec=5.0)
        combined = (r.stdout or "") + (r.stderr or "")
        if required_lang not in combined:
            return False, f"lang {required_lang!r} not found in tesseract --list-langs"
    except Exception as e:
        return False, f"tesseract --list-langs error: {e}"
    return True, ""


def _probe_functional(bin_path: str, langs: str = "chi_tra+eng") -> Tuple[bool, str]:
    """功能性 probe：跑一次真實 OCR 在最小合成 PNG 上。"""
    import base64
    try:
        png_bytes = base64.b64decode(_PROBE_PNG_B64)
    except Exception as e:
        return False, f"probe PNG decode error: {e}"

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png_bytes)
            tmp_path = f.name

        r = _safe_run(
            [bin_path, tmp_path, "stdout", "-l", langs, "--psm", "3"],
            timeout_sec=10.0,
        )
        # 1x1 白 PNG OCR 結果可能是空字串或換行，只要 rc=0 就算通過
        if r.timed_out:
            return False, "functional probe timed out"
        if r.returncode not in (0, 1):   # rc=1 在純空白頁也可能出現
            return False, f"functional probe rc={r.returncode}"
        return True, ""
    except Exception as e:
        return False, f"functional probe error: {e}"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def check_available() -> Tuple[bool, str]:
    """session-cached 功能性 probe。

    回傳 (available: bool, reason: str)。
    reason 在不可用時說明失敗原因。
    """
    global _PROBE_CACHE
    with _PROBE_LOCK:
        if _PROBE_CACHE is not None:
            return _PROBE_CACHE

        if not _env_bool("MAGI_TESSERACT_ENABLE", default=True):
            _PROBE_CACHE = (False, "MAGI_TESSERACT_ENABLE=0")
            return _PROBE_CACHE

        bin_path = _env_str("MAGI_TESSERACT_BIN", "tesseract")

        ok, reason = _probe_binary(bin_path)
        if not ok:
            _PROBE_CACHE = (False, reason)
            return _PROBE_CACHE

        ok, reason = _probe_langs(bin_path)
        if not ok:
            _PROBE_CACHE = (False, reason)
            return _PROBE_CACHE

        ok, reason = _probe_functional(bin_path)
        _PROBE_CACHE = (ok, reason)
        return _PROBE_CACHE


def reset_probe_cache() -> None:
    """測試用：清除 session-cached probe 結果。"""
    global _PROBE_CACHE
    with _PROBE_LOCK:
        _PROBE_CACHE = None


# --- OCR 執行 ---------------------------------------------------------------

# Tesseract PSM 策略（依文件類型選擇）
PSM_AUTO = 3             # 完全自動分頁，無 OSD（預設）
PSM_SINGLE_COLUMN = 4    # 單欄文字
PSM_SINGLE_BLOCK = 6     # 單一均勻文字區塊
PSM_SINGLE_LINE = 7      # 單行文字


def run(
    image_path: str,
    psm: Optional[int] = None,
    langs: Optional[str] = None,
    task_type: str = "legal",
    timeout_sec: float = 30.0,
) -> OCRProviderResult:
    """對 image_path 執行 Tesseract OCR。

    Args:
        image_path: PNG/JPEG/TIFF 圖片路徑。
        psm: Tesseract PSM 策略（預設讀 MAGI_TESSERACT_PSM，再 fallback 3）。
        langs: OCR 語言字串（預設讀 MAGI_TESSERACT_LANGS，再 fallback "chi_tra+eng"）。
        task_type: 傳給 legal_corrector；"captcha" 時 bypass 所有修正。
        timeout_sec: SafeProcess timeout。

    Returns:
        OCRProviderResult，失敗時 success=False + error 說明。
        **禁止 raise**，所有例外封裝在 error 欄位。
    """
    t0 = time.monotonic()

    # feature flag guard
    if not _env_bool("MAGI_TESSERACT_ENABLE", default=True):
        return OCRProviderResult.failure("tesseract", "MAGI_TESSERACT_ENABLE=0")

    # 可用性快取 probe
    available, reason = check_available()
    if not available:
        return OCRProviderResult.failure("tesseract", f"not available: {reason}")

    bin_path = _env_str("MAGI_TESSERACT_BIN", "tesseract")
    effective_langs = langs or _env_str("MAGI_TESSERACT_LANGS", "chi_tra+eng")
    effective_psm = psm if psm is not None else _env_int("MAGI_TESSERACT_PSM", PSM_AUTO)

    # 路徑檢查
    if not image_path or not os.path.isfile(image_path):
        return OCRProviderResult.failure(
            "tesseract",
            f"image file not found: {image_path!r}",
        )

    try:
        r = _safe_run(
            [
                bin_path,
                image_path,
                "stdout",
                "-l", effective_langs,
                "--psm", str(effective_psm),
            ],
            timeout_sec=timeout_sec,
        )
    except PermissionError as e:
        # SafeProcess 白名單被擋
        return OCRProviderResult.failure("tesseract", f"SafeProcess blocked: {e}")
    except Exception as e:
        return OCRProviderResult.failure("tesseract", f"run error: {type(e).__name__}: {e}")

    timed_out = getattr(r, "timed_out", False)
    if timed_out:
        return OCRProviderResult.failure(
            "tesseract",
            f"timeout after {timeout_sec}s",
            timed_out=True,
        )

    raw_text = (r.stdout or "").strip()

    # quality score
    q_score = compute_quality_score(raw_text)

    # deterministic correction（captcha 會自動 bypass）
    correction = correct_legal_text(raw_text, task_type=task_type)
    corrected = correction.corrected_text

    # entity extraction
    entities = extract_entities(corrected) if task_type != "captcha" else None

    duration = time.monotonic() - t0

    return OCRProviderResult(
        success=True,
        provider="tesseract",
        raw_text=raw_text,
        corrected_text=corrected,
        quality_score=q_score,
        entities=entities,
        duration_sec=duration,
        psm=effective_psm,
        timed_out=False,
    )
