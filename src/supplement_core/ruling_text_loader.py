# -*- coding: utf-8 -*-
"""
ruling_text_loader.py — 補正裁定 PDF → OCR 文字載入器

流程：
  1. 檢查 cache（除非 force_refresh=True）
  2. cache miss → PDF → PNG → 每頁 run_consensus → 合併文字
  3. 寫 cache

Cache 策略：
  - cache key = sha1(f"{abs_path}|{mtime}|{file_size}")
  - cache 檔存放在 <MAGI_ROOT>/runtime/supplement_cache/
  - 命中條件：cache 檔存在 + JSON 解析成功 + pdf_mtime/pdf_size 相符

Python 3.9 + 3.14 相容。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .exceptions import SupplementError


# ---------------------------------------------------------------------------
# MAGI_ROOT 解析
# ---------------------------------------------------------------------------

def _magi_root() -> Path:
    """解析 MAGI_ROOT。

    優先順序：
      1. 環境變數 MAGI_ROOT
      2. src/supplement_core → 上溯 3 層 = worktree 根
    """
    env = os.environ.get("MAGI_ROOT", "").strip()
    if env:
        return Path(env)
    # __file__ = .../src/supplement_core/ruling_text_loader.py
    return Path(__file__).parent.parent.parent


def _cache_dir() -> Path:
    """回傳 cache 目錄（自動 mkdir -p）。"""
    d = _magi_root() / "runtime" / "supplement_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def _cache_key(abs_path: str, mtime: float, size: int) -> str:
    """計算 cache key（sha1）。"""
    raw = f"{abs_path}|{mtime}|{size}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cache IO
# ---------------------------------------------------------------------------

def _read_cache(
    cache_path: Path,
    expected_mtime: float,
    expected_size: int,
) -> Optional[dict]:
    """讀取 cache 檔，驗證 mtime/size 後回傳 dict；任何問題回 None。"""
    if not cache_path.exists():
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # 防 stale：確認 mtime 與 size 相符（允許 mtime 誤差 1e-3 秒）
        if abs(data.get("pdf_mtime", -1) - expected_mtime) > 1e-3:
            return None
        if data.get("pdf_size", -1) != expected_size:
            return None
        return data
    except Exception:
        return None


def _write_cache(
    cache_path: Path,
    text: str,
    page_count: int,
    pdf_abs_path: str,
    pdf_mtime: float,
    pdf_size: int,
) -> None:
    """將 OCR 結果寫入 cache 檔（原子寫：先寫 .tmp 再 rename）。"""
    payload = {
        "text": text,
        "page_count": page_count,
        "char_count": len(text),
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "pdf_abs_path": pdf_abs_path,
        "pdf_mtime": pdf_mtime,
        "pdf_size": pdf_size,
    }
    tmp_path = cache_path.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp_path.rename(cache_path)
    except Exception:
        # 寫 cache 失敗不影響主流程
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# OCR 實作
# ---------------------------------------------------------------------------

def _convert_pdf_to_images(pdf_path: str, output_dir: str) -> List[str]:
    """PDF → PNG 清單。

    優先嘗試 skills.pdf.scripts.convert_pdf_to_images.convert，
    fallback 到 pdf2image.convert_from_path。

    Returns:
        已排序的 PNG 絕對路徑清單。

    Raises:
        SupplementError: 轉換失敗。
    """
    try:
        from skills.pdf.scripts.convert_pdf_to_images import convert as _skills_convert
        _skills_convert(pdf_path, output_dir)
        pngs = sorted(
            str(Path(output_dir) / f)
            for f in os.listdir(output_dir)
            if f.lower().endswith(".png")
        )
        if pngs:
            return pngs
        # skills_convert 執行成功但沒有輸出（不應發生）→ fallback
    except Exception:
        pass  # fallback 到 pdf2image

    try:
        from pdf2image import convert_from_path  # type: ignore
        images = convert_from_path(pdf_path, dpi=200)
        pngs = []
        for i, image in enumerate(images):
            png_path = os.path.join(output_dir, f"page_{i + 1}.png")
            image.save(png_path)
            pngs.append(png_path)
        if not pngs:
            raise SupplementError(f"pdf2image returned 0 pages for: {pdf_path!r}")
        return sorted(pngs)
    except SupplementError:
        raise
    except Exception as e:
        raise SupplementError(
            f"PDF 轉圖失敗（{pdf_path!r}）: {type(e).__name__}: {e}"
        ) from e


def _run_ocr_per_page(png_path: str) -> Tuple[str, Optional[float]]:
    """對單頁 PNG 執行 OCR，回傳 (text, confidence)。

    不 raise：任何失敗回傳空字串與 None。
    """
    try:
        from skills.engine.ocr.consensus import run_consensus  # type: ignore
        result = run_consensus(png_path, task_type="legal", timeout_sec=60)
        if result.success:
            text = result.selected_text or result.corrected_text or ""
            confidence: Optional[float] = result.confidence if result.confidence else None
            return text, confidence
        else:
            return "", None
    except ImportError as e:
        raise SupplementError(
            f"無法 import OCR 模組 (skills.engine.ocr.consensus): {e}。"
            "請確認 OCR 依賴已安裝（tesseract、Apple Vision framework）。"
        ) from e
    except Exception:
        return "", None


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def load_text(pdf_path: str, *, force_refresh: bool = False) -> dict:
    """載入 PDF 文字內容（OCR + 檔案 cache）。

    流程：
        1. 檢查 cache（除非 force_refresh=True）
        2. cache miss → PDF → 圖片 → 每頁 run_consensus → 合併文字
        3. 寫 cache

    Args:
        pdf_path: 絕對路徑
        force_refresh: True 則跳過 cache 重 OCR

    Returns: {
        "text": str,                    # 完整 OCR 文字（換頁用「\\n\\n--- page N ---\\n\\n」分隔）
        "source": "cache" | "ocr",
        "page_count": int,
        "char_count": int,
        "cache_path": str,              # cache 檔絕對路徑
        "duration_sec": float,          # 本次 load 耗時
        "page_confidences": list[float] | None,  # 各頁 OCR confidence（cache 命中為 None）
    }

    Raises:
        FileNotFoundError: pdf_path 不存在
        SupplementError: PDF 轉圖失敗或全部頁 OCR 失敗
    """
    t0 = time.monotonic()

    abs_path = os.path.abspath(pdf_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"PDF 不存在：{abs_path!r}")

    # 取得 stat
    stat = os.stat(abs_path)
    mtime: float = stat.st_mtime
    size: int = stat.st_size

    # cache 路徑
    key = _cache_key(abs_path, mtime, size)
    cache_path = _cache_dir() / f"{key}.json"

    # --- 嘗試 cache 命中 ---
    if not force_refresh:
        cached = _read_cache(cache_path, mtime, size)
        if cached is not None:
            duration = time.monotonic() - t0
            return {
                "text": cached["text"],
                "source": "cache",
                "page_count": cached["page_count"],
                "char_count": cached["char_count"],
                "cache_path": str(cache_path),
                "duration_sec": round(duration, 3),
                "page_confidences": None,
            }

    # --- OCR 路徑 ---
    tmp_dir = tempfile.mkdtemp(prefix="magi_ocr_")
    try:
        # 轉圖
        pngs = _convert_pdf_to_images(abs_path, tmp_dir)

        # 逐頁 OCR
        page_texts: List[str] = []
        page_confidences: List[Optional[float]] = []
        failed_pages: List[int] = []

        for i, png in enumerate(pngs):
            page_num = i + 1
            try:
                text, conf = _run_ocr_per_page(png)
                if not text:
                    failed_pages.append(page_num)
                    page_texts.append(f"[OCR FAILED page {page_num}]")
                else:
                    page_texts.append(text)
                page_confidences.append(conf)
            except SupplementError:
                # ImportError → 直接重新拋出（無法繼續）
                raise
            except Exception:
                failed_pages.append(page_num)
                page_texts.append(f"[OCR FAILED page {page_num}]")
                page_confidences.append(None)

        if len(failed_pages) == len(pngs):
            raise SupplementError(
                f"all pages ocr failed for: {abs_path!r}"
            )

        # 合併文字
        parts: List[str] = []
        for i, txt in enumerate(page_texts):
            page_num = i + 1
            if i == 0:
                parts.append(txt)
            else:
                parts.append(f"\n\n--- page {page_num} ---\n\n{txt}")
        full_text = "".join(parts)

        # 過濾掉 None confidence
        clean_confidences: List[float] = [
            c for c in page_confidences if c is not None
        ]

        page_count = len(pngs)

    finally:
        # 清除暫存目錄
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    # 寫 cache
    _write_cache(
        cache_path=cache_path,
        text=full_text,
        page_count=page_count,
        pdf_abs_path=abs_path,
        pdf_mtime=mtime,
        pdf_size=size,
    )

    duration = time.monotonic() - t0
    return {
        "text": full_text,
        "source": "ocr",
        "page_count": page_count,
        "char_count": len(full_text),
        "cache_path": str(cache_path),
        "duration_sec": round(duration, 3),
        "page_confidences": clean_confidences if clean_confidences else None,
    }
