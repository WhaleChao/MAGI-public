from __future__ import annotations

import importlib.util
from pathlib import Path

from scripts.ops.skill_realworld_smoke import _create_sample_pdf

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "skills" / "pdf-namer" / "action.py"
SPEC = importlib.util.spec_from_file_location("pdf_namer_action", MODULE_PATH)
pdf_namer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(pdf_namer)


def test_generate_name_proposal_uses_fast_text_path_without_vision(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    _create_sample_pdf(pdf_path)

    called = {"vision": False}

    def _boom(*args, **kwargs):
        called["vision"] = True
        raise AssertionError("vision path should not run for searchable text pdfs")

    monkeypatch.setattr(pdf_namer, "_vision_analyze_for_naming", _boom)

    result = pdf_namer.generate_name_proposal(
        str(pdf_path),
        case_name="王小明",
        return_structured=True,
    )

    assert called["vision"] is False
    assert result["date"] == "20260403"
    assert result["court"] == "臺灣臺北地方法院"
    assert result["doc_type"] == "起訴書"
    assert result["party"] == "王小明"
    assert result["date_method"] == "ocr_fast_path"
    assert result["filename"] == "20260403 臺灣臺北地方法院起訴書（王小明）.pdf"
