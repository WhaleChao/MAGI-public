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
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))
_AGENT_DIR = Path(os.environ.get("MAGI_AGENT_DIR", "").strip() or MAGI_ROOT / ".agent").expanduser()

INDEX_DB_PATH = Path(
    os.environ.get("TRANSCRIPT_TODO_INDEX_DB", str(_AGENT_DIR / "transcript_index.json"))
)
DEFAULT_LIMIT = int(os.environ.get("TRANSCRIPT_TODO_LIMIT", "50") or "50")
TAIL_PAGES = int(os.environ.get("TRANSCRIPT_TODO_TAIL_PAGES", "3") or "3")
RECENT_DAYS = int(os.environ.get("TRANSCRIPT_TODO_RECENT_DAYS", "45") or "45")
LISTING_BUDGET_SEC = int(os.environ.get("TRANSCRIPT_TODO_LISTING_BUDGET_SEC", "120") or "120")
PDF_TIMEOUT_SEC = int(os.environ.get("TRANSCRIPT_TODO_PDF_TIMEOUT_SEC", "60") or "60")
_PDF_WORKER_MARKER = "__MAGI_TRANSCRIPT_PAGES_JSON__"
_TRANSCRIPT_SUBDIRS = [
    s.strip()
    for s in (
        os.environ.get("TRANSCRIPT_TODO_DIRS")
        or os.environ.get("TRANSCRIPT_DIRS")
        or "05_筆錄,06_筆錄,07_筆錄,08_筆錄"
    ).split(",")
    if s.strip()
]

_CASE_NO_RE = re.compile(r"(20\d{2}-\d{4})")
_COURT_CASE_NO_RE = re.compile(
    r"(?P<year>\d{2,3})年度(?P<word>[\u4e00-\u9fff]{1,12}?字)第0*(?P<serial>\d{1,8})號"
)
_COURT_CASE_ROC_YEAR_MIN = 80
_COURT_CASE_ROC_YEAR_MAX = 130
_COURT_CASE_SERIAL_MAX = 999_999
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


def _safe_path_call(fn, *, timeout_sec: float = 2.0, default=None):
    box: dict[str, Any] = {"value": default}

    def _runner() -> None:
        try:
            box["value"] = fn()
        except Exception:
            box["value"] = default

    t = threading.Thread(target=_runner, daemon=True, name="transcript-todo-path-probe")
    t.start()
    t.join(timeout_sec)
    return default if t.is_alive() else box.get("value", default)


def _safe_exists(path: Path) -> bool:
    return bool(_safe_path_call(lambda: path.exists(), default=False))


def _safe_is_file(path: Path) -> bool:
    return bool(_safe_path_call(lambda: path.is_file(), default=False))


def _safe_is_dir(path: Path) -> bool:
    return bool(_safe_path_call(lambda: path.is_dir(), default=False))


def _safe_stat_mtime(path: Path) -> float:
    return float(_safe_path_call(lambda: path.stat().st_mtime, timeout_sec=1.5, default=0.0) or 0.0)


def _safe_child_dirs(path: Path) -> list[Path]:
    return list(
        _safe_path_call(
            lambda: [x for x in path.iterdir() if x.is_dir() and not x.name.startswith(".")],
            timeout_sec=3.0,
            default=[],
        )
        or []
    )


def _safe_pdf_glob(path: Path, *, limit: int) -> list[Path]:
    out = list(
        _safe_path_call(
            lambda: [p for p in path.glob("*.pdf") if not p.name.startswith(".") and not p.name.startswith("~$")][:limit],
            timeout_sec=4.0,
            default=[],
        )
        or []
    )
    return out[:limit]


