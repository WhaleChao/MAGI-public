# -*- coding: utf-8 -*-
"""
export_docx.py
==============
將翻譯、逐字稿、摘要等結構化內容輸出成 docx 表格，
並儲存至 /static/exports，方便透過 LINE/DC 傳送連結或檔案。

支援三種表格模式：
  1. bilingual  — 雙語對照表（翻譯用）
  2. transcript — 逐字稿表格（發言人｜時間｜內容）
  3. summary    — 摘要表格（段落｜摘要｜原文節錄）

依賴：docx-js (Node.js)，透過 subprocess 呼叫。
"""

from __future__ import annotations
import logging

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

_MAGI_ROOT = Path(__file__).resolve().parents[2]
_EXPORTS_DIR = os.environ.get(
    "MAGI_EXPORTS_DIR",
    str(_MAGI_ROOT / "static" / "exports"),
)

# Reuse public URL logic from export_text
try:
    from skills.ops.export_text import _load_public_base_url
except Exception:
    def _load_public_base_url() -> str:
        return ""


def _find_node() -> str:
    """Find node binary."""
    for p in ["/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node"]:
        if os.path.exists(p):
            return p
    return "node"


def _find_node_path() -> str:
    """Find NODE_PATH for docx module."""
    candidates = [
        str(_MAGI_ROOT / "node_modules"),
        str(Path(__file__).resolve().parent / "node_modules"),
        "/opt/homebrew/lib/node_modules",
        "/usr/local/lib/node_modules",
        "/usr/lib/node_modules",
    ]
    for c in candidates:
        docx_path = os.path.join(c, "docx")
        if os.path.isdir(docx_path):
            return c
    return ""


def export_bilingual_docx(
    pages: List[Dict[str, Any]],
    *,
    title: str = "",
    subtitle: str = "",
    header_text: str = "",
    prefix: str = "translate",
    filename: str = "",
    col_labels: Optional[Dict[str, str]] = None,
) -> dict:
    """
    產生雙語對照 docx 表格。

    pages: [{"page": 1, "source": "English text...", "target": "中文翻譯..."}]
    title: 文件標題
    subtitle: 副標題
    header_text: 頁首文字
    prefix: 檔名前綴
    filename: 指定檔名（不含路徑），若空則自動產生
    col_labels: 自訂表頭 {"col1": "段落", "col2": "原文", "col3": "摘要"}

    Returns: {"success": True, "path": "...", "filename": "...", "url": "..."}
    """
    if not pages:
        return {"success": False, "error": "empty pages"}

    Path(_EXPORTS_DIR).mkdir(parents=True, exist_ok=True)

    if not filename:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        filename = f"{prefix}_{stamp}_{token}.docx"

    out_path = os.path.join(_EXPORTS_DIR, filename)

    # Write page data to temp JSON
    data = {
        "mode": "bilingual",
        "title": title or "",
        "subtitle": subtitle or "",
        "header_text": header_text or "",
        "pages": pages,
        "out_path": out_path,
    }
    if col_labels:
        data["col_labels"] = col_labels

    return _run_docx_generator(data, out_path, filename)


def export_transcript_docx(
    segments: List[Dict[str, Any]],
    *,
    title: str = "",
    case_info: str = "",
    prefix: str = "transcript",
    filename: str = "",
) -> dict:
    """
    產生逐字稿 docx 表格。

    segments: [{"speaker": "法官", "time": "10:30", "content": "..."}]
    title: 文件標題（如「114年度訴字第123號 審理程序筆錄」）
    case_info: 案件資訊（頁首）
    prefix: 檔名前綴
    filename: 指定檔名

    Returns: {"success": True, "path": "...", "filename": "...", "url": "..."}
    """
    if not segments:
        return {"success": False, "error": "empty segments"}

    Path(_EXPORTS_DIR).mkdir(parents=True, exist_ok=True)

    if not filename:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        filename = f"{prefix}_{stamp}_{token}.docx"

    out_path = os.path.join(_EXPORTS_DIR, filename)

    data = {
        "mode": "transcript",
        "title": title or "",
        "case_info": case_info or "",
        "segments": segments,
        "out_path": out_path,
    }

    return _run_docx_generator(data, out_path, filename)


def export_summary_docx(
    sections: List[Dict[str, Any]],
    *,
    title: str = "",
    prefix: str = "summary",
    filename: str = "",
) -> dict:
    """
    產生摘要 docx 表格。

    sections: [{"heading": "第一部分", "summary": "摘要...", "excerpt": "原文節錄..."}]
    title: 文件標題
    prefix: 檔名前綴
    filename: 指定檔名

    Returns: {"success": True, "path": "...", "filename": "...", "url": "..."}
    """
    if not sections:
        return {"success": False, "error": "empty sections"}

    Path(_EXPORTS_DIR).mkdir(parents=True, exist_ok=True)

    if not filename:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        filename = f"{prefix}_{stamp}_{token}.docx"

    out_path = os.path.join(_EXPORTS_DIR, filename)

    data = {
        "mode": "summary",
        "title": title or "",
        "sections": sections,
        "out_path": out_path,
    }

    return _run_docx_generator(data, out_path, filename)


def _run_docx_generator(data: dict, out_path: str, filename: str) -> dict:
    """Execute the Node.js docx generator script."""
    node = _find_node()
    node_path = _find_node_path()

    # Write data to temp file
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(data, tmp, ensure_ascii=False)
        tmp.close()

        script_path = os.path.join(os.path.dirname(__file__), "_docx_table_gen.js")
        env = os.environ.copy()
        if node_path:
            env["NODE_PATH"] = node_path

        cp = subprocess.run(
            [node, script_path, tmp.name],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        if cp.returncode != 0:
            return {
                "success": False,
                "error": f"docx generator failed (rc={cp.returncode}): {(cp.stderr or '')[:300]}",
            }

        if not os.path.exists(out_path):
            return {"success": False, "error": "docx file not created"}

        base = _load_public_base_url()
        url = (base.rstrip("/") + f"/static/exports/{filename}") if base else ""

        return {
            "success": True,
            "path": out_path,
            "filename": filename,
            "url": url,
            "format": "docx",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "docx generator timeout"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 247, exc_info=True)
