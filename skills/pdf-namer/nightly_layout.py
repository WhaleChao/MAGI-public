#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nightly Docling layout sidecar backfill for recently filed PDFs.

Default is disabled by MAGI_PDF_NAMER_DOCLING_ENABLED=0 in cron_jobs.json.
When enabled, prefer pdf-namer's filing log and only fall back to a bounded scan
of the auto-filed PDF area.
"""

import datetime
import hashlib
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

from state_paths import configured_read_path, prepare_write, state_path

FILING_LOG = state_path("_filing_log.json")
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
    filing_log = configured_read_path("_filing_log.json", FILING_LOG)
    if not filing_log.exists():
        return []
    try:
        data = json.loads(filing_log.read_text(encoding="utf-8"))
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


def _certification_fixture_root() -> Path | None:
    if os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") != "real_entrypoint_fixture_v1":
        return None
    fixture_raw = str(os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT") or "").strip()
    fixture = Path(fixture_raw).expanduser().resolve() if fixture_raw else None
    if (
        os.environ.get("MAGI_V3_SCHEDULE_DRY_RUN") != "1"
        or fixture is None
        or not (fixture / ".magi-v3-schedule-fixture").is_file()
    ):
        raise RuntimeError("nightly layout fixture is not safely bound")
    return fixture


def _write_manifest(payload: dict) -> Path:
    target = prepare_write(state_path("_nightly_layout_report.json"))
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def main() -> int:
    if not _enabled():
        logger.info("MAGI_PDF_NAMER_DOCLING_ENABLED disabled; no-op")
        return 0

    from layout_extractor import generate_layout_sidecar

    cutoff = time.time() - LOOKBACK_SEC
    paths = _collect_from_filing_log(cutoff) or _collect_from_scan_root(cutoff)
    paths = _dedupe(paths)
    fixture = _certification_fixture_root()
    if fixture is not None:
        bounded: List[str] = []
        for raw in paths:
            path = Path(raw).expanduser()
            resolved = path.resolve()
            if path.is_symlink() or not resolved.is_file() or not resolved.is_relative_to(fixture):
                raise RuntimeError("nightly layout PDF escaped its owned fixture root")
            bounded.append(str(resolved))
        paths = bounded
    if not paths:
        logger.info("no recent PDFs")
        if fixture is not None:
            _write_manifest(
                {
                    "ok": False,
                    "status": "no_fixture_pdfs",
                    "total": 0,
                    "items": [],
                    "provider_quality_certified": False,
                }
            )
            return 1
        return 0

    ok = 0
    fail = 0
    items = []
    for pdf_path in paths:
        sidecar = generate_layout_sidecar(pdf_path)
        if sidecar and os.path.exists(sidecar):
            ok += 1
            sidecar_path = Path(sidecar)
            try:
                payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                payload = {}
            items.append(
                {
                    "pdf": pdf_path,
                    "pdf_sha256": hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest(),
                    "sidecar": sidecar,
                    "sidecar_sha256": hashlib.sha256(sidecar_path.read_bytes()).hexdigest(),
                    "page_count": payload.get("page_count"),
                    "parsed_text_sha256": payload.get("parsed_text_sha256"),
                    "provider_quality_certified": payload.get("provider_quality_certified"),
                    "provider_role": payload.get("provider_role") or "live_docling",
                    "ok": bool(payload),
                }
            )
        else:
            fail += 1
            items.append({"pdf": pdf_path, "sidecar": "", "ok": False})
    manifest = {
        "ok": fail == 0 and ok == len(paths),
        "status": "passed" if fail == 0 and ok == len(paths) else "failed",
        "total": len(paths),
        "generated": ok,
        "failed": fail,
        "items": items,
        "provider_quality_certified": not any(
            item.get("provider_quality_certified") is False for item in items
        ),
        "provider_role": (
            "deterministic_docling_layout_fixture"
            if any(item.get("provider_quality_certified") is False for item in items)
            else "live_docling"
        ),
    }
    _write_manifest(manifest)
    logger.info("done: total=%d ok=%d fail=%d", len(paths), ok, fail)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
