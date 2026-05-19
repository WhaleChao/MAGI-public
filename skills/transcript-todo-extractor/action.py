#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcript-todo-extractor
=========================
從法院筆錄 PDF 中擷取可建立 OSC 待辦的法官指示。

任務：
  dry_run  掃描筆錄並輸出高信心/待審候選，不寫 DB。
  apply    只寫入高信心待辦到 case_todos。
  status   顯示目前筆錄索引可用狀態。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

INDEX_DB_PATH = Path(
    os.environ.get("TRANSCRIPT_TODO_INDEX_DB", str(MAGI_ROOT / ".agent" / "transcript_index.json"))
)
DEFAULT_LIMIT = int(os.environ.get("TRANSCRIPT_TODO_LIMIT", "50") or "50")
TAIL_PAGES = int(os.environ.get("TRANSCRIPT_TODO_TAIL_PAGES", "3") or "3")

_CASE_NO_RE = re.compile(r"(20\d{2}-\d{4})")
_DATE8_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_ROC_DATE_RE = re.compile(r"(?:民國)?(?P<year>\d{2,4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日")
_TIME_RE = re.compile(
    r"(?P<ampm>上午|下午|晚上|早上|中午)?(?P<hour>\d{1,2})(?:時|點|[:：])(?P<minute>\d{1,2})?分?"
)
_RELATIVE_DEADLINE_RE = re.compile(
    r"(?P<days>\d{1,2})日內(?P<action>補正|陳報|提出|具狀|表示意見|陳述意見|補提|補具)"
)

_SCHEDULE_WORDS = ("定於", "訂於", "改在", "另定", "改期", "延展至")
_PROCEDURE_WORDS = ("準備程序", "言詞辯論", "審判", "審理", "調解", "宣判", "開庭", "訊問", "調查")
_ACTION_WORDS = (
    "補正",
    "陳報",
    "提出",
    "具狀",
    "表示意見",
    "陳述意見",
    "補提",
    "補具",
    "爭點",
    "證據能力",
    "攜帶",
    "帶同",
    "到庭說明",
)
_PRE_HEARING_HINTS = (
    "下次",
    "庭期前",
    "期日前",
    "開庭時",
    "到庭時",
    "庭上",
    "下次開庭時",
    "下次庭期",
    "審理時",
    "準備程序時",
    "言詞辯論時",
)
_RELEVANT_WORDS = _SCHEDULE_WORDS + _PROCEDURE_WORDS + _ACTION_WORDS + (
    "法官",
    "審判長",
    "諭知",
    "下次",
    "開庭時",
    "到庭時",
    "庭上",
    "庭期前",
    "期日前",
    "候核辦",
    "候核",
    "候辦",
)
_LOW_VALUE_WORDS = (
    "權利",
    "緘默",
    "出生",
    "年籍",
    "身分證",
    "住居所",
    "有何意見",
    "有無其他主張",
    "有無其他證據",
    "是否還有主張",
    "是否還有證據",
)


@dataclass
class TodoCandidate:
    type: str
    date: str
    time: str
    description: str
    source_file: str
    confidence: str
    rule: str
    page: int
    excerpt: str
    case_number: str = ""
    client_name: str = ""

    def to_insert_payload(self) -> dict[str, str]:
        return {
            "type": self.type,
            "date": self.date,
            "time": self.time,
            "description": self.description,
        }


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _clean_line(line: str) -> str:
    line = str(line or "").strip()
    line = re.sub(r"^\d{1,4}\s+", "", line)
    line = re.sub(r"\s+\d{1,4}\s*$", "", line)
    line = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", line)
    return line.strip()


