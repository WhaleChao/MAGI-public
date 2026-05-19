#!/usr/bin/env python3
"""
週清 cache 腳本（A2，2026-05-12）

每週日 04:00 cron 觸發，清退役 runtime 與可重建 cache。

- 退役 runtime：~/.ollama/（MAGI 已改由 oMLX/MLX-MTP 供應本機模型）
- 可重建 cache：oMLX cache、HF/Whisper/uv/pip/npm、Library/Caches、MAGI 專案 cache

Summary 寫至 .runtime/metrics/weekly_cache_cleanup.jsonl。
出錯走 log_issue() 推 self_repair 主題。

紅線：
- ~/.omlx/models/  絕不清（模型本體）
- ~/.omlx/models-vision/  絕不清（OCR/vision 模型本體）
- ~/.omlx/training/  絕不清（訓練成果）
- ~/.cache/judgment_collector/ 絕不清（司法院 raw backlog / process state，不是 disposable cache）
- MAGI DB、NAS、瀏覽器登入 profile 根目錄絕不整包清
- 單機版 JSON / pickle / db / sqlite 狀態檔絕不當一般 cache 清

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
    Path.home() / ".omlx" / "models-vision",
    Path.home() / ".omlx" / "training",
    Path.home() / ".cache" / "judgment_collector",
    MAGI_ROOT / "_db_backups",
    MAGI_ROOT / ".runtime" / "db_backups",
}

_PRESERVED_STANDALONE_SUFFIXES = {
    ".json",
    ".pickle",
    ".db",
    ".sqlite",
    ".sqlite3",
}

# 退役 root：整包移除。要暫停可設 MAGI_KEEP_RETIRED_OLLAMA=1。
_RETIRED_ROOT_TARGETS = [
    {
        "path": Path.home() / ".ollama",
        "label": "retired_ollama",
        "env_keep": "MAGI_KEEP_RETIRED_OLLAMA",
    },
    {
        "path": Path.home() / "Desktop" / ".openclaw_archived_20260412",
        "label": "retired_openclaw_archive",
        "env_keep": "MAGI_KEEP_RETIRED_OPENCLAW",
    },
    {
        "path": Path.home() / ".openclaw",
        "label": "retired_openclaw_home",
        "env_keep": "MAGI_KEEP_RETIRED_OPENCLAW",
    },
]

# 預設清理目標（atime > N 天）。只清目標底下的條目，不移除目標根目錄。
_TARGETS = [
    {
        "path": Path.home() / ".omlx-vision" / "cache",
        "atime_days": 14,
        "label": "omlx_vision_cache",
    },
    {
        "path": Path.home() / ".omlx" / "cache",
        "atime_days": 7,
        "label": "omlx_cache",
    },
    {
        "path": Path.home() / ".omlx" / "cache-e4b",
        "atime_days": 7,
        "label": "omlx_cache_e4b",
    },
    {
        "path": Path.home() / ".omlx" / "cache-26b",
        "atime_days": 7,
        "label": "omlx_cache_26b",
    },
    {
        "path": Path.home() / ".omlx" / "cache-phi4",
        "atime_days": 7,
        "label": "omlx_cache_phi4",
    },
    {
        "path": Path.home() / ".omlx" / "cache-smol",
        "atime_days": 7,
        "label": "omlx_cache_smol",
    },
    {
        "path": Path.home() / ".cache" / "huggingface" / "hub",
        "atime_days": 14,
        "label": "huggingface_hub",
    },
    {
        "path": Path.home() / ".cache" / "uv",
        "atime_days": 14,
        "label": "uv_cache",
    },
    {
        "path": Path.home() / ".cache" / "pip",
        "atime_days": 14,
        "label": "pip_cache",
    },
    {
        "path": Path.home() / ".cache" / "whisper",
        "atime_days": 30,
        "label": "whisper_model_cache",
    },
    {
        "path": Path.home() / ".npm" / "_cacache",
        "atime_days": 14,
        "label": "npm_cache",
    },
    {
        "path": Path.home() / "Library" / "Caches",
        "atime_days": 14,
        "label": "user_library_caches",
    },
    {
        "path": MAGI_ROOT / ".cache",
        "atime_days": 7,
        "label": "magi_project_cache",
    },
    {
        "path": MAGI_ROOT / "graphify-out" / "cache",
        "atime_days": 7,
        "label": "graphify_cache",
    },
    {
        "path": MAGI_ROOT / ".runtime" / "osc_draft_ocr_cache",
        "atime_days": 7,
        "label": "osc_draft_ocr_cache",
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


def _has_preserved_standalone_content(p: Path) -> bool:
    """保留單機版資源/狀態檔，避免把可攜版內容當一般 cache 清掉。"""
    try:
        if p.is_file():
            return p.suffix.lower() in _PRESERVED_STANDALONE_SUFFIXES
        if p.is_dir():
            for child in p.rglob("*"):
                if child.is_file() and child.suffix.lower() in _PRESERVED_STANDALONE_SUFFIXES:
                    return True
    except OSError:
        return True
    return False


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
        "skipped_permission": 0,
        "skipped_preserved_content": 0,
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
            if _has_preserved_standalone_content(entry):
                summary["skipped_preserved_content"] += 1
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
        except PermissionError:
            # macOS protects some Apple cache roots via TCC. They are normal
            # system-owned caches, not MAGI failures, so keep reports quiet.
            summary["skipped_permission"] += 1
        except Exception as e:
            summary["errors"].append(f"{entry.name}: {type(e).__name__}: {e}")

    return summary


def cleanup_retired_root(target: dict, dry_run: bool) -> dict:
    """清退役 runtime root；只允許明列於 _RETIRED_ROOT_TARGETS 的路徑。"""
    path: Path = target["path"]
    label: str = target["label"]
    env_keep = str(target.get("env_keep") or "")
    keep = bool(env_keep and os.environ.get(env_keep, "").strip().lower() in {"1", "true", "on", "yes"})
    summary = {
        "label": label,
        "path": str(path),
        "exists": path.exists(),
        "deleted_entries": 0,
        "freed_bytes": 0,
        "skipped_protected": 0,
        "skipped_by_env": keep,
        "errors": [],
        "dry_run": dry_run,
        "mode": "retired_root",
    }

    if keep or not path.exists():
        return summary
    if _is_protected(path):
        summary["skipped_protected"] = 1
        return summary
    try:
        size = _dir_size_bytes(path) if path.is_dir() else path.stat().st_size
        summary["freed_bytes"] = size
        summary["deleted_entries"] = 1
        if not dry_run:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=False)
            else:
                path.unlink()
    except Exception as e:
        summary["errors"].append(f"{path.name}: {type(e).__name__}: {e}")
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
    for target in _RETIRED_ROOT_TARGETS:
        try:
            s = cleanup_retired_root(target, dry_run=args.dry_run)
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
