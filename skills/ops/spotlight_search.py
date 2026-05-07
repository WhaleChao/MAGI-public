# -*- coding: utf-8 -*-
"""
spotlight_search.py
===================
macOS Spotlight (mdfind) 全文檢索模組。

透過 macOS 原生 Spotlight 索引進行精確查詢（案號、人名、日期），
完全不消耗 GPU，回應時間 <0.5 秒。

用於分流精確查詢，減少 FAISS embedding + GPU 的負擔。
語意搜尋仍走 FAISS pipeline。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("SpotlightSearch")

# ---------------------------------------------------------------------------
# 案號格式正規表達式
# ---------------------------------------------------------------------------
# 例：113年度勞訴字第19號、112年度訴字第1234號
_RE_CASE_NUMBER = re.compile(
    r"(\d{2,3})\s*年度?\s*([^\s第]{1,6})\s*字?\s*第?\s*(\d{1,6})\s*號?"
)

# 簡化案號：113勞訴19
_RE_CASE_SHORT = re.compile(r"(\d{2,3})([a-zA-Z\u4e00-\u9fff]{1,6})(\d{1,6})")

# 日期格式：2026-04-08 或 115.04.08 (ROC)
_RE_DATE = re.compile(r"\d{2,4}[\.\-/]\d{1,2}[\.\-/]\d{1,2}")

# 人名：2-4 個中文字
_RE_CHINESE_NAME = re.compile(r"^[\u4e00-\u9fff]{2,4}$")


def is_exact_query(query: str) -> bool:
    """判斷查詢是否為精確搜尋（含案號、人名、日期格式）。"""
    q = query.strip()
    if _RE_CASE_NUMBER.search(q):
        return True
    if _RE_CASE_SHORT.search(q):
        return True
    if _RE_DATE.search(q):
        return True
    if _RE_CHINESE_NAME.match(q):
        return True
    return False


def normalize_case_number(case_number: str) -> str:
    """
    將案號展開為 Spotlight 搜尋格式。

    例：113年度勞訴字第19號 → '"113年度勞訴字第19號" OR "113勞訴19"'
    """
    m = _RE_CASE_NUMBER.search(case_number)
    if m:
        year, case_type, num = m.group(1), m.group(2), m.group(3)
        # Strip trailing 字 to avoid duplication (e.g. 勞訴字 → 勞訴)
        case_type_clean = case_type.rstrip("字")
        full = f"{year}年度{case_type_clean}字第{num}號"
        short = f"{year}{case_type_clean}{num}"
        return f'"{full}" OR "{short}"'

    m = _RE_CASE_SHORT.search(case_number)
    if m:
        year, case_type, num = m.group(1), m.group(2), m.group(3)
        case_type_clean = case_type.rstrip("字")
        full = f"{year}年度{case_type_clean}字第{num}號"
        short = f"{year}{case_type_clean}{num}"
        return f'"{full}" OR "{short}"'

    return f'"{case_number}"'


def spotlight_search(
    query: str,
    folder: Optional[str] = None,
    file_type: Optional[str] = None,
    limit: int = 20,
    timeout: int = 10,
) -> list[dict]:
    """
    透過 macOS Spotlight (mdfind) 進行全文檢索。

    Args:
        query: 搜尋關鍵字（支援布林運算：AND, OR, NOT）
        folder: 限定搜尋資料夾（如 /Volumes/homes/...）
        file_type: 限定檔案類型（如 pdf, docx）
        limit: 最大回傳筆數
        timeout: mdfind 最大等待秒數

    Returns:
        [{"path": str, "name": str, "modified": str, "size": int}, ...]
    """
    cmd = ["mdfind"]

    if folder:
        if not os.path.isdir(folder):
            logger.warning("Spotlight: folder does not exist: %s", folder)
            return []
        cmd += ["-onlyin", folder]

    # 建構查詢條件
    conditions = []
    if file_type:
        ext = file_type.lstrip(".")
        conditions.append(f"kMDItemFSName == '*.{ext}'")
        conditions.append(query)
        cmd.append(" && ".join(conditions))
    else:
        cmd.append(query)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            logger.warning("mdfind returned %d: %s", result.returncode, result.stderr.strip())
            return []
    except subprocess.TimeoutExpired:
        logger.warning("mdfind timed out after %ds for query: %s", timeout, query)
        return []
    except FileNotFoundError:
        logger.error("mdfind not found — not running on macOS?")
        return []

    paths = [p for p in result.stdout.strip().split("\n") if p][:limit]
    if not paths:
        return []

    # 取得每個檔案的 metadata
    files = []
    for path in paths:
        try:
            stat = os.stat(path)
            files.append({
                "path": path,
                "name": os.path.basename(path),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "size": stat.st_size,
            })
        except OSError:
            # 檔案可能已被移動/刪除
            files.append({
                "path": path,
                "name": os.path.basename(path),
                "modified": "",
                "size": 0,
            })

    return files


def spotlight_search_case(
    case_number: str,
    case_folder: Optional[str] = None,
    file_type: str = "pdf",
    limit: int = 20,
) -> list[dict]:
    """
    針對案號的專用搜尋，自動展開常見案號格式。

    Args:
        case_number: 案號（完整或簡化格式皆可）
        case_folder: 限定搜尋的案件根資料夾
        file_type: 限定檔案類型（預設 pdf）
        limit: 最大回傳筆數
    """
    normalized = normalize_case_number(case_number)
    return spotlight_search(normalized, folder=case_folder, file_type=file_type, limit=limit)


def spotlight_search_person(
    name: str,
    case_folder: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """
    針對人名的專用搜尋。

    Args:
        name: 當事人或律師姓名
        case_folder: 限定搜尋的案件根資料夾
        limit: 最大回傳筆數
    """
    query = f'"{name}"'
    return spotlight_search(query, folder=case_folder, limit=limit)


def check_spotlight_indexed(folder: str) -> bool:
    """
    檢查指定資料夾是否已被 Spotlight 索引。

    Returns:
        True if indexed, False if not or if check fails.
    """
    try:
        result = subprocess.run(
            ["mdutil", "-s", folder],
            capture_output=True, text=True, timeout=5,
        )
        return "Indexing enabled" in result.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# CLI 入口（測試用）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python spotlight_search.py <query> [folder]")
        sys.exit(1)

    q = sys.argv[1]
    f = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"Is exact query: {is_exact_query(q)}")
    print(f"Searching: {q}" + (f" in {f}" if f else ""))

    results = spotlight_search(q, folder=f)
    for r in results:
        print(f"  {r['name']}  ({r['size']} bytes)  {r['path']}")
    print(f"\nTotal: {len(results)} results")
