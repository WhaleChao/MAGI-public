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
  - error_rate           : % of accessible PDFs that raised unexpected runtime errors

Exit 1 if live-regression thresholds are missed.  The daily NAS sample is noisy
and can contain mixed OCR quality; the archived golden benchmark remains the
strict 100% gate for curated fixtures.
Writes results to .runtime/benchmark_pdf_namer_latest.json.
"""
import importlib.util
import json
import os
import sys
import time
import logging

logging.basicConfig(level=logging.WARNING)


def _warmup_vision_model(timeout_sec: int = 90) -> bool:
    """Pre-load the currently configured vision model before the benchmark.

    Day mode usually points at Phi-4 on port 8082.  Night mode may only keep
    the 26B text model alive on port 8080, so this must honor the configured
    MAGI_OMLX_VISION_URL instead of assuming 8082.

    Returns True if the configured endpoint is responsive, False otherwise.
    """
    import requests

    default_base = os.environ.get("MAGI_OMLX_CHAT_URL") or os.environ.get("OMLX_URL") or "http://127.0.0.1:8080"
    configured_base = os.environ.get("MAGI_OMLX_VISION_URL", default_base).rstrip("/")
    vision_bases = list(dict.fromkeys((configured_base, default_base.rstrip("/"))))
    preferred_model = (
        os.environ.get("MAGI_OMLX_VISION_MODEL")
        or os.environ.get("MAGI_OMLX_CHAT_MODEL")
        or os.environ.get("OMLX_MODEL")
        or "gemma-4-26b-a4b-it-4bit"
    )

    deadline = time.time() + timeout_sec
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        for vision_base in vision_bases:
            models_url = f"{vision_base}/v1/models"
            chat_url = f"{vision_base}/v1/chat/completions"
            try:
                r = requests.get(models_url, timeout=5)
                if r.status_code != 200:
                    continue
                payload = r.json()
                available_models = [
                    str(item.get("id") or "").strip()
                    for item in (payload.get("data") or [])
                    if isinstance(item, dict) and str(item.get("id") or "").strip()
                ] if isinstance(payload, dict) else []
                if not available_models:
                    continue
                vision_model = preferred_model if preferred_model in available_models else available_models[0]

                # Fire a minimal chat to trigger model load.  The model list only
                # proves that the endpoint is reachable; warmup succeeds only when
                # inference itself returns a 2xx response.
                try:
                    warmup = requests.post(
                        chat_url,
                        json={
                            "model": vision_model,
                            "messages": [{"role": "user", "content": "ping"}],
                            "max_tokens": 5,
                        },
                        timeout=min(60, max(5, int(deadline - time.time()))),
                    )
                    if 200 <= warmup.status_code < 300:
                        print(
                            f"[warmup] vision endpoint {vision_base} model={vision_model} "
                            f"ready after attempt {attempt}"
                        )
                        return True
                    if attempt == 1:
                        print(
                            f"[warmup] {vision_base} inference returned HTTP "
                            f"{warmup.status_code}; waiting...",
                            file=sys.stderr,
                        )
                except Exception as exc:
                    if attempt == 1:
                        print(
                            f"[warmup] {vision_base} inference not ready "
                            f"({exc.__class__.__name__}), waiting...",
                            file=sys.stderr,
                        )
            except Exception as exc:
                if attempt == 1:
                    print(
                        f"[warmup] vision endpoint {vision_base} not yet available "
                        f"({exc.__class__.__name__}), waiting...",
                        file=sys.stderr,
                    )
        time.sleep(5)
    print(
        f"[warmup] vision endpoints {', '.join(vision_bases)} still not ready after "
        f"{timeout_sec}s — benchmark may have degraded results",
        file=sys.stderr,
    )
    return False

MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, MAGI_ROOT)

NAS_CASE_ROOT = os.environ.get("MAGI_BENCHMARK_NAS_CASE_ROOT", "").strip()
FALLBACK_ROOT = os.path.expanduser("~/Library/CloudStorage/SynologyDrive-homes/01_案件")
FALLBACK_ROOTS = [
    os.path.expanduser("~/SynologyDrive/01_案件"),
    os.path.expanduser("~/SynologyDrive/homes/01_案件"),
    FALLBACK_ROOT,
]
MAX_PDFS = int(os.environ.get("MAGI_PDF_NAMER_BENCHMARK_MAX_PDFS", "100"))
ALLOW_NAS_SCAN = os.environ.get("MAGI_BENCHMARK_ALLOW_NAS_SCAN", "").strip().lower() in {
    "1", "true", "yes", "on",
}
MAX_SCAN_DIRS = int(os.environ.get("MAGI_BENCHMARK_MAX_SCAN_DIRS", "500"))
RUNTIME_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("MAGI_RUNTIME_DIR", "").strip())
    or os.path.join(MAGI_ROOT, ".runtime")
)
OUTPUT_PATH = os.path.join(RUNTIME_DIR, "benchmark_pdf_namer_latest.json")


def _load_certification_proposals():
    """Return fixture-owned model proposals for isolated body certification.

    The benchmark still runs the production scanner, validators, counters and
    threshold gate.  Only the model provider is deterministic, and the result
    explicitly records that provider quality was not certified.
    """
    raw_path = os.environ.get("MAGI_PDF_NAMER_BENCHMARK_FIXTURE_PROPOSALS", "").strip()
    if not raw_path:
        return None
    fixture_root_raw = os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT", "").strip()
    if (
        os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") != "real_entrypoint_fixture_v1"
        or os.environ.get("MAGI_V3_SCHEDULE_DRY_RUN") != "1"
        or not fixture_root_raw
    ):
        raise RuntimeError("pdf-namer certification proposals are not safely bound")
    from pathlib import Path

    fixture_root = Path(fixture_root_raw).expanduser().resolve()
    proposal_path = Path(raw_path).expanduser().resolve()
    if (
        not (fixture_root / ".magi-v3-schedule-fixture").is_file()
        or not proposal_path.is_file()
        or not proposal_path.is_relative_to(fixture_root)
    ):
        raise RuntimeError("pdf-namer certification proposals escaped their owned root")
    try:
        payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("pdf-namer certification proposals are unreadable") from exc
    proposals = payload.get("proposals")
    if payload.get("schema") != "magi.v3.pdf-namer-proposals/v1" or not isinstance(proposals, dict):
        raise RuntimeError("pdf-namer certification proposals schema is invalid")
    if not proposals or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in proposals.items()):
        raise RuntimeError("pdf-namer certification proposals are invalid")
    return proposals


def _threshold_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning("Invalid %s=%r; using %.2f", name, raw, default)
        return default
    return max(0.0, min(1.0, value))


FORMAT_VALID_THRESHOLD = _threshold_from_env("MAGI_PDF_NAMER_FORMAT_THRESHOLD", 0.99)
QUALITY_PASS_THRESHOLD = _threshold_from_env("MAGI_PDF_NAMER_QUALITY_THRESHOLD", 0.99)
OVERALL_PASS_THRESHOLD = _threshold_from_env("MAGI_PDF_NAMER_OVERALL_THRESHOLD", 0.99)
EMPTY_THRESHOLD = _threshold_from_env("MAGI_PDF_NAMER_EMPTY_THRESHOLD", 0.01)
ERROR_THRESHOLD = _threshold_from_env("MAGI_PDF_NAMER_ERROR_THRESHOLD", 0.01)
HOLDING_THRESHOLD = _threshold_from_env("MAGI_PDF_NAMER_HOLDING_THRESHOLD", 0.50)


def find_pdfs(root: str, limit: int = MAX_PDFS):
    """Scan NAS for PDF files with depth limit."""
    pdfs = []
    visited_dirs = 0
    try:
        for dirpath, dirnames, files in os.walk(root):
            visited_dirs += 1
            if visited_dirs > MAX_SCAN_DIRS:
                dirnames.clear()
                break
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


def _select_case_root() -> str:
    candidates = [*FALLBACK_ROOTS]
    if ALLOW_NAS_SCAN:
        candidates.append(NAS_CASE_ROOT)
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return ""


def _collect_threshold_failures(
    format_valid_rate: float,
    quality_pass_rate: float,
    overall_pass_rate: float,
    empty_rate: float,
    error_rate: float = 0.0,
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
    if error_rate > ERROR_THRESHOLD:
        failed.append(f"error_rate {error_rate:.1%} > {ERROR_THRESHOLD:.0%}")
    return failed


def _failure_results(results, limit: int = 50):
    """Retain actionable failures even when they occur after the sample cap."""
    return [
        row
        for row in results
        if row.get("valid") is False
        or row.get("quality_ok") is False
        or row.get("format_ok") is False
        or row.get("runtime_error") is True
    ][: max(1, int(limit))]


def main():
    case_root = _select_case_root()
    if not case_root:
        print("[SKIP] NAS/Synology case roots not available. Skipping benchmark.")
        sys.exit(0)

    fixture_proposals = _load_certification_proposals()

    # Pre-warm the configured vision endpoint before hitting it with many PDFs.
    # If the model is cold-starting, the first real request can timeout,
    # causing every PDF to fail → format_valid_rate=0% → spurious FAIL.
    if fixture_proposals is None:
        _warmup_vision_model(timeout_sec=90)

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
    error_count = 0
    holding_applicable = 0
    holding_found = 0
    quality_issue_counts = {}
    results = []

    print(f"[benchmark] Running pdf-namer on {total} PDFs...")
    inaccessible_count = 0
    for pdf_path in pdfs:
        try:
            if fixture_proposals is None:
                r = namer.generate_name_proposal(pdf_path, return_structured=True)
            else:
                r = fixture_proposals.get(os.path.basename(pdf_path))
                if r is None:
                    raise RuntimeError("certification provider has no proposal for fixture PDF")
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
            err_str = str(e)
            if "Failed to open file" in err_str or "no such file" in err_str.lower():
                inaccessible_count += 1
                results.append({"path": pdf_path, "error": err_str, "inaccessible": True})
            else:
                error_count += 1
                results.append({"path": pdf_path, "error": err_str, "runtime_error": True, "valid": False})

    effective_total = total - inaccessible_count
    if effective_total <= 0:
        print(f"[SKIP] All {total} PDFs were inaccessible (likely Synology offline placeholders). Skipping benchmark.")
        sys.exit(0)
    format_valid_rate = valid_format / effective_total if effective_total else 0.0
    quality_pass_rate = quality_pass / effective_total if effective_total else 0.0
    overall_pass_rate = overall_pass / effective_total if effective_total else 0.0
    empty_rate = empty_count / effective_total if effective_total else 0.0
    error_rate = error_count / effective_total if effective_total else 0.0
    holding_coverage = holding_found / holding_applicable if holding_applicable else None
    if inaccessible_count:
        print(f"[benchmark] {inaccessible_count}/{total} PDFs were inaccessible (excluded from rates)")

    summary = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": total,
        "inaccessible_count": inaccessible_count,
        "effective_total": effective_total,
        "format_valid_rate": round(format_valid_rate, 3),
        "quality_pass_rate": round(quality_pass_rate, 3),
        "overall_pass_rate": round(overall_pass_rate, 3),
        "empty_filename_rate": round(empty_rate, 3),
        "runtime_error_count": error_count,
        "error_rate": round(error_rate, 3),
        "holding_coverage": round(holding_coverage, 3) if holding_coverage is not None else None,
        "rules_source": rules_status.get("source", "unavailable"),
        "rules_degraded": bool(rules_status.get("degraded", True)),
        "rules_reason": rules_status.get("reason", ""),
        "rules_count": int(rules_status.get("rules_count", 0) or 0),
        "provider_quality_certified": fixture_proposals is None,
        "provider_role": (
            "live_pdf_namer_model"
            if fixture_proposals is None
            else "deterministic_pdf_namer_proposal_fixture"
        ),
        "quality_issue_counts": quality_issue_counts,
        "thresholds": {
            "format_valid_rate": FORMAT_VALID_THRESHOLD,
            "quality_pass_rate": QUALITY_PASS_THRESHOLD,
            "overall_pass_rate": OVERALL_PASS_THRESHOLD,
            "empty_rate": EMPTY_THRESHOLD,
            "error_rate": ERROR_THRESHOLD,
            "holding_coverage": HOLDING_THRESHOLD,
        },
        "ok": True,
        "results": results[:20],  # first 20 for inspection
        "failure_results": _failure_results(results),
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    benchmark_line = (
        f"[benchmark] format_valid_rate={format_valid_rate:.1%}  "
        f"quality_pass_rate={quality_pass_rate:.1%}  "
        f"overall_pass_rate={overall_pass_rate:.1%}  "
        f"empty_rate={empty_rate:.1%}  "
        f"error_rate={error_rate:.1%}"
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
        error_rate=error_rate,
    )
    if fixture_proposals is None and summary["rules_degraded"]:
        failed.append(
            "naming rules degraded: " + str(summary.get("rules_reason") or "unknown")
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
