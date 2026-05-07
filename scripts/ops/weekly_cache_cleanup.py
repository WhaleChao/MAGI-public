#!/usr/bin/env python3
"""
週清 cache 腳本（A1，2026-04-25）

每週日 04:00 cron 觸發，清以下目錄中 atime > 14 天的條目（避免熱資料被誤清）：

- ~/.omlx-vision/cache/   (Vision OCR 結果 cache，無 LRU)
- ~/.cache/huggingface/hub/   (一次性試用模型，HF 沒有自動清理)

Summary 寫至 .runtime/metrics/weekly_cache_cleanup.jsonl。
出錯走 log_issue() 推 self_repair 主題。

紅線：
- ~/.omlx/models/  絕不清（模型本體）
- ~/.omlx/cache-e4b/ ~/.omlx/cache-phi4/ ~/.omlx/cache-smol/ 絕不清（推理熱 cache，由 Layer 4 上限管控）
- ~/.cache/whisper/  絕不清（轉錄主路徑）
- ~/.ollama/ 絕不清

使用：
    python3 scripts/ops/weekly_cache_cleanup.py            # 實際清
    python3 scripts/ops/weekly_cache_cleanup.py --dry-run  # 只列不清
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MAGI_ROOT))

# 紅線：絕不觸碰
_PROTECTED_PATHS = {
    Path.home() / ".omlx" / "models",
    Path.home() / ".omlx" / "cache-e4b",
    Path.home() / ".omlx" / "cache-phi4",
    Path.home() / ".omlx" / "cache-smol",
    Path.home() / ".cache" / "whisper",
    Path.home() / ".ollama",
}

# 預設清理目標（atime > N 天）
_TARGETS = [
    {
        "path": Path.home() / ".omlx-vision" / "cache",
        "atime_days": 14,
        "label": "omlx_vision_cache",
    },
    {
        "path": Path.home() / ".cache" / "huggingface" / "hub",
        "atime_days": 14,
        "label": "huggingface_hub",
    },
]


def _is_protected(p: Path) -> bool:
    p = p.resolve() if p.exists() else p
    for prot in _PROTECTED_PATHS:
        try:
            p.relative_to(prot)
            return True
        except ValueError:
            continue
        if p == prot:
            return True
    return False


def _dir_size_bytes(p: Path) -> int:
    total = 0
    try:
        for root, _, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _max_atime(p: Path) -> float:
    """取目錄內最大 atime（最近被讀的時間），用於判斷整體新舊。"""
    latest = 0.0
    try:
        for root, _, files in os.walk(p):
            for f in files:
                try:
                    a = os.path.getatime(os.path.join(root, f))
                    if a > latest:
                        latest = a
                except OSError:
                    continue
    except OSError:
        pass
    return latest


def cleanup_target(target: dict, dry_run: bool) -> dict:
    """清單一目標，回 summary。"""
    path: Path = target["path"]
    atime_days: int = target["atime_days"]
    label: str = target["label"]

    summary = {
        "label": label,
        "path": str(path),
        "atime_threshold_days": atime_days,
        "exists": path.exists(),
        "scanned_entries": 0,
        "deleted_entries": 0,
        "freed_bytes": 0,
        "skipped_protected": 0,
        "errors": [],
        "dry_run": dry_run,
    }

    if not path.exists() or not path.is_dir():
        return summary

    cutoff = time.time() - atime_days * 86400

    for entry in path.iterdir():
        summary["scanned_entries"] += 1
        try:
            if _is_protected(entry):
                summary["skipped_protected"] += 1
                continue

            # 看子目錄/檔案的最新 atime
            if entry.is_dir():
                latest = _max_atime(entry)
            else:
                latest = entry.stat().st_atime

            if latest == 0.0:
                # 空目錄或讀不到 → 視為舊
                latest = entry.stat().st_atime if entry.exists() else 0.0

            if latest >= cutoff:
                continue  # 14 天內被讀過，保留

            size = _dir_size_bytes(entry) if entry.is_dir() else entry.stat().st_size
            if dry_run:
                summary["deleted_entries"] += 1
                summary["freed_bytes"] += size
                continue

            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=False)
            else:
                entry.unlink()
            summary["deleted_entries"] += 1
            summary["freed_bytes"] += size
        except Exception as e:
            summary["errors"].append(f"{entry.name}: {type(e).__name__}: {e}")

    return summary


def write_metrics(summaries: list) -> None:
    """寫摘要至 .runtime/metrics/weekly_cache_cleanup.jsonl。"""
    try:
        from api.platforms.runtime_dir import metrics, atomic_append_jsonl
        path = metrics("ops") / "weekly_cache_cleanup.jsonl"
        record = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "summaries": summaries,
            "total_freed_bytes": sum(s["freed_bytes"] for s in summaries),
            "total_freed_gb": round(sum(s["freed_bytes"] for s in summaries) / 1024 / 1024 / 1024, 2),
        }
        atomic_append_jsonl(path, record, rotate_at=200, keep_tail=100)
    except Exception as e:
        print(f"[WARN] metrics write failed: {e}", file=sys.stderr)


def report_failure(err: Exception) -> None:
    """出錯時推 self_repair。"""
    try:
        from skills.management.issue_tracker import log_issue
        log_issue(
            command="cron:job_weekly_cache_cleanup",
            error_msg=f"{type(err).__name__}: {err}",
            severity="High",
            source="weekly_cache_cleanup",
        )
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只列不清")
    args = parser.parse_args()

    summaries = []
    overall_ok = True
    for target in _TARGETS:
        try:
            s = cleanup_target(target, dry_run=args.dry_run)
            summaries.append(s)
        except Exception as e:
            overall_ok = False
            report_failure(e)
            summaries.append({
                "label": target["label"],
                "path": str(target["path"]),
                "fatal_error": f"{type(e).__name__}: {e}",
                "dry_run": args.dry_run,
            })

    write_metrics(summaries)

    print(json.dumps({
        "success": overall_ok,
        "dry_run": args.dry_run,
        "summaries": summaries,
        "total_freed_gb": round(sum(s.get("freed_bytes", 0) for s in summaries) / 1024 / 1024 / 1024, 2),
    }, ensure_ascii=False, indent=2))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
