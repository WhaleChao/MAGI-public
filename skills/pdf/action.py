#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf / action.py — PDF 瑞士刀
==============================
統一入口：合併、分割、擷取文字、OCR、加密、解密、旋轉、浮水印、表單填寫。

Usage (CLI):
    python action.py --task 'merge --files a.pdf b.pdf c.pdf --output merged.pdf'
    python action.py --task 'split --file input.pdf --pages 1-5,6-10'
    python action.py --task 'extract --file input.pdf'
    python action.py --task 'ocr --file scanned.pdf'
    python action.py --task 'encrypt --file input.pdf --password secret'
    python action.py --task 'decrypt --file input.pdf --password secret'
    python action.py --task 'rotate --file input.pdf --pages 1,3 --degrees 90'
    python action.py --task 'watermark --file input.pdf --watermark wm.pdf'
    python action.py --task 'info --file input.pdf'
    python action.py --task 'images --file input.pdf --output-dir ./images'
    python action.py --task 'help'
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

logger = logging.getLogger("pdf-tool")

# ── 匯出目錄 ──
_DEFAULT_EXPORTS_DIR = _MAGI_ROOT / "static" / "exports"
_EXPORTS_DIR = Path(os.environ.get("MAGI_EXPORTS_DIR", str(_DEFAULT_EXPORTS_DIR)))
_DEFAULT_EXTERNAL_TIMEOUT_SEC = 60.0


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _exports_dir() -> Path:
    return Path(os.environ.get("MAGI_EXPORTS_DIR", str(_DEFAULT_EXPORTS_DIR))).expanduser().resolve(strict=False)


def _split_env_paths(raw: str) -> List[Path]:
    paths: List[Path] = []
    for part in re.split(r"[:;,]", str(raw or "")):
        part = part.strip()
        if part:
            paths.append(Path(part).expanduser())
    return paths


def _allowed_roots() -> List[Path]:
    roots: List[Path] = [_exports_dir()]
    for key in (
        "MAGI_PDF_TOOL_ALLOWED_ROOTS",
        "MAGI_CASE_ROOT",
        "MAGI_ACTIVE_CASE_ROOT",
        "MAGI_CLOSED_CASE_ROOT",
        "MAGI_DRIVE_SYNC_ACTIVE_CASE_ROOT",
        "MAGI_DRIVE_SYNC_CLOSED_CASE_ROOT",
        "SYNOLOGY_CASE_ROOT",
        "SYNOLOGY_CASE_ROOTS",
    ):
        roots.extend(_split_env_paths(os.environ.get(key, "")))
    try:
        from api.case_path_mapper import default_case_roots, preferred_case_roots

        roots.extend(Path(p).expanduser() for p in preferred_case_roots(include_closed=True) if p)
        roots.extend(Path(p).expanduser() for p in default_case_roots(include_closed=True) if p)
    except Exception:
        logger.debug("pdf-tool: case roots unavailable", exc_info=True)

    deduped: List[Path] = []
    seen = set()
    for root in roots:
        try:
            resolved = root.expanduser().resolve(strict=False)
        except Exception:
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            deduped.append(resolved)
    return deduped


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _ensure_under_allowed_root(path: Path, *, purpose: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    for root in _allowed_roots():
        if _is_relative_to(resolved, root):
            return resolved
    roots = ", ".join(str(r) for r in _allowed_roots())
    raise PermissionError(f"{purpose} path outside allowed roots: {resolved} (allowed: {roots})")


def _input_file(path: str, *, suffixes: Tuple[str, ...] = ()) -> Path:
    p = _ensure_under_allowed_root(Path(path), purpose="input")
    if not p.is_file():
        raise FileNotFoundError(f"找不到檔案：{path}")
    if suffixes and p.suffix.lower() not in {s.lower() for s in suffixes}:
        raise ValueError(f"不支援的輸入副檔名：{p.suffix}")
    return p


def _output_file(name: str, user_output: Optional[str] = None) -> Path:
    p = Path(user_output).expanduser() if user_output else _exports_dir() / Path(name).name
    p = _ensure_under_allowed_root(p, purpose="output")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _output_dir(name: str, user_output_dir: Optional[str] = None) -> Path:
    p = Path(user_output_dir).expanduser() if user_output_dir else _exports_dir() / Path(name).name
    p = _ensure_under_allowed_root(p, purpose="output")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _temp_sibling(path: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    return Path(tmp_name)


def _atomic_write_pdf(path: Path, writer: Any) -> None:
    tmp = _temp_sibling(path)
    try:
        with tmp.open("wb") as f:
            writer.write(f)
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = _temp_sibling(path)
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = _temp_sibling(path)
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _external_timeout_sec(default: float = _DEFAULT_EXTERNAL_TIMEOUT_SEC) -> float:
    try:
        return max(1.0, float(os.environ.get("MAGI_PDF_TOOL_EXTERNAL_TIMEOUT_SEC", str(default)) or default))
    except Exception:
        return default


def _safe_filename_piece(name: str) -> str:
    base = Path(str(name or "image.bin")).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return base or "image.bin"


def _output_path(name: str, user_output: Optional[str] = None) -> str:
    """決定輸出路徑，且限制在 managed exports/case roots 內。"""
    return str(_output_file(name, user_output))


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None) -> None:
    """Best-effort event log."""
    try:
        from api.runtime_paths import ensure_orch_on_sys_path
        ensure_orch_on_sys_path()
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(event, ok=ok, payload=payload or {}, tags={}, source="pdf_tool")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 80, exc_info=True)


