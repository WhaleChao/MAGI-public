from pathlib import Path


class _FakeRequest:
    def __init__(self, value=None, exc=None):
        self.value = value if value is not None else {}
        self.exc = exc

    def execute(self):
        if self.exc:
            raise self.exc
        return self.value


class _FakeMessages:
    def __init__(self, full_messages=None):
        self.full_messages = full_messages or {}
        self.list_calls = []
        self.modify_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return _FakeRequest({"messages": [{"id": msg_id} for msg_id in self.full_messages]})

    def get(self, userId, id, **_kwargs):
        return _FakeRequest(self.full_messages[id])

    def modify(self, **kwargs):
        self.modify_calls.append(kwargs)
        return _FakeRequest({})


class _FakeUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, messages):
        self.messages_api = _FakeMessages(messages)

    def users(self):
        return _FakeUsers(self.messages_api)


class _Creds:
    def __init__(self, scopes):
        self.scopes = scopes

    def has_scopes(self, scopes):
        return all(scope in self.scopes for scope in scopes)


def _laf_message(subject, labels):
    return {
        "id": "MSG-SPAM",
        "labelIds": labels,
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "laf.server@msa.hinet.net"},
            ],
            "mimeType": "text/plain",
            "body": {"data": ""},
        },
    }


def test_laf_gmail_monitor_never_uses_destructive_message_operations():
    source = (Path(__file__).resolve().parents[1] / "skills" / "legal" / "laf.py").read_text(encoding="utf-8")

    assert ".messages().delete(" not in source
    assert ".messages().trash(" not in source
    assert "'removeLabelIds': ['INBOX']" not in source
    assert '"removeLabelIds": ["INBOX"]' not in source


def test_laf_one_shot_uses_canonical_laf_module():
    source = (Path(__file__).resolve().parents[1] / "skills" / "magi-autopilot" / "action.py").read_text(encoding="utf-8")

    assert "import laf_automation_v2 as laf" not in source
    assert "from skills.legal import laf" in source


def test_laf_gmail_monitor_restores_spam_mail_to_inbox():
    from skills.legal.laf import LAFGmailMonitor

    logs = []
    subject = "【法扶花蓮分會派案通知】高弘軒-1141121-E-006-消費者債務清理事件-更生"
    msg = _laf_message(subject, ["SPAM", "UNREAD"])
    service = _FakeService({"MSG-SPAM": msg})
    monitor = LAFGmailMonitor("credentials.json", "token.pickle", log_callback=logs.append)
    monitor.service = service
    monitor.credentials = _Creds([LAFGmailMonitor.MODIFY_SCOPE])
    monitor._processed_ids = {"MSG-SPAM"}

    results = monitor.check_emails(max_results=10)

    assert results == []
    assert service.messages_api.list_calls[0]["q"].startswith("in:anywhere -in:trash ")
    assert service.messages_api.modify_calls == [
        {
            "userId": "me",
            "id": "MSG-SPAM",
            "body": {"removeLabelIds": ["SPAM"], "addLabelIds": ["INBOX"]},
        }
    ]
    assert set(msg["labelIds"]) == {"INBOX", "UNREAD"}
    assert any("已移回收件匣" in line for line in logs)


def test_laf_gmail_monitor_warns_when_restore_scope_missing():
    from skills.legal.laf import LAFGmailMonitor

    logs = []
    msg = _laf_message("【法扶花蓮分會派案通知】測試-1150101-A-001-民事通常程序第一審-測試", ["SPAM"])
    service = _FakeService({"MSG-SPAM": msg})
    monitor = LAFGmailMonitor("credentials.json", "token.pickle", log_callback=logs.append)
    monitor.service = service
    monitor.credentials = _Creds(["https://www.googleapis.com/auth/gmail.readonly"])

    restored = monitor._restore_spam_to_inbox_if_needed("MSG-SPAM", msg, "測試")

    assert restored is False
    assert service.messages_api.modify_calls == []
    assert any("缺 gmail.modify" in line for line in logs)


