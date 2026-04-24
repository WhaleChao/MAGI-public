"""Tests for api.handlers.laf_handler."""


def test_detect_action_go_live():
    from api.handlers.laf_handler import detect_laf_report_action
    action, label = detect_laf_report_action("幫我做開辦回報")
    assert action == "go_live"
    assert label == "開辦"


def test_detect_action_inquiry():
    from api.handlers.laf_handler import detect_laf_report_action
    action, label = detect_laf_report_action("疑義回報")
    assert action == "inquiry"


def test_detect_action_fee():
    from api.handlers.laf_handler import detect_laf_report_action
    action, label = detect_laf_report_action("訴訟中費用支付回報")
    assert action == "fee"


def test_detect_action_empty():
    from api.handlers.laf_handler import detect_laf_report_action
    assert detect_laf_report_action("你好") == ("", "")


def test_parse_payload_with_name():
    from api.handlers.laf_handler import parse_laf_report_payload
    p = parse_laf_report_payload("幫我做蕭仁俊開辦回報")
    assert p is not None
    assert p["action"] == "go_live"
    assert p["client_name"] == "蕭仁俊"


def test_parse_payload_with_case_no():
    from api.handlers.laf_handler import parse_laf_report_payload
    p = parse_laf_report_payload("法扶回報 疑義 1140728-K-002 原因 資力不合標準")
    assert p is not None
    assert p["action"] == "inquiry"
    assert p["laf_case_no"] == "1140728-K-002"
    assert "資力不合標準" in p["reason"]


def test_parse_condition_payload_with_client_name():
    from api.handlers.laf_handler import parse_laf_report_payload
    p = parse_laf_report_payload("[當事人G]二階段回報")
    assert p is not None
    assert p["action"] == "condition"
    assert p["client_name"] == "[當事人G]"
    assert p["fields"]["at_ctype"] == "附條件審查"


def test_parse_withdrawal_beneficiary_phrase_keeps_client_name():
    from api.handlers.laf_handler import parse_laf_report_payload
    p = parse_laf_report_payload("[當事人G]受扶助人撤回回報 原因 測試")
    assert p is not None
    assert p["action"] == "withdrawal"
    assert p["client_name"] == "[當事人G]"
    assert p["reason"] == "測試"


def test_parse_payload_unrelated():
    from api.handlers.laf_handler import parse_laf_report_payload
    assert parse_laf_report_payload("今天天氣很好") is None


def test_help_text():
    from api.handlers.laf_handler import laf_report_command_help
    h = laf_report_command_help()
    assert "法扶" in h
    assert "開辦" in h
