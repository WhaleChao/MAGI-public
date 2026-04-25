#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcript-indexer/action.py
=============================
掃描所有案件筆錄 PDF，分段向量化存入 KEEPER 記憶庫，
支援自然語句查詢並附筆錄出處（案件、日期、頁次、發言人）。

任務（--task）：
  index   掃描所有案件筆錄 → 向量入庫（排程用）
  query   自然語句查詢筆錄，輸出 JSON {results:[...]}
  status  顯示已索引筆錄統計

環境變數：
  SYNOLOGY_CASE_ROOT  案件根目錄（default: shared MAGI case root）
  TRANSCRIPT_DIRS     筆錄子目錄名（逗號分隔，default: 05_筆錄,06_筆錄,07_筆錄,08_筆錄）
  TRANSCRIPT_INDEX_DB 索引狀態紀錄路徑（default: MAGI_ROOT/.agent/transcript_index.json）
  TRANSCRIPT_BATCH    每批向量化筆數（default: 20）
"""
from __future__ import annotations
import logging

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

# ── MAGI path setup ───────────────────────────────────────────────────────────
_DEFAULT_MAGI_ROOT = Path(__file__).resolve().parents[2]
MAGI_ROOT = Path(os.environ.get("MAGI_ROOT", str(_DEFAULT_MAGI_ROOT)))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))
from api.case_path_mapper import preferred_case_roots

# ── Config ────────────────────────────────────────────────────────────────────
_DEFAULT_CASE_ROOTS = ",".join(preferred_case_roots(include_closed=True))
# Support comma-separated list of case roots; legacy single-root env var also accepted
_env_roots = (
    os.environ.get("SYNOLOGY_CASE_ROOTS")
    or os.environ.get("SYNOLOGY_CASE_ROOT")
    or _DEFAULT_CASE_ROOTS
)
CASE_ROOTS = [Path(r.strip()) for r in _env_roots.split(",") if r.strip()]
# backwards compat alias
CASE_ROOT = CASE_ROOTS[0] if CASE_ROOTS else Path(_DEFAULT_CASE_ROOTS.split(",")[0])

_TRANSCRIPT_SUBDIRS = [
    s.strip()
    for s in os.environ.get("TRANSCRIPT_DIRS", "05_筆錄,06_筆錄,07_筆錄,08_筆錄").split(",")
    if s.strip()
]

INDEX_DB_PATH = Path(
    os.environ.get("TRANSCRIPT_INDEX_DB", str(MAGI_ROOT / ".agent" / "transcript_index.json"))
)
BATCH_SIZE = int(os.environ.get("TRANSCRIPT_BATCH", "20") or "20")

# 2026-04-25: 進度節流 + wall-clock budget（NAS over Tailscale 平均 1.5-3s/PDF，
# 602 個 PDF 約 20 分鐘超 cron timeout 600s）
TRANSCRIPT_MAX_PDFS_PER_RUN = int(os.environ.get("TRANSCRIPT_MAX_PDFS_PER_RUN", "150") or "150")
TRANSCRIPT_BUDGET_SEC = int(os.environ.get("TRANSCRIPT_BUDGET_SEC", "480") or "480")  # 8 min < 10 min cron timeout
TRANSCRIPT_PDF_TIMEOUT_SEC = int(os.environ.get("TRANSCRIPT_PDF_TIMEOUT_SEC", "30") or "30")  # 單檔 fitz timeout
TRANSCRIPT_THROTTLE_EVERY = int(os.environ.get("TRANSCRIPT_THROTTLE_EVERY", "20") or "20")
TRANSCRIPT_THROTTLE_SLEEP = float(os.environ.get("TRANSCRIPT_THROTTLE_SLEEP", "0.3") or "0.3")
TRANSCRIPT_LISTING_BUDGET_SEC = int(os.environ.get("TRANSCRIPT_LISTING_BUDGET_SEC", "120") or "120")

# Source prefix stored in KEEPER — kept short to fit VARCHAR(250)
_SOURCE_PREFIX = "transcript"

# ── Speaker detection patterns (Traditional Chinese) ─────────────────────────
_SPEAKER_PATTERNS = [
    (re.compile(r"法\s*官[問答問曰：:\s]"), "法官"),
    (re.compile(r"審判長[問答曰：:\s]"), "審判長"),
    (re.compile(r"被\s*告[答問曰：:\s]"), "被告"),
    (re.compile(r"證\s*人[答問曰：:\s]"), "證人"),
    (re.compile(r"辯護人[答問曰：:\s]"), "辯護人"),
    (re.compile(r"檢察官[答問曰：:\s]"), "檢察官"),
    (re.compile(r"告訴人[答問曰：:\s]"), "告訴人"),
    (re.compile(r"告訴代理人[答問曰：:\s]"), "告訴代理人"),
    (re.compile(r"^問[:：]"), "發問"),
    (re.compile(r"^答[:：]"), "回答"),
]

# Line-number prefix like "  02\n" or "  123\n"
_LINE_NUM_RE = re.compile(r"^\s{0,8}\d{1,4}\s*$")

# Date from filename, e.g. "20240722 訊問筆錄.pdf"
_DATE_FROM_FILENAME_RE = re.compile(r"(\d{8})")


# ── Index state ───────────────────────────────────────────────────────────────

def _load_index() -> Dict[str, Any]:
    if INDEX_DB_PATH.exists():
        try:
            return json.loads(INDEX_DB_PATH.read_text("utf-8"))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 91, exc_info=True)
    return {"indexed": {}, "stats": {"total_chunks": 0, "total_files": 0}}


def _is_transcript_indexed(pdf_key: str, mtime: str, indexed_map: Dict) -> bool:
    """Check if a transcript PDF is already indexed — DB 優先，JSON fallback。"""
    try:
        from skills.ops.dedup_db import is_done as _dd_is_done
        if _dd_is_done("transcript", pdf_key):
            return True
    except Exception:
        pass
    return indexed_map.get(pdf_key, {}).get("mtime") == mtime


def _save_index(idx: Dict[str, Any]) -> None:
    INDEX_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_DB_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(INDEX_DB_PATH)
    # DB dedup sync: write all indexed keys
    try:
        from skills.ops.dedup_db import mark_done as _dd_mark
        for pdf_key, info in (idx.get("indexed") or {}).items():
            _dd_mark("transcript", pdf_key, metadata={
                "case_name": info.get("case_name", ""),
                "file_name": info.get("file_name", ""),
                "chunks": info.get("chunks", 0),
                "source": "transcript_indexer",
            })
    except Exception:
        pass


# ── Case folder traversal ─────────────────────────────────────────────────────

def _iter_case_dirs(root: Path):
    """Yield case directories under any supported root structure."""
    if not root.exists():
        return
    # Walk up to 4 levels deep looking for dirs that contain transcript subdirs
    # Structure A (SynologyDrive): root/案件類型/法律類型/YEAR-NO-NAME/
    # Structure B (lumi 結案):     root/案件類型/法律類型/YEAR-NO-NAME/
    for lvl1 in root.iterdir():
        if not lvl1.is_dir() or lvl1.name.startswith("."):
            continue
        for lvl2 in lvl1.iterdir():
            if not lvl2.is_dir() or lvl2.name.startswith("."):
                continue
            for lvl3 in lvl2.iterdir():
                if not lvl3.is_dir() or lvl3.name.startswith("."):
                    continue
                # Check if this looks like a case dir (has any transcript subdir)
                has_transcript = any(
                    (lvl3 / s).is_dir() for s in _TRANSCRIPT_SUBDIRS
                )
                if has_transcript:
                    yield lvl3
                    continue
                # One more level for deeply nested structures
                for lvl4 in lvl3.iterdir():
                    if not lvl4.is_dir() or lvl4.name.startswith("."):
                        continue
                    if any((lvl4 / s).is_dir() for s in _TRANSCRIPT_SUBDIRS):
                        yield lvl4


def _iter_transcript_pdfs() -> Generator[Tuple[Path, str, str], None, None]:
    """Yield (pdf_path, case_name, transcript_subdir) across all case roots.

    2026-04-25: 加 listing budget — NAS over Tailscale 列舉 600+ PDFs 需 ~80s，
    超過 TRANSCRIPT_LISTING_BUDGET_SEC（預設 120s）截斷。
    """
    import time as _t
    seen: set = set()
    t_start = _t.time()
    for root in CASE_ROOTS:
        for case_dir in _iter_case_dirs(root):
            if _t.time() - t_start > TRANSCRIPT_LISTING_BUDGET_SEC:
                print(f"[index] ⏱️ listing budget {TRANSCRIPT_LISTING_BUDGET_SEC}s 已用盡，截斷列舉",
                      file=sys.stderr, flush=True)
                return
            case_name = case_dir.name
            for subdir_name in _TRANSCRIPT_SUBDIRS:
                subdir = case_dir / subdir_name
                if not subdir.is_dir():
                    continue
                try:
                    pdfs = sorted(subdir.glob("*.pdf"))
                except OSError as e:
                    print(f"[index] ⚠️ skip {subdir.name}: {e}", file=sys.stderr, flush=True)
                    continue
                for pdf in pdfs:
                    try:
                        key = str(pdf.resolve())
                    except OSError:
                        continue
                    if key in seen:
                        continue
                    seen.add(key)
                    yield pdf, case_name, subdir_name


# ── PDF text extraction ───────────────────────────────────────────────────────

def _extract_pages_inner(pdf_path: Path) -> List[Tuple[int, str]]:
    """Inner extractor (no timeout) — used by _extract_pages with thread wrapper."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
        pages = []
        for i, page in enumerate(doc):
            pages.append((i + 1, page.get_text()))
        doc.close()
        return pages
    except Exception:
        return []