def test_laf_gmail_monitor_skips_json_processed_even_when_db_missing_by_default(tmp_path):
    from skills.legal.laf import LAFGmailMonitor

    logs = []
    subject = "【法扶花蓮分會派案通知】王惠薰-1150529-E-005-消費者債務清理事件-消費者債務清理程序"
    msg = _laf_message(subject, ["INBOX"])
    service = _FakeService({"MSG-RECOVER": msg})
    monitor = LAFGmailMonitor(
        "credentials.json",
        "token.pickle",
        log_callback=logs.append,
        processed_ids_file=str(tmp_path / "processed_laf_emails.json"),
    )
    monitor.service = service
    monitor._processed_ids = {"MSG-RECOVER"}

    results = monitor.check_emails(
        max_results=10,
        check_exists_func=lambda _mid: False,
        mark_processed=False,
    )

    assert results == []
    assert any("為避免重複通知仍略過" in line for line in logs)


def test_laf_gmail_monitor_can_recover_json_processed_when_enabled(tmp_path, monkeypatch):
    from skills.legal.laf import LAFGmailMonitor

    monkeypatch.setenv("MAGI_LAF_GMAIL_RECOVER_JSON_ONLY", "1")
    logs = []
    subject = "【法扶花蓮分會派案通知】王惠薰-1150529-E-005-消費者債務清理事件-消費者債務清理程序"
    msg = _laf_message(subject, ["INBOX"])
    service = _FakeService({"MSG-RECOVER": msg})
    monitor = LAFGmailMonitor(
        "credentials.json",
        "token.pickle",
        log_callback=logs.append,
        processed_ids_file=str(tmp_path / "processed_laf_emails.json"),
    )
    monitor.service = service
    monitor._processed_ids = {"MSG-RECOVER"}

    results = monitor.check_emails(
        max_results=10,
        check_exists_func=lambda _mid: False,
        mark_processed=False,
    )

    assert len(results) == 1
    assert results[0].client_name == "王惠薰"
    assert results[0].laf_case_number == "1150529-E-005"
    assert any("依 recovery 設定重新補處理法扶信件" in line for line in logs)


def test_laf_gmail_monitor_trusts_db_record_over_json(tmp_path):
    from skills.legal.laf import LAFGmailMonitor

    logs = []
    subject = "【法扶花蓮分會派案通知】王惠薰-1150529-E-005-消費者債務清理事件-消費者債務清理程序"
    msg = _laf_message(subject, ["INBOX"])
    service = _FakeService({"MSG-DONE": msg})
    monitor = LAFGmailMonitor(
        "credentials.json",
        "token.pickle",
        log_callback=logs.append,
        processed_ids_file=str(tmp_path / "processed_laf_emails.json"),
    )
    monitor.service = service
    monitor._processed_ids = set()

    results = monitor.check_emails(
        max_results=10,
        check_exists_func=lambda _mid: True,
        mark_processed=False,
    )

    assert results == []


def test_laf_manager_records_gmail_message_id_for_durable_dedup(tmp_path):
    from skills.legal.laf import LAFAutomationManager, LAFCaseInfo

    class FakeDB:
        def __init__(self):
            self.records = []

        def add_laf_email_record(self, record):
            self.records.append(dict(record))
            return True

    db = FakeDB()
    manager = LAFAutomationManager(
        config={"laf": {"download_folder": str(tmp_path), "auto_create_case": False}},
        db_manager=db,
        discord_notifier=None,
        log_callback=lambda _msg: None,
        gemini_client=object(),
    )
    info = LAFCaseInfo(
        message_id="MSG-DURABLE",
        subject="【法扶花蓮分會派案通知】王惠薰-1150529-E-005-消費者債務清理事件-消費者債務清理程序",
        sender="laf.server@msa.hinet.net",
        laf_case_number="1150529-E-005",
        client_name="王惠薰",
        branch="花蓮",
        case_type="消費者債務清理",
        case_stage="其他",
        case_reason="更生",
        notification_type="派案通知",
    )

    manager._on_new_case(info)

    assert db.records
    assert db.records[0]["gmail_message_id"] == "MSG-DURABLE"
    assert "message_id" not in db.records[0]


def test_canonical_laf_case_creator_exposes_legacy_dedupe_method(tmp_path):
    from skills.legal.laf import OSCCaseCreator

    creator = OSCCaseCreator(target_folder=str(tmp_path), log_callback=lambda _msg: None)

    result = creator.dedupe_case_folders_by_laf_marker()

    assert result["ok"] is True
    assert result["duplicates"] == 0
