from __future__ import annotations

import importlib.util
from pathlib import Path

import fitz

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


def test_generate_name_proposal_falls_back_to_laf_filename_hints(tmp_path, monkeypatch):
    case_dir = tmp_path / "2026-0017-張裕和-一審-洗錢防制法" / "01_法扶資料"
    case_dir.mkdir(parents=True)
    pdf_path = case_dir / "委任狀_1150225-E-007_1150226.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(pdf_path)
    doc.close()

    monkeypatch.setattr(pdf_namer, "HAS_OCR", False)

    def _empty_vision(*args, **kwargs):
        return {}

    monkeypatch.setattr(pdf_namer, "_vision_analyze_for_naming", _empty_vision)
    monkeypatch.setattr(pdf_namer, "_extract_receipt_date_from_stamp", lambda *a, **k: (None, "not_found"))
    monkeypatch.setattr(pdf_namer, "_try_stamp_crop_vision", lambda *a, **k: (None, "not_found"))

    result = pdf_namer.generate_name_proposal(str(pdf_path), return_structured=True)

    assert result["date"] == "20260226"
    assert result["date_method"].startswith("filename_roc_compact_fallback")
    assert result["doc_type"] == "委任狀"
    assert result["party"] == "張裕和"
    assert result["filename"] == "20260226 委任狀（張裕和）.pdf"


def test_generate_name_proposal_falls_back_when_fast_text_guard_empties_filename(tmp_path, monkeypatch):
    case_dir = tmp_path / "2026-0017-張裕和-一審-洗錢防制法" / "01_法扶資料"
    case_dir.mkdir(parents=True)
    pdf_path = case_dir / "委任狀_1150225-E-007_1150226.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (50, 100),
        "臺灣花蓮地方法院 法律扶助委任狀 本文件為測試文字，含有足夠中文字供快速路徑判定。",
        fontsize=12,
    )
    doc.save(pdf_path)
    doc.close()

    monkeypatch.setattr(
        pdf_namer,
        "_maybe_fast_text_name_result",
        lambda *a, **k: {
            "filename": None,
            "date": None,
            "court": "",
            "case_number": "",
            "doc_type": "委任狀",
            "party": "或犯",
            "date_method": "ocr_fast_path",
        },
    )

    result = pdf_namer.generate_name_proposal(str(pdf_path), return_structured=True)

    assert result["date"] == "20260226"
    assert result["doc_type"] == "委任狀"
    assert result["party"] == "張裕和"
    assert result["filename"] == "20260226 委任狀（張裕和）.pdf"
