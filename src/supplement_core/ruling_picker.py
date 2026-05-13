"""
ruling_picker.py — 消債補件模組 M1：列出法院通知或程序裁定資料夾內的 PDF 檔
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .exceptions import CourtNoticeFolderMissingError


# ── doc_type_guess 規則 ───────────────────────────────────────────────────────

def _guess_doc_type(filename: str) -> str:
    """依檔名關鍵字猜測檔案類型。"""
    if "補正" in filename:
        return "補正裁定"
    if "裁定" in filename:
        return "裁定"
    if "函" in filename:
        return "函"
    return "其他"


# ── 主要 API ──────────────────────────────────────────────────────────────────

def list_court_notices(case_meta: dict) -> list[dict]:
    """列出該案 09_法院通知或程序裁定/ 內所有 PDF（按 mtime 倒序）。

    參數：
        case_meta: parse_case_meta() 回傳的 dict

    回傳每筆：
        {
          "path": 絕對路徑（str）,
          "filename": 純檔名（str）,
          "mtime": float (unix timestamp),
          "mtime_human": "YYYY-MM-DD HH:MM",
          "size_bytes": int,
          "doc_type_guess": str,  # "補正裁定" / "裁定" / "函" / "其他"
        }

    raises:
        CourtNoticeFolderMissingError: 若 09_法院通知或程序裁定 資料夾不存在
    """
    case_dir = case_meta["case_dir"]
    subfolder = case_meta.get("subfolder_court_notice", "")

    if not subfolder:
        raise CourtNoticeFolderMissingError(
            f"case_meta 中找不到 subfolder_court_notice，case_dir={case_dir}"
        )

    notice_dir = os.path.join(case_dir, subfolder)

    if not os.path.isdir(notice_dir):
        raise CourtNoticeFolderMissingError(
            f"法院通知資料夾不存在：{notice_dir}"
        )

    results = []
    for entry in os.scandir(notice_dir):
        if not entry.is_file():
            continue
        name = entry.name
        if not name.lower().endswith(".pdf"):
            continue

        stat = entry.stat()
        mtime = stat.st_mtime
        mtime_human = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

        results.append({
            "path": entry.path,
            "filename": name,
            "mtime": mtime,
            "mtime_human": mtime_human,
            "size_bytes": stat.st_size,
            "doc_type_guess": _guess_doc_type(name),
        })

    # 按 mtime 倒序（最新在前）
    results.sort(key=lambda x: x["mtime"], reverse=True)

    return results