def _safe_pdf_rglob(path: Path, *, limit: int) -> list[Path]:
    def _scan() -> list[Path]:
        out: list[Path] = []
        for p in path.rglob("*.pdf"):
            if any(part.startswith(".") for part in p.parts):
                continue
            out.append(p)
            if len(out) >= limit:
                break
        return out

    return list(_safe_path_call(_scan, timeout_sec=8.0, default=[]) or [])[:limit]


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
    court_case_numbers: tuple[str, ...] = ()

    def to_insert_payload(self) -> dict[str, str]:
        return {
            "type": self.type,
            "date": self.date,
            "time": self.time,
            "description": self.description,
        }


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _normalize_court_case_number(value: str) -> str:
    """Return a plausible Taiwan court docket number, otherwise fail closed.

    OCR commonly joins page numbers or unrelated numeric text to a docket.
    A value outside the modern ROC-year range or with an implausibly long
    serial must never be used to rebind a transcript to another OSC case.
    """
    match = _COURT_CASE_NO_RE.search(_compact(value))
    if not match:
        return ""
    year = int(match.group("year"))
    serial = int(match.group("serial"))
    if not (_COURT_CASE_ROC_YEAR_MIN <= year <= _COURT_CASE_ROC_YEAR_MAX):
        return ""
    if not (0 < serial <= _COURT_CASE_SERIAL_MAX):
        return ""
    return f"{year}年度{match.group('word')}第{serial}號"


def _extract_court_case_numbers(pages: list[tuple[int, str]]) -> tuple[str, ...]:
    found: list[str] = []
    for _page, text in pages:
        for match in _COURT_CASE_NO_RE.finditer(_compact(text)):
            value = _normalize_court_case_number(match.group(0))
            if not value:
                continue
            if value not in found:
                found.append(value)
    return tuple(found)


def _resolve_case_identity_from_rows(
    *,
    source_case_number: str,
    client_name: str,
    court_case_numbers: Iterable[str],
    rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve by transcript content; never move a case from name matching alone."""
    candidates = [dict(row or {}) for row in rows]
    source = next(
        (row for row in candidates if str(row.get("case_number") or "").strip() == source_case_number),
        {},
    )
    wanted = {_normalize_court_case_number(value) for value in court_case_numbers}
    wanted.discard("")
    exact = [
        row
        for row in candidates
        if _normalize_court_case_number(str(row.get("court_case_number") or "")) in wanted
    ]
    if len(exact) == 1:
        target = exact[0]
        target_no = str(target.get("case_number") or source_case_number).strip()
        return {
            "case_number": target_no,
            "client_name": str(target.get("client_name") or client_name).strip(),
            "source_case_number": source_case_number,
            "court_case_numbers": sorted(wanted),
            "reason": "exact_court_case_number",
            "rebound": target_no != source_case_number,
        }
    reason = "ambiguous_exact_court_case_number" if len(exact) > 1 else "source_folder_fallback"
    return {
        "case_number": source_case_number,
        "client_name": str(source.get("client_name") or client_name).strip(),
        "source_case_number": source_case_number,
        "court_case_numbers": sorted(wanted),
        "reason": reason,
        "rebound": False,
    }


def _resolve_case_identity_db(
    conn: Any,
    *,
    source_case_number: str,
    client_name: str,
    court_case_numbers: Iterable[str],
) -> dict[str, Any]:
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT case_number, client_name, court_case_number, status, case_stage, folder_path
              FROM cases
             WHERE case_number=%s
                OR (%s<>'' AND client_name=%s)
            """,
            (source_case_number, client_name, client_name),
        )
        rows = [dict(row or {}) for row in (cur.fetchall() or [])]
    finally:
        cur.close()
    return _resolve_case_identity_from_rows(
        source_case_number=source_case_number,
        client_name=client_name,
        court_case_numbers=court_case_numbers,
        rows=rows,
    )


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
        current_roc_year = date.today().year - 1911
        if year < 80 or year > current_roc_year + 3:
            return None
        year += 1911
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _is_plausible_todo_date(value: date) -> bool:
    return value <= date.today() + timedelta(days=730)


def _parse_date_time(text: str, *, fallback_year: Optional[int] = None) -> Tuple[str, str]:
    packed = _compact(text)
    for dm in _ROC_DATE_RE.finditer(packed):
        y = int(dm.group("year"))
        if y < 100 and fallback_year:
            y = fallback_year
        parsed_date = _coerce_roc_date(y, int(dm.group("month")), int(dm.group("day")))
        if not parsed_date:
            continue
        if not _is_plausible_todo_date(parsed_date):
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


def _extract_pages_inner(pdf_path: Path) -> list[tuple[int, str]]:
    try:
        import fitz  # PyMuPDF
    except Exception as exc:
        raise RuntimeError(f"PyMuPDF unavailable: {exc}") from exc
    doc = fitz.open(str(pdf_path))
    try:
        return [(idx + 1, page.get_text()) for idx, page in enumerate(doc)]
    finally:
        doc.close()


