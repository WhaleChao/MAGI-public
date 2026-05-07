# -*- coding: utf-8 -*-
"""Tests for Chandra fallback selection inside pdf-namer."""

from __future__ import annotations


def test_chandra_not_called_when_existing_ocr_is_good(monkeypatch):
    import importlib.util
    from pathlib import Path

    action_path = Path(__file__).resolve().parents[1] / "skills" / "pdf-namer" / "action.py"
    spec = importlib.util.spec_from_file_location("pdf_namer_action_chandra_good", action_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    calls = []
    monkeypatch.setattr(module, "_CHANDRA_OCR_MIN_SCORE", 0.10)
    monkeypatch.setattr(module, "_chandra_ocr_page", lambda pdf_path, page_idx=0: calls.append(page_idx) or "bad")

    text = "臺灣花蓮地方法院\n114年度訴字第123號\n聲請人王大明"
    selected = module._prefer_chandra_if_better(text, "/tmp/example.pdf", 0)

    assert selected == text
    assert calls == []


def test_chandra_wins_when_current_ocr_is_empty(monkeypatch):
    import importlib.util
    from pathlib import Path

    action_path = Path(__file__).resolve().parents[1] / "skills" / "pdf-namer" / "action.py"
    spec = importlib.util.spec_from_file_location("pdf_namer_action_chandra_empty", action_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "_CHANDRA_OCR_MIN_SCORE", 0.45)
    monkeypatch.setattr(
        module,
        "_chandra_ocr_page",
        lambda pdf_path, page_idx=0: "臺灣花蓮地方法院\n114年度訴字第123號\n民事陳報狀",
    )

    selected = module._prefer_chandra_if_better("", "/tmp/example.pdf", 0)

    assert "臺灣花蓮地方法院" in selected


def test_chandra_failure_keeps_current_text(monkeypatch):
    import importlib.util
    from pathlib import Path

    action_path = Path(__file__).resolve().parents[1] / "skills" / "pdf-namer" / "action.py"
    spec = importlib.util.spec_from_file_location("pdf_namer_action_chandra_failure", action_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "_CHANDRA_OCR_MIN_SCORE", 0.90)
    monkeypatch.setattr(module, "_chandra_ocr_page", lambda pdf_path, page_idx=0: "")

    selected = module._prefer_chandra_if_better("??", "/tmp/example.pdf", 0)

    assert selected == "??"