def _extract_pages(pdf_path: Path) -> List[Tuple[int, str]]:
    """Return [(page_num_1based, text), ...].

    2026-04-25: 加 thread-based timeout (TRANSCRIPT_PDF_TIMEOUT_SEC，預設 30s)。
    fitz 對某些壞 PDF 或 NAS 慢 I/O 會 hang，必須有外層守時。
    Thread 雖無法真正 kill C 層 fitz，但主流程能繼續往下走（孤兒 thread 會在 process exit 時收掉）。
    """
    import threading
    result: List[Tuple[int, str]] = []
    done = threading.Event()

    def _runner():
        try:
            r = _extract_pages_inner(pdf_path)
            result.extend(r)
        finally:
            done.set()

    t = threading.Thread(target=_runner, daemon=True, name=f"pdf-extract-{pdf_path.name[:30]}")
    t.start()
    if not done.wait(timeout=TRANSCRIPT_PDF_TIMEOUT_SEC):
        print(f"  ⏱️ extract timeout {TRANSCRIPT_PDF_TIMEOUT_SEC}s, skip: {pdf_path.name}",
              file=sys.stderr, flush=True)
        return []  # 放棄這個 PDF（thread 自然結束於 process exit）
    return result


def _clean_line(line: str) -> str:
    """Remove line-number prefix artefacts from OCR output."""
    line = line.strip()
    # Remove standalone line-number tokens like "  02  " between content
    line = re.sub(r"\s+\d{1,4}\s*$", "", line)
    line = re.sub(r"^\d{1,4}\s+", "", line)
    # Collapse extra whitespace (from spaced OCR characters like "法  官")
    line = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", line)
    return line.strip()