# ═══════════════════════════════════════════════════════════════
# Task implementations
# ═══════════════════════════════════════════════════════════════

def task_merge(args: List[str]) -> Dict[str, Any]:
    """合併多個 PDF 為一份。"""
    from pypdf import PdfReader, PdfWriter

    parser = argparse.ArgumentParser(prog="pdf merge")
    parser.add_argument("--files", nargs="+", required=True, help="要合併的 PDF 檔案路徑")
    parser.add_argument("--output", "-o", default=None, help="輸出檔案路徑")
    opts = parser.parse_args(args)

    writer = PdfWriter()
    total_pages = 0
    input_files = [_input_file(pdf_file, suffixes=(".pdf",)) for pdf_file in opts.files]
    for pdf_file in input_files:
        reader = PdfReader(str(pdf_file))
        for page in reader.pages:
            writer.add_page(page)
            total_pages += 1

    out_path = _output_file(f"merged_{_ts()}.pdf", opts.output)
    _atomic_write_pdf(out_path, writer)
    out = str(out_path)

    _eventlog(f"PDF 合併完成：{len(opts.files)} 份 → {total_pages} 頁", ok=True, payload={"output": out})
    return {"ok": True, "output": out, "files_merged": len(opts.files), "total_pages": total_pages}


def task_split(args: List[str]) -> Dict[str, Any]:
    """分割 PDF：依頁碼範圍切割成多份。"""
    from pypdf import PdfReader, PdfWriter

    parser = argparse.ArgumentParser(prog="pdf split")
    parser.add_argument("--file", required=True, help="輸入 PDF")
    parser.add_argument("--pages", default=None, help="頁碼範圍，逗號分隔（如 1-5,6-10）。不指定則每頁一份。")
    parser.add_argument("--output-dir", default=None, help="輸出目錄")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))
    reader = PdfReader(str(input_file))
    total = len(reader.pages)
    base = input_file.stem
    out_dir = _output_dir(f"split_{base}_{_ts()}", opts.output_dir)

    outputs = []

    if opts.pages:
        # 解析頁碼範圍：1-5,6-10,15
        ranges = []
        for part in opts.pages.split(","):
            part = part.strip()
            m = re.match(r"(\d+)\s*-\s*(\d+)", part)
            if m:
                ranges.append((int(m.group(1)), int(m.group(2))))
            elif part.isdigit():
                ranges.append((int(part), int(part)))
            else:
                return {"ok": False, "error": f"無法解析頁碼：{part}"}

        for start, end in ranges:
            writer = PdfWriter()
            for i in range(max(1, start), min(end, total) + 1):
                writer.add_page(reader.pages[i - 1])
            fname_path = out_dir / f"{base}_p{start}-{end}.pdf"
            _atomic_write_pdf(fname_path, writer)
            fname = str(fname_path)
            outputs.append(fname)
    else:
        for i, page in enumerate(reader.pages, 1):
            writer = PdfWriter()
            writer.add_page(page)
            fname_path = out_dir / f"{base}_p{i}.pdf"
            _atomic_write_pdf(fname_path, writer)
            fname = str(fname_path)
            outputs.append(fname)

    _eventlog(f"PDF 分割完成：{total} 頁 → {len(outputs)} 份", ok=True)
    return {"ok": True, "output_dir": str(out_dir), "files": outputs, "count": len(outputs)}


