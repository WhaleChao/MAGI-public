#!/usr/bin/env python3
"""
weekend_bookmark_batch.py — 週六批次自動書籤（兩階段）

Stage 1: regex (pdf-bookmarker) 快速掃描，建立基本導覽書籤
Stage 2: vision (oMLX gemma-4) 逐頁補漏，修正 regex 辨識不到的頁面

已完成的 PDF 記錄在 state file 中，下次跑自動跳過。
首次全量可能需要 2-3 個週末，之後增量每週 ~1 小時。

排程：每週六 03:00
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[1]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("weekend-bookmark")

# ── State persistence ─────────────────────────────────────────────────────────
STATE_FILE = MAGI_ROOT / ".agent" / "bookmark_batch_state.json"

# ── Config ────────────────────────────────────────────────────────────────────
VISION_BUDGET_SECONDS = int(os.environ.get("BOOKMARK_VISION_BUDGET_SEC", "28800"))  # 8 hours default
VISION_PER_PAGE_TIMEOUT = 30  # seconds per vision call
TARGET_SUBDIRS = ["05_閱卷資料", "06_閱卷資料", "06_證據資料"]
BACKFILL_PLAN_PATH = MAGI_ROOT / ".runtime" / "bookmark_backfill_plan_latest.json"
SINGLE_DOC_FASTPATH_RE = re.compile(
    r"(?:判決|裁定|聲請書|申請書|申冤表格|調查報告|不起訴處分書|起訴書|答辯狀|陳報狀|抗告狀|上訴狀)"
)
MERGED_RECORD_HINT_RE = re.compile(
    r"(?:全案卷宗|調查卷|卷\d+|卷[一二三四五六七八九十]|DOC_|_OCR|P\d+|P\d+-\d+)"
)

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    from api.case_path_mapper import preferred_case_roots
except ImportError:
    logger.error("Cannot import case_path_mapper — aborting")
    sys.exit(1)


def _load_bookmarker():
    """Import pdf-bookmarker's scan_and_bookmark function."""
    bm_path = MAGI_ROOT / "skills" / "pdf-bookmarker" / "action.py"
    if not bm_path.exists():
        logger.error(f"pdf-bookmarker not found at {bm_path}")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("pdf_bookmarker_action", str(bm_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.scan_and_bookmark


def _load_vision_gateway():
    """Import InferenceGateway for vision calls."""
    try:
        from skills.bridge.inference_gateway import InferenceGateway
        return InferenceGateway()
    except Exception as e:
        logger.warning(f"Cannot load InferenceGateway: {e}")
        return None


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"completed": {}, "vision_done": {}, "last_run": None}


def _set_state_file(path: str | None) -> None:
    """Allow priority/example runs to keep their progress outside the global batch state."""
    if not path:
        return
    global STATE_FILE
    state_path = Path(path).expanduser()
    if not state_path.is_absolute():
        state_path = MAGI_ROOT / state_path
    STATE_FILE = state_path


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_loop_progress(state: dict, key: str, index: int, total: int) -> None:
    state["last_file"] = key
    state["last_index"] = index
    state["last_total"] = total
    _save_state(state)


# ── Path discovery ────────────────────────────────────────────────────────────

def find_all_pdfs(roots: list[str], target_subdirs: list[str] | None = None) -> list[Path]:
    """Find PDFs in target evidence/review folders under case roots."""
    target_subdirs = target_subdirs or TARGET_SUBDIRS
    pdfs = []
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            logger.warning(f"Case root not mounted: {root}")
            continue
        logger.info(f"Scanning root: {root}")
        for case_type_dir in sorted(root_path.iterdir()):
            if not case_type_dir.is_dir() or case_type_dir.name.startswith("."):
                continue
            for case_dir in sorted(case_type_dir.iterdir()):
                if not case_dir.is_dir() or case_dir.name.startswith("."):
                    continue
                _collect_pdfs_from(case_dir, pdfs, target_subdirs)
                # One level deeper (e.g. 法扶案件/刑事/case_name)
                for sub_dir in sorted(case_dir.iterdir()):
                    if not sub_dir.is_dir() or sub_dir.name.startswith("."):
                        continue
                    _collect_pdfs_from(sub_dir, pdfs, target_subdirs)
    return pdfs


def find_direct_pdfs(roots: list[str]) -> list[Path]:
    """Find PDFs directly below explicit roots for priority case/folder processing."""
    pdfs: list[Path] = []
    for root in roots:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            logger.warning(f"Explicit PDF root not mounted: {root}")
            continue
        logger.info(f"Scanning explicit PDF root: {root_path}")
        for pdf in sorted(root_path.rglob("*.pdf")):
            if not pdf.name.startswith("."):
                pdfs.append(pdf)
    return pdfs


def _collect_pdfs_from(case_dir: Path, out: list[Path], target_subdirs: list[str] | None = None):
    target_subdirs = target_subdirs or TARGET_SUBDIRS
    for sub in target_subdirs:
        target = case_dir / sub
        if not target.is_dir():
            continue
        for pdf in sorted(target.rglob("*.pdf")):
            if not pdf.name.startswith("."):
                out.append(pdf)


def _looks_like_obvious_single_doc(pdf: Path, page_count: int) -> bool:
    """Fast-path clearly single legal documents so large example batches focus on merged records."""
    max_pages = int(os.environ.get("BOOKMARK_SINGLE_DOC_FASTPATH_MAX_PAGES", "80"))
    if page_count > max_pages:
        return False
    text = str(pdf)
    name = pdf.name
    if MERGED_RECORD_HINT_RE.search(text):
        return False
    if page_count <= 3:
        return True
    return bool(SINGLE_DOC_FASTPATH_RE.search(name))


# ── oMLX management ──────────────────────────────────────────────────────────

def _stop_omlx():
    """Stop oMLX to free RAM for regex stage."""
    try:
        subprocess.run(["launchctl", "stop", "com.magi.omlx"], capture_output=True, timeout=30)
        subprocess.run(["pkill", "-f", "omlx.*--port.*8080"], capture_output=True, timeout=10)
        logger.info("⏸️ Stopped oMLX text inference")
        time.sleep(3)
    except Exception as e:
        logger.warning(f"Could not stop oMLX: {e}")


def _start_omlx():
    """Start oMLX for vision stage (or restore after completion)."""
    try:
        subprocess.run(["launchctl", "start", "com.magi.omlx"], capture_output=True, timeout=30)
        logger.info("▶️ Starting oMLX text inference...")
        # Wait for model to load
        for _ in range(30):
            time.sleep(5)
            try:
                import urllib.request
                port = os.environ.get("MAGI_OMLX_PORT", "8080")
                resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5)
                if resp.status == 200:
                    logger.info("✅ oMLX ready")
                    return True
            except Exception:
                pass
        logger.warning("oMLX did not become ready in 150s")
        return False
    except Exception as e:
        logger.warning(f"Could not start oMLX: {e}")
        return False


