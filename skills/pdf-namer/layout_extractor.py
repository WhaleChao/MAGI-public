#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Docling-based layout sidecar generator for pdf-namer.

This is an optional post-processing step. It writes <pdf>.layout.json and does
not participate in naming decisions.
"""

import json
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pdf-namer.layout")

_CONVERTER = None


def _docling_enabled() -> bool:
    return os.environ.get("MAGI_PDF_NAMER_DOCLING_ENABLED", "0").strip() in {"1", "true", "True"}


def _get_converter():
    global _CONVERTER
    if _CONVERTER is None:
        from docling.document_converter import DocumentConverter

        _CONVERTER = DocumentConverter()
    return _CONVERTER


def generate_layout_sidecar(pdf_path: str, force: bool = False) -> Optional[str]:
    """Generate <pdf>.layout.json with Docling, or return None when disabled/failed."""
    if not _docling_enabled():
        logger.debug("[docling] disabled by env")
        return None
    if not os.path.exists(pdf_path):
        logger.warning("[docling] PDF not found: %s", pdf_path)
        return None

    sidecar = pdf_path + ".layout.json"
    if os.path.exists(sidecar) and not force:
        return sidecar

    try:
        doc_dict = _fixture_layout_document(pdf_path)
        if doc_dict is None:
            result = _get_converter().convert(pdf_path)
            doc_dict = result.document.export_to_dict()
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(doc_dict, f, ensure_ascii=False, indent=2)
        logger.info("[docling] wrote %s", sidecar)
        return sidecar
    except Exception as e:
        logger.warning("[docling] failed for %s: %s", pdf_path, e)
        return None


def _fixture_layout_document(pdf_path: str) -> dict | None:
    """Extract real PDF text/layout with a fixture-owned layout provider."""
    provider_raw = str(os.environ.get("MAGI_PDF_NAMER_LAYOUT_FIXTURE_PATH") or "").strip()
    if not provider_raw:
        return None
    fixture_raw = str(os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT") or "").strip()
    if (
        os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") != "real_entrypoint_fixture_v1"
        or os.environ.get("MAGI_V3_SCHEDULE_DRY_RUN") != "1"
        or not fixture_raw
    ):
        raise RuntimeError("pdf layout fixture is not safely bound")
    fixture = Path(fixture_raw).expanduser().resolve()
    provider_path = Path(provider_raw).expanduser().resolve()
    raw_target = Path(pdf_path).expanduser()
    target = raw_target.resolve()
    if (
        not (fixture / ".magi-v3-schedule-fixture").is_file()
        or not provider_path.is_file()
        or not provider_path.is_relative_to(fixture)
        or not target.is_file()
        or raw_target.is_symlink()
        or not target.is_relative_to(fixture)
    ):
        raise RuntimeError("pdf layout fixture escaped its owned root")
    try:
        provider = json.loads(provider_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("pdf layout fixture provider is unreadable") from exc
    documents = provider.get("documents")
    expected = documents.get(target.name) if isinstance(documents, dict) else None
    if provider.get("schema") != "magi.v3.pdf-layout-fixture/v1" or not isinstance(expected, dict):
        raise RuntimeError("pdf layout fixture provider lacks a bound document")
    import fitz

    document = fitz.open(str(target))
    pages = []
    full_text: list[str] = []
    try:
        for index in range(document.page_count):
            page = document.load_page(index)
            blocks = []
            for block in page.get_text("blocks"):
                text = str(block[4] or "").strip()
                if not text:
                    continue
                blocks.append(
                    {
                        "bbox": [round(float(value), 3) for value in block[:4]],
                        "text": text,
                    }
                )
                full_text.append(text)
            pages.append(
                {
                    "page": index + 1,
                    "width": round(float(page.rect.width), 3),
                    "height": round(float(page.rect.height), 3),
                    "blocks": blocks,
                }
            )
    finally:
        document.close()
    combined = "\n".join(full_text)
    expected_text = str(expected.get("expected_text") or "").strip()
    if not pages or not combined or (expected_text and expected_text not in combined):
        raise RuntimeError("pdf layout fixture expectation is not supported by parsed PDF")
    return {
        "schema": "magi.pdf-layout-sidecar/v1",
        "source_pdf": target.name,
        "source_pdf_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "parsed_text_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        "page_count": len(pages),
        "pages": pages,
        "provider_quality_certified": False,
        "provider_role": "deterministic_docling_layout_fixture",
    }
