# -*- coding: utf-8 -*-
"""OCR image preprocessing helpers.

This module intentionally implements MAGI-owned preprocessing logic instead of
copying code from browser PDF tools.  It keeps the useful ideas: white
background compositing, contrast normalization, conservative upscaling, and
small-angle deskew before OCR.
"""

from __future__ import annotations

import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class OCRPreprocessResult:
    ok: bool
    input_path: str
    output_path: str = ""
    changed: bool = False
    angle_deg: float = 0.0
    scale: float = 1.0
    threshold: int = 0
    error: Optional[str] = None
    duration_sec: float = 0.0


def _env_bool(key: str, default: bool = True) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)).strip())
    except Exception:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.environ.get(key, str(default)).strip()))
    except Exception:
        return default


def _otsu_threshold(gray) -> int:
    import numpy as np

    arr = np.asarray(gray, dtype=np.uint8)
    hist = np.bincount(arr.ravel(), minlength=256).astype(float)
    total = arr.size
    if total <= 0:
        return 180
    sum_total = float((hist * np.arange(256)).sum())
    sum_bg = 0.0
    weight_bg = 0.0
    best_var = -1.0
    best = 180
    for i in range(256):
        weight_bg += hist[i]
        if weight_bg <= 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg <= 0:
            break
        sum_bg += i * hist[i]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        var_between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if var_between > best_var:
            best_var = var_between
            best = i
    return int(max(80, min(best, 220)))


def _projection_score(gray, threshold: int) -> float:
    import numpy as np

    arr = np.asarray(gray, dtype=np.uint8)
    ink = arr < threshold
    if ink.mean() < 0.002:
        return 0.0
    projection = ink.sum(axis=1).astype(float)
    return float(projection.var())


def estimate_skew_angle(gray, *, threshold: int, max_angle: float = 5.0, step: float = 0.5) -> tuple[float, float]:
    """Estimate correction angle for small text-line skew.

    The returned angle is the rotation to apply to the image.  If confidence is
    too weak, callers should ignore the angle.
    """
    from PIL import Image

    max_angle = max(0.0, min(float(max_angle), 10.0))
    step = max(0.25, min(float(step), 2.0))
    baseline = _projection_score(gray, threshold)
    if baseline <= 0:
        return 0.0, 0.0

    best_angle = 0.0
    best_score = baseline
    steps = int(round(max_angle / step))
    candidates = [i * step for i in range(-steps, steps + 1)]
    for angle in candidates:
        if abs(angle) < 1e-6:
            continue
        rotated = gray.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
        score = _projection_score(rotated, threshold)
        if score > best_score:
            best_score = score
            best_angle = angle

    confidence = (best_score - baseline) / max(baseline, 1.0)
    return float(best_angle), float(confidence)


def preprocess_image(
    image_path: str,
    *,
    output_dir: Optional[str] = None,
    min_width: Optional[int] = None,
    deskew: Optional[bool] = None,
) -> OCRPreprocessResult:
    """Create a normalized PNG for OCR.

    Returns a result with ``changed=False`` when preprocessing would not change
    the image enough to justify an additional OCR pass.
    """
    t0 = time.monotonic()
    src = str(image_path or "")
    if not src or not os.path.isfile(src):
        return OCRPreprocessResult(False, input_path=src, error="image_not_found")

    try:
        from PIL import Image, ImageOps
    except Exception as exc:
        return OCRPreprocessResult(False, input_path=src, error=f"pillow_unavailable: {exc}")

    try:
        img = Image.open(src)
        img.load()
    except Exception as exc:
        return OCRPreprocessResult(False, input_path=src, error=f"image_open_failed: {exc}")

    try:
        original_size = img.size
        composited_alpha = False
        if img.mode in {"RGBA", "LA"} or ("transparency" in img.info):
            rgba = img.convert("RGBA")
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            white.alpha_composite(rgba)
            img = white.convert("RGB")
            composited_alpha = True
        else:
            img = img.convert("RGB")

        min_width = int(min_width if min_width is not None else _env_int("MAGI_OCR_PREPROCESS_MIN_WIDTH", 1800))
        max_width = _env_int("MAGI_OCR_PREPROCESS_MAX_WIDTH", 3200)
        scale = 1.0
        if 0 < img.width < min_width:
            scale = min(max_width / max(1, img.width), min_width / max(1, img.width))
            if scale > 1.01:
                img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)

        gray = ImageOps.grayscale(img)
        threshold = _otsu_threshold(gray)
        gray = ImageOps.autocontrast(gray, cutoff=1)

        angle = 0.0
        angle_conf = 0.0
        do_deskew = _env_bool("MAGI_OCR_PREPROCESS_DESKEW", True) if deskew is None else bool(deskew)
        if do_deskew and min(gray.size) >= 120:
            probe = gray.copy()
            longest = max(probe.size)
            if longest > 900:
                ratio = 900 / longest
                probe = probe.resize((int(probe.width * ratio), int(probe.height * ratio)), Image.Resampling.BILINEAR)
            angle, angle_conf = estimate_skew_angle(
                probe,
                threshold=_otsu_threshold(probe),
                max_angle=_env_float("MAGI_OCR_PREPROCESS_MAX_ANGLE", 5.0),
                step=_env_float("MAGI_OCR_PREPROCESS_ANGLE_STEP", 0.5),
            )
            min_conf = _env_float("MAGI_OCR_PREPROCESS_DESKEW_CONFIDENCE", 0.08)
            if abs(angle) >= 0.35 and angle_conf >= min_conf:
                img = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(255, 255, 255))
                gray = ImageOps.grayscale(img)
            else:
                angle = 0.0

        img = ImageOps.autocontrast(img, cutoff=1)

        changed = (
            img.size != original_size
            or abs(angle) >= 0.35
            or composited_alpha
            or Path(src).suffix.lower() not in {".png"}
        )
        if not changed:
            return OCRPreprocessResult(
                True,
                input_path=src,
                output_path=src,
                changed=False,
                angle_deg=0.0,
                scale=round(scale, 3),
                threshold=threshold,
                duration_sec=round(time.monotonic() - t0, 3),
            )

        out_dir = Path(output_dir) if output_dir else Path(tempfile.mkdtemp(prefix="magi_ocr_pre_"))
        out_dir.mkdir(parents=True, exist_ok=True)
        digest = f"{abs(hash((src, img.size, round(angle, 2), round(scale, 2)))):x}"[:12]
        out_path = out_dir / f"{Path(src).stem}_ocr_pre_{digest}.png"
        img.save(out_path, "PNG", optimize=True)
        return OCRPreprocessResult(
            True,
            input_path=src,
            output_path=str(out_path),
            changed=True,
            angle_deg=round(angle, 3),
            scale=round(scale, 3),
            threshold=threshold,
            duration_sec=round(time.monotonic() - t0, 3),
        )
    except Exception as exc:
        return OCRPreprocessResult(False, input_path=src, error=f"preprocess_failed: {type(exc).__name__}: {exc}")
