#!/usr/bin/env python3
"""Backfill PDF versions for Supreme Court interpreter judgment exports.

The Judicial Yuan public HTML endpoint can reset connections during the day.
This script therefore creates PDFs from the clean TXT/JDoc text that MAGI has
already verified.  During the official API window it can also fetch missing JDoc
texts and immediately render PDFs for them without disturbing the clean TXT
folder numbering.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import sys
import time
import textwrap
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import fitz  # PyMuPDF


MAGI_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEXT_DIR = Path("/Users/ai/Desktop/最高法院_通譯_TXT")
DEFAULT_PDF_DIR = DEFAULT_TEXT_DIR / "PDF"
DEFAULT_API_BACKFILL_DIR = DEFAULT_TEXT_DIR / "_api_pdf_backfill"
DEFAULT_LIST_PATH = DEFAULT_TEXT_DIR / "最高法院_通譯_812清單.json"

PAGE_WIDTH = 595  # A4 points
PAGE_HEIGHT = 842
MARGIN_X = 48
MARGIN_Y = 52
FONT_NAME = "china-t"
FONT_SIZE = 11
LINE_HEIGHT = 17
MAX_LINE_UNITS = 48


def load_dotenv() -> None:
    env_path = MAGI_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def safe_title(title: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", (title or "").strip())
    value = re.sub(r"\s+", "_", value)
    return value[:160].strip("_") or "judgment"


def text_width_units(text: str) -> float:
    units = 0.0
    for ch in text:
        if ch == "\t":
            units += 2
        elif ord(ch) < 128:
            units += 0.55
        else:
            units += 1.0
    return units


def wrap_text_line(line: str, max_units: int = MAX_LINE_UNITS) -> list[str]:
    line = line.rstrip()
    if not line:
        return [""]
    out: list[str] = []
    buf = ""
    for ch in line:
        candidate = buf + ch
        if buf and text_width_units(candidate) > max_units:
            out.append(buf)
            buf = ch
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def wrap_text(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        raw = raw.rstrip()
        if not raw:
            lines.append("")
            continue
        # Extremely long ASCII fragments are rare but can appear in URLs.
        for part in textwrap.wrap(raw, width=110, break_long_words=False, replace_whitespace=False) or [raw]:
            lines.extend(wrap_text_line(part))
    return lines


def render_text_pdf(text: str, pdf_path: Path, title: str = "") -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    lines = wrap_text(text)
    page = None
    y = MARGIN_Y

    def new_page():
        nonlocal page, y
        page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        y = MARGIN_Y
        if title:
            page.insert_text(
                (MARGIN_X, 28),
                title[:90],
                fontname=FONT_NAME,
                fontsize=9,
                color=(0.32, 0.32, 0.32),
            )
            page.draw_line((MARGIN_X, 38), (PAGE_WIDTH - MARGIN_X, 38), color=(0.82, 0.82, 0.82), width=0.5)

    new_page()
    assert page is not None
    for line in lines:
        if y > PAGE_HEIGHT - MARGIN_Y:
            new_page()
            assert page is not None
        if line:
            page.insert_text((MARGIN_X, y), line, fontname=FONT_NAME, fontsize=FONT_SIZE)
        y += LINE_HEIGHT
    doc.set_metadata({"title": title or pdf_path.stem, "creator": "MAGI Supreme Interpreter PDF Backfill"})
    tmp = pdf_path.with_suffix(".tmp.pdf")
    doc.save(tmp, garbage=4, deflate=True)
    doc.close()
    tmp.replace(pdf_path)


def txt_to_pdf_name(txt_path: Path) -> str:
    return txt_path.with_suffix(".pdf").name


def build_authoritative_index_by_current_txt(text_dir: Path) -> dict[int, Path]:
    mapping_path = text_dir / "重新編號對照表.csv"
    result: dict[int, Path] = {}
    if not mapping_path.exists():
        return result
    with mapping_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            current = str(row.get("new_index") or "").zfill(4)
            auth_raw = str(row.get("old_authoritative_index") or "").strip()
            if not current or not auth_raw:
                continue
            try:
                auth_idx = int(auth_raw)
            except ValueError:
                continue
            matches = sorted(text_dir.glob(f"{current}_*.txt"))
            if matches:
                result[auth_idx] = matches[0]
    return result


def generate_existing_pdfs(text_dir: Path, pdf_dir: Path, force: bool = False) -> dict:
    written = 0
    skipped = 0
    failed: list[dict] = []
    for txt_path in sorted(text_dir.glob("*.txt")):
        pdf_path = pdf_dir / txt_to_pdf_name(txt_path)
        if pdf_path.exists() and pdf_path.stat().st_size > 500 and not force:
            skipped += 1
            continue
        try:
            text = txt_path.read_text(encoding="utf-8", errors="replace")
            render_text_pdf(text, pdf_path, title=txt_path.stem)
            written += 1
        except Exception as exc:
            failed.append({"txt": str(txt_path), "error": f"{type(exc).__name__}: {str(exc)[:220]}"})
    return {"written": written, "skipped": skipped, "failed": failed}


def load_judicial_web_search_module():
    action_path = MAGI_ROOT / "skills" / "judicial-web-search" / "action.py"
    spec = importlib.util.spec_from_file_location("magi_judicial_web_search_pdf_backfill", action_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot import {action_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def jid_from_url(url: str) -> str:
    match = re.search(r"[?&]id=([^&]+)", url or "")
    return unquote(match.group(1)) if match else ""


def fetch_missing_api_pdfs(
    text_dir: Path,
    pdf_dir: Path,
    list_path: Path,
    api_backfill_dir: Path,
    max_api: int,
    force: bool = False,
    delay_sec: float = 0.25,
) -> dict:
    if not list_path.exists():
        return {"attempted": 0, "written": 0, "skipped": 0, "failed": [{"error": f"missing list: {list_path}"}]}
    data = json.loads(list_path.read_text(encoding="utf-8"))
    items = list(data.get("results") or [])
    existing_by_auth = build_authoritative_index_by_current_txt(text_dir)
    api_backfill_dir.mkdir(parents=True, exist_ok=True)
    api_txt_dir = api_backfill_dir / "TXT"
    api_pdf_dir = api_backfill_dir / "PDF"
    api_txt_dir.mkdir(parents=True, exist_ok=True)
    api_pdf_dir.mkdir(parents=True, exist_ok=True)

    jws = load_judicial_web_search_module()
    attempted = 0
    written = 0
    skipped = 0
    failed: list[dict] = []

    for auth_idx, item in enumerate(items, start=1):
        title = str(item.get("title") or "")
        url = str(item.get("url") or "")
        if auth_idx in existing_by_auth:
            # Existing clean TXT PDFs are generated by generate_existing_pdfs.
            continue
        stem = f"auth{auth_idx:04d}_{safe_title(title)}"
        txt_path = api_txt_dir / f"{stem}.txt"
        pdf_path = api_pdf_dir / f"{stem}.pdf"
        if pdf_path.exists() and pdf_path.stat().st_size > 500 and txt_path.exists() and not force:
            skipped += 1
            continue
        if max_api >= 0 and attempted >= max_api:
            break
        attempted += 1
        try:
            if txt_path.exists() and txt_path.stat().st_size > 80 and not force:
                text = txt_path.read_text(encoding="utf-8", errors="replace")
            else:
                result = jws._fetch_text_from_jdg_api(url, timeout_sec=25, max_chars=500000)
                if not result.get("success"):
                    failed.append(
                        {
                            "auth_index": auth_idx,
                            "title": title,
                            "jid": jid_from_url(url),
                            "error": result.get("error"),
                            "recoverable": bool(result.get("recoverable")),
                        }
                    )
                    continue
                source = Path(str(result.get("text_path") or ""))
                text = source.read_text(encoding="utf-8", errors="replace") if source.exists() else str(result.get("text_preview") or "")
                txt_path.write_text(text, encoding="utf-8")
            render_text_pdf(text, pdf_path, title=title or stem)
            written += 1
            time.sleep(delay_sec)
        except Exception as exc:
            failed.append({"auth_index": auth_idx, "title": title, "jid": jid_from_url(url), "error": f"{type(exc).__name__}: {str(exc)[:220]}"})
    return {
        "attempted": attempted,
        "written": written,
        "skipped": skipped,
        "failed": failed,
        "api_backfill_dir": str(api_backfill_dir),
    }


def write_report(text_dir: Path, report: dict) -> Path:
    path = text_dir / "PDF補抓報告.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="最高法院通譯裁判 PDF 補抓/生成")
    parser.add_argument("--text-dir", default=str(DEFAULT_TEXT_DIR))
    parser.add_argument("--pdf-dir", default=str(DEFAULT_PDF_DIR))
    parser.add_argument("--list-path", default=str(DEFAULT_LIST_PATH))
    parser.add_argument("--api-backfill-dir", default=str(DEFAULT_API_BACKFILL_DIR))
    parser.add_argument("--use-api", action="store_true", help="在司法院 API 服務時段補抓尚缺 JDoc 並產 PDF")
    parser.add_argument("--max-api", type=int, default=500, help="最多嘗試補抓幾筆 API 缺漏；-1 表示不限")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    text_dir = Path(args.text_dir)
    pdf_dir = Path(args.pdf_dir)
    list_path = Path(args.list_path)
    api_backfill_dir = Path(args.api_backfill_dir)

    existing = generate_existing_pdfs(text_dir, pdf_dir, force=args.force)
    api_result = None
    if args.use_api:
        api_result = fetch_missing_api_pdfs(
            text_dir=text_dir,
            pdf_dir=pdf_dir,
            list_path=list_path,
            api_backfill_dir=api_backfill_dir,
            max_api=args.max_api,
            force=args.force,
        )
    report = {
        "success": not existing["failed"] and not (api_result and api_result.get("failed") and not all(f.get("recoverable") for f in api_result.get("failed", []))),
        "text_dir": str(text_dir),
        "pdf_dir": str(pdf_dir),
        "list_path": str(list_path),
        "existing_txt_pdf": existing,
        "api_missing_pdf": api_result,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    report_path = write_report(text_dir, report)
    print(json.dumps({k: v for k, v in report.items() if k not in {"existing_txt_pdf", "api_missing_pdf"}}, ensure_ascii=False))
    print(f"existing_written={existing['written']} existing_skipped={existing['skipped']} existing_failed={len(existing['failed'])}")
    if api_result:
        print(f"api_attempted={api_result['attempted']} api_written={api_result['written']} api_skipped={api_result['skipped']} api_failed={len(api_result['failed'])}")
    print(f"report={report_path}")
    return 0 if not existing["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
