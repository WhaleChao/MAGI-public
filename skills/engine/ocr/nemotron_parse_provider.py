# -*- coding: utf-8 -*-
"""Opt-in client for the Nemotron Parse MLX sidecar.

The provider is intentionally not wired into the default OCR consensus path.
Set ``MAGI_NEMOTRON_PARSE_ENABLE=1`` before calling it in production code.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from skills.engine.ocr.ocr_schema import OCRProviderResult
from skills.engine.ocr.quality import compute_quality_score


def _enabled() -> bool:
    return str(os.environ.get("MAGI_NEMOTRON_PARSE_ENABLE", "0")).strip().lower() in {"1", "true", "yes", "on"}


def run(
    image_path: str,
    *,
    task_type: str = "legal",
    timeout_sec: float = 120.0,
    sidecar_url: str | None = None,
) -> OCRProviderResult:
    """Run Nemotron Parse through the local MLX sidecar when explicitly enabled."""
    started = time.monotonic()
    if not _enabled():
        return OCRProviderResult.failure("nemotron_parse_mlx", "MAGI_NEMOTRON_PARSE_ENABLE is not enabled")
    if not image_path or not os.path.isfile(image_path):
        return OCRProviderResult.failure("nemotron_parse_mlx", f"image file not found: {image_path!r}")

    url = (sidecar_url or os.environ.get("MAGI_NEMOTRON_PARSE_URL") or "http://127.0.0.1:8094").rstrip("/")
    payload = json.dumps({"image_path": image_path, "task_type": task_type}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/parse",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_sec)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        return OCRProviderResult.failure("nemotron_parse_mlx", f"sidecar request failed: {exc}")
    except Exception as exc:
        return OCRProviderResult.failure("nemotron_parse_mlx", f"sidecar error: {exc}")

    if not bool(data.get("ok")):
        return OCRProviderResult.failure("nemotron_parse_mlx", str(data.get("error") or "sidecar returned ok=false"))

    text = str(data.get("text") or "")
    score = compute_quality_score(text)
    return OCRProviderResult(
        success=bool(text),
        provider="nemotron_parse_mlx",
        raw_text=str(data.get("decoded_text") or text),
        corrected_text=text,
        quality_score=score,
        duration_sec=round(time.monotonic() - started, 3),
    )
