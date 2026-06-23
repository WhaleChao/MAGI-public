import os

import pytest

_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

try:
    from api.osc_document_generator import (
        generate_engagement_agreement,
        generate_poa,
        generate_receipt,
    )
    from api.osc.drafts import _osc_build_form_preview
except Exception as exc:  # pragma: no cover - dependency gate
    pytest.skip(f"osc document generator unavailable: {exc}", allow_module_level=True)


DATA = {
    "court_case_no": "112年度訴字第123號",
    "client_name": "張三",
    "case_reason": "損害賠償",
    "lawyer_name": "李四",
    "amount": "150,000",
    "item": "一審訴訟",
    "case_category": "民事",
    # Match the generator's legacy field names as well.
    "法院/檢察署": "臺灣臺北地方法院",
    "案號": "112年度訴字第123號",
    "委任人/當事人": "張三",
    "受任律師": "李四律師",
    "案由/事件": "損害賠償",
    "金額": "150,000",
    "律師姓名": "李四律師",
}

CONFIG = {
    "company_name": "範例法律事務所",
    "default_lawyer": "範例律師",
}


def test_generate_poa_docx(tmp_path):
    doc = generate_poa(DATA, "民事", "代理人", CONFIG)
    output = tmp_path / "poa.docx"
    doc.save(output)
    assert output.exists()
    assert output.stat().st_size > 0


def test_generate_engagement_agreement_docx(tmp_path):
    doc = generate_engagement_agreement(DATA, CONFIG)
    output = tmp_path / "agreement.docx"
    doc.save(output)
    assert output.exists()
    assert output.stat().st_size > 0


def test_generate_receipt_docx(tmp_path):
    doc = generate_receipt(DATA, "法律服務費", CONFIG)
    output = tmp_path / "receipt.docx"
    doc.save(output)
    assert output.exists()
    assert output.stat().st_size > 0


def test_power_of_attorney_preview_includes_court_specific_fields():
    preview = _osc_build_form_preview(
        "power_of_attorney",
        {
            "case_number": "2025-0027",
            "client_name": "林黃阿姐",
            "case_reason": "債務人異議之訴",
            "court_case_no": "115年度上字第000221號",
            "court_name": "臺灣高等法院花蓮分院",
            "laf_case_no": "LAF-001",
        },
        {
            "lawyer_name": "測試律師",
            "address": "花蓮市測試路100號",
            "phone": "0912345678",
            "tax_id": "A123456789",
        },
    )

    text = preview["preview_text"]
    assert preview["form_type"] == "power_of_attorney"
    assert "法院名稱：臺灣高等法院花蓮分院" in text
    assert "法院案號：115年度上字第000221號" in text
    assert "通訊地址：花蓮市測試路100號" in text
    assert "身分證字號：A123456789" in text
