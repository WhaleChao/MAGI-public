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
import json
import logging
import os
import random
import re
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

from api.case_path_mapper import default_case_roots, preferred_case_roots

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
        logging.FileHandler(
            os.path.join(SKILL_DIR, "_nightly_train.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("nightly-train")

_CASE_ROOTS = preferred_case_roots(include_closed=False)
_FALLBACK_CASE_ROOTS = default_case_roots(include_closed=False)
CASE_ROOT = os.environ.get(
    "MAGI_CASE_ROOT",
    _CASE_ROOTS[0] if _CASE_ROOTS else (_FALLBACK_CASE_ROOTS[0] if _FALLBACK_CASE_ROOTS else str(Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件")),
)
REPORT_PATH = os.path.join(SKILL_DIR, "_nightly_report.json")
DATE_PREFIX_RE = re.compile(r"^(20\d{6})")


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

    for root, dirs, files in os.walk(case_root):
        # Skip hidden and system dirs
        dirs[:] = [d for d in dirs if not d.startswith(".")]
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
    _prev_trust = os.environ.get("MAGI_PDF_NAMER_TRUST_PREFIX_FIRST")
    os.environ["MAGI_PDF_NAMER_TRUST_PREFIX_FIRST"] = "0"
    try:
        from action import task_analyze
        raw = task_analyze(pdf_path)
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e)}
    finally:
        if _prev_trust is None:
            os.environ.pop("MAGI_PDF_NAMER_TRUST_PREFIX_FIRST", None)
        else:
            os.environ["MAGI_PDF_NAMER_TRUST_PREFIX_FIRST"] = _prev_trust


def _validate_filename_format(filename: str) -> dict:
    """Validate that a filename follows the standard convention:
    {YYYYMMDD} {法院全名}{案號}{文件類型}（{當事人}）.pdf

    Returns dict with: valid (bool), issues (list of str)
    """
    issues = []
    if not filename:
        return {"valid": False, "issues": ["空檔名"]}
    bn = os.path.splitext(filename)[0]

    # Check date prefix
    if not re.match(r"^20\d{6}\s", bn):
        issues.append("缺少 YYYYMMDD 日期前綴")

    # Check space separator (not underscore)
    if "_" in bn[:12]:
        issues.append("日期後用底線而非空格分隔")

    # Check parentheses for party
    if "（" not in bn and "(" not in bn:
        issues.append("缺少當事人括號")

    # Check court name presence
    if "法院" not in bn and "法扶" not in bn:
        issues.append("缺少法院名稱")

    return {"valid": len(issues) == 0, "issues": issues}


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
    samples = collect_samples(max_files=max_files)
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

        results.append({
            "filename": sample["filename"],
            "label": label,
            "comparison": comp,
            "predicted_filename": predicted.get("suggested_filename"),
            "predicted_doc_type": predicted.get("doc_type"),
        })

    # Step 3: Compute metrics
    elapsed = (datetime.now() - started).total_seconds()
    date_acc = (date_correct / date_total * 100) if date_total else 0
    date_close_acc = ((date_correct + date_close) / date_total * 100) if date_total else 0
    party_acc = (party_correct / party_total * 100) if party_total else 0

    report = {
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

    # Step 5: Send Discord notification
    if not report_only:
        _notify_discord(report)

    # Save report
    try:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info("報告已存: %s", REPORT_PATH)
    except Exception as e:
        logger.warning("報告儲存失敗: %s", e)

    return report


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
    args = parser.parse_args()

    report = run_training(
        max_files=args.max_files,
        dry_run=args.dry_run,
        report_only=args.report_only,
    )

    # Print summary to stdout
    m = report.get("metrics", {})
    print(f"\n日期精確度: {m.get('date_accuracy_pct', 0)}%")
    print(f"當事人精確度: {m.get('party_accuracy_pct', 0)}%")
    print(f"收文章辨識: {m.get('stamp_verified_count', 0)}")
    print(f"不一致: {report.get('mismatches_count', 0)}")
