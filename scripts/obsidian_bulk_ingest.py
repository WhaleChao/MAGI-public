#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MAGI Obsidian 全案件批次匯入腳本

功能：
- 遍歷所有案件資料夾，逐案呼叫 ingest_source
- 支援斷點續跑（已完成的案件記錄在 progress file）
- 詳細 log 輸出，含進度百分比和 ETA
- 匯入完成後重建 FAISS index

用法：
  python3 scripts/obsidian_bulk_ingest.py [--dry-run] [--force] [--category 法扶案件]
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAGI_ROOT))

# 設定 CHUNK_CAP 為超大值，避免被截斷
os.environ["MAGI_OBSIDIAN_CHUNK_CAP"] = "999999"

from skills.obsidian.action import (
    task_ingest_source,
    task_status,
    _get_vault_path,
    SOURCE_ROOTS,
)

# ── Config ────────────────────────────────────────────────────────
CASE_ROOT = SOURCE_ROOTS.get("案件")
PROGRESS_PATH = MAGI_ROOT / ".agent" / "obsidian_bulk_progress.json"
LOG_PATH = MAGI_ROOT / ".agent" / "obsidian_bulk_ingest.log"

SUPPORTED_EXTENSIONS = {".md", ".txt", ".text", ".log", ".csv", ".pdf", ".docx", ".pptx", ".xlsx"}

# 每案最多匯入檔案數（0 = 無限制）
PER_CASE_LIMIT = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("BulkIngest")


# ── Progress tracking ─────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        try:
            return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"completed": {}, "failed": {}, "started_at": "", "updated_at": ""}


def save_progress(progress: dict):
    progress["updated_at"] = datetime.now().isoformat()
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Discovery ─────────────────────────────────────────────────────

def discover_cases(case_root: Path, category_filter: str = "") -> list:
    """找到所有案件資料夾（depth=3: 案件類別/案由類別/案件名稱）"""
    cases = []
    if not case_root or not case_root.is_dir():
        return cases

    for category_dir in sorted(case_root.iterdir()):
        if not category_dir.is_dir() or category_dir.name.startswith("."):
            continue
        if category_filter and category_dir.name != category_filter:
            continue

        for type_dir in sorted(category_dir.iterdir()):
            if not type_dir.is_dir() or type_dir.name.startswith("."):
                continue

            for case_dir in sorted(type_dir.iterdir()):
                if not case_dir.is_dir() or case_dir.name.startswith("."):
                    continue

                # 計算此案件有多少可匯入檔案
                file_count = 0
                for f in case_dir.rglob("*"):
                    if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                        if not any(part.startswith(".") for part in f.relative_to(case_root).parts):
                            file_count += 1

                relpath = str(case_dir.relative_to(case_root))
                cases.append({
                    "relpath": relpath,
                    "name": case_dir.name,
                    "category": category_dir.name,
                    "type": type_dir.name,
                    "file_count": file_count,
                    "abs_path": str(case_dir),
                })

    return cases


# ── Main ──────────────────────────────────────────────────────────

