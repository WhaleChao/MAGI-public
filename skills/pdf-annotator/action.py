#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf-annotator/action.py
=========================
依據游秀鈴案 PDF 書籤（超連結/TOC）樣式，
使用視覺模組對所有案件卷宗 PDF 自動建立導覽書籤（目錄）。

「手工標籤」= Acrobat 左側面板的 PDF 書籤（doc.get_toc() / doc.set_toc()），
每個書籤是一個 (level, title, page_num) 三元組，指向特定頁面。

任務（--task）：
  learn     從游秀鈴案閱卷 PDF 學習書籤命名慣例，儲存 schema
  annotate  對所有案件卷宗 PDF 批次自動加書籤（不覆蓋已有書籤的檔案）
  test      驗證自動書籤品質（每頁平均書籤數、標題格式正確率）
  status    顯示已標籤統計

環境變數：
  SYNOLOGY_CASE_ROOTS     案件根目錄（逗號分隔）
  ANNOTATION_SOURCE_CASE  學習書籤的參考案件目錄名
  ANNOTATION_SOURCE_SUBDIR 閱卷資料子目錄
  ANNOTATION_TARGET_SUBDIRS 要標籤的子目錄（逗號分隔）
  ANNOTATION_MAX_PAGES    視覺模型每次處理最多頁數（default: 3）
  ANNOTATION_DPI          頁面截圖解析度（default: 150）
  ANNOTATION_OUT_SUFFIX   輸出檔案後綴（default: _bookmarked）
