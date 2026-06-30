from __future__ import annotations

import logging
import os
import math
import json
import re
import secrets
import sys
import time
import uuid
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
import urllib.request

import fitz
from flask import Blueprint, jsonify, request
from flask_login import login_required
from werkzeug.utils import secure_filename

from skills.bridge.shared_utils.judgment_folder_names import (
    JUDGMENT_FOLDER_LABEL,
    judgment_folder_name,
    legacy_judgment_folder_name,
    path_has_judgment_folder,
)


osc_pdf_bp = Blueprint("osc_pdf", __name__)

_RAPID_OCR_ENGINE: Any | None = None
_RAPID_OCR_UNAVAILABLE = False


def _upload_dir() -> Path:
    root = Path(__file__).resolve().parents[2]
    path = root / ".agent" / "pdf_uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path_from_request(value: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("請先指定 PDF 路徑")
    path = Path(text).expanduser()
    if not path.is_file():
        raise ValueError("找不到指定檔案")
    if path.suffix.lower() != ".pdf":
        raise ValueError("目前僅支援 PDF 檔案")
    return path.resolve()


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _osc_exec(sql: str, params=(), fetch: str = "none"):
    from api.osc.utils import _osc_exec as _utils_exec
    return _utils_exec(sql, params, fetch=fetch)


def _load_headless_todo_helpers():
    skill_dir = _repo_root() / "skills" / "osc-orchestrator"
    if str(skill_dir) in sys.path:
        sys.path.remove(str(skill_dir))
    sys.path.insert(0, str(skill_dir))
    package = sys.modules.get("osc_headless")
    current = sys.modules.get("osc_headless.todos")
    package_file = str(getattr(package, "__file__", "")) if package is not None else ""
    current_file = str(getattr(current, "__file__", "")) if current is not None else ""
    if (package is not None and not package_file.startswith(str(skill_dir))) or (current is not None and not current_file.startswith(str(skill_dir))):
        sys.modules.pop("osc_headless.todos", None)
        sys.modules.pop("osc_headless", None)
    from osc_headless.todos import extract_todos_from_filename, get_default_patterns  # type: ignore
    return extract_todos_from_filename, get_default_patterns


def _load_headless_date_helpers():
    skill_dir = _repo_root() / "skills" / "osc-orchestrator"
    if str(skill_dir) in sys.path:
        sys.path.remove(str(skill_dir))
    sys.path.insert(0, str(skill_dir))
    package = sys.modules.get("osc_headless")
    current = sys.modules.get("osc_headless.todos")
    package_file = str(getattr(package, "__file__", "")) if package is not None else ""
    current_file = str(getattr(current, "__file__", "")) if current is not None else ""
    if (package is not None and not package_file.startswith(str(skill_dir))) or (current is not None and not current_file.startswith(str(skill_dir))):
        sys.modules.pop("osc_headless.todos", None)
        sys.modules.pop("osc_headless", None)
    from osc_headless.todos import extract_base_year_from_filename, extract_document_date_from_filename  # type: ignore
    return extract_document_date_from_filename, extract_base_year_from_filename


def _open_case_status_sql(column: str = "status") -> str:
    return _pdf_todo_case_status_sql(column)


def _pdf_todo_case_status_sql(column: str = "status") -> str:
    """Case filter for court-PDF deadline scans.

    "結案中", "待報結", and "待送出" still need PDF deadline coverage:
    judgments/rulings commonly arrive during closing prep, and missing an
    appeal/objection all-day event is worse than scanning one extra case row.
    """
    col = column if re.fullmatch(r"[A-Za-z0-9_`.]+", column or "") else "status"
    return f"""
        (
          {col} IS NULL OR {col}=''
          OR (
            LOWER({col}) NOT IN ('closed', 'done')
            AND {col} NOT IN ('已結案', '結案')
            AND {col} NOT LIKE '%已結案%'
          )
        )
    """


def _pdf_text_date_context(path: Path) -> tuple[datetime, int]:
    extract_doc_date, extract_base_year = _load_headless_date_helpers()
    doc_date = extract_doc_date(path.name, str(path))
    if not doc_date:
        try:
            doc_date = datetime.fromtimestamp(path.stat().st_mtime)
        except Exception:
            doc_date = datetime.now()
    return doc_date, int(extract_base_year(path.name, str(path), doc_date))


def _get_rapid_ocr_engine():
    global _RAPID_OCR_ENGINE, _RAPID_OCR_UNAVAILABLE
    if _RAPID_OCR_UNAVAILABLE:
        return None
    if _RAPID_OCR_ENGINE is not None:
        return _RAPID_OCR_ENGINE
    try:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except Exception:
            from rapidocr import RapidOCR  # type: ignore

        _RAPID_OCR_ENGINE = RapidOCR()
        return _RAPID_OCR_ENGINE
    except Exception:
        _RAPID_OCR_UNAVAILABLE = True
        return None


def _normalise_ocr_result(result: Any) -> str:
    if not result:
        return ""
    if hasattr(result, "txts"):
        try:
            return "\n".join(str(t) for t in (result.txts or []) if str(t or "").strip())
        except Exception:
            return ""
    payload = result[0] if isinstance(result, tuple) and result else result
    lines: list[str] = []
    for item in payload or []:
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = item[1]
                if isinstance(text, (list, tuple)) and text:
                    text = text[0]
                if str(text or "").strip():
                    lines.append(str(text).strip())
        except Exception:
            continue
    return "\n".join(lines)


def _ocr_pdf_page(page: fitz.Page) -> str:
    engine = _get_rapid_ocr_engine()
    if engine is None:
        return ""
    try:
        dpi = max(120, min(260, int(os.environ.get("OSC_PDF_CALENDAR_OCR_DPI", "180") or "180")))
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        image_bytes = pix.tobytes("png")
        return _normalise_ocr_result(engine(image_bytes))
    except Exception:
        logging.getLogger(__name__).warning("pdf calendar OCR fallback failed", exc_info=True)
        return ""


def _pdf_text(path: Path, max_pages: int = 5) -> str:
    doc = fitz.open(path)
    parts: list[str] = []
    try:
        page_limit = min(doc.page_count, max(1, max_pages))
        for idx in range(page_limit):
            parts.append(doc[idx].get_text("text") or "")
        native_text = "\n".join(parts).strip()
        min_chars = max(0, int(os.environ.get("OSC_PDF_CALENDAR_OCR_MIN_TEXT_CHARS", "20") or "20"))
        ocr_enabled = str(os.environ.get("OSC_PDF_CALENDAR_OCR_FALLBACK", "1")).strip().lower() in {"1", "true", "yes", "on"}
        if ocr_enabled and len(re.sub(r"\s+", "", native_text)) < min_chars:
            ocr_limit = max(1, min(page_limit, int(os.environ.get("OSC_PDF_CALENDAR_OCR_MAX_PAGES", "3") or "3")))
            ocr_parts = [_ocr_pdf_page(doc[idx]) for idx in range(ocr_limit)]
            parts.extend([p for p in ocr_parts if str(p or "").strip()])
    finally:
        doc.close()
    return "\n".join(parts).strip()


def _infer_case_from_path(path: Path) -> dict[str, str]:
    case_number, client_name = _case_folder_identity_from_path(path)
    try:
        row, _ = _osc_exec(
            """
            SELECT di.case_number, COALESCE(c.client_name, di.party, '') AS client_name
            FROM document_index di
            LEFT JOIN cases c ON c.case_number = di.case_number
            WHERE di.file_path=%s OR di.file_name=%s
            ORDER BY di.modified_date DESC, di.id DESC
            LIMIT 1
            """,
            (str(path), path.name),
            fetch="one",
        )
        if row:
            case_number = str(row.get("case_number") or case_number or "").strip()
            client_name = str(row.get("client_name") or client_name or "").strip()
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 107, exc_info=True)
    return {"case_number": case_number, "client_name": client_name}


def _load_todo_patterns() -> dict[str, list[dict[str, Any]]]:
    _extract, get_default_patterns = _load_headless_todo_helpers()
    defaults = get_default_patterns()
    try:
        rows, _ = _osc_exec(
            """
            SELECT todo_type, pattern, pattern_type, days
            FROM todo_keywords
            WHERE is_active=1
            ORDER BY todo_type, id
            """,
            fetch="all",
        )
        patterns: dict[str, list[dict[str, Any]]] = {}
        for row in rows or []:
            todo_type = str(row.get("todo_type") or "").strip()
            pattern = str(row.get("pattern") or "").strip()
            if not todo_type or not pattern:
                continue
            patterns.setdefault(todo_type, []).append(
                {
                    "pattern": pattern,
                    "pattern_type": str(row.get("pattern_type") or "").strip(),
                    "days": row.get("days"),
                }
            )
        if patterns:
            for todo_type, items in defaults.items():
                patterns.setdefault(todo_type, [])
                seen = {str(item.get("pattern") or "") for item in patterns[todo_type]}
                for item in items:
                    if str(item.get("pattern") or "") not in seen:
                        patterns[todo_type].append(item)
            return patterns
    except Exception:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 146, exc_info=True)
    return defaults


def _parse_roc_or_ad_date(year: str, month: str, day: str) -> datetime | None:
    try:
        y = int(year)
        if y < 1911:
            current_roc_year = datetime.now().year - 1911
            if y < 80 or y > current_roc_year + 3:
                return None
            y += 1911
        dt = datetime(y, int(month), int(day))
        if dt.date() > (datetime.now() + timedelta(days=730)).date():
            return None
        return dt
    except Exception:
        return None


def _parse_compact_roc_or_ad_date(value: str) -> datetime | None:
    text = re.sub(r"\D", "", str(value or ""))
    try:
        if len(text) == 7:
            return _parse_roc_or_ad_date(text[:3], text[3:5], text[5:7])
        if len(text) == 8:
            return _parse_roc_or_ad_date(text[:4], text[4:6], text[6:8])
    except Exception:
        return None
    return None


