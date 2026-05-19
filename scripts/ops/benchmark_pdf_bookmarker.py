#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lightweight live benchmark for pdf-bookmarker quality."""

import importlib.util
import json
import os
import re
import sys
import time

MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_PATH = os.path.join(MAGI_ROOT, ".runtime", "benchmark_pdf_bookmarker_latest.json")
NAS_CASE_ROOT = "/Volumes/lumi/lumi/01_案件"
FALLBACK_ROOT = os.path.expanduser("~/Library/CloudStorage/SynologyDrive-homes/01_案件")
FALLBACK_ROOTS = [
    os.path.expanduser("~/SynologyDrive/01_案件"),
    os.path.expanduser("~/SynologyDrive/homes/01_案件"),
    FALLBACK_ROOT,
]
MAX_PDFS = int(os.environ.get("MAGI_PDF_BOOKMARKER_BENCHMARK_MAX_PDFS", "20") or "20")
ALLOW_NAS_SCAN = os.environ.get("MAGI_BENCHMARK_ALLOW_NAS_SCAN", "").strip().lower() in {
    "1", "true", "yes", "on",
}
MAX_SCAN_DIRS = int(os.environ.get("MAGI_BENCHMARK_MAX_SCAN_DIRS", "500"))
RECALL_THRESHOLD = 0.85
EMPTY_FAILURE_THRESHOLD = 0.10
LABEL_MATCH_THRESHOLD = 0.80
NEEDS_MANUAL_REVIEW_THRESHOLD = int(os.environ.get("MAGI_PDF_BOOKMARKER_REVIEW_THRESHOLD", "1") or "1")
_LEGACY_IMAGE_LABEL_RE = re.compile(r"^image\d{4,}$", re.IGNORECASE)
_SINGLE_DOC_FILENAME_HINTS = (
    "預付酬金領款單",
    "准予扶助證明書",
    "扶助律師接案通知書",
    "法律扶助申請書",
    "資力詢問表",
    "審查表",
    "案件概述單",
    "委任狀",
)
_LAF_FORM_PART_RE = re.compile(r"(?:^|[\s_-])2[ABC](?:\(|（|[\s_.-]|$)", re.IGNORECASE)


