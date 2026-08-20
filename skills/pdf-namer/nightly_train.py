#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf-namer / nightly_train.py
==============================
夜間批次訓練：掃描案件資料夾中的已歸檔 PDF，
用視覺解析重新分析並與現有檔名（=正確答案）比對，
藉此驗證 & 提升命名精準度與收文章辨識能力。

Usage:
    python3 nightly_train.py [--max-files N] [--dry-run] [--report-only]
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Setup paths ──
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
MAGI_ROOT = os.path.abspath(os.path.join(SKILL_DIR, "..", ".."))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from state_paths import prepare_write, read_path, state_path
from api.case_path_mapper import default_case_roots, preferred_case_roots
from skills.bridge.shared_utils.judgment_folder_names import JUDGMENT_FOLDER_LABEL

# Load .env
_env_path = os.path.join(MAGI_ROOT, ".env")
if os.path.exists(_env_path):
    for line in open(_env_path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("nightly-train")


def _enable_main_file_logging() -> Path:
    """Attach the nightly log only for CLI execution, never during import."""
    log_path = prepare_write(state_path("logs/nightly_train.log"))
    root_logger = logging.getLogger()
    resolved = log_path.resolve(strict=False)
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve(strict=False) == resolved:
                    return log_path
            except Exception:
                continue
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root_logger.addHandler(handler)
    return log_path

_CASE_ROOTS = preferred_case_roots(include_closed=False)
_FALLBACK_CASE_ROOTS = default_case_roots(include_closed=False)
CASE_ROOT = os.environ.get(
    "MAGI_CASE_ROOT",
    _CASE_ROOTS[0] if _CASE_ROOTS else (_FALLBACK_CASE_ROOTS[0] if _FALLBACK_CASE_ROOTS else str(Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件")),
)
REPORT_PATH = str(state_path("_nightly_report.json"))
DATE_PREFIX_RE = re.compile(r"^(20\d{6})")
_STRONG_SYNTHETIC_CASE_MARKERS = (
    "2026-9998",
    "測試消債",
    "magi-live-delete",
    "magi-csv-live-delete",
)
_CASE_FOLDER_SYNTHETIC_MARKERS = ("測試", "test", "dummy", "fake", "sample")
_CASE_FOLDER_RE = re.compile(r"^\d{4}-\d{4}(?:-|$)")
_TRANSIENT_STORAGE_ERRNOS = {5, 6, 19, 35, 60, 110}


# ── Helpers ──────────────────────────────────────────────────────────────

def _parse_existing_filename(fn: str) -> dict:
    """
    Parse a correctly-named PDF filename into components.
    e.g. '20250707 花蓮地方法院113年度原易字第179號刑事判決（余秋菊；主文：施用毒品罪）.pdf'
    """
    info: dict = {"raw": fn, "date": None, "doc_type_hint": None, "party_hint": None}
    bn = os.path.splitext(fn)[0]

    # Date prefix
    m = DATE_PREFIX_RE.match(bn)
    if m:
        info["date"] = m.group(1)
        bn = bn[8:].strip()

    # Party in parentheses
    paren_m = re.search(r"[（(]([^）)]+)[）)]", bn)
    if paren_m:
        inner = paren_m.group(1)
        party = inner.split("；")[0].split(";")[0].strip()
        if re.match(r"^[\u4e00-\u9fffA-Za-z·\-]{2,20}$", party):
            info["party_hint"] = party

    # Doc type: everything before the first parenthesis
    before_paren = re.split(r"[（(]", bn)[0].strip()
    if before_paren:
        info["doc_type_hint"] = before_paren

    return info


def _is_synthetic_case_path(path: str) -> bool:
    for part in [p for p in str(path or "").replace("\\", "/").split("/") if p]:
        lowered = part.lower()
        if any(marker in lowered for marker in _STRONG_SYNTHETIC_CASE_MARKERS):
            return True
        if _CASE_FOLDER_RE.match(part) and any(marker in lowered for marker in _CASE_FOLDER_SYNTHETIC_MARKERS):
            return True
    return False


def _is_transient_storage_error(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and int(getattr(exc, "errno", 0) or 0) in _TRANSIENT_STORAGE_ERRNOS


def _sha256_file(path: str, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a PDF without loading the whole case file into MAGI's memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_date(d: str) -> Optional[str]:
    """Ensure YYYYMMDD format."""
    if not d:
        return None
    d = str(d).strip().replace("-", "").replace("/", "").replace(".", "")
    if re.match(r"^20\d{6}$", d):
        return d
    return None


def _subfolder_label(subfolder: str) -> Optional[str]:
    """Map subfolder name to category label for validation."""
    mapping = {
        JUDGMENT_FOLDER_LABEL: "判決",
        "判決書": "判決",
        "法院通知或程序裁定": "法院通知",
        "我方歷次書狀": "書狀_我方",
        "對方歷次書狀": "書狀_對造",
        "證據資料": "證據",
        "閱卷資料": "閱卷",
        "信件往返": "信件",
        "委任資料": "契約",
        "收據": "收據",
    }
    # Handle numbered prefixes like "05_證據資料"
    clean = re.sub(r"^\d+_", "", subfolder)
    return mapping.get(clean) or mapping.get(subfolder)


# ── Main training loop ───────────────────────────────────────────────────

def collect_samples(
    case_root: str = CASE_ROOT,
    max_files: int = 200,
    shuffle: bool = True,
) -> List[dict]:
    """
    Collect PDF samples from case folders.
    Only picks files that already have a proper YYYYMMDD prefix (= ground truth).
    """
    samples = []
    if not os.path.isdir(case_root):
        logger.error("案件資料夾不存在: %s", case_root)
        return samples
    fixture_root: Path | None = None
    if os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") == "real_entrypoint_fixture_v1":
        fixture_raw = str(os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT") or "").strip()
        fixture_root = Path(fixture_raw).expanduser().resolve() if fixture_raw else None
        root = Path(case_root).expanduser().resolve()
        if (
            os.environ.get("MAGI_V3_SCHEDULE_DRY_RUN") != "1"
            or fixture_root is None
            or not (fixture_root / ".magi-v3-schedule-fixture").is_file()
            or not root.is_dir()
            or not root.is_relative_to(fixture_root)
        ):
            raise RuntimeError("pdf-namer nightly fixture case root is not safely bound")

    def _raise_transient_walk_error(exc: OSError) -> None:
        if _is_transient_storage_error(exc):
            raise exc
        logger.warning("略過無法讀取的資料夾: %s", type(exc).__name__)

    for root, dirs, files in os.walk(case_root, onerror=_raise_transient_walk_error):
        # Skip hidden and system dirs
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and (fixture_root is not None or not _is_synthetic_case_path(os.path.join(root, d)))
        ]
        if fixture_root is None and _is_synthetic_case_path(root):
            continue
        subfolder = os.path.basename(root)
        label = _subfolder_label(subfolder)
        if not label:
            continue
        for fn in files:
            if not fn.lower().endswith(".pdf") or fn.startswith("."):
                continue
            if not DATE_PREFIX_RE.match(fn):
                continue  # Only use properly named files as ground truth
            fp = os.path.join(root, fn)
            if fixture_root is not None:
                raw_target = Path(fp)
                target = raw_target.resolve()
                if raw_target.is_symlink() or not target.is_relative_to(fixture_root):
                    raise RuntimeError("pdf-namer nightly fixture PDF escaped its owned root")
            samples.append({
                "path": fp,
                "filename": fn,
                "subfolder": subfolder,
                "label": label,
                "ground_truth": _parse_existing_filename(fn),
            })

    if shuffle:
        random.shuffle(samples)
    return samples[:max_files]


def analyze_one(pdf_path: str) -> dict:
    """Run task_analyze on a single PDF and return parsed result.

    Training mode: disable fast-prefix shortcut so stamp OCR is always tested,
    giving meaningful stamp_verified metrics.
    """
    # Each sample runs in a bounded child so OCR/image libraries cannot retain
    # hundreds of megabytes across the full nightly batch.  This also keeps the
    # input method and interactive API responsive while training is active.
    env = os.environ.copy()
    env["MAGI_PDF_NAMER_TRUST_PREFIX_FIRST"] = "0"
    timeout = max(
        30,
        min(600, int(os.environ.get("MAGI_PDF_NAMER_TRAIN_SAMPLE_TIMEOUT_SEC", "180"))),
    )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                os.path.join(SKILL_DIR, "action.py"),
                "--task",
                "analyze",
                "--path",
                pdf_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "sample_timeout"}
    except Exception as exc:
        return {"error": f"sample_worker_failure:{type(exc).__name__}"}
    if completed.returncode != 0:
        return {"error": f"sample_worker_exit:{completed.returncode}"}
    try:
        parsed = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return {"error": "sample_worker_invalid_json"}
    return parsed if isinstance(parsed, dict) else {"error": "sample_worker_invalid_payload"}


def _validate_filename_format(filename: str) -> dict:
    """Validate that a filename follows the standard convention:
    {YYYYMMDD} {法院全名}{案號}{文件類型}（{當事人}）.pdf

    Returns dict with: valid (bool), issues (list of str)
    """
    if not filename:
        return {"valid": False, "issues": ["空檔名"]}
    try:
        from naming_validator import validate_filename
    except ImportError:
        return {"valid": False, "issues": ["命名驗證器無法載入"]}
    valid, issues = validate_filename(filename)
    return {"valid": bool(valid), "issues": list(issues)}


def compare_result(ground_truth: dict, predicted: dict) -> dict:
    """Compare prediction against ground truth filename."""
    gt_date = _normalize_date(ground_truth.get("date"))
    pred_date = _normalize_date(predicted.get("date"))

    date_match = (gt_date == pred_date) if (gt_date and pred_date) else None
    date_close = False
    if gt_date and pred_date and not date_match:
        try:
            gt_d = datetime.strptime(gt_date, "%Y%m%d")
            pr_d = datetime.strptime(pred_date, "%Y%m%d")
            date_close = abs((gt_d - pr_d).days) <= 3
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 234, exc_info=True)

    gt_party = ground_truth.get("party_hint") or ""
    pred_parties = predicted.get("parties") or []
    pred_party = pred_parties[0] if pred_parties else ""
    party_match = (gt_party == pred_party) if (gt_party and pred_party) else None

    stamp_verified = predicted.get("stamp_verified", False)
    date_method = predicted.get("date_method", "")
    db_template_used = predicted.get("db_template_used", False)

    # Validate filename format compliance
    pred_fn = predicted.get("suggested_filename") or ""
    fmt_check = _validate_filename_format(pred_fn)

    return {
        "date_match": date_match,
        "date_close": date_close,
        "party_match": party_match,
        "stamp_verified": stamp_verified,
        "date_method": date_method,
        "db_template_used": db_template_used,
        "confidence": predicted.get("confidence", 0.0),
        "gt_date": gt_date,
        "pred_date": pred_date,
        "gt_party": gt_party,
        "pred_party": pred_party,
        "format_valid": fmt_check["valid"],
        "format_issues": fmt_check["issues"],
    }


def run_training(
    max_files: int = 200,
    dry_run: bool = False,
    report_only: bool = False,
) -> dict:
    """
    Main nightly training loop.

    1. Collect samples from case folders (ground truth)
    2. Run task_analyze on each (predicted)
    3. Compare and compute accuracy metrics
    4. Optionally update learning rules
    """
    started = datetime.now()
    logger.info("=" * 60)
    logger.info("夜間訓練開始 %s (max_files=%d, dry_run=%s)",
                started.strftime("%Y-%m-%d %H:%M"), max_files, dry_run)

    # Step 1: Collect samples
    try:
        samples = collect_samples(max_files=max_files)
    except OSError as exc:
        if not _is_transient_storage_error(exc):
            raise
        return {
            "ok": False,
            "status": "deferred",
            "deferred": True,
            "reason": "storage_device_temporarily_unavailable",
            "started": started.isoformat(),
            "total_samples": 0,
            "analyzed": 0,
            "errors": 0,
        }
    logger.info("收集到 %d 個樣本", len(samples))
    if not samples:
        return {"error": "no_samples", "started": started.isoformat()}

    # Step 2: Analyze each sample
    results = []
    date_correct = 0
    date_close = 0
    date_total = 0
    party_correct = 0
    party_total = 0
    stamp_count = 0
    db_template_count = 0
    format_valid_count = 0
    format_issue_counter = Counter()
    method_counter = Counter()
    label_accuracy: Dict[str, Dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
    errors = 0
    storage_deferred = False

    for i, sample in enumerate(samples):
        if i > 0 and i % 20 == 0:
            logger.info("進度: %d/%d (日期準確率: %.1f%%)",
                        i, len(samples),
                        (date_correct / date_total * 100) if date_total else 0)

        try:
            predicted = analyze_one(sample["path"])
        except Exception as e:
            logger.warning("分析失敗 %s: %s", sample["filename"], e)
            errors += 1
            continue

        if predicted.get("error"):
            errors += 1
            continue

        comp = compare_result(sample["ground_truth"], predicted)

        # Accumulate stats
        if comp["date_match"] is not None:
            date_total += 1
            if comp["date_match"]:
                date_correct += 1
            elif comp["date_close"]:
                date_close += 1

        if comp["party_match"] is not None:
            party_total += 1
            if comp["party_match"]:
                party_correct += 1

        if comp["stamp_verified"]:
            stamp_count += 1
        if comp["db_template_used"]:
            db_template_count += 1
        if comp.get("format_valid"):
            format_valid_count += 1
        for issue in comp.get("format_issues", []):
            format_issue_counter[issue] += 1
        if comp["date_method"]:
            method_counter[comp["date_method"]] += 1

        # Per-label accuracy (based on date match as primary metric)
        label = sample["label"]
        label_accuracy[label]["total"] += 1
        if comp["date_match"]:
            label_accuracy[label]["correct"] += 1

        # The production analyzer does not always return a content hash.  Do
        # not reopen a remote NAS PDF merely to enrich a nightly quality
        # sample: a disconnected SMB volume can leave that read in an
        # uninterruptible kernel wait, outside the analyzer's per-sample
        # timeout.  The hash is optional evidence here; naming quality is
        # measured from the already bounded analyzer result.
        pdf_sha256 = predicted.get("pdf_sha256") or None

        results.append({
            "filename": sample["filename"],
            "label": label,
            "comparison": comp,
            "predicted_filename": predicted.get("suggested_filename"),
            "predicted_doc_type": predicted.get("doc_type"),
            "pdf_sha256": pdf_sha256,
            "pdf_sha256_available": bool(pdf_sha256),
            "parsed_page_count": predicted.get("parsed_page_count"),
            "parsed_text_sha256": predicted.get("parsed_text_sha256"),
            "provider_quality_certified": predicted.get("provider_quality_certified"),
            "provider_role": predicted.get("provider_role") or "live_pdf_namer_model",
        })

    # Step 3: Compute metrics
    elapsed = (datetime.now() - started).total_seconds()
    date_acc = (date_correct / date_total * 100) if date_total else 0
    date_close_acc = ((date_correct + date_close) / date_total * 100) if date_total else 0
    party_acc = (party_correct / party_total * 100) if party_total else 0

    report = {
        "ok": bool(results) and not storage_deferred,
        "status": "deferred" if storage_deferred else ("completed" if results else "failed"),
        "deferred": storage_deferred,
        "reason": "storage_device_temporarily_unavailable" if storage_deferred else "",
        "started": started.isoformat(),
        "elapsed_sec": round(elapsed, 1),
        "total_samples": len(samples),
        "analyzed": len(results),
        "errors": errors,
        "metrics": {
            "date_exact_match": date_correct,
            "date_close_match": date_close,
            "date_total": date_total,
            "date_accuracy_pct": round(date_acc, 1),
            "date_close_accuracy_pct": round(date_close_acc, 1),
            "party_correct": party_correct,
            "party_total": party_total,
            "party_accuracy_pct": round(party_acc, 1),
            "stamp_verified_count": stamp_count,
            "db_template_used_count": db_template_count,
            "format_valid_count": format_valid_count,
            "format_valid_pct": round((format_valid_count / len(results) * 100) if results else 0, 1),
            "format_issues": dict(format_issue_counter.most_common()),
        },
        "date_methods": dict(method_counter.most_common()),
        "sample_manifest": results,
        "provider_quality_certified": not any(
            item.get("provider_quality_certified") is False for item in results
        ),
        "provider_role": (
            "deterministic_pdf_naming_proposal_fixture"
            if any(item.get("provider_quality_certified") is False for item in results)
            else "live_pdf_namer_model"
        ),
        "per_label_accuracy": {
            label: {
                "correct": v["correct"],
                "total": v["total"],
                "accuracy_pct": round(v["correct"] / v["total"] * 100, 1) if v["total"] else 0,
            }
            for label, v in sorted(label_accuracy.items())
        },
    }

    # Mismatches for review
    mismatches = [
        r for r in results
        if r["comparison"].get("date_match") is False
        and not r["comparison"].get("date_close")
    ]
    report["mismatches_count"] = len(mismatches)
    report["mismatches_sample"] = mismatches[:20]  # Keep top 20 for review

    logger.info("=" * 60)
    logger.info("訓練完成！耗時 %.0f 秒", elapsed)
    logger.info("日期精確度: %d/%d (%.1f%%)", date_correct, date_total, date_acc)
    logger.info("日期近似度 (±3天): %d/%d (%.1f%%)",
                date_correct + date_close, date_total, date_close_acc)
    logger.info("當事人精確度: %d/%d (%.1f%%)", party_correct, party_total, party_acc)
    logger.info("收文章成功辨識: %d/%d", stamp_count, len(results))
    logger.info("DB 模板命名: %d/%d", db_template_count, len(results))
    logger.info("日期方法分布: %s", dict(method_counter.most_common(5)))
    logger.info("錯誤: %d", errors)
    logger.info("不一致樣本: %d", len(mismatches))

    for label, acc in sorted(report["per_label_accuracy"].items()):
        logger.info("  [%s] %d/%d = %.1f%%", label, acc["correct"], acc["total"], acc["accuracy_pct"])

    # Step 4: Update learning rules (if not dry run)
    if not dry_run and not report_only:
        try:
            from action import task_self_train
            train_res = json.loads(task_self_train())
            report["self_train"] = train_res
            logger.info("自我訓練完成: %d rules, %d samples",
                        train_res.get("rule_count", 0), train_res.get("sample_count", 0))
        except Exception as e:
            logger.warning("自我訓練失敗: %s", e)
            report["self_train_error"] = str(e)

        # Sync DB rules
        try:
            from training_loader import sync_db_to_training, sync_pending_learns
            sync_res = sync_db_to_training()
            report["db_sync"] = sync_res
            pending = sync_pending_learns()
            report["pending_learns_synced"] = pending
            logger.info("DB 同步: %s, pending=%d", sync_res, pending)
        except Exception as e:
            logger.warning("DB 同步失敗: %s", e)

        # Step 4b: Auto-adjust filing confidence threshold based on accuracy
        # Closed feedback loop: if accuracy improves → lower threshold (more auto-filing)
        # If accuracy drops → raise threshold (more manual review)
        try:
            _auto_adjust_filing_threshold(report)
        except Exception as e:
            logger.warning("門檻自動調整失敗: %s", e)

    # Step 5: Send Discord notification
    if not report_only:
        _notify_discord(report)

    # Step 4c: Log what nightly_train produces vs what naming pipeline actually uses
    # This helps diagnose the feedback loop disconnect
    report["feedback_loop_status"] = {
        "learned_rules_path": str(read_path("_learned_filename_rules.json")),
        "learned_rules_exist": read_path("_learned_filename_rules.json").exists(),
        "corrections_path": str(read_path("_corrections.json")),
        "corrections_exist": read_path("_corrections.json").exists(),
        "db_rules_cache_path": str(read_path("db_rules_cache.json")),
        "db_rules_cache_exist": read_path("db_rules_cache.json").exists(),
        "pipeline_uses_learned_rules": True,  # Connected in Phase 1A
        "pipeline_uses_db_templates": True,   # Connected in Phase 1B
    }

    # Save report
    try:
        with open(prepare_write(REPORT_PATH), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info("報告已存: %s", REPORT_PATH)
    except Exception as e:
        logger.warning("報告儲存失敗: %s", e)

    return report


_THRESHOLD_STATE_PATH = str(state_path("_threshold_state.json"))


def _auto_adjust_filing_threshold(report: dict):
    """Closed feedback loop: adjust FILING_CONFIDENCE_THRESHOLD based on nightly accuracy.

    Rules:
    - date_accuracy >= 80% AND party_accuracy >= 60%: lower threshold by 0.01 (min 0.78)
    - date_accuracy < 50% OR party_accuracy < 30%: raise threshold by 0.02 (max 0.92)
    - Otherwise: no change
    """
    metrics = report.get("metrics", {})
    date_acc = metrics.get("date_accuracy_pct", 0)
    party_acc = metrics.get("party_accuracy_pct", 0)
    total = metrics.get("date_total", 0)

    if total < 5:
        logger.info("門檻調整: 樣本數不足 (%d < 5), 跳過", total)
        return

    # Load current state
    state = {}
    threshold_read_path = read_path("_threshold_state.json")
    if threshold_read_path.exists():
        try:
            state = json.loads(threshold_read_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            pass

    current = float(state.get("threshold", 0.82))
    new_threshold = current

    if date_acc >= 80 and party_acc >= 60:
        new_threshold = max(0.78, current - 0.01)
        reason = f"accuracy good (date={date_acc}% party={party_acc}%): lower"
    elif date_acc < 50 or party_acc < 30:
        new_threshold = min(0.92, current + 0.02)
        reason = f"accuracy poor (date={date_acc}% party={party_acc}%): raise"
    else:
        reason = f"accuracy moderate (date={date_acc}% party={party_acc}%): hold"

    logger.info("門檻調整: %.2f → %.2f (%s)", current, new_threshold, reason)

    state["threshold"] = round(new_threshold, 3)
    state["last_adjusted"] = datetime.now().isoformat()
    state["reason"] = reason
    state["date_accuracy"] = date_acc
    state["party_accuracy"] = party_acc
    state["history"] = (state.get("history") or [])[-19:]  # Keep last 20
    state["history"].append({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "threshold": state["threshold"],
        "date_acc": date_acc,
        "party_acc": party_acc,
    })

    prepare_write(_THRESHOLD_STATE_PATH).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Also update smart_filer at runtime (if imported)
    try:
        import smart_filer
        smart_filer.FILING_CONFIDENCE_THRESHOLD = new_threshold
        logger.info("smart_filer.FILING_CONFIDENCE_THRESHOLD 已更新為 %.3f", new_threshold)
    except Exception:
        pass

    report["threshold_adjustment"] = {
        "previous": current,
        "new": new_threshold,
        "reason": reason,
    }


def _notify_discord(report: dict):
    """Send summary to Discord filescan webhook."""
    webhook_url = os.environ.get("MAGI_DISCORD_FILESCAN_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    try:
        import requests
        m = report.get("metrics", {})
        lines = [
            "## 🌙 PDF Namer 夜間訓練報告",
            f"**時間**: {report.get('started', '?')}",
            f"**樣本數**: {report.get('analyzed', 0)} / {report.get('total_samples', 0)}",
            f"**日期精確度**: {m.get('date_exact_match', 0)}/{m.get('date_total', 0)} "
            f"(**{m.get('date_accuracy_pct', 0)}%**)",
            f"**日期近似 (±3天)**: {m.get('date_close_accuracy_pct', 0)}%",
            f"**當事人精確度**: {m.get('party_correct', 0)}/{m.get('party_total', 0)} "
            f"({m.get('party_accuracy_pct', 0)}%)",
            f"**收文章辨識**: {m.get('stamp_verified_count', 0)}",
            f"**DB 模板命名**: {m.get('db_template_used_count', 0)}",
            f"**不一致**: {report.get('mismatches_count', 0)}",
            f"**耗時**: {report.get('elapsed_sec', 0)}s",
        ]
        # Per-label accuracy
        per_label = report.get("per_label_accuracy", {})
        if per_label:
            lines.append("\n**分類準確度**:")
            for label, acc in per_label.items():
                lines.append(f"- {label}: {acc['correct']}/{acc['total']} = {acc['accuracy_pct']}%")

        # Top mismatches
        mismatches = report.get("mismatches_sample", [])
        if mismatches:
            lines.append(f"\n**不一致樣本** (前 {min(5, len(mismatches))} 筆):")
            for mm in mismatches[:5]:
                c = mm.get("comparison", {})
                lines.append(
                    f"- `{mm['filename'][:50]}` "
                    f"正確={c.get('gt_date', '?')} 預測={c.get('pred_date', '?')} "
                    f"方法={c.get('date_method', '?')}"
                )

        body = "\n".join(lines)
        requests.post(webhook_url, json={"content": body[:1900]}, timeout=10)
    except Exception as e:
        logger.warning("Discord 通知失敗: %s", e)


# ── CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PDF Namer 夜間批次訓練")
    parser.add_argument("--max-files", type=int, default=200,
                        help="最多分析幾個 PDF (default: 200)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只分析不更新規則")
    parser.add_argument("--report-only", action="store_true",
                        help="只輸出報告，不更新規則、不發通知")
    parser.add_argument("--json-out", default="", help="另存完整 JSON 報告")
    args = parser.parse_args()
    _enable_main_file_logging()

    report = run_training(
        max_files=args.max_files,
        dry_run=args.dry_run,
        report_only=args.report_only,
    )
    if args.json_out:
        output = Path(args.json_out).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Print summary to stdout
    m = report.get("metrics", {})
    print(f"\n日期精確度: {m.get('date_accuracy_pct', 0)}%")
    print(f"當事人精確度: {m.get('party_accuracy_pct', 0)}%")
    print(f"收文章辨識: {m.get('stamp_verified_count', 0)}")
    print(f"不一致: {report.get('mismatches_count', 0)}")
    if os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") == "real_entrypoint_fixture_v1":
        if report.get("error") or int(report.get("analyzed") or 0) < 1 or int(report.get("errors") or 0) > 0:
            raise SystemExit(1)
    if report.get("status") == "deferred":
        raise SystemExit(75)
    if (
        report.get("error") or report.get("status") == "failed"
    ) and not (args.dry_run or args.report_only):
        raise SystemExit(1)
