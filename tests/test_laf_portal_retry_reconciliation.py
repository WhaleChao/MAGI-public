from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import zipfile

from casper_ecosystem.law_firm_orchestrators import laf_orchestrator as laf_module
from casper_ecosystem.law_firm_orchestrators.laf_orchestrator import LAFOrchestrator
from skills.legal.laf import LAFCaseInfo, LAFCaseTypeParser, OSCCaseCreator


def _email_case(**overrides):
    values = {
        "laf_case_number": "1150001-A-001",
        "client_name": "測試當事人",
        "notification_type": "結案回報通知",
        "subject": "通知律師回報(結案)1150001-A-001-測試當事人之資料，業經分會轉入系統",
        "snippet": "",
        "body": "本案資料業經分會轉入系統，後續請由律師線上操作系統查詢進度。",
        "sender": "service@example.laf.org.tw",
        "message_id": "synthetic-transfer-message",
        "needs_download": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_generic_portal_reference_is_not_a_download_instruction():
    body = "本案資料業經分會轉入系統，後續請由律師線上操作系統查詢進度。"
    assert LAFCaseTypeParser.check_needs_download(body) is False


def test_explicit_portal_download_instruction_is_detected():
    assert LAFCaseTypeParser.check_needs_download("請至律師線上操作系統下載表單辦理下載。") is True
    assert LAFCaseTypeParser.check_needs_download("請登入律師線上操作系統後下載附件。") is True


def test_report_transfer_subject_does_not_claim_download_before_body_evidence():
    parsed = LAFCaseTypeParser.parse_subject(
        "通知律師回報(結案)1150001-A-001-測試當事人之資料，業經分會轉入系統"
    )
    assert isinstance(parsed, LAFCaseInfo)
    assert parsed.notification_type == "結案回報通知"
    assert parsed.needs_download is False


def test_transfer_confirmation_routes_without_portal_download():
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    assert orchestrator._resolve_email_route(_email_case(), "結案回報通知") == "transfer_confirmation"


def test_transfer_confirmation_with_explicit_download_routes_to_result_download():
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    case_info = _email_case(body="請至律師線上操作系統下載表單下載本次通知附件。")
    assert orchestrator._resolve_email_route(case_info, "結案回報通知") == "result_download"


def test_official_review_result_subject_without_download_instruction_is_notice_only():
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    case_info = _email_case(
        notification_type="審核結果通知",
        subject="【法扶測試分會審核結果通知】測試當事人-1150001-A-001-民事一審-測試",
        body="",
    )
    parsed = LAFCaseTypeParser.parse_subject(case_info.subject)

    assert isinstance(parsed, LAFCaseInfo)
    assert parsed.needs_download is False
    assert orchestrator._resolve_email_route(case_info, "審核結果通知") == "review_notice"


def test_official_review_result_with_explicit_download_instruction_routes_to_download():
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    case_info = _email_case(
        notification_type="審核結果通知",
        subject="【法扶測試分會審核結果通知】測試當事人-1150001-A-001-民事一審-測試",
        body="請於七日內至律師線上操作系統下載表單。",
    )

    assert orchestrator._resolve_email_route(case_info, "審核結果通知") == "result_download"


def test_fee_words_without_download_instruction_do_not_route_to_result_download():
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    case_info = _email_case(
        notification_type="費用",
        subject="領款單處理進度 1150001-A-001",
        body="請由律師線上操作系統查詢狀態。",
    )
    assert orchestrator._resolve_email_route(case_info, "費用") == "fee"


def test_transfer_confirmation_callback_never_calls_download_handler(monkeypatch):
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    orchestrator.dry_run = True
    monkeypatch.setattr(laf_module, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "handle_review_result_download",
        lambda _case: (_ for _ in ()).throw(AssertionError("download handler must not run")),
    )

    result = orchestrator.on_new_email(_email_case())

    assert result == {
        "ok": True,
        "route": "transfer_confirmation",
        "ignored": True,
        "created_case": False,
        "portal_download_requested": False,
    }


def test_review_notice_without_download_instruction_never_calls_download_handler(monkeypatch):
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    orchestrator.dry_run = True
    monkeypatch.setattr(laf_module, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "handle_review_result_download",
        lambda _case: (_ for _ in ()).throw(AssertionError("download handler must not run")),
    )
    case_info = _email_case(
        notification_type="審核結果通知",
        subject="【法扶測試分會審核結果通知】測試當事人-1150001-A-001-民事一審-測試",
        body="本案審查結果已更新，請登入系統查詢進度。",
    )

    result = orchestrator.on_new_email(case_info)

    assert result == {
        "ok": True,
        "route": "review_notice",
        "ignored": True,
        "created_case": False,
        "portal_download_requested": False,
    }


def test_transfer_confirmation_reconciles_only_same_message_retry(monkeypatch):
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    orchestrator.dry_run = False
    orchestrator._db = None
    cleared = []
    monkeypatch.setattr(laf_module, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrator, "_log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator,
        "_clear_pending_portal_download",
        lambda laf_number, *, expected_queue_token="": cleared.append(
            (laf_number, expected_queue_token)
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "handle_review_result_download",
        lambda _case: (_ for _ in ()).throw(AssertionError("download handler must not run")),
    )
    case_info = _email_case(message_id="synthetic-transfer-message")

    result = orchestrator.on_new_email(case_info)

    assert result["route"] == "transfer_confirmation"
    assert cleared == [
        (
            "1150001-A-001",
            orchestrator._portal_retry_token_for_trigger("synthetic-transfer-message"),
        )
    ]


class _DB:
    def __init__(self, *, rows=None, states=None):
        self.rows = list(rows or [])
        self.states = dict(states or {})
        self.queries: list[str] = []

    def fetch_all(self, query, params):
        self.queries.append(str(query))
        return list(self.rows)

    def fetch_one(self, query, params, as_dict=True):
        self.queries.append(str(query))
        return dict(self.states.get(str(params[0]), {}))


def _orch(tmp_path: Path) -> LAFOrchestrator:
    orchestrator = LAFOrchestrator.__new__(LAFOrchestrator)
    orchestrator.dry_run = False
    orchestrator._portal_retry_state_path = tmp_path / "laf_pending_portal_downloads.json"
    orchestrator._portal_retry_lock_path = tmp_path / "laf_pending_portal_downloads.lock"
    return orchestrator


def test_new_result_event_renews_logically_expired_pending_row(tmp_path):
    orchestrator = _orch(tmp_path)
    old_first_seen = datetime.now() - timedelta(days=30)
    orchestrator._save_pending_portal_downloads(
        {
            "115-SYNTHETIC": {
                "laf_case_number": "115-SYNTHETIC",
                "status": "pending_retry",
                "origin_reason": "review_result_download",
                "first_queued_at": old_first_seen.isoformat(timespec="seconds"),
                "first_observed_at": old_first_seen.isoformat(timespec="seconds"),
                "expires_at": (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),
                "tries": 9,
                "queue_token": "old-event",
            }
        }
    )

    queued = orchestrator._queue_pending_portal_download(
        laf_number="115-SYNTHETIC",
        reason="review_result_download",
        trigger_id="synthetic-gmail-message-2",
    )
    saved = orchestrator._load_pending_portal_downloads()["115-SYNTHETIC"]

    assert queued is True
    assert saved["status"] == "pending_retry"
    assert saved["tries"] == 0
    assert saved["queue_token"] != "old-event"
    assert datetime.fromisoformat(saved["expires_at"]) > datetime.now()
    assert orchestrator._portal_retry_item_is_pending(saved) is True
    assert orchestrator._last_portal_retry_receipts["115-SYNTHETIC"] == saved["queue_token"]


def test_new_result_event_uses_email_received_time_for_observation_window(tmp_path):
    orchestrator = _orch(tmp_path)
    received_at = (datetime.now() - timedelta(days=3)).replace(microsecond=0)

    queued = orchestrator._queue_pending_portal_download(
        laf_number="115-SYNTHETIC",
        reason="review_result_download",
        trigger_id="synthetic-gmail-message-received-at",
        trigger_received_at=received_at,
    )
    saved = orchestrator._load_pending_portal_downloads()["115-SYNTHETIC"]

    assert queued is True
    assert saved["event_received_at"] == received_at.isoformat(timespec="seconds")
    assert saved["first_observed_at"] == received_at.isoformat(timespec="seconds")
    assert datetime.fromisoformat(saved["expires_at"]) == received_at + timedelta(
        days=laf_module._PORTAL_ATTACHMENT_RETENTION_DAYS
    )
    assert datetime.fromisoformat(saved["first_queued_at"]) > received_at


def test_same_expired_gmail_event_does_not_extend_retention_forever(tmp_path):
    orchestrator = _orch(tmp_path)
    trigger_id = "same-synthetic-gmail-message"
    token = hashlib.sha256(
        f"laf-portal-retry-v1\0{trigger_id}".encode("utf-8")
    ).hexdigest()
    expired_at = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    orchestrator._save_pending_portal_downloads(
        {
            "115-SYNTHETIC": {
                "laf_case_number": "115-SYNTHETIC",
                "status": "pending_retry",
                "origin_reason": "review_result_download",
                "expires_at": expired_at,
                "queue_token": token,
                "tries": 1,
            }
        }
    )

    queued = orchestrator._queue_pending_portal_download(
        laf_number="115-SYNTHETIC",
        reason="review_result_download",
        trigger_id=trigger_id,
    )
    saved = orchestrator._load_pending_portal_downloads()["115-SYNTHETIC"]

    assert queued is False
    assert saved["expires_at"] == expired_at
    assert orchestrator._portal_retry_item_is_pending(saved) is False


def test_stale_retry_merge_cannot_overwrite_new_gmail_receipt(tmp_path):
    orchestrator = _orch(tmp_path)
    orchestrator._save_pending_portal_downloads(
        {
            "115-SYNTHETIC": {
                "laf_case_number": "115-SYNTHETIC",
                "status": "pending_retry",
                "queue_token": "old-event",
                "tries": 1,
            }
        }
    )
    stale_retry_row = dict(
        orchestrator._load_pending_portal_downloads()["115-SYNTHETIC"]
    )

    assert orchestrator._queue_pending_portal_download(
        laf_number="115-SYNTHETIC",
        reason="review_result_download",
        trigger_id="synthetic-gmail-message-new",
    ) is True
    new_receipt = orchestrator._last_portal_retry_receipts["115-SYNTHETIC"]
    stale_retry_row.update(status="done", resolution_reason="stale-worker")
    orchestrator._merge_pending_portal_downloads(
        {
            "115-SYNTHETIC": stale_retry_row,
            "115-UNRELATED": {
                "laf_case_number": "115-UNRELATED",
                "status": "pending_retry",
            },
        }
    )
    saved = orchestrator._load_pending_portal_downloads()

    assert saved["115-SYNTHETIC"]["queue_token"] == new_receipt
    assert saved["115-SYNTHETIC"]["status"] == "pending_retry"
    assert saved["115-UNRELATED"]["status"] == "pending_retry"


def test_missing_files_cannot_report_success_when_retry_store_is_not_durable(tmp_path):
    orchestrator = _orch(tmp_path)
    orchestrator._resolve_case_folder_for_laf = lambda *_args, **_kwargs: ""
    orchestrator._queue_pending_portal_download = lambda **_kwargs: False

    result = orchestrator._process_portal_download_result(
        laf_number="115-SYNTHETIC",
        files=[],
        trigger_reason="review_result_download",
        trigger_id="synthetic-message",
    )

    assert result["ok"] is False
    assert result["retry_queued"] is False
    assert result["error"] == "portal_retry_queue_not_durable"


def test_opening_retry_is_resolved_only_for_active_opening_workflow():
    active = {"legal_aid_status": "進行中"}
    unopened = {"legal_aid_status": "未開辦"}

    for reason in (
        "go_live",
        "portal_not_listed",
        "startup_backfill_missing_opening_docs",
    ):
        assert LAFOrchestrator._opening_retry_is_resolved_by_case_state(
            {"origin_reason": reason}, active
        )
        assert not LAFOrchestrator._opening_retry_is_resolved_by_case_state(
            {"origin_reason": reason}, unopened
        )

    assert not LAFOrchestrator._opening_retry_is_resolved_by_case_state(
        {"origin_reason": "startup_backfill_missing_closing_docs"}, active
    )


def test_seed_selects_laf_status_and_skips_active_case_before_folder_scan(tmp_path):
    orchestrator = _orch(tmp_path)
    orchestrator._db = _DB(
        rows=[
            {
                "case_number": "2026-0077",
                "client_name": "吳芷菁",
                "case_type": "刑事",
                "case_reason": "傷害",
                "legal_aid_number": "1150722-W-001",
                "folder_path": "/nonexistent-sync-root/2026-0077",
                "status": "進行中",
                "legal_aid_status": "進行中",
                "created_date": datetime.now() - timedelta(days=3),
            }
        ]
    )
    orchestrator._load_seed_skip_list = lambda: set()
    orchestrator._to_local_case_folder = lambda path: (_ for _ in ()).throw(
        AssertionError("active cases must not resolve stale sync folders")
    )
    orchestrator._scan_case_folder_docs = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("active cases must not be rescanned for opening documents")
    )

    result = orchestrator._seed_pending_portal_retries_from_case_inventory(limit=80)

    assert result == {"ok": True, "seeded": 0, "scanned": 1}
    assert "legal_aid_status" in orchestrator._db.queries[0]


def test_seed_uses_existing_folder_fallback_for_unopened_case(tmp_path):
    stale = tmp_path / "missing-cloud-root"
    mounted = tmp_path / "mounted-case"
    mounted.mkdir()
    orchestrator = _orch(tmp_path)
    orchestrator._db = _DB(
        rows=[
            {
                "case_number": "2026-0088",
                "client_name": "測試",
                "case_type": "刑事",
                "case_reason": "傷害",
                "legal_aid_number": "1150801-W-001",
                "folder_path": str(stale),
                "status": "進行中",
                "legal_aid_status": "未開辦",
                "created_date": datetime.now() - timedelta(days=3),
            }
        ]
    )
    orchestrator._load_seed_skip_list = lambda: set()
    orchestrator._to_local_case_folder = lambda path: str(stale)
    orchestrator._resolve_case_folder_with_fallback = lambda path: str(mounted)
    observed = {}

    def scan(path, *, action):
        observed.update(path=path, action=action)
        return {"opening_notice_files": [], "poa_files": []}

    orchestrator._scan_case_folder_docs = scan
    orchestrator._queue_pending_portal_download = lambda **kwargs: observed.update(
        queued=kwargs
    ) or True

    result = orchestrator._seed_pending_portal_retries_from_case_inventory(limit=80)

    assert result["seeded"] == 1
    assert observed["path"] == str(mounted)
    assert observed["queued"]["case_folder"] == str(mounted)


def test_seed_does_not_reopen_closed_case_when_transfer_notice_exists(tmp_path):
    case_folder = tmp_path / "closed-case"
    case_folder.mkdir()
    orchestrator = _orch(tmp_path)
    orchestrator._db = _DB(
        rows=[
            {
                "case_number": "2026-0001",
                "client_name": "測試",
                "case_type": "刑事",
                "case_reason": "傷害",
                "legal_aid_number": "115-A",
                "folder_path": str(case_folder),
                "status": "已結案",
                "legal_aid_status": "已結案",
                "created_date": datetime.now() - timedelta(days=120),
            }
        ]
    )
    orchestrator._load_seed_skip_list = lambda: set()
    orchestrator._to_local_case_folder = lambda path: str(case_folder)
    orchestrator._resolve_case_folder_with_fallback = lambda path: str(case_folder)
    orchestrator._scan_case_folder_docs = lambda *args, **kwargs: {
        "closing_fee_files": [],
    }
    orchestrator._nas_satisfies_trigger = lambda reason, folder: (
        True,
        "nas_has_closing_docs",
    )
    orchestrator._queue_pending_portal_download = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("completed closing evidence must not be requeued")
    )

    result = orchestrator._seed_pending_portal_retries_from_case_inventory(limit=80)

    assert result == {"ok": True, "seeded": 0, "scanned": 1}


