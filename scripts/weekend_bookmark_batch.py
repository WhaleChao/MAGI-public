#!/usr/bin/env python3
"""
weekend_bookmark_batch.py — 週六批次自動書籤

掃描所有案件根目錄下的 06_閱卷資料，
對沒有（或書籤不足的）PDF 自動建立書籤目錄。
已有書籤的 PDF 會自動跳過。

排程：每週六 03:00（與週日蒸餾/resummary 錯開）
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

MAGI_ROOT = Path(__file__).resolve().parents[1]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("weekend-bookmark")

# ── 匯入 ──────────────────────────────────────────────────────────────────────
try:
    from api.case_path_mapper import preferred_case_roots
except ImportError:
    logger.error("Cannot import case_path_mapper — aborting")
    sys.exit(1)

try:
    from skills.pdf_bookmarker_action import batch_process  # type: ignore
except ImportError:
    # Direct import via importlib
    import importlib.util
    _bm_path = MAGI_ROOT / "skills" / "pdf-bookmarker" / "action.py"
    if not _bm_path.exists():
        logger.error(f"pdf-bookmarker not found at {_bm_path}")
        sys.exit(1)
    _spec = importlib.util.spec_from_file_location("pdf_bookmarker_action", str(_bm_path))
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    batch_process = _mod.batch_process

# ── 目標子目錄 ─────────────────────────────────────────────────────────────────
TARGET_SUBDIRS = ["06_閱卷資料"]


def find_target_folders(roots: list[str]) -> list[Path]:
    """Find all 06_閱卷資料 folders under case roots."""
    folders = []
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            logger.warning(f"Case root not mounted: {root}")
            continue
        # Walk 3 levels: root / case_type / case_name / 06_閱卷資料
        for case_type_dir in sorted(root_path.iterdir()):
            if not case_type_dir.is_dir() or case_type_dir.name.startswith("."):
                continue
            for case_dir in sorted(case_type_dir.iterdir()):
                if not case_dir.is_dir() or case_dir.name.startswith("."):
                    continue
                for sub in TARGET_SUBDIRS:
                    target = case_dir / sub
                    if target.is_dir():
                        folders.append(target)
                # Also check one level deeper (e.g. 法扶案件/刑事/case_name)
                for sub_case_dir in sorted(case_dir.iterdir()):
                    if not sub_case_dir.is_dir() or sub_case_dir.name.startswith("."):
                        continue
                    for sub in TARGET_SUBDIRS:
                        target = sub_case_dir / sub
                        if target.is_dir():
                            folders.append(target)
    return folders


def main():
    started = time.time()
    roots = preferred_case_roots(include_closed=False)
    logger.info(f"📑 Weekend Bookmark Batch — scanning {len(roots)} case roots")

    folders = find_target_folders(roots)
    logger.info(f"Found {len(folders)} target folders with 06_閱卷資料")

    if not folders:
        logger.info("No folders to process — done")
        return

    total_processed = 0
    total_bookmarks = 0
    total_skipped = 0
    total_errors = 0

    for i, folder in enumerate(folders, 1):
        case_name = folder.parent.name
        logger.info(f"[{i}/{len(folders)}] {case_name} / {folder.name}")
        try:
            result = batch_process(str(folder), recursive=True, dry_run=False)
            logger.info(f"  {result}")
            # Parse result for summary
            for line in result.splitlines():
                line = line.strip()
                if line.startswith("處理："):
                    import re
                    m = re.search(r"(\d+)\s*份.*?(\d+)\s*個書籤", line)
                    if m:
                        total_processed += int(m.group(1))
                        total_bookmarks += int(m.group(2))
                elif line.startswith("跳過："):
                    import re
                    m = re.search(r"(\d+)", line)
                    if m:
                        total_skipped += int(m.group(1))
                elif line.startswith("錯誤："):
                    import re
                    m = re.search(r"(\d+)", line)
                    if m:
                        total_errors += int(m.group(1))
        except Exception as e:
            logger.warning(f"  Error: {e}")
            total_errors += 1

    elapsed = time.time() - started
    summary = (
        f"📑 Weekend Bookmark Batch 完成\n"
        f"  資料夾：{len(folders)} 個\n"
        f"  處理：{total_processed} 份 PDF / {total_bookmarks} 個書籤\n"
        f"  跳過（已有書籤）：{total_skipped} 份\n"
        f"  錯誤：{total_errors} 筆\n"
        f"  耗時：{elapsed:.0f} 秒"
    )
    logger.info(summary)

    # Notify via red_phone if available
    try:
        from api.red_phone import notify
        notify(summary, channel="system")
    except Exception:
        pass


if __name__ == "__main__":
    main()
