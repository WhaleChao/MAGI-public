# -*- coding: utf-8 -*-
"""Optional Chandra OCR adapter for PDF page extraction.

Chandra is layout-aware and useful for hard scanned PDFs, but its model backend
normally requires a vLLM server/GPU or a large HuggingFace model. MAGI therefore
keeps it as an explicitly enabled provider and never imports Chandra into the
main process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_DEFAULT_CLI_CANDIDATES = (
    "/tmp/magi_chandra_venv/bin/chandra",
    "/opt/magi_chandra_venv/bin/chandra",
)


@dataclass
class ChandraProbe:
    available: bool
    reason: str = ""
    cli_path: str = ""
    method: str = "vllm"
    api_base: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "available": self.available,
            "reason": self.reason,
            "cli_path": self.cli_path,
            "method": self.method,
            "api_base": self.api_base,
        }


@dataclass
class ChandraOCRResult:
    success: bool
    text: str = ""
    error: str = ""
    duration_sec: float = 0.0
    command: Optional[List[str]] = None
    output_dir: str = ""


def _env_truthy(key: str, default: str = "0") -> bool:
    value = os.environ.get(key, default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _env_truthy("MAGI_CHANDRA_OCR_ENABLE")


def model_license_accepted() -> bool:
    return _env_truthy("MAGI_CHANDRA_ACCEPT_MODEL_LICENSE")


def method() -> str:
    raw = os.environ.get("MAGI_CHANDRA_OCR_METHOD") or os.environ.get("CHANDRA_METHOD") or "vllm"
    raw = raw.strip().lower()
    return raw if raw in {"vllm", "hf"} else "vllm"


def api_base() -> str:
    return (
        os.environ.get("MAGI_CHANDRA_VLLM_API_BASE")
        or os.environ.get("VLLM_API_BASE")
        or "http://127.0.0.1:8000/v1"
    ).strip()


def resolve_cli() -> str:
    configured = os.environ.get("MAGI_CHANDRA_CLI", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(_DEFAULT_CLI_CANDIDATES)
    candidates.append(shutil.which("chandra") or "")
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


def _vllm_server_reachable(base_url: str, timeout_sec: float = 2.0) -> Tuple[bool, str]:
    try:
        import requests
    except Exception:
        return False, "requests not importable"

    try:
        url = base_url.rstrip("/") + "/models"
        response = requests.get(url, timeout=timeout_sec)
        if 200 <= response.status_code < 300:
            return True, ""
        return False, "vLLM /models returned HTTP %s" % response.status_code
    except Exception as exc:
        return False, "vLLM unavailable: %s" % exc


def probe(check_server: bool = True) -> ChandraProbe:
    cli = resolve_cli()
    selected_method = method()
    base = api_base()
    if not enabled():
        return ChandraProbe(False, "MAGI_CHANDRA_OCR_ENABLE is not enabled", cli, selected_method, base)
    if not cli:
        return ChandraProbe(False, "chandra CLI not found", "", selected_method, base)
    if not model_license_accepted():
        return ChandraProbe(
            False,
            "MAGI_CHANDRA_ACCEPT_MODEL_LICENSE is required before model inference",
            cli,
            selected_method,
            base,
        )
    if selected_method == "hf" and not _env_truthy("MAGI_CHANDRA_ALLOW_HF"):
        return ChandraProbe(False, "HF backend blocked unless MAGI_CHANDRA_ALLOW_HF=1", cli, selected_method, base)
    if selected_method == "vllm" and check_server:
        ok, reason = _vllm_server_reachable(base)
        if not ok:
            return ChandraProbe(False, reason, cli, selected_method, base)
    return ChandraProbe(True, "", cli, selected_method, base)


def _read_markdown(output_dir: Path) -> str:
    markdown_files = sorted(output_dir.rglob("*.md"), key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
    for md_path in markdown_files:
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
        except Exception:
            continue
    return ""


def run_pdf_page(pdf_path: str, page_num: int = 0, timeout_sec: Optional[float] = None) -> ChandraOCRResult:
    start = time.monotonic()
    availability = probe(check_server=True)
    if not availability.available:
        return ChandraOCRResult(False, error=availability.reason, duration_sec=round(time.monotonic() - start, 3))

    if not pdf_path or not os.path.isfile(pdf_path):
        return ChandraOCRResult(False, error="pdf file not found: %r" % pdf_path, duration_sec=round(time.monotonic() - start, 3))

    if timeout_sec is None:
        try:
            timeout_sec = float(os.environ.get("MAGI_CHANDRA_OCR_TIMEOUT_SEC", "180") or "180")
        except Exception:
            timeout_sec = 180.0

    one_based_page = max(1, int(page_num) + 1)
    with tempfile.TemporaryDirectory(prefix="magi_chandra_ocr_") as tmp:
        output_dir = Path(tmp)
        command = [
            availability.cli_path,
            pdf_path,
            str(output_dir),
            "--method",
            availability.method,
            "--page-range",
            str(one_based_page),
            "--batch-size",
            "1",
            "--no-images",
            "--no-html",
        ]
        env = os.environ.copy()
        env["VLLM_API_BASE"] = availability.api_base
        try:
            completed = subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return ChandraOCRResult(
                False,
                error="chandra timeout after %.1fs" % timeout_sec,
                duration_sec=round(time.monotonic() - start, 3),
                command=command,
                output_dir=str(output_dir),
            )
        except Exception as exc:
            return ChandraOCRResult(
                False,
                error="chandra invocation failed: %s: %s" % (type(exc).__name__, exc),
                duration_sec=round(time.monotonic() - start, 3),
                command=command,
                output_dir=str(output_dir),
            )

        text = _read_markdown(output_dir)
        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout or "").strip()
            return ChandraOCRResult(
                False,
                text=text,
                error=stderr[-1000:] or "chandra exited with %s" % completed.returncode,
                duration_sec=round(time.monotonic() - start, 3),
                command=command,
                output_dir=str(output_dir),
            )
        if not text:
            return ChandraOCRResult(
                False,
                error="chandra completed but no markdown output was produced",
                duration_sec=round(time.monotonic() - start, 3),
                command=command,
                output_dir=str(output_dir),
            )
        return ChandraOCRResult(
            True,
            text=text,
            duration_sec=round(time.monotonic() - start, 3),
            command=command,
            output_dir=str(output_dir),
        )
