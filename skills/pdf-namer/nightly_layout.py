#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nightly Docling layout sidecar backfill for recently filed PDFs.

Default is disabled by MAGI_PDF_NAMER_DOCLING_ENABLED=0 in cron_jobs.json.
When enabled, prefer pdf-namer's filing log and only fall back to a bounded scan
of the auto-filed PDF area.
"""

import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Set

SCRIPT_DIR = Path(__file__).resolve().parent
MAGI_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

FILING_LOG = SCRIPT_DIR / "_filing_log.json"
SCAN_ROOT = (
    Path.home()
    / "Library"
    / "CloudStorage"
    / "SynologyDrive-homes"
    / "02_掃描檔案"
    / "02_自動歸檔區"
)
LOOKBACK_SEC = int(os.environ.get("MAGI_PDF_NAMER_DOCLING_LOOKBACK_SEC", "86400") or "86400")
MAX_SCAN_PDFS = int(os.environ.get("MAGI_PDF_NAMER_DOCLING_MAX_SCAN", "50") or "50")
MAX_SCAN_DEPTH = int(os.environ.get("MAGI_PDF_NAMER_DOCLING_MAX_DEPTH", "5") or "5")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [nightly_layout] %(levelname)s %(message)s")
logger = logging.getLogger("pdf-namer.nightly-layout")


def _enabled() -> bool:
    return os.environ.get("MAGI_PDF_NAMER_DOCLING_ENABLED", "0").strip() in {"1", "true", "True"}


def _collect_from_filing_log(cutoff: float) -> List[str]:
    if not FILING_LOG.exists():
        return []
    try:
        data = json.loads(FILING_LOG.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("failed to read filing log: %s", e)
        return []

    paths: List[str] = []
    for entry in data:
        try:
            ts = datetime.datetime.fromisoformat(str(entry.get("timestamp", ""))).timestamp()
        except Exception:
            continue
        if ts < cutoff:
            continue
        for filed in entry.get("filed", []):
            if filed.get("status") != "filed":
                continue
            dest = filed.get("destination") or ""
            name = filed.get("new_name") or ""
            if not dest or not name:
                continue
            full = os.path.join(dest, name)
            if os.path.exists(full):
                paths.append(full)
    logger.info("filing log candidates=%d", len(paths))
    return paths


def _collect_from_scan_root(cutoff: float) -> List[str]:
    if not SCAN_ROOT.is_dir():
        return []
    root_s = str(SCAN_ROOT)
    paths: List[str] = []
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root_s):
        depth = dirpath[len(root_s):].count(os.sep)
        if depth >= MAX_SCAN_DEPTH:
            dirnames.clear()
        visited += 1
        if visited % 50 == 0:
            time.sleep(0.05)
        for fn in filenames:
            if not fn.lower().endswith(".pdf") or fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getmtime(full) >= cutoff:
                    paths.append(full)
            except OSError:
                continue
            if len(paths) >= MAX_SCAN_PDFS:
                logger.info("scan-root candidates capped at %d", MAX_SCAN_PDFS)
                return paths
    logger.info("scan-root candidates=%d", len(paths))
    return paths


def _dedupe(paths: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def main() -> int:
    if not _enabled():
        logger.info("MAGI_PDF_NAMER_DOCLING_ENABLED disabled; no-op")
        return 0

    from layout_extractor import generate_layout_sidecar

    cutoff = time.time() - LOOKBACK_SEC
    paths = _collect_from_filing_log(cutoff) or _collect_from_scan_root(cutoff)
    paths = _dedupe(paths)
    if not paths:
        logger.info("no recent PDFs")
        return 0

    ok = 0
    fail = 0
    for pdf_path in paths:
        sidecar = generate_layout_sidecar(pdf_path)
        if sidecar and os.path.exists(sidecar):
            ok += 1
        else:
            fail += 1
    logger.info("done: total=%d ok=%d fail=%d", len(paths), ok, fail)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