def task_extract(args: List[str]) -> Dict[str, Any]:
    """擷取 PDF 文字（優先用 pdfplumber，fallback pypdf）。"""
    parser = argparse.ArgumentParser(prog="pdf extract")
    parser.add_argument("--file", required=True, help="輸入 PDF")
    parser.add_argument("--pages", default=None, help="頁碼範圍（如 1-5）")
    parser.add_argument("--output", "-o", default=None, help="輸出文字檔路徑")
    parser.add_argument("--tables", action="store_true", help="一併擷取表格")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))

    text_parts = []
    tables_found = []
    start_page, end_page = 1, 99999

    if opts.pages:
        m = re.match(r"(\d+)\s*-\s*(\d+)", opts.pages)
        if m:
            start_page, end_page = int(m.group(1)), int(m.group(2))

    try:
        import pdfplumber
        with pdfplumber.open(str(input_file)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                if i < start_page or i > end_page:
                    continue
                txt = page.extract_text() or ""
                text_parts.append(f"--- 第 {i} 頁 ---\n{txt}")
                if opts.tables:
                    for table in (page.extract_tables() or []):
                        tables_found.append({"page": i, "data": table})
    except ImportError:
        from pypdf import PdfReader
        reader = PdfReader(str(input_file))
        for i, page in enumerate(reader.pages, 1):
            if i < start_page or i > end_page:
                continue
            txt = page.extract_text() or ""
            text_parts.append(f"--- 第 {i} 頁 ---\n{txt}")

    full_text = "\n\n".join(text_parts)

    if opts.output:
        out_path = _output_file(f"{input_file.stem}_text.txt", opts.output)
        _atomic_write_text(out_path, full_text)

    result: Dict[str, Any] = {
        "ok": True,
        "pages_extracted": len(text_parts),
        "chars": len(full_text),
        "text_preview": full_text[:2000],
    }
    if opts.output:
        result["output"] = str(out_path)
    if tables_found:
        result["tables"] = tables_found
    return result


def task_ocr(args: List[str]) -> Dict[str, Any]:
    """OCR 掃描 PDF → 可搜尋文字。"""
    parser = argparse.ArgumentParser(prog="pdf ocr")
    parser.add_argument("--file", required=True, help="掃描的 PDF")
    parser.add_argument("--output", "-o", default=None, help="輸出文字檔路徑")
    parser.add_argument("--lang", default="chi_tra+eng", help="OCR 語言（預設中文繁體+英文）")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))

    try:
        old_lang = os.environ.get("MAGI_PDF_OCR_LANGS")
        try:
            os.environ["MAGI_PDF_OCR_LANGS"] = opts.lang
            from skills.documents.pdf_bridge import extract_text as _extract_pdf_text

            full_text = _extract_pdf_text(str(input_file))
        finally:
            if old_lang is None:
                os.environ.pop("MAGI_PDF_OCR_LANGS", None)
            else:
                os.environ["MAGI_PDF_OCR_LANGS"] = old_lang

        if not full_text or full_text.startswith("[PDF 提取失敗"):
            return {"ok": False, "error": full_text or "OCR 無文字輸出"}
        if opts.output:
            out_path = _output_file(f"{input_file.stem}_ocr.txt", opts.output)
            _atomic_write_text(out_path, full_text)

        return {
            "ok": True,
            "engine": "MAGI PDF OCR pipeline",
            "pages": len(re.findall(r"--- 第\\s*\\d+\\s*頁", full_text)) or None,
            "chars": len(full_text),
            "text_preview": full_text[:2000],
            **({"output": str(out_path)} if opts.output else {}),
        }
    except Exception as e:
        return {"ok": False, "error": f"MAGI OCR pipeline failed: {e}"}