def test_retry_reconciles_active_items_without_browser_login(tmp_path):
    orchestrator = _orch(tmp_path)
    items = {
        "115-A": {
            "laf_case_number": "115-A",
            "origin_reason": "portal_not_listed",
            "status": "pending_retry",
        },
        "115-B": {
            "laf_case_number": "115-B",
            "origin_reason": "startup_backfill_missing_opening_docs",
            "status": "pending_retry",
        },
    }
    orchestrator._save_pending_portal_downloads(items)
    orchestrator._db = _DB(
        states={
            "115-A": {"legal_aid_status": "進行中"},
            "115-B": {"legal_aid_status": "進行中"},
        }
    )
    orchestrator._get_automation = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("reconciled items must not create a portal browser")
    )

    result = orchestrator._retry_pending_portal_downloads(max_items=6)
    saved = orchestrator._load_pending_portal_downloads()

    assert result["ok"] is True
    assert result["processed"] == 2
    assert all(item["status"] == "done" for item in saved.values())
    assert all(
        item["resolution_reason"] == "db_laf_status_active"
        for item in saved.values()
    )


def test_expired_closing_retry_is_done_when_laf_report_was_transferred(tmp_path):
    orchestrator = _orch(tmp_path)
    orchestrator._save_pending_portal_downloads(
        {
            "1150413-I-005": {
                "laf_case_number": "1150413-I-005",
                "origin_reason": "startup_backfill_missing_closing_docs",
                "status": "expired",
                "last_error": "portal_attachment_retention_expired",
            }
        }
    )
    orchestrator._db = _DB(
        states={
            "1150413-I-005": {
                "status": "已結案",
                "legal_aid_status": "已結案",
                "legal_aid_approval_status": "已轉入",
            }
        }
    )
    orchestrator._resolve_case_folder_for_laf = lambda *args, **kwargs: ""
    orchestrator._get_automation = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("accepted closing reports must not reopen the portal")
    )

    result = orchestrator._retry_pending_portal_downloads(max_items=6)
    saved = orchestrator._load_pending_portal_downloads()["1150413-I-005"]

    assert result["ok"] is True
    assert result["processed"] == 1
    assert saved["status"] == "done"
    assert saved["last_error"] == ""
    assert saved["resolution_reason"] == "laf_closing_accepted"