def _detect_speaker(line: str) -> Optional[str]:
    for pattern, label in _SPEAKER_PATTERNS:
        if pattern.search(line):
            return label
    return None


# ── Chunk building ────────────────────────────────────────────────────────────

def _parse_chunks(
    pages: List[Tuple[int, str]],
    pdf_path: Path,
    case_name: str,
    date_str: str,
    transcript_type: str,
) -> List[Dict[str, Any]]:
    """
    Split transcript into semantic chunks of ~200-400 chars.
    Each chunk = one speaker turn or a few consecutive lines.
    Returns list of {text, page, speaker, line_start, line_end, ...}.
    """
    chunks: List[Dict[str, Any]] = []
    current_speaker = "不明"
    current_lines: List[str] = []
    current_page = 1
    current_line_start = 1
    line_counter = 0

    def _flush(speaker: str, lines: List[str], page: int, ln_start: int, ln_end: int) -> None:
        text = "".join(lines).strip()
        if len(text) < 10:
            return
        chunks.append({
            "text": text,
            "speaker": speaker,
            "page": page,
            "line_start": ln_start,
            "line_end": ln_end,
            "date": date_str,
            "case_name": case_name,
            "transcript_type": transcript_type,
            "file_name": pdf_path.name,
        })

    for page_no, page_text in pages:
        raw_lines = page_text.split("\n")
        for raw_line in raw_lines:
            line_counter += 1
            if _LINE_NUM_RE.match(raw_line):
                continue  # skip pure line-number rows
            line = _clean_line(raw_line)
            if not line:
                continue

            speaker = _detect_speaker(line)
            if speaker and speaker != current_speaker and current_lines:
                # Speaker change → flush current chunk
                _flush(current_speaker, current_lines, current_page, current_line_start, line_counter)
                current_lines = [line + "\n"]
                current_speaker = speaker
                current_page = page_no
                current_line_start = line_counter
            else:
                if speaker:
                    current_speaker = speaker
                current_lines.append(line + "\n")
                # Split on size to keep chunks manageable
                joined = "".join(current_lines)
                if len(joined) >= 350:
                    _flush(current_speaker, current_lines, current_page, current_line_start, line_counter)
                    current_lines = []
                    current_page = page_no
                    current_line_start = line_counter

    if current_lines:
        _flush(current_speaker, current_lines, current_page, current_line_start, line_counter)

    return chunks


