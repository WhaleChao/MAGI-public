#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Private Chandra OCR readiness/live check.

This script deliberately records only metadata and text lengths. It does not
write raw OCR text to runtime JSON, because OCR inputs are often case material.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from skills.engine.ocr import chandra_provider
from skills.engine.ocr.legal_entities import extract_entities


def _runtime_path() -> Path:
    base = Path(os.environ.get("MAGI_RUNTIME_DIR", str(ROOT / ".runtime")))
    base.mkdir(parents=True, exist_ok=True)
    return base / "chandra_ocr_health_latest.json"


def _entity_counts(text: str) -> Dict[str, int]:
    ents = extract_entities(text or "")
    return ents.to_counts()


def run(pdf_path: str = "", page: int = 0, timeout_sec: float = 45.0) -> Dict[str, Any]:
    probe = chandra_provider.probe(check_server=True)
    report: Dict[str, Any] = {
        "ok": bool(probe.available),
        "provider": "chandra",
        "probe": probe.to_dict(),
        "live_pdf": None,
        "notes": [
            "private-only optional OCR fallback",
            "raw OCR text is intentionally not persisted",
        ],
    }
    if not pdf_path:
        return report

    live: Dict[str, Any] = {
        "path": pdf_path,
        "page": page,
        "success": False,
        "text_len": 0,
        "duration_sec": 0.0,
        "entity_counts": {},
        "error": "",
    }
    result = chandra_provider.run_pdf_page(pdf_path, page_num=page, timeout_sec=timeout_sec)
    live.update(
        {
            "success": bool(result.success),
            "text_len": len(result.text or ""),
            "duration_sec": result.duration_sec,
            "entity_counts": _entity_counts(result.text or "") if result.success else {},
            "error": result.error or "",
        }
    )
    report["live_pdf"] = live
    report["ok"] = bool(probe.available and result.success)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check private Chandra OCR readiness.")
    parser.add_argument("--pdf", default="", help="Optional PDF path for a one-page live OCR test.")
    parser.add_argument("--page", type=int, default=0, help="Zero-based page index for --pdf.")
    parser.add_argument("--timeout-sec", type=float, default=45.0)
    parser.add_argument("--json-out", default="", help="Optional output path; defaults to .runtime.")
    args = parser.parse_args()

    report = run(args.pdf, page=args.page, timeout_sec=args.timeout_sec)
    out_path = Path(args.json_out) if args.json_out else _runtime_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