def test_expired_retry_is_done_when_matching_attachment_exists_locally(tmp_path):
    orchestrator = _orch(tmp_path)
    case_folder = tmp_path / "2026-0001"
    case_folder.mkdir()
    orchestrator._save_pending_portal_downloads(
        {
            "115-A": {
                "laf_case_number": "115-A",
                "origin_reason": "startup_backfill_missing_closing_docs",
                "case_folder": str(case_folder),
                "status": "expired",
                "last_error": "portal_attachment_retention_expired",
            }
        }
    )
    orchestrator._db = _DB(states={"115-A": {}})
    orchestrator._resolve_case_folder_for_laf = (
        lambda *args, **kwargs: str(case_folder)
    )
    orchestrator._nas_satisfies_trigger = (
        lambda reason, folder, **_kwargs: (True, "nas_has_closing_fee")
    )
    orchestrator._get_automation = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("existing local attachments must not reopen the portal")
    )

    result = orchestrator._retry_pending_portal_downloads(max_items=6)
    saved = orchestrator._load_pending_portal_downloads()["115-A"]

    assert result["ok"] is True
    assert saved["status"] == "done"
    assert saved["resolution_reason"] == "nas_has_closing_fee"


def test_expired_retry_without_available_action_is_archived_not_retried(tmp_path):
    orchestrator = _orch(tmp_path)
    orchestrator._save_pending_portal_downloads(
        {
            "115-OLD": {
                "laf_case_number": "115-OLD",
                "origin_reason": "startup_backfill_missing_closing_docs",
                "status": "expired",
                "last_error": "portal_attachment_retention_expired",
            }
        }
    )
    orchestrator._db = _DB(states={"115-OLD": {}})
    orchestrator._resolve_case_folder_for_laf = lambda *args, **kwargs: ""
    orchestrator._get_automation = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("expired historical items must not reopen the portal")
    )

    result = orchestrator._retry_pending_portal_downloads(max_items=6)
    saved = orchestrator._load_pending_portal_downloads()["115-OLD"]

    assert result["ok"] is True
    assert saved["status"] == "archived"
    assert saved["resolution_reason"] == "portal_retention_expired_archived"


