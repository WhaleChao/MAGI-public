# -*- coding: utf-8 -*-
"""
multimodal_parser.py — 多模態文件解析器
========================================
靈感來源：RAG-Anything (HKUDS/RAG-Anything)

將法律文件（PDF 掃描件、含表格判決書等）解析為結構化內容：
- 純文字段落
- 表格（HTML + 結構化摘要）
- 圖片/印章/簽名（描述文字）
- 數學公式/金額計算

解析後輸出統一格式，可直接餵入 MAGI 的 vector_pipeline 進行 RAG。

依賴優先級：
1. MinerU (mineru CLI) — 最佳，支援 layout analysis + table extraction
2. PyMuPDF (fitz) + camelot/pdfplumber — 備用，表格提取
3. PyMuPDF only — 最低限度，純文字 + 基礎 OCR
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("MultimodalParser")

_MAGI_ROOT = Path(__file__).resolve().parents[2]

# ── 解析後的內容類型 ──────────────────────────────────────────────

class ContentType(str, Enum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"
    EQUATION = "equation"
    HEADER = "header"
    FOOTER = "footer"


@dataclass
class ParsedBlock:
    """單一解析區塊 — 多模態文件解析的原子單位"""
    type: ContentType
    content: str                     # 文字內容或表格 HTML
    page_idx: int = 0               # 所在頁碼（0-based）
    caption: str = ""               # 表格/圖片標題
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 表格專用
    table_html: str = ""            # 原始 HTML 表格
    table_summary: str = ""         # LLM 生成的表格摘要
    # 圖片專用
    image_path: str = ""            # 擷取的圖片路徑
    image_description: str = ""     # VLM/LLM 生成的圖片描述

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @property
    def char_count(self) -> int:
        return len(self.content or "")


@dataclass
class ParseResult:
    """完整文件解析結果"""
    file_path: str
    parser_used: str                 # "mineru" | "pymupdf_tables" | "pymupdf_basic"
    blocks: List[ParsedBlock] = field(default_factory=list)
    total_pages: int = 0
    parse_time_sec: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def text_blocks(self) -> List[ParsedBlock]:
        return [b for b in self.blocks if b.type == ContentType.TEXT]

    @property
    def table_blocks(self) -> List[ParsedBlock]:
        return [b for b in self.blocks if b.type == ContentType.TABLE]

    @property
    def image_blocks(self) -> List[ParsedBlock]:
        return [b for b in self.blocks if b.type == ContentType.IMAGE]

    @property
    def full_text(self) -> str:
        """組合所有文字區塊為完整文本（含表格摘要）"""
        parts = []
        for b in self.blocks:
            if b.type == ContentType.TEXT:
                parts.append(b.content)
            elif b.type == ContentType.TABLE:
                # 表格用摘要文字 + 原始內容都保留
                if b.table_summary:
                    parts.append(f"[表格] {b.caption}\n{b.table_summary}")
                elif b.content:
                    parts.append(f"[表格] {b.caption}\n{b.content}")
            elif b.type == ContentType.IMAGE and b.image_description:
                parts.append(f"[圖片] {b.image_description}")
        return "\n\n".join(parts)

    @property
    def structured_text(self) -> str:
        """帶結構標記的完整文本，適合 RAG ingestion"""
        parts = []
        for b in self.blocks:
            if b.type == ContentType.TEXT:
                parts.append(b.content)
            elif b.type == ContentType.TABLE:
                header = f"[TABLE p.{b.page_idx + 1}]"
                if b.caption:
                    header += f" {b.caption}"
                body = b.table_summary or b.content or ""
                parts.append(f"{header}\n{body}\n[/TABLE]")
            elif b.type == ContentType.IMAGE and b.image_description:
                parts.append(f"[IMAGE p.{b.page_idx + 1}] {b.image_description} [/IMAGE]")
        return "\n\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "parser_used": self.parser_used,
            "total_pages": self.total_pages,
            "parse_time_sec": round(self.parse_time_sec, 2),
            "block_count": len(self.blocks),
            "text_blocks": len(self.text_blocks),
            "table_blocks": len(self.table_blocks),
            "image_blocks": len(self.image_blocks),
            "errors": self.errors,
        }


# ══════════════════════════════════════════════════════════════════
# Parser Backends
# ══════════════════════════════════════════════════════════════════

def _check_mineru_available() -> bool:
    """Check if MinerU CLI is installed"""
    try:
        r = subprocess.run(["mineru", "--version"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _check_pdfplumber_available() -> bool:
    try:
        import pdfplumber
        return True
    except ImportError:
        return False


# ── Backend 1: MinerU (最佳) ──────────────────────────────────────

def _parse_with_mineru(pdf_path: str, output_dir: str) -> ParseResult:
    """
    使用 MinerU CLI 解析 PDF — 支援 layout analysis、table extraction、OCR。
    MinerU 是 RAG-Anything 的核心解析器。
    """
    result = ParseResult(file_path=pdf_path, parser_used="mineru")
    t0 = time.time()

    try:
        cmd = [
            "mineru", "parse", pdf_path,
            "-o", output_dir,
            "-m", "auto",      # auto-detect: text or OCR
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=300, cwd=str(_MAGI_ROOT),
        )
        if proc.returncode != 0:
            result.errors.append(f"MinerU exit code {proc.returncode}: {proc.stderr[:500]}")
            result.parse_time_sec = time.time() - t0
            return result

        # MinerU outputs a content_list.json in the output directory
        content_file = None
        for root, dirs, files in os.walk(output_dir):
            for f in files:
                if f.endswith("content_list.json"):
                    content_file = os.path.join(root, f)
                    break
            if content_file:
                break

        if not content_file:
            # Try markdown output
            for root, dirs, files in os.walk(output_dir):
                for f in files:
                    if f.endswith(".md"):
                        md_path = os.path.join(root, f)
                        md_text = Path(md_path).read_text(encoding="utf-8", errors="ignore")
                        result.blocks.append(ParsedBlock(
                            type=ContentType.TEXT,
                            content=md_text,
                            page_idx=0,
                        ))
                        break
            result.parse_time_sec = time.time() - t0
            return result

        content_list = json.loads(Path(content_file).read_text(encoding="utf-8"))

        for item in content_list:
            item_type = item.get("type", "text")
            page_idx = item.get("page_idx", 0)

            if item_type == "text":
                text = item.get("text", "").strip()
                if text:
                    result.blocks.append(ParsedBlock(
                        type=ContentType.TEXT,
                        content=text,
                        page_idx=page_idx,
                        metadata={"text_level": item.get("text_level", 0)},
                    ))

            elif item_type == "table":
                table_body = item.get("table_body", "")
                caption_list = item.get("table_caption", [])
                caption = " ".join(caption_list) if isinstance(caption_list, list) else str(caption_list)
                # 將 HTML 表格轉為純文字摘要
                plain = _html_table_to_text(table_body) if table_body else ""
                result.blocks.append(ParsedBlock(
                    type=ContentType.TABLE,
                    content=plain,
                    page_idx=page_idx,
                    caption=caption.strip(),
                    table_html=table_body,
                ))

            elif item_type == "image":
                img_path = item.get("img_path", "")
                caption_list = item.get("image_caption", [])
                caption = " ".join(caption_list) if isinstance(caption_list, list) else str(caption_list)
                result.blocks.append(ParsedBlock(
                    type=ContentType.IMAGE,
                    content=caption.strip(),
                    page_idx=page_idx,
                    caption=caption.strip(),
                    image_path=img_path,
                ))

            elif item_type == "equation":
                eq_text = item.get("text", "").strip()
                if eq_text:
                    result.blocks.append(ParsedBlock(
                        type=ContentType.EQUATION,
                        content=eq_text,
                        page_idx=page_idx,
                    ))

    except subprocess.TimeoutExpired:
        result.errors.append("MinerU timeout (300s)")
    except Exception as e:
        result.errors.append(f"MinerU error: {e}")

    result.parse_time_sec = time.time() - t0
    return result


# ── Backend 2: PyMuPDF + pdfplumber (表格提取) ───────────────────

def _parse_with_pymupdf_tables(pdf_path: str) -> ParseResult:
    """
    PyMuPDF 文字提取 + pdfplumber 表格提取。
    不需要額外 CLI，純 Python。
    """
    import fitz
    result = ParseResult(file_path=pdf_path, parser_used="pymupdf_tables")
    t0 = time.time()

    try:
        import pdfplumber
        has_plumber = True
    except ImportError:
        has_plumber = False

    try:
        doc = fitz.open(pdf_path)
        result.total_pages = len(doc)

        # pdfplumber 用於表格偵測
        plumber_doc = None
        if has_plumber:
            try:
                plumber_doc = pdfplumber.open(pdf_path)
            except Exception:
                pass

        for page_idx in range(len(doc)):
            page = doc[page_idx]
            page_text = page.get_text("text").strip()

            # 嘗試用 pdfplumber 提取表格
            tables_on_page = []
            if plumber_doc and page_idx < len(plumber_doc.pages):
                try:
                    pp = plumber_doc.pages[page_idx]
                    raw_tables = pp.extract_tables()
                    for tbl in (raw_tables or []):
                        if tbl and len(tbl) > 1:  # 至少需要 header + 1 row
                            tables_on_page.append(tbl)
                except Exception:
                    pass

            if tables_on_page:
                # 有表格的頁面：分離表格和文字
                for tbl in tables_on_page:
                    html = _list_table_to_html(tbl)
                    plain = _list_table_to_text(tbl)
                    result.blocks.append(ParsedBlock(
                        type=ContentType.TABLE,
                        content=plain,
                        page_idx=page_idx,
                        table_html=html,
                    ))
                # 表格外的文字
                # 簡單策略：表格文字通常在 page_text 裡重複出現，
                # 但我們仍保留完整頁面文字以免遺漏
                if page_text:
                    result.blocks.append(ParsedBlock(
                        type=ContentType.TEXT,
                        content=page_text,
                        page_idx=page_idx,
                    ))
            elif page_text:
                result.blocks.append(ParsedBlock(
                    type=ContentType.TEXT,
                    content=page_text,
                    page_idx=page_idx,
                ))

            # 圖片提取
            images = page.get_images(full=True)
            for img_info in images:
                # 只記錄有意義的圖片（排除小圖標）
                xref = img_info[0]
                try:
                    base_image = doc.extract_image(xref)
                    if base_image and base_image.get("width", 0) > 100 and base_image.get("height", 0) > 100:
                        result.blocks.append(ParsedBlock(
                            type=ContentType.IMAGE,
                            content=f"Image on page {page_idx + 1} ({base_image.get('width')}x{base_image.get('height')})",
                            page_idx=page_idx,
                            metadata={
                                "width": base_image.get("width"),
                                "height": base_image.get("height"),
                                "ext": base_image.get("ext", ""),
                            },
                        ))
                except Exception:
                    pass

        doc.close()
        if plumber_doc:
            plumber_doc.close()

    except Exception as e:
        result.errors.append(f"PyMuPDF+tables error: {e}")

    result.parse_time_sec = time.time() - t0
    return result


# ── Backend 3: PyMuPDF basic (最低限度) ──────────────────────────

def _parse_with_pymupdf_basic(pdf_path: str) -> ParseResult:
    """純 PyMuPDF 文字提取 — 無表格偵測，但最穩定"""
    import fitz
    result = ParseResult(file_path=pdf_path, parser_used="pymupdf_basic")
    t0 = time.time()

    try:
        doc = fitz.open(pdf_path)
        result.total_pages = len(doc)

        for page_idx in range(len(doc)):
            page = doc[page_idx]
            text = page.get_text("text").strip()
            if text:
                result.blocks.append(ParsedBlock(
                    type=ContentType.TEXT,
                    content=text,
                    page_idx=page_idx,
                ))
        doc.close()
    except Exception as e:
        result.errors.append(f"PyMuPDF basic error: {e}")

    result.parse_time_sec = time.time() - t0
    return result


# ══════════════════════════════════════════════════════════════════
# 表格工具函式
# ══════════════════════════════════════════════════════════════════

def _html_table_to_text(html: str) -> str:
    """將 HTML 表格轉為純文字格式"""
    if not html:
        return ""
    try:
        # 簡單 regex 解析
        html = re.sub(r"<br\s*/?>", "\n", html)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL)
        lines = []
        for row in rows:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if any(cells):
                lines.append(" | ".join(cells))
        return "\n".join(lines)
    except Exception:
        # Fallback: strip all tags
        return re.sub(r"<[^>]+>", " ", html).strip()


def _list_table_to_html(table: list) -> str:
    """將 pdfplumber 的 list-of-lists 表格轉為 HTML"""
    if not table:
        return ""
    rows_html = []
    for i, row in enumerate(table):
        tag = "th" if i == 0 else "td"
        cells = "".join(f"<{tag}>{c or ''}</{tag}>" for c in row)
        rows_html.append(f"<tr>{cells}</tr>")
    return f"<table>{''.join(rows_html)}</table>"


def _list_table_to_text(table: list) -> str:
    """將 pdfplumber 的 list-of-lists 表格轉為純文字"""
    if not table:
        return ""
    lines = []
    for row in table:
        cells = [(str(c) if c else "").strip() for c in row]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# LLM 增強：表格摘要 & 圖片描述
# ══════════════════════════════════════════════════════════════════

def _summarize_table_with_llm(table_text: str, caption: str = "",
                               context: str = "") -> Optional[str]:
    """
    用本地 LLM 生成表格結構化摘要 — 將雜亂的表格數據轉為可 RAG 的文字。
    Best-effort：失敗時返回 None，使用原始文字。
    """
    if not table_text or len(table_text) < 20:
        return None

    prompt = f"""請分析以下表格並用繁體中文摘要其關鍵資訊。
