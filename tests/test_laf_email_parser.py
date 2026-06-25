"""Regression tests for LAF Gmail subject classification."""

import base64
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


def test_indigenous_staff_material_body_fills_pending_consumer_debt_reason(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import (
        LAFGmailMonitor,
        LAFCaseTypeParser,
    )
    from skills.legal.laf import LAFGmailMonitor as LegacyLAFGmailMonitor
    from skills.legal.laf import LAFCaseTypeParser as LegacyLAFCaseTypeParser

    subject = "【法扶原民中心來信】寄送1150529-W-002 林文俊 案件資料"
    body = "檢陳1150529-W-002 林文俊 消費者債務清理 案件資料，提供律師參考。"

    for parser, monitor_cls in (
        (LAFCaseTypeParser, LAFGmailMonitor),
        (LegacyLAFCaseTypeParser, LegacyLAFGmailMonitor),
    ):
        info = parser.parse_subject(subject)
        assert info is not None
        assert info.notification_type == "原民中心案件資料"
        assert info.case_reason == "待確認"
        assert info.has_attachment is True
        assert info.needs_download is False

        monitor = monitor_cls.__new__(monitor_cls)
        monitor._parse_staff_info(body, info)

        assert info.case_type == "消費者債務清理"
        assert info.case_stage == "其他"
        assert info.case_reason == "更生"


def test_laf_orchestrator_routes_staff_material_without_go_live():
    from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import LAFOrchestrator

    orch = LAFOrchestrator.__new__(LAFOrchestrator)
    staff = SimpleNamespace(
        notification_type="原民中心案件資料",
        subject="【法扶原民中心來信】寄送1150529-W-002 林文俊 案件資料",
        snippet="",
        body="",
        laf_case_number="1150529-W-002",
    )
    dispatch = SimpleNamespace(
        notification_type="派案通知",
        subject="【法扶花蓮分會派案通知】王惠薰-1150529-E-005-消費者債務清理事件-消費者債務清理程序",
        snippet="",
        body="",
        laf_case_number="1150529-E-005",
    )

    assert orch._resolve_email_route(staff, staff.notification_type) == "staff_material"
    assert orch._resolve_email_route(dispatch, dispatch.notification_type) == "dispatch"


def test_laf_parser_keeps_branch_staff_material_out_of_dispatch():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser
    from skills.legal.laf import LAFCaseTypeParser as LegacyLAFCaseTypeParser

    subject = "[台東分會]檢送1150116-J-002潘美雲之案件資料"

    for parser in (LAFCaseTypeParser, LegacyLAFCaseTypeParser):
        info = parser.parse_subject(subject)
        assert info is not None
        assert info.notification_type == "專員來信"
        assert info.laf_case_number == "1150116-J-002"
        assert info.client_name == "潘美雲"
        assert info.needs_download is False
        assert info.has_attachment is True


def test_laf_parser_recognizes_formal_taitung_dispatch():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser
    from skills.legal.laf import LAFCaseTypeParser as LegacyLAFCaseTypeParser

    subject = "【法扶台東分會派案通知】潘美雲-1150116-J-002-刑事偵查中辯護-詐欺等案"

    for parser in (LAFCaseTypeParser, LegacyLAFCaseTypeParser):
        info = parser.parse_subject(subject)
        assert info is not None
        assert info.notification_type == "派案通知"
        assert info.branch == "台東"
        assert info.laf_case_number == "1150116-J-002"
        assert info.client_name == "潘美雲"
        assert info.case_type == "刑事"
        assert info.case_stage == "偵查"
        assert info.case_reason == "詐欺等"
        assert info.needs_download is True


def test_laf_parser_routes_inquiry_transfer_notice_as_inquiry():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser
    from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import LAFOrchestrator
    from skills.legal.laf import LAFCaseTypeParser as LegacyLAFCaseTypeParser

    subject = "通知喬政翔律師回報(對扶助案件有疑義)1150121-T-017-呂柏暐-消費者債務清理事件-消費者債務清理事件之資料，業經分會轉入系統"
    orch = LAFOrchestrator.__new__(LAFOrchestrator)

    for parser in (LAFCaseTypeParser, LegacyLAFCaseTypeParser):
        info = parser.parse_subject(subject)
        assert info is not None
        assert info.notification_type == "疑義"
        assert info.laf_case_number == "1150121-T-017"
        assert info.client_name == "呂柏暐"
        assert info.case_type == "消費者債務清理"
        assert info.needs_download is False
        assert orch._resolve_email_route(info, info.notification_type) == "inquiry"


def test_legacy_laf_parser_treats_short_staff_attachment_as_staff_material():
    from skills.legal.laf import LAFCaseTypeParser

    for subject in (
        "潘美雲(1150116-J-002)--案情文件",
        "1150116-J-002潘美雲(刑事)",
    ):
        info = LAFCaseTypeParser.parse_subject(subject)
        assert info is not None
        assert info.notification_type == "專員來信"
        assert info.laf_case_number == "1150116-J-002"
        assert info.needs_download is False


def test_laf_gmail_queries_cover_full_mailbox_and_non_laf_senders():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFGmailMonitor
    from skills.legal.laf import LAFGmailMonitor as LegacyLAFGmailMonitor

    for monitor_cls in (LAFGmailMonitor, LegacyLAFGmailMonitor):
        queries = monitor_cls._laf_mail_search_queries(3)
        joined = "\n".join(queries)
        assert len(queries) >= 3
        assert "in:anywhere" in joined
        assert "-in:trash" in joined
        assert "from:@laf.org.tw" in joined
        assert "原民中心" in joined
        assert "法律扶助" in joined
        assert "案件資料" in joined


def test_indigenous_staff_material_process_message_keeps_download_disabled(tmp_path):
    from skills.legal.laf import LAFGmailMonitor

    monitor = LAFGmailMonitor(
        credentials_path=str(tmp_path / "credentials.json"),
        token_path=str(tmp_path / "token.pickle"),
        log_callback=lambda _msg: None,
    )
    body = "請參考案件資料，必要時請從系統下載正式文件。"
    payload = {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Subject", "value": "【法扶原民中心來信】寄送1150529-W-002 林文俊 案件資料"},
            {"name": "From", "value": "center@example.test"},
            {"name": "Date", "value": "Mon, 1 Jun 2026 10:00:00 +0800"},
        ],
        "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")},
    }

    info = monitor._process_message("MSG-STAFF", {"payload": payload, "labelIds": ["INBOX"]})

    assert info is not None
    assert info.notification_type == "原民中心案件資料"
    assert info.needs_download is False