def _parse_chinese_number_token(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    mapping = {
        "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    if text in mapping:
        return mapping[text]
    if "十" in text:
        left, right = text.split("十", 1)
        tens = 10 if not left else mapping.get(left, 0) * 10
        ones = 0 if not right else mapping.get(right, 0)
        return tens + ones
    return None


def _parse_ampm_time(period: str, hour: str, minute: str = "") -> tuple[int, int]:
    label = str(period or "").strip()
    h = _parse_chinese_number_token(hour)
    m = _parse_chinese_number_token(minute) if str(minute or "").strip() else 0
    if h is None or m is None:
        raise ValueError("invalid time token")
    return _apply_ampm_period(label, h), m


def _apply_ampm_period(period: str, hour: int) -> int:
    label = str(period or "").strip()
    if label == "上":
        label = "上午"
    if label == "下":
        label = "下午"
    h = int(hour)
    if label in {"下午", "晚上", "晚間", "傍晚", "夜間"} and h != 12:
        h += 12
    if label == "中午" and h < 11:
        h += 12
    if label in {"上午", "早上"} and h == 12:
        h = 0
    return h


def _parse_compact_ampm_time(period: str, hour: str, minute: str = "") -> tuple[int, int]:
    token = str(hour or "").strip()
    minute_token = str(minute or "").strip()
    if token.isdigit() and not minute_token and len(token) in {3, 4}:
        h = int(token[:-2])
        m = int(token[-2:])
        if not (0 <= m <= 59 and 0 <= h <= 23):
            raise ValueError("invalid compact time token")
        if h <= 12:
            h = _apply_ampm_period(period, h)
        return h, m
    return _parse_ampm_time(period, token, minute_token)


def _next_tw_workday(dt: datetime) -> datetime:
    try:
        import holidays  # type: ignore

        tw = holidays.Taiwan(years=range(dt.year - 1, dt.year + 2))
        d = dt.date()
        while True:
            name = tw.get(d)
            if name and "補行上班日" not in str(name):
                d = d + timedelta(days=1)
                continue
            if d.weekday() >= 5:
                d = d + timedelta(days=1)
                continue
            return datetime.combine(d, dt.time())
    except Exception:
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        return dt


_PDF_TODO_HINT_RE = re.compile(
    r"(開庭|調解|審理|辯論|宣判|訊問|調查|補正|陳報|表示意見|提出|檢送|檢附|繳納|繳費|裁判費|抗告|上訴|日內|日前|文到|送達後|送達翌日起|訂於|定於)"
)

_PDF_CALENDAR_SOURCE_HINTS = (
    "法院通知",
    "程序裁定",
    "法院通知或程序裁定",
    "法院通知及程序裁定",
    "法院通知與程序裁定",
    "法院_通知",
    "法院_傳票",
    JUDGMENT_FOLDER_LABEL,
    "判決書",
    "開庭通知",
    "法庭通知",
    "庭期通知",
    "民事庭通知",
    "刑事庭通知",
    "地檢署通知",
    "檢察署通知",
)

_PDF_CALENDAR_SOURCE_DIR_NAMES = (
    "法院通知與程序裁定",
    "02_法院通知與程序裁定",
    "01_法院通知與程序裁定",
    "法院通知",
    "程序裁定",
    "法院通知或程序裁定",
    "法院通知及程序裁定",
    "09_法院通知或程序裁定",
    "09_法院通知及程序裁定",
    "法院_通知",
    "法院_傳票",
    JUDGMENT_FOLDER_LABEL,
    judgment_folder_name(3),
    judgment_folder_name(4),
    judgment_folder_name(10),
    "判決書",
    legacy_judgment_folder_name(3),
    legacy_judgment_folder_name(4),
    legacy_judgment_folder_name(10),
    "開庭通知",
    "法庭通知",
    "庭期通知",
    "民事庭通知",
    "刑事庭通知",
    "地檢署通知",
    "檢察署通知",
)

_PDF_CALENDAR_EXCLUDED_PATH_HINTS = (
    "我方歷次書狀",
    "對方歷次書狀",
    "歷次書狀",
    "書狀",
    "證據資料",
    "閱卷資料",
    "電子筆錄",
    "訊問筆錄",
    "筆錄",
    "開辦資料",
    "結案資料",
    "回執",
    "信件往返",
    "自行收納款項收據",
)

_PDF_LAF_STAFF_MAIL_DIR_NAMES = (
    "專員來信",
)

_PDF_LAF_DATA_DIR_NAMES = (
    "01_法扶資料",
    "法扶資料",
)

_PDF_LAF_STAFF_COURT_ATTACHMENT_RE = re.compile(
    r"(法院|地院|高院|最高法院|地檢|檢察署|執行處|民事庭|刑事庭|家事庭|行政庭|法庭)"
)


def _case_folder_identity_from_path(path: Path) -> tuple[str, str]:
    case_number = ""
    client_name = ""
    for part in reversed(path.parts):
        m = re.search(r"(20\d{2}-\d{3,5})", part)
        if not m:
            continue
        case_number = m.group(1)
        rest = part.replace(case_number, "")
        rest = re.sub(r"^[\\/_\-\s]+|[\\/_\-\s]+$", "", rest)
        client_name = rest.split("-")[0].split("_")[0].strip()
        break
    return case_number, client_name


def _is_pdf_calendar_candidate_path(path: Path) -> bool:
    name = path.name
    if not name or name.startswith(".") or name.startswith("~$"):
        return False
    if path.suffix.lower() != ".pdf":
        return False
    text = str(path).replace("\\", "/")
    if "專員來信" in text:
        return bool(
            _PDF_TODO_HINT_RE.search(name)
            or _PDF_LAF_STAFF_COURT_ATTACHMENT_RE.search(name)
        )
    if any(hint in text for hint in _PDF_CALENDAR_EXCLUDED_PATH_HINTS):
        return False
    if any(hint in text for hint in _PDF_CALENDAR_SOURCE_HINTS):
        return True
    return False


def _infer_deadline_type_from_context(context: str) -> str | None:
    text = context or ""
    data_request = re.search(
        r"(?:提出|檢送|檢附|補送|補提).{0,20}(?:資料|文件|清冊|報告書|截圖|證據)"
        r"|(?:資料|文件|清冊|報告書|截圖|證據).{0,20}(?:提出|檢送|檢附|補送|補提)",
        text,
    )
    mapping = [
        ("繳費", ("繳納", "繳費", "裁判費", "規費", "聲請費")),
        ("補正", ("補正", "補繳", "補提")),
        ("陳述意見", ("陳述意見",)),
        ("陳報", ("陳報", "回覆", "表示意見", "具狀表示", "確答", "陳明", "說明")),
        ("上訴", ("上訴",)),
        ("抗告", ("抗告",)),
        ("閱卷期限", ("閱卷",)),
    ]
    for todo_type, keywords in mapping:
        if any(keyword in text for keyword in keywords):
            return todo_type
    if data_request:
        return "提出資料"
    return None


def _extract_todos_from_pdf_text(path: Path, text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    body = re.sub(r"\s+", "", text or "")
    if not body:
        return items
    filename_doc_date, filename_base_year = _pdf_text_date_context(path)
    year_prefix = r"(?:中華)?(?:民國)?"
    time_period = r"(上午|下午|早上|中午|晚上|晚間|傍晚|夜間|上|下)"
    procedure = r"(開庭|準備程序|協商程序|言詞辯論|調解|審理程序|審判程序|審理|宣判|訊問|調查)?"

    def _yearless_dt(month: str, day: str) -> datetime | None:
        base = filename_doc_date
        try:
            dt = datetime(filename_base_year, int(month), int(day))
            if dt.date() < base.date() - timedelta(days=30) and filename_base_year == base.year:
                dt = dt.replace(year=dt.year + 1)
            return dt
        except Exception:
            return None

    def _append_hearing(dt: datetime, period: str, hour: str, minute: str, proc: str | None, *, compact_time: bool = False) -> None:
        try:
            h, mi = _parse_compact_ampm_time(period, hour, minute) if compact_time else _parse_ampm_time(period, hour, minute)
        except Exception:
            return
        dt = dt.replace(hour=h, minute=mi)
        kind = "審理" if proc in {"審理程序", "審判程序"} else (proc or "開庭")
        items.append(
            {
                "type": kind,
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M"),
                "description": f"⚖️ PDF 擷取：{dt.strftime('%m/%d %H:%M')} {kind}",
                "source": "pdf_text",
                "source_file": str(path),
            }
        )

    hearing_patterns = [
        rf"(?:定|訂)於?{year_prefix}(\d{{2,4}})年(\d{{1,2}})月(\d{{1,2}})日{time_period}(\d{{1,2}}|[零一二三四五六七八九十]{{1,3}})時([零一二三四五六七八九十\d]{{0,3}})(?:分|整)?.{{0,40}}?{procedure}",
        rf"(?:定|訂)於?(?<!年)(\d{{1,2}})月(\d{{1,2}})日{time_period}(\d{{1,2}}|[零一二三四五六七八九十]{{1,3}})時([零一二三四五六七八九十\d]{{0,3}})(?:分|整)?.{{0,40}}?{procedure}",
    ]
    for pattern in hearing_patterns:
        for m in re.finditer(pattern, body):
            if len(m.groups()) == 7:
                dt = _parse_roc_or_ad_date(m.group(1), m.group(2), m.group(3))
                period, hour, minute, proc = m.group(4), m.group(5), m.group(6), m.group(7)
            elif len(m.groups()) == 6:
                # Yearless hearing: mirror original OSC base-year logic.
                dt = _yearless_dt(m.group(1), m.group(2))
                period, hour, minute, proc = m.group(3), m.group(4), m.group(5), m.group(6)
            else:
                continue
            if not dt:
                continue
            _append_hearing(dt, period, hour, minute, proc)

    compact_hearing_pat = re.compile(
        rf"(?:定|訂)於?(\d{{7,8}}){time_period}(\d{{1,4}})(?:時([零一二三四五六七八九十\d]{{0,3}}))?(?:分|整)?.{{0,40}}?{procedure}"
    )
    for m in compact_hearing_pat.finditer(body):
        dt = _parse_compact_roc_or_ad_date(m.group(1))
        if not dt:
            continue
        _append_hearing(dt, m.group(2), m.group(3), m.group(4) or "", m.group(5), compact_time=True)

    shared_time_pat = re.compile(
        rf"(?P<dates>(?:(?:{year_prefix}\d{{2,4}}年)?\d{{1,2}}月\d{{1,2}}日[、，,及和]*){{2,}}){time_period}(\d{{1,2}}|[零一二三四五六七八九十]{{1,3}})時([零一二三四五六七八九十\d]{{0,3}})(?:分|整)?.{{0,40}}?{procedure}"
    )
    shared_date_pat = re.compile(rf"(?:{year_prefix}(\d{{2,4}})年)?(\d{{1,2}})月(\d{{1,2}})日")
    for m in shared_time_pat.finditer(body):
        for dm in shared_date_pat.finditer(m.group("dates") or ""):
            if dm.group(1):
                dt = _parse_roc_or_ad_date(dm.group(1), dm.group(2), dm.group(3))
            else:
                dt = _yearless_dt(dm.group(2), dm.group(3))
            if not dt:
                continue
            _append_hearing(dt, m.group(2), m.group(3), m.group(4), m.group(5))

    date_only_patterns = [
        rf"(?:定|訂)於?{year_prefix}(\d{{2,4}})年(\d{{1,2}})月(\d{{1,2}})日(?!上午|下午|早上|中午|晚上|晚間|傍晚|夜間|上|下).{{0,40}}?(開庭|準備程序|協商程序|言詞辯論|調解|審理程序|審判程序|審理|宣判|訊問|調查)",
        rf"(?:定|訂)於?(?<!年)(\d{{1,2}})月(\d{{1,2}})日(?!上午|下午|早上|中午|晚上|晚間|傍晚|夜間|上|下).{{0,40}}?(開庭|準備程序|協商程序|言詞辯論|調解|審理程序|審判程序|審理|宣判|訊問|調查)",
    ]
    for pattern in date_only_patterns:
        for m in re.finditer(pattern, body):
            if len(m.groups()) == 4:
                dt = _parse_roc_or_ad_date(m.group(1), m.group(2), m.group(3))
                proc = m.group(4)
            elif len(m.groups()) == 3:
                dt = _yearless_dt(m.group(1), m.group(2))
                proc = m.group(3)
            else:
                continue
            if not dt:
                continue
            matched_text = m.group(0) or ""
            if re.search(rf"\d{{1,2}}月\d{{1,2}}日[、，,及和]+(?:{year_prefix}\d{{2,4}}年)?\d{{1,2}}月\d{{1,2}}日{time_period}", matched_text):
                continue
            after_date = body[m.end() : m.end() + 32]
            if re.search(rf"[、，,及和](?:{year_prefix}\d{{2,4}}年)?\d{{1,2}}月\d{{1,2}}日{time_period}", after_date):
                continue
            kind = "審理" if proc in {"審理程序", "審判程序"} else (proc or "開庭")
            items.append(
                {
                    "type": kind,
                    "date": dt.strftime("%Y-%m-%d"),
                    "time": "",
                    "description": f"⚖️ PDF 擷取：{dt.strftime('%m/%d')} {kind}",
                    "source": "pdf_text",
                    "source_file": str(path),
                }
            )

    doc_date = None
    for m in re.finditer(rf"{year_prefix}(\d{{2,4}})年(\d{{1,2}})月(\d{{1,2}})日", body):
        doc_date = _parse_roc_or_ad_date(m.group(1), m.group(2), m.group(3))
        if doc_date:
            break
    if doc_date is None:
        doc_date = filename_doc_date

    absolute_deadline_pat = re.compile(
        rf"(?:請惠予|應|請|命|限|惠予)?(?:於)?{year_prefix}(\d{{2,4}})年(\d{{1,2}})月(\d{{1,2}})日(?:以前|前)[，,、\s]*([^）)]{{0,90}})"
    )
    for m in absolute_deadline_pat.finditer(body):
        before = body[max(0, m.start() - 24) : m.start()]
        tail = m.group(4) or ""
        matched_text = m.group(0) or ""
        if not (
            re.match(r"(請惠予|應|請|命|限|惠予)", matched_text)
            or re.search(r"(請惠予|應於|請於|命於|限於|惠予於|應|請|命|限).{0,16}$", before)
        ):
            continue
        todo_type = _infer_deadline_type_from_context(f"{before}{tail}")
        if not todo_type:
            continue
        deadline = _parse_roc_or_ad_date(m.group(1), m.group(2), m.group(3))
        if not deadline:
            continue
        items.append(
            {
                "type": todo_type,
                "date": deadline.strftime("%Y-%m-%d"),
                "time": "",
                "description": f"📝 PDF 擷取：{deadline.strftime('%m/%d')}前{todo_type}",
                "source": "pdf_text",
                "source_file": str(path),
            }
        )

    separated_deadline_pat = re.compile(
        r"(?:期限|至|應於|限於|請於|命於|繳費期限|繳費日期)[:：]?\s*(\d{2,4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s*\d{1,2}[:：]\d{2})?"
    )
    for m in separated_deadline_pat.finditer(body):
        before = body[max(0, m.start() - 28) : m.start()]
        after = body[m.end() : m.end() + 40]
        todo_type = _infer_deadline_type_from_context(f"{before}{after}")
        if not todo_type:
            continue
        deadline = _parse_roc_or_ad_date(m.group(1), m.group(2), m.group(3))
        if not deadline:
            continue
        items.append(
            {
                "type": todo_type,
                "date": deadline.strftime("%Y-%m-%d"),
                "time": "",
                "description": f"📝 PDF 擷取：{deadline.strftime('%m/%d')}{todo_type}",
                "source": "pdf_text",
                "source_file": str(path),
            }
        )

    compact_deadline_pat = re.compile(r"(?:繳費期限|繳費日期|期限|至|應於|限於|請於|命於)[:：]?\s*(\d{7,8})(?!\d)")
    for m in compact_deadline_pat.finditer(body):
        before = body[max(0, m.start() - 28) : m.start()]
        after = body[m.end() : m.end() + 40]
        todo_type = _infer_deadline_type_from_context(f"{before}{after}")
        if not todo_type:
            continue
        deadline = _parse_compact_roc_or_ad_date(m.group(1))
        if not deadline:
            continue
        items.append(
            {
                "type": todo_type,
                "date": deadline.strftime("%Y-%m-%d"),
                "time": "",
                "description": f"📝 PDF 擷取：{deadline.strftime('%m/%d')}{todo_type}",
                "source": "pdf_text",
                "source_file": str(path),
            }
        )

    day_token = r"([零一二三四五六七八九十\d]{1,4})(日|週|周)"
    relative_map = [
        ("補正", rf"{day_token}內.{{0,25}}補正"),
        ("陳述意見", rf"{day_token}內.{{0,25}}陳述意見"),
        ("陳報", rf"{day_token}內.{{0,35}}(?:陳報|回覆|表示意見|確答|陳明)"),
        ("提出資料", rf"{day_token}內.{{0,35}}(?:提出|檢送|補提).{{0,20}}(?:資料|文件|清冊|報告書|截圖|證據)"),
        ("繳費", rf"{day_token}內.{{0,25}}(?:繳納|繳費)"),
        ("再抗告", rf"{day_token}內.{{0,25}}再抗告"),
        ("再議", rf"{day_token}內.{{0,25}}(?:聲請|提出)?再議"),
        ("異議", rf"{day_token}內.{{0,25}}(?:提出|聲明)?異議"),
        ("上訴", rf"{day_token}內.{{0,25}}上訴"),
        ("抗告", rf"{day_token}內.{{0,25}}抗告"),
    ]
    for todo_type, pattern in relative_map:
        m = re.search(pattern, body)
        if not m:
            continue
        days = _parse_chinese_number_token(m.group(1))
        if days is None:
            continue
        if (m.group(2) or "") in {"週", "周"}:
            days *= 7
        deadline = _next_tw_workday(doc_date + timedelta(days=days))
        items.append(
            {
                "type": todo_type,
                "date": deadline.strftime("%Y-%m-%d"),
                "time": "",
                "description": f"📝 PDF 擷取：{days}日內{todo_type}（基準日 {doc_date.strftime('%m/%d')}）",
                "source": "pdf_text",
                "source_file": str(path),
            }
        )
    return items


def _is_court_calendar_pdf(path: Path, text: str = "") -> bool:
    haystack = f"{path}\n{path.name}\n{(text or '')[:3000]}"
    if path_has_judgment_folder(str(path)):
        return True
    return any(
        key in haystack
        for key in (
            "法院通知",
            "程序裁定",
            "法院通知或程序裁定",
            "法院通知及程序裁定",
            JUDGMENT_FOLDER_LABEL,
            "判決書",
            "地方法院",
            "高等法院",
            "最高法院",
            "裁定",
            "通知",
            "函",
            "開庭方式意願徵詢",
        )
    )


def _looks_like_tentative_confirmation_request(path: Path, text: str = "") -> bool:
    """Return True only for no-deadline PDFs that still ask us to confirm/reply.

    The original desktop OSC did not invent generic "確認" todos for every
    court letter or ruling.  MAGI keeps a narrow 14-day fallback only for
    documents that are themselves a confirmation/reply request, such as a court
    hearing-method inquiry form.  Plain rulings, judgments, "檢送...狀", and
    documents whose text merely mentions data/evidence must not create work.
    """
    haystack = re.sub(r"\s+", "", f"{path.name}\n{(text or '')[:4000]}")
    if not haystack:
        return False
    if any(token in haystack for token in ("開庭方式意願徵詢", "意願徵詢表", "確認開庭方式")):
        return True
    if re.search(r"(?:請|應|命|限|惠予).{0,16}(?:確認|回覆|回復).{0,40}(?:是否|意願|開庭方式|視訊|到庭方式)", haystack):
        return True
    if re.search(r"(?:請|應|命|限|惠予).{0,20}(?:回覆|回復).{0,40}(?:是否同意|有無意見)", haystack):
        return True
    return False


def _tentative_no_deadline_todo(path: Path, text: str = "") -> list[dict[str, Any]]:
    enabled = str(os.environ.get("OSC_PDF_CALENDAR_TENTATIVE_IF_NO_DEADLINE", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled or not _is_court_calendar_pdf(path, text):
        return []
    if not _looks_like_tentative_confirmation_request(path, text):
        return []
    try:
        st = path.stat()
        mtime_dt = datetime.fromtimestamp(st.st_mtime)
    except Exception:
        mtime_dt = datetime.now()
    max_age_days = max(0, int(os.environ.get("OSC_PDF_CALENDAR_TENTATIVE_MAX_MTIME_DAYS", "45") or "45"))
    if max_age_days and (datetime.now() - mtime_dt).total_seconds() > max_age_days * 86400:
        return []
    doc_date, _base_year = _pdf_text_date_context(path)
    base_dt = max(doc_date, mtime_dt)
    days = max(1, int(os.environ.get("OSC_PDF_CALENDAR_TENTATIVE_DAYS", "14") or "14"))
    deadline = _next_tw_workday(base_dt + timedelta(days=days))
    return [
        {
            "type": "確認",
            "date": deadline.strftime("%Y-%m-%d"),
            "time": "",
            "description": f"📝 PDF 擷取：未載明明確期限，暫定於{deadline.strftime('%m/%d')}前確認（基準日 {base_dt.strftime('%m/%d')}）",
            "source": "pdf_tentative_no_deadline",
            "source_file": str(path),
        }
    ]


def _todo_semantic_family(item: dict[str, Any]) -> str:
    todo_type = str(item.get("type") or "").strip()
    if todo_type == "繳費":
        return "deadline_payment"
    if todo_type == "補正":
        return "deadline_correction"
    if todo_type in {"上訴", "抗告", "再抗告", "異議", "再議"}:
        return "deadline_challenge"
    if todo_type in {"陳報", "陳述意見", "提出資料", "表示意見"}:
        return "deadline_response"
    text = f"{todo_type} {item.get('description') or ''}"
    if any(k in text for k in ("開庭", "準備程序", "言詞辯論", "審理程序", "審判程序", "審理", "宣判", "訊問", "調解", "調查")):
        return "hearing"
    if any(k in text for k in ("繳納", "繳費", "裁判費", "規費", "聲請費")):
        return "deadline_payment"
    if any(k in text for k in ("補正", "補繳", "補提", "補送", "補件")):
        return "deadline_correction"
    if any(k in text for k in ("上訴", "抗告", "再抗告", "異議", "再議")):
        return "deadline_challenge"
    if any(k in text for k in ("陳報", "陳述意見", "表示意見", "提出資料", "提出", "具狀表示", "回覆", "確答", "陳明")):
        return "deadline_response"
    return str(item.get("type") or "待辦").strip() or "待辦"


def _todo_priority(item: dict[str, Any]) -> tuple[int, int]:
    todo_type = str(item.get("type") or "").strip()
    text = str(item.get("description") or "")
    type_rank = {
        "繳費": 100,
        "補正": 95,
        "陳報": 90,
        "陳述意見": 88,
        "上訴": 86,
        "抗告": 86,
        "再抗告": 86,
        "異議": 84,
        "提出資料": 72,
        "確認": 20,
        "待辦": 10,
    }.get(todo_type, 50)
    source_rank = {
        "filename": 4,
        "pdf_text": 3,
        "pdf_tentative_no_deadline": 1,
    }.get(str(item.get("source") or ""), 2)
    if "MAGI分享連結：" in text or "來源PDF：" in text:
        source_rank += 1
    return type_rank, source_rank


def _dedupe_todos(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    semantic: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    out: list[dict[str, Any]] = []
    for item in items:
        key = (
            str(item.get("type") or ""),
            str(item.get("date") or ""),
            str(item.get("time") or ""),
            str(item.get("description") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        family = _todo_semantic_family(item)
        if family in {"deadline_response"}:
            semantic_key = (
                str(item.get("date") or ""),
                str(item.get("time") or ""),
                family,
            )
            current = semantic.get(semantic_key)
            if current is None:
                semantic[semantic_key] = item
                order.append(semantic_key)
                continue
            if _todo_priority(item) > _todo_priority(current):
                semantic[semantic_key] = item
            continue
        out.append(item)
    out.extend(semantic[k] for k in order if k in semantic)
    return out


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _event_color(todo_type: str) -> str:
    if todo_type in {"開庭", "宣判", "調解", "言詞辯論", "準備程序", "審理", "訊問", "調查"}:
        return "#3f51b5"
    if todo_type in {"補正", "繳費", "上訴", "抗告", "再抗告", "陳述意見", "陳報"}:
        return "#f5511d"
    return "#0ea5e9"


def _todo_to_calendar_event(todo: dict[str, Any], *, case_number: str, client_name: str, source_file: str) -> dict[str, Any]:
    todo_type = str(todo.get("type") or "待辦").strip()
    todo_date = str(todo.get("date") or "").strip()
    todo_time = str(todo.get("time") or "").strip()
    title_parts = [case_number, client_name, todo_type]
    title = "｜".join([p for p in title_parts if p])
    if not title:
        title = todo_type
    description = str(todo.get("description") or "").strip()
    if source_file:
        description = f"{description}\n來源 PDF：{source_file}".strip()
    if todo_time:
        start = f"{todo_date}T{todo_time}"
        try:
            end_dt = datetime.fromisoformat(start) + timedelta(hours=1)
            end = end_dt.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            end = start
        is_all_day = 0
    else:
        start = todo_date
        try:
            end = (datetime.fromisoformat(todo_date) + timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            end = todo_date
        is_all_day = 1
    return {
        "event_id": f"osc-pdf-{uuid.uuid4().hex[:20]}",
        "title": title,
        "summary": todo_type,
        "description": description,
        "start_date": start,
        "end_date": end,
        "color": _event_color(todo_type),
        "location": "",
        "is_all_day": is_all_day,
        "reminder_minutes": 1440 if is_all_day else 60,
        "case_number": case_number,
        "raw_data": {"source": "pdf_calendar_scan", "todo": todo, "source_file": source_file},
    }


def _create_calendar_share_link(path: Path) -> dict[str, Any]:
    """Create a MAGI/Paperclip share URL for calendar descriptions."""
    try:
        from api.blueprints import osc_files

        local = osc_files._resolve_safe_file(str(path))
        if not local:
            return {"ok": False, "error": "file_not_found_or_not_allowed"}
        public_probe, probe_mode = osc_files._share_url_for_token("probe")
        if not public_probe:
            return {"ok": False, "error": probe_mode}
        public_base = public_probe.rsplit("/s/", 1)[0].rstrip("/")
        try:
            with urllib.request.urlopen(public_base + "/health", timeout=10) as resp:
                if not (200 <= int(resp.status) < 300):
                    return {"ok": False, "error": f"share_public_base_unhealthy:{resp.status}"}
        except Exception as exc:
            return {"ok": False, "error": f"share_public_base_unreachable:{type(exc).__name__}"}
        token = secrets.token_urlsafe(32)
        token_hash = osc_files._share_token_hash(token)
        now = int(time.time())
        ttl = int(os.environ.get("MAGI_OSC_PDF_CALENDAR_SHARE_TTL_SEC") or osc_files._MAX_SHARE_TTL_SEC)
        ttl = max(300, min(ttl, osc_files._MAX_SHARE_TTL_SEC))
        st = osc_files._stat_with_retry(local)
        public_url, url_mode = osc_files._share_url_for_token(token)
        if not public_url:
            return {"ok": False, "error": url_mode}
        row = {
            "path": local,
            "raw_path": str(path),
            "name": os.path.basename(local),
            "size": int(st.st_size),
            "created_at": now,
            "expires_at": now + ttl,
            "created_by": "osc_pdf_calendar_scan",
            "downloads": 0,
        }
        # Calendar todo generation must not block on copying a NAS-backed PDF.
        # The public share endpoint can stage the file lazily on first access;
        # this keeps deadline creation ahead of tunnel/SMB hiccups.
        if str(os.environ.get("MAGI_OSC_PDF_CALENDAR_SHARE_STAGE_NOW") or "0").strip().lower() in {"1", "true", "yes", "on"}:
            osc_files._ensure_share_cached_copy(token_hash, row, local)
        data = osc_files._prune_share_store(osc_files._load_share_store())
        data.setdefault("shares", {})[token_hash] = row
        osc_files._save_share_store(data)
        return {
            "ok": True,
            "url": public_url,
            "url_mode": url_mode,
            "expires_at": datetime.fromtimestamp(now + ttl).isoformat(timespec="seconds"),
            "name": os.path.basename(local),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


_CALENDAR_SHARE_METADATA_PREFIXES = (
    "MAGI分享連結：",
    "連結有效至：",
    "MAGI分享狀態：",
    "來源PDF：",
)


def _strip_calendar_share_metadata(description: str) -> str:
    """Remove stale calendar share/source metadata before writing a fresh link."""
    lines: list[str] = []
    for line in str(description or "").splitlines():
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _CALENDAR_SHARE_METADATA_PREFIXES):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _append_calendar_share_link(description: str, share: dict[str, Any]) -> str:
    desc = _strip_calendar_share_metadata(description)
    if not share.get("ok") or not share.get("url"):
        return desc
    lines = [desc] if desc else []
    lines.append(f"MAGI分享連結：{share['url']}")
    if share.get("expires_at"):
        lines.append(f"連結有效至：{share['expires_at']}")
    return "\n".join(lines).strip()


def _append_calendar_source_reference(description: str, *, source_path: Path, share: dict[str, Any]) -> str:
    """Keep PDF-created todos traceable even when the public share tunnel is unavailable."""
    desc = _strip_calendar_share_metadata(description)
    if share.get("ok") and share.get("url"):
        return _append_calendar_share_link(desc, share)
    lines = [desc] if desc else []
    lines.append(f"來源PDF：{source_path}")
    if share:
        lines.append(f"MAGI分享狀態：分享連結暫不可用（{share.get('error') or 'unknown'}）")
    return "\n".join(lines).strip()


def _insert_todo(item: dict[str, Any], *, case_number: str, client_name: str, source_file: str, allow_duplicates: bool) -> str:
    todo_type = str(item.get("type") or "待辦").strip()
    todo_date = str(item.get("date") or "").strip() or None
    todo_time = str(item.get("time") or "").strip() or None
    desc = str(item.get("description") or "").strip()
    if not allow_duplicates:
        existing, _ = _osc_exec(
            """
            SELECT id, description, client_name, status FROM case_todos
            WHERE case_number=%s
              AND todo_type=%s
              AND ((todo_date=%s) OR (%s IS NULL AND todo_date IS NULL))
              AND ((todo_time=%s) OR (%s IS NULL AND todo_time IS NULL))
              AND (status IS NULL OR status='' OR status!='deleted')
              AND (source_file IS NULL OR source_file NOT LIKE 'gcal_import%%')
            LIMIT 1
            """,
            (case_number, todo_type, todo_date, todo_date, todo_time, todo_time),
            fetch="one",
        )
        if existing:
            existing_desc = str((existing or {}).get("description") or "").strip()
            existing_client = str((existing or {}).get("client_name") or "").strip()
            existing_status = str((existing or {}).get("status") or "").strip().lower()
            row_id = int((existing or {}).get("id") or 0)
            if existing_status in {"completed", "done", "已完成"}:
                return "skipped"
            if row_id and (desc and desc != existing_desc or client_name and client_name != existing_client):
                _osc_exec(
                    "UPDATE case_todos SET client_name=%s, description=%s, status='pending', source_file=%s WHERE id=%s",
                    (client_name, desc, source_file, row_id),
                    fetch="none",
                )
                return "updated"
            return "skipped"
    _osc_exec(
        """
        INSERT INTO case_todos
          (case_number, client_name, todo_type, todo_date, todo_time, description, source_file, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')
        """,
        (case_number, client_name, todo_type, todo_date, todo_time, desc, source_file),
        fetch="none",
    )
    return "inserted"


def _insert_todos_single_machine(
    todos: list[dict[str, Any]],
    *,
    case_number: str,
    client_name: str,
    source_file: str,
    allow_duplicates: bool,
) -> dict[str, int]:
    """Use the original OSC headless single-machine todo writer."""
    skill_dir = _repo_root() / "skills" / "osc-orchestrator"
    if str(skill_dir) not in sys.path:
        sys.path.insert(0, str(skill_dir))
    from osc_headless.db import connect_mysql, db_config_from_env, ensure_osc_min_schema, insert_case_todos  # type: ignore

    conn = None
    try:
        conn = connect_mysql(db_config_from_env())
        ensure_osc_min_schema(conn)
        return insert_case_todos(
            conn,
            case_number=case_number,
            client_name=client_name,
            todos=todos,
            source_file=source_file,
            allow_duplicates=allow_duplicates,
        )
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 567, exc_info=True)


def _insert_calendar_event(event: dict[str, Any], *, allow_duplicates: bool) -> str:
    if not allow_duplicates:
        existing, _ = _osc_exec(
            """
            SELECT id FROM calendar_events
            WHERE title=%s AND start_date=%s AND COALESCE(case_number,'')=%s
            LIMIT 1
            """,
            (event["title"], event["start_date"], event.get("case_number") or ""),
            fetch="one",
        )
        if existing:
            return "skipped"
    _osc_exec(
        """
        INSERT INTO calendar_events
          (event_id, title, summary, description, start_date, end_date, color, location,
           is_all_day, reminder_minutes, raw_data, case_number)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            event["event_id"],
            event["title"],
            event.get("summary"),
            event.get("description"),
            event["start_date"],
            event["end_date"],
            event.get("color") or "#0ea5e9",
            event.get("location") or None,
            int(event.get("is_all_day") or 0),
            int(event.get("reminder_minutes") or 0),
            json.dumps(_json_safe(event.get("raw_data") or {}), ensure_ascii=False),
            event.get("case_number") or None,
        ),
        fetch="none",
    )
    return "inserted"


def _scan_pdf_for_calendar(
    path: Path,
    *,
    case_number: str = "",
    client_name: str = "",
    max_pages: int = 5,
    include_share_link: bool = False,
    scan_text: bool = True,
    text_when_filename: bool | None = None,
) -> dict[str, Any]:
    inferred = _infer_case_from_path(path)
    case_number = (case_number or inferred.get("case_number") or "").strip()
    client_name = (client_name or inferred.get("client_name") or "").strip()
    extract_todos_from_filename, _get_default_patterns = _load_headless_todo_helpers()
    patterns = _load_todo_patterns()
    filename_todos = extract_todos_from_filename(path.name, str(path), patterns=patterns)
    text = ""
    text_error = ""
    text_max_mb = float(os.environ.get("OSC_PDF_CALENDAR_TEXT_MAX_MB", "8") or "8")
    if text_when_filename is None:
        text_when_filename = str(os.environ.get("OSC_PDF_CALENDAR_TEXT_WHEN_FILENAME", "0")).strip().lower() in {"1", "true", "yes", "on"}
    try:
        file_size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        file_size_mb = 0.0
    if not scan_text:
        text_error = "skipped_text_bulk_scan"
    elif filename_todos and not text_when_filename:
        text_error = "skipped_text_filename_todos"
    elif filename_todos and text_max_mb > 0 and file_size_mb > text_max_mb:
        text_error = f"skipped_large_pdf:{file_size_mb:.1f}MB"
    else:
        try:
            text = _pdf_text(path, max_pages=max_pages)
        except Exception as exc:
            # Synology Drive can leave a cloud placeholder with a .pdf suffix before
            # the real bytes are local.  Filename rules still carry court dates, so
            # do not let a transient unreadable PDF drop calendar todos.
            text_error = f"{type(exc).__name__}: {str(exc)[:180]}"
    text_todos = _extract_todos_from_pdf_text(path, text) if text else []
    todos = _dedupe_todos([*filename_todos, *text_todos])
    if not todos:
        todos = _tentative_no_deadline_todo(path, text)
    todos = [_json_safe(t) for t in todos]
    share_link = _create_calendar_share_link(path) if include_share_link and todos else {}
    if include_share_link and todos:
        for todo in todos:
            todo["description"] = _append_calendar_source_reference(str(todo.get("description") or ""), source_path=path, share=share_link)
    events = [
        _todo_to_calendar_event(t, case_number=case_number, client_name=client_name, source_file=str(path))
        for t in todos
        if str(t.get("date") or "").strip()
    ]
    out = {
        "path": str(path),
        "file_name": path.name,
        "case_number": case_number,
        "client_name": client_name,
        "text_available": bool(text),
        "text_error": text_error,
        "todos": todos,
        "events": events,
    }
    if include_share_link:
        if share_link.get("ok"):
            out["share_link"] = share_link
        else:
            out["share_warning"] = share_link.get("error") or "share_link_unavailable"
    return out


def _iter_scan_targets(raw_path: str, recursive: bool, limit: int) -> list[Path]:
    base = Path(str(raw_path or "").strip()).expanduser()
    if not base.exists():
        raise ValueError("找不到指定的 PDF 或資料夾")
    if base.is_file():
        if base.suffix.lower() != ".pdf":
            raise ValueError("目前僅支援 PDF 檔案")
        return [base.resolve()]
    pattern = "**/*.pdf" if recursive else "*.pdf"
    items = [p.resolve() for p in base.glob(pattern) if p.is_file() and not p.name.startswith(".")]
    return items[: max(1, min(limit, 2000))]


def _count_all_case_pdf_case_rows() -> int:
    rows, _ = _osc_exec(
        """
        SELECT COUNT(*) AS count
        FROM cases
        WHERE folder_path IS NOT NULL AND folder_path!=''
          AND """ + _pdf_todo_case_status_sql("status") + """
        """,
        fetch="one",
    )
    if isinstance(rows, dict):
        return int(rows.get("count") or 0)
    if isinstance(rows, (list, tuple)) and rows:
        return int(rows[0] or 0)
    return 0


def _parse_index_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        logging.getLogger(__name__).debug("timestamp ISO parse failed: %r", text, exc_info=True)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt).timestamp()
        except Exception:
            continue
    return 0.0


def _iter_indexed_case_pdf_candidates(
    limit: int,
    *,
    existing_sources: set[tuple[str, str]],
) -> list[tuple[int, int, float, str, Path, str, str]]:
    """Use document_index as the fast path for full filename sweeps.

    Synology Drive/NAS path walking can time out before reaching fresh court
    PDFs.  The index already records file paths, but older rows often have an
    empty document_index.case_number, so we infer the case from the folder name
    and keep only open cases.
    """
    max_items = max(1, min(limit, 50000))
    case_rows, _ = _osc_exec(
        """
        SELECT case_number, client_name, folder_path, status
        FROM cases
        WHERE folder_path IS NOT NULL AND folder_path!=''
          AND """ + _pdf_todo_case_status_sql("status") + """
        """,
        fetch="all",
    )
    open_cases: dict[str, dict[str, Any]] = {}
    for row in case_rows or []:
        case_number = str((row or {}).get("case_number") or "").strip()
        if not case_number:
            continue
        open_cases[case_number] = row
    if not open_cases:
        return []

    indexed_limit = max(max_items * 6, int(os.environ.get("OSC_PDF_CALENDAR_INDEX_TARGET_LIMIT", "2500") or "2500"))
    indexed_limit = max(100, min(indexed_limit, 50000))
    try:
        rows, _ = _osc_exec(
            """
            SELECT case_number, file_path, file_name, party, subfolder_name, modified_date, id
            FROM document_index
            WHERE (
                    LOWER(COALESCE(file_name, '')) LIKE '%%.pdf'
                 OR LOWER(COALESCE(file_path, '')) LIKE '%%.pdf'
            )
              AND (
                    file_path LIKE '%%法院通知%%'
                 OR file_path LIKE '%%程序裁定%%'
                 OR file_path LIKE '%%判決書或終局裁定及處分%%'
                 OR file_path LIKE '%%判決書%%'
                 OR file_path LIKE '%%開庭通知%%'
                 OR file_path LIKE '%%法庭通知%%'
                 OR file_path LIKE '%%庭期通知%%'
                 OR file_path LIKE '%%地檢署通知%%'
                 OR file_path LIKE '%%檢察署通知%%'
                 OR file_path LIKE '%%專員來信%%'
                 OR file_name LIKE '%%開庭%%'
                 OR file_name LIKE '%%調解%%'
                 OR file_name LIKE '%%審理%%'
                 OR file_name LIKE '%%補正%%'
                 OR file_name LIKE '%%陳報%%'
                 OR file_name LIKE '%%繳費%%'
                 OR file_name LIKE '%%上訴%%'
                 OR file_name LIKE '%%抗告%%'
              )
            ORDER BY modified_date DESC, id DESC
            LIMIT %s
            """,
            (indexed_limit,),
            fetch="all",
        )
    except Exception:
        logging.getLogger(__name__).warning("document_index pdf candidate fast path failed", exc_info=True)
        return []

    candidates: list[tuple[int, int, float, str, Path, str, str]] = []
    seen_paths: set[str] = set()
    seen_case_names: set[tuple[str, str]] = set()
    for row in rows or []:
        raw_path = str((row or {}).get("file_path") or "").strip()
        if not raw_path:
            raw_path = str((row or {}).get("file_name") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        path_key = str(path)
        if path_key in seen_paths:
            continue
        if not _is_pdf_calendar_candidate_path(path):
            continue
        inferred_case, inferred_client = _case_folder_identity_from_path(path)
        row_case = str((row or {}).get("case_number") or "").strip()
        case_number = row_case if row_case in open_cases else inferred_case
        if case_number not in open_cases:
            continue
        case_name_key = (case_number, path.name)
        if case_name_key in seen_case_names:
            continue
        case_row = open_cases[case_number]
        client_name = (
            str(case_row.get("client_name") or "").strip()
            or str((row or {}).get("party") or "").strip()
            or inferred_client
        )
        source_text = path_key
        processed_rank = 1 if (
            (case_number, source_text) in existing_sources
            or (case_number, path.name) in existing_sources
        ) else 0
        hint_rank = 0 if _PDF_TODO_HINT_RE.search(path.name) else 1
        mtime = _parse_index_timestamp((row or {}).get("modified_date"))
        candidates.append((processed_rank, hint_rank, -mtime, path.name, path, case_number, client_name))
        seen_paths.add(path_key)
        seen_case_names.add(case_name_key)
        if len(candidates) >= max_items * 4:
            break
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return candidates[:max_items]


def _iter_all_case_pdf_targets(
    limit: int,
    *,
    case_offset: int = 0,
    case_batch: int | None = None,
    filename_only: bool = False,
) -> list[tuple[Path, str, str]]:
    from api.case_path_mapper import _is_dir_accessible, local_case_path_candidates

    started = time.monotonic()
    if filename_only:
        target_budget_sec = max(0, int(os.environ.get("OSC_PDF_CALENDAR_FILENAME_TARGET_BUDGET_SEC", "180") or "180"))
    else:
        target_budget_sec = max(0, int(os.environ.get("OSC_PDF_CALENDAR_TARGET_BUDGET_SEC", "45") or "45"))
    max_items = max(1, min(limit, 50000))
    row_limit = max(1, min(10000, int(case_batch or 0) or max(20, max_items * 2)))
    row_offset = max(0, int(case_offset or 0))
    rows, _ = _osc_exec(
        """
        SELECT case_number, client_name, folder_path
        FROM cases
        WHERE folder_path IS NOT NULL AND folder_path!=''
          AND """ + _pdf_todo_case_status_sql("status") + """
        ORDER BY updated_at DESC, created_date DESC, case_number DESC
        LIMIT %s OFFSET %s
        """,
        (row_limit, row_offset),
        fetch="all",
    )
    recent_sweep_hours = 0 if filename_only else max(0, int(os.environ.get("OSC_PDF_CALENDAR_RECENT_SWEEP_HOURS", "96") or "96"))
    recent_case_limit = 0 if filename_only else max(0, min(1000, int(os.environ.get("OSC_PDF_CALENDAR_RECENT_SWEEP_CASE_LIMIT", "300") or "300")))
    recent_cutoff = time.time() - recent_sweep_hours * 3600
    recent_rows: list[dict[str, Any]] = []
    if recent_sweep_hours and recent_case_limit:
        try:
            recent_rows, _ = _osc_exec(
                """
                SELECT case_number, client_name, folder_path
                FROM cases
                WHERE folder_path IS NOT NULL AND folder_path!=''
                  AND """ + _pdf_todo_case_status_sql("status") + """
                ORDER BY updated_at DESC, created_date DESC, case_number DESC
                LIMIT %s
                """,
                (recent_case_limit,),
                fetch="all",
            )
        except Exception:
            recent_rows = []
    out: list[tuple[Path, str, str]] = []
    candidates: list[tuple[int, int, float, str, Path, str, str]] = []
    wanted = _PDF_CALENDAR_SOURCE_HINTS
    wanted_dir_names = _PDF_CALENDAR_SOURCE_DIR_NAMES
    candidate_cap = max_items if filename_only else min(max(max_items * 4, max_items, 4), max_items * 8)
    existing_sources: set[tuple[str, str]] = set()

    try:
        source_rows, _ = _osc_exec(
            """
            SELECT case_number, source_file
            FROM case_todos
            WHERE source_file IS NOT NULL AND source_file!=''
              AND COALESCE(status, '') <> 'deleted'
            """,
            fetch="all",
        )
        existing_sources = {
            (str(r.get("case_number") or ""), Path(str(r.get("source_file") or "")).name)
            for r in (source_rows or [])
            if str(r.get("source_file") or "").strip()
        }
        existing_sources.update(
            {
                (str(r.get("case_number") or ""), str(r.get("source_file") or ""))
                for r in (source_rows or [])
                if str(r.get("source_file") or "").strip()
            }
        )
    except Exception:
        existing_sources = set()

    if filename_only:
        candidates.extend(_iter_indexed_case_pdf_candidates(candidate_cap, existing_sources=existing_sources))
    candidate_path_keys: set[str] = set()
    for _processed_rank, _hint_rank, _mtime, _name, pdf, _case_number, _client_name in candidates:
        candidate_path_keys.add(str(pdf))
        try:
            candidate_path_keys.add(str(pdf.resolve()))
        except Exception:
            logging.getLogger(__name__).warning(
                "pdf calendar candidate path resolve failed: %s",
                pdf,
                exc_info=True,
            )

    def _run_path_probe(fn, *, timeout_sec: float = 3.0, fallback=None):
        import threading

        box: dict[str, Any] = {"value": fallback}

        def _runner():
            try:
                box["value"] = fn()
            except Exception:
                box["value"] = fallback

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)
        return fallback if t.is_alive() else box.get("value", fallback)

    def _pdfs_shallow_timeout(root: Path, *, is_case_root: bool) -> list[Path]:
        def _scan() -> list[Path]:
            found = list(root.glob("*.pdf"))
            if is_case_root:
                return found
            for child in root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    found.extend(child.glob("*.pdf"))
            return found

        return list(_run_path_probe(_scan, timeout_sec=5.0, fallback=[]) or [])

    def _pdf_mtime_timeout(pdf: Path) -> float:
        return float(_run_path_probe(lambda: pdf.stat().st_mtime, timeout_sec=1.5, fallback=0.0) or 0.0)

    def _pdf_resolve_timeout(pdf: Path) -> Path:
        return _run_path_probe(lambda: pdf.resolve(), timeout_sec=1.5, fallback=pdf) or pdf

    def _relevant_roots(folder: Path) -> list[Path]:
        def _scan() -> list[Path]:
            roots: list[Path] = []
            if any(k in str(folder) for k in wanted):
                roots.append(folder)
            for name in wanted_dir_names:
                child = folder / name
                try:
                    if child.is_dir():
                        roots.append(child)
                except OSError:
                    continue
            for laf_name in _PDF_LAF_DATA_DIR_NAMES:
                laf_root = folder / laf_name
                for mail_name in _PDF_LAF_STAFF_MAIL_DIR_NAMES:
                    child = laf_root / mail_name
                    try:
                        if child.is_dir():
                            roots.append(child)
                    except OSError:
                        continue
            for mail_name in _PDF_LAF_STAFF_MAIL_DIR_NAMES:
                child = folder / mail_name
                try:
                    if child.is_dir():
                        roots.append(child)
                except OSError:
                    continue
            return roots

        roots = list(_run_path_probe(_scan, timeout_sec=4.0, fallback=[]) or [])
        return roots

    def _iter_relevant_pdfs(root: Path, *, is_case_root: bool):
        # Court notice/ruling/judgment folders should be shallow.  Avoid a deep
        # rglob here because one large archive folder can stall the six-hour
        # todo refresh and prevent newer files from being reached.
        yield from _pdfs_shallow_timeout(root, is_case_root=is_case_root)

    def _candidate_processed_rank(case_number: str, pdf: Path) -> int:
        source_text = str(pdf)
        return 1 if (
            (case_number, source_text) in existing_sources
            or (case_number, pdf.name) in existing_sources
        ) else 0

    def _append_pdf_candidate(pdf: Path, case_number: str, client_name: str, *, priority_final_doc: bool = False) -> None:
        if pdf.name.startswith(".") or pdf.name.startswith("~$"):
            return
        if not _is_pdf_calendar_candidate_path(pdf):
            return
        resolved_pdf = _pdf_resolve_timeout(pdf)
        path_keys = {str(pdf), str(resolved_pdf)}
        if any(key in candidate_path_keys for key in path_keys):
            return
        processed_rank = _candidate_processed_rank(case_number, pdf)
        if priority_final_doc and processed_rank == 0:
            processed_rank = -1
        hint_rank = 0 if _PDF_TODO_HINT_RE.search(pdf.name) else 1
        candidates.append((processed_rank, hint_rank, -_pdf_mtime_timeout(pdf), pdf.name, resolved_pdf, case_number, client_name))
        candidate_path_keys.update(path_keys)

    def _case_judgment_roots(folder: Path) -> list[Path]:
        roots: list[Path] = []
        for name in wanted_dir_names:
            if not path_has_judgment_folder(name):
                continue
            child = folder / name
            try:
                if child.is_dir():
                    roots.append(child)
            except OSError:
                continue

        def _scan_children() -> list[Path]:
            out: list[Path] = []
            for child in folder.iterdir():
                try:
                    if child.is_dir() and path_has_judgment_folder(child.name):
                        out.append(child)
                except OSError:
                    continue
            return out

        roots.extend(_run_path_probe(_scan_children, timeout_sec=4.0, fallback=[]) or [])
        return list(dict.fromkeys(roots))

    def _append_priority_final_doc_candidates() -> None:
        priority_limit = max(0, min(2000, int(os.environ.get("OSC_PDF_CALENDAR_FINAL_DOC_PRIORITY_CASE_LIMIT", "500") or "500")))
        if priority_limit <= 0:
            return
        try:
            priority_rows, _ = _osc_exec(
                """
                SELECT case_number, client_name, folder_path, status
                FROM cases
                WHERE folder_path IS NOT NULL AND folder_path!=''
                  AND case_number IS NOT NULL AND case_number!='' AND case_number!='0000-0000'
                  AND """ + _pdf_todo_case_status_sql("status") + """
                ORDER BY updated_at DESC, created_date DESC, case_number DESC
                LIMIT %s
                """,
                (priority_limit,),
                fetch="all",
            )
        except Exception:
            logging.getLogger(__name__).warning("priority final-doc pdf candidate scan failed", exc_info=True)
            return

        priority_candidate_cap = candidate_cap + max(50, priority_limit)
        for row in priority_rows or []:
            if len(candidates) >= priority_candidate_cap:
                break
            raw_folder = str((row or {}).get("folder_path") or "").strip()
            folder: Path | None = None
            for candidate in local_case_path_candidates(raw_folder):
                cand = Path(candidate).expanduser()
                if _is_dir_accessible(str(cand)):
                    folder = cand
                    break
            if folder is None:
                continue
            case_number = str((row or {}).get("case_number") or "")
            client_name = str((row or {}).get("client_name") or "")
            for root in _case_judgment_roots(folder):
                for pdf in _iter_relevant_pdfs(root, is_case_root=False):
                    _append_pdf_candidate(pdf, case_number, client_name, priority_final_doc=True)
                    if len(candidates) >= priority_candidate_cap:
                        break
                if len(candidates) >= priority_candidate_cap:
                    break

    _append_priority_final_doc_candidates()
    if filename_only and len(candidates) >= candidate_cap:
        candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        return [(pdf, case_number, client_name) for _p, _h, _m, _n, pdf, case_number, client_name in candidates[:max_items]]

    row_entries: list[tuple[dict[str, Any], bool]] = []
    seen_rows: set[tuple[str, str]] = set()

    def _add_rows(items: Any, *, recent_only: bool) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("case_number") or ""), str(item.get("folder_path") or ""))
            if key in seen_rows:
                continue
            seen_rows.add(key)
            row_entries.append((item, recent_only))

    # Recent PDFs must win over the rotating cursor batch.  Otherwise a newly
    # arrived court notice can wait several six-hour cycles behind older cases.
    # The filename-only sweep is different: it is the governance pass and must
    # walk the full open-case batch without the recent-file shortcut/filter.
    if not filename_only:
        _add_rows(recent_rows, recent_only=True)
    _add_rows(rows, recent_only=False)

    for row, recent_only in row_entries:
        if len(candidates) >= candidate_cap:
            break
        if target_budget_sec and time.monotonic() - started > target_budget_sec:
            break
        raw_folder = str(row.get("folder_path") or "").strip()
        folder: Path | None = None
        for candidate in local_case_path_candidates(raw_folder):
            cand = Path(candidate).expanduser()
            if _is_dir_accessible(str(cand)):
                folder = cand
                break
        if folder is None:
            continue
        for root in _relevant_roots(folder):
            for pdf in _iter_relevant_pdfs(root, is_case_root=(root == folder and not any(k in str(root) for k in wanted))):
                if len(candidates) >= candidate_cap:
                    break
                if target_budget_sec and time.monotonic() - started > target_budget_sec:
                    break
                if pdf.name.startswith(".") or pdf.name.startswith("~$"):
                    continue
                if not _is_pdf_calendar_candidate_path(pdf):
                    continue
                case_number = str(row.get("case_number") or "")
                mtime = _pdf_mtime_timeout(pdf)
                if not filename_only and recent_only and recent_sweep_hours and mtime < recent_cutoff:
                    continue
                resolved_pdf = _pdf_resolve_timeout(pdf)
                path_keys = {str(pdf), str(resolved_pdf)}
                if any(key in candidate_path_keys for key in path_keys):
                    continue
                processed_rank = _candidate_processed_rank(case_number, pdf)
                hint_rank = 0 if _PDF_TODO_HINT_RE.search(pdf.name) else 1
                candidates.append((processed_rank, hint_rank, -mtime, pdf.name, resolved_pdf, case_number, str(row.get("client_name") or "")))
                candidate_path_keys.update(path_keys)

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    for _processed_rank, _hint_rank, _mtime, _name, pdf, case_number, client_name in candidates[:max_items]:
        out.append((pdf, case_number, client_name))
    return out


def _output_path(input_path: Path, action: str, ext: str = ".pdf") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_action = "".join(ch for ch in action if ch.isalnum() or ch in {"_", "-"}) or "out"
    return input_path.with_name(f"{input_path.stem}_{safe_action}_{stamp}{ext}")


def _parse_pages(raw: str, page_count: int, *, allow_empty: bool = True) -> list[int]:
    text = str(raw or "").strip()
    if not text:
        if allow_empty:
            return list(range(page_count))
        raise ValueError("請輸入頁碼")
    pages: set[int] = set()
    for part in text.replace("，", ",").split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            left, right = [x.strip() for x in item.split("-", 1)]
            start = int(left)
            end = int(right)
            if start > end:
                start, end = end, start
            for page in range(start, end + 1):
                pages.add(page - 1)
        else:
            pages.add(int(item) - 1)
    valid = sorted(p for p in pages if 0 <= p < page_count)
    if not valid:
        raise ValueError("頁碼超出 PDF 範圍")
    return valid


def _parse_ranges(raw: str, page_count: int) -> list[tuple[int, int]]:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("請輸入拆分範圍，例如 1-3,4-6")
    ranges: list[tuple[int, int]] = []
    for part in text.replace("，", ",").split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            left, right = [x.strip() for x in item.split("-", 1)]
            start = int(left) - 1
            end = int(right) - 1
        else:
            start = end = int(item) - 1
        if start > end:
            start, end = end, start
        start = max(0, min(start, page_count - 1))
        end = max(0, min(end, page_count - 1))
        ranges.append((start, end))
    if not ranges:
        raise ValueError("沒有可用的拆分範圍")
    return ranges


def _save_doc(doc: fitz.Document, output: Path) -> None:
    doc.save(str(output), garbage=4, deflate=True)


def _add_watermark(page: fitz.Page, text: str, font_size: float) -> None:
    rect = page.rect
    center = fitz.Point(rect.x0 + rect.width / 2, rect.y0 + rect.height / 2)
    angle = math.radians(-35)
    matrix = fitz.Matrix(math.cos(angle), math.sin(angle), -math.sin(angle), math.cos(angle), 0, 0)
    start = fitz.Point(center.x - min(rect.width * 0.38, len(text) * font_size * 0.26), center.y)
    shape = page.new_shape()
    shape.insert_text(
        start,
        text,
        fontsize=font_size,
        color=(0.68, 0.68, 0.68),
        fill=(0.68, 0.68, 0.68),
        render_mode=0,
        morph=(center, matrix),
    )
    shape.commit(overlay=True)


def _info(path: Path) -> dict[str, Any]:
    doc = fitz.open(path)
    try:
        metadata = doc.metadata or {}
        encrypted = bool(doc.needs_pass)
        return {
            "file_name": path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "page_count": doc.page_count,
            "encrypted": encrypted,
            "metadata": {
                "title": metadata.get("title") or "",
                "author": metadata.get("author") or "",
                "subject": metadata.get("subject") or "",
                "creator": metadata.get("creator") or "",
                "producer": metadata.get("producer") or "",
            },
        }
    finally:
        doc.close()


@osc_pdf_bp.route("/api/osc/pdf/info", methods=["GET"])
@login_required
def osc_pdf_info_api():
    try:
        path = _path_from_request(request.args.get("path") or "")
        return jsonify({"ok": True, "item": _info(path)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@osc_pdf_bp.route("/api/osc/pdf/upload", methods=["POST"])
@login_required
def osc_pdf_upload_api():
    try:
        upload = request.files.get("file")
        if not upload or not upload.filename:
            raise ValueError("請選擇要上傳的 PDF")
        original = secure_filename(upload.filename) or "upload.pdf"
        if Path(original).suffix.lower() != ".pdf":
            raise ValueError("目前僅支援 PDF 檔案")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output = _upload_dir() / f"{Path(original).stem}_{stamp}.pdf"
        upload.save(output)
        path = _path_from_request(str(output))
        return jsonify({"ok": True, "path": str(path), "item": _info(path), "message": "PDF 已上傳並帶入工具"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@osc_pdf_bp.route("/api/osc/pdf/action", methods=["POST"])
@login_required
def osc_pdf_action_api():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "").strip()
    try:
        path = _path_from_request(data.get("file_path") or "")
        if action == "info":
            return jsonify({"ok": True, "item": _info(path)})

        if action == "extract_text":
            doc = fitz.open(path)
            try:
                pages = _parse_pages(data.get("pages") or "", doc.page_count)
                text = "\n\n".join(doc[i].get_text("text") for i in pages).strip()
            finally:
                doc.close()
            output = _output_path(path, "text", ".txt")
            output.write_text(text, encoding="utf-8")
            return jsonify({"ok": True, "outputs": [str(output)], "message": "文字已抽出"})

        if action == "rotate":
            angle = int(data.get("angle") or 90)
            if angle not in {90, 180, 270}:
                raise ValueError("旋轉角度僅支援 90、180、270")
            doc = fitz.open(path)
            try:
                pages = _parse_pages(data.get("pages") or "", doc.page_count)
                for i in pages:
                    page = doc[i]
                    page.set_rotation((page.rotation + angle) % 360)
                output = _output_path(path, f"rotate{angle}")
                _save_doc(doc, output)
            finally:
                doc.close()
            return jsonify({"ok": True, "outputs": [str(output)], "message": "旋轉完成"})

        if action == "extract_pages":
            doc = fitz.open(path)
            try:
                pages = _parse_pages(data.get("pages") or "", doc.page_count, allow_empty=False)
                out_doc = fitz.open()
                try:
                    out_doc.insert_pdf(doc, from_page=0, to_page=doc.page_count - 1)
                    out_doc.select(pages)
                    output = _output_path(path, "pages")
                    _save_doc(out_doc, output)
                finally:
                    out_doc.close()
            finally:
                doc.close()
            return jsonify({"ok": True, "outputs": [str(output)], "message": "頁面已擷取"})

        if action == "split_ranges":
            doc = fitz.open(path)
            outputs: list[str] = []
            try:
                ranges = _parse_ranges(data.get("ranges") or data.get("pages") or "", doc.page_count)
                for idx, (start, end) in enumerate(ranges, start=1):
                    out_doc = fitz.open()
                    try:
                        out_doc.insert_pdf(doc, from_page=start, to_page=end)
                        output = path.with_name(
                            f"{path.stem}_part{idx}_{start + 1}-{end + 1}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
                        )
                        _save_doc(out_doc, output)
                        outputs.append(str(output))
                    finally:
                        out_doc.close()
            finally:
                doc.close()
            return jsonify({"ok": True, "outputs": outputs, "message": "拆分完成"})

        if action == "merge":
            other_paths = data.get("other_paths") or data.get("other_path") or ""
            if isinstance(other_paths, str):
                candidates = [x.strip() for x in other_paths.replace("\n", ",").split(",") if x.strip()]
            else:
                candidates = [str(x).strip() for x in other_paths if str(x).strip()]
            if not candidates:
                raise ValueError("請指定要合併的 PDF")
            doc = fitz.open(path)
            try:
                for item in candidates:
                    other = _path_from_request(item)
                    src = fitz.open(other)
                    try:
                        doc.insert_pdf(src)
                    finally:
                        src.close()
                output = _output_path(path, "merged")
                _save_doc(doc, output)
            finally:
                doc.close()
            return jsonify({"ok": True, "outputs": [str(output)], "message": "合併完成"})

        if action == "watermark":
            text = str(data.get("text") or "").strip()
            if not text:
                raise ValueError("請輸入浮水印文字")
            doc = fitz.open(path)
            try:
                pages = _parse_pages(data.get("pages") or "", doc.page_count)
                for i in pages:
                    _add_watermark(doc[i], text, float(data.get("font_size") or 52))
                output = _output_path(path, "watermark")
                _save_doc(doc, output)
            finally:
                doc.close()
            return jsonify({"ok": True, "outputs": [str(output)], "message": "浮水印已加入"})

        if action == "optimize":
            doc = fitz.open(path)
            try:
                output = _output_path(path, "optimized")
                _save_doc(doc, output)
            finally:
                doc.close()
            return jsonify({"ok": True, "outputs": [str(output)], "message": "最佳化完成"})

        if action == "encrypt":
            password = str(data.get("password") or "").strip()
            if not password:
                raise ValueError("請輸入開啟密碼")
            doc = fitz.open(path)
            try:
                output = _output_path(path, "encrypted")
                doc.save(
                    str(output),
                    garbage=4,
                    deflate=True,
                    encryption=fitz.PDF_ENCRYPT_AES_256,
                    user_pw=password,
                    owner_pw=str(data.get("owner_password") or password),
                    permissions=int(fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY),
                )
            finally:
                doc.close()
            return jsonify({"ok": True, "outputs": [str(output)], "message": "PDF 已加密"})

        raise ValueError("不支援的 PDF 動作")
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@osc_pdf_bp.route("/api/osc/pdf/calendar-scan", methods=["POST"])
@login_required
def osc_pdf_calendar_scan_api():
    data = request.get_json(silent=True) or {}
    try:
        raw_path = str(data.get("file_path") or data.get("path") or "").strip()
        all_cases = _safe_bool(data.get("all_cases"), False)
        if not raw_path and not all_cases:
            raise ValueError("請先指定 PDF 路徑、資料夾，或選擇掃描全部案件")
        recursive = _safe_bool(data.get("recursive"), True)
        limit = int(data.get("limit") or 300)
        max_pages = max(1, min(int(data.get("max_pages") or 5), 20))
        write = _safe_bool(data.get("write"), False)
        write_todos = _safe_bool(data.get("write_todos"), True)
        # OSC 單機版正規流程：PDF 建立的是 case_todos；Google 日曆由
        # gcal_sync 讀 case_todos 推送。calendar_events 只保留給明確指定的舊相容模式。
        write_osc_calendar_events = _safe_bool(data.get("write_osc_calendar_events"), False)
        include_share_link = _safe_bool(data.get("include_share_link"), True)
        allow_duplicates = _safe_bool(data.get("allow_duplicates"), False)
        case_number = str(data.get("case_number") or "").strip()
        client_name = str(data.get("client_name") or "").strip()

        if all_cases:
            target_specs = _iter_all_case_pdf_targets(limit=limit)
        else:
            target_specs = [(p, case_number, client_name) for p in _iter_scan_targets(raw_path, recursive=recursive, limit=limit)]
        scanned: list[dict[str, Any]] = []
        todo_inserted = todo_updated = todo_skipped = event_inserted = event_skipped = 0
        for path, target_case_number, target_client_name in target_specs:
            item = _scan_pdf_for_calendar(
                path,
                case_number=target_case_number,
                client_name=target_client_name,
                max_pages=max_pages,
                include_share_link=bool(write and write_todos and include_share_link),
            )
            scanned.append(item)
            if not write:
                continue
            if not item.get("case_number") and write_todos:
                item["write_warning"] = "未判斷案件編號，已略過待辦寫入；請補案件編號後再寫入。"
            if write_todos and item.get("case_number"):
                insert_result = _insert_todos_single_machine(
                    item.get("todos") or [],
                    case_number=item.get("case_number") or "",
                    client_name=item.get("client_name") or "",
                    source_file=str(Path(item.get("path") or "")),
                    allow_duplicates=allow_duplicates,
                )
                item["todo_write"] = insert_result
                todo_inserted += int(insert_result.get("inserted") or 0)
                todo_updated += int(insert_result.get("updated") or 0)
                todo_skipped += int(insert_result.get("skipped") or 0)
            for event in item.get("events") or []:
                if write_osc_calendar_events:
                    status = _insert_calendar_event(event, allow_duplicates=allow_duplicates)
                    if status == "inserted":
                        event_inserted += 1
                    else:
                        event_skipped += 1

        total_todos = sum(len(x.get("todos") or []) for x in scanned)
        total_events = sum(len(x.get("events") or []) for x in scanned)
        message = (
            f"已掃描 {len(scanned)} 份 PDF，找到 {total_todos} 筆待辦、{total_events} 筆行事曆事件。"
            if not write else
            f"已掃描 {len(scanned)} 份 PDF；待辦新增 {todo_inserted} 筆、更新 {todo_updated} 筆、略過 {todo_skipped} 筆；"
            + (
                f"本機行事曆事件新增 {event_inserted} 筆、略過 {event_skipped} 筆。"
                if write_osc_calendar_events
                else "Google 日曆請由原 OSC case_todos 同步流程推送。"
            )
        )
        return jsonify(
            {
                "ok": True,
                "write": write,
                "all_cases": all_cases,
                "scanned_count": len(scanned),
                "todo_count": total_todos,
                "event_count": total_events,
                "todo_inserted": todo_inserted,
                "todo_updated": todo_updated,
                "todo_skipped": todo_skipped,
                "event_inserted": event_inserted,
                "event_skipped": event_skipped,
                "items": scanned,
                "message": message,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