摘要應包含：1) 表格主題 2) 關鍵數據/結論 3) 重要的比較或趨勢。
保持簡潔，不超過 200 字。

{f'表格標題：{caption}' if caption else ''}
{f'上下文：{context[:300]}' if context else ''}

表格內容：
{table_text[:2000]}"""

    try:
        import urllib.request
        omlx_url = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:8080/v1/chat/completions")
        payload = json.dumps({
            "model": os.environ.get("MAGI_OMLX_MODEL", "gemma-4-26b-a4b-it-4bit"),
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(omlx_url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        summary = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        # 降級檢測
        if any(m in summary for m in ("系統降級", "逾時", "請稍後重試", "無法處理")):
            return None
        return summary if len(summary) > 20 else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# 主要公開 API
# ══════════════════════════════════════════════════════════════════

def parse_document(
    file_path: str,
    *,
    prefer_parser: str = "auto",     # "auto" | "mineru" | "pymupdf"
    enable_llm_summary: bool = True, # 是否用 LLM 摘要表格
    output_dir: Optional[str] = None,
    use_markitdown: bool = False,    # 是否優先用 MarkItDown（數位 PDF 直接跳過 MinerU）
) -> ParseResult:
    """
    解析文件為結構化多模態區塊。

    Args:
        file_path: PDF 文件路徑
        prefer_parser: 偏好的解析器
        enable_llm_summary: 是否用 LLM 增強表格摘要
        output_dir: MinerU 輸出目錄（僅 mineru 需要）

    Returns:
        ParseResult 物件，包含所有解析區塊
    """
    file_path = str(Path(file_path).resolve())
    if not os.path.exists(file_path):
        return ParseResult(
            file_path=file_path, parser_used="none",
            errors=[f"File not found: {file_path}"],
        )

    ext = Path(file_path).suffix.lower()
    if ext != ".pdf":
        return ParseResult(
            file_path=file_path, parser_used="none",
            errors=[f"Unsupported format: {ext} (only PDF supported)"],
        )

    result = None

    # MarkItDown opt-in path for digital PDFs (skips MinerU pipeline)
    if use_markitdown or os.environ.get("MAGI_USE_MARKITDOWN", "0").strip() == "1":
        try:
            from skills.engine.document_reader import read_document
            r = read_document(file_path)
            if r.success and r.text:
                block = {"type": "text", "text": r.text, "page": 1}
                return ParseResult(
                    file_path=file_path, parser_used="markitdown",
                    blocks=[block], errors=[],
                )
        except Exception:
            pass  # fall through to MinerU/PyMuPDF

    # 選擇解析器
    if prefer_parser == "mineru" or (prefer_parser == "auto" and _check_mineru_available()):
        out_dir = output_dir or tempfile.mkdtemp(prefix="magi_mineru_")
        result = _parse_with_mineru(file_path, out_dir)
        if result.errors and not result.blocks:
            logger.warning("MinerU failed, falling back to PyMuPDF: %s", result.errors)
            result = None

    if result is None:
        if _check_pdfplumber_available():
            result = _parse_with_pymupdf_tables(file_path)
        else:
            result = _parse_with_pymupdf_basic(file_path)

    # LLM 增強：為表格生成摘要
    if enable_llm_summary and result.table_blocks:
        for block in result.table_blocks:
            if block.content and not block.table_summary:
                # 取前後文字區塊作為上下文
                context = _get_surrounding_context(result.blocks, block)
                summary = _summarize_table_with_llm(
                    block.content, caption=block.caption, context=context,
                )
                if summary:
                    block.table_summary = summary

    logger.info(
        "Parsed %s: %d blocks (%d text, %d tables, %d images) in %.1fs [%s]",
        Path(file_path).name, len(result.blocks),
        len(result.text_blocks), len(result.table_blocks), len(result.image_blocks),
        result.parse_time_sec, result.parser_used,
    )
    return result


def _get_surrounding_context(blocks: List[ParsedBlock], target: ParsedBlock,
                              window: int = 500) -> str:
    """取目標區塊前後的文字內容作為上下文"""
    idx = None
    for i, b in enumerate(blocks):
        if b is target:
            idx = i
            break
    if idx is None:
        return ""

    context_parts = []
    # 向前找
    for i in range(max(0, idx - 3), idx):
        if blocks[i].type == ContentType.TEXT:
            context_parts.append(blocks[i].content[-window:])
    # 向後找
    for i in range(idx + 1, min(len(blocks), idx + 4)):
        if blocks[i].type == ContentType.TEXT:
            context_parts.append(blocks[i].content[:window])
    return " ".join(context_parts)[:window]


# ══════════════════════════════════════════════════════════════════
# Vector Pipeline 整合
# ══════════════════════════════════════════════════════════════════

def ingest_multimodal_to_vectors(
    file_path: str,
    *,
    source_prefix: str = "multimodal",
    case_number: str = "",
    chunk_chars: int = 1200,
    overlap: int = 120,
    max_chunks: int = 240,
    enable_llm_summary: bool = True,
    quiet: bool = False,
) -> Dict[str, Any]:
    """
    端到端：解析 PDF → 結構化區塊 → 向量記憶體。

    比純文字 ingestion 多了：
    - 表格獨立存儲（含 HTML + 摘要）
    - 圖片描述存儲
    - 結構化 source metadata
    """
    from skills.documents.vector_pipeline import (
        _chunk_text, _doc_key, _sha1, _load_index, _save_index, _now_iso,
    )
    from skills.memory.mem_bridge import remember_batch

    result = parse_document(
        file_path,
        enable_llm_summary=enable_llm_summary,
    )

    if result.errors and not result.blocks:
        return {"success": False, "errors": result.errors}

    fname = Path(file_path).stem
    primary = f"{source_prefix}|{file_path}"
    doc_key = _doc_key(source_prefix, primary)

    batch_items = []
    chunks_written = 0

    for block in result.blocks:
        if chunks_written >= max_chunks:
            break

        if block.type == ContentType.TEXT:
            parts = _chunk_text(block.content, chunk_chars=chunk_chars,
                                overlap=overlap, max_chunks=max_chunks - chunks_written)
            for idx, part in enumerate(parts, start=1):
                src = (
                    f"doc={doc_key}|kind={source_prefix}|type=text"
                    f"|page={block.page_idx + 1}|title={fname}"
                    f"{f'|case={case_number}' if case_number else ''}"
                    f"|chunk={idx}/{len(parts)}"
                )
                batch_items.append({"content": part, "source": src})
                chunks_written += 1

        elif block.type == ContentType.TABLE:
            # 表格作為獨立區塊存儲 — 摘要 + 原始文字
            table_content = block.table_summary or block.content
            if table_content and len(table_content) > 20:
                src = (
                    f"doc={doc_key}|kind={source_prefix}|type=table"
                    f"|page={block.page_idx + 1}|title={fname}"
                    f"{f'|case={case_number}' if case_number else ''}"
                    f"|caption={block.caption[:100]}"
                )
                # 表格不做 chunking — 通常單個表格就是一個完整語義單位
                batch_items.append({"content": table_content, "source": src})
                chunks_written += 1

                # 如果有 HTML 且與摘要不同，也存一份結構化版本
                if block.table_html and block.table_summary:
                    plain = block.content
                    if plain and len(plain) > 30 and plain != block.table_summary:
                        src2 = src.replace("|type=table", "|type=table_raw")
                        batch_items.append({"content": plain, "source": src2})
                        chunks_written += 1

        elif block.type == ContentType.IMAGE and block.image_description:
            src = (
                f"doc={doc_key}|kind={source_prefix}|type=image"
                f"|page={block.page_idx + 1}|title={fname}"
                f"{f'|case={case_number}' if case_number else ''}"
            )
            batch_items.append({"content": block.image_description, "source": src})
            chunks_written += 1

    if not batch_items:
        return {"success": False, "error": "No content extracted", "parse_result": result.to_dict()}

    res = remember_batch(batch_items)

    # 更新索引
    index = _load_index()
    index[doc_key] = {
        "doc_key": doc_key,
        "kind": source_prefix,
        "file_path": file_path,
        "title": fname,
        "case_number": case_number,
        "parser_used": result.parser_used,
        "total_pages": result.total_pages,
        "blocks": {
            "text": len(result.text_blocks),
            "table": len(result.table_blocks),
            "image": len(result.image_blocks),
        },
        "chunks_written": chunks_written,
        "updated_at": _now_iso(),
    }
    _save_index(index)

    if not quiet:
        logger.info(
            "Ingested %s: %d chunks (text=%d, table=%d, image=%d) [%s]",
            fname, chunks_written,
            len(result.text_blocks), len(result.table_blocks), len(result.image_blocks),
            result.parser_used,
        )

    return {
        "success": True,
        "doc_key": doc_key,
        "chunks_written": chunks_written,
        "parse_result": result.to_dict(),
        "batch_result": {
            "inserted": res.get("inserted", 0),
            "failed": res.get("failed", 0),
        },
    }