def task_encrypt(args: List[str]) -> Dict[str, Any]:
    """加密 PDF。"""
    from pypdf import PdfReader, PdfWriter

    parser = argparse.ArgumentParser(prog="pdf encrypt")
    parser.add_argument("--file", required=True, help="輸入 PDF")
    parser.add_argument("--password", required=True, help="密碼")
    parser.add_argument("--output", "-o", default=None, help="輸出路徑")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))

    reader = PdfReader(str(input_file))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(opts.password)

    base = input_file.stem
    out_path = _output_file(f"{base}_encrypted.pdf", opts.output)
    _atomic_write_pdf(out_path, writer)
    out = str(out_path)

    _eventlog(f"PDF 加密完成：{base}", ok=True)
    return {"ok": True, "output": out}


def task_decrypt(args: List[str]) -> Dict[str, Any]:
    """解密 PDF。"""
    from pypdf import PdfReader, PdfWriter

    parser = argparse.ArgumentParser(prog="pdf decrypt")
    parser.add_argument("--file", required=True, help="加密的 PDF")
    parser.add_argument("--password", required=True, help="密碼")
    parser.add_argument("--output", "-o", default=None, help="輸出路徑")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))

    reader = PdfReader(str(input_file))
    if reader.is_encrypted:
        if not reader.decrypt(opts.password):
            return {"ok": False, "error": "密碼錯誤"}

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    base = input_file.stem
    out_path = _output_file(f"{base}_decrypted.pdf", opts.output)
    _atomic_write_pdf(out_path, writer)
    out = str(out_path)

    _eventlog(f"PDF 解密完成：{base}", ok=True)
    return {"ok": True, "output": out}


def task_rotate(args: List[str]) -> Dict[str, Any]:
    """旋轉 PDF 頁面。"""
    from pypdf import PdfReader, PdfWriter

    parser = argparse.ArgumentParser(prog="pdf rotate")
    parser.add_argument("--file", required=True, help="輸入 PDF")
    parser.add_argument("--degrees", type=int, default=90, help="旋轉角度（90, 180, 270）")
    parser.add_argument("--pages", default="all", help="要旋轉的頁碼（如 1,3,5 或 all）")
    parser.add_argument("--output", "-o", default=None, help="輸出路徑")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))

    reader = PdfReader(str(input_file))
    writer = PdfWriter()

    if opts.pages == "all":
        target_pages = set(range(len(reader.pages)))
    else:
        target_pages = {int(p.strip()) - 1 for p in opts.pages.split(",")}

    for i, page in enumerate(reader.pages):
        if i in target_pages:
            page.rotate(opts.degrees)
        writer.add_page(page)

    base = input_file.stem
    out_path = _output_file(f"{base}_rotated.pdf", opts.output)
    _atomic_write_pdf(out_path, writer)
    out = str(out_path)

    return {"ok": True, "output": out, "rotated_pages": len(target_pages), "degrees": opts.degrees}


