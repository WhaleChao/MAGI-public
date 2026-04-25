#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Docling-based layout sidecar generator for pdf-namer.

This is an optional post-processing step. It writes <pdf>.layout.json and does
not participate in naming decisions.
"""

import json
import logging
import os
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
        result = _get_converter().convert(pdf_path)
        doc_dict = result.document.export_to_dict()
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(doc_dict, f, ensure_ascii=False, indent=2)
        logger.info("[docling] wrote %s", sidecar)
        return sidecar
    except Exception as e:
        logger.warning("[docling] failed for %s: %s", pdf_path, e)
        return None
