"""
document_reader.py
==================
Unified document-to-text adapter backed by Microsoft MarkItDown.

Provides a single ``read_document()`` entry point that converts
PDF / DOCX / XLSX / PPTX / HTML / CSV / JSON / XML / images → text + Markdown.

For scanned PDFs the quality gate auto-falls-back to the existing
pdf_bridge OCR chain (pdftotext → fitz → pdfplumber → tesseract+Vision).

Phase 1 — standalone module.  No existing module imports this yet.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("DocumentReader")

# ---------------------------------------------------------------------------
# Quality hint words — ported from skills/documents/pdf_bridge.py
# ---------------------------------------------------------------------------
_QUALITY_HINT_WORDS = frozenset({
    "the", "court", "judgment", "judgements", "application", "case", "convention",
    "international", "justice", "objections", "preliminary", "compensation", "republic",
    "kingdom", "great", "britain", "northern", "ireland", "president", "judges",
    "declaration", "dissenting", "opinion", "article", "statute", "tribunal",
    "法院", "判決", "裁定", "裁判", "主文", "理由", "原告", "被告", "上訴", "抗告",
    "司法院", "國際法院", "法官", "程序", "聲請", "裁判書",
})


# ---------------------------------------------------------------------------
# DocumentResult dataclass
# ---------------------------------------------------------------------------
@dataclass
class DocumentResult:
    success: bool = False
    text: str = ""           # plain text (for vector ingest)
    markdown: str = ""       # raw MarkItDown output (for LLM consumption)
    method: str = ""         # "markitdown" | "legacy_ocr" | "fallback" | "error"
    quality_score: float = 0.0
    page_count: int = 0
    error: str = ""
    elapsed_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Quality gate — adapted from pdf_bridge._text_quality_stats
# ---------------------------------------------------------------------------
def _strip_markers(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"---\s*第\s*\d+\s*頁(?:\s*\(OCR\))?\s*---", " ", s)
    s = re.sub(r"\f", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def text_quality_score(text: str) -> float:
    """Return 0.0–1.0 score indicating how likely *text* is real content."""
    body = _strip_markers(text)
    if not body:
        return 0.0

    normalized_tokens = re.findall(r"[A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff''\-]{1,}", body)
    token_count = len(normalized_tokens)
    lower_tokens = [tok.lower() for tok in normalized_tokens]
    hint_hits = sum(1 for tok in lower_tokens if tok in _QUALITY_HINT_WORDS)

    letter_tokens = [tok for tok in re.findall(r"\S+", body) if re.search(r"[A-Za-z\u4e00-\u9fff]", tok)]
    suspicious = 0
    for tok in letter_tokens:
        bad_mix = bool(re.search(r"[A-Za-z]+\d+[A-Za-z]*|\d+[A-Za-z]+", tok))
        bad_punct = bool(re.search(r"[A-Za-z][^A-Za-z\u4e00-\u9fff\s]{2,}[A-Za-z]", tok))
        if bad_mix or bad_punct or "\\x" in tok or "\\u" in tok:
            suspicious += 1

    weird_chars = len(re.findall(r"[\\^~<>|{}\[\]`]", body))
    alpha_chars = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", body))
    token_base = max(1, len(letter_tokens))
    hint_ratio = hint_hits / max(1, token_count)
    suspicious_ratio = suspicious / token_base
    weird_ratio = weird_chars / max(1, alpha_chars)
    density = min(1.0, token_count / 180.0)

    score = max(
        0.0,
        min(
            1.0,
            (0.55 * hint_ratio)
            + (0.25 * density)
            + (0.12 * max(0.0, 1.0 - suspicious_ratio))
            + (0.08 * max(0.0, 1.0 - min(1.0, weird_ratio * 8.0))),
        ),
    )
    return score


def _is_meaningful(text: str, min_chars: int = 16) -> bool:
    return len(re.sub(r"\s+", "", str(text or ""))) >= min_chars


# ---------------------------------------------------------------------------
# Markdown → plain text helper
# ---------------------------------------------------------------------------
def _markdown_to_plain(md_text: str) -> str:
    """Rough Markdown → plain text (strip headings, bold, links, etc.)."""
    s = str(md_text or "")
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.MULTILINE)       # headings
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)                     # bold
    s = re.sub(r"\*(.+?)\*", r"\1", s)                          # italic
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)              # links
    s = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", s)             # images
    s = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", s)               # code
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---------------------------------------------------------------------------
# MarkItDown wrapper (lazy import)
# ---------------------------------------------------------------------------
_markitdown_available: Optional[bool] = None
_markitdown_instance = None


def _get_markitdown():
    """Lazy-init MarkItDown instance. Returns None if package unavailable."""
    global _markitdown_available, _markitdown_instance
    if _markitdown_available is False:
        return None
    if _markitdown_instance is not None:
        return _markitdown_instance
    try:
        from markitdown import MarkItDown  # type: ignore
        _markitdown_instance = MarkItDown()
        _markitdown_available = True
        return _markitdown_instance
    except Exception:
        _markitdown_available = False
        logger.info("markitdown not available, will use legacy extractors")
        return None


# ---------------------------------------------------------------------------
# PDF OCR fallback (delegates to pdf_bridge)
# ---------------------------------------------------------------------------
def _pdf_ocr_fallback(file_path: str, max_chars: int) -> DocumentResult:
    """Fall back to pdf_bridge multi-engine OCR chain for scanned PDFs."""
    try:
        from skills.documents.pdf_bridge import extract_text  # type: ignore
        result = extract_text(file_path)
        text = str(result.get("text", "") if isinstance(result, dict) else result or "")
        text = text[:max_chars]
        score = text_quality_score(text)
        return DocumentResult(
            success=bool(text),
            text=text,
            markdown=text,
            method="legacy_ocr",
            quality_score=score,
            page_count=result.get("page_count", 0) if isinstance(result, dict) else 0,
        )
    except Exception as e:
        logger.warning("pdf_bridge fallback failed: %s", e)
        return DocumentResult(success=False, method="error", error=str(e))


# ---------------------------------------------------------------------------
# file_bridge fallback (for DOCX/etc.)
# ---------------------------------------------------------------------------
def _file_bridge_fallback(file_path: str, max_chars: int) -> DocumentResult:
    """Fall back to file_bridge for non-PDF formats."""
    try:
        from skills.documents.file_bridge import extract_text_from_file  # type: ignore
        result = extract_text_from_file(file_path)
        text = str(result.get("text", "") if isinstance(result, dict) else result or "")
        text = text[:max_chars]
        return DocumentResult(
            success=bool(text),
            text=text,
            markdown=text,
            method="fallback",
            quality_score=text_quality_score(text),
        )
    except Exception as e:
        logger.warning("file_bridge fallback failed: %s", e)
        return DocumentResult(success=False, method="error", error=str(e))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_PDF_EXTS = {".pdf"}
_DIGITAL_NATIVE_EXTS = {
    ".docx", ".doc", ".pptx", ".ppt", ".html", ".htm",
    ".csv", ".json", ".xml", ".epub", ".md", ".txt", ".log",
}
_XLSX_EXTS = {".xlsx", ".xls"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"}


def read_document(
    file_path: str,
    *,
    mode: str = "auto",
    max_chars: int = 500_000,
    ocr_fallback: bool = True,
    quality_threshold: float = 0.3,
    timeout_sec: int = 60,
) -> DocumentResult:
    """
    Convert a document file to text + markdown.

    Parameters
    ----------
    file_path : str
        Path to the file.
    mode : str
        ``"auto"`` (default) — MarkItDown first, fallback if quality low.
        ``"markitdown"`` — MarkItDown only, no fallback.
        ``"legacy"`` — skip MarkItDown entirely, use existing extractors.
    max_chars : int
        Truncate output at this many characters.
    ocr_fallback : bool
        Whether to fall back to OCR for low-quality PDF extraction.
    quality_threshold : float
        Minimum quality score (0–1) to accept MarkItDown PDF output.
    timeout_sec : int
        Timeout for MarkItDown conversion (unused in v0.0.2, reserved).

    Returns
    -------
    DocumentResult
    """
    t0 = time.monotonic()
    path = str(file_path or "").strip()
    if not path or not os.path.isfile(path):
        return DocumentResult(
            success=False, method="error",
            error="file_not_found: %s" % path,
            elapsed_ms=(time.monotonic() - t0) * 1000,
        )

    ext = os.path.splitext(path)[1].lower()

    # --- Legacy-only mode ---
    if mode == "legacy":
        if ext in _PDF_EXTS:
            r = _pdf_ocr_fallback(path, max_chars)
        else:
            r = _file_bridge_fallback(path, max_chars)
        r.elapsed_ms = (time.monotonic() - t0) * 1000
        return r

    # --- Try MarkItDown ---
    md = _get_markitdown()
    if md is None:
        # MarkItDown not installed — silent fallback
        if ext in _PDF_EXTS:
            r = _pdf_ocr_fallback(path, max_chars) if ocr_fallback else DocumentResult(
                success=False, method="error", error="markitdown_unavailable")
        else:
            r = _file_bridge_fallback(path, max_chars)
        r.elapsed_ms = (time.monotonic() - t0) * 1000
        return r

    try:
        result = md.convert(path)
        md_text = str(result.text_content or "")[:max_chars]
    except Exception as e:
        logger.info("MarkItDown convert failed for %s: %s", path, e)
        md_text = ""

    if md_text and _is_meaningful(md_text):
        score = text_quality_score(md_text)
        plain = _markdown_to_plain(md_text)

        # PDF quality gate
        if ext in _PDF_EXTS and score < quality_threshold and mode != "markitdown":
            if ocr_fallback:
                logger.info(
                    "MarkItDown PDF quality %.2f < %.2f, falling back to OCR: %s",
                    score, quality_threshold, path,
                )
                r = _pdf_ocr_fallback(path, max_chars)
                r.elapsed_ms = (time.monotonic() - t0) * 1000
                r.metadata["markitdown_attempted"] = True
                r.metadata["markitdown_score"] = score
                return r

        elapsed = (time.monotonic() - t0) * 1000
        logger.info(
            "MarkItDown OK: %s | score=%.2f | method=markitdown | %.0fms",
            os.path.basename(path), score, elapsed,
        )
        return DocumentResult(
            success=True,
            text=plain,
            markdown=md_text,
            method="markitdown",
            quality_score=score,
            elapsed_ms=elapsed,
            metadata={
                "has_cell_access": False if ext in _XLSX_EXTS else None,
            },
        )

    # MarkItDown produced nothing useful — fallback
    if mode == "markitdown":
        return DocumentResult(
            success=False, method="markitdown",
            error="empty_output",
            elapsed_ms=(time.monotonic() - t0) * 1000,
        )

    if ext in _PDF_EXTS and ocr_fallback:
        r = _pdf_ocr_fallback(path, max_chars)
    else:
        r = _file_bridge_fallback(path, max_chars)
    r.elapsed_ms = (time.monotonic() - t0) * 1000
    r.metadata["markitdown_attempted"] = True
    return r