def test_inventory_backfill_cannot_requeue_archived_retry(tmp_path):
    orchestrator = _orch(tmp_path)
    orchestrator._save_pending_portal_downloads(
        {
            "115-OLD": {
                "laf_case_number": "115-OLD",
                "status": "archived",
                "tries": 0,
                "reason": "startup_backfill_missing_closing_docs",
            }
        }
    )

    queued = orchestrator._queue_pending_portal_download(
        laf_number="115-OLD",
        reason="startup_backfill_missing_closing_docs",
    )

    assert queued is False
    assert orchestrator._load_pending_portal_downloads()["115-OLD"]["status"] == "archived"


def test_new_mail_event_may_reopen_archived_retry_with_new_observation_window(tmp_path):
    orchestrator = _orch(tmp_path)
    orchestrator._save_pending_portal_downloads(
        {
            "115-OLD": {
                "laf_case_number": "115-OLD",
                "status": "archived",
                "tries": 0,
                "reason": "startup_backfill_missing_closing_docs",
            }
        }
    )

    queued = orchestrator._queue_pending_portal_download(
        laf_number="115-OLD",
        reason="review_result_download",
    )

    saved = orchestrator._load_pending_portal_downloads()["115-OLD"]
    assert queued is True
    assert saved["status"] == "pending_retry"
    assert saved["origin_reason"] == "review_result_download"
    assert saved["first_queued_at"]
    assert saved["expires_at"] > saved["first_queued_at"]


