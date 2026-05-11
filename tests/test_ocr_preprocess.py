# -*- coding: utf-8 -*-
"""Tests for OCR preprocessing and provider adoption."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch


def _make_png(path: Path, *, text: str = "臺灣花蓮地方法院 114年度訴字第123號") -> Path:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (420, 120), "white")
    draw = ImageDraw.Draw(img)
    draw.text((18, 42), text, fill="black")
    img.save(path)
    return path


def _make_safe_run_result(returncode=0, stdout="", stderr="", timed_out=False):
    from unittest.mock import MagicMock

    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    r.timed_out = timed_out
    r.killed = False
    r.duration_sec = 0.1
    return r


def test_preprocess_image_upscales_small_png(tmp_path, monkeypatch):
    from skills.engine.ocr.preprocess import preprocess_image

    src = _make_png(tmp_path / "scan.png")
    monkeypatch.setenv("MAGI_OCR_PREPROCESS_MIN_WIDTH", "800")

    result = preprocess_image(str(src), output_dir=str(tmp_path))

    assert result.ok is True
    assert result.changed is True
    assert result.scale > 1.0
    assert result.output_path
    assert Path(result.output_path).exists()


def test_preprocess_image_rejects_missing_file():
    from skills.engine.ocr.preprocess import preprocess_image

    result = preprocess_image("/tmp/no_such_magi_ocr_image.png")

    assert result.ok is False
    assert result.error == "image_not_found"


@patch("skills.engine.ocr.tesseract_provider._safe_run")
@patch("skills.engine.ocr.tesseract_provider.check_available")
def test_tesseract_provider_uses_preprocessed_when_quality_improves(mock_check, mock_run, tmp_path, monkeypatch):
    from skills.engine.ocr import tesseract_provider as tp
    from skills.engine.ocr.preprocess import OCRPreprocessResult

    src = _make_png(tmp_path / "raw.png")
    pre = _make_png(tmp_path / "pre.png")
    mock_check.return_value = (True, "")
    mock_run.side_effect = [
        _make_safe_run_result(returncode=0, stdout="???"),
        _make_safe_run_result(returncode=0, stdout="臺灣花蓮地方法院\n114年度訴字第123號\n判決"),
    ]

    monkeypatch.setenv("MAGI_TESSERACT_PREPROCESS_ENABLE", "1")
    monkeypatch.setenv("MAGI_TESSERACT_PREPROCESS_SKIP_QUALITY", "1.0")
    monkeypatch.setattr(
        "skills.engine.ocr.preprocess.preprocess_image",
        lambda *_args, **_kwargs: OCRPreprocessResult(
            ok=True,
            input_path=str(src),
            output_path=str(pre),
            changed=True,
            scale=1.2,
        ),
    )

    result = tp.run(str(src), task_type="legal")

    assert result.success is True
    assert "臺灣花蓮地方法院" in result.raw_text
    assert mock_run.call_count == 2


@patch("skills.engine.ocr.tesseract_provider.run")
def test_pdf_namer_tesseract_bytes_uses_shared_provider(mock_provider, tmp_path):
    import importlib.util
    from skills.engine.ocr.ocr_schema import OCRProviderResult

    skill_dir = Path(__file__).resolve().parent.parent / "skills" / "pdf-namer"
    spec = importlib.util.spec_from_file_location("pdf_namer_ocr_provider_test", skill_dir / "action.py")
    mod = importlib.util.module_from_spec(spec)
    sys_path_added = False
    if str(skill_dir) not in sys.path:
        sys.path.insert(0, str(skill_dir))
        sys_path_added = True
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        mock_provider.return_value = OCRProviderResult(
            success=True,
            provider="tesseract",
            raw_text="shared provider text",
            corrected_text="shared provider text",
        )
        assert mod._ocr_image_bytes_tesseract(b"\x89PNG\r\n", timeout_sec=2, psm=6) == "shared provider text"
        assert mock_provider.called
    finally:
        if sys_path_added:
            try:
                sys.path.remove(str(skill_dir))
            except ValueError:
                pass


@patch("skills.engine.ocr.tesseract_provider._safe_run")
@patch("skills.engine.ocr.tesseract_provider.check_available")
def test_tesseract_provider_skips_preprocess_for_captcha(mock_check, mock_run, tmp_path, monkeypatch):
    from skills.engine.ocr import tesseract_provider as tp

    src = _make_png(tmp_path / "captcha.png", text="4Xk7mP")
    mock_check.return_value = (True, "")
    mock_run.return_value = _make_safe_run_result(returncode=0, stdout="4Xk7mP")
    monkeypatch.setenv("MAGI_TESSERACT_PREPROCESS_ENABLE", "1")

    result = tp.run(str(src), task_type="captcha")

    assert result.success is True
    assert result.raw_text == "4Xk7mP"
    assert mock_run.call_count == 1