def _extract_pages(pdf_path: Path) -> list[tuple[int, str]]:
    """Extract text in a killable subprocess with a bounded wall-clock timeout.

    A Python thread cannot be stopped after a timeout.  The previous
    implementation therefore left timed-out PyMuPDF threads alive and opened
    the same NAS PDF again on the next retry.  MuPDF is not safe under that
    overlap: two independent macOS crash reports show SIGSEGV/SIGBUS in
    ``fz_open_document`` for the same transcript.  Process isolation contains
    both a native crash and a stalled SMB read to the one input PDF.
    """

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_extract-pages-worker",
        str(pdf_path),
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, PDF_TIMEOUT_SEC),
            check=False,
            env=env,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"transcript_pdf_timeout:{PDF_TIMEOUT_SEC}s") from exc

    stdout = completed.stdout or ""
    marker_at = stdout.rfind(_PDF_WORKER_MARKER)
    payload: dict[str, Any] = {}
    if marker_at >= 0:
        raw_payload = stdout[marker_at + len(_PDF_WORKER_MARKER) :].strip()
        try:
            decoded = json.loads(raw_payload)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = {}

    if completed.returncode != 0:
        if completed.returncode < 0:
            try:
                signal_name = signal.Signals(-completed.returncode).name
            except (ValueError, OSError):
                signal_name = f"SIGNAL_{-completed.returncode}"
            raise RuntimeError(f"transcript_pdf_worker_crashed:{signal_name}")
        child_error = str(payload.get("error") or "").strip()
        if not child_error:
            child_error = (completed.stderr or "").strip()[-600:]
        raise RuntimeError(
            f"transcript_pdf_worker_failed:rc={completed.returncode}:"
            f"{child_error or 'unknown error'}"
        )

    pages = payload.get("pages")
    if payload.get("ok") is not True or not isinstance(pages, list):
        raise RuntimeError("transcript_pdf_worker_invalid_result")
    normalized: list[tuple[int, str]] = []
    for row in pages:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise RuntimeError("transcript_pdf_worker_invalid_page")
        normalized.append((int(row[0]), str(row[1] or "")))
    return normalized


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


def _explicit_absolute_deadline_action(text: str) -> str:
    """Return the concrete action tied to an absolute date, if unambiguous.

    A bare ``候核辦`` is a procedural state, not work for counsel.  The
    transcript must contain an actual directive, an absolute date, a deadline
    cue, and an action before MAGI may create a calendar item.
    """

    packed = _compact(text)
    date_match = _ROC_DATE_RE.search(packed)
    if not date_match:
        return ""
    before = packed[max(0, date_match.start() - 48) : date_match.start()]
    after = packed[date_match.end() : date_match.end() + 120]
    if not any(word in before + after for word in ("請", "應", "命", "諭知", "須", "限")):
        return ""
    if not any(word in after[:24] for word in ("前", "以前", "截止", "期限", "最遲", "內")):
        return ""
    matches = [
        (after.find(action), action)
        for action in _ACTION_WORDS
        if after.find(action) >= 0
    ]
    return min(matches)[1] if matches else ""


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
    court_case_numbers: tuple[str, ...] = (),
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
        parsed_date, parsed_time = _parse_date_time(segment, fallback_year=fallback_year)
        absolute_action = _explicit_absolute_deadline_action(segment) if parsed_date else ""
        if absolute_action:
            source = f"{pdf_path.name}#p{page_no}:deadline:absolute:{parsed_date}:{absolute_action}"
            _append(
                TodoCandidate(
                    type=absolute_action,
                    date=parsed_date,
                    time="",
                    description=_description(
                        f"{parsed_date} 前{absolute_action}",
                        pdf_path.name,
                        page_no,
                        segment,
                        prefix="筆錄明確指示",
                    ),
                    source_file=source,
                    confidence="high",
                    rule="absolute_action_deadline",
                    page=page_no,
                    excerpt=_brief(segment),
                    case_number=case_number,
                    client_name=client_name,
                    court_case_numbers=court_case_numbers,
                )
            )
            continue

        has_schedule_word = any(word in packed for word in _SCHEDULE_WORDS)
        has_procedure = any(word in packed for word in _PROCEDURE_WORDS)
        scheduled_in_segment = False
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
                court_case_numbers=court_case_numbers,
            )
            _append(candidate)
            scheduled_in_segment = True
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
                    court_case_numbers=court_case_numbers,
                )
            )
            continue

        if ("候核辦" in packed or "候核" in packed or "候辦" in packed) and not scheduled_in_segment:
            # A procedural status without a concrete task is not actionable.
            # Do not create a synthetic seven-day follow-up or a review item.
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
                            court_case_numbers=court_case_numbers,
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
                    court_case_numbers=court_case_numbers,
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
                    court_case_numbers=court_case_numbers,
                )
            )

    return candidates