def test_legacy_case_only_seed_skip_does_not_block_new_inventory_event(tmp_path):
    orchestrator = _orch(tmp_path)
    case_folder = tmp_path / "case"
    case_folder.mkdir()
    orchestrator._db = _DB(
        rows=[
            {
                "case_number": "2026-0099",
                "client_name": "測試",
                "case_type": "刑事",
                "case_reason": "傷害",
                "legal_aid_number": "115-SYNTHETIC",
                "folder_path": str(case_folder),
                "status": "進行中",
                "legal_aid_status": "未開辦",
                "created_date": datetime.now() - timedelta(days=3),
            }
        ]
    )
    # A legacy file keyed only by case number must be fail-closed and ignored.
    orchestrator._load_seed_skip_list = lambda: {"115-SYNTHETIC"}
    orchestrator._to_local_case_folder = lambda path: str(case_folder)
    orchestrator._resolve_case_folder_with_fallback = lambda path: str(case_folder)
    orchestrator._scan_case_folder_docs = lambda *args, **kwargs: {
        "opening_notice_files": [],
        "poa_files": [],
    }
    queued = []
    orchestrator._queue_pending_portal_download = lambda **kwargs: queued.append(kwargs) or True

    result = orchestrator._seed_pending_portal_retries_from_case_inventory(limit=80)

    assert result["seeded"] == 1
    assert queued and queued[0]["laf_number"] == "115-SYNTHETIC"


def test_laf_archive_same_name_uses_content_identity(tmp_path):
    creator = OSCCaseCreator.__new__(OSCCaseCreator)
    creator.log = lambda *_args, **_kwargs: None
    creator.db_manager = None
    case_folder = tmp_path / "case"
    source = tmp_path / "source"
    case_folder.mkdir()
    source.mkdir()

    first = source / "通知書.pdf"
    first.write_bytes(b"first-event")
    first_result = creator._archive_files_to_folder([str(first)], str(case_folder))
    assert len(first_result["new_files"]) == 1
    assert not first_result["skipped_existing"]

    second = source / "通知書.pdf"
    second.write_bytes(b"second-event")
    second_result = creator._archive_files_to_folder([str(second)], str(case_folder))
    assert len(second_result["new_files"]) == 1
    assert not second_result["skipped_existing"]
    assert Path(second_result["new_files"][0]).name != "通知書.pdf"
    assert Path(second_result["new_files"][0]).read_bytes() == b"second-event"

    exact = source / "通知書.pdf"
    exact.write_bytes(b"second-event")
    exact_result = creator._archive_files_to_folder([str(exact)], str(case_folder))
    assert not exact_result["new_files"]
    assert len(exact_result["skipped_existing"]) == 1
    assert Path(exact_result["skipped_existing"][0]).read_bytes() == b"second-event"


