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


def test_payment_screenshot_text_parser_accepts_wide_table_simple_court():
    text = (
        "案件繳費狀況查詢清單\n"
        "編號         案號 (遞狀流水號)                   銷帳編號              繳款人       法院名稱        繳費管道       繳費期限       繳費金額            入帳日期/入帳時間/繳費日期                  繳費狀況\n"
        "1           114.花補.000502            0031001561821271         喬o翔      花蓮簡易庭                  1150701       100                  //1150629                   繳費完成 (請款中)\n"
    )

    parsed = FileReviewManager._parse_payment_text(text)

    assert parsed["raw_case_id"] == "114.花補.000502"
    assert parsed["case_type"] == "花補"
    assert parsed["case_number"] == "502"
    assert parsed["court_name"] == "臺灣花蓮地方法院"
    assert parsed["court_code"] == "HLD"
    assert parsed["pay_id"] == "0031001561821271"
    assert parsed["payer"] == "喬o翔"
    assert parsed["payment_deadline"] == "1150701"
    assert parsed["amount"] == "100"
    assert parsed["payment_date"] == "1150629"
    assert parsed["payment_status"] == "繳費完成（請款中）"