def extract_candidates_from_pdf(pdf_path: Path, *, tail_pages: int = TAIL_PAGES) -> list[TodoCandidate]:
    case_number, client_name = _infer_case_identity(pdf_path)
    pages = _extract_pages(pdf_path)
    court_case_numbers = _extract_court_case_numbers(pages)
    return extract_candidates_from_pages(
        pages,
        pdf_path=pdf_path,
        transcript_date=_date_from_filename(pdf_path),
        case_number=case_number,
        client_name=client_name,
        tail_pages=tail_pages,
        court_case_numbers=court_case_numbers,
    )


def _iter_index_paths(limit: int) -> Iterable[Path]:
    if not _safe_exists(INDEX_DB_PATH):
        return []
    try:
        data = json.loads(INDEX_DB_PATH.read_text("utf-8"))
    except Exception:
        return []
    rows: list[tuple[float, Path]] = []
    for key, info in (data.get("indexed") or {}).items():
        p = Path(key)
        if _safe_exists(p) and p.suffix.lower() == ".pdf":
            try:
                mtime = float((info or {}).get("mtime") or _safe_stat_mtime(p) or 0)
            except Exception:
                mtime = 0
            rows.append((mtime, p))
        if len(rows) >= max(limit * 5, limit):
            break
    rows.sort(key=lambda it: it[0], reverse=True)
    return [p for _, p in rows[:limit]]


def _case_roots() -> list[Path]:
    raw = (
        os.environ.get("TRANSCRIPT_TODO_CASE_ROOTS")
        or os.environ.get("SYNOLOGY_CASE_ROOTS")
        or os.environ.get("SYNOLOGY_CASE_ROOT")
        or ""
    )
    if raw.strip():
        return [Path(x.strip()).expanduser() for x in raw.split(",") if x.strip()]
    try:
        from api.case_path_mapper import preferred_case_roots

        return [Path(x).expanduser() for x in preferred_case_roots(include_closed=True)]
    except Exception:
        return []


def _looks_like_case_dir(path: Path) -> bool:
    if _CASE_NO_RE.search(path.name):
        return True
    return any(_safe_is_dir(path / subdir) for subdir in _TRANSCRIPT_SUBDIRS)


def _iter_case_dirs_under(root: Path, *, started_at: float) -> Iterable[Path]:
    if not _safe_exists(root):
        return
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        if datetime.now().timestamp() - started_at > LISTING_BUDGET_SEC:
            return
        current, depth = stack.pop()
        if depth > 5:
            continue
        children = _safe_child_dirs(current)
        for child in children:
            if _looks_like_case_dir(child):
                yield child
                continue
            stack.append((child, depth + 1))


