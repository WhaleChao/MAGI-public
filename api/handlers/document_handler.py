"""
Document processing handler — extracted from orchestrator.py.

Pure functions for text normalization, extraction, ingestion,
export, and processing time estimation.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

logger = logging.getLogger("DocumentHandler")


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

def normalize_txt_body(text: str) -> str:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in s.split("\n")]
    out = []
    prev_blank = False
    for ln in lines:
        if not ln.strip():
            if not prev_blank:
                out.append("")
            prev_blank = True
            continue
        out.append(ln.strip())
        prev_blank = False
    return "\n".join(out).strip()


def prepare_document_text_for_llm(text: str) -> str:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not s:
        return ""

    page_marker_re = re.compile(r"^--- 第\s*\d+\s*頁(?:\s*\(OCR\))? ---$")
    french_line_re = re.compile(
        r"[éèàùâêîôûçœ]|"
        r"\b(?:cour|arr[eê]t|avis|ordonnances|préliminaires|recueil|émirats|formes|"
        r"internationale|discrimination|royaume|république|application de la convention)\b",
        re.IGNORECASE,
    )
    english_line_re = re.compile(
        r"\b(?:court|judgment|application|preliminary|reports|orders|convention|"
        r"objections|committee|jurisdiction|international|justice)\b",
        re.IGNORECASE,
    )
    english_anchor_re = re.compile(
        r"\b(?:INTERNATIONAL|COURT|JUSTICE|REPORTS|JUDGMENT|APPLICATION|PART|"
        r"WRITTEN\s+PROCEEDINGS|LETTER|NOTE|MEMORIAL|BRITISH|UNITED\s+KINGDOM|"
        r"PRELIMINARY|OBJECTIONS|ORDERS|DOCUMENTS)\b",
        re.IGNORECASE,
    )

    def _looks_like_heading(line: str) -> bool:
        t = str(line or "").strip()
        if not t:
            return False
        if page_marker_re.fullmatch(t):
            return True
        if re.match(r"^(?:[IVXLC]+\.)\s+", t):
            return True
        if re.match(r"^(?:[A-Z]\.|[0-9]+\.)\s+", t):
            return True
        if len(t) <= 72:
            letters = re.findall(r"[A-Za-z]", t)
            if letters:
                uppercase_ratio = sum(1 for ch in letters if ch.isupper()) / max(1, len(letters))
                if uppercase_ratio >= 0.70:
                    return True
            if re.search(r"[\u4e00-\u9fff]", t) and len(t) <= 28:
                return True
        return False

    def _clean_page(page_lines: list[str]) -> list[str]:
        cleaned = []
        prev_norm = ""
        for raw in page_lines:
            line = str(raw or "").replace("\u00ad", "").replace("\u2011", "-").replace("\u2010", "-")
            line = line.replace("\u0008", " ").strip()
            if not line:
                if cleaned and cleaned[-1]:
                    cleaned.append("")
                continue
            if re.search(r"\b\w+\.indb\b", line, re.IGNORECASE):
                continue
            if re.fullmatch(r"[\d\W_]{1,8}", line):
                continue
            if re.match(r"^(?:ISSN|ISBN)\b", line, re.IGNORECASE):
                continue
            if re.match(r"^(?:Sales number|No de vente)\b", line, re.IGNORECASE):
                continue
            if french_line_re.search(line) and english_line_re.search(line):
                anchor = english_anchor_re.search(line)
                if anchor and anchor.start() >= 8:
                    line = line[anchor.start() :].strip(" -:;")
            norm = re.sub(r"\s+", " ", line).strip().lower()
            if norm and norm == prev_norm:
                continue
            prev_norm = norm
            cleaned.append(line)

        short_lines = [ln for ln in cleaned if ln and len(ln) <= 180]
        english_lines = sum(1 for ln in short_lines if english_line_re.search(ln))
        french_lines = sum(1 for ln in short_lines if french_line_re.search(ln))
        if english_lines >= 2 and french_lines >= 2 and len(short_lines) <= 32:
            cleaned = [ln for ln in cleaned if (not ln) or (not french_line_re.search(ln))]

        out = []
        buf = ""

        def _flush():
            nonlocal buf
            if buf.strip():
                out.append(re.sub(r"\s+", " ", buf).strip())
            buf = ""

        def _should_join(prev_line: str, next_line: str) -> bool:
            if not prev_line or not next_line:
                return False
            if _looks_like_heading(prev_line) or _looks_like_heading(next_line):
                return False
            if re.search(r"[。！？!?;；:：]$", prev_line):
                return False
            if prev_line.endswith(("-", "—", "–", "/", "(")):
                return True
            if len(prev_line) >= 42:
                return True
            if len(prev_line) >= 24 and next_line and next_line[:1].islower():
                return True
            return False

        for line in cleaned:
            if not line:
                _flush()
                if out and out[-1] != "":
                    out.append("")
                continue
            line = re.sub(r"\s+", " ", line).strip()
            if not buf:
                buf = line
                continue
            if _should_join(buf, line):
                sep = "" if buf.endswith(("-", "—", "–", "/")) else " "
                buf = (buf + sep + line).strip()
            else:
                _flush()
                buf = line
        _flush()

        while out and out[-1] == "":
            out.pop()
        return out

    pages = []
    current_page = []
    for raw in s.split("\n"):
        line = str(raw or "").rstrip()
        if page_marker_re.fullmatch(line.strip()):
            if current_page:
                pages.extend(_clean_page(current_page))
                current_page = []
            pages.append(line.strip())
            continue
        current_page.append(line)
    if current_page:
        pages.extend(_clean_page(current_page))

    normalized = []
    prev_blank = False
    for line in pages:
        if not str(line or "").strip():
            if not prev_blank:
                normalized.append("")
            prev_blank = True
            continue
        normalized.append(str(line).strip())
        prev_blank = False
    return "\n".join(normalized).strip()


def polish_translated_document_text(text: str) -> str:
    s = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not s:
        return ""
    try:
        from opencc import OpenCC
        s = OpenCC("s2twp").convert(s)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 196, exc_info=True)
    s = re.sub(r"^\s*[\w-]+\.indb.*$", "", s, flags=re.IGNORECASE | re.MULTILINE)
    s = re.sub(r"^\s*(?:ISSN|ISBN)\b.*$", "", s, flags=re.IGNORECASE | re.MULTILINE)
    s = re.sub(r"^\s*(?:Sales number|No de vente|銷售數量|銷售數字)\b.*$", "", s, flags=re.IGNORECASE | re.MULTILINE)
    s = re.sub(r"(?m)^(?<!-)\s*(\d{1,3})\s*$", "", s)
    s = s.replace("初步反對意見", "初步異議")
    s = s.replace("初步例外情況", "初步異議")
    s = s.replace("執行條款", "主文")
    s = s.replace("程式年表", "程序年表")
    s = s.replace("綜合名單", "案件總表")
    s = s.replace("官方引用：", "官方引用格式：")
    s = s.replace("官方引用方式：", "官方引用格式：")
    s = s.replace("登記官", "書記官長")
    s = s.replace("口頭辯論、記錄", "口頭辯論與文件")
    s = s.replace("書記官處", "書記官長辦公室")
    s = re.sub(r"(?m)^應用(?=\s*國際公約)", "適用", s)
    s = s.replace("官方報價：", "官方引用格式：")
    s = re.sub(r"(?m)^目錄 段落 ", "目錄\n", s)
    s = re.sub(r"(?m)^([A-D])\.(?=[^\n])", r"\1. ", s)
    lines = []
    prev_norm = ""
    for raw in s.split("\n"):
        line = str(raw or "").strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            prev_norm = ""
            continue
        norm = re.sub(r"\s+", " ", line).lower()
        if norm == prev_norm:
            continue
        lines.append(re.sub(r"\s+", " ", line))
        prev_norm = norm
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).strip()


def _split_bilingual_blocks(text: str, max_chars: int = 700) -> list[str]:
    s = normalize_txt_body(text or "")
    if not s:
        return []

    page_marker_re = re.compile(r"^---\s*第\s*\d+\s*頁(?:\s*\(OCR\))?\s*---$")
    raw_parts = [part.strip() for part in re.split(r"\n{2,}", s) if part.strip()]
    blocks: list[str] = []
    pending_label = ""

    def _push_block(value: str) -> None:
        body = re.sub(r"\s+", " ", str(value or "")).strip()
        if not body:
            return
        if pending_label:
            body = f"{pending_label}\n{body}".strip()
        if len(body) <= max_chars:
            blocks.append(body)
            return
        sentences = [
            re.sub(r"\s+", " ", piece).strip()
            for piece in re.split(r"(?<=[。！？!?；;])\s+|(?<=\.)\s+", body)
            if re.sub(r"\s+", " ", piece).strip()
        ]
        if len(sentences) <= 1:
            for i in range(0, len(body), max_chars):
                blocks.append(body[i : i + max_chars].strip())
            return
        buf = ""
        for sentence in sentences:
            candidate = f"{buf} {sentence}".strip() if buf else sentence
            if buf and len(candidate) > max_chars:
                blocks.append(buf.strip())
                buf = sentence
            else:
                buf = candidate
        if buf.strip():
            blocks.append(buf.strip())

    for part in raw_parts:
        if page_marker_re.fullmatch(part):
            pending_label = part
            continue
        _push_block(part)
        pending_label = ""

    if pending_label:
        blocks.append(pending_label)
    return [block for block in blocks if block.strip()]


def build_bilingual_translation_table(
    source_chunks: list[str],
    translated_chunks: list[str],
    *,
    left_header: str = "原文",
    right_header: str = "中文",
    max_rows: int = 400,
) -> str:
    pairs: list[tuple[str, str]] = []
    for src_chunk, tgt_chunk in zip(source_chunks or [], translated_chunks or []):
        src_blocks = _split_bilingual_blocks(src_chunk)
        tgt_blocks = _split_bilingual_blocks(tgt_chunk)
        if src_blocks and tgt_blocks and len(src_blocks) == len(tgt_blocks) and len(src_blocks) <= 12:
            pairs.extend(zip(src_blocks, tgt_blocks))
            continue
        src = normalize_txt_body(src_chunk or "")
        tgt = polish_translated_document_text(tgt_chunk or "")
        if src or tgt:
            pairs.append((src, tgt))

    def _cell(text: str) -> str:
        value = normalize_txt_body(text or "")
        value = value.replace("|", "\\|")
        value = value.replace("\n", "<br>")
        return value or " "

    lines = ["【中英對照表】", "", f"| {left_header} | {right_header} |", "| --- | --- |"]
    for src, tgt in pairs[:max_rows]:
        lines.append(f"| {_cell(src)} | {_cell(tgt)} |")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def build_translation_txt(translated_text: str, source: str, provider: str, mode: str) -> str:
    body = normalize_txt_body(translated_text)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    src = (source or "文字內容").strip()
    pvd = (provider or "unknown").strip()
    md = (mode or "full").strip()
    parts = [
        "MAGI Translation Output",
        "=======================",
        f"Generated At: {stamp}",
        f"Source: {src}",
        f"Mode: {md}",
        f"Provider: {pvd}",
        "",
        "[Translated Text]",
        body,
        "",
    ]
    return "\n".join(parts)


def is_file_protocol_user(user_id: str) -> bool:
    uid = str(user_id or "").strip()
    return (
        uid.startswith("discord_")
        or uid.startswith("telegram_")
        or (uid.startswith("U") and len(uid) >= 20)
    )


def export_translation_txt(
    *,
    translated_text: str,
    source: str,
    provider: str,
    mode: str,
    prefix: str,
    user_id: str,
) -> Optional[str]:
    try:
        from skills.ops.export_text import export_txt
    except Exception as e:
        logger.warning("export_txt unavailable for translation: %s", e)
        return None

    content = build_translation_txt(
        translated_text=translated_text,
        source=source,
        provider=provider,
        mode=mode,
    )
    try:
        ex = export_txt(content, prefix=(prefix or "translation").strip() or "translation")
    except Exception as e:
        logger.warning("translation export_txt failed: %s", e)
        return None
    if not isinstance(ex, dict) or not ex.get("success"):
        return None

    url = str(ex.get("url") or "").strip()
    path = str(ex.get("path") or "").strip()
    head = "📄 已輸出排版良好的翻譯 TXT 檔案。"
    if url:
        head = f"{head}\n{url}"
    if is_file_protocol_user(user_id) and path:
        return f"{head}|||FILE_PATH|||{path}"
    return f"{head}\n{path}".strip()


def export_translation_docx(
    *,
    source_text: str,
    translated_text: str,
    source_chunks: Optional[list] = None,
    translated_chunks: Optional[list] = None,
    title: str = "",
    subtitle: str = "",
    prefix: str = "translate",
    user_id: str,
) -> Optional[str]:
    """
    將翻譯結果輸出為雙語對照 docx 表格，支援 LINE/DC/TG 檔案傳送。
    優先使用 chunk 級別的 source/target 配對（翻譯流程已對齊），
    fallback 才用段落分割。
    """
    try:
        from skills.ops.export_docx import export_bilingual_docx
    except Exception as e:
        logger.warning("export_docx unavailable for translation: %s", e)
        return None

    import re as _re

    # Prefer chunk-level pairs from translation handler (already aligned)
    _src_chunks = source_chunks or []
    _tgt_chunks = translated_chunks or []
    if _src_chunks and len(_src_chunks) == len(_tgt_chunks):
        pages = [
            {"page": i + 1, "source": str(s).strip(), "target": str(t).strip()}
            for i, (s, t) in enumerate(zip(_src_chunks, _tgt_chunks))
            if str(s).strip() or str(t).strip()
        ]
    else:
        # Fallback: split by double newlines and pair
        src_paras = [p.strip() for p in _re.split(r"\n{2,}", (source_text or "").strip()) if p.strip()]
        tgt_paras = [p.strip() for p in _re.split(r"\n{2,}", (translated_text or "").strip()) if p.strip()]

        max_len = max(len(src_paras), len(tgt_paras), 1)
        while len(src_paras) < max_len:
            src_paras.append("")
        while len(tgt_paras) < max_len:
            tgt_paras.append("")

        pages = [
            {"page": i + 1, "source": s, "target": t}
            for i, (s, t) in enumerate(zip(src_paras, tgt_paras))
        ]

    try:
        ex = export_bilingual_docx(
            pages,
            title=title or "",
            subtitle=subtitle or "",
            header_text=title or "",
            prefix=(prefix or "translate").strip() or "translate",
        )
    except Exception as e:
        logger.warning("export_bilingual_docx failed: %s", e)
        return None

    if not isinstance(ex, dict) or not ex.get("success"):
        return None

    path = str(ex.get("path") or "").strip()
    url = str(ex.get("url") or "").strip()
    head = "📄 已輸出雙語對照 DOCX 表格檔案。"
    if url:
        head = f"{head}\n{url}"
    if is_file_protocol_user(user_id) and path:
        return f"{head}|||FILE_PATH|||{path}"
    return f"{head}\n{path}".strip()


def export_plain_docx(
    *,
    segments: list,
    mode: str = "transcript",
    title: str = "",
    case_info: str = "",
    prefix: str = "export",
    user_id: str,
) -> Optional[str]:
    """
    將逐字稿/摘要輸出為 docx 表格，支援 LINE/DC/TG 檔案傳送。
    mode: "transcript" or "summary"
    """
    try:
        if mode == "summary":
            from skills.ops.export_docx import export_summary_docx
            ex = export_summary_docx(segments, title=title, prefix=prefix)
        else:
            from skills.ops.export_docx import export_transcript_docx
            ex = export_transcript_docx(
                segments, title=title, case_info=case_info, prefix=prefix,
            )
    except Exception as e:
        logger.warning("export_docx (%s) failed: %s", mode, e)
        return None

    if not isinstance(ex, dict) or not ex.get("success"):
        return None

    path = str(ex.get("path") or "").strip()
    url = str(ex.get("url") or "").strip()
    head = f"📄 已輸出{title or 'DOCX'} 表格檔案。"
    if url:
        head = f"{head}\n{url}"
    if is_file_protocol_user(user_id) and path:
        return f"{head}|||FILE_PATH|||{path}"
    return f"{head}\n{path}".strip()


def export_plain_txt(
    *,
    content: str,
    prefix: str,
    user_id: str,
    title: str = "📄 已輸出 TXT 檔案。",
) -> Optional[str]:
    try:
        from skills.ops.export_text import export_txt
    except Exception as e:
        logger.warning("export_txt unavailable: %s", e)
        return None
    body = normalize_txt_body(content)
    if not body:
        return None
    try:
        ex = export_txt(body, prefix=(prefix or "export").strip() or "export")
    except Exception as e:
        logger.warning("export_txt failed: %s", e)
        return None
    if not isinstance(ex, dict) or not ex.get("success"):
        return None
    path = str(ex.get("path") or "").strip()
    url = str(ex.get("url") or "").strip()
    head = str(title or "📄 已輸出 TXT 檔案。").strip()
    if url:
        head = f"{head}\n{url}"
    if is_file_protocol_user(user_id) and path:
        return f"{head}|||FILE_PATH|||{path}"
    return f"{head}\n{path}".strip()


# ---------------------------------------------------------------------------
# File extraction
# ---------------------------------------------------------------------------

def extract_text_from_uploaded_file(path: str, filename: str = "") -> dict:
    p = str(path or "").strip()
    name = str(filename or os.path.basename(p) or "").strip()
    ext = os.path.splitext(name.lower())[1]
    if not p or not os.path.exists(p):
        return {"success": False, "text": "", "kind": "", "title": name or "file", "error": f"file not found: {p}"}

    try:
        if ext == ".pdf":
            from skills.documents.pdf_bridge import extract_text

            max_pages = int(os.environ.get("MAGI_FILE_TRANSLATE_MAX_PAGES", "0") or "0")
            if max_pages <= 0:
                max_pages = int(os.environ.get("MAGI_FILE_TRANSLATE_MAX_PAGES_HARD", "1000000") or "1000000")
            txt = extract_text(p, max_pages=max_pages)
            if not txt or str(txt).startswith("[PDF 提取失敗"):
                return {"success": False, "text": "", "kind": "pdf", "title": name, "error": str(txt or "pdf_extract_failed")}
            return {"success": True, "text": str(txt), "kind": "pdf", "title": name, "error": ""}

        if ext == ".epub":
            from skills.documents.epub_bridge import extract_chapters, get_epub_info

            chapters = extract_chapters(p)
            if not chapters:
                return {"success": False, "text": "", "kind": "epub", "title": name, "error": "epub_extract_failed"}
            info = get_epub_info(p) or {}
            parts = []
            btitle = str(info.get("title") or name or "EPUB").strip()
            author = str(info.get("author") or "").strip()
            parts.append(f"書名: {btitle}")
            if author:
                parts.append(f"作者: {author}")
            parts.append("")
            max_chars = int(os.environ.get("MAGI_FILE_TRANSLATE_MAX_CHARS", "0") or "0")
            for i, ch in enumerate(chapters, 1):
                ctitle = str(ch.get("title") or f"Chapter {i}").strip()
                cbody = str(ch.get("content") or "").strip()
                if not cbody:
                    continue
                parts.append(f"## {ctitle}")
                parts.append(cbody)
                parts.append("")
                if max_chars > 0 and sum(len(x) for x in parts) > max_chars:
                    parts.append("（內容過長，已截斷）")
                    break
            txt = "\n".join(parts).strip()
            return {"success": bool(txt), "text": txt, "kind": "epub", "title": btitle or name, "error": "" if txt else "epub_empty"}

        from skills.documents.file_bridge import extract_text_from_file

        max_bytes = int(os.environ.get("MAGI_FILE_TRANSLATE_MAX_BYTES", "0") or "0")
        max_json_chars = int(os.environ.get("MAGI_FILE_TRANSLATE_MAX_JSON_CHARS", "0") or "0")
        max_docx_chars = int(os.environ.get("MAGI_FILE_TRANSLATE_MAX_DOCX_CHARS", "0") or "0")
        info = extract_text_from_file(
            p,
            filename=name,
            max_bytes=None if max_bytes <= 0 else max_bytes,
            max_json_chars=None if max_json_chars <= 0 else max_json_chars,
            max_docx_chars=None if max_docx_chars <= 0 else max_docx_chars,
        )
        txt = str((info or {}).get("text") or "")
        kind = str((info or {}).get("type") or ext.lstrip(".") or "file")
        if not info.get("success"):
            return {"success": False, "text": "", "kind": kind, "title": name, "error": str(info.get("error") or "extract_failed")}
        return {"success": True, "text": txt, "kind": kind, "title": name, "error": ""}
    except Exception as e:
        return {"success": False, "text": "", "kind": ext.lstrip(".") or "file", "title": name, "error": str(e)}


# ---------------------------------------------------------------------------
# Vector memory ingestion
# ---------------------------------------------------------------------------

def ingest_uploaded_text(*, kind: str, primary: str, title: str, text: str) -> dict:
    try:
        from skills.documents.vector_pipeline import ingest_text_to_vector_memory
    except Exception as e:
        return {"success": False, "error": str(e)}
    try:
        chunk_chars = int(os.environ.get("MAGI_FILE_VECTOR_CHUNK_CHARS", "1200") or "1200")
        overlap = int(os.environ.get("MAGI_FILE_VECTOR_OVERLAP", "120") or "120")
        hard_max = int(os.environ.get("MAGI_FILE_VECTOR_MAX_CHUNKS_HARD", "12000") or "12000")
        auto_max = max(20, (len(text or "") // max(1, chunk_chars)) + 10)
        max_chunks = min(hard_max, int(os.environ.get("MAGI_FILE_VECTOR_MAX_CHUNKS", str(auto_max)) or str(auto_max)))
        r = ingest_text_to_vector_memory(
            kind=kind, primary=primary, title=title,
            text=text, chunk_chars=chunk_chars, overlap=overlap,
            max_chunks_total=max_chunks,
        )
        return r if isinstance(r, dict) else {"success": False, "error": "unexpected_result"}
    except Exception as e:
        logger.warning("ingest_uploaded_text failed: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------

def cap_translation_source_text(text: str) -> tuple[str, bool]:
    max_chars = int(os.environ.get("MAGI_FILE_TRANSLATE_MAX_CHARS", "0") or "0")
    if max_chars <= 0 or len(text or "") <= max_chars:
        return (text or ""), False
    return (text or "")[:max_chars], True


def detect_summary_target_pref(message: str) -> str:
    msg = str(message or "").lower()
    if any(k in msg for k in ["原文摘要", "摘要原文", "原文重點", "source summary", "原文整理"]):
        return "source"
    if any(k in msg for k in ["翻譯後摘要", "翻譯摘要", "translated summary", "翻完再摘"]):
        return "translated"
    return "auto"


def detect_summary_length(message: str) -> str:
    msg = str(message or "").lower()
    if any(k in msg for k in ["簡短", "精簡", "簡要", "short", "brief"]):
        return "short"
    if any(k in msg for k in ["詳細", "完整", "detailed", "full", "long"]):
        return "long"
    return "medium"


def split_translate_chunks(text: str, chunk_chars: int = 4000) -> list[str]:
    paragraphs = re.split(r"\n{2,}", text or "")
    chunks = []
    buf = ""

    def _hard_split(big: str) -> list[str]:
        parts = []
        while big:
            if len(big) <= chunk_chars:
                parts.append(big)
                break
            cut = chunk_chars
            search_start = int(chunk_chars * 0.6)
            # Priority 1: cut at paragraph boundary (double newline)
            dnl = big.rfind("\n\n", search_start, chunk_chars)
            if dnl > 0:
                cut = dnl + 2
            else:
                # Priority 2: cut at newline
                nl = big.rfind("\n", search_start, chunk_chars)
                if nl > 0:
                    cut = nl + 1
                else:
                    # Priority 3: cut at sentence boundary (。！？.!?)
                    sent = -1
                    for sep in ("。", "！", "？", ". ", "! ", "? "):
                        pos = big.rfind(sep, search_start, chunk_chars)
                        if pos > sent:
                            sent = pos
                    if sent > 0:
                        cut = sent + len("。")  # include the punctuation
                    else:
                        # Priority 4: cut at space
                        sp = big.rfind(" ", search_start, chunk_chars)
                        if sp > 0:
                            cut = sp + 1
            parts.append(big[:cut])
            big = big[cut:]
        return parts

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > chunk_chars:
            if buf:
                chunks.append(buf.strip())
                buf = ""
            chunks.extend(_hard_split(para))
            continue
        if buf and len(buf) + len(para) + 2 > chunk_chars:
            chunks.append(buf.strip())
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf.strip())
    return [c for c in chunks if c.strip()]


def estimate_file_processing_time(file_size_bytes: int, filename: str = "", prompt: str = "", file_path: str = "") -> str:
    size_kb = max(1, file_size_bytes / 1024)
    size_mb = size_kb / 1024
    size_str = f"{size_mb:.1f}MB" if size_mb >= 1 else f"{int(size_kb)}KB"
    ext = os.path.splitext((filename or "").lower())[1]
    prompt_lower = (prompt or "").lower()

    # Known image/document extensions
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic"}
    _KNOWN_EXTS = _IMAGE_EXTS | {".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".ogg",
                                  ".aac", ".pdf", ".txt", ".md", ".log", ".csv", ".docx",
                                  ".epub", ".html", ".htm", ".json", ".xml", ".xlsx", ".pptx"}
    # Fallback: if ext is not a known type and filename looks like a screenshot, treat as image
    if ext not in _KNOWN_EXTS and filename:
        _fn_lower = filename.lower()
        if any(kw in _fn_lower for kw in ["截圖", "screenshot", "img_", "image", "photo"]):
            ext = ".png"  # assume PNG for screenshots

    if ext in (".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".ogg", ".aac"):
        task_label = "語音辨識 (逐字稿)"
        if "翻譯" in prompt_lower:
            task_label = "逐字稿含翻譯"
        elif "摘要" in prompt_lower:
            task_label = "逐字稿含摘要"

        audio_duration_s = 0.0
        probe_path = file_path or ""
        if probe_path and os.path.exists(probe_path):
            try:
                import subprocess as _sp
                _dur_out = _sp.run(
                    ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                     "-of", "csv=p=0", probe_path],
                    capture_output=True, text=True, timeout=10,
                )
                if _dur_out.returncode == 0 and _dur_out.stdout.strip():
                    audio_duration_s = float(_dur_out.stdout.strip())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 734, exc_info=True)

        if audio_duration_s > 0:
            audio_min = audio_duration_s / 60.0
            est_seconds = 15 + audio_duration_s * 1.0
        else:
            if ext == ".wav":
                audio_min = size_mb / 10.0
            elif ext in (".mp3", ".aac", ".ogg"):
                audio_min = size_mb / 1.0
            elif ext == ".m4a":
                audio_min = size_mb / 0.9
            elif ext in (".mp4", ".mov", ".mkv"):
                audio_min = size_mb / 8.0
            else:
                audio_min = size_mb / 1.0
            est_seconds = 15 + audio_min * 60.0

        if "翻譯" in prompt_lower or "摘要" in prompt_lower:
            est_text_chars = audio_min * 300
            n_chunks = max(1, int(est_text_chars / 3200))
            if "翻譯" in prompt_lower:
                est_seconds += (n_chunks / 5) * 12 + 10
            else:
                est_seconds += (n_chunks / 5) * 3 + 5

    else:
        # ── Payment proof detection: show correct label in ACK ──
        _payment_kw = ["繳費", "繳款", "繳費憑證", "繳費單", "繳費截圖", "payment proof",
                       "上傳繳費", "銷帳", "入帳", "收據", "裁判費", "上傳閱卷",
                       "上傳收據", "費用憑證"]
        _is_image_ext = ext in _IMAGE_EXTS
        if _is_image_ext and any(kw in prompt_lower for kw in _payment_kw):
            return (
                f"⏳ 已收到截圖 `{filename or '附件'}` ({size_str})，"
                f"正在進行 **繳費憑證辨識與上傳**，預估需要 **約 30 秒**。\n"
                f"處理中請耐心等候，完成後我會回覆結果。"
            )
        if _is_image_ext and not any(k in prompt_lower for k in ["翻譯", "translate", "摘要", "summary"]):
            return (
                f"⏳ 已收到截圖 `{filename or '附件'}` ({size_str})，"
                f"正在進行 **圖片辨識**，預估需要 **約 15-30 秒**。\n"
                f"處理中請耐心等候，完成後我會回覆結果。"
            )

        if ext == ".pdf":
            est_chars = file_size_bytes * 0.08
        elif ext in (".txt", ".md", ".log", ".csv"):
            est_chars = file_size_bytes * 0.9
        elif ext == ".docx":
            est_chars = file_size_bytes * 0.3
        elif ext == ".epub":
            est_chars = file_size_bytes * 0.15
        else:
            est_chars = file_size_bytes * 0.5

        wants_translate = any(k in prompt_lower for k in ["翻譯", "translate", "翻成"])
        wants_full = wants_translate and not any(k in prompt_lower for k in ["摘要", "summary", "總結"])

        # Detect summary level for display
        _summary_level = detect_summary_length(prompt or "")
        _level_label = {"short": "精簡", "long": "詳細"}.get(_summary_level, "")

        if wants_full:
            translate_chunk = 4000
            translate_workers = 2
            n_chunks = max(1, int(est_chars / translate_chunk))
            rounds = max(1, (n_chunks + translate_workers - 1) // translate_workers)
            est_seconds = rounds * 25 + 15
            task_label = "全文翻譯"
        elif wants_translate:
            summary_chunk = 5000
            summary_workers = 1
            n_summary_chunks = max(1, int(est_chars / summary_chunk))
            n_sampled = min(n_summary_chunks, 10)
            summary_rounds = max(1, (n_sampled + summary_workers - 1) // summary_workers)
            est_seconds = summary_rounds * 30 + 20
            est_seconds += 30
            task_label = f"{_level_label}摘要翻譯" if _level_label else "摘要翻譯"
        else:
            summary_chunk = 5000
            summary_workers = 1
            n_chunks = max(1, int(est_chars / summary_chunk))
            n_sampled = min(n_chunks, 10)
            rounds = max(1, (n_sampled + summary_workers - 1) // summary_workers)
            est_seconds = max(25, rounds * 30 + 20)
            task_label = f"{_level_label}摘要" if _level_label else "摘要"

    try:
        from skills.bridge.melchior_client import get_circuit_breaker_status
        if get_circuit_breaker_status().get("open"):
            est_seconds *= 3
            task_label += " (本地降級模式)"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 806, exc_info=True)

    if est_seconds < 60:
        time_str = f"約 {int(est_seconds)} 秒"
    elif est_seconds < 120:
        time_str = "約 1-2 分鐘"
    elif est_seconds < 300:
        time_str = f"約 {int(est_seconds / 60)}-{int(est_seconds / 60) + 1} 分鐘"
    elif est_seconds < 1200:
        time_str = f"約 {int(est_seconds / 60)}-{int(est_seconds / 60) + 3} 分鐘"
    else:
        time_str = f"大於 {int(est_seconds / 60)} 分鐘"

    if ext in (".mp3", ".m4a", ".wav", ".mp4", ".mov", ".mkv", ".ogg", ".aac"):
        chars_str = "音訊/影片檔"
    else:
        chars_str = f"估算約 {int(est_chars / 1000)}K 字" if est_chars >= 1000 else f"約 {int(est_chars)} 字"

    return (
        f"⏳ 已收到檔案 `{filename or '附件'}` ({size_str}, {chars_str})，"
        f"正在進行 **{task_label}**，預估需要 **{time_str}**。\n"
        f"處理中請耐心等候，完成後我會回覆結果。"
    )