def run_bulk_ingest(dry_run: bool = False, force: bool = False, category: str = ""):
    # 前置檢查
    vault = _get_vault_path()
    if not vault:
        log.error("Vault 未設定，請先執行 bootstrap_synology_vault.py")
        return

    if not CASE_ROOT or not CASE_ROOT.is_dir():
        log.error(f"案件根目錄不存在: {CASE_ROOT}")
        return

    status = task_status()
    log.info(f"Vault: {status.get('vault_path')} ({status.get('notes_on_disk')} notes on disk)")

    # 探索所有案件
    log.info(f"掃描案件目錄: {CASE_ROOT}")
    cases = discover_cases(CASE_ROOT, category_filter=category)
    total_files = sum(c["file_count"] for c in cases)
    log.info(f"發現 {len(cases)} 個案件，共 {total_files} 個可匯入檔案")

    if not cases:
        log.warning("沒有找到任何案件")
        return

    # 載入進度
    progress = load_progress()
    if not progress.get("started_at"):
        progress["started_at"] = datetime.now().isoformat()

    completed = progress.get("completed", {})
    pending = [c for c in cases if c["relpath"] not in completed or force]
    pending_files = sum(c["file_count"] for c in pending)

    log.info(f"待處理: {len(pending)}/{len(cases)} 案件，{pending_files} 檔案")
    if completed and not force:
        log.info(f"已完成: {len(completed)} 案件（斷點續跑）")

    if dry_run:
        log.info("=== DRY RUN 模式 — 不會實際匯入 ===")
        for i, c in enumerate(pending, 1):
            log.info(f"  [{i:3d}/{len(pending)}] {c['relpath']} ({c['file_count']} files)")
        log.info(f"預估總 chunks: ~{pending_files * 5} (以每檔平均 5 chunks 估算)")
        return

    # 開始匯入
    start_time = time.time()
    total_processed = 0
    total_chunks = 0
    total_errors = 0
    files_done = 0

    for i, case in enumerate(pending, 1):
        relpath = case["relpath"]
        file_count = case["file_count"]

        if file_count == 0:
            log.info(f"[{i:3d}/{len(pending)}] SKIP {relpath} (0 files)")
            completed[relpath] = {
                "status": "skipped",
                "reason": "no_files",
                "at": datetime.now().isoformat(),
            }
            progress["completed"] = completed
            save_progress(progress)
            continue

        # ETA 計算
        elapsed = time.time() - start_time
        if files_done > 0:
            rate = elapsed / files_done  # seconds per file
            remaining_files = pending_files - files_done
            eta_sec = rate * remaining_files
            eta_str = str(timedelta(seconds=int(eta_sec)))
        else:
            eta_str = "calculating..."

        log.info(
            f"[{i:3d}/{len(pending)}] 匯入 {relpath} "
            f"({file_count} files) | ETA: {eta_str}"
        )

        try:
            result = task_ingest_source(
                source="案件",
                subpath=relpath,
                limit=PER_CASE_LIMIT if PER_CASE_LIMIT > 0 else 99999,
                force=force,
            )

            processed = result.get("processed", 0)
            skipped = result.get("skipped", 0)
            errors = result.get("errors", 0)
            notes = result.get("notes_created", [])

            total_processed += processed
            total_errors += errors
            files_done += file_count

            # 估算 chunks（從 notes_created 推估）
            case_chunks = processed * 5  # 粗估

            log.info(
                f"         → processed={processed} skipped={skipped} "
                f"errors={errors} notes={len(notes)}"
            )

            if result.get("error_details"):
                for err in result["error_details"][:3]:
                    log.warning(f"         ⚠ {err.get('path', '?')}: {err.get('error', '?')}")

            completed[relpath] = {
                "status": "done",
                "processed": processed,
                "skipped": skipped,
                "errors": errors,
                "notes_count": len(notes),
                "at": datetime.now().isoformat(),
            }

        except Exception as e:
            log.error(f"         ✗ 例外: {e}")
            files_done += file_count
            total_errors += 1
            completed[relpath] = {
                "status": "error",
                "error": str(e)[:200],
                "at": datetime.now().isoformat(),
            }
            progress.setdefault("failed", {})[relpath] = str(e)[:200]

        # 每個案件都存一次進度（斷點續跑用）
        progress["completed"] = completed
        save_progress(progress)

    # 匯入完成 — 統計
    elapsed = time.time() - start_time
    elapsed_str = str(timedelta(seconds=int(elapsed)))

    log.info("=" * 60)
    log.info("批次匯入完成")
    log.info(f"  耗時: {elapsed_str}")
    log.info(f"  處理案件: {len(pending)}")
    log.info(f"  匯入檔案: {total_processed}")
    log.info(f"  錯誤數: {total_errors}")
    log.info(f"  進度檔: {PROGRESS_PATH}")
    log.info(f"  Log 檔: {LOG_PATH}")
    log.info("=" * 60)

    # 寫入最終統計
    progress["summary"] = {
        "total_cases": len(cases),
        "processed_cases": len(pending),
        "total_files_processed": total_processed,
        "total_errors": total_errors,
        "elapsed_seconds": int(elapsed),
        "finished_at": datetime.now().isoformat(),
    }
    save_progress(progress)

    # FAISS index 重建提示
    log.info("建議重建 FAISS index 以確保搜尋品質:")
    log.info("  python3 -c \"from skills.memory.mem_bridge import rebuild_faiss_index; rebuild_faiss_index()\"")


def main():
    parser = argparse.ArgumentParser(description="MAGI Obsidian 全案件批次匯入")
    parser.add_argument("--dry-run", action="store_true", help="只列出待處理案件，不實際匯入")
    parser.add_argument("--force", action="store_true", help="強制重新匯入已完成的案件")
    parser.add_argument("--category", type=str, default="", help="只處理特定類別 (法扶案件/一般案件/無償案件/指定辯護案件)")
    args = parser.parse_args()

    run_bulk_ingest(dry_run=args.dry_run, force=args.force, category=args.category)


if __name__ == "__main__":
    main()
