from api.blueprints.osc_cases import _osc_normalize_template_case_row


def test_template_case_status_and_category_display_as_dash():
    row = {
        "id": "template-case-0000-0000-0001",
        "case_number": "0000-0000",
        "client_name": "範本",
        "case_category": "upsert-smoke",
        "case_type": "民事",
        "case_reason": "upsert-smoke",
        "status": "已結案",
    }

    normalized = _osc_normalize_template_case_row(row)

    assert normalized["case_category"] == "—"
    assert normalized["case_type"] == "—"
    assert normalized["case_reason"] == "upsert-smoke"
    assert normalized["status"] == "—"
    assert normalized["is_template_case"] is True


def test_non_template_case_display_is_unchanged():
    row = {
        "case_number": "2026-0001",
        "client_name": "王小明",
        "case_category": "一般案件",
        "case_type": "民事",
        "case_reason": "請求給付",
        "status": "進行中",
    }

    assert _osc_normalize_template_case_row(row) == row