def _load_module(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_pdfs(root, limit=MAX_PDFS):
    pdfs = []
    visited_dirs = 0
    for dirpath, dirnames, files in os.walk(root):
        visited_dirs += 1
        if visited_dirs > MAX_SCAN_DIRS:
            dirnames[:] = []
            break
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


def _select_case_root():
    candidates = [*FALLBACK_ROOTS]
    if ALLOW_NAS_SCAN:
        candidates.append(NAS_CASE_ROOT)
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return ""


def _compute_recall_metrics(outcomes):
    """Compute recall/empty metrics while excluding legitimate single-doc no-boundary files."""
    bookmarkable_total = 0
    non_empty = 0
    empty_failures = 0
    legitimate_single_doc = 0
    needs_manual_review = 0

    for item in outcomes:
        toc_count = int(item.get("toc_count", 0) or 0)
        classification = item.get("classification") or ("bookmarkable" if toc_count > 0 else "empty_failure")

        if toc_count > 0:
            bookmarkable_total += 1
            non_empty += 1
            continue

        if classification == "legitimate_single_doc" or _looks_like_single_doc_form(item.get("pdf")):
            legitimate_single_doc += 1
        elif classification == "needs_manual_review":
            bookmarkable_total += 1
            needs_manual_review += 1
        else:
            bookmarkable_total += 1
            empty_failures += 1

    bookmark_recall = non_empty / bookmarkable_total if bookmarkable_total else 0.0
    empty_failure_rate = empty_failures / bookmarkable_total if bookmarkable_total else 0.0
    needs_manual_review_rate = needs_manual_review / bookmarkable_total if bookmarkable_total else 0.0
    return {
        "bookmarkable_total": bookmarkable_total,
        "non_empty": non_empty,
        "empty_failures": empty_failures,
        "legitimate_single_doc": legitimate_single_doc,
        "needs_manual_review": needs_manual_review,
        "bookmark_recall": bookmark_recall,
        "empty_failure_rate": empty_failure_rate,
        "needs_manual_review_rate": needs_manual_review_rate,
    }


def _is_legacy_image_label(label):
    return bool(_LEGACY_IMAGE_LABEL_RE.match(str(label or "").strip()))


def _looks_like_single_doc_form(pdf_path):
    name = os.path.basename(str(pdf_path or ""))
    return any(hint in name for hint in _SINGLE_DOC_FILENAME_HINTS) or bool(_LAF_FORM_PART_RE.search(name))


def _collect_legacy_cleanup_candidate(pdf_path, observed_toc, generated_toc, classification):
    legacy_labels = sorted({label for _, label, _ in (observed_toc or []) if _is_legacy_image_label(label)})
    if not legacy_labels:
        return None

    proposed_generated_toc = [
        {"page": page, "label": label}
        for _, label, page in (generated_toc or [])
    ]
    if proposed_generated_toc:
        action = "replace_with_generated_toc"
    elif classification == "legitimate_single_doc":
        action = "remove_legacy_then_mark_single_doc"
    else:
        action = "manual_review_before_cleanup"

    return {
        "pdf": pdf_path,
        "legacy_labels": legacy_labels,
        "proposed_generated_toc": proposed_generated_toc,
        "classification": classification,
        "recommended_action": action,
    }


def _build_legacy_cleanup_plan(candidates):
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_root = os.path.join(MAGI_ROOT, ".backup", "pdf_bookmarker_legacy", ts)
    return {
        "candidate_count": len(candidates),
        "backup_root": backup_root,
        "planner": {
            "weekend_backfill_dry_run_cmd": "./venv/bin/python scripts/weekend_bookmark_batch.py --dry-run --plan-limit 50",
            "weekend_backfill_plan_path": os.path.join(MAGI_ROOT, ".runtime", "bookmark_backfill_plan_latest.json"),
        },
        "apply_runbook": [
            "1) 先執行 benchmark dry-run，確認 candidates 與 proposed_generated_toc。",
            f"2) 逐檔備份原始 PDF 到 {backup_root}（保持原目錄結構）。",
            "3) 逐檔以 pdf-bookmarker 產生書籤（非 dry-run），只套用到 candidates。",
            "4) 驗證：書籤數>0、不得包含 image0000x、頁碼遞增，且可人工抽查 3 份。",
            "5) 若驗證失敗，從 backup 還原單檔，不做全量覆寫。",
        ],
        "candidates": candidates,
    }


def main():
    case_root = _select_case_root()
    if not case_root:
        print("[SKIP] NAS/Synology case roots not available. Skipping bookmark benchmark.")
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
    outcome_samples = []
    label_samples = []
    invalid_label_samples = []
    legacy_cleanup_candidates = []

    generated_valid_labels = 0
    generated_examined_labels = 0
    observed_valid_labels = 0
    observed_examined_labels = 0

    for pdf_path in pdfs:
        try:
            result = bookmarker.scan_and_bookmark(pdf_path, dry_run=True)
            toc = result.get("toc") or []
            generated_toc = result.get("generated_toc")
            if generated_toc is None:
                generated_toc = toc

            classification = result.get("classification") or ("bookmarkable" if toc else "empty_failure")
            reason = result.get("classification_reason") or ""
            message = result.get("message") or ""

            outcome_samples.append(
                {
                    "pdf": pdf_path,
                    "toc_count": len(toc),
                    "generated_toc_count": len(generated_toc),
                    "classification": classification,
                    "reason": reason,
                    "message": message,
                }
            )
            cleanup_candidate = _collect_legacy_cleanup_candidate(pdf_path, toc, generated_toc, classification)
            if cleanup_candidate:
                legacy_cleanup_candidates.append(cleanup_candidate)

            for source_name, entries in (("generated", generated_toc), ("observed", toc)):
                for _, label, page in entries:
                    ok, warns = validator.validate_bookmark(label)
                    if source_name == "generated":
                        generated_examined_labels += 1
                        if ok:
                            generated_valid_labels += 1
                    else:
                        observed_examined_labels += 1
                        if ok:
                            observed_valid_labels += 1

                    if len(label_samples) < 20 and source_name == "generated":
                        label_samples.append(
                            {"pdf": pdf_path, "page": page, "label": label, "valid": ok, "warns": warns}
                        )
                    if not ok and len(invalid_label_samples) < 20:
                        invalid_label_samples.append(
                            {
                                "source": source_name,
                                "pdf": pdf_path,
                                "page": page,
                                "label": label,
                                "warns": warns,
                            }
                        )
        except Exception as exc:
            outcome_samples.append(
                {
                    "pdf": pdf_path,
                    "toc_count": 0,
                    "generated_toc_count": 0,
                    "classification": "empty_failure",
                    "reason": "exception",
                    "message": str(exc),
                }
            )

    recall_metrics = _compute_recall_metrics(outcome_samples)
    bookmark_recall = recall_metrics["bookmark_recall"]
    empty_failure_rate = recall_metrics["empty_failure_rate"]
    overall_empty_rate = 1.0 - (recall_metrics["non_empty"] / total if total else 0.0)
    label_match_rate = (
        generated_valid_labels / generated_examined_labels if generated_examined_labels else 0.0
    )
    observed_label_match_rate = (
        observed_valid_labels / observed_examined_labels if observed_examined_labels else 0.0
    )

    recall_or_empty_failed = (
        bookmark_recall < RECALL_THRESHOLD and empty_failure_rate > EMPTY_FAILURE_THRESHOLD
    )
    label_failed = label_match_rate < LABEL_MATCH_THRESHOLD
    needs_review_failed = recall_metrics["needs_manual_review"] > NEEDS_MANUAL_REVIEW_THRESHOLD
    ok = not (recall_or_empty_failed or label_failed or needs_review_failed)

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ok": ok,
        "success": ok,
        "total_pdfs": total,
        "bookmarkable_pdfs": recall_metrics["bookmarkable_total"],
        "legitimate_single_doc_pdfs": recall_metrics["legitimate_single_doc"],
        "needs_manual_review_pdfs": recall_metrics["needs_manual_review"],
        "empty_failure_pdfs": recall_metrics["empty_failures"],
        "bookmark_recall": round(bookmark_recall, 3),
        "empty_failure_rate": round(empty_failure_rate, 3),
        "needs_manual_review_rate": round(recall_metrics["needs_manual_review_rate"], 3),
        "overall_empty_rate": round(overall_empty_rate, 3),
        "label_match_rate": round(label_match_rate, 3),
        "observed_label_match_rate": round(observed_label_match_rate, 3),
        "thresholds": {
            "bookmark_recall": RECALL_THRESHOLD,
            "empty_failure_rate": EMPTY_FAILURE_THRESHOLD,
            "label_match_rate": LABEL_MATCH_THRESHOLD,
            "needs_manual_review_pdfs": NEEDS_MANUAL_REVIEW_THRESHOLD,
        },
        "label_samples": label_samples,
        "invalid_label_samples": invalid_label_samples,
        "legacy_cleanup_plan": _build_legacy_cleanup_plan(legacy_cleanup_candidates),
        "file_outcomes": outcome_samples,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    print(
        "[benchmark] bookmark_recall={:.1%} empty_failure_rate={:.1%} "
        "needs_manual_review_rate={:.1%} overall_empty_rate={:.1%} "
        "label_match_rate={:.1%} (observed_all_labels={:.1%}) "
        "legitimate_single_doc={} needs_manual_review={} legacy_cleanup_candidates={}".format(
            bookmark_recall,
            empty_failure_rate,
            recall_metrics["needs_manual_review_rate"],
            overall_empty_rate,
            label_match_rate,
            observed_label_match_rate,
            recall_metrics["legitimate_single_doc"],
            recall_metrics["needs_manual_review"],
            len(legacy_cleanup_candidates),
        )
    )
    if not ok:
        print("[FAIL] bookmark benchmark below threshold.")
        return 1
    print("[PASS] bookmark benchmark thresholds met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