def test_laf_zip_member_same_name_uses_content_identity(tmp_path):
    creator = OSCCaseCreator.__new__(OSCCaseCreator)
    creator.log = lambda *_args, **_kwargs: None
    creator.db_manager = None
    case_folder = tmp_path / "case"
    source = tmp_path / "source"
    case_folder.mkdir()
    source.mkdir()

    def make_zip(payload: bytes) -> Path:
        archive = source / "通知.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("通知書.pdf", payload)
        return archive

    first_result = creator._archive_files_to_folder([str(make_zip(b"zip-first"))], str(case_folder))
    assert len(first_result["new_files"]) == 1

    second_result = creator._archive_files_to_folder([str(make_zip(b"zip-second"))], str(case_folder))
    assert len(second_result["new_files"]) == 1
    assert Path(second_result["new_files"][0]).read_bytes() == b"zip-second"
    assert not second_result["skipped_existing"]

    exact_result = creator._archive_files_to_folder([str(make_zip(b"zip-second"))], str(case_folder))
    assert not exact_result["new_files"]
    assert len(exact_result["skipped_existing"]) == 1
    assert Path(exact_result["skipped_existing"][0]).read_bytes() == b"zip-second"
    assert exact_result["zip_backup_skipped"]
    with zipfile.ZipFile(exact_result["zip_backup_skipped"][0]) as bundle:
        assert bundle.read("通知書.pdf") == b"zip-second"


def test_successful_portal_login_clears_previous_login_error(tmp_path):
    orchestrator = _orch(tmp_path)
    orchestrator._save_pending_portal_downloads(
        {
            "115-A": {
                "laf_case_number": "115-A",
                "origin_reason": "review_result_download",
                "reason": "review_result_download",
                "status": "pending_retry",
                "last_error": "login_failed",
                "tries": 1,
            }
        }
    )
    orchestrator._db = _DB(states={"115-A": {}})
    orchestrator._resolve_case_folder_for_laf = lambda *args, **kwargs: ""
    orchestrator._acquire_pending_portal_retry_lock = lambda: True
    orchestrator._release_pending_portal_retry_lock = lambda: None
    orchestrator._process_portal_download_result = lambda **kwargs: {
        "downloaded_count": 0,
        "retry_queued": True,
    }

    class _Automation:
        def login(self):
            return True

        def download_case_files(self, laf_case_no):
            return []

        def close(self):
            return None

    orchestrator._get_automation = lambda **kwargs: _Automation()

    result = orchestrator._retry_pending_portal_downloads(max_items=1)
    saved = orchestrator._load_pending_portal_downloads()["115-A"]

    assert result["ok"] is True
    assert saved["status"] == "pending_retry"
    assert saved["last_error"] == ""


def test_retry_budget_exhaustion_does_not_count_an_unmade_attempt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(laf_module, "_PORTAL_RETRY_MAX_TRIES", 3)
    orchestrator = _orch(tmp_path)
    orchestrator._save_pending_portal_downloads(
        {
            "115-A": {
                "laf_case_number": "115-A",
                "origin_reason": "review_result_download",
                "reason": "review_result_download",
                "status": "pending_retry",
                "last_error": "",
                "tries": 3,
                "last_try_at": "2026-08-19T08:00:00",
            }
        }
    )
    orchestrator._db = _DB(states={"115-A": {}})
    orchestrator._resolve_case_folder_for_laf = lambda *args, **kwargs: ""
    orchestrator._acquire_pending_portal_retry_lock = lambda: True
    orchestrator._release_pending_portal_retry_lock = lambda: None

    class _Notifier:
        def __init__(self):
            self.messages = []

        def notify_admin(self, message, **_kwargs):
            self.messages.append(str(message))

    class _Automation:
        def login(self):
            return True

        def download_case_files(self, _laf_case_no):
            raise AssertionError("an exhausted retry must not make another portal request")

        def close(self):
            return None

    notifier = _Notifier()
    orchestrator._notifier = notifier
    orchestrator._get_automation = lambda **kwargs: _Automation()

    result = orchestrator._retry_pending_portal_downloads(max_items=1)
    saved = orchestrator._load_pending_portal_downloads()["115-A"]

    assert result["ok"] is True
    assert saved["status"] == "exhausted"
    assert saved["tries"] == 3
    assert saved["last_try_at"] == "2026-08-19T08:00:00"
    assert len(notifier.messages) == 1
    assert "已嘗試: 3 次（上限 3）" in notifier.messages[0]
    assert "已嘗試: 4 次" not in notifier.messages[0]


