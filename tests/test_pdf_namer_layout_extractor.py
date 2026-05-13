# -*- coding: utf-8 -*-
import importlib.util
import json
import os
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "pdf-namer" / "layout_extractor.py"


def _load_module(name="pdf_namer_layout_extractor_test"):
    spec = importlib.util.spec_from_file_location(name, str(MODULE_PATH))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_docling_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGI_PDF_NAMER_DOCLING_ENABLED", raising=False)
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    mod = _load_module("pdf_namer_layout_disabled")
    assert mod.generate_layout_sidecar(str(pdf)) is None


def test_docling_sidecar_generated_with_fake_converter(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_PDF_NAMER_DOCLING_ENABLED", "1")
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    mod = _load_module("pdf_namer_layout_enabled")

    class FakeDocument:
        def export_to_dict(self):
            return {"texts": [{"text": "hello"}]}

    class FakeConverter:
        def convert(self, _path):
            return types.SimpleNamespace(document=FakeDocument())

    monkeypatch.setattr(mod, "_get_converter", lambda: FakeConverter())
    out = mod.generate_layout_sidecar(str(pdf))

    assert out == str(pdf) + ".layout.json"
    assert os.path.exists(out)
    assert json.loads(Path(out).read_text(encoding="utf-8"))["texts"][0]["text"] == "hello"
