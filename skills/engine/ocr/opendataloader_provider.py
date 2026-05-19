# -*- coding: utf-8 -*-
"""Optional OpenDataLoader PDF text/OCR provider.

OpenDataLoader PDF provides layout-aware reading order, Markdown/JSON output,
and optional hybrid OCR for scanned PDFs. MAGI keeps it as a best-effort
provider: missing package, missing Java, or hybrid server errors never break
callers.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from skills.engine.ocr.legal_corrector import correct_legal_text
from skills.engine.ocr.legal_entities import extract_entities
from skills.engine.ocr.ocr_schema import OCRProviderResult
from skills.engine.ocr.quality import compute_quality_score


_CACHE: Dict[Tuple[str, int, int, str, str], OCRProviderResult] = {}


def _enabled() -> bool:
    raw = os.environ.get("MAGI_OPENDATALOADER_PDF_ENABLE", "auto").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _hybrid_mode() -> str:
    return os.environ.get("MAGI_OPENDATALOADER_PDF_HYBRID", "").strip()


def _max_chars() -> int:
    try:
        return max(1000, int(os.environ.get("MAGI_OPENDATALOADER_PDF_MAX_CHARS", "24000") or "24000"))
    except Exception:
        return 24000


def _collect_json_text(obj: Any, chunks: list[str], limit: int) -> None:
    if sum(len(c) for c in chunks) >= limit:
        return
    if isinstance(obj, dict):
        for key in ("markdown", "text", "content", "caption", "title", "html"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                chunks.append(val.strip())
        for val in obj.values():
            if isinstance(val, (dict, list)):
                _collect_json_text(val, chunks, limit)
    elif isinstance(obj, list):
        for val in obj:
            _collect_json_text(val, chunks, limit)


def _read_output_text(output_dir: Path, limit: int) -> str:
    chunks: list[str] = []
    for pattern in ("*.md", "*.markdown", "*.txt", "*.html"):
        for path in sorted(output_dir.rglob(pattern)):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                continue
            if text:
                chunks.append(text)
            if sum(len(c) for c in chunks) >= limit:
                break
    for path in sorted(output_dir.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        _collect_json_text(payload, chunks, limit)
        if sum(len(c) for c in chunks) >= limit:
            break
    return "\n\n".join(chunks)[:limit].strip()


def _page_key(page_indexes: Optional[Sequence[int]]) -> str:
    if not page_indexes:
        return "all"
    return ",".join(str(int(p)) for p in page_indexes)


def _materialize_page_subset(path: Path, page_indexes: Optional[Sequence[int]], work_dir: Path) -> Path:
    if not page_indexes:
        return path
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"PyMuPDF unavailable for page subset: {exc}") from exc

    src = fitz.open(str(path))
    try:
        dst = fitz.open()
        for raw_idx in page_indexes:
            idx = int(raw_idx)
            if 0 <= idx < src.page_count:
                dst.insert_pdf(src, from_page=idx, to_page=idx)
        if dst.page_count <= 0:
            raise ValueError("page subset is empty")
        subset = work_dir / f"{path.stem}_pages_{_page_key(page_indexes).replace(',', '_')}.pdf"
        dst.save(str(subset))
        dst.close()
        return subset
    finally:
        src.close()


def run_pdf(
    pdf_path: str,
    *,
    task_type: str = "legal",
    timeout_sec: float | None = None,
    page_indexes: Optional[Sequence[int]] = None,
) -> OCRProviderResult:
    """Extract text from a PDF using OpenDataLoader when available."""

    if not _enabled():
        return OCRProviderResult.failure("opendataloader_pdf", "disabled")

    path = Path(pdf_path)
    if not path.exists() or not path.is_file():
        return OCRProviderResult.failure("opendataloader_pdf", "pdf not found")

    try:
        stat = path.stat()
    except Exception as exc:
        return OCRProviderResult.failure("opendataloader_pdf", f"stat failed: {type(exc).__name__}: {exc}")

    hybrid = _hybrid_mode()
    page_scope = _page_key(page_indexes)
    key = (str(path.resolve()), int(stat.st_mtime), int(stat.st_size), hybrid, page_scope)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    started = time.monotonic()
    try:
        import opendataloader_pdf  # type: ignore
    except Exception as exc:
        result = OCRProviderResult.failure("opendataloader_pdf", f"import failed: {type(exc).__name__}: {exc}")
        _CACHE[key] = result
        return result

    try:
        with tempfile.TemporaryDirectory(prefix="magi_opendataloader_pdf_") as td:
            work_dir = Path(td)
            input_pdf = _materialize_page_subset(path, page_indexes, work_dir)
            kwargs: dict[str, Any] = {
                "input_path": [str(input_pdf)],
                "output_dir": td,
                "format": "markdown,json",
            }
            if hybrid:
                kwargs["hybrid"] = hybrid
            opendataloader_pdf.convert(**kwargs)
            raw_text = _read_output_text(Path(td), _max_chars())
    except Exception as exc:
        result = OCRProviderResult.failure(
            "opendataloader_pdf",
            f"convert failed: {type(exc).__name__}: {str(exc)[:220]}",
        )
        result.duration_sec = round(time.monotonic() - started, 3)
        _CACHE[key] = result
        return result

    if not raw_text.strip():
        result = OCRProviderResult.failure("opendataloader_pdf", "empty output")
        result.duration_sec = round(time.monotonic() - started, 3)
        _CACHE[key] = result
        return result

    corrected = raw_text if task_type == "captcha" else correct_legal_text(raw_text, task_type=task_type).corrected_text
    quality = compute_quality_score(corrected or raw_text)
    entities = None if task_type == "captcha" else extract_entities(corrected or raw_text)
    result = OCRProviderResult(
        success=True,
        provider="opendataloader_pdf_hybrid" if hybrid else "opendataloader_pdf",
        raw_text=raw_text,
        corrected_text=corrected,
        quality_score=quality,
        entities=entities,
        duration_sec=round(time.monotonic() - started, 3),
    )
    _CACHE[key] = result
    return result
