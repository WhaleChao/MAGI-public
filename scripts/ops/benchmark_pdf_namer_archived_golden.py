#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare pdf-namer proposals against already filed/renamed PDFs."""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MAGI_ROOT / "skills" / "pdf-namer"))

ACTION_PATH = MAGI_ROOT / "skills" / "pdf-namer" / "action.py"
FILING_LOG = MAGI_ROOT / "skills" / "pdf-namer" / "_filing_log.json"
OUTPUT_PATH = MAGI_ROOT / ".runtime" / "benchmark_pdf_namer_archived_golden_latest.json"
MAX_CASES = int(os.environ.get("MAGI_PDF_NAMER_ARCHIVED_GOLDEN_MAX", "5") or "5")


def _load_namer():
    spec = importlib.util.spec_from_file_location("pdf_namer_action_golden", str(ACTION_PATH))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _collect_cases():
    data = json.loads(FILING_LOG.read_text(encoding="utf-8"))
    cases = []
    for entry in data:
        if entry.get("dry_run"):
            continue
        for filed in entry.get("filed", []):
            if filed.get("status") != "filed":
                continue
            dest = filed.get("destination") or ""
            name = filed.get("new_name") or ""
            if not dest or not name:
                continue
            path = os.path.join(dest, name)
            if os.path.exists(path):
                cases.append({
                    "path": path,
                    "expected": os.path.basename(path),
                    "case": filed.get("case", ""),
                    "doc_type": filed.get("doc_type", ""),
                })
    return cases[:MAX_CASES]


def main() -> int:
    namer = _load_namer()
    cases = _collect_cases()
    results = []
    matched = 0
    for case in cases:
        started = time.time()
        proposal = namer.generate_name_proposal(case["path"], return_structured=True)
        actual = proposal.get("filename") if isinstance(proposal, dict) else None
        ok = actual == case["expected"]
        matched += int(ok)
        results.append({
            **case,
            "actual": actual,
            "ok": ok,
            "elapsed_sec": round(time.time() - started, 2),
        })
        status = "OK" if ok else "DIFF"
        print(f"[{status}] {case['expected']} -> {actual}")

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": len(cases),
        "matched": matched,
        "match_rate": round(matched / len(cases), 3) if cases else None,
        "results": results,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not cases:
        print("[SKIP] no accessible filed PDFs")
        return 0
    print(f"[summary] matched={matched}/{len(cases)}")
    return 0 if matched == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