def _brief(text: str, limit: int = 180) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    cleaned = re.sub(r"(?<!\d)(\d)\s+(\d)\s+(\d)\s*年", r"\1\2\3年", cleaned)
    cleaned = re.sub(r"(?<!\d)(\d)\s+(\d)\s*月", r"\1\2月", cleaned)
    cleaned = re.sub(r"(?<!\d)(\d)\s+(\d)\s*日", r"\1\2日", cleaned)
    cleaned = re.sub(r"(?<!\d)(\d)\s+(\d)\s*時", r"\1\2時", cleaned)
    cleaned = re.sub(r"(?<!\d)(\d)\s+(\d)\s*分", r"\1\2分", cleaned)
    cleaned = re.sub(r"(\d{2,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", r"\1年\2月\3日", cleaned)
    cleaned = re.sub(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", r"\1月\2日", cleaned)
    cleaned = re.sub(r"(\d{1,2})\s*時\s*(\d{1,2})\s*分", r"\1時\2分", cleaned)
    cleaned = re.sub(r"(上午|下午|晚上|早上|中午)\s*(\d{1,2})\s*時", r"\1\2時", cleaned)
    cleaned = re.sub(r"(?:^|\s)\d{1,3}(?=\s|$)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _date_from_filename(path: Path) -> str:
    m = _DATE8_RE.search(path.stem)
    if not m:
        return ""
    y, mo, d = m.groups()
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return ""


def _coerce_roc_date(year: int, month: int, day: int) -> Optional[date]:
    if year < 1911:
        year += 1911
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_date_time(text: str, *, fallback_year: Optional[int] = None) -> Tuple[str, str]:
    packed = _compact(text)
    for dm in _ROC_DATE_RE.finditer(packed):
        y = int(dm.group("year"))
        if y < 100 and fallback_year:
            y = fallback_year
        parsed_date = _coerce_roc_date(y, int(dm.group("month")), int(dm.group("day")))
        if not parsed_date:
            continue
        tail = packed[dm.end() : dm.end() + 28]
        parsed_time = ""
        tm = _TIME_RE.search(tail)
        if tm:
            hour = int(tm.group("hour"))
            minute = int(tm.group("minute") or 0)
            ampm = tm.group("ampm") or ""
            if ampm in {"下午", "晚上"} and hour < 12:
                hour += 12
            elif ampm in {"上午", "早上"} and hour == 12:
                hour = 0
            elif ampm == "中午" and hour < 12:
                hour += 12
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                parsed_time = f"{hour:02d}:{minute:02d}"
        return parsed_date.isoformat(), parsed_time
    return "", ""


def _next_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _subtract_business_days(d: date, days: int) -> date:
    out = d
    remaining = max(0, days)
    while remaining:
        out -= timedelta(days=1)
        if out.weekday() < 5:
            remaining -= 1
    return out


def _add_days_with_weekend_adjust(d: date, days: int) -> date:
    return _next_business_day(d + timedelta(days=days))


def _parse_iso_date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value)
    except Exception:
        return None


def _infer_case_identity(path: Path) -> tuple[str, str]:
    for part in [path.parent, *path.parents]:
        name = part.name
        m = _CASE_NO_RE.search(name)
        if not m:
            continue
        case_number = m.group(1)
        rest = name[m.end() :].lstrip("-")
        client_name = ""
        for marker in (
            "-偵查-",
            "-一審-",
            "-二審-",
            "-三審-",
            "-更審-",
            "-再審-",
            "-調解-",
            "-清算-",
            "-更生-",
            "-執行-",
            "-抗告-",
            "-聲請-",
            "-行政-",
            "-民事-",
            "-刑事-",
            "-非訟-",
        ):
            idx = rest.find(marker)
            if idx > 0:
                client_name = rest[:idx].strip("- ")
                break
        if not client_name:
            pieces = rest.split("-")
            client_name = pieces[0].strip() if pieces else ""
        return case_number, client_name
    return "", ""


def _extract_pages(pdf_path: Path) -> list[tuple[int, str]]:
    try:
        import fitz  # PyMuPDF
    except Exception as exc:
        raise RuntimeError(f"PyMuPDF unavailable: {exc}") from exc
    doc = fitz.open(str(pdf_path))
    try:
        return [(idx + 1, page.get_text()) for idx, page in enumerate(doc)]
    finally:
        doc.close()


def _candidate_segments(pages: list[tuple[int, str]], *, tail_pages: int = TAIL_PAGES) -> list[tuple[int, str]]:
    if not pages:
        return []
    selected = pages[-max(1, tail_pages) :]
    segments: list[tuple[int, str]] = []
    seen: set[str] = set()
    for page_no, page_text in selected:
        lines = [_clean_line(x) for x in str(page_text or "").splitlines()]
        lines = [x for x in lines if x]
        for i, line in enumerate(lines):
            packed_line = _compact(line)
            if not any(word in packed_line for word in _RELEVANT_WORDS):
                continue
            window = " ".join(lines[max(0, i - 1) : min(len(lines), i + 4)])
            packed_window = _compact(window)
            if packed_window in seen:
                continue
            seen.add(packed_window)
            segments.append((page_no, window))
    return segments


def _procedure_type(text: str) -> str:
    packed = _compact(text).replace("審判長", "")
    if "辯論終結" in packed and ("宣判" in packed or "定" in packed or "訂" in packed):
        return "宣判"
    for word in ("宣判", "調解", "言詞辯論", "準備程序", "審理", "審判", "訊問", "調查"):
        if word in packed:
            return word
    return "開庭"


def _is_low_value_segment(text: str) -> bool:
    packed = _compact(text)
    if "諭知" in packed or "定於" in packed or "訂於" in packed or "候核" in packed:
        return False
    return any(word in packed for word in _LOW_VALUE_WORDS)


def _description(todo_type: str, pdf_name: str, page: int, excerpt: str, *, prefix: str = "筆錄法官指示") -> str:
    return (
        f"{prefix}：{todo_type}\n"
        f"來源筆錄：{pdf_name} 第{page}頁\n"
        f"原文：{_brief(excerpt)}"
    )


def extract_candidates_from_pages(
    pages: list[tuple[int, str]],
    *,
    pdf_path: Path,
    transcript_date: str = "",
    case_number: str = "",
    client_name: str = "",
    tail_pages: int = TAIL_PAGES,
) -> list[TodoCandidate]:
    """Return high/review candidates. Pure enough for unit tests."""
    transcript_date = transcript_date or _date_from_filename(pdf_path)
    transcript_day = _parse_iso_date(transcript_date)
    fallback_year = transcript_day.year if transcript_day else None
    segments = _candidate_segments(pages, tail_pages=tail_pages)
    candidates: list[TodoCandidate] = []
    next_hearing: Optional[TodoCandidate] = None
    dedupe: set[tuple[str, str, str, str, int]] = set()

    def _append(candidate: TodoCandidate) -> None:
        key = (candidate.type, candidate.date, candidate.time, _compact(candidate.excerpt)[:90], candidate.page)
        if key in dedupe:
            return
        dedupe.add(key)
        candidates.append(candidate)

    for page_no, segment in segments:
        if _is_low_value_segment(segment):
            continue
        packed = _compact(segment)
        if "候核辦" in packed or "候核" in packed or "候辦" in packed:
            if transcript_day:
                due = _add_days_with_weekend_adjust(transcript_day, 7)
                source = f"{pdf_path.name}#p{page_no}:candidate:review7"
                _append(
                    TodoCandidate(
                        type="追蹤",
                        date=due.isoformat(),
                        time="",
                        description=_description(
                            "候核辦後續追蹤",
                            pdf_path.name,
                            page_no,
                            segment,
                            prefix="筆錄記載候核辦，無下次庭期，請於7日後追蹤",
                        ),
                        source_file=source,
                        confidence="high",
                        rule="candidate_review_7_days",
                        page=page_no,
                        excerpt=_brief(segment),
                        case_number=case_number,
                        client_name=client_name,
                    )
                )
            else:
                source = f"{pdf_path.name}#p{page_no}:candidate:review_missing_date"
                _append(
                    TodoCandidate(
                        type="追蹤",
                        date="",
                        time="",
                        description=_description("候核辦後續追蹤", pdf_path.name, page_no, segment),
                        source_file=source,
                        confidence="review",
                        rule="candidate_review_needs_transcript_date",
                        page=page_no,
                        excerpt=_brief(segment),
                        case_number=case_number,
                        client_name=client_name,
                    )
                )
            continue

        parsed_date, parsed_time = _parse_date_time(segment, fallback_year=fallback_year)
        has_schedule_word = any(word in packed for word in _SCHEDULE_WORDS)
        has_procedure = any(word in packed for word in _PROCEDURE_WORDS)
        if parsed_date and (has_schedule_word or has_procedure) and ("日內" not in packed):
            todo_type = _procedure_type(segment)
            source = f"{pdf_path.name}#p{page_no}:hearing:{parsed_date}:{parsed_time or 'all-day'}"
            candidate = TodoCandidate(
                type=todo_type,
                date=parsed_date,
                time=parsed_time,
                description=_description(todo_type, pdf_path.name, page_no, segment),
                source_file=source,
                confidence="high",
                rule="scheduled_hearing",
                page=page_no,
                excerpt=_brief(segment),
                case_number=case_number,
                client_name=client_name,
            )
            _append(candidate)
            if not next_hearing:
                next_hearing = candidate

        rel = _RELATIVE_DEADLINE_RE.search(packed)
        if rel and transcript_day:
            days = int(rel.group("days"))
            action = rel.group("action")
            due = _add_days_with_weekend_adjust(transcript_day, days)
            source = f"{pdf_path.name}#p{page_no}:deadline:{days}:{action}"
            _append(
                TodoCandidate(
                    type=action,
                    date=due.isoformat(),
                    time="",
                    description=_description(f"{days}日內{action}", pdf_path.name, page_no, segment),
                    source_file=source,
                    confidence="high",
                    rule="relative_deadline",
                    page=page_no,
                    excerpt=_brief(segment),
                    case_number=case_number,
                    client_name=client_name,
                )
            )
            continue

        if any(hint in packed for hint in _PRE_HEARING_HINTS) and any(word in packed for word in _ACTION_WORDS):
            if next_hearing and next_hearing.date:
                hearing_day = _parse_iso_date(next_hearing.date)
                if hearing_day:
                    due = _subtract_business_days(hearing_day, 7)
                    source = f"{pdf_path.name}#p{page_no}:prehearing:{next_hearing.date}"
                    _append(
                        TodoCandidate(
                            type="庭前準備",
                            date=due.isoformat(),
                            time="",
                            description=_description("庭前準備", pdf_path.name, page_no, segment),
                            source_file=source,
                            confidence="high",
                            rule="pre_hearing_seven_business_days",
                            page=page_no,
                            excerpt=_brief(segment),
                            case_number=case_number,
                            client_name=client_name,
                        )
                    )
                    continue
            source = f"{pdf_path.name}#p{page_no}:prehearing:review"
            _append(
                TodoCandidate(
                    type="庭前準備",
                    date="",
                    time="",
                    description=_description("庭前準備", pdf_path.name, page_no, segment),
                    source_file=source,
                    confidence="review",
                    rule="pre_hearing_needs_date",
                    page=page_no,
                    excerpt=_brief(segment),
                    case_number=case_number,
                    client_name=client_name,
                )
            )
            continue

        if any(word in packed for word in _ACTION_WORDS) and ("法官" in packed or "審判長" in packed or "諭知" in packed):
            source = f"{pdf_path.name}#p{page_no}:action:review"
            _append(
                TodoCandidate(
                    type="待確認",
                    date="",
                    time="",
                    description=_description("待確認", pdf_path.name, page_no, segment),
                    source_file=source,
                    confidence="review",
                    rule="action_without_due_date",
                    page=page_no,
                    excerpt=_brief(segment),
                    case_number=case_number,
                    client_name=client_name,
                )
            )

    return candidates


def extract_candidates_from_pdf(pdf_path: Path, *, tail_pages: int = TAIL_PAGES) -> list[TodoCandidate]:
    case_number, client_name = _infer_case_identity(pdf_path)
    pages = _extract_pages(pdf_path)
    return extract_candidates_from_pages(
        pages,
        pdf_path=pdf_path,
        transcript_date=_date_from_filename(pdf_path),
        case_number=case_number,
        client_name=client_name,
        tail_pages=tail_pages,
    )


def _iter_index_paths(limit: int) -> Iterable[Path]:
    if not INDEX_DB_PATH.exists():
        return []
    try:
        data = json.loads(INDEX_DB_PATH.read_text("utf-8"))
    except Exception:
        return []
    rows: list[tuple[float, Path]] = []
    for key, info in (data.get("indexed") or {}).items():
        p = Path(key)
        if p.exists() and p.suffix.lower() == ".pdf":
            try:
                mtime = float((info or {}).get("mtime") or p.stat().st_mtime or 0)
            except Exception:
                mtime = 0
            rows.append((mtime, p))
        if len(rows) >= max(limit * 5, limit):
            break
    rows.sort(key=lambda it: it[0], reverse=True)
    return [p for _, p in rows[:limit]]


def _iter_pdf_targets(raw_path: str, *, limit: int) -> list[Path]:
    if raw_path:
        root = Path(raw_path).expanduser()
        if root.is_file() and root.suffix.lower() == ".pdf":
            return [root]
        if root.is_dir():
            out: list[Path] = []
            for p in root.rglob("*.pdf"):
                if any(part.startswith(".") for part in p.parts):
                    continue
                out.append(p)
                if len(out) >= limit:
                    break
            return out
        return []
    return list(_iter_index_paths(limit))


def scan_targets(paths: list[Path], *, tail_pages: int = TAIL_PAGES) -> dict[str, Any]:
    items: list[TodoCandidate] = []
    errors: list[dict[str, str]] = []
    scanned = 0
    for path in paths:
        try:
            candidates = extract_candidates_from_pdf(path, tail_pages=tail_pages)
            scanned += 1
            items.extend(candidates)
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    items = _dedupe_candidates(items)
    high = [x for x in items if x.confidence == "high"]
    review = [x for x in items if x.confidence == "review"]
    return {
        "ok": True,
        "scanned": scanned,
        "high_count": len(high),
        "review_count": len(review),
        "errors_count": len(errors),
        "items": [asdict(x) for x in items],
        "errors": errors,
    }


def _dedupe_candidates(items: list[TodoCandidate]) -> list[TodoCandidate]:
    def _priority(item: TodoCandidate) -> tuple[int, int, int]:
        text = _compact(item.excerpt)
        source = _compact(item.source_file)
        return (
            0 if item.confidence == "high" else 1,
            0 if ("諭知" in text or "宣示" in text) else 1,
            0 if "#p" in source else 1,
        )

    out: list[TodoCandidate] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for item in sorted(items, key=_priority):
        if item.rule == "scheduled_hearing":
            key = (item.case_number, item.rule, item.type, item.date, item.time)
        elif item.rule == "candidate_review_7_days":
            key = (item.case_number, item.rule, item.type, item.date, "")
        elif item.rule == "pre_hearing_seven_business_days":
            key = (item.case_number, item.rule, item.type, item.date, "")
        else:
            key = (item.case_number, item.rule, item.type, item.date, _compact(item.excerpt)[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def apply_high_confidence(items: list[dict[str, Any]], *, include_past: bool = False) -> dict[str, Any]:
    sys.path.insert(0, str(MAGI_ROOT / "skills" / "osc-orchestrator"))
    from osc_headless.db import connect_mysql, db_config_from_env, ensure_osc_min_schema, insert_case_todos

    conn = connect_mysql(db_config_from_env())
    inserted = skipped = updated = 0
    written: list[dict[str, Any]] = []
    past_skipped = 0
    today = date.today()
    try:
        ensure_osc_min_schema(conn)
        for item in items:
            if item.get("confidence") != "high":
                continue
            case_number = str(item.get("case_number") or "").strip()
            if not case_number:
                continue
            todo_date = _parse_iso_date(str(item.get("date") or ""))
            if todo_date and todo_date < today and not include_past:
                past_skipped += 1
                continue
            todo = {
                "type": str(item.get("type") or "待辦"),
                "date": str(item.get("date") or ""),
                "time": str(item.get("time") or ""),
                "description": str(item.get("description") or ""),
            }
            result = insert_case_todos(
                conn,
                case_number=case_number,
                client_name=str(item.get("client_name") or ""),
                todos=[todo],
                source_file=str(item.get("source_file") or "transcript_todo"),
                allow_duplicates=False,
                commit=False,
            )
            inserted += int(result.get("inserted") or 0)
            skipped += int(result.get("skipped") or 0)
            updated += int(result.get("updated") or 0)
            written.append({"case_number": case_number, "todo": todo, "result": result})
        conn.commit()
    finally:
        conn.close()
    return {
        "inserted": inserted,
        "skipped": skipped,
        "updated": updated,
        "past_skipped": past_skipped,
        "written": written,
    }


def cmd_status() -> dict[str, Any]:
    indexed = 0
    existing = 0
    if INDEX_DB_PATH.exists():
        try:
            data = json.loads(INDEX_DB_PATH.read_text("utf-8"))
            indexed = len(data.get("indexed") or {})
            for key in (data.get("indexed") or {}).keys():
                if Path(key).exists():
                    existing += 1
        except Exception:
            pass
    return {"ok": True, "index_path": str(INDEX_DB_PATH), "indexed": indexed, "existing": existing}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI 筆錄待辦抽取器")
    parser.add_argument("--task", choices=["dry_run", "apply", "status"], default="dry_run")
    parser.add_argument("--path", default="", help="單一 PDF 或筆錄資料夾；未提供時使用 transcript_index.json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--tail-pages", type=int, default=TAIL_PAGES)
    parser.add_argument("--json", action="store_true", help="輸出完整 JSON")
    parser.add_argument("--include-past", action="store_true", help="apply 時允許寫入已逾期項目")
    args = parser.parse_args(argv)

    if args.task == "status":
        payload = cmd_status()
    else:
        paths = _iter_pdf_targets(args.path, limit=max(1, args.limit))
        payload = scan_targets(paths, tail_pages=max(1, args.tail_pages))
        if args.task == "apply":
            payload["write_result"] = apply_high_confidence(
                payload.get("items") or [],
                include_past=bool(args.include_past),
            )

    if args.json or args.task in {"apply", "status"}:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"筆錄待辦 dry-run：掃描 {payload['scanned']} 份，高信心 {payload['high_count']} 筆，"
            f"待審 {payload['review_count']} 筆，錯誤 {payload['errors_count']} 筆"
        )
        for item in (payload.get("items") or [])[:20]:
            print(
                f"- [{item['confidence']}] {item['case_number'] or '未辨識案件'} "
                f"{item['type']} {item['date']} {item['time']}｜{item['excerpt']}"
            )
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
