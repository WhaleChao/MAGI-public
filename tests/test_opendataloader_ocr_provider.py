from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path


def test_opendataloader_provider_extracts_markdown(monkeypatch, tmp_path):
    from skills.engine.ocr import opendataloader_provider

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def fake_convert(**kwargs):
        out = Path(kwargs["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "scan.md").write_text("臺灣花蓮地方法院\n114年度訴字第123號\n民事裁定", encoding="utf-8")

    fake_module = types.SimpleNamespace(convert=fake_convert)
    monkeypatch.setitem(sys.modules, "opendataloader_pdf", fake_module)
    monkeypatch.setenv("MAGI_OPENDATALOADER_PDF_ENABLE", "1")
    opendataloader_provider._CACHE.clear()

    result = opendataloader_provider.run_pdf(str(pdf))

    assert result.success is True
    assert result.provider == "opendataloader_pdf"
    assert "臺灣花蓮地方法院" in result.corrected_text
    assert result.quality_score > 0


def test_pdf_namer_prefers_opendataloader_when_current_text_is_weak(monkeypatch):
    action_path = Path(__file__).resolve().parents[1] / "skills" / "pdf-namer" / "action.py"
    spec = importlib.util.spec_from_file_location("pdf_namer_action_opendataloader", action_path)
    action = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(action)

    class FakeProvider:
        @staticmethod
        def run_pdf(_path, task_type="legal"):
            return types.SimpleNamespace(
                success=True,
                corrected_text="臺灣花蓮地方法院\n114年度訴字第123號\n民事裁定\n聲請人王大明",
                raw_text="",
                duration_sec=0.01,
                error=None,
            )

    monkeypatch.setattr(action, "_opendataloader_provider", FakeProvider)
    selected = action._prefer_opendataloader_if_better("??", "/tmp/example.pdf", context="unit")

    assert "臺灣花蓮地方法院" in selected


def test_pdf_bridge_uses_opendataloader_for_empty_text(monkeypatch, tmp_path):
    from skills.documents import pdf_bridge

    pdf = tmp_path / "empty.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    class FakeProvider:
        @staticmethod
        def run_pdf(_path, task_type="legal"):
            return types.SimpleNamespace(
                success=True,
                corrected_text="臺灣臺北地方法院\n115年度聲字第10號\n抗告狀",
                raw_text="",
                provider="opendataloader_pdf",
                duration_sec=0.01,
                error=None,
            )

    monkeypatch.setattr("skills.engine.ocr.opendataloader_provider.run_pdf", FakeProvider.run_pdf)
    text = pdf_bridge._maybe_use_opendataloader("", str(pdf))

    assert "臺灣臺北地方法院" in text


def test_pdf_bridge_tries_opendataloader_before_expensive_ocr(monkeypatch):
    from skills.documents import pdf_bridge

    calls = []

    def fake_odl(text, path):
        calls.append("odl")
        return "臺灣花蓮地方法院\n114年度訴字第123號\n民事裁定"

    def fake_ocr(*args, **kwargs):
        calls.append("ocr")
        return ("should not run", 1)

    monkeypatch.setattr(pdf_bridge, "_extract_text_pdftotext", lambda *a, **k: ("????", 1))
    monkeypatch.setattr(pdf_bridge, "_extract_text_fitz", lambda *a, **k: ("", 0))
    monkeypatch.setattr(pdf_bridge, "_extract_text_pdfplumber", lambda *a, **k: ("", 0))
    monkeypatch.setattr(pdf_bridge, "_maybe_use_opendataloader", fake_odl)
    monkeypatch.setattr(pdf_bridge, "_maybe_use_ocr", fake_ocr)

    text = pdf_bridge.extract_text("/tmp/fake.pdf", max_pages=1)

    assert "臺灣花蓮地方法院" in text
    assert calls == ["odl"]