def _iter_recent_filesystem_paths(limit: int, *, recent_days: int = RECENT_DAYS) -> list[Path]:
    """Find newly downloaded transcripts even before the nightly indexer runs.

    The old flow only read `.agent/transcript_index.json`; a transcript downloaded
    at 06:00/21:00 could therefore be invisible to the six-hour todo refresh
    until the next index run.  This filesystem pass is bounded and mtime-sorted,
    so it catches fresh transcript PDFs without doing a full NAS crawl.
    """
    import time

    started = time.time()
    cutoff = started - max(1, recent_days) * 86400
    rows: list[tuple[float, Path]] = []
    seen: set[str] = set()
    scan_limit = max(limit * 4, limit, 50)
    for root in _case_roots():
        for case_dir in _iter_case_dirs_under(root, started_at=started):
            for subdir_name in _TRANSCRIPT_SUBDIRS:
                folder = case_dir / subdir_name
                if not _safe_is_dir(folder):
                    continue
                pdfs = _safe_pdf_glob(folder, limit=scan_limit - len(rows))
                for pdf in pdfs:
                    if pdf.name.startswith(".") or pdf.name.startswith("~$"):
                        continue
                    try:
                        mtime = _safe_stat_mtime(pdf)
                    except OSError:
                        continue
                    if mtime < cutoff:
                        continue
                    try:
                        key = str(pdf.resolve())
                    except OSError:
                        key = str(pdf)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append((float(mtime), pdf))
                    if len(rows) >= scan_limit:
                        break
                if len(rows) >= scan_limit:
                    break
            if len(rows) >= scan_limit or time.time() - started > LISTING_BUDGET_SEC:
                break
        if len(rows) >= scan_limit or time.time() - started > LISTING_BUDGET_SEC:
            break
    rows.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in rows[:limit]]