def test_closing_backfill_uses_closing_document_rules_before_generic_backfill(
    tmp_path,
):
    orchestrator = _orch(tmp_path)
    orchestrator._resolve_authoritative_case_folder_for_write = lambda path: path
    orchestrator._scan_case_folder_docs = lambda folder, action: {
        "closing_fee_files": [str(tmp_path / "結案酬金領款單.pdf")]
    }
    orchestrator._scan_go_live_docs = lambda folder: (_ for _ in ()).throw(
        AssertionError("closing backfill must not use opening-document rules")
    )
    orchestrator._scan_closing_docs = lambda folder: []

    satisfied, reason = orchestrator._nas_satisfies_trigger(
        "startup_backfill_missing_closing_docs",
        str(tmp_path),
    )

    assert satisfied is True
    assert reason == "nas_has_closing_fee"


def test_advance_payment_form_never_satisfies_closing_fee_gate(tmp_path):
    case_folder = tmp_path / "case"
    opening = case_folder / "02_開辦資料"
    closing = case_folder / "03_結案資料"
    opening.mkdir(parents=True)
    closing.mkdir(parents=True)
    (opening / "預付酬金領款單_115-A.pdf").write_bytes(b"%PDF-1.4\n")

    orchestrator = _orch(tmp_path)
    docs = orchestrator._scan_case_folder_docs(str(case_folder), action="closing")

    assert docs.get("closing_fee_files") == []
    satisfied, reason = orchestrator._nas_satisfies_trigger(
        "startup_backfill_missing_closing_docs",
        str(case_folder),
    )
    assert satisfied is False
    assert reason == ""

    (closing / "結案酬金領款單_115-A.pdf").write_bytes(b"%PDF-1.4\n")
    docs = orchestrator._scan_case_folder_docs(str(case_folder), action="closing")
    assert [Path(path).name for path in docs.get("closing_fee_files") or []] == [
        "結案酬金領款單_115-A.pdf"
    ]


def test_mail_triggered_result_requires_new_exact_attachment(tmp_path):
    case_folder = tmp_path / "case"
    mail = case_folder / "01_法扶資料" / "專員來信"
    closing = case_folder / "03_結案資料"
    mail.mkdir(parents=True)
    closing.mkdir(parents=True)
    (mail / "20260810_結案轉入通知_115-A.txt").write_text(
        "案件已轉入", encoding="utf-8"
    )
    old_fee = closing / "結案酬金領款單_舊.pdf"
    old_fee.write_bytes(b"%PDF-1.4\n")
    old_time = datetime.now() - timedelta(days=2)
    old_ts = old_time.timestamp()
    old_fee.touch()
    import os

    os.utime(old_fee, (old_ts, old_ts))

    orchestrator = _orch(tmp_path)
    orchestrator._resolve_authoritative_case_folder_for_write = lambda path: path
    queued_at = datetime.now().isoformat(timespec="seconds")

    assert orchestrator._nas_satisfies_trigger(
        "review_result_download",
        str(case_folder),
        evidence_after=queued_at,
    ) == (False, "")

    fresh_fee = closing / "結案酬金領款單_新.pdf"
    fresh_fee.write_bytes(b"%PDF-1.4\n")
    assert orchestrator._nas_satisfies_trigger(
        "review_result_download",
        str(case_folder),
        evidence_after=queued_at,
    ) == (True, "nas_has_closing_fee")


def test_result_download_is_not_resolved_only_by_transfer_status():
    item = {"origin_reason": "review_result_download"}
    case = {"legal_aid_approval_status": "已轉入"}

    assert LAFOrchestrator._closing_retry_is_resolved_by_case_state(item, case) is False


def test_closing_transfer_notice_text_is_completion_evidence(tmp_path):
    orchestrator = _orch(tmp_path)
    correspondence = tmp_path / "01_法扶資料" / "專員來信"
    correspondence.mkdir(parents=True)
    (correspondence / "20260729_結案轉入通知_1150413-I-005.txt").write_text(
        "案件已轉入",
        encoding="utf-8",
    )
    orchestrator._resolve_authoritative_case_folder_for_write = lambda path: path
    orchestrator._scan_case_folder_docs = lambda folder, action: {
        "closing_fee_files": []
    }

    satisfied, reason = orchestrator._nas_satisfies_trigger(
        "startup_backfill_missing_closing_docs",
        str(tmp_path),
    )

    assert satisfied is True
    assert reason == "nas_has_closing_docs"


def test_clearing_one_successful_retry_does_not_remove_another_queue_item(tmp_path):
    orchestrator = _orch(tmp_path)
    expired = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    orchestrator._save_pending_portal_downloads(
        {
            "115-A": {
                "laf_case_number": "115-A",
                "status": "pending_retry",
                "expires_at": expired,
                "queue_token": "receipt-a",
            },
            "115-B": {
                "laf_case_number": "115-B",
                "status": "pending_retry",
                "expires_at": expired,
            },
        }
    )

    orchestrator._clear_pending_portal_download(
        "115-A",
        expected_queue_token="receipt-a",
    )
    saved = orchestrator._load_pending_portal_downloads()

    assert "115-A" not in saved
    assert saved["115-B"]["status"] == "archived"
    assert saved["115-B"]["resolution_reason"] == "portal_retention_expired_archived"


