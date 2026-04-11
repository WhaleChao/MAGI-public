"""
Summary export helpers extracted from Orchestrator.

All functions accept `orch` (the Orchestrator instance) instead of `self`.
"""
from __future__ import annotations

import logging
import re as _re
from typing import Optional

logger = logging.getLogger("Orchestrator")


def export_summary_docx_or_txt(
    orch,
    summary_text: str,
    *,
    prefix: str,
    title: str,
    user_id: str,
    source_path: str = "",
) -> Optional[str]:
    """摘要輸出：優先 DOCX 原文／摘要對照表格，fallback TXT。"""
    src_text = ""
    if source_path:
        try:
            extracted = orch._extract_text_from_uploaded_file(source_path)
            if extracted.get("success"):
                src_text = str(extracted.get("text") or "").strip()
        except Exception:
            logger.debug("export_summary: source extraction failed", exc_info=True)
    if src_text and summary_text:
        try:
            from skills.ops.export_docx import export_bilingual_docx
            from api.handlers.document_handler import is_file_protocol_user
            _page_pattern = r"---\s*第\s*\d+\s*頁\s*---"
            if _re.search(_page_pattern, src_text):
                _raw_pages = _re.split(_page_pattern, src_text)
                src_chunks = [p.strip() for p in _raw_pages if p.strip()]
            else:
                _raw = [p.strip() for p in _re.split(r"\n{2,}", src_text) if p.strip()]
                src_chunks = []
                _buf = []
                _buf_len = 0
                for p in _raw:
                    _buf.append(p)
                    _buf_len += len(p)
                    if _buf_len >= 800:
                        src_chunks.append("\n\n".join(_buf))
                        _buf, _buf_len = [], 0
                if _buf:
                    src_chunks.append("\n\n".join(_buf))
            sum_chunks = [p.strip() for p in _re.split(
                r"\n(?=#{1,3}\s|(?:\d+[\.\、])|(?:[-\*]\s))|(?:\n{2,})",
                summary_text.strip(),
            ) if p.strip()]
            while sum_chunks and _re.match(r"^[📄📚🌐\*#\s]+$", sum_chunks[0].strip().replace("*", "")):
                sum_chunks.pop(0)
            max_rows = max(len(src_chunks), len(sum_chunks), 1)
            while len(src_chunks) < max_rows:
                src_chunks.append("")
            while len(sum_chunks) < max_rows:
                sum_chunks.append("")
            pages = [
                {"page": i + 1, "source": s, "target": t}
                for i, (s, t) in enumerate(zip(src_chunks, sum_chunks))
                if s.strip() or t.strip()
            ]
            if pages:
                ex = export_bilingual_docx(
                    pages, title=title, header_text=title,
                    prefix=prefix,
                    col_labels={"col1": "段落", "col2": "原文", "col3": "摘要"},
                )
                if isinstance(ex, dict) and ex.get("success"):
                    path = str(ex.get("path") or "").strip()
                    url = str(ex.get("url") or "").strip()
                    head = "📄 已輸出原文／摘要對照 DOCX 表格檔案。"
                    if url:
                        head = f"{head}\n{url}"
                    if is_file_protocol_user(user_id) and path:
                        return f"{head}|||FILE_PATH|||{path}"
                    return f"{head}\n{path}".strip()
        except Exception:
            logger.debug("export_summary: bilingual docx failed", exc_info=True)
    # Fallback: summary-only DOCX table
    sections = []
    parts = _re.split(r"\n(?=#{1,3}\s|(?:\d+[\.\、]))", summary_text.strip())
    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        heading_match = _re.match(r"^#{1,3}\s*(.+?)$", part, _re.MULTILINE)
        num_match = _re.match(r"^(\d+[\.\、])\s*(.+?)$", part, _re.MULTILINE)
        if heading_match:
            heading = heading_match.group(1).strip()
            body = part[heading_match.end():].strip()
        elif num_match:
            heading = num_match.group(0).split("\n")[0].strip()
            body = "\n".join(part.split("\n")[1:]).strip() or part.strip()
        else:
            heading = f"段落 {i + 1}" if len(parts) > 1 else ""
            body = part
        sections.append({"heading": heading, "summary": body, "excerpt": ""})
    if sections:
        exported = orch._export_plain_docx(
            segments=sections, mode="summary",
            title=title, prefix=prefix, user_id=user_id,
        )
        if exported:
            return exported
    return orch._export_plain_txt(
        content=summary_text, prefix=prefix,
        user_id=user_id, title=f"📄 已輸出{title}摘要 TXT 檔案。",
    )
