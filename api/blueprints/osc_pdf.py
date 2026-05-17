from __future__ import annotations

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

import fitz
from flask import Blueprint, jsonify, request
from flask_login import login_required
from werkzeug.utils import secure_filename


osc_pdf_bp = Blueprint("osc_pdf", __name__)


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
    if str(skill_dir) not in sys.path:
        sys.path.insert(0, str(skill_dir))
    from osc_headless.todos import extract_todos_from_filename, get_default_patterns  # type: ignore
    return extract_todos_from_filename, get_default_patterns


def _pdf_text(path: Path, max_pages: int = 5) -> str:
    doc = fitz.open(path)
    parts: list[str] = []
    try:
        for idx in range(min(doc.page_count, max(1, max_pages))):
            parts.append(doc[idx].get_text("text") or "")
    finally:
        doc.close()
    return "\n".join(parts).strip()


def _infer_case_from_path(path: Path) -> dict[str, str]:
    text = str(path)
    case_number = ""
    client_name = ""
    for part in reversed(path.parts):
        m = re.search(r"(20\d{2}-\d{3,5})", part)
        if m:
            case_number = m.group(1)
            rest = part.replace(case_number, "")
            rest = re.sub(r"^[\\/_\-\s]+|[\\/_\-\s]+$", "", rest)
            client_name = rest.split("-")[0].split("_")[0].strip()
            break
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
        pass
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
        pass
    return defaults


def _parse_roc_or_ad_date(year: str, month: str, day: str) -> datetime | None:
    try:
        y = int(year)
        if y < 1911:
            y += 1911
        return datetime(y, int(month), int(day))
    except Exception:
        return None


def _parse_ampm_time(period: str, hour: str, minute: str = "") -> tuple[int, int]:
    h = int(hour)
    m = int(minute or "0")
    if "下" in period and h != 12:
        h += 12
    if "上" in period and h == 12:
        h = 0
    return h, m