"""
from __future__ import annotations
import logging

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── MAGI path setup ───────────────────────────────────────────────────────────
_DEFAULT_MAGI_ROOT = Path(__file__).resolve().parents[2]
MAGI_ROOT = Path(os.environ.get("MAGI_ROOT", str(_DEFAULT_MAGI_ROOT)))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))
from api.case_path_mapper import preferred_case_roots

# ── Config ────────────────────────────────────────────────────────────────────
_DEFAULT_CASE_ROOTS = ",".join(preferred_case_roots(include_closed=True))
_env_roots = (
    os.environ.get("SYNOLOGY_CASE_ROOTS")
    or os.environ.get("SYNOLOGY_CASE_ROOT")
    or _DEFAULT_CASE_ROOTS
)
CASE_ROOTS = [Path(r.strip()) for r in _env_roots.split(",") if r.strip()]
CASE_ROOT = CASE_ROOTS[0] if CASE_ROOTS else Path(_DEFAULT_CASE_ROOTS.split(",")[0])

SOURCE_CASE = os.environ.get("ANNOTATION_SOURCE_CASE", "2025-0002-游秀鈴-一審-傷害致死")
SOURCE_SUBDIR = os.environ.get("ANNOTATION_SOURCE_SUBDIR", "06_閱卷資料")
TARGET_SUBDIRS = [
    s.strip()
    for s in os.environ.get("ANNOTATION_TARGET_SUBDIRS", "06_閱卷資料").split(",")
    if s.strip()
]
MAX_PAGES_PER_CALL = int(os.environ.get("ANNOTATION_MAX_PAGES", "3") or "3")
PAGE_DPI = int(os.environ.get("ANNOTATION_DPI", "150") or "150")
OUT_SUFFIX = os.environ.get("ANNOTATION_OUT_SUFFIX", "_bookmarked")

SCHEMA_PATH = MAGI_ROOT / ".agent" / "annotation_schema.json"
STATE_PATH = MAGI_ROOT / ".agent" / "annotation_state.json"

# ── Bookmark category keywords (used for naming bookmarks) ────────────────────
_BOOKMARK_CATEGORIES = [
    "重要事實",
    "矛盾點",
    "日期時間",
    "人名",
    "關鍵證詞",
    "物證",
    "鑑定意見",
    "法律爭點",
]


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8"))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 89, exc_info=True)
    return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


# ── PDF helpers ───────────────────────────────────────────────────────────────

def _page_to_image(doc, page_num: int, dpi: int = 150) -> Optional[str]:
    """Render a PDF page to a temporary PNG; returns path or None."""
    try:
        import fitz
        page = doc[page_num]
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=mat)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        pix.save(tmp.name)
        return tmp.name
    except Exception:
        return None


def _get_toc(doc) -> List[List]:
    """Return existing TOC; empty list if none."""
    try:
        return doc.get_toc() or []
    except Exception:
        return []


# ── Vision model ──────────────────────────────────────────────────────────────

def _get_gateway():
    try:
        from skills.bridge.inference_gateway import InferenceGateway
        return InferenceGateway()
    except ImportError:
        sys.path.insert(0, str(MAGI_ROOT))
        from skills.bridge.inference_gateway import InferenceGateway
        return InferenceGateway()


def _vision_suggest_bookmarks(
    image_path: str,
    page_num: int,
    schema: Dict[str, Any],
    existing_toc: List[List],
) -> List[Dict[str, Any]]:
    """
    Ask vision model to suggest bookmark(s) for this page.
    Returns list of {title, level, reason}.
    """
    cats = "、".join(_BOOKMARK_CATEGORIES)
    examples_text = ""
    examples = schema.get("examples") or []
    if examples:
        sample = "\n".join(
            f"  第{e.get('page','')}頁：{e.get('title','')}"
            for e in examples[:8]
        )
        examples_text = f"\n\n已學習的書籤命名範例（請模仿這種風格）：\n{sample}"

    # Tell model what's already bookmarked to avoid duplicates
    existing_text = ""
    if existing_toc:
        existing_text = "\n已有書籤（不要重複）：" + "、".join(
            str(e[1]) for e in existing_toc[:10]
        )

    prompt = (
        f"這是台灣法院卷宗（正體中文）第 {page_num+1} 頁。{existing_text}\n"
        f"書籤類別（參考）：{cats}。{examples_text}\n\n"
        f"請判斷此頁是否值得加書籤：\n"
        f"- 若是，給出書籤標題（格式：「類別 - 簡短說明」，20字以內，台灣繁體中文）\n"
        f"- 若否，回傳空陣列\n\n"
        f"回傳 JSON 陣列，每筆包含：\n"
        f"- title: 書籤標題（繁體中文）\n"
        f"- level: 層級（1=主要，2=次要）\n"
        f"- reason: 加書籤理由（10字以內）\n\n"
        f"只輸出 JSON，不要其他文字。"
    )

    gw = _get_gateway()
    result = gw.vision(image_path, prompt, timeout=60, task_type="vision")

    if not result.get("success"):
        return []

    raw = str(result.get("text") or result.get("content") or "").strip()
    m = re.search(r"\[.*?\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            level = int(item.get("level") or 1)
            reason = str(item.get("reason") or "").strip()
            if title and 3 <= len(title) <= 40:
                out.append({"title": title, "level": max(1, min(2, level)), "reason": reason})
        return out
    except Exception:
        return []


# ── LEARN command ─────────────────────────────────────────────────────────────

def cmd_learn(max_sample_files: int = 5) -> str:
    """
    Extract TOC/bookmark patterns from 游秀鈴 case reference PDFs.
    If no bookmarks found in synced files, uses vision-based inference
    to build a naming convention schema from page content.
    """
    import fitz

    # Find source case
    source_dir = None
    for case_root in CASE_ROOTS:
        if not case_root.exists():
            continue
        for case_type in case_root.iterdir():
            if not case_type.is_dir():
                continue
            for law_type in case_type.iterdir():
                if not law_type.is_dir():
                    continue
                candidate = law_type / SOURCE_CASE
                if candidate.is_dir():
                    source_dir = candidate
                    break
            if source_dir:
                break
        if source_dir:
            break

    if not source_dir:
        return f"找不到參考案件目錄：{SOURCE_CASE}"

    source_pdf_dir = source_dir / SOURCE_SUBDIR
    if not source_pdf_dir.is_dir():
        return f"找不到閱卷資料目錄：{source_pdf_dir}"

    # Collect sample PDFs — prefer non-OCR (may have bookmarks)
    pdfs = sorted(source_pdf_dir.glob("**/*.pdf"))
    pdfs = [p for p in pdfs if not p.name.startswith(".")][:max_sample_files]

    examples: List[Dict[str, Any]] = []
    toc_examples: List[Dict[str, Any]] = []
    files_processed = 0

    for pdf_path in pdfs:
        doc = None
        try:
            doc = fitz.open(str(pdf_path))
            toc = _get_toc(doc)

            if toc:
                # ── Learn directly from existing bookmarks ──
                for level, title, page_num, *_ in toc:
                    toc_examples.append({
                        "title": str(title),
                        "level": int(level),
                        "page": int(page_num),
                        "source_file": pdf_path.name,
                        "from_toc": True,
                    })
            else:
                # ── Fallback: vision-based learning ──
                for pg_idx in range(min(2, len(doc))):
                    img_path = _page_to_image(doc, pg_idx, dpi=PAGE_DPI)
                    if not img_path:
                        continue
                    try:
                        cats = "、".join(_BOOKMARK_CATEGORIES)
                        prompt = (
                            f"這是台灣法院卷宗文件（正體中文）第 {pg_idx+1} 頁。\n"
                            f"若你要為這份文件建立 PDF 書籤目錄，此頁的書籤標題應該是什麼？\n"
                            f"書籤類別參考：{cats}。\n"
                            f"格式：「類別 - 簡短說明（人名/日期/事件）」\n"
                            f"回傳 JSON 陣列：[{{\"title\": \"書籤標題\", \"level\": 1, \"reason\": \"理由\"}}]\n"
                            f"若此頁不值得加書籤，回傳 []。只輸出 JSON。"
                        )
                        gw = _get_gateway()
                        result = gw.vision(img_path, prompt, timeout=60, task_type="vision")
                        if result.get("success"):
                            raw = str(result.get("text") or result.get("content") or "")
                            m = re.search(r"\[.*?\]", raw, re.DOTALL)
                            if m:
                                items = json.loads(m.group(0))
                                for item in (items if isinstance(items, list) else []):
                                    if isinstance(item, dict):
                                        t = str(item.get("title") or "").strip()
                                        if t and 3 <= len(t) <= 40:
                                            examples.append({
                                                "title": t,
                                                "level": int(item.get("level") or 1),
                                                "page": pg_idx + 1,
                                                "reason": str(item.get("reason") or "")[:40],
                                                "source_file": pdf_path.name,
                                                "from_toc": False,
                                            })
                    finally:
                        try:
                            os.unlink(img_path)
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 304, exc_info=True)
            files_processed += 1
        except Exception:
            continue
        finally:
            if doc:
                try: doc.close()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 312, exc_info=True)

    # Merge both sources (TOC examples are higher quality)
    all_examples = toc_examples + examples
    toc_based = len(toc_examples) > 0

    schema = {
        "source_case": SOURCE_CASE,
        "examples": all_examples[:60],
        "categories": _BOOKMARK_CATEGORIES,
        "toc_based": toc_based,
        "learned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files_processed": files_processed,
        "note": (
            "從現有書籤學習" if toc_based
            else "參考 PDF 無書籤，已使用視覺模型推斷命名慣例。"
            " 建議從 Acrobat 匯出含書籤的 PDF 後重新執行 learn，可提升準確度。"
        ),
    }
    _save_json(SCHEMA_PATH, schema)

    lines = [
        f"書籤學習完成（{'直接從 TOC 學習' if toc_based else '視覺模型推斷'}）：",
        f"- 處理檔案：{files_processed} 份",
        f"- 學習範例：{len(all_examples)} 條",
    ]
    if toc_examples:
        lines.append(f"- 直接讀取到的書籤：{len(toc_examples)} 條")
        for e in toc_examples[:5]:
            lines.append(f"  第{e['page']}頁：{e['title']}")
    elif examples:
        lines.append("- 視覺推斷範例（前5條）：")
        for e in examples[:5]:
            lines.append(f"  {e['title']}")
    lines.append(f"- Schema 儲存：{SCHEMA_PATH}")
    if not toc_based:
        lines.append(
            "\n提示：在 Acrobat 中選「匯出 → 含書籤的 PDF」存回 Synology 後重跑 learn，效果更好。"
        )
    return "\n".join(lines)


# ── ANNOTATE command ──────────────────────────────────────────────────────────

def cmd_annotate(target_case: str = "", force: bool = False) -> str:
    """
    Auto-generate PDF bookmarks for all case volume PDFs.
    Skips files that already have bookmarks (unless --force).
    """
    import fitz

    schema = _load_json(SCHEMA_PATH, {})
    if not schema:
        return "尚未完成學習，請先執行 --task learn"

    state = _load_json(STATE_PATH, {"annotated": {}})
    annotated_map = state.get("annotated") or {}

    total_files = total_bookmarks = total_skipped = 0
    errors: List[str] = []
    seen_pdfs: set = set()

    for case_root in CASE_ROOTS:
        if not case_root.exists():
            continue
        for case_type_dir in sorted(case_root.iterdir()):
            if not case_type_dir.is_dir() or case_type_dir.name.startswith("."):
                continue
            for law_type_dir in sorted(case_type_dir.iterdir()):
                if not law_type_dir.is_dir() or law_type_dir.name.startswith("."):
                    continue
                for case_dir in sorted(law_type_dir.iterdir()):
                    if not case_dir.is_dir() or case_dir.name.startswith("."):
                        continue
                    if case_dir.name == SOURCE_CASE and not force:
                        continue
                    if target_case and target_case not in case_dir.name:
                        continue

                    for subdir_name in TARGET_SUBDIRS:
                        subdir = case_dir / subdir_name
                        if not subdir.is_dir():
                            continue
                        for pdf_path in sorted(subdir.glob("**/*_OCR.pdf")):
                            resolved = str(pdf_path.resolve())
                            if resolved in seen_pdfs:
                                continue
                            seen_pdfs.add(resolved)

                            pdf_key = str(pdf_path)
                            mtime = str(pdf_path.stat().st_mtime)
                            if not force and annotated_map.get(pdf_key, {}).get("mtime") == mtime:
                                total_skipped += 1
                                continue

                            out_path = pdf_path.parent / (pdf_path.stem + OUT_SUFFIX + ".pdf")
                            if out_path.exists() and not force:
                                total_skipped += 1
                                continue

                            doc = None
                            try:
                                doc = fitz.open(str(pdf_path))
                                existing_toc = _get_toc(doc)

                                # Skip if already well-bookmarked (>= 3 bookmarks per 10 pages)
                                if (not force and len(existing_toc) >= max(3, len(doc) // 10)):
                                    total_skipped += 1
                                    continue

                                new_toc = list(existing_toc)
                                page_bookmarks = 0

                                for pg_idx in range(len(doc)):
                                    img_path = _page_to_image(doc, pg_idx, dpi=PAGE_DPI)
                                    if not img_path:
                                        continue
                                    try:
                                        suggestions = _vision_suggest_bookmarks(
                                            img_path, pg_idx, schema, new_toc
                                        )
                                        for s in suggestions:
                                            new_toc.append([
                                                s["level"],
                                                s["title"],
                                                pg_idx + 1,  # 1-based page number
                                            ])
                                            page_bookmarks += 1
                                    finally:
                                        try:
                                            os.unlink(img_path)
                                        except Exception:
                                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 443, exc_info=True)

                                if new_toc and new_toc != existing_toc:
                                    doc.set_toc(new_toc)
                                    doc.save(str(out_path), garbage=4, deflate=True)

                                annotated_map[pdf_key] = {
                                    "mtime": mtime,
                                    "output": str(out_path),
                                    "bookmarks": page_bookmarks,
                                    "annotated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                }
                                total_files += 1
                                total_bookmarks += page_bookmarks
                            except Exception as e:
                                errors.append(f"{pdf_path.name}: {e}")
                            finally:
                                if doc:
                                    try: doc.close()
                                    except Exception:
                                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 463, exc_info=True)

    state["annotated"] = annotated_map
    state["stats"] = {
        "total_files": len(annotated_map),
        "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_json(STATE_PATH, state)

    lines = [
        "PDF 自動書籤完成",
        f"- 本次處理：{total_files} 份 / {total_bookmarks} 個書籤",
        f"- 已跳過：{total_skipped} 份",
    ]
    if errors:
        lines.append(f"- 錯誤（{len(errors)} 筆）：" + "；".join(errors[:5]))
    return "\n".join(lines)


# ── TEST command ──────────────────────────────────────────────────────────────

def cmd_test(sample_pages: int = 5) -> str:
    """
    Validate auto-bookmark quality.
    Pass criteria: avg >= 0.5 bookmarks/page AND all titles are non-empty, <= 40 chars.
    """
    import fitz

    schema = _load_json(SCHEMA_PATH, {})
    if not schema:
        return "FAIL: 尚未完成 learn"

    source_dir = None
    for case_root in CASE_ROOTS:
        if not case_root.exists():
            continue
        for case_type in case_root.iterdir():
            if not case_type.is_dir():
                continue
            for law_type in case_type.iterdir():
                if not law_type.is_dir():
                    continue
                candidate = law_type / SOURCE_CASE
                if candidate.is_dir():
                    source_dir = candidate
                    break
            if source_dir:
                break
        if source_dir:
            break

    if not source_dir:
        return f"FAIL: 找不到參考案件：{SOURCE_CASE}"

    test_pdfs = sorted((source_dir / SOURCE_SUBDIR).glob("**/*_OCR.pdf"))[:2]
    if not test_pdfs:
        return "FAIL: 找不到測試 PDF"

    total_pages = 0
    total_bookmarks = 0
    format_errors = 0

    for pdf_path in test_pdfs:
        doc = None
        try:
            doc = fitz.open(str(pdf_path))
            for pg_idx in range(min(3, len(doc))):
                if total_pages >= sample_pages:
                    break
                img_path = _page_to_image(doc, pg_idx, dpi=PAGE_DPI)
                if not img_path:
                    continue
                try:
                    suggestions = _vision_suggest_bookmarks(img_path, pg_idx, schema, [])
                    total_pages += 1
                    total_bookmarks += len(suggestions)
                    for s in suggestions:
                        t = s.get("title", "")
                        if not t or len(t) > 40 or len(t) < 3:
                            format_errors += 1
                finally:
                    try:
                        os.unlink(img_path)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 546, exc_info=True)
        except Exception:
            continue
        finally:
            if doc:
                try: doc.close()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 553, exc_info=True)

    if total_pages == 0:
        return "FAIL: 無法處理測試頁面（視覺模型無回應）"

    avg_per_page = total_bookmarks / total_pages
    format_ok = total_bookmarks > 0 and format_errors == 0
    accuracy = 100.0 if format_ok else max(0.0, (1 - format_errors / max(1, total_bookmarks)) * 100)

    lines = [
        "=== PDF 書籤測試報告 ===",
        f"測試頁面數：{total_pages}",
        f"產生書籤數：{total_bookmarks}（平均 {avg_per_page:.2f}/頁）",
        f"格式正確率：{accuracy:.1f}%（格式錯誤：{format_errors} 條）",
        f"Schema 來源：{'TOC直接學習' if schema.get('toc_based') else '視覺推斷'}",
        f"學習範例數：{len(schema.get('examples', []))}",
        "",
    ]

    if avg_per_page >= 0.5 and accuracy >= 100.0:
        lines.append("結果：PASS ✓")
    else:
        reasons = []
        if avg_per_page < 0.5:
            reasons.append(f"書籤覆蓋率不足（{avg_per_page:.2f}/頁，需 ≥ 0.5）")
        if accuracy < 100.0:
            reasons.append(f"格式正確率 {accuracy:.1f}% < 100%")
        lines.append(f"結果：FAIL ✗ - {' / '.join(reasons)}")
        if not schema.get("toc_based"):
            lines.append("建議：從 Acrobat 匯出含書籤的 PDF 存回 Synology 後重跑 --task learn")
    return "\n".join(lines)


# ── STATUS command ────────────────────────────────────────────────────────────

def cmd_status() -> str:
    schema = _load_json(SCHEMA_PATH, {})
    state = _load_json(STATE_PATH, {"annotated": {}})
    annotated = state.get("annotated") or {}
    stats = state.get("stats") or {}

    lines = [
        "PDF 書籤系統狀態：",
        f"- 學習來源：{'TOC 直接讀取' if schema.get('toc_based') else '視覺推斷'}",
        f"- 學習範例：{len(schema.get('examples', []))} 條（{schema.get('learned_at', 'n/a')}）",
        f"- 已標籤檔案：{len(annotated)} 份（最後：{stats.get('last_run', 'n/a')}）",
        f"- 參考案件：{SOURCE_CASE}",
        f"- Schema 路徑：{SCHEMA_PATH}",
        f"- 書籤類別：{', '.join(_BOOKMARK_CATEGORIES)}",
    ]
    if schema.get("note"):
        lines.append(f"- 備註：{schema['note']}")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="MAGI PDF 自動書籤技能")
    ap.add_argument("--task", default="annotate", help="learn|annotate|test|status")
    ap.add_argument("--case", default="", help="指定案件（含於案件目錄名中即可）")
    ap.add_argument("--force", default="0", help="1=強制重新處理已標籤檔案")
    ap.add_argument("--sample", type=int, default=5, help="test 模式樣本頁數（default: 5）")
    ap.add_argument("--learn_files", type=int, default=5, help="learn 模式樣本檔數（default: 5）")
    args = ap.parse_args()

    task = str(args.task or "annotate").strip().lower()
    if task == "help":
        print(json.dumps({"skill": "pdf-annotator", "tasks": ["learn", "annotate", "test", "status"], "description": "案件卷宗 PDF 書籤自動標註工具"}, ensure_ascii=False, indent=2))
        return 0
    force = str(args.force or "0").strip().lower() in {"1", "true", "yes"}

    if task == "learn":
        print(cmd_learn(max_sample_files=args.learn_files))
    elif task == "annotate":
        print(cmd_annotate(target_case=args.case, force=force))
    elif task == "test":
        print(cmd_test(sample_pages=args.sample))
    elif task == "status":
        print(cmd_status())
    else:
        print(f"未知 task: {task}，請使用 learn|annotate|test|status")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
