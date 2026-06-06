from __future__ import annotations

import zipfile
from pathlib import Path


def _docx_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def _docx_text(path: Path) -> str:
    import re

    return re.sub(r"<[^>]+>", "", _docx_xml(path))


def _make_pdf(path: Path, text: str = "法律扶助基金會委任狀") -> None:
    import fitz  # type: ignore

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(fitz.Rect(72, 72, 540, 800), text, fontsize=12)
    doc.save(str(path))
    doc.close()


def test_laf_poa_pdf_creates_fillable_word_companion(tmp_path):
    from docx import Document  # type: ignore

    from api.laf_poa_docx import (
        ensure_laf_poa_docx_companion,
        laf_poa_docx_path,
        laf_poa_template_docx_path,
    )

    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _make_pdf(pdf)

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={
            "client_name": "王惠薰",
            "laf_case_number": "1150529-E-005",
            "branch": "花蓮",
            "case_type": "消費者債務清理",
            "case_reason": "更生",
        },
    )
    docx_path = laf_poa_docx_path(pdf)
    template_path = laf_poa_template_docx_path(pdf)

    assert result["ok"] is True
    assert result["status"] == "created"
    assert result["pages"] == 1
    assert result["template_key"] == "general"
    assert result["filled_fields"]["laf_case_number"] == "1150529-E-005"
    assert template_path.exists()
    assert docx_path.exists()
    doc = Document(str(docx_path))
    assert doc.tables, "Word 版要有可直接打字的透明輸入格線"
    xml = _docx_xml(docx_path)
    assert "<w:tbl" in xml, "需要保留可輸入表格層"
    assert "王惠薰" in xml
    assert "1150529-E-005" in xml
    assert "花蓮分會" in xml
    assert "待確認" in xml
    assert "{{" in _docx_xml(template_path)

    second = ensure_laf_poa_docx_companion(pdf)
    assert second["ok"] is True
    assert second["status"] == "exists"


def test_laf_portal_zip_poa_generates_word_companion(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import _move_downloaded_to_case_folder

    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    zip_path = download_dir / "1150529-E-005.zip"
    pdf_name = "委任狀_1150529-E-005_1150601.pdf"
    pdf_source = tmp_path / pdf_name
    _make_pdf(pdf_source)

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(pdf_source, pdf_name)

    case_root = tmp_path / "case"
    moved, failed = _move_downloaded_to_case_folder([str(zip_path)], str(case_root))

    archived_pdf = case_root / "01_法扶資料" / pdf_name
    archived_docx = case_root / "01_法扶資料" / "委任狀_1150529-E-005_1150601（可填寫版）.docx"
    assert failed == []
    assert pdf_name in moved
    assert archived_pdf.exists()
    assert archived_docx.exists()


def test_laf_poa_indigenous_template_uses_center_phone(tmp_path):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    pdf = tmp_path / "委任狀_1150421-W-004_1150529.pdf"
    _make_pdf(pdf)

    result = ensure_laf_poa_docx_companion(
        pdf,
        case_metadata={
            "client_name": "李秀英",
            "client_birthday": "60年12月9日",
            "client_id": "U220557046",
            "laf_case_number": "1150421-W-004",
            "branch": "原住民族法律服務中心",
            "case_type": "行政",
            "case_reason": "勞工保險爭議",
            "lawyer_name": "喬政翔",
        },
    )

    xml = _docx_xml(laf_poa_docx_path(pdf))
    assert result["ok"] is True
    assert result["template_key"] == "indigenous_center"
    text = _docx_text(laf_poa_docx_path(pdf))
    assert "受原住民族委員會委託辦理原住民法律扶助專用委任狀" in text
    assert "李秀英" in xml
    assert "1150421-W-004" in xml
    assert "U220557046" in xml
    assert "原住民族法律服務中心" in xml
    assert "03-8509917" in xml
    assert "{{" not in xml


def test_laf_poa_pdf_background_fallback_still_works(tmp_path, monkeypatch):
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    monkeypatch.setenv("MAGI_LAF_POA_DOCX_TEMPLATES", "0")
    pdf = tmp_path / "委任狀_1150529-E-005_1150601.pdf"
    _make_pdf(pdf)

    result = ensure_laf_poa_docx_companion(pdf, case_metadata={"client_name": "王惠薰"})
    xml = _docx_xml(laf_poa_docx_path(pdf))

    assert result["ok"] is True
    assert result["status"] == "created"
    assert result["template_key"] == ""
    assert "<wp:anchor" in xml and 'behindDoc="1"' in xml, "PDF 頁面應作為文字後方背景，避免打字推動版面"
    assert "王惠薰" in xml


def test_laf_poa_pdf_itself_is_authoritative_metadata_source(tmp_path, monkeypatch):
    import api.laf_poa_docx as poa
    from api.laf_poa_docx import ensure_laf_poa_docx_companion, laf_poa_docx_path

    pdf = tmp_path / "委任狀_1150421-W-004_1150529.pdf"
    _make_pdf(pdf)

    def fake_pdf_extract(_path):
        return {
            "laf_case_number": "1150421-W-004",
            "client_name": "李秀英",
            "client_birthday": "60年12月9日",
            "client_id": "U220557046",
            "lawyer_name": "喬政翔律師",
            "branch": "原住民族法律服務中心",
            "branch_phone": "03-8509917",
            "roc_year": "115",
            "roc_month": "5",
            "roc_day": "29",
        }

    monkeypatch.setattr(poa, "_extract_laf_poa_pdf_metadata", fake_pdf_extract)

    result = ensure_laf_poa_docx_companion(pdf)
    xml = _docx_xml(laf_poa_docx_path(pdf))

    assert result["ok"] is True
    assert result["template_key"] == "indigenous_center"
    assert result["pdf_extracted_fields"]["client_name"] == "李秀英"
    assert result["pdf_extracted_fields"]["branch_phone"] == "03-8509917"
    assert "1150421-W-004" in xml
    assert "李秀英" in xml
    assert "60年12月9日" in xml
    assert "U220557046" in xml
    assert "原住民族法律服務中心" in xml
    assert "03-8509917" in xml
    assert "115 年 5 月 29 日" in _docx_text(laf_poa_docx_path(pdf))


def test_laf_poa_roc_date_uses_file_tail_not_laf_number():
    from api.laf_poa_docx import _roc_date_from_filename

    assert _roc_date_from_filename(Path("委任狀_1150421-W-004_1150529.pdf")) == {
        "roc_year": "115",
        "roc_month": "5",
        "roc_day": "29",
    }