def test_laf_report_result_keeps_labor_insurance_as_admin():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFCaseTypeParser
    from skills.legal.laf import LAFCaseTypeParser as LegacyLAFCaseTypeParser

    subject = "通知範例律師回報(附條件)1150421-W-004-李秀英-民事通常程序第一審-勞工保險爭議之資料，業經分會轉入系統"

    for parser in (LAFCaseTypeParser, LegacyLAFCaseTypeParser):
        info = parser.parse_subject(subject)
        assert info is not None
        assert info.case_type == "行政"
        assert info.case_reason == "勞工保險爭議"


def test_laf_default_case_lawyer_uses_same_settings_in_legacy_and_casper():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import _laf_default_case_lawyer as casper_default
    from skills.legal.laf import _laf_default_case_lawyer as legacy_default

    class FakeDB:
        def fetch_one(self, sql, params, as_dict=True):
            values = {
                "default_lawyer": "一般承辦律師",
                "default_debt_lawyer": "消債承辦律師",
            }
            return {"value": values.get(params[0], "")}

    for fn in (casper_default, legacy_default):
        assert fn(FakeDB(), case_type="消費者債務清理", case_reason="更生") == "消債承辦律師"
        assert fn(FakeDB(), case_type="民事", case_reason="拆屋還地") == "一般承辦律師"


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
