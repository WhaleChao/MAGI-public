# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pytest

try:
    from PyPDF2 import PdfWriter
except ImportError:  # pragma: no cover - optional dependency name
    try:
        from pypdf import PdfWriter
    except ImportError:  # pragma: no cover - dependency gate
        PdfWriter = None

from api.debt_document_generator import (
    generate_application,
    generate_asset_statement,
    generate_creditor_list,
    generate_report,
    generate_supplement,
    get_robot_source_status,
    merge_debt_pdfs,
)


def test_debt_robot_source_bundle_is_complete(tmp_path, monkeypatch):
    address_book = tmp_path / "address-book"
    address_book.mkdir()
    (address_book / "all adress - bank.csv").write_text(
        "name,address\n合成銀行,合成市測試路一號\n", encoding="utf-8"
    )
    (address_book / "all adress - company.csv").write_text(
        "name,address\n合成公司,合成市測試路二號\n", encoding="utf-8"
    )
    monkeypatch.setenv("MAGI_DEBT_ADDRESS_BOOK_DIR", str(address_book))
    status = get_robot_source_status()
    assert status["ok"], status
    assert status["source_dir"].endswith("integrations/debt_robot")
    assert Path(status["document_dir"], "A.docx").exists()
    assert Path(status["modules"]["supplement"]).name == "06_F.py"
    assert Path(status["runtime_files"]["src/supplement_core/__init__.py"]).exists()
    assert Path(status["runtime_files"]["data/templates/D_supplement.docx"]).exists()


def test_six_debt_robot_modules_generate_outputs(tmp_path):
    application = generate_application({
        "name": "測試聲請人",
        "address": "臺北市測試路1號",
        "lawyer_name": "合成律師",
        "asset_total": 10000,
        "debt_total": 300000,
        "max_creditor_bank": "測試銀行",
        "application_court": "臺灣臺北地方法院",
    })
    app_path = tmp_path / "01_application.docx"
    application.save(app_path)

    asset = generate_asset_statement({
        "income": [{"type": "薪資", "source": "測試公司", "amount": 35000}],
        "expenses": [{"category": "房租", "monthly": 10000}],
    })
    asset_path = tmp_path / "02_asset.docx"
    asset.save(asset_path)

    creditors = generate_creditor_list({
        "creditors": [{"name": "測試銀行", "address": "臺北市測試路2號", "amount": 300000, "debt_type": "信用貸款"}],
        "auto_lookup_address": False,
    })
    creditors_path = tmp_path / "03_creditors.docx"
    creditors.save(creditors_path)

    report = generate_report({
        "A1": "1",
        "A2": "113年度司消債更字第1號",
        "A3": "公",
        "A4": "測試聲請人",
        "lawyer_name": "合成律師",
        "lawyer_address": "合成市測試路2號",
        "lawyer_phone": "02-0000-0000",
        "lawyer_mobile": "0900-000-000",
        "B1": "測試借款原因。",
        "B2": "測試調解不成立原因。",
        "B3": "測試更生方案。",
        "C1": 0,
        "C2": 0,
        "D1": 0,
        "E1": "臺灣臺北地方法院",
    })
    report_path = tmp_path / "05_report.docx"
    report.save(report_path)

    supplement = generate_supplement({
        "court": "臺灣臺北地方法院",
        "case_no": "113年度司消債更字第1號",
        "branch": "公",
        "applicant": "測試聲請人",
        "lawyer_name": "合成律師",
        "lawyer_address": "合成市測試路2號",
        "lawyer_phone": "02-0000-0000",
        "lawyer_mobile": "0900-000-000",
        "procedure": "更生",
        "items": [{"category": "勞保資料", "period": "111年至112年", "attachment": "勞保投保資料"}],
    })
    supplement_path = tmp_path / "06_supplement.docx"
    supplement.save(supplement_path)

    for path in [app_path, asset_path, creditors_path, report_path, supplement_path]:
        assert path.exists()
        assert path.stat().st_size > 0

    if PdfWriter is None:
        pytest.skip("PyPDF2/pypdf is not installed in this test environment")

    pdfs = []
    for idx in range(2):
        pdf_path = tmp_path / f"input_{idx}.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with pdf_path.open("wb") as fh:
            writer.write(fh)
        pdfs.append(str(pdf_path))

    merged_path = tmp_path / "04_merged.pdf"
    result = merge_debt_pdfs(pdfs, output_path=str(merged_path))
    assert result == str(merged_path)
    assert merged_path.exists()
    assert merged_path.stat().st_size > 0
