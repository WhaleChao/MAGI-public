from __future__ import annotations

import zipfile
from pathlib import Path


def _docx_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def _make_pdf(path: Path, text: str = "法律扶助基金會委任狀") -> None:
    import fitz  # type: ignore

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 96), text, fontsize=18)
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
    assert result["filled_fields"]["laf_case_number"] == "1150529-E-005"
    assert template_path.exists()
    assert docx_path.exists()
    doc = Document(str(docx_path))
    assert doc.tables, "Word 版要有可直接打字的透明輸入格線"
    xml = _docx_xml(docx_path)
    assert "<wp:anchor" in xml and 'behindDoc="1"' in xml, "PDF 頁面應作為文字後方背景，避免打字推動版面"
    assert "<w:tbl" in xml, "需要保留可輸入表格層"
    assert "王惠薰" in xml
    assert "1150529-E-005" in xml
    assert "花蓮" in xml

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
