from __future__ import annotations

from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager


def test_payment_screenshot_text_parser_accepts_ocr_court_typo():
    text = (
        "案件繳費狀況查詢清單\n"
        "1 115.原交易.000021 0030996961487451 HoH "
        "臺灣臺束地方法院 1150528 200 //1150526 繳費完成(請款中)"
    )

    parsed = FileReviewManager._parse_payment_text(text)

    assert parsed["raw_case_id"] == "115.原交易.000021"
    assert parsed["court_name"] == "臺灣臺東地方法院"
    assert parsed["court_code"] == "TTD"
    assert parsed["amount"] == "200"


def test_payment_screenshot_text_parser_accepts_tai_variants():
    text = "案件繳費狀況查詢清單 115.原交易.000021 台灣台東地方法院 1150528 200 繳費完成"

    parsed = FileReviewManager._parse_payment_text(text)

    assert parsed["court_name"] == "臺灣臺東地方法院"
    assert parsed["court_code"] == "TTD"

