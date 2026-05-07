#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAGI Model Benchmark Suite

Runs regression tests for:
1. Taiwan legal text retrieval (embedding quality)
2. OCR / filing-stamp extraction accuracy
3. Summarization / translation quality

Usage:
    python3 benchmark_models.py --suite all
    python3 benchmark_models.py --suite legal_retrieval
    python3 benchmark_models.py --suite ocr
    python3 benchmark_models.py --suite summary_translation
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT", str(Path(__file__).resolve().parents[2])))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

RESULTS_DIR = MAGI_ROOT / "static" / "benchmark_results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ── Suite 1: Legal Text Retrieval (MODEL-2: nomic-embed-text evaluation) ──

LEGAL_RETRIEVAL_CASES = [
    {
        "query": "勞動基準法加班費計算",
        "expected_top3_contains": ["勞動基準法", "加班", "工資"],
        "description": "Basic labor law overtime query",
    },
    {
        "query": "車禍過失傷害刑事責任",
        "expected_top3_contains": ["過失傷害", "刑法", "車禍"],
        "description": "Criminal negligence traffic accident",
    },
    {
        "query": "存證信函寄送效力",
        "expected_top3_contains": ["存證信函", "送達", "效力"],
        "description": "Legal attestation letter validity",
    },
    {
        "query": "法律扶助申請資格",
        "expected_top3_contains": ["法律扶助", "資格", "申請"],
        "description": "Legal aid eligibility",
    },
    {
        "query": "租賃契約提前終止違約金",
        "expected_top3_contains": ["租賃", "終止", "違約"],
        "description": "Lease early termination penalty",
    },
]


def run_legal_retrieval_benchmark() -> Dict[str, Any]:
    """Test embedding retrieval quality on Traditional Chinese legal queries."""
    try:
        from skills.memory.mem_bridge import recall
    except ImportError:
        return {"suite": "legal_retrieval", "status": "skip", "reason": "mem_bridge not available"}

    results = []
    for case in LEGAL_RETRIEVAL_CASES:
        start = time.monotonic()
        try:
            hits = recall(case["query"], top_k=3)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            hit_texts = " ".join(str(h.get("content", "")) for h in hits)
            matched = sum(1 for kw in case["expected_top3_contains"] if kw in hit_texts)
            results.append({
                "query": case["query"],
                "description": case["description"],
                "hits": len(hits),
                "keywords_matched": matched,
                "keywords_total": len(case["expected_top3_contains"]),
                "latency_ms": elapsed_ms,
                "pass": matched >= 1,
            })
        except Exception as e:
            results.append({
                "query": case["query"],
                "description": case["description"],
                "error": str(e),
                "pass": False,
            })

    passed = sum(1 for r in results if r.get("pass"))
    return {
        "suite": "legal_retrieval",
        "status": "pass" if passed == len(results) else "partial",
        "passed": passed,
        "total": len(results),
        "avg_latency_ms": int(sum(r.get("latency_ms", 0) for r in results) / max(len(results), 1)),
        "cases": results,
    }


# ── Suite 2: OCR Accuracy (MODEL-1 partial: filing-stamp extraction) ──

OCR_TEST_CASES_DIR = MAGI_ROOT / "scripts" / "ops" / "benchmark_ocr_samples"


def run_ocr_benchmark() -> Dict[str, Any]:
    """Test OCR model accuracy on sample images."""
    if not OCR_TEST_CASES_DIR.is_dir():
        return {
            "suite": "ocr",
            "status": "skip",
            "reason": f"No test samples at {OCR_TEST_CASES_DIR}. Create directory with sample images and expected.json.",
        }

    expected_file = OCR_TEST_CASES_DIR / "expected.json"
    if not expected_file.exists():
        return {"suite": "ocr", "status": "skip", "reason": "No expected.json in OCR samples dir"}

    try:
        from skills.bridge.inference_gateway import InferenceGateway
        gw = InferenceGateway()
    except ImportError:
        return {"suite": "ocr", "status": "skip", "reason": "InferenceGateway not available"}

    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    results = []
    for entry in expected:
        img_path = OCR_TEST_CASES_DIR / entry["image"]
        if not img_path.exists():
            results.append({"image": entry["image"], "error": "file not found", "pass": False})
            continue

        start = time.monotonic()
        r = gw.vision(str(img_path), "請完整辨識這張圖片中的所有文字。", timeout=30, task_type="ocr")
        elapsed_ms = int((time.monotonic() - start) * 1000)

        text = str(r.get("analysis", ""))
        expected_texts = entry.get("expected_contains", [])
        matched = sum(1 for kw in expected_texts if kw in text)
        results.append({
            "image": entry["image"],
            "model": r.get("model", "unknown"),
            "keywords_matched": matched,
            "keywords_total": len(expected_texts),
            "latency_ms": elapsed_ms,
            "pass": matched >= len(expected_texts) * 0.5,
        })

    passed = sum(1 for r in results if r.get("pass"))
    return {
        "suite": "ocr",
        "status": "pass" if passed == len(results) else "partial",
        "passed": passed,
        "total": len(results),
        "cases": results,
    }


