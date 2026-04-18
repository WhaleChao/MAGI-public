#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lightweight live benchmark for pdf-bookmarker quality."""

import importlib.util
import json
import os
import sys
import time

MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_PATH = os.path.join(MAGI_ROOT, ".runtime", "benchmark_pdf_bookmarker_latest.json")
NAS_CASE_ROOT = "/Volumes/lumi/lumi/01_案件"
FALLBACK_ROOT = os.path.expanduser("~/Library/CloudStorage/SynologyDrive-homes/01_案件")
MAX_PDFS = 20
RECALL_THRESHOLD = 0.80
EMPTY_RATE_THRESHOLD = 0.20


def _load_module(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_pdfs(root, limit=MAX_PDFS):
    pdfs = []
    for dirpath, dirnames, files in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= 5:
            dirnames[:] = []
            continue
        for name in files:
            if name.lower().endswith(".pdf") and not name.startswith("."):
                pdfs.append(os.path.join(dirpath, name))
                if len(pdfs) >= limit:
                    return pdfs
    return pdfs


def main():
    case_root = NAS_CASE_ROOT if os.path.isdir(NAS_CASE_ROOT) else FALLBACK_ROOT
    if not os.path.isdir(case_root):
        print("[SKIP] NAS not mounted. Skipping bookmark benchmark.")
        return 0

    validator = _load_module(
        "bookmark_validator",
        os.path.join(MAGI_ROOT, "skills", "pdf-bookmarker", "bookmark_validator.py"),
    )
    bookmarker = _load_module(
        "pdf_bookmarker_action",
        os.path.join(MAGI_ROOT, "skills", "pdf-bookmarker", "action.py"),
    )

    pdfs = find_pdfs(case_root)
    if not pdfs:
        print("[SKIP] No PDFs found. Skipping bookmark benchmark.")
        return 0

    total = len(pdfs)
    non_empty = 0
    valid_labels = 0
    examined_labels = 0
    samples = []

    for pdf_path in pdfs:
        try:
            result = bookmarker.scan_and_bookmark(pdf_path, dry_run=True)
            toc = result.get("toc") or []
            if toc:
                non_empty += 1
            for _, label, page in toc:
                examined_labels += 1
                ok, warns = validator.validate_bookmark(label)
                if ok:
                    valid_labels += 1
                if len(samples) < 20:
                    samples.append({"pdf": pdf_path, "page": page, "label": label, "valid": ok, "warns": warns})
        except Exception as exc:
            if len(samples) < 20:
                samples.append({"pdf": pdf_path, "error": str(exc)})

    bookmark_recall = non_empty / total if total else 0.0
    empty_rate = 1.0 - bookmark_recall
    label_match_rate = valid_labels / examined_labels if examined_labels else 0.0

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_pdfs": total,
        "bookmark_recall": round(bookmark_recall, 3),
        "empty_rate": round(empty_rate, 3),
        "label_match_rate": round(label_match_rate, 3),
        "thresholds": {
            "bookmark_recall": RECALL_THRESHOLD,
            "empty_rate": EMPTY_RATE_THRESHOLD,
        },
        "samples": samples,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    print(
        "[benchmark] bookmark_recall={:.1%} empty_rate={:.1%} label_match_rate={:.1%}".format(
            bookmark_recall, empty_rate, label_match_rate
        )
    )
    if bookmark_recall < RECALL_THRESHOLD or empty_rate > EMPTY_RATE_THRESHOLD:
        print("[FAIL] bookmark benchmark below threshold.")
        return 1
    print("[PASS] bookmark benchmark thresholds met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