def test_initial_download_cannot_clear_newer_same_case_receipt(tmp_path):
    orchestrator = _orch(tmp_path)
    older_trigger = "synthetic-message-older"
    newer_trigger = "synthetic-message-newer"
    newer_token = orchestrator._portal_retry_token_for_trigger(newer_trigger)
    orchestrator._save_pending_portal_downloads(
        {
            "115-SYNTHETIC": {
                "laf_case_number": "115-SYNTHETIC",
                "status": "pending_retry",
                "queue_token": newer_token,
                "event_received_at": (
                    datetime.now() + timedelta(days=1)
                ).isoformat(timespec="seconds"),
            }
        }
    )
    orchestrator._resolve_case_folder_for_laf = lambda *_args, **_kwargs: str(tmp_path)
    orchestrator._archive_portal_downloads = lambda *_args, **_kwargs: {
        "ok": True,
        "new_files": ["synthetic.pdf"],
        "skipped_existing": [],
    }

    result = orchestrator._process_portal_download_result(
        laf_number="115-SYNTHETIC",
        files=["synthetic.pdf"],
        source="initial",
        trigger_id=older_trigger,
        trigger_received_at=datetime.now(),
    )
    saved = orchestrator._load_pending_portal_downloads()

    assert result["ok"] is True
    assert saved["115-SYNTHETIC"]["queue_token"] == newer_token
    assert saved["115-SYNTHETIC"]["status"] == "pending_retry"


def test_newer_initial_download_clears_older_same_case_receipt(tmp_path):
    orchestrator = _orch(tmp_path)
    old_observed = datetime.now() - timedelta(days=2)
    orchestrator._save_pending_portal_downloads(
        {
            "115-SYNTHETIC": {
                "laf_case_number": "115-SYNTHETIC",
                "status": "pending_retry",
                "queue_token": "older-event-token",
                "event_received_at": old_observed.isoformat(timespec="seconds"),
            }
        }
    )
    orchestrator._resolve_case_folder_for_laf = lambda *_args, **_kwargs: str(tmp_path)
    orchestrator._archive_portal_downloads = lambda *_args, **_kwargs: {
        "ok": True,
        "new_files": ["synthetic.pdf"],
        "skipped_existing": [],
    }

    result = orchestrator._process_portal_download_result(
        laf_number="115-SYNTHETIC",
        files=["synthetic.pdf"],
        source="initial",
        trigger_id="newer-successful-message",
        trigger_received_at=datetime.now(),
    )

    assert result["ok"] is True
    assert "115-SYNTHETIC" not in orchestrator._load_pending_portal_downloads()


def test_portal_archive_rejects_user_mount_cache(tmp_path):
    orchestrator = _orch(tmp_path)
    local_cache = tmp_path / ".magi_mounts" / "lumi" / "case"
    local_cache.mkdir(parents=True)

    result = orchestrator._archive_portal_downloads(
        [str(tmp_path / "download.zip")], str(local_cache)
    )

    assert result["ok"] is False
    assert result["error"] == "authoritative_case_storage_unavailable"


def test_user_mount_cache_resolves_to_authoritative_write_target(monkeypatch):
    monkeypatch.setattr(
        laf_module,
        "translate_local_path_to_canonical",
        lambda _path: r"Y:\\lumi\\03_工作資料\\10_結案\\case",
    )
    monkeypatch.setattr(
        laf_module,
        "resolve_case_path_for_write",
        lambda path: {
            "ok": path.startswith("Y:"),
            "local_path": "/Volumes/lumi/lumi/03_工作資料/10_結案/case"
            if path.startswith("Y:")
            else "",
        },
    )

    resolved = LAFOrchestrator._resolve_authoritative_case_folder_for_write(
        "/Users/test/.magi_mounts/lumi/lumi/03_工作資料/10_結案/case"
    )

    assert resolved == "/Volumes/lumi/lumi/03_工作資料/10_結案/case"


def test_initial_download_clears_only_its_own_receipt_and_empty_token_clears_nothing(tmp_path):
    orchestrator = _orch(tmp_path)
    trigger_id = "synthetic-message-own"
    token = orchestrator._portal_retry_token_for_trigger(trigger_id)
    row = {
        "laf_case_number": "115-SYNTHETIC",
        "status": "pending_retry",
        "queue_token": token,
    }
    orchestrator._save_pending_portal_downloads({"115-SYNTHETIC": row})

    orchestrator._clear_pending_portal_download("115-SYNTHETIC")
    assert orchestrator._load_pending_portal_downloads()["115-SYNTHETIC"] == row

    orchestrator._resolve_case_folder_for_laf = lambda *_args, **_kwargs: str(tmp_path)
    orchestrator._archive_portal_downloads = lambda *_args, **_kwargs: {
        "ok": True,
        "new_files": ["synthetic.pdf"],
        "skipped_existing": [],
    }
    orchestrator._process_portal_download_result(
        laf_number="115-SYNTHETIC",
        files=["synthetic.pdf"],
        source="initial",
        trigger_id=trigger_id,
    )

    assert "115-SYNTHETIC" not in orchestrator._load_pending_portal_downloads()
