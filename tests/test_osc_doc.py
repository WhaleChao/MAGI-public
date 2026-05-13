import os

import pytest

_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

try:
    from api.osc_document_generator import (
        generate_engagement_agreement,
        generate_poa,
        generate_receipt,
    )
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