def task_watermark(args: List[str]) -> Dict[str, Any]:
    """加上浮水印。"""
    from pypdf import PdfReader, PdfWriter

    parser = argparse.ArgumentParser(prog="pdf watermark")
    parser.add_argument("--file", required=True, help="輸入 PDF")
    parser.add_argument("--watermark", required=True, help="浮水印 PDF（單頁）")
    parser.add_argument("--output", "-o", default=None, help="輸出路徑")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))
    watermark_file = _input_file(opts.watermark, suffixes=(".pdf",))

    wm_page = PdfReader(str(watermark_file)).pages[0]
    reader = PdfReader(str(input_file))
    writer = PdfWriter()

    for page in reader.pages:
        page.merge_page(wm_page)
        writer.add_page(page)

    base = input_file.stem
    out_path = _output_file(f"{base}_watermarked.pdf", opts.output)
    _atomic_write_pdf(out_path, writer)
    out = str(out_path)

    return {"ok": True, "output": out, "pages": len(reader.pages)}


def task_info(args: List[str]) -> Dict[str, Any]:
    """顯示 PDF 基本資訊。"""
    from pypdf import PdfReader

    parser = argparse.ArgumentParser(prog="pdf info")
    parser.add_argument("--file", required=True, help="輸入 PDF")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))

    reader = PdfReader(str(input_file))
    meta = reader.metadata or {}
    size_mb = input_file.stat().st_size / (1024 * 1024)

    return {
        "ok": True,
        "file": str(input_file),
        "pages": len(reader.pages),
        "size_mb": round(size_mb, 2),
        "encrypted": reader.is_encrypted,
        "title": getattr(meta, "title", None),
        "author": getattr(meta, "author", None),
        "subject": getattr(meta, "subject", None),
        "creator": getattr(meta, "creator", None),
    }


def task_images(args: List[str]) -> Dict[str, Any]:
    """從 PDF 中擷取所有圖片。"""
    parser = argparse.ArgumentParser(prog="pdf images")
    parser.add_argument("--file", required=True, help="輸入 PDF")
    parser.add_argument("--output-dir", default=None, help="輸出目錄")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))

    base = input_file.stem
    out_dir = _output_dir(f"images_{base}_{_ts()}", opts.output_dir)

    # 嘗試 pdfimages（poppler-utils）
    pdfimages = shutil.which("pdfimages")
    if pdfimages:
        stage = Path(tempfile.mkdtemp(prefix=f".{out_dir.name}.", suffix=".tmp", dir=str(out_dir.parent)))
        prefix = str(stage / "img")
        try:
            subprocess.run(
                [pdfimages, "-j", str(input_file), prefix],
                check=True,
                capture_output=True,
                text=True,
                timeout=_external_timeout_sec(),
            )
            for item in sorted(stage.iterdir()):
                if item.is_file():
                    item.replace(out_dir / _safe_filename_piece(item.name))
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        files = sorted(str(f) for f in out_dir.iterdir() if f.is_file())
        return {"ok": True, "output_dir": str(out_dir), "images": files, "count": len(files)}

    # Fallback: pypdf
    from pypdf import PdfReader
    reader = PdfReader(str(input_file))
    extracted = []
    idx = 0
    for page in reader.pages:
        for image_obj in page.images:
            idx += 1
            fname_path = out_dir / f"img_{idx:03d}_{_safe_filename_piece(image_obj.name)}"
            _atomic_write_bytes(fname_path, image_obj.data)
            fname = str(fname_path)
            extracted.append(fname)

    return {"ok": True, "output_dir": str(out_dir), "images": extracted, "count": len(extracted)}


