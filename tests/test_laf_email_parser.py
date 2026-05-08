"""Regression tests for LAF Gmail subject classification."""

from types import SimpleNamespace


def test_casper_laf_parser_handles_closing_transfer_notice():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser

    subject = "通知喬政翔律師回報(結案)1150128-I-011-陳文明-刑事偵查中辯護-詐欺之資料，業經分會轉入系統"

    info = LAFCaseTypeParser.parse_subject(subject)

    assert info is not None
    assert info.notification_type == "結案回報通知"
    assert info.laf_case_number == "1150128-I-011"
    assert info.client_name == "陳文明"
    assert info.case_type == "刑事"
    assert info.case_reason == "詐欺"
    assert info.needs_download is True


def test_casper_laf_parser_handles_progress_reminder():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser

    subject = "【提醒！請扶助律師回報案件辦理進度】(李明志)-(1131106-I-007)"

    info = LAFCaseTypeParser.parse_subject(subject)

    assert info is not None
    assert info.notification_type == "進度回報"
    assert info.laf_case_number == "1131106-I-007"
    assert info.client_name == "李明志"
    assert info.needs_download is False


def test_legacy_laf_parser_matches_closing_transfer_notice():
    from skills.legal.laf import LAFCaseTypeParser

    subject = "通知喬政翔律師回報(附條件)1140605-A-025-鄭羢允-消費者債務清理事件-消費者債務清理條例之資料，業經分會轉入系統"

    info = LAFCaseTypeParser.parse_subject(subject)

    assert info is not None
    assert info.notification_type == "附條件回報通知"
    assert info.laf_case_number == "1140605-A-025"
    assert info.client_name == "鄭羢允"
    assert info.case_type == "消費者債務清理"
    assert info.needs_download is True


def test_casper_notified_laf_email_still_queues_download(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFAutomationManager

    manager = LAFAutomationManager(
        config={"laf": {"download_folder": str(tmp_path), "auto_create_case": True}},
        db_manager=None,
        discord_notifier=None,
        log_callback=lambda _msg: None,
    )
    manager._notified_cases_file = str(tmp_path / "notified_laf_cases.json")
    manager._notified_cases = {"MSG-CLOSING"}

    case_info = SimpleNamespace(
        message_id="MSG-CLOSING",
        branch="宜蘭",
        notification_type="結案回報通知",
        client_name="陳文明",
        laf_case_number="1150128-I-011",
        case_type="刑事",
        case_stage="偵查",
        case_reason="詐欺",
        sender="laf.server@msa.hinet.net",
        received_at="2026-05-08 18:25:27",
        needs_download=True,
        has_attachment=False,
    )

    manager._on_new_case(case_info)

    assert manager.task_queue.get_nowait() is case_info