# ── Source string ─────────────────────────────────────────────────────────────

def _make_source(chunk: Dict[str, Any]) -> str:
    """Build a compact source string that fits VARCHAR(250)."""
    case = chunk["case_name"][:40]
    fname = chunk["file_name"][:30]
    date = chunk["date"]
    page = chunk["page"]
    speaker = chunk["speaker"]
    ttype = chunk["transcript_type"][:6]
    return f"{_SOURCE_PREFIX}|{case}|{ttype}|{date}|p{page}|{speaker}|{fname}"[:249]


# ── mem_bridge import ─────────────────────────────────────────────────────────

def _get_mem_bridge():
    try:
        from skills.memory.mem_bridge import remember_batch, recall as search_memory
        return remember_batch, search_memory
    except ImportError:
        try:
            sys.path.insert(0, str(MAGI_ROOT))
            from skills.memory.mem_bridge import remember_batch, recall as search_memory
            return remember_batch, search_memory
        except ImportError as e:
            raise RuntimeError(f"Cannot import mem_bridge: {e}")


# ── Index command ─────────────────────────────────────────────────────────────

def cmd_index(force: bool = False) -> str:
    """2026-04-25: 加 wall-clock budget + max-batch limit + 進度節流。

    NAS over Tailscale relay 下，每 PDF ~1.5-3s，602 個 PDF 會超過 cron 600s timeout。
    每輪 cron 限制：
      - TRANSCRIPT_BUDGET_SEC=480（8 min < 10 min cron）
      - TRANSCRIPT_MAX_PDFS_PER_RUN=150
      - 每 TRANSCRIPT_THROTTLE_EVERY=20 個 PDF sleep TRANSCRIPT_THROTTLE_SLEEP=0.3 秒
    """
    import time as _t
    remember_batch, _ = _get_mem_bridge()
    idx = _load_index()
    indexed_map = idx.get("indexed") or {}

    total_files = total_new_chunks = total_skipped = 0
    errors = []
    aborted_reason = ""
    t_start = _t.time()

    for pdf_path, case_name, subdir_name in _iter_transcript_pdfs():
        # Wall-clock budget guard
        if _t.time() - t_start > TRANSCRIPT_BUDGET_SEC:
            aborted_reason = f"budget_exhausted ({TRANSCRIPT_BUDGET_SEC}s)"
            print(f"[index] ⏱️ {aborted_reason} — 提前結束，下次 cron 會接續處理",
                  file=sys.stderr, flush=True)
            break
        # Max-batch guard
        if total_files >= TRANSCRIPT_MAX_PDFS_PER_RUN:
            aborted_reason = f"max_batch_reached ({TRANSCRIPT_MAX_PDFS_PER_RUN})"
            print(f"[index] 🛑 {aborted_reason} — 提前結束，下次 cron 會接續處理",
                  file=sys.stderr, flush=True)
            break

        pdf_key = str(pdf_path)
        try:
            mtime = str(pdf_path.stat().st_mtime) if pdf_path.exists() else ""
        except OSError:
            mtime = ""
        if not force and _is_transcript_indexed(pdf_key, mtime, indexed_map):
            total_skipped += 1
            continue

        total_files += 1
        print(f"[index] {total_files}: {pdf_path.name} ({case_name})", file=sys.stderr, flush=True)

        # 進度節流：每 N 個 PDF 喘息一次，避免連續 NAS I/O 衝爆
        if total_files > 0 and total_files % TRANSCRIPT_THROTTLE_EVERY == 0:
            _t.sleep(TRANSCRIPT_THROTTLE_SLEEP)

        # Extract date from filename
        m = _DATE_FROM_FILENAME_RE.search(pdf_path.stem)
        date_str = m.group(1) if m else ""
        if date_str and len(date_str) == 8:
            date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

        # Derive transcript type from filename
        tt_match = re.search(r"(訊問筆錄|審判筆錄|準備程序筆錄|勘驗筆錄|調查筆錄|程序筆錄)", pdf_path.name)
        transcript_type = tt_match.group(1) if tt_match else "筆錄"

        pages = _extract_pages(pdf_path)
        if not pages:
            errors.append(f"無法讀取: {pdf_path.name}")
            continue

        chunks = _parse_chunks(pages, pdf_path, case_name, date_str, transcript_type)
        if not chunks:
            continue

        # Prepare batch for mem_bridge
        items = [{"content": c["text"], "source": _make_source(c)} for c in chunks]

        # Batch insert in groups
        chunk_count = 0
        ok = True
        for i in range(0, len(items), BATCH_SIZE):
            batch = items[i : i + BATCH_SIZE]
            result = remember_batch(batch)
            if not (result or {}).get("ok"):
                ok = False
                errors.append(f"{pdf_path.name}: batch {i} failed ({result})")
                print(f"  [batch FAIL] {pdf_path.name} batch {i}: {result}", file=sys.stderr, flush=True)
            chunk_count += len(batch)
        print(f"  -> {chunk_count} chunks {'OK' if ok else 'PARTIAL'}", file=sys.stderr, flush=True)

        if ok:
            indexed_map[pdf_key] = {
                "mtime": mtime,
                "case_name": case_name,
                "file_name": pdf_path.name,
                "date": date_str,
                "chunks": chunk_count,
                "indexed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            total_new_chunks += chunk_count

    idx["indexed"] = indexed_map
    idx["stats"] = {
        "total_chunks": sum(v.get("chunks", 0) for v in indexed_map.values()),
        "total_files": len(indexed_map),
        "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_index(idx)

    elapsed = round(_t.time() - t_start, 1)
    lines = [
        f"筆錄索引{'完成' if not aborted_reason else '部分完成'}",
        f"- 本次新索引：{total_files} 份（{total_new_chunks} 段）",
        f"- 已跳過（無變動）：{total_skipped} 份",
        f"- 累計索引：{idx['stats']['total_files']} 份 / {idx['stats']['total_chunks']} 段",
        f"- 耗時：{elapsed}s",
    ]
    if aborted_reason:
        lines.append(f"- 提前結束：{aborted_reason}（剩餘 PDF 將於下次 cron 接續）")
    if errors:
        lines.append(f"- 錯誤（{len(errors)} 筆）：" + "；".join(errors[:5]))
    return "\n".join(lines)


# ── Query command ─────────────────────────────────────────────────────────────

def cmd_query(query: str, top_k: int = 8) -> str:
    if not query.strip():
        return json.dumps({"error": "查詢不可為空"}, ensure_ascii=False)

    _, search_memory = _get_mem_bridge()
    results = search_memory(query, top_k=top_k * 3, source_contains=_SOURCE_PREFIX)  # filter by transcript prefix

    transcript_results = []
    for r in (results or []):
        src = str(r.get("source") or "")
        if not src.startswith(_SOURCE_PREFIX + "|"):
            continue
        content = str(r.get("content") or "").strip()
        score = float(r.get("score") or r.get("similarity") or 0.0)
        # Parse source back
        parts = src.split("|")
        citation = {
            "score": round(score, 3),
            "content": content,
            "case_name": parts[1] if len(parts) > 1 else "",
            "transcript_type": parts[2] if len(parts) > 2 else "",
            "date": parts[3] if len(parts) > 3 else "",
            "page": parts[4] if len(parts) > 4 else "",
            "speaker": parts[5] if len(parts) > 5 else "",
            "file_name": parts[6] if len(parts) > 6 else "",
            "source": src,
        }
        transcript_results.append(citation)
        if len(transcript_results) >= top_k:
            break

    if not transcript_results:
        return json.dumps({"results": [], "message": "查無相關筆錄內容"}, ensure_ascii=False)

    # Human-readable output
    lines = [f"筆錄查詢結果（「{query}」，共 {len(transcript_results)} 條）：", ""]
    for i, r in enumerate(transcript_results, 1):
        lines.append(
            f"{i}. 【{r['case_name']}】{r['transcript_type']} {r['date']} {r['page']}"
            f"  發言人：{r['speaker']}"
        )
        lines.append(f"   {r['content'][:250]}")
        lines.append(f"   出處：{r['file_name']}")
        lines.append("")

    return json.dumps(
        {"results": transcript_results, "formatted": "\n".join(lines)},
        ensure_ascii=False,
        indent=2,
    )


def cmd_query_docx(query: str, top_k: int = 8) -> str:
    """查詢筆錄並輸出成 docx 表格（發言人｜時間｜內容）。"""
    raw = cmd_query(query, top_k=top_k)
    data = json.loads(raw)
    results = data.get("results") or []
    if not results:
        return raw

    try:
        if str(MAGI_ROOT) not in sys.path:
            sys.path.insert(0, str(MAGI_ROOT))
        from skills.ops.export_docx import export_transcript_docx  # type: ignore
    except Exception as e:
        data["docx_error"] = f"export_docx not available: {e}"
        return json.dumps(data, ensure_ascii=False, indent=2)

    segments = []
    for r in results:
        segments.append({
            "speaker": r.get("speaker") or "",
            "time": f"{r.get('date', '')} {r.get('page', '')}".strip(),
            "content": r.get("content") or "",
        })

    title = f"筆錄查詢結果：「{query}」"
    case_names = list({r.get("case_name", "") for r in results if r.get("case_name")})
    case_info = "｜".join(case_names) if case_names else ""

    docx_res = export_transcript_docx(
        segments,
        title=title,
        case_info=case_info,
        prefix="transcript_query",
    )
    if docx_res.get("success"):
        data["docx_exported"] = True
        data["docx_path"] = docx_res.get("path", "")
        data["docx_filename"] = docx_res.get("filename", "")
        data["docx_url"] = docx_res.get("url", "")
    else:
        data["docx_error"] = docx_res.get("error", "unknown")

    return json.dumps(data, ensure_ascii=False, indent=2)


# ── Status command ────────────────────────────────────────────────────────────

def cmd_status() -> str:
    idx = _load_index()
    stats = idx.get("stats") or {}
    indexed = idx.get("indexed") or {}

    # Group by case
    cases: Dict[str, int] = {}
    for info in indexed.values():
        cname = str(info.get("case_name") or "未知")
        cases[cname] = cases.get(cname, 0) + 1

    lines = [
        "筆錄向量庫狀態：",
        f"- 已索引檔案：{stats.get('total_files', 0)} 份",
        f"- 已索引段落：{stats.get('total_chunks', 0)} 段",
        f"- 最後更新：{stats.get('last_run', 'n/a')}",
        "",
        "各案件筆錄數量：",
    ]
    for cname, cnt in sorted(cases.items(), key=lambda x: -x[1]):
        lines.append(f"  {cname}：{cnt} 份")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="MAGI 筆錄向量化技能")
    ap.add_argument("--task", default="index", help="index|query|status")
    ap.add_argument("--query", default="", help="查詢語句（task=query 時使用）")
    ap.add_argument("--top_k", type=int, default=8, help="回傳結果數（default: 8）")
    ap.add_argument("--force", default="0", help="1=重新索引所有（忽略 mtime 快取）")
    args = ap.parse_args()

    task = str(args.task or "index").strip().lower()
    if task == "help":
        print(json.dumps({"skill": "transcript-indexer", "tasks": ["index", "query", "query_docx", "status"], "description": "MAGI 筆錄向量化索引與查詢"}, ensure_ascii=False, indent=2))
        return 0
    force = str(args.force or "0").strip().lower() in {"1", "true", "yes"}

    if task == "index":
        print(cmd_index(force=force))
    elif task == "query":
        print(cmd_query(args.query, top_k=args.top_k))
    elif task == "query_docx":
        print(cmd_query_docx(args.query, top_k=args.top_k))
    elif task == "status":
        print(cmd_status())
    else:
        print(f"未知 task: {task}，請使用 index|query|query_docx|status")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