def task_form(args: List[str]) -> Dict[str, Any]:
    """填寫 PDF 表單（wrapper for scripts/fill_fillable_fields.py）。"""
    parser = argparse.ArgumentParser(prog="pdf form")
    parser.add_argument("--file", required=True, help="含表單的 PDF")
    parser.add_argument("--fields-json", required=True, help="JSON 檔（field_id → value）")
    parser.add_argument("--output", "-o", default=None, help="輸出路徑")
    opts = parser.parse_args(args)

    input_file = _input_file(opts.file, suffixes=(".pdf",))
    fields_json = _input_file(opts.fields_json, suffixes=(".json",))

    base = input_file.stem
    out_path = _output_file(f"{base}_filled.pdf", opts.output)

    scripts_dir = Path(__file__).parent / "scripts"
    fill_script = scripts_dir / "fill_fillable_fields.py"
    if fill_script.exists():
        stage_out = _temp_sibling(out_path)
        try:
            result = subprocess.run(
                [sys.executable, str(fill_script), str(input_file), str(fields_json), str(stage_out)],
                capture_output=True, text=True, timeout=_external_timeout_sec(60.0),
            )
            if result.returncode == 0:
                stage_out.replace(out_path)
                out = str(out_path)
                return {"ok": True, "output": out}
            return {"ok": False, "error": result.stderr[:500]}
        finally:
            try:
                stage_out.unlink(missing_ok=True)
            except Exception:
                pass

    return {"ok": False, "error": "表單填寫腳本不存在"}


def task_help(_args: List[str] = None) -> Dict[str, Any]:
    """顯示所有可用指令。"""
    cmds = {
        "merge": "合併多個 PDF（--files a.pdf b.pdf --output merged.pdf）",
        "split": "分割 PDF（--file input.pdf --pages 1-5,6-10）",
        "extract": "擷取文字（--file input.pdf [--tables]）",
        "ocr": "OCR 掃描文件（--file scanned.pdf [--lang chi_tra+eng]）",
        "encrypt": "加密（--file input.pdf --password 密碼）",
        "decrypt": "解密（--file input.pdf --password 密碼）",
        "rotate": "旋轉頁面（--file input.pdf --degrees 90 [--pages 1,3]）",
        "watermark": "加浮水印（--file input.pdf --watermark wm.pdf）",
        "info": "顯示 PDF 資訊（--file input.pdf）",
        "images": "擷取圖片（--file input.pdf）",
        "form": "填寫表單（--file form.pdf --fields-json data.json）",
        "help": "顯示本說明",
    }
    lines = ["PDF 瑞士刀 — 可用指令：", ""]
    for cmd, desc in cmds.items():
        lines.append(f"  {cmd:12s} {desc}")
    return {"ok": True, "commands": cmds, "help_text": "\n".join(lines)}


# ═══════════════════════════════════════════════════════════════
# Dispatcher
# ═══════════════════════════════════════════════════════════════

_TASKS = {
    "merge": task_merge,
    "split": task_split,
    "extract": task_extract,
    "ocr": task_ocr,
    "encrypt": task_encrypt,
    "decrypt": task_decrypt,
    "rotate": task_rotate,
    "watermark": task_watermark,
    "info": task_info,
    "images": task_images,
    "form": task_form,
    "help": task_help,
}


def dispatch(task_str: str) -> Dict[str, Any]:
    """解析 task 字串並分派到對應的處理函式。"""
    if not task_str or not task_str.strip():
        return task_help()

    parts = shlex.split(task_str.strip())
    cmd = parts[0].lower()
    args = parts[1:]

    handler = _TASKS.get(cmd)
    if not handler:
        return {"ok": False, "error": f"未知指令：{cmd}", "available": list(_TASKS.keys())}

    try:
        return handler(args)
    except SystemExit:
        # argparse 會在 --help 或參數錯誤時 raise SystemExit
        return {"ok": False, "error": f"參數錯誤，請用「pdf help」查看用法"}
    except Exception as e:
        logger.exception(f"執行 pdf {cmd} 時發生錯誤")
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="PDF 瑞士刀 — MAGI PDF 處理工具")
    parser.add_argument("--task", "-t", required=True, help="任務指令（如 merge --files a.pdf b.pdf）")
    parser.add_argument("--text", default=None, help="額外文字參數（部分任務使用）")
    args = parser.parse_args()

    task_str = args.task
    if args.text:
        task_str += f" {args.text}"

    result = dispatch(task_str)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
