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
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_MAGI_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_EXPORTS_DIR = _MAGI_ROOT / "static" / "exports"
_EXPORTS_DIR = os.environ.get("MAGI_EXPORTS_DIR", str(_DEFAULT_EXPORTS_DIR))

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


def _exports_dir() -> Path:
    return Path(os.environ.get("MAGI_EXPORTS_DIR", str(_DEFAULT_EXPORTS_DIR))).expanduser().resolve(strict=False)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_generated_filename(prefix: str) -> str:
    safe_prefix = "".join(ch if (ch.isalnum() or ch in {"_", "-"}) else "_" for ch in str(prefix or "export"))
    safe_prefix = safe_prefix.strip("._-") or "export"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex[:8]
    return f"{safe_prefix}_{stamp}_{token}.docx"


def _resolve_export_docx_path(filename: str) -> tuple[Path, str]:
    name = str(filename or "").strip()
    if not name:
        raise ValueError("empty filename")
    candidate = Path(name)
    if candidate.is_absolute() or candidate.name != name or "\\" in name or ".." in candidate.parts:
        raise ValueError("filename must be a plain .docx basename")
    if candidate.suffix.lower() != ".docx":
        raise ValueError("filename must end with .docx")
    exports_dir = _exports_dir()
    exports_dir.mkdir(parents=True, exist_ok=True)
    out_path = (exports_dir / candidate.name).resolve(strict=False)
    if not _is_relative_to(out_path, exports_dir):
        raise ValueError("resolved DOCX path escapes exports directory")
    return out_path, candidate.name


def _validate_docx_file(path: Path) -> dict:
    if not path.exists():
        return {"ok": False, "error": "docx file not created"}
    if path.stat().st_size < 512:
        return {"ok": False, "error": f"docx file too small:{path.stat().st_size}"}
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            for required in ("[Content_Types].xml", "word/document.xml"):
                if required not in names:
                    return {"ok": False, "error": f"docx missing zip entry:{required}"}
            bad = zf.testzip()
            if bad:
                return {"ok": False, "error": f"docx corrupt zip entry:{bad}"}
    except zipfile.BadZipFile as exc:
        return {"ok": False, "error": f"docx bad zip:{exc}"}
    try:
        from docx import Document

        doc = Document(str(path))
        text_chars = sum(len(p.text or "") for p in doc.paragraphs)
        table_rows = sum(len(table.rows) for table in doc.tables)
    except Exception as exc:
        return {"ok": False, "error": f"docx readback failed:{exc}"}
    return {"ok": True, "text_chars": text_chars, "table_rows": table_rows}


def export_bilingual_docx(
    pages: List[Dict[str, Any]],
    *,
    title: str = "",
    subtitle: str = "",
    header_text: str = "",
    prefix: str = "translate",
    filename: str = "",
    col_labels: Optional[Dict[str, str]] = None,
    hide_page_column: bool = False,
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

    if not filename:
        filename = _safe_generated_filename(prefix)

    try:
        out_path, filename = _resolve_export_docx_path(filename)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    # Write page data to temp JSON
    data = {
        "mode": "bilingual",
        "title": title or "",
        "subtitle": subtitle or "",
        "header_text": header_text or "",
        "pages": pages,
        "out_path": str(out_path),
        "filename": filename,
        "exports_dir": str(_exports_dir()),
        "hide_page_column": bool(hide_page_column),
    }
    if col_labels:
        data["col_labels"] = col_labels

    return _run_docx_generator(data, str(out_path), filename)


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

    if not filename:
        filename = _safe_generated_filename(prefix)

    try:
        out_path, filename = _resolve_export_docx_path(filename)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    data = {
        "mode": "transcript",
        "title": title or "",
        "case_info": case_info or "",
        "segments": segments,
        "out_path": str(out_path),
        "filename": filename,
        "exports_dir": str(_exports_dir()),
    }

    return _run_docx_generator(data, str(out_path), filename)


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

    if not filename:
        filename = _safe_generated_filename(prefix)

    try:
        out_path, filename = _resolve_export_docx_path(filename)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    data = {
        "mode": "summary",
        "title": title or "",
        "sections": sections,
        "out_path": str(out_path),
        "filename": filename,
        "exports_dir": str(_exports_dir()),
    }

    return _run_docx_generator(data, str(out_path), filename)


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

        validation = _validate_docx_file(Path(out_path))
        if not validation.get("ok"):
            return {"success": False, "error": str(validation.get("error") or "docx validation failed")}

        base = _load_public_base_url()
        url = (base.rstrip("/") + f"/static/exports/{filename}") if base else ""

        return {
            "success": True,
            "path": out_path,
            "filename": filename,
            "url": url,
            "format": "docx",
            "validation": validation,
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