def _iter_pdf_targets(raw_path: str, *, limit: int) -> list[Path]:
    if raw_path:
        root = Path(raw_path).expanduser()
        if _safe_is_file(root) and root.suffix.lower() == ".pdf":
            return [root]
        if _safe_is_dir(root):
            return _safe_pdf_rglob(root, limit=limit)
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for path in [*_iter_recent_filesystem_paths(limit), *_iter_index_paths(limit * 2)]:
        key = _logical_case_pdf_key(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
        if len(out) >= limit:
            break
    return out


def _logical_case_pdf_key(path: Path) -> str:
    """Deduplicate the same case PDF across SMB and Synology placeholders."""

    normalized = str(path).replace("\\", "/")
    for marker, scope in (
        ("/01_案件/", "active"),
        ("/03_工作資料/10_結案/", "closed"),
    ):
        if marker in normalized:
            relative = normalized.split(marker, 1)[1].strip("/")
            return f"{scope}:{relative}"
    try:
        return str(path.resolve())
    except OSError:
        return normalized


def _alternate_transcript_paths(path: Path) -> list[Path]:
    """Return equivalent real-storage paths for a dataless local placeholder."""

    try:
        from api.case_path_mapper import local_case_path_candidates

        candidates = [Path(item) for item in local_case_path_candidates(str(path))]
    except Exception:
        candidates = []
    original = str(path).replace("\\", "/")
    out: list[Path] = []
    seen: set[str] = {original}
    for candidate in candidates:
        key = str(candidate).replace("\\", "/")
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def scan_targets(paths: list[Path], *, tail_pages: int = TAIL_PAGES) -> dict[str, Any]:
    items: list[TodoCandidate] = []
    errors: list[dict[str, str]] = []
    scanned = 0
    past_skipped = 0
    implausible_skipped = 0
    recovered_count = 0
    stale_missing_skipped = 0
    today = date.today()
    for path in paths:
        source_candidates = [path, *_alternate_transcript_paths(path)]
        candidates: list[TodoCandidate] | None = None
        attempt_errors: list[Exception] = []
        used_path = path
        for candidate_index, candidate_path in enumerate(source_candidates):
            # Always try the canonical path so its real error is retained.  A
            # synthesized legacy/Synology alias is attempted only when a bounded
            # probe confirms it is a file; otherwise PDF libraries emit varying
            # "cannot open" messages and turn one stale index row into a forever
            # retry.
            if candidate_index > 0 and not _safe_is_file(candidate_path):
                continue
            try:
                candidates = extract_candidates_from_pdf(
                    candidate_path,
                    tail_pages=tail_pages,
                )
                used_path = candidate_path
                break
            except Exception as exc:
                attempt_errors.append(exc)
        if candidates is None:
            missing_markers = ("no such file", "not found", "does not exist")
            if attempt_errors and all(
                any(marker in str(exc).lower() for marker in missing_markers)
                for exc in attempt_errors
            ):
                # The transcript index may briefly retain a path after a file was
                # moved or renamed.  This is stale input, not a failed todo scan.
                # The next index rebuild will rediscover the canonical location.
                stale_missing_skipped += 1
                continue
            errors.append(
                {
                    "path": str(path),
                    # Preserve the canonical-path failure.  Reporting the last
                    # legacy alias error made a healthy canonical path look wrong.
                    "error": str(
                        attempt_errors[0]
                        if attempt_errors
                        else "transcript source unreadable"
                    ),
                }
            )
            continue
        scanned += 1
        if used_path != path:
            recovered_count += 1
        for candidate in candidates:
            if candidate.confidence != "high":
                items.append(candidate)
                continue
            parsed_date = _parse_iso_date(candidate.date)
            if parsed_date is None:
                items.append(candidate)
                continue
            if parsed_date < today:
                past_skipped += 1
                continue
            if not _is_plausible_todo_date(parsed_date):
                implausible_skipped += 1
                continue
            items.append(candidate)
    items = _dedupe_candidates(items)
    high = [x for x in items if x.confidence == "high"]
    review = [x for x in items if x.confidence == "review"]
    return {
        "ok": True,
        "scanned": scanned,
        "high_count": len(high),
        "review_count": len(review),
        "past_skipped": past_skipped,
        "implausible_skipped": implausible_skipped,
        "errors_count": len(errors),
        "recovered_count": recovered_count,
        "stale_missing_skipped": stale_missing_skipped,
        "deferred": bool(errors),
        "reason": "transcript_source_retry_pending" if errors else "complete",
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
    future_skipped = 0
    today = date.today()
    try:
        ensure_osc_min_schema(conn)
        for item in items:
            if item.get("confidence") != "high":
                continue
            case_number = str(item.get("case_number") or "").strip()
            if not case_number:
                continue
            identity = _resolve_case_identity_db(
                conn,
                source_case_number=case_number,
                client_name=str(item.get("client_name") or "").strip(),
                court_case_numbers=item.get("court_case_numbers") or (),
            )
            case_number = str(identity.get("case_number") or case_number).strip()
            todo_date = _parse_iso_date(str(item.get("date") or ""))
            if todo_date and todo_date < today and not include_past:
                past_skipped += 1
                continue
            if todo_date and not _is_plausible_todo_date(todo_date):
                future_skipped += 1
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
                client_name=str(identity.get("client_name") or item.get("client_name") or ""),
                todos=[todo],
                source_file=str(item.get("source_file") or "transcript_todo"),
                allow_duplicates=False,
                commit=False,
            )
            inserted += int(result.get("inserted") or 0)
            skipped += int(result.get("skipped") or 0)
            updated += int(result.get("updated") or 0)
            written.append({"case_number": case_number, "todo": todo, "result": result, "identity": identity})
        conn.commit()
    finally:
        conn.close()
    return {
        "inserted": inserted,
        "skipped": skipped,
        "updated": updated,
        "past_skipped": past_skipped,
        "future_skipped": future_skipped,
        "written": written,
    }


def cmd_status() -> dict[str, Any]:
    indexed = 0
    existing = 0
    if _safe_exists(INDEX_DB_PATH):
        try:
            data = json.loads(INDEX_DB_PATH.read_text("utf-8"))
            indexed = len(data.get("indexed") or {})
            for key in (data.get("indexed") or {}).keys():
                if _safe_exists(Path(key)):
                    existing += 1
        except Exception:
            pass
    return {"ok": True, "index_path": str(INDEX_DB_PATH), "indexed": indexed, "existing": existing}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI 筆錄待辦抽取器")
    parser.add_argument("--_extract-pages-worker", default="", help=argparse.SUPPRESS)
    parser.add_argument("--task", choices=["dry_run", "apply", "status"], default="dry_run")
    parser.add_argument("--path", default="", help="單一 PDF 或筆錄資料夾；未提供時使用 transcript_index.json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--tail-pages", type=int, default=TAIL_PAGES)
    parser.add_argument("--json", action="store_true", help="輸出完整 JSON")
    parser.add_argument("--include-past", action="store_true", help="apply 時允許寫入已逾期項目")
    args = parser.parse_args(argv)

    if args._extract_pages_worker:
        try:
            pages = _extract_pages_inner(Path(args._extract_pages_worker))
            payload = {"ok": True, "pages": pages}
            returncode = 0
        except BaseException as exc:
            payload = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            returncode = 2
        print(_PDF_WORKER_MARKER + json.dumps(payload, ensure_ascii=False))
        return returncode

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
