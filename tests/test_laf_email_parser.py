"""Regression tests for LAF Gmail subject classification."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_autopilot_action_module():
    path = Path(__file__).resolve().parents[1] / "skills" / "magi-autopilot" / "action.py"
    spec = importlib.util.spec_from_file_location("magi_autopilot_action_for_laf_tests", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_casper_laf_parser_handles_closing_transfer_notice():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser

    subject = "通知範例律師回報(結案)1150128-I-011-陳文明-刑事偵查中辯護-詐欺之資料，業經分會轉入系統"

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

    subject = "通知範例律師回報(附條件)1140605-A-025-鄭羢允-消費者債務清理事件-消費者債務清理條例之資料，業經分會轉入系統"

    info = LAFCaseTypeParser.parse_subject(subject)

    assert info is not None
    assert info.notification_type == "附條件回報通知"
    assert info.laf_case_number == "1140605-A-025"
    assert info.client_name == "鄭羢允"
    assert info.case_type == "消費者債務清理"
    assert info.needs_download is True


def test_laf_parser_files_labor_insurance_dispute_as_admin():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser
    from skills.legal.laf import LAFCaseTypeParser as LegacyLAFCaseTypeParser

    subject = "【法扶花蓮分會派案通知】李秀英-1150421-W-004-民事通常程序第一審-勞工保險爭議"

    for parser in (LAFCaseTypeParser, LegacyLAFCaseTypeParser):
        info = parser.parse_subject(subject)
        assert info is not None
        assert info.laf_case_number == "1150421-W-004"
        assert info.client_name == "李秀英"
        assert info.case_reason == "勞工保險爭議"
        assert info.case_type == "行政"
        assert info.case_stage == "一審"


def test_laf_report_result_keeps_labor_insurance_as_admin():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser
    from skills.legal.laf import LAFCaseTypeParser as LegacyLAFCaseTypeParser

    subject = "通知範例律師回報(附條件)1150421-W-004-李秀英-民事通常程序第一審-勞工保險爭議之資料，業經分會轉入系統"

    for parser in (LAFCaseTypeParser, LegacyLAFCaseTypeParser):
        info = parser.parse_subject(subject)
        assert info is not None
        assert info.case_type == "行政"
        assert info.case_reason == "勞工保險爭議"


def test_legacy_staff_short_labor_insurance_hint_is_admin():
    from skills.legal.laf import LAFCaseTypeParser

    info = LAFCaseTypeParser.parse_subject("1150421-W-004李秀英(勞保)")

    assert info is not None
    assert info.case_type == "行政"
    assert info.case_reason == "勞保"


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


def test_laf_one_shot_routes_result_and_progress_emails_to_orchestrator():
    action = _load_autopilot_action_module()

    closing = SimpleNamespace(
        notification_type="結案回報通知",
        subject="通知範例律師回報(結案)1150128-I-011-陳文明-刑事偵查中辯護-詐欺之資料，業經分會轉入系統",
        snippet="",
        body="",
    )
    progress = SimpleNamespace(
        notification_type="進度回報",
        subject="【提醒！請扶助律師回報案件辦理進度】(李明志)-(1131106-I-007)",
        snippet="",
        body="",
    )

    assert action._laf_case_should_use_orchestrator(closing) is True
    assert action._laf_case_should_use_orchestrator(progress) is True


def test_laf_one_shot_keeps_dispatch_on_laf_automation_manager():
    action = _load_autopilot_action_module()

    dispatch = SimpleNamespace(
        notification_type="派案通知",
        subject="【法扶派案通知】1150501-A-001-王小明-刑事偵查中辯護-詐欺",
        snippet="",
        body="",
    )

    assert action._laf_case_should_use_orchestrator(dispatch) is False