# ── Suite 3: Summarization / Translation Quality ──

SUMMARY_TRANSLATION_CASES = [
    {
        "text": (
            "The Supreme Court held that the lower court erred in its application of the statute of limitations. "
            "The plaintiff filed the complaint within the prescribed period, and the defendant's motion to dismiss "
            "should have been denied. The case is remanded for further proceedings consistent with this opinion."
        ),
        "task": "summary",
        "expected_contains": ["法院", "時效"],
        "description": "English legal text summary to Chinese",
    },
    {
        "text": "勞動基準法第二十四條規定，雇主延長勞工工作時間者，其延長工作時間之工資，依下列標準加給：一、延長工作時間在二小時以內者，按平日每小時工資額加給三分之一以上。",
        "task": "translate",
        "expected_contains": ["overtime", "wage", "employer"],
        "description": "Chinese labor law to English translation",
    },
]


def run_summary_translation_benchmark() -> Dict[str, Any]:
    """Test summarization and translation quality."""
    try:
        from skills.bridge.inference_gateway import InferenceGateway
        gw = InferenceGateway()
    except ImportError:
        return {"suite": "summary_translation", "status": "skip", "reason": "InferenceGateway not available"}

    results = []
    for case in SUMMARY_TRANSLATION_CASES:
        start = time.monotonic()
        try:
            if case["task"] == "summary":
                r = gw.summarize(case["text"], timeout=60)
            else:
                r = gw.chat(f"Translate to English:\n{case['text']}", timeout=60)
            elapsed_ms = int((time.monotonic() - start) * 1000)

            output = str(r.get("text", r.get("analysis", "")))
            matched = sum(1 for kw in case["expected_contains"] if kw.lower() in output.lower())
            results.append({
                "description": case["description"],
                "task": case["task"],
                "model": r.get("model", "unknown"),
                "keywords_matched": matched,
                "keywords_total": len(case["expected_contains"]),
                "latency_ms": elapsed_ms,
                "pass": matched >= 1,
            })
        except Exception as e:
            results.append({
                "description": case["description"],
                "task": case["task"],
                "error": str(e),
                "pass": False,
            })

    passed = sum(1 for r in results if r.get("pass"))
    return {
        "suite": "summary_translation",
        "status": "pass" if passed == len(results) else "partial",
        "passed": passed,
        "total": len(results),
        "cases": results,
    }


# ── Main ──

SUITES = {
    "legal_retrieval": run_legal_retrieval_benchmark,
    "ocr": run_ocr_benchmark,
    "summary_translation": run_summary_translation_benchmark,
}


def main():
    ap = argparse.ArgumentParser(description="MAGI Model Benchmark Suite")
    ap.add_argument("--suite", default="all", help="all | legal_retrieval | ocr | summary_translation")
    ap.add_argument("--json-out", default="", help="Write results to JSON file")
    args = ap.parse_args()

    suites_to_run = list(SUITES.keys()) if args.suite == "all" else [args.suite]
    all_results = {}

    for name in suites_to_run:
        fn = SUITES.get(name)
        if not fn:
            print(f"Unknown suite: {name}")
            continue
        print(f"Running {name}...")
        result = fn()
        all_results[name] = result
        status = result.get("status", "unknown")
        passed = result.get("passed", 0)
        total = result.get("total", 0)
        print(f"  {name}: {status} ({passed}/{total})")

    # Save results
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = args.json_out or str(RESULTS_DIR / f"benchmark_{ts}.json")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
