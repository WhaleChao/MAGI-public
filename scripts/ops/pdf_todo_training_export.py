#!/usr/bin/env python3
"""Export PDF/transcript todo extraction outcomes as training samples.

The operational todo refresh writes only high-confidence tasks.  This exporter
keeps a separate learning record for court notices, procedural rulings,
judgments, and transcripts so future rule changes can be tested against real
misses without re-reading the whole NAS every time.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import signal
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUT_JSONL = ROOT / ".runtime" / "pdf_todo_training_latest.jsonl"
OUT_SUMMARY = ROOT / ".runtime" / "pdf_todo_training_summary.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skills.bridge.shared_utils.judgment_folder_names import JUDGMENT_FOLDER_LABEL, path_has_judgment_folder


class _TrainingScanTimeout(TimeoutError):
    pass


@contextlib.contextmanager
def _time_limit(seconds: int):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handle(_signum, _frame):
        raise _TrainingScanTimeout(f"training_scan_timeout:{seconds}s")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handle)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _load_transcript_module():
    path = ROOT / "skills" / "transcript-todo-extractor" / "action.py"
    spec = importlib.util.spec_from_file_location("_magi_transcript_todo_training", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _folder_kind(path: Path) -> str:
    text = str(path)
    if "筆錄" in text:
        return "筆錄"
    if path_has_judgment_folder(text):
        return JUDGMENT_FOLDER_LABEL
    if "程序裁定" in text:
        return "程序裁定"
    if "法院通知" in text or "法院_通知" in text or "法院_傳票" in text:
        return "法院通知或程序裁定"
    return "其他PDF"


def _training_label_for_pdf(item: dict[str, Any]) -> str:
    todos = item.get("todos") or []
    name = str(item.get("file_name") or "")
    text_error = str(item.get("text_error") or "")
    if todos:
        return "todo_extracted"
    try:
        from api.blueprints.osc_pdf import _PDF_TODO_HINT_RE

        if _PDF_TODO_HINT_RE.search(name):
            return "todo_like_name_no_hit"
    except Exception:
        pass
    if text_error and text_error not in {"skipped_text_bulk_scan", "skipped_text_filename_todos"}:
        return "needs_text_or_ocr_review"
    return "no_todo_expected"


def _training_label_for_transcript(path: Path, candidates: list[Any], error: str = "") -> str:
    if error:
        return "transcript_read_error"
    if candidates:
        high = [x for x in candidates if getattr(x, "confidence", "") == "high"]
        return "transcript_high_confidence_todo" if high else "transcript_review_candidate"
    return "transcript_no_todo_expected"


def collect_pdf_rows(*, limit: int, max_pages: int, scan_text: bool, file_timeout_sec: int = 15) -> list[dict[str, Any]]:
    from api.blueprints import osc_pdf

    rows: list[dict[str, Any]] = []
    for path, case_number, client_name in osc_pdf._iter_all_case_pdf_targets(limit=limit):
        try:
            with _time_limit(file_timeout_sec):
                item = osc_pdf._scan_pdf_for_calendar(
                    path,
                    case_number=case_number,
                    client_name=client_name,
                    max_pages=max_pages,
                    include_share_link=False,
                    scan_text=scan_text,
                    text_when_filename=True,
                )
        except Exception as exc:
            item = {
                "case_number": case_number,
                "client_name": client_name,
                "file_name": path.name,
                "todos": [],
                "events": [],
                "text_available": False,
                "text_error": f"{type(exc).__name__}: {str(exc)[:180]}",
            }
        rows.append(
            {
                "source_kind": "court_pdf",
                "folder_kind": _folder_kind(path),
                "case_number": item.get("case_number") or case_number,
                "client_name": item.get("client_name") or client_name,
                "path": str(path),
                "file_name": path.name,
                "training_label": _training_label_for_pdf(item),
                "todo_count": len(item.get("todos") or []),
                "event_count": len(item.get("events") or []),
                "text_available": bool(item.get("text_available")),
                "text_error": item.get("text_error") or "",
                "todos": (item.get("todos") or [])[:5],
            }
        )
    return rows


def collect_transcript_rows(*, limit: int, tail_pages: int) -> list[dict[str, Any]]:
    mod = _load_transcript_module()
    rows: list[dict[str, Any]] = []
    for path in mod._iter_pdf_targets("", limit=limit):
        error = ""
        candidates: list[Any] = []
        try:
            candidates = mod.extract_candidates_from_pdf(path, tail_pages=tail_pages)
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:220]}"
        rows.append(
            {
                "source_kind": "transcript_pdf",
                "folder_kind": "筆錄",
                "case_number": candidates[0].case_number if candidates else "",
                "client_name": candidates[0].client_name if candidates else "",
                "path": str(path),
                "file_name": path.name,
                "training_label": _training_label_for_transcript(path, candidates, error),
                "todo_count": len(candidates),
                "high_count": len([x for x in candidates if getattr(x, "confidence", "") == "high"]),
                "review_count": len([x for x in candidates if getattr(x, "confidence", "") == "review"]),
                "error": error,
                "todos": [
                    {
                        "type": x.type,
                        "date": x.date,
                        "time": x.time,
                        "confidence": x.confidence,
                        "rule": x.rule,
                        "source_file": x.source_file,
                        "excerpt": x.excerpt,
                    }
                    for x in candidates[:5]
                ],
            }
        )
    return rows


def write_outputs(rows: list[dict[str, Any]], *, jsonl_out: Path, summary_out: Path) -> dict[str, Any]:
    jsonl_out.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    labels = Counter(str(row.get("training_label") or "") for row in rows)
    folder_kinds = Counter(str(row.get("folder_kind") or "") for row in rows)
    summary = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": len(rows),
        "labels": dict(labels),
        "folder_kinds": dict(folder_kinds),
        "todo_rows": len([row for row in rows if int(row.get("todo_count") or 0) > 0]),
        "jsonl_out": str(jsonl_out),
    }
    tmp = summary_out.with_suffix(summary_out.suffix + ".tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(summary_out)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="匯出法院 PDF 與筆錄待辦抽取訓練樣本")
    parser.add_argument("--pdf-limit", type=int, default=120)
    parser.add_argument("--transcript-limit", type=int, default=80)
    parser.add_argument("--pdf-max-pages", type=int, default=8)
    parser.add_argument("--transcript-tail-pages", type=int, default=3)
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--skip-transcript", action="store_true")
    parser.add_argument("--no-text", action="store_true")
    parser.add_argument("--file-timeout-sec", type=int, default=15)
    parser.add_argument("--jsonl-out", default=str(OUT_JSONL))
    parser.add_argument("--summary-out", default=str(OUT_SUMMARY))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows: list[dict[str, Any]] = []
    if not args.skip_pdf:
        rows.extend(
            collect_pdf_rows(
                limit=max(1, args.pdf_limit),
                max_pages=max(1, args.pdf_max_pages),
                scan_text=not args.no_text,
                file_timeout_sec=max(1, args.file_timeout_sec),
            )
        )
    if not args.skip_transcript:
        rows.extend(collect_transcript_rows(limit=max(1, args.transcript_limit), tail_pages=max(1, args.transcript_tail_pages)))
    summary = write_outputs(rows, jsonl_out=Path(args.jsonl_out), summary_out=Path(args.summary_out))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