# ── Stage 1: Regex bookmarks ─────────────────────────────────────────────────

def stage1_regex(pdfs: list[Path], state: dict, scan_fn) -> dict:
    """Fast regex-based bookmark pass. Returns stats dict."""
    import fitz

    stats = {
        "processed": 0,
        "bookmarks": 0,
        "skipped": 0,
        "no_boundary": 0,
        "needs_ocr": 0,
        "deferred_large_ocr": 0,
        "errors": 0,
    }
    completed = state.setdefault("completed", {})
    stage1_errors = state.setdefault("stage1_errors", {})

    # Optional soft budget (seconds) — nightly caller sets this to ~1800 to bound wall-clock.
    _budget_raw = os.environ.get("BOOKMARK_REGEX_BUDGET_SEC", "").strip()
    try:
        budget_sec = int(_budget_raw) if _budget_raw else 0
    except ValueError:
        budget_sec = 0
    single_doc_fastpath = os.environ.get("BOOKMARK_SINGLE_DOC_FASTPATH", "").strip() == "1"
    skip_large_non_ocr = os.environ.get("BOOKMARK_SKIP_LARGE_NON_OCR", "").strip() == "1"
    try:
        large_non_ocr_pages = int(os.environ.get("BOOKMARK_LARGE_NON_OCR_MIN_PAGES", "100"))
    except ValueError:
        large_non_ocr_pages = 100
    try:
        defer_large_ocr_pages = int(os.environ.get("BOOKMARK_DEFER_LARGE_OCR_PAGES", "0"))
    except ValueError:
        defer_large_ocr_pages = 0
    stage_start = time.time()

    for i, pdf in enumerate(pdfs, 1):
        if budget_sec and (time.time() - stage_start) > budget_sec:
            logger.info(
                f"⏰ Stage 1 regex budget exhausted ({budget_sec}s) at {i}/{len(pdfs)}; remaining PDFs will be picked up next run"
            )
            break
        key = str(pdf)
        try:
            mtime = str(pdf.stat().st_mtime)
        except Exception:
            continue

        # Skip if already processed with same mtime
        prev = completed.get(key, {})
        if prev.get("mtime") == mtime and prev.get("stage1"):
            stats["skipped"] += 1
            stage1_errors.pop(key, None)
            _save_loop_progress(state, key, i, len(pdfs))
            continue

        # Skip if already has enough bookmarks
        try:
            doc = fitz.open(str(pdf))
            existing = doc.get_toc() or []
            page_count = doc.page_count
            doc.close()
            if len(existing) >= max(3, page_count // 15):
                completed[key] = {
                    "mtime": mtime, "stage1": True,
                    "stage1_bookmarks": len(existing),
                    "pages": page_count,
                    "processed_at": datetime.now().isoformat(),
                }
                stats["skipped"] += 1
                stage1_errors.pop(key, None)
                _save_loop_progress(state, key, i, len(pdfs))
                continue
        except Exception:
            stage1_errors[key] = {
                "mtime": mtime,
                "stage1": False,
                "message": "PDF 開啟或讀取既有書籤失敗",
                "processed_at": datetime.now().isoformat(),
            }
            stats["errors"] += 1
            _save_loop_progress(state, key, i, len(pdfs))
            continue

        if skip_large_non_ocr and page_count >= large_non_ocr_pages and "_OCR" not in pdf.stem.upper():
            completed[key] = {
                "mtime": mtime,
                "stage1": True,
                "stage1_bookmarks": 0,
                "pages": page_count,
                "needs_ocr": True,
                "message": "大型非 OCR PDF，專案模式先略過，需 OCR 後再編標籤",
                "processed_at": datetime.now().isoformat(),
            }
            stats["needs_ocr"] += 1
            stage1_errors.pop(key, None)
            _save_loop_progress(state, key, i, len(pdfs))
            continue

        if defer_large_ocr_pages and page_count >= defer_large_ocr_pages and "_OCR" in pdf.stem.upper():
            completed[key] = {
                "mtime": mtime,
                "stage1": True,
                "stage1_bookmarks": 0,
                "pages": page_count,
                "deferred_large_ocr": True,
                "message": f"大型 OCR PDF（{page_count} 頁）先排入分割/離峰重跑，避免專案批次被單檔阻塞",
                "processed_at": datetime.now().isoformat(),
            }
            stats["deferred_large_ocr"] += 1
            stage1_errors.pop(key, None)
            _save_loop_progress(state, key, i, len(pdfs))
            continue

        if single_doc_fastpath and _looks_like_obvious_single_doc(pdf, page_count):
            completed[key] = {
                "mtime": mtime,
                "stage1": True,
                "stage1_bookmarks": 0,
                "pages": page_count,
                "no_boundary": True,
                "classification": "legitimate_single_doc",
                "message": "明確單一文件，專案快徑略過逐頁編標籤",
                "processed_at": datetime.now().isoformat(),
            }
            stats["no_boundary"] += 1
            stage1_errors.pop(key, None)
            _save_loop_progress(state, key, i, len(pdfs))
            continue

        if i % 50 == 0:
            logger.info(f"  Stage 1 progress: {i}/{len(pdfs)}")

        try:
            result = scan_fn(str(pdf), output_path=None, dry_run=False)
            if result.get("success"):
                bm_count = result.get("bookmarks", 0)
                try:
                    stored_mtime = str(pdf.stat().st_mtime)
                except Exception:
                    stored_mtime = mtime
                stats["processed"] += 1
                stats["bookmarks"] += bm_count
                completed[key] = {
                    "mtime": stored_mtime, "stage1": True,
                    "stage1_bookmarks": bm_count,
                    "pages": page_count,
                    "processed_at": datetime.now().isoformat(),
                }
                stage1_errors.pop(key, None)
            elif _is_stage1_no_hit_result(result):
                message = str(result.get("message") or "")
                completed[key] = {
                    "mtime": mtime,
                    "stage1": True,
                    "stage1_bookmarks": 0,
                    "pages": page_count,
                    "no_boundary": True,
                    "message": message,
                    "processed_at": datetime.now().isoformat(),
                }
                stats["no_boundary"] += 1
                stage1_errors.pop(key, None)
            else:
                stage1_errors[key] = {
                    "mtime": mtime,
                    "stage1": False,
                    "pages": page_count,
                    "message": str((result or {}).get("message") if isinstance(result, dict) else result)[:500],
                    "processed_at": datetime.now().isoformat(),
                }
                stats["errors"] += 1
        except Exception as e:
            logger.debug(f"  Stage 1 error {pdf.name}: {e}")
            stage1_errors[key] = {
                "mtime": mtime,
                "stage1": False,
                "pages": page_count,
                "message": str(e)[:500],
                "processed_at": datetime.now().isoformat(),
            }
            stats["errors"] += 1

        _save_loop_progress(state, key, i, len(pdfs))

    _save_state(state)
    return stats


def _is_stage1_no_hit_result(result: Any) -> bool:
    """Classify expected regex misses (no boundary / empty toc) as non-errors."""
    if not isinstance(result, dict):
        return False
    if result.get("success") is True:
        return False

    msg = str(result.get("message") or "")
    if re.search(r"(未偵測到文件邊界|無法產生書籤|no\s*boundary|empty\s*toc|toc\s*empty)", msg, re.IGNORECASE):
        return True

    bookmarks = result.get("bookmarks")
    toc = result.get("toc")
    zero_bookmarks = bookmarks == 0
    toc_empty = toc is None or (isinstance(toc, list) and len(toc) == 0)
    return zero_bookmarks and toc_empty


def build_backfill_plan(pdfs: list[Path], state: dict, sample_limit: int = 20) -> dict:
    """Build a non-destructive backfill plan from current state + file mtimes."""
    completed = state.get("completed", {}) or {}
    vision_done = state.get("vision_done", {}) or {}

    stage1_pending = []
    stage1_done = 0
    no_boundary_backlog = []
    vision_pending = []
    stage1_skipped_with_bookmarks = 0

    for pdf in pdfs:
        key = str(pdf)
        info = completed.get(key, {}) or {}
        try:
            mtime = str(pdf.stat().st_mtime)
        except Exception:
            continue

        stage1_done_same_version = bool(info.get("stage1") and info.get("mtime") == mtime)
        if stage1_done_same_version:
            stage1_done += 1
            if int(info.get("stage1_bookmarks", 0) or 0) >= 3:
                stage1_skipped_with_bookmarks += 1
        else:
            stage1_pending.append(key)

        if info.get("no_boundary") and info.get("mtime") == mtime:
            no_boundary_backlog.append(key)

        if stage1_done_same_version and int(info.get("pages", 0) or 0) >= 5:
            vision_info = vision_done.get(key, {}) or {}
            if vision_info.get("mtime") != mtime:
                vision_pending.append(key)

    plan = {
        "generated_at": datetime.now().isoformat(),
        "total_pdfs": len(pdfs),
        "stage1_pending_count": len(stage1_pending),
        "stage1_done_count": stage1_done,
        "stage1_existing_bookmark_skip_estimate": stage1_skipped_with_bookmarks,
        "no_boundary_backlog_count": len(no_boundary_backlog),
        "vision_pending_count": len(vision_pending),
        "samples": {
            "stage1_pending": stage1_pending[:sample_limit],
            "no_boundary_backlog": no_boundary_backlog[:sample_limit],
            "vision_pending": vision_pending[:sample_limit],
        },
    }
    return plan


# ── Stage 2: Vision refinement ───────────────────────────────────────────────

def stage2_vision(pdfs: list[Path], state: dict, gw) -> dict:
    """Vision-based bookmark refinement for pages regex missed."""
    import fitz

    stats = {"pages_checked": 0, "bookmarks_added": 0, "files_refined": 0, "errors": 0}
    completed = state.get("completed", {})
    vision_done = state.setdefault("vision_done", {})
    budget_start = time.time()

    # Sort by fewest existing bookmarks first (most benefit from vision)
    candidates = []
    for pdf in pdfs:
        key = str(pdf)
        info = completed.get(key, {})
        if not info.get("stage1"):
            continue
        mtime = info.get("mtime", "")
        if vision_done.get(key, {}).get("mtime") == mtime:
            continue  # Already vision-processed with same mtime
        pages = info.get("pages", 0)
        bm_count = info.get("stage1_bookmarks", 0)
        if pages < 5:
            continue
        # Priority: fewer bookmarks relative to pages = more benefit
        ratio = bm_count / max(pages, 1)
        candidates.append((ratio, pdf, pages))
    candidates.sort()  # Lowest ratio first

    if not candidates:
        logger.info("Stage 2: No files need vision refinement")
        return stats

    logger.info(f"Stage 2: {len(candidates)} files to refine with vision")

    prompt_template = (
        "這是台灣法院卷宗的第 {page} 頁。\n"
        "請判斷此頁的文件類型。\n"
        "回傳 JSON：{{\"type\": \"文件類型\", \"date\": \"民國年月日\", \"title\": \"書籤標題(20字內)\"}}\n"
        "文件類型包括：起訴書、判決、裁定、聲請狀、答辯狀、筆錄、鑑定報告、\n"
        "搜索票、通訊監察、診斷證明、財產資料、戶籍謄本、送達證書、債權人清冊、\n"
        "陳報狀、委任狀、報到單、照片截圖、票據契約、收發文函等。\n"
        "書籤標題格式：「日期 文件類型 當事人」，如「114.09.18 調查筆錄 陳OO」\n"
        "若此頁不值得加書籤（空白頁、浮水印頁、前一文件的續頁），回傳 {{\"type\": null}}\n"
        "只輸出 JSON。"
    )

    for _, pdf, page_count in candidates:
        if time.time() - budget_start > VISION_BUDGET_SECONDS:
            logger.info(f"⏰ Vision budget exhausted ({VISION_BUDGET_SECONDS}s)")
            break

        key = str(pdf)
        try:
            doc = fitz.open(str(pdf))
            existing_toc = doc.get_toc() or []
            # Build set of pages already bookmarked (by regex)
            bookmarked_pages = {pg for _, _, pg in existing_toc}
            new_bookmarks = []

            for pg_idx in range(page_count):
                pg_num = pg_idx + 1
                if pg_num in bookmarked_pages:
                    continue  # Regex already handled this page

                if time.time() - budget_start > VISION_BUDGET_SECONDS:
                    break

                page = doc[pg_idx]
                text = page.get_text().strip()
                # Skip pages with very little content
                if len(text) < 20:
                    continue

                # Render page to image
                try:
                    pix = page.get_pixmap(dpi=150)
                    img_path = tempfile.mktemp(suffix=".png")
                    pix.save(img_path)
                except Exception:
                    continue

                try:
                    prompt = prompt_template.format(page=pg_num)
                    result = gw.vision(img_path, prompt, timeout=VISION_PER_PAGE_TIMEOUT, task_type="vision")
                    stats["pages_checked"] += 1

                    if result.get("success"):
                        raw = str(result.get("text") or result.get("content") or "")
                        parsed = _parse_vision_response(raw)
                        if parsed and parsed.get("type") and parsed.get("title"):
                            title = str(parsed["title"])[:30].strip()
                            if title and len(title) >= 3:
                                new_bookmarks.append([1, title, pg_num])
                                stats["bookmarks_added"] += 1
                finally:
                    try:
                        os.unlink(img_path)
                    except Exception:
                        pass

            # Write new bookmarks to PDF
            if new_bookmarks:
                merged_toc = list(existing_toc) + new_bookmarks
                # Sort by page number
                merged_toc.sort(key=lambda x: (x[2], x[0]))
                doc.set_toc(merged_toc)
                doc.saveIncr()
                stats["files_refined"] += 1
                logger.info(f"  📑 Vision: +{len(new_bookmarks)} bookmarks → {pdf.name}")

            doc.close()
            mtime = str(pdf.stat().st_mtime)
            vision_done[key] = {"mtime": mtime, "added": len(new_bookmarks)}

        except Exception as e:
            logger.warning(f"  Vision error {pdf.name}: {e}")
            stats["errors"] += 1

        # Save state after each file
        _save_state(state)

    _save_state(state)
    return stats


def _parse_vision_response(raw: str) -> dict | None:
    """Parse JSON from vision model response."""
    try:
        # Find JSON in response
        m = re.search(r"\{[^{}]*\}", raw)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Two-stage PDF bookmark batch (regex + vision)"
    )
    parser.add_argument(
        "--stage",
        choices=["regex", "vision", "all"],
        default="all",
        help="regex=fast nightly pass (no oMLX restart), vision=refinement only, all=full weekend pass (default)",
    )
    parser.add_argument(
        "--max-minutes",
        type=int,
        default=0,
        help="Soft time budget for regex stage in minutes (0=no limit). Nightly runs should cap ~30.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only output a backfill plan (no PDF writes, no oMLX restart).",
    )
    parser.add_argument(
        "--plan-limit",
        type=int,
        default=20,
        help="Number of sample paths per plan category.",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Explicit folder to process recursively. Use for a priority case/evidence folder; can be repeated.",
    )
    parser.add_argument(
        "--target-subdir",
        action="append",
        default=[],
        help="Case subfolder name to scan during normal root discovery. Defaults include evidence and review folders.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="Progress JSON path. Defaults to .agent/bookmark_batch_state.json; priority/example runs should use a separate file.",
    )
    parser.add_argument(
        "--report-path",
        default=None,
        help="Optional JSON report path for this run.",
    )
    parser.add_argument(
        "--single-doc-fastpath",
        action="store_true",
        help="Skip obvious single-document filenames in priority/example runs and record them as legitimate single docs.",
    )
    parser.add_argument(
        "--skip-large-non-ocr",
        action="store_true",
        help="In priority/example runs, mark large non-OCR PDFs as needs_ocr instead of spending the batch on OCR.",
    )
    parser.add_argument(
        "--defer-large-ocr-pages",
        type=int,
        default=0,
        help="In priority/example runs, defer OCR PDFs at or above this page count to a split/off-peak pass.",
    )
    args = parser.parse_args()

    _set_state_file(args.state_file)
    if args.single_doc_fastpath:
        os.environ["BOOKMARK_SINGLE_DOC_FASTPATH"] = "1"
    if args.skip_large_non_ocr:
        os.environ["BOOKMARK_SKIP_LARGE_NON_OCR"] = "1"
    if args.defer_large_ocr_pages > 0:
        os.environ["BOOKMARK_DEFER_LARGE_OCR_PAGES"] = str(args.defer_large_ocr_pages)
    started = time.time()
    state = _load_state()

    target_subdirs = args.target_subdir or TARGET_SUBDIRS
    # preferred_case_roots already prefers NAS SMB over Synology Drive
    roots = [str(Path(r).expanduser()) for r in args.root] if args.root else preferred_case_roots(include_closed=False)
    label = "Nightly Regex" if args.stage == "regex" else ("Vision Only" if args.stage == "vision" else "Weekend")
    logger.info(f"📑 Bookmark Batch [{label}] — roots: {roots}")
    logger.info(f"Progress state: {STATE_FILE}")
    if not args.root:
        logger.info(f"Target subfolders: {target_subdirs}")

    pdfs = find_direct_pdfs(roots) if args.root else find_all_pdfs(roots, target_subdirs)
    logger.info(f"Found {len(pdfs)} PDFs across all case folders")

    if not pdfs:
        logger.info("No PDFs to process — done")
        return

    if args.dry_run:
        plan = build_backfill_plan(pdfs, state, sample_limit=max(1, args.plan_limit))
        BACKFILL_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        BACKFILL_PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Dry-run backfill plan written: %s", BACKFILL_PLAN_PATH)
        logger.info(
            "Plan summary: total=%d stage1_pending=%d no_boundary=%d vision_pending=%d",
            plan["total_pdfs"],
            plan["stage1_pending_count"],
            plan["no_boundary_backlog_count"],
            plan["vision_pending_count"],
        )
        return

    s1 = {
        "processed": 0,
        "bookmarks": 0,
        "skipped": 0,
        "no_boundary": 0,
        "needs_ocr": 0,
        "deferred_large_ocr": 0,
        "errors": 0,
    }
    s2 = {"pages_checked": 0, "bookmarks_added": 0, "files_refined": 0, "errors": 0}

    do_regex = args.stage in ("regex", "all")
    do_vision = args.stage in ("vision", "all")

    if do_regex:
        # ── Stage 1: Regex (fast, no LLM needed) ──
        logger.info("═══ Stage 1: Regex bookmarks ═══")
        # Only stop oMLX for full weekend run; nightly regex coexists with text/vision models.
        if args.stage == "all":
            _stop_omlx()  # Free RAM for faster I/O

        scan_fn = _load_bookmarker()
        # Pass soft budget through env so stage1_regex can honor it without signature churn
        if args.max_minutes > 0:
            os.environ["BOOKMARK_REGEX_BUDGET_SEC"] = str(args.max_minutes * 60)
        s1 = stage1_regex(pdfs, state, scan_fn)
        logger.info(
            f"Stage 1 done: {s1['processed']} processed, "
            f"{s1['bookmarks']} bookmarks, {s1['skipped']} skipped, "
            f"{s1['no_boundary']} no-boundary, {s1['errors']} errors ({time.time() - started:.0f}s)"
        )

    if do_vision:
        # ── Stage 2: Vision refinement (needs oMLX) ──
        logger.info("═══ Stage 2: Vision refinement ═══")
        # For --stage vision we assume oMLX is already up; only --stage all had to restart it.
        omlx_ok = _start_omlx() if args.stage == "all" else True
        if not omlx_ok:
            logger.warning("oMLX not available — skipping vision stage")
        else:
            gw = _load_vision_gateway()
            if gw:
                s2 = stage2_vision(pdfs, state, gw)

    elapsed = time.time() - started
    lines = [f"📑 Bookmark Batch [{label}] 完成"]
    lines.append(f"  PDF 數量：{len(pdfs)} 個")
    if do_regex:
        lines.append("  ── Stage 1 (regex) ──")
        lines.append(f"  處理：{s1['processed']} 份 / {s1['bookmarks']} 個書籤")
        lines.append(f"  跳過：{s1['skipped']} 份")
        lines.append(f"  無邊界（待 vision 補漏）：{s1['no_boundary']} 份")
        if s1.get("needs_ocr"):
            lines.append(f"  大型非 OCR（待 OCR 後再編）：{s1['needs_ocr']} 份")
        if s1.get("deferred_large_ocr"):
            lines.append(f"  大型 OCR（待分割/離峰重跑）：{s1['deferred_large_ocr']} 份")
    if do_vision:
        lines.append("  ── Stage 2 (vision) ──")
        lines.append(f"  視覺檢查：{s2['pages_checked']} 頁")
        lines.append(f"  補充書籤：{s2['bookmarks_added']} 個 / {s2['files_refined']} 份")
    lines.append(f"  錯誤：{s1['errors'] + s2['errors']} 筆")
    lines.append(f"  耗時：{elapsed:.0f} 秒（{elapsed / 3600:.1f} 小時）")
    summary = "\n".join(lines)
    logger.info(summary)

    if args.report_path:
        report_path = Path(args.report_path).expanduser()
        if not report_path.is_absolute():
            report_path = MAGI_ROOT / report_path
        completed = state.get("completed", {}) or {}
        stage1_errors = state.get("stage1_errors", {}) or {}
        relevant_completed = [
            key for key in completed
            if any(str(Path(root).expanduser()) in key for root in roots)
        ] if args.root else list(completed.keys())
        relevant_errors = [
            key for key in stage1_errors
            if any(str(Path(root).expanduser()) in key for root in roots)
        ] if args.root else list(stage1_errors.keys())
        report = {
            "timestamp": datetime.now().isoformat(),
            "label": label,
            "stage": args.stage,
            "roots": roots,
            "target_subdirs": target_subdirs if not args.root else None,
            "state_file": str(STATE_FILE),
            "pdf_count": len(pdfs),
            "stats": {"regex": s1, "vision": s2},
            "completed_count_for_roots": len(relevant_completed),
            "bookmarked_count_for_roots": sum(
                1 for key in relevant_completed
                if int((completed.get(key) or {}).get("stage1_bookmarks") or 0) > 0
            ),
            "no_boundary_count_for_roots": sum(
                1 for key in relevant_completed
                if (completed.get(key) or {}).get("no_boundary")
            ),
            "stage1_error_count_for_roots": len(relevant_errors),
            "stage1_error_samples": [
                {
                    "path": key,
                    "message": str((stage1_errors.get(key) or {}).get("message") or ""),
                }
                for key in relevant_errors[:20]
            ],
            "elapsed_sec": round(elapsed, 3),
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Run report written: %s", report_path)

    # Notify (weekend full pass gets the system channel; nightly is quieter — log only)
    if args.stage == "all":
        try:
            from api.red_phone import notify
            notify(summary, channel="system")
        except Exception:
            pass


if __name__ == "__main__":
    main()
