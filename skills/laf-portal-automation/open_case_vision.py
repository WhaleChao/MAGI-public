# -*- coding: utf-8 -*-
"""
open_case_vision.py
===================
掃描案件資料夾內「02_開辦資料」的 PDF，
用 pdf-namer 的收文章偵測模組抽取法院收文章日期，
供法扶開辦（go_live）表單填寫時使用。

Usage:
    from open_case_vision import extract_open_case_date
    result = extract_open_case_date("/Volumes/SynologyDrive/case_folder")
    # result = {"date": "20251120", "date_str": "2025-11-20", "source_file": "...", "method": "stamp_vision"}
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger("open_case_vision")

# Make sure pdf-namer is importable
_PDF_NAMER_DIR = Path(__file__).resolve().parent.parent / "pdf-namer"
if str(_PDF_NAMER_DIR) not in sys.path:
    sys.path.insert(0, str(_PDF_NAMER_DIR))

OPEN_CASE_SUBFOLDER = "02_開辦資料"
LAF_SUBFOLDER = "01_法扶資料"


def _iter_pdfs(folder: str, max_files: int = 20):
    """Yield PDF paths under folder, newest-modified first."""
    import glob
    pdfs = sorted(
        glob.glob(os.path.join(folder, "**", "*.pdf"), recursive=True)
        + glob.glob(os.path.join(folder, "**", "*.PDF"), recursive=True),
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    seen = set()
    for p in pdfs:
        norm = os.path.normpath(p)
        if norm not in seen:
            seen.add(norm)
            yield norm
        if len(seen) >= max_files:
            break


def extract_open_case_date(case_folder: str, *, verbose: bool = False) -> Dict[str, Any]:
    """
    掃描 case_folder/02_開辦資料（和 01_法扶資料 作為備援）下的 PDF，
    抽取「收文章日期」。

    Returns:
        {
            "date": "20251120",          # YYYYMMDD 或 None
            "date_str": "2025-11-20",    # YYYY-MM-DD 或 None
            "source_file": "...",        # 來源檔名
            "method": "stamp_vision|ocr_text|not_found",
        }
    """
    result: Dict[str, Any] = {"date": None, "date_str": None, "source_file": None, "method": "not_found"}

    case_folder = (case_folder or "").strip()
    if not os.path.isdir(case_folder):
        logger.warning(f"open_case_vision: 案件資料夾不存在: {case_folder}")
        return result

    # Search order: 02_開辦資料 first, then 01_法扶資料
    search_dirs = []
    for sub in [OPEN_CASE_SUBFOLDER, LAF_SUBFOLDER, ""]:
        d = os.path.join(case_folder, sub) if sub else case_folder
        if os.path.isdir(d):
            search_dirs.append(d)

    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.error("open_case_vision: 需要安裝 PyMuPDF (fitz)")
        return result

    try:
        from action import _extract_receipt_date_from_stamp, _find_receipt_date_from_text, _ocr_page_rapid
        HAS_PDF_NAMER = True
    except Exception as e:
        logger.warning(f"open_case_vision: 無法載入 pdf-namer action: {e}")
        HAS_PDF_NAMER = False

    for search_dir in search_dirs:
        for pdf_path in _iter_pdfs(search_dir):
            if verbose:
                logger.info(f"open_case_vision: 掃描 {pdf_path}")
            try:
                doc = fitz.open(pdf_path)
                if doc.needs_pass:
                    try:
                        doc.authenticate("3800")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 102, exc_info=True)

                scan_depth = min(3, doc.page_count)
                for page_idx in range(scan_depth):
                    page = doc[page_idx]

                    # Method 1: stamp vision (preferred)
                    if HAS_PDF_NAMER:
                        try:
                            stamp_date, method_tag = _extract_receipt_date_from_stamp(page)
                            if stamp_date:
                                result["date"] = stamp_date
                                result["date_str"] = f"{stamp_date[:4]}-{stamp_date[4:6]}-{stamp_date[6:]}"
                                result["source_file"] = os.path.basename(pdf_path)
                                result["method"] = f"stamp_vision({method_tag})"
                                logger.info(f"open_case_vision: 找到收文章日期 {stamp_date} ({method_tag}) from {os.path.basename(pdf_path)}")
                                return result
                        except Exception as e:
                            logger.debug(f"open_case_vision: stamp vision error on {pdf_path}: {e}")

                    # Method 2: OCR text scan
                    if HAS_PDF_NAMER:
                        try:
                            text = page.get_text()
                            if len(text.strip()) < 30:
                                text = _ocr_page_rapid(page)
                            from action import _find_receipt_date_from_text as _frt
                            date_str = _frt(text)
                            if date_str:
                                result["date"] = date_str
                                result["date_str"] = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                                result["source_file"] = os.path.basename(pdf_path)
                                result["method"] = "ocr_text"
                                logger.info(f"open_case_vision: OCR 找到收文日期 {date_str} from {os.path.basename(pdf_path)}")
                                return result
                        except Exception as e:
                            logger.debug(f"open_case_vision: ocr text error on {pdf_path}: {e}")
            except Exception as e:
                logger.warning(f"open_case_vision: 無法開啟 {pdf_path}: {e}")
                continue

    logger.info(f"open_case_vision: 未找到收文章日期（已掃描 {case_folder}）")
    return result


def build_go_live_remark(case_folder_or_info, base_remark: str = "") -> str:
    """
    從 02_開辦資料 抽取收文章日期，
    組合成開辦備註欄（含日期資訊）。

    `case_folder_or_info` 可傳入案件資料夾字串，或直接傳 `extract_open_case_date()`
    的結果 dict，方便呼叫端重用已完成的 OCR 結果。
    """
    if isinstance(case_folder_or_info, dict):
        info = dict(case_folder_or_info)
    else:
        info = extract_open_case_date(str(case_folder_or_info or ""))
    parts = []
    if base_remark:
        parts.append(base_remark)
    if info.get("date_str"):
        parts.append(f"[收文章日期: {info['date_str']}]")
        if info.get("source_file"):
            parts.append(f"（來源: {info['source_file']}）")
    return "；".join(parts) if parts else ""


if __name__ == "__main__":
    import argparse
    import json
    p = argparse.ArgumentParser()
    p.add_argument("case_folder", help="案件資料夾路徑")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    res = extract_open_case_date(args.case_folder, verbose=args.verbose)
    print(json.dumps(res, ensure_ascii=False, indent=2))
