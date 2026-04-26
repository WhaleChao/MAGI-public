#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark_pdf_namer.py
========================
Live benchmark for the pdf-namer skill.

Metrics:
  - format_valid_rate    : % of proposals passing naming_validator
  - quality_pass_rate    : % of proposals passing semantic quality checks
  - overall_pass_rate    : % passing both format + quality
  - holding_coverage     : % with non-empty holding field (for 判決/裁定)
  - empty_filename_rate  : % of proposals returning empty filename

Exit 1 if format_valid_rate < 70%, quality_pass_rate < 100%, overall_pass_rate < 70%,
or empty_filename_rate > 5%.
Writes results to .runtime/benchmark_pdf_namer_latest.json.
"""
import importlib.util
import json
import os
import sys
import time
import logging

logging.basicConfig(level=logging.WARNING)


def _warmup_phi4(timeout_sec: int = 90) -> bool:
    """Pre-load phi4 on port 8082 to avoid cold-start timeout in benchmark.

    phi4 (Melchior, MAGI-02) runs on port 8082 with loaded_count=0 after idle.
    First request triggers model load (~30-60s). This warmup fires a cheap
    dummy request before the real benchmark so the subsequent PDF-namer calls
    don't timeout waiting for model initialization.

    Returns True if phi4 is responsive, False if still unreachable after timeout.
    """
    import requests

    phi4_base = os.environ.get("MAGI_OMLX_VISION_URL", "http://127.0.0.1:8082").rstrip("/")
    models_url = f"{phi4_base}/v1/models"
    chat_url = f"{phi4_base}/v1/chat/completions"
    phi4_model = os.environ.get("MAGI_OMLX_VISION_MODEL", "Phi-4-mini-instruct-4bit")

    deadline = time.time() + timeout_sec
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = requests.get(models_url, timeout=5)
            if r.status_code == 200:
                # Port is up; fire a minimal chat to trigger model load if needed
                try:
                    requests.post(
                        chat_url,
                        json={
                            "model": phi4_model,
                            "messages": [{"role": "user", "content": "ping"}],
                            "max_tokens": 5,
                        },
                        timeout=min(60, max(5, int(deadline - time.time()))),
                    )
                except Exception:
                    pass  # timeout here is OK — model is loading; will be ready for real calls
                print(f"[warmup] phi4 responsive after attempt {attempt}")
                return True
        except Exception as exc:
            if attempt == 1:
                print(f"[warmup] phi4 not yet available ({exc.__class__.__name__}), waiting...",
                      file=sys.stderr)
        time.sleep(5)
    print(f"[warmup] phi4 still unreachable after {timeout_sec}s — benchmark may have degraded results",
          file=sys.stderr)
    return False

MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, MAGI_ROOT)

NAS_CASE_ROOT = "/Volumes/lumi/lumi/01_案件"
FALLBACK_ROOT = os.path.expanduser("~/Library/CloudStorage/SynologyDrive-homes/01_案件")
MAX_PDFS = int(os.environ.get("MAGI_PDF_NAMER_BENCHMARK_MAX_PDFS", "100"))
OUTPUT_PATH = os.path.join(MAGI_ROOT, ".runtime", "benchmark_pdf_namer_latest.json")

FORMAT_VALID_THRESHOLD = 0.70
QUALITY_PASS_THRESHOLD = 1.00
OVERALL_PASS_THRESHOLD = 0.70
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


def _collect_threshold_failures(
    format_valid_rate: float,
    quality_pass_rate: float,
    overall_pass_rate: float,
    empty_rate: float,
):
    failed = []
    if format_valid_rate < FORMAT_VALID_THRESHOLD:
        failed.append(f"format_valid_rate {format_valid_rate:.1%} < {FORMAT_VALID_THRESHOLD:.0%}")
    if quality_pass_rate < QUALITY_PASS_THRESHOLD:
        failed.append(f"quality_pass_rate {quality_pass_rate:.1%} < {QUALITY_PASS_THRESHOLD:.0%}")
    if overall_pass_rate < OVERALL_PASS_THRESHOLD:
        failed.append(f"overall_pass_rate {overall_pass_rate:.1%} < {OVERALL_PASS_THRESHOLD:.0%}")
    if empty_rate > EMPTY_THRESHOLD:
        failed.append(f"empty_filename_rate {empty_rate:.1%} > {EMPTY_THRESHOLD:.0%}")
    return failed


def main():
    case_root = NAS_CASE_ROOT if os.path.isdir(NAS_CASE_ROOT) else FALLBACK_ROOT
    if not os.path.isdir(case_root):
        print(f"[SKIP] NAS not mounted at {case_root}. Skipping benchmark.")
        sys.exit(0)

    # Pre-warm phi4 (port 8082 / OMLX_VISION_BASE) before hitting it with 100 PDFs.
    # If phi4 is cold-starting (loaded_count=0), the first real request can timeout
    # causing every PDF to fail → format_valid_rate=0% → spurious FAIL.
    _warmup_phi4(timeout_sec=90)

    try:
        sys.path.insert(0, os.path.join(MAGI_ROOT, "skills", "pdf-namer"))
        from naming_validator import validate_filename, validate_filename_quality
        import action as namer
        import training_loader
    except ImportError:
        validator_spec = importlib.util.spec_from_file_location(
            "pdf_namer_validator",
            os.path.join(MAGI_ROOT, "skills", "pdf-namer", "naming_validator.py"),
        )
        validator_mod = importlib.util.module_from_spec(validator_spec)
        validator_spec.loader.exec_module(validator_mod)
        validate_filename = validator_mod.validate_filename
        validate_filename_quality = validator_mod.validate_filename_quality

        namer_spec = importlib.util.spec_from_file_location(
            "pdf_namer_action",
            os.path.join(MAGI_ROOT, "skills", "pdf-namer", "action.py"),
        )
        namer = importlib.util.module_from_spec(namer_spec)
        namer_spec.loader.exec_module(namer)
        loader_spec = importlib.util.spec_from_file_location(
            "pdf_namer_training_loader",
            os.path.join(MAGI_ROOT, "skills", "pdf-namer", "training_loader.py"),
        )
        training_loader = importlib.util.module_from_spec(loader_spec)
        loader_spec.loader.exec_module(training_loader)

    try:
        training_loader.load_doc_rules_from_db()
        rules_status = training_loader.get_doc_rules_status()
    except Exception as exc:
        rules_status = {
            "source": "unavailable",
            "degraded": True,
            "reason": f"rules_status_error:{type(exc).__name__}",
            "rules_count": 0,
        }

    pdfs = find_pdfs(case_root)
    if not pdfs:
        print("[SKIP] No PDFs found. Skipping benchmark.")
        sys.exit(0)

    total = len(pdfs)
    valid_format = 0
    quality_pass = 0
    overall_pass = 0
    empty_count = 0
    holding_applicable = 0
    holding_found = 0
    quality_issue_counts = {}
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
            source_hint = "\n".join(
                part for part in [pdf_path, os.path.basename(pdf_path), r.get("party", ""), r.get("doc_type", "")]
                if str(part or "").strip()
            )
            quality_ok, quality_issues, quality_details = validate_filename_quality(filename, source_hint=source_hint)
            if quality_ok:
                quality_pass += 1
            else:
                for issue in quality_issues:
                    quality_issue_counts[issue] = quality_issue_counts.get(issue, 0) + 1
            combined_ok = bool(ok and quality_ok)
            if combined_ok:
                overall_pass += 1

            doc_type = r.get("doc_type", "")
            if doc_type and any(t in doc_type for t in ("判決", "裁定")):
                holding_applicable += 1
                if r.get("holding"):
                    holding_found += 1

            results.append({
                "path": pdf_path,
                "filename": filename,
                "valid": combined_ok,
                "format_ok": ok,
                "quality_ok": quality_ok,
                "warns": warns,
                "quality_issues": quality_issues,
                "quality_issue_details": quality_details,
                "holding": r.get("holding", ""),
                "doc_type": doc_type,
            })
        except Exception as e:
            empty_count += 1
            results.append({"path": pdf_path, "error": str(e)})

    format_valid_rate = valid_format / total if total else 0.0
    quality_pass_rate = quality_pass / total if total else 0.0
    overall_pass_rate = overall_pass / total if total else 0.0
    empty_rate = empty_count / total if total else 0.0
    holding_coverage = holding_found / holding_applicable if holding_applicable else None

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": total,
        "format_valid_rate": round(format_valid_rate, 3),
        "quality_pass_rate": round(quality_pass_rate, 3),
        "overall_pass_rate": round(overall_pass_rate, 3),
        "empty_filename_rate": round(empty_rate, 3),
        "holding_coverage": round(holding_coverage, 3) if holding_coverage is not None else None,
        "rules_source": rules_status.get("source", "unavailable"),
        "rules_degraded": bool(rules_status.get("degraded", True)),
        "rules_reason": rules_status.get("reason", ""),
        "rules_count": int(rules_status.get("rules_count", 0) or 0),
        "quality_issue_counts": quality_issue_counts,
        "thresholds": {
            "format_valid_rate": FORMAT_VALID_THRESHOLD,
            "quality_pass_rate": QUALITY_PASS_THRESHOLD,
            "overall_pass_rate": OVERALL_PASS_THRESHOLD,
            "empty_rate": EMPTY_THRESHOLD,
            "holding_coverage": HOLDING_THRESHOLD,
        },
        "ok": True,
        "results": results[:20],  # first 20 for inspection
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    benchmark_line = (
        f"[benchmark] format_valid_rate={format_valid_rate:.1%}  "
        f"quality_pass_rate={quality_pass_rate:.1%}  "
        f"overall_pass_rate={overall_pass_rate:.1%}  "
        f"empty_rate={empty_rate:.1%}"
    )
    if holding_coverage is not None:
        benchmark_line += f"  holding_coverage={holding_coverage:.1%}"
    benchmark_line += (
        f"  rules_source={summary['rules_source']}"
        f"{' (degraded)' if summary['rules_degraded'] else ''}"
    )
    print(benchmark_line)

    failed = _collect_threshold_failures(
        format_valid_rate=format_valid_rate,
        quality_pass_rate=quality_pass_rate,
        overall_pass_rate=overall_pass_rate,
        empty_rate=empty_rate,
    )

    if failed:
        summary["ok"] = False
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"[FAIL] {'; '.join(failed)}")
        sys.exit(1)
    else:
        summary["ok"] = True
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print("[PASS] All thresholds met.")


if __name__ == "__main__":
    main()