def _extract_todos_from_pdf_text(path: Path, text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    body = re.sub(r"\s+", "", text or "")
    if not body:
        return items

    hearing_patterns = [
        r"(?:定|訂)於?(?:民國)?(\d{2,4})年(\d{1,2})月(\d{1,2})日([上下]午)(\d{1,2})時(\d{0,2})分?.{0,40}?(開庭|準備程序|言詞辯論|調解|審理|宣判)?",
        r"(?:定|訂)於?(\d{1,2})月(\d{1,2})日([上下]午)(\d{1,2})時(\d{0,2})分?.{0,40}?(開庭|準備程序|言詞辯論|調解|審理|宣判)?",
    ]
    for pattern in hearing_patterns:
        for m in re.finditer(pattern, body):
            if len(m.groups()) == 7:
                dt = _parse_roc_or_ad_date(m.group(1), m.group(2), m.group(3))
                period, hour, minute, proc = m.group(4), m.group(5), m.group(6), m.group(7)
            elif len(m.groups()) == 6:
                # Yearless hearing: use document/mtime year.
                base = datetime.fromtimestamp(path.stat().st_mtime)
                dt = datetime(base.year, int(m.group(1)), int(m.group(2)))
                period, hour, minute, proc = m.group(3), m.group(4), m.group(5), m.group(6)
            else:
                continue
            if not dt:
                continue
            h, mi = _parse_ampm_time(period, hour, minute)
            dt = dt.replace(hour=h, minute=mi)
            kind = proc or "開庭"
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

    doc_date = None
    for m in re.finditer(r"(?:民國)?(\d{2,4})年(\d{1,2})月(\d{1,2})日", body):
        doc_date = _parse_roc_or_ad_date(m.group(1), m.group(2), m.group(3))
        if doc_date:
            break
    if doc_date is None:
        doc_date = datetime.fromtimestamp(path.stat().st_mtime)

    relative_map = [
        ("補正", r"(\d{1,2})日內.{0,25}補正"),
        ("陳述意見", r"(\d{1,2})日內.{0,25}陳述意見"),
        ("繳費", r"(\d{1,2})日內.{0,25}(?:繳納|繳費)"),
        ("上訴", r"(\d{1,2})日內.{0,25}上訴"),
        ("抗告", r"(\d{1,2})日內.{0,25}抗告"),
    ]
    for todo_type, pattern in relative_map:
        m = re.search(pattern, body)
        if not m:
            continue
        days = int(m.group(1))
        deadline = doc_date + timedelta(days=days)
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


def _dedupe_todos(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
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
        out.append(item)
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
    if todo_type in {"開庭", "宣判", "調解", "言詞辯論", "準備程序", "審理"}:
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


def _append_calendar_share_link(description: str, share: dict[str, Any]) -> str:
    desc = str(description or "").strip()
    if not share.get("ok") or not share.get("url"):
        return desc
    lines = [desc] if desc else []
    lines.append(f"MAGI分享連結：{share['url']}")
    if share.get("expires_at"):
        lines.append(f"連結有效至：{share['expires_at']}")
    return "\n".join(lines).strip()


def _insert_todo(item: dict[str, Any], *, case_number: str, client_name: str, source_file: str, allow_duplicates: bool) -> str:
    todo_type = str(item.get("type") or "待辦").strip()
    todo_date = str(item.get("date") or "").strip() or None
    todo_time = str(item.get("time") or "").strip() or None
    desc = str(item.get("description") or "").strip()
    if not allow_duplicates:
        existing, _ = _osc_exec(
            """
            SELECT id, description, client_name FROM case_todos
            WHERE case_number=%s
              AND todo_type=%s
              AND ((todo_date=%s) OR (%s IS NULL AND todo_date IS NULL))
              AND ((todo_time=%s) OR (%s IS NULL AND todo_time IS NULL))
              AND source_file=%s
            LIMIT 1
            """,
            (case_number, todo_type, todo_date, todo_date, todo_time, todo_time, source_file),
            fetch="one",
        )
        if existing:
            existing_desc = str((existing or {}).get("description") or "").strip()
            existing_client = str((existing or {}).get("client_name") or "").strip()
            row_id = int((existing or {}).get("id") or 0)
            if row_id and (desc and desc != existing_desc or client_name and client_name != existing_client):
                _osc_exec(
                    "UPDATE case_todos SET client_name=%s, description=%s WHERE id=%s",
                    (client_name, desc, row_id),
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
            pass


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
) -> dict[str, Any]:
    inferred = _infer_case_from_path(path)
    case_number = (case_number or inferred.get("case_number") or "").strip()
    client_name = (client_name or inferred.get("client_name") or "").strip()
    extract_todos_from_filename, _get_default_patterns = _load_headless_todo_helpers()
    patterns = _load_todo_patterns()
    filename_todos = extract_todos_from_filename(path.name, str(path), patterns=patterns)
    text = _pdf_text(path, max_pages=max_pages)
    text_todos = _extract_todos_from_pdf_text(path, text)
    todos = _dedupe_todos([*filename_todos, *text_todos])
    todos = [_json_safe(t) for t in todos]
    share_link = _create_calendar_share_link(path) if include_share_link else {}
    if share_link.get("ok"):
        for todo in todos:
            todo["description"] = _append_calendar_share_link(str(todo.get("description") or ""), share_link)
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


def _iter_all_case_pdf_targets(limit: int) -> list[tuple[Path, str, str]]:
    rows, _ = _osc_exec(
        """
        SELECT case_number, client_name, folder_path
        FROM cases
        WHERE folder_path IS NOT NULL AND folder_path!=''
          AND (status IS NULL OR status='' OR status NOT IN ('已結案'))
        ORDER BY updated_at DESC, created_date DESC
        LIMIT 2000
        """,
        fetch="all",
    )
    out: list[tuple[Path, str, str]] = []
    wanted = ("法院通知", "程序裁定", "判決書", "法院_通知", "法院_傳票")
    max_items = max(1, min(limit, 5000))
    for row in rows or []:
        folder = Path(str(row.get("folder_path") or "").strip()).expanduser()
        if not folder.exists() or not folder.is_dir():
            continue
        for pdf in folder.rglob("*.pdf"):
            if len(out) >= max_items:
                return out
            if pdf.name.startswith(".") or pdf.name.startswith("~$"):
                continue
            text = str(pdf)
            if wanted and not any(k in text for k in wanted):
                continue
            out.append((pdf.resolve(), str(row.get("case_number") or ""), str(row.get("client_name") or "")))
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
        include_share_link = _safe_bool(data.get("include_share_link"), False)
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
                    source_file=Path(item.get("path") or "").name,
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
