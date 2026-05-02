"""
OSC unified preview helper — Phase 1 (NAS file manager).

Converts various source files into browser-renderable artifacts:
  - Office (docx/xlsx/pptx/doc/xls/ppt) → PDF via LibreOffice headless
  - HEIC/HEIF → JPEG via macOS sips
  - CSV/TSV → list of rows (JSON)
  - EML → header+body+attachments (JSON)
  - ZIP → file listing (JSON)
  - Other → hex dump of first N bytes

All converted artifacts are cached at ~/.cache/paperclip-preview/<sha1>.<ext>
keyed on (path + mtime) so cache invalidates automatically when source updates.

Subprocess timeouts hard-set to 60s (LibreOffice). Cache LRU 5GB enforced
on cleanup (lazy: triggered each conversion).
"""
from __future__ import annotations

import csv
import email
import hashlib
import io
import logging
import os
import shutil
import subprocess
import time
import zipfile
from email import policy
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

CACHE_DIR = Path(os.path.expanduser("~/.cache/paperclip-preview"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_MAX_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB
LIBREOFFICE_TIMEOUT_SEC = 60
SIPS_TIMEOUT_SEC = 30

# soffice CLI fallback paths (macOS LibreOffice not on PATH by default)
_SOFFICE_CANDIDATES = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    shutil.which("soffice") or "",
    shutil.which("libreoffice") or "",
)

OFFICE_EXTS = {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp"}
HEIC_EXTS = {".heic", ".heif"}
CSV_EXTS = {".csv", ".tsv"}
EMAIL_EXTS = {".eml"}
ZIP_EXTS = {".zip"}


def _soffice_path() -> str:
    for c in _SOFFICE_CANDIDATES:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def _cache_key(source_path: str) -> str:
    try:
        mtime = int(os.path.getmtime(source_path))
    except OSError:
        mtime = 0
    h = hashlib.sha1()
    h.update(f"{source_path}:{mtime}".encode("utf-8"))
    return h.hexdigest()


def _cache_lru_cleanup():
    """Evict oldest cache entries when total exceeds CACHE_MAX_BYTES."""
    try:
        entries = []
        total = 0
        for p in CACHE_DIR.iterdir():
            if not p.is_file():
                continue
            st = p.stat()
            entries.append((p, st.st_atime, st.st_size))
            total += st.st_size
        if total <= CACHE_MAX_BYTES:
            return
        entries.sort(key=lambda x: x[1])  # oldest atime first
        for p, _, sz in entries:
            try:
                p.unlink()
                total -= sz
            except OSError:
                continue
            if total <= CACHE_MAX_BYTES * 0.85:
                break
    except OSError:
        pass


# ── Office → PDF ────────────────────────────────────────────────────────


def preview_office_to_pdf(path: str) -> Optional[str]:
    soffice = _soffice_path()
    if not soffice:
        _log.warning("preview_office_to_pdf: soffice binary not found")
        return None
    key = _cache_key(path)
    cached = CACHE_DIR / f"{key}.pdf"
    if cached.exists():
        os.utime(cached, None)  # bump atime for LRU
        return str(cached)
    # LibreOffice's --convert-to writes to outdir/<basename>.pdf
    try:
        env = os.environ.copy()
        env["HOME"] = os.path.expanduser("~")
        proc = subprocess.run(
            [soffice, "--headless", "--norestore", "--nologo", "--nofirststartwizard",
             "--convert-to", "pdf", "--outdir", str(CACHE_DIR), path],
            timeout=LIBREOFFICE_TIMEOUT_SEC, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            _log.error("soffice convert failed rc=%s err=%s", proc.returncode, proc.stderr[:200])
            return None
    except subprocess.TimeoutExpired:
        _log.error("soffice convert timeout for %s", path)
        return None
    except OSError as e:
        _log.error("soffice convert OSError: %s", e)
        return None

    # soffice writes <basename_no_ext>.pdf
    src_stem = Path(path).stem
    out_default = CACHE_DIR / f"{src_stem}.pdf"
    if not out_default.exists():
        return None
    try:
        out_default.rename(cached)
    except OSError:
        return str(out_default) if out_default.exists() else None
    _cache_lru_cleanup()
    return str(cached)


# ── HEIC → JPEG ─────────────────────────────────────────────────────────


def preview_heic_to_jpg(path: str) -> Optional[str]:
    sips = shutil.which("sips") or "/usr/bin/sips"
    if not os.path.isfile(sips):
        return None
    key = _cache_key(path)
    cached = CACHE_DIR / f"{key}.jpg"
    if cached.exists():
        os.utime(cached, None)
        return str(cached)
    try:
        proc = subprocess.run(
            [sips, "-s", "format", "jpeg", path, "--out", str(cached)],
            timeout=SIPS_TIMEOUT_SEC,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0 or not cached.exists():
            return None
    except (subprocess.TimeoutExpired, OSError):
        return None
    _cache_lru_cleanup()
    return str(cached)


# ── CSV → rows ──────────────────────────────────────────────────────────


def preview_csv_to_rows(path: str, max_rows: int = 500) -> dict:
    delim = "\t" if path.lower().endswith(".tsv") else ","
    rows = []
    headers = []
    truncated = False
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=delim)
            for i, row in enumerate(reader):
                if i == 0:
                    headers = row
                    continue
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(row)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "headers": headers, "rows": rows, "truncated": truncated, "row_count": len(rows)}


# ── Email → parsed ──────────────────────────────────────────────────────


def preview_email(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    body_text = ""
    body_html = ""
    attachments = []
    try:
        for part in msg.walk():
            ct = part.get_content_type()
            cd = (part.get("Content-Disposition") or "").lower()
            if "attachment" in cd or part.get_filename():
                attachments.append({
                    "filename": part.get_filename() or "(unnamed)",
                    "content_type": ct,
                    "size": len(part.get_payload(decode=True) or b""),
                })
                continue
            if ct == "text/plain" and not body_text:
                body_text = (part.get_content() or "")[:50000]
            elif ct == "text/html" and not body_html:
                body_html = (part.get_content() or "")[:50000]
    except Exception as e:
        _log.warning("preview_email walk error: %s", e)
    return {
        "ok": True,
        "from": str(msg.get("From") or ""),
        "to": str(msg.get("To") or ""),
        "cc": str(msg.get("Cc") or ""),
        "subject": str(msg.get("Subject") or ""),
        "date": str(msg.get("Date") or ""),
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
    }


# ── ZIP → listing ───────────────────────────────────────────────────────


def preview_zip(path: str, max_entries: int = 500) -> dict:
    items = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for i, info in enumerate(zf.infolist()):
                if i >= max_entries:
                    break
                items.append({
                    "name": info.filename,
                    "size": info.file_size,
                    "compressed_size": info.compress_size,
                    "is_dir": info.is_dir(),
                    "modified": "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*info.date_time)
                                if info.date_time else "",
                })
    except (zipfile.BadZipFile, OSError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "items": items, "truncated": len(items) >= max_entries}


# ── Hex fallback ────────────────────────────────────────────────────────


def preview_hex_dump(path: str, n_bytes: int = 256) -> dict:
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            data = f.read(n_bytes)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join((chr(b) if 32 <= b < 127 else ".") for b in chunk)
        lines.append(f"{i:08x}  {hex_part:<48}  {ascii_part}")
    return {"ok": True, "size": sz, "shown_bytes": len(data), "hex": "\n".join(lines)}


# ── Dispatcher ──────────────────────────────────────────────────────────


def categorize(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return "pdf"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in HEIC_EXTS:
        return "heic"
    if ext in CSV_EXTS:
        return "csv"
    if ext in EMAIL_EXTS:
        return "email"
    if ext in ZIP_EXTS:
        return "zip"
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg"}:
        return "image"
    if ext in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
        return "audio"
    if ext in {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}:
        return "video"
    if ext in {".txt", ".md", ".json", ".log", ".xml", ".html", ".htm", ".sql",
               ".py", ".js", ".ts", ".css", ".yml", ".yaml"}:
        return "text"
    return "other"
