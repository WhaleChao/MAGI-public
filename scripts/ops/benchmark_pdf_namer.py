#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark_pdf_namer.py
========================
Live benchmark for the pdf-namer skill.

Metrics:
  - format_valid_rate    : % of proposals passing naming_validator
  - holding_coverage     : % with non-empty holding field (for 判決/裁定)
  - empty_filename_rate  : % of proposals returning empty filename

Exit 1 if format_valid_rate < 70% or empty_filename_rate > 5%.
Writes results to .runtime/benchmark_pdf_namer_latest.json.
"""
import importlib.util
import json
import os
import sys
import time
import logging

logging.basicConfig(level=logging.WARNING)

MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, MAGI_ROOT)

NAS_CASE_ROOT = "/Volumes/lumi/lumi/01_案件"
FALLBACK_ROOT = os.path.expanduser("~/Library/CloudStorage/SynologyDrive-homes/01_案件")
MAX_PDFS = int(os.environ.get("MAGI_PDF_NAMER_BENCHMARK_MAX_PDFS", "100"))
OUTPUT_PATH = os.path.join(MAGI_ROOT, ".runtime", "benchmark_pdf_namer_latest.json")

FORMAT_VALID_THRESHOLD = 0.70
EMPTY_THRESHOLD = 0.05
HOLDING_THRESHOLD = 0.50


def find_pdfs(root: str, limit: int = MAX_PDFS):
    """Scan NAS for PDF files with depth limit."""
    pdfs = []
    try:
        for dirpath, dirnames, files in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth >= 5:
                dirnames.clear()
                continue
            for f in files:
                if f.lower().endswith(".pdf") and not f.startswith("."):
                    pdfs.append(os.path.join(dirpath, f))
                    if len(pdfs) >= limit:
                        return pdfs
            if len(pdfs) >= limit:
                break
    except Exception as e:
        print(f"[WARN] scan error: {e}")
    return pdfs


def main():
    case_root = NAS_CASE_ROOT if os.path.isdir(NAS_CASE_ROOT) else FALLBACK_ROOT
    if not os.path.isdir(case_root):
        print(f"[SKIP] NAS not mounted at {case_root}. Skipping benchmark.")
        sys.exit(0)

    try:
        sys.path.insert(0, os.path.join(MAGI_ROOT, "skills", "pdf-namer"))
        from naming_validator import validate_filename
        import action as namer
    except ImportError:
        validator_spec = importlib.util.spec_from_file_location(
            "pdf_namer_validator",
            os.path.join(MAGI_ROOT, "skills", "pdf-namer", "naming_validator.py"),
        )
        validator_mod = importlib.util.module_from_spec(validator_spec)
        validator_spec.loader.exec_module(validator_mod)
        validate_filename = validator_mod.validate_filename

        namer_spec = importlib.util.spec_from_file_location(
            "pdf_namer_action",
            os.path.join(MAGI_ROOT, "skills", "pdf-namer", "action.py"),
        )
        namer = importlib.util.module_from_spec(namer_spec)
        namer_spec.loader.exec_module(namer)

    pdfs = find_pdfs(case_root)
    if not pdfs:
        print("[SKIP] No PDFs found. Skipping benchmark.")
        sys.exit(0)

    total = len(pdfs)
    valid_format = 0
    empty_count = 0
    holding_applicable = 0
    holding_found = 0
    results = []

    print(f"[benchmark] Running pdf-namer on {total} PDFs...")
    for pdf_path in pdfs:
        try:
            r = namer.generate_name_proposal(pdf_path, return_structured=True)
            if r is None:
                empty_count += 1
                results.append({"path": pdf_path, "filename": None, "valid": False})
                continue

            filename = r.get("filename") or ""
            if not filename:
                empty_count += 1
                results.append({"path": pdf_path, "filename": None, "valid": False})
                continue

            ok, warns = validate_filename(filename)
            if ok:
                valid_format += 1

            doc_type = r.get("doc_type", "")
            if doc_type and any(t in doc_type for t in ("判決", "裁定")):
                holding_applicable += 1
                if r.get("holding"):
                    holding_found += 1

            results.append({
                "path": pdf_path,
                "filename": filename,
                "valid": ok,
                "warns": warns,
                "holding": r.get("holding", ""),
            })
        except Exception as e:
            empty_count += 1
            results.append({"path": pdf_path, "error": str(e)})

    format_valid_rate = valid_format / total if total else 0.0
    empty_rate = empty_count / total if total else 0.0
    holding_coverage = holding_found / holding_applicable if holding_applicable else None

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": total,
        "format_valid_rate": round(format_valid_rate, 3),
        "empty_filename_rate": round(empty_rate, 3),
        "holding_coverage": round(holding_coverage, 3) if holding_coverage is not None else None,
        "thresholds": {
            "format_valid_rate": FORMAT_VALID_THRESHOLD,
            "empty_rate": EMPTY_THRESHOLD,
            "holding_coverage": HOLDING_THRESHOLD,
        },
        "results": results[:20],  # first 20 for inspection
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[benchmark] format_valid_rate={format_valid_rate:.1%}  "
          f"empty_rate={empty_rate:.1%}  "
          f"holding_coverage={holding_coverage:.1%}" if holding_coverage else
          f"[benchmark] format_valid_rate={format_valid_rate:.1%}  empty_rate={empty_rate:.1%}")

    failed = []
    if format_valid_rate < FORMAT_VALID_THRESHOLD:
        failed.append(f"format_valid_rate {format_valid_rate:.1%} < {FORMAT_VALID_THRESHOLD:.0%}")
    if empty_rate > EMPTY_THRESHOLD:
        failed.append(f"empty_filename_rate {empty_rate:.1%} > {EMPTY_THRESHOLD:.0%}")

    if failed:
        print(f"[FAIL] {'; '.join(failed)}")
        sys.exit(1)
    else:
        print("[PASS] All thresholds met.")


if __name__ == "__main__":
    main()
