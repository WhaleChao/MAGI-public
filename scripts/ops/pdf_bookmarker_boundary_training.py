#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export PDF bookmarker boundary failures as review/training data.

This script is intentionally non-destructive: it reads the historical
bookmark-batch state and writes a JSONL corpus that tells MAGI whether an old
no-boundary PDF should be resolved by a filename/page-1 bookmark, OCR, retry,
or manual/vision review.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import weekend_bookmark_batch as bookmark_batch  # noqa: E402


def _training_label(action: dict) -> str:
    name = str(action.get("action") or "")
    if name == "single_doc_page1_bookmark":
        return "filename_page1_bookmark"
    if name == "ocr_then_bookmark":
        return "ocr_required"
    if name in {"offpeak_retry_stage1", "split_large_ocr"}:
        return "retry_or_split"
    if name == "missing":
        return "ignore_missing_runtime_file"
    return "manual_or_vision_review"


def build_training_rows(state_path: Path) -> list[dict]:
    data = json.loads(state_path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for raw_path, info in sorted((data.get("completed") or {}).items()):
        if not isinstance(info, dict) or not info.get("no_boundary"):
            continue
        pdf = Path(raw_path)
        try:
            mtime = str(pdf.stat().st_mtime)
        except Exception:
            mtime = ""
        action = bookmark_batch._followup_action_for(pdf, info, mtime)
        rows.append({
            "path": raw_path,
            "filename": pdf.name,
            "exists": pdf.exists(),
            "pages": int(info.get("pages") or 0),
            "previous_message": info.get("message", ""),
            "recommended_action": action.get("action"),
            "reason": action.get("reason", ""),
            "training_label": _training_label(action),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Export pdf-bookmarker no-boundary training data.")
    parser.add_argument("--state", default=str(ROOT / ".agent" / "bookmark_batch_state.json"))
    parser.add_argument("--out", default=str(ROOT / ".runtime" / "pdf_bookmarker_boundary_training_latest.jsonl"))
    parser.add_argument("--summary", default=str(ROOT / ".runtime" / "pdf_bookmarker_boundary_training_summary.json"))
    args = parser.parse_args()

    state_path = Path(args.state).expanduser()
    out_path = Path(args.out).expanduser()
    summary_path = Path(args.summary).expanduser()
    if not state_path.is_absolute():
        state_path = ROOT / state_path
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    if not summary_path.is_absolute():
        summary_path = ROOT / summary_path

    rows = build_training_rows(state_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    summary = {
        "generated_at": datetime.now().isoformat(),
        "state": str(state_path),
        "rows": len(rows),
        "actions": dict(Counter(row["recommended_action"] for row in rows)),
        "training_labels": dict(Counter(row["training_label"] for row in rows)),
        "output": str(out_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
