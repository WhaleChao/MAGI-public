# -*- coding: utf-8 -*-

from __future__ import annotations

import base64
import json

import pytest
from api.laf_closing_transfer import (
    apply_laf_closing_transfer_notice,
    parse_laf_closing_transfer_notice,
)


SAMPLE_BODY = """
範例律師您好：(本郵件是由系統自動寄出，請勿直接回覆此郵件)
您自律師線上操作系統回報之下列資料，分會業已轉入本會系統！
分會將於完成各該審核程序後，再以電話及mailto通知您結果：
※律師姓名：範例律師
※身分證字號：A123456789
※申請編號：1140715-A-024
※受扶助人姓名：測試受扶助人
※回報類型：問題回報 - 結案
※派案分會承辦人：測試承辦人 電話：02-23225151 Email：caseworker@example.test
請注意！目前您的回報已發生回報效力！
"""


class FakeDB:
    def __init__(self, row):
        self.row = row
        self.writes = []

    def execute(self, sql, params=(), fetch=None):
        if "FROM `cases`" in sql and fetch == "one":
            return self.row
        return None

    def execute_write(self, sql, params=()):
        self.writes.append((sql, params))


def _pending_row(**overrides):
    row = {
        "id": 80,
        "case_number": "2025-0080",
        "client_name": "測試受扶助人",
        "status": "結案中",
        "legal_aid_status": "已結案，待送出",
        "legal_aid_approval_status": "暫存",
        "manual_status_lock": 0,
        "legal_aid_number": "1140715-A-024",
    }
    row.update(overrides)
    return row


@pytest.fixture(autouse=True)
def _isolate_archive_pending_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGI_LAF_CLOSING_ARCHIVE_PENDING_PATH", str(tmp_path / "closing_archive_pending.json"))
    return tmp_path / "closing_archive_pending.json"


def test_parse_user_sample_closing_transfer_notice():
    notice = parse_laf_closing_transfer_notice("法扶結案轉入通知", SAMPLE_BODY)

    assert notice is not None
    assert notice.laf_case_number == "1140715-A-024"
    assert notice.client_name == "測試受扶助人"
    assert notice.lawyer_name == "範例律師"
    assert notice.report_type == "問題回報 - 結案"
    assert notice.staff_name == "測試承辦人"
    assert notice.staff_phone == "02-23225151"
    assert notice.staff_email == "caseworker@example.test"


def test_parse_rejects_non_closing_transfer_notice():
    body = SAMPLE_BODY.replace("問題回報 - 結案", "問題回報 - 附條件")

    assert parse_laf_closing_transfer_notice("法扶附條件轉入通知", body) is None


def test_apply_updates_pending_closing_case_to_final_status(_isolate_archive_pending_path):
    notice = parse_laf_closing_transfer_notice("法扶結案轉入通知", SAMPLE_BODY)
    db = FakeDB(_pending_row())

    result = apply_laf_closing_transfer_notice(db, notice, source_message_id="msg-1")

    assert result["ok"] is True
    assert result["updated"] is True
    assert result["status"] == "updated"
    assert db.writes
    sql, params = db.writes[0]
    assert "legal_aid_status" in sql
    assert params[:2] == ("已結案", "已結案")
    assert db.row["legal_aid_status"] == "已結案"
    assert db.row["legal_aid_approval_status"] is None
    assert db.row["status"] == "已結案"
    assert result["archive_pending"]["ok"] is True
    pending = json.loads(_isolate_archive_pending_path.read_text(encoding="utf-8"))
    item = pending["items"]["case-id:80"]
    assert item["status"] == "pending_archive"
    assert item["source_message_id"] == "msg-1"


def test_apply_is_idempotent_for_already_final_case_without_secondary_status():
    notice = parse_laf_closing_transfer_notice("法扶結案轉入通知", SAMPLE_BODY)
    db = FakeDB(_pending_row(status="已結案", legal_aid_status="已結案", legal_aid_approval_status=None))

    result = apply_laf_closing_transfer_notice(db, notice)

    assert result["ok"] is True
    assert result["updated"] is False
    assert result["status"] == "already_final"
    assert db.writes == []


def test_apply_clears_stale_secondary_transfer_status():
    notice = parse_laf_closing_transfer_notice("法扶結案轉入通知", SAMPLE_BODY)
    db = FakeDB(_pending_row(status="已結案", legal_aid_status="已結案", legal_aid_approval_status="已轉入"))

    result = apply_laf_closing_transfer_notice(db, notice)

    assert result["ok"] is True
    assert result["updated"] is True
    assert db.row["legal_aid_status"] == "已結案"
    assert db.row["legal_aid_approval_status"] is None


def test_apply_blocks_client_name_mismatch():
    notice = parse_laf_closing_transfer_notice("法扶結案轉入通知", SAMPLE_BODY)
    db = FakeDB(_pending_row(client_name="不同受扶助人"))

    result = apply_laf_closing_transfer_notice(db, notice)

    assert result["ok"] is False
    assert result["status"] == "client_name_mismatch"
    assert db.writes == []


def test_apply_blocks_open_case_status():
    notice = parse_laf_closing_transfer_notice("法扶結案轉入通知", SAMPLE_BODY)
    db = FakeDB(_pending_row(status="進行中", legal_aid_status="進行中", legal_aid_approval_status=""))

    result = apply_laf_closing_transfer_notice(db, notice)

    assert result["ok"] is False
    assert result["status"] == "not_pending_closing"
    assert db.writes == []


def test_apply_blocks_open_case_even_with_stale_secondary_status():
    notice = parse_laf_closing_transfer_notice("法扶結案轉入通知", SAMPLE_BODY)
    db = FakeDB(_pending_row(status="進行中", legal_aid_status="進行中", legal_aid_approval_status="暫存"))

    result = apply_laf_closing_transfer_notice(db, notice)

    assert result["ok"] is False
    assert result["status"] == "not_pending_closing"
    assert db.writes == []


def test_apply_db_unavailable_does_not_consume_notice():
    notice = parse_laf_closing_transfer_notice("法扶結案轉入通知", SAMPLE_BODY)

    result = apply_laf_closing_transfer_notice(None, notice, source_message_id="msg-1")

    assert result["ok"] is False
    assert result["status"] == "db_unavailable"
    assert result["updated"] is False


def test_laf_gmail_monitor_parses_body_only_closing_transfer_notice():
    from casper_ecosystem.law_firm_orchestrators.laf_automation_v2 import LAFGmailMonitor

    encoded = base64.urlsafe_b64encode(SAMPLE_BODY.encode("utf-8")).decode("ascii")
    msg = {
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "系統自動通知"},
                {"name": "From", "value": "laf.server@laf.org.tw"},
                {"name": "Date", "value": "Wed, 01 Jul 2026 05:30:16 +0800"},
            ],
            "body": {"data": encoded},
        }
    }
    monitor = LAFGmailMonitor(
        credentials_path="/tmp/missing_credentials.json",
        token_path="/tmp/missing_laf_token.pickle",
        log_callback=lambda _msg: None,
    )

    info = monitor._process_message("gmail-msg-1", msg)

    assert info is not None
    assert info.notification_type == "結案轉入通知"
    assert info.laf_case_number == "1140715-A-024"
    assert info.client_name == "測試受扶助人"
    assert info.needs_download is False
