from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, render_template, request


lottery_bp = Blueprint("lottery", __name__)

SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xlmx"}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_ROWS = 20_000

NAME_KEYS = ("姓名", "名字", "抽獎人", "參加者", "中獎人", "收件人", "當事人", "客戶姓名", "name")
PHONE_KEYS = ("電話", "手機", "行動電話", "聯絡電話", "電話號碼", "手機號碼", "phone", "mobile", "tel")
ADDRESS_KEYS = ("地址", "住址", "通訊地址", "戶籍地址", "收件地址", "寄送地址", "address")


def _norm_header(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _find_column(headers: list[str], candidates: tuple[str, ...]) -> str:
    normalized = [(_norm_header(h), h) for h in headers]
    for candidate in candidates:
        key = _norm_header(candidate)
        for norm, original in normalized:
            if norm == key:
                return original
    for candidate in candidates:
        key = _norm_header(candidate)
        for norm, original in normalized:
            if key and key in norm:
                return original
    return ""


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return re.sub(r"\s+", " ", text)


def mask_name(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    if not text:
        return ""
    chars = list(text)
    if len(chars) == 1:
        return "○"
    chars[1] = "○"
    return "".join(chars)


def mask_phone(value: Any) -> str:
    text = str(value or "").strip()
    digits = re.sub(r"\D+", "", text)
    if not digits:
        return ""
    if len(digits) <= 4:
        return "○" * len(digits)
    if len(digits) <= 7:
        return digits[:1] + "○" * (len(digits) - 2) + digits[-1:]
    return digits[:3] + "○" * max(3, len(digits) - 6) + digits[-3:]


def mask_address(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    if not text:
        return ""
    match = re.search(r"^(.{0,8}?[縣市].{0,8}?[區鄉鎮市])", text)
    if match:
        return match.group(1) + "○○○"
    visible = text[: min(3, len(text))]
    return visible + "○○○"


def _read_csv_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            decoded = content.decode(encoding)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        raise ValueError(f"CSV 編碼無法辨識：{last_error}")

    sample = decoded[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except Exception:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(decoded), dialect=dialect)
    headers = [str(h or "").strip() for h in (reader.fieldnames or []) if str(h or "").strip()]
    if not headers:
        raise ValueError("找不到表頭列")
    rows: list[dict[str, str]] = []
    for idx, row in enumerate(reader, start=2):
        item = {h: _clean_cell(row.get(h)) for h in headers}
        if any(item.values()):
            item["_row_number"] = str(idx)
            rows.append(item)
        if len(rows) >= MAX_ROWS:
            break
    return headers, rows


def _read_excel_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover - dependency exists in MAGI runtime
        raise ValueError(f"Excel 讀取模組未安裝：{exc}") from exc
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    raw_rows = ws.iter_rows(values_only=True)
    header_values = None
    header_index = 0
    for header_index, values in enumerate(raw_rows, start=1):
        if values and any(_clean_cell(v) for v in values):
            header_values = list(values)
            break
    if header_values is None:
        raise ValueError("Excel 檔沒有可讀取的資料")
    headers = [_clean_cell(h) or f"欄位{i + 1}" for i, h in enumerate(header_values)]
    rows: list[dict[str, str]] = []
    for offset, values in enumerate(raw_rows, start=header_index + 1):
        values = list(values or [])
        item = {headers[i]: _clean_cell(values[i]) if i < len(values) else "" for i in range(len(headers))}
        if any(item.values()):
            item["_row_number"] = str(offset)
            rows.append(item)
        if len(rows) >= MAX_ROWS:
            break
    return headers, rows


def parse_upload(filename: str, content: bytes) -> dict[str, Any]:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("只支援 CSV、XLSX、XLSM 檔案")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("檔案過大，請控制在 12MB 以內")
    headers, rows = _read_csv_rows(content) if suffix == ".csv" else _read_excel_rows(content)
    if not rows:
        raise ValueError("檔案內沒有可抽獎的資料列")
    name_col = _find_column(headers, NAME_KEYS)
    phone_col = _find_column(headers, PHONE_KEYS)
    address_col = _find_column(headers, ADDRESS_KEYS)
    return {
        "headers": headers,
        "rows": rows,
        "columns": {"name": name_col, "phone": phone_col, "address": address_col},
    }


def _public_row(row: dict[str, str], columns: dict[str, str]) -> dict[str, str]:
    name_col = columns.get("name") or ""
    phone_col = columns.get("phone") or ""
    address_col = columns.get("address") or ""
    return {
        "row_number": str(row.get("_row_number") or ""),
        "name": mask_name(row.get(name_col, "")) if name_col else "",
        "phone": mask_phone(row.get(phone_col, "")) if phone_col else "",
        "address": mask_address(row.get(address_col, "")) if address_col else "",
    }


def draw_lottery(filename: str, content: bytes, winner_count: int) -> dict[str, Any]:
    parsed = parse_upload(filename, content)
    rows = parsed["rows"]
    if winner_count < 1:
        raise ValueError("抽獎人數至少要 1 人")
    if winner_count > len(rows):
        raise ValueError(f"抽獎人數不可超過有效資料筆數（{len(rows)} 筆）")
    shuffled = list(rows)
    secrets.SystemRandom().shuffle(shuffled)
    winners = shuffled[:winner_count]
    alternates = shuffled[winner_count : winner_count + min(10, max(0, len(shuffled) - winner_count))]
    digest = hashlib.sha256(content).hexdigest()
    columns = parsed["columns"]
    return {
        "ok": True,
        "filename": Path(filename or "upload").name,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_rows": len(rows),
        "winner_count": winner_count,
        "source_sha256": digest,
        "detected_columns": columns,
        "winners": [_public_row(row, columns) | {"rank": str(i + 1)} for i, row in enumerate(winners)],
        "alternates": [_public_row(row, columns) | {"rank": str(i + 1)} for i, row in enumerate(alternates)],
    }


@lottery_bp.get("/lottery")
def lottery_page():
    return render_template("lottery.html")


@lottery_bp.post("/api/lottery/draw")
def lottery_draw_api():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "missing_file", "message": "請先上傳 Excel 或 CSV 檔"}), 400
    try:
        winner_count = int(request.form.get("winner_count") or "1")
    except Exception:
        return jsonify({"ok": False, "error": "invalid_winner_count", "message": "抽獎人數必須是數字"}), 400
    try:
        content = upload.read()
        result = draw_lottery(upload.filename, content, winner_count)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"ok": False, "error": "invalid_upload", "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": "draw_failed", "message": f"抽獎失敗：{exc}"}), 500
