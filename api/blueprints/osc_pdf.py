from __future__ import annotations

import os
import math
from datetime import datetime
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
