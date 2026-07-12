"""Regression tests for file-review notification aggregation."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parent.parent / "skills" / "file-review-orchestrator" / "action.py"


def _load_action_module():
    name = f"file_review_action_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch("builtins.print"), patch.object(sys, "argv", [str(MODULE_PATH)]), patch(
        "api.runtime_paths.get_skill_python", return_value=Path(sys.executable)
    ), patch("api.product_runtime.apply_product_runtime_env", return_value={}):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module


def test_ready_to_download_items_expose_case_identity_without_internal_paths():
    module = _load_action_module()
    info = types.SimpleNamespace(
        court_case_no="115年度訴字第1號",
        laf_case_no="1150001-A-001",
        application_no="A-9",
        client_name="王小明",
        court="臺灣花蓮地方法院",
    )

    items = module._ready_to_download_items(types.SimpleNamespace(ready_to_download=[info]))

    assert items == [
        {
            "court_case_no": "115年度訴字第1號",
            "laf_case_no": "1150001-A-001",
            "application_no": "A-9",
            "client_name": "王小明",
            "court": "臺灣花蓮地方法院",
        }
    ]


def test_recent_download_activity_ignores_exists_skip(tmp_path):
    module = _load_action_module()
    job_dir = tmp_path / "_bg_jobs"
    job_dir.mkdir()

    skip_job = {
        "success": True,
        "finished_at": datetime.now().isoformat(),
        "result": {
            "items": [
                {
                    "party": "張裕和",
                    "court_case_no": "114.易.000321",
                    "file": "ebook_ROW003.zip",
                    "action": "exists_skip",
                }
            ]
        },
    }
    copied_job = {
        "success": True,
        "finished_at": datetime.now().isoformat(),
        "result": {
            "items": [
                {
                    "party": "[當事人H]",
                    "court_case_no": "115.原金訴.000044",
                    "file": "卷宗A.pdf",
                    "dst": "/tmp/卷宗A.pdf",
                    "action": "copied",
                },
                {
                    "party": "[當事人H]",
                    "court_case_no": "115.原金訴.000044",
                    "file": "卷宗B.pdf",
                    "dst": "/tmp/卷宗B.pdf",
                    "action": "copied",
                },
            ]
        },
    }

    (job_dir / "download_skip.json").write_text(json.dumps(skip_job, ensure_ascii=False), encoding="utf-8")
    (job_dir / "download_copy.json").write_text(json.dumps(copied_job, ensure_ascii=False), encoding="utf-8")

    with patch.object(module, "BG_JOB_DIR", str(job_dir)):
        records = module._load_recent_download_activity(days=7)

    assert len(records) == 1
    assert records[0]["party"] == "[當事人H]"
    assert records[0]["case_number"] == "115.原金訴.000044"
    assert records[0]["detail"] == "已下載卷宗（2 份）"


def test_recent_activity_backlog_is_seeded_then_only_new_items_surface(tmp_path, monkeypatch):
    module = _load_action_module()
    seen_dedup = set()
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(
            is_done=lambda _category, key: key in seen_dedup,
            mark_done=lambda _category, key, metadata=None: seen_dedup.add(key),
        ),
    )
    download_folder = str(tmp_path)
    base_record = {
        "processed_at": datetime.now() - timedelta(minutes=30),
        "party": "張裕和",
        "case_number": "114.易.000321",
        "detail": "已下載卷宗（3 份）",
        "count": 3,
        "source": "download_job",
        "artifact_type": "review_download",
        "key": "download_20260320_023957_577560.json",
    }

    first = module._filter_unnotified_recent_activity(
        [base_record], download_folder, "recent_review_download_activity"
    )
    assert first == []

    second = module._filter_unnotified_recent_activity(
        [base_record], download_folder, "recent_review_download_activity"
    )
    assert second == []

    new_record = dict(base_record)
    new_record["processed_at"] = datetime.now()
    new_record["detail"] = "已下載卷宗（1 份）"
    new_record["count"] = 1
    new_record["key"] = "download_20260320_120000_test.json"

    fresh = module._filter_unnotified_recent_activity(
        [new_record], download_folder, "recent_review_download_activity"
    )
    assert len(fresh) == 1
    assert fresh[0]["detail"] == "已下載卷宗（1 份）"

    module._mark_recent_activity_notified(
        fresh, download_folder, "recent_review_download_activity"
    )
    after_mark = module._filter_unnotified_recent_activity(
        [new_record], download_folder, "recent_review_download_activity"
    )
    assert after_mark == []


def test_portal_probe_error_is_business_readable():
    module = _load_action_module()

    text = module._format_portal_probe_error(
        {
            "error": "list_page_verification_failed",
            "error_detail": {
                "page_check": {
                    "has_list_markers": False,
                    "has_table": False,
                    "tr_count": 0,
                    "body_preview": "",
                },
                "frame_diagnostics": [
                    {
                        "frame_name": "",
                        "frame_url": "https://ola.judicial.gov.tw/",
                        "body_preview": "會員登入 驗證碼 密碼",
                    }
                ],
            },
        }
    )

    assert "入口列表沒有正確載入" in text
    assert "會員登入 驗證碼 密碼" in text
    assert "{" not in text
    assert "frame_diagnostics" not in text


def test_portal_item_display_party_uses_db_case_folder_for_typos():
    module = _load_action_module()

    class FakeDb:
        def execute(self, query, params=None, fetch=None):
            if params and params[0] == "115年度原訴字第000036號":
                return {
                    "case_number": "2026-0034",
                    "court_case_number": "115年度原訴字第000036號",
                    "client_name": "陳文眀",
                    "folder_path": "/案件/法扶案件/刑事/2026-0034-陳文明-一審-傷害",
                }
            return None

    party = module._display_party_for_case_item(
        {
            "party": "陳文眀",
            "court_case_no": "115年度原訴字第000036號",
        },
        db=FakeDb(),
        cache={},
    )

    assert party == "陳文明"


def test_recent_activity_block_uses_folder_name_for_display_typos():
    module = _load_action_module()

    lines = module._format_recent_activity_block(
        "最近卷宗下載",
        [
            {
                "processed_at": datetime.now(),
                "party": "李秀瑛",
                "case_number": "115年度勞簡字第1號",
                "folder_path": "/案件/法扶案件/行政/2026-0045-李秀英-一審-勞工保險爭議/09_閱卷/卷宗.pdf",
                "detail": "已下載卷宗（1 份）",
            }
        ],
    )

    rendered = "\n".join(lines)
    assert "李秀英｜115年度勞簡字第1號" in rendered
    assert "李秀瑛" not in rendered


def test_ola_error_page_is_labeled_and_treated_as_transient():
    module = _load_action_module()

    result = {
        "error": "navigate_failed",
        "error_code": "ola_error_page",
        "error_detail": "https://ola.judicial.gov.tw/judrf/lssologinchk.htm",
    }

    text = module._format_portal_probe_error(result)

    assert "法院入口回傳錯誤頁" in text
    assert module._is_transient_portal_probe_failure(result) is True


def test_portal_probe_failure_alerts_only_after_streak(tmp_path, monkeypatch):
    module = _load_action_module()

    monkeypatch.setenv("MAGI_FILE_REVIEW_PORTAL_FAILURE_NOTIFY_STREAK", "3")
    result = {
        "success": False,
        "error": "navigate_failed",
        "error_code": "ola_error_page",
    }

    first = module._record_portal_probe_state(str(tmp_path), result)
    second = module._record_portal_probe_state(str(tmp_path), result)
    third = module._record_portal_probe_state(str(tmp_path), result)

    assert first["failure_streak"] == 1
    assert first["should_alert"] is False
    assert second["failure_streak"] == 2
    assert second["should_alert"] is False
    assert third["failure_streak"] == 3
    assert third["should_alert"] is True

    cleared = module._record_portal_probe_state(str(tmp_path), {"success": True})
    assert cleared["failure_streak"] == 0
    assert not (tmp_path / ".portal_probe_failure_state.json").exists()


def test_court_pickup_portal_row_does_not_become_pending_payment(tmp_path):
    module = _load_action_module()
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "鑫源企業社請至本院閱覽紙本卷宗，不另製發繳費單。",
        "party": "鑫源企業社",
        "court_case_no": "115年度聲字第123號",
        "rowid": "CP001",
        "applydt": _roc_compact(-1),
    }

    assert module._portal_item_is_court_pickup_ready(item) is True
    assert module._portal_item_is_actionable_pending(item) is False

    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))

    assert collapsed["court_pickup_count"] == 1
    assert collapsed["court_pickup_history_count"] == 0
    assert collapsed["pending_payment_count"] == 0
    assert collapsed["items"][0]["status"] == "court_pickup"


def test_old_portal_court_pickup_rows_are_history_not_notifications(tmp_path):
    module = _load_action_module()
    item = {
        "status": "court_pickup",
        "status_name": "法院回覆同意",
        "result_text": "請至本院閱覽紙本卷宗，不另製發繳費單。",
        "party": "王有烈等",
        "court_case_no": "108年度基簡字第000686號",
        "rowid": "CP-OLD",
        "applydt": _roc_compact(-90),
    }

    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))

    assert collapsed["court_pickup_count"] == 0
    assert collapsed["court_pickup_history_count"] == 1
    assert collapsed["count"] == 0
    assert collapsed["items"] == []


def test_completed_court_pickup_text_is_not_actionable(tmp_path):
    module = _load_action_module()
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "已到院閱卷",
        "party": "李家榛",
        "court_case_no": "108年度基簡字第000883號",
        "rowid": "CP-DONE",
        "applydt": _roc_compact(-1),
    }

    assert module._portal_item_is_court_pickup_ready(item) is False
    assert module._portal_item_is_actionable_pending(item) is False


def test_downloaded_registry_does_not_suppress_downloadable_case_by_default(tmp_path, monkeypatch):
    module = _load_action_module()
    (tmp_path / "downloaded_registry.json").write_text(
        json.dumps({
            "卷宗_劉信義.pdf": {
                "yyidno": "115.原侵重訴.000001",
                "case_info": {
                    "artifact_type": "review_download",
                    "showyyidno": "115年度原侵重訴字第000001號",
                    "case_number": "115.原侵重訴.000001",
                },
            },
            "繳費單_劉信義.pdf": {
                "yyidno": "115.原侵重訴.000001",
                "case_info": {"artifact_type": "payment_slip"},
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "downloadable",
        "party": "劉信義",
        "case_number": "115年度原侵重訴字第000001號",
        "rowid": "DL001",
    }

    monkeypatch.delenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", raising=False)

    assert module._filter_not_yet_downloaded([item], str(tmp_path)) == [item]

    monkeypatch.setenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "1")
    assert module._filter_not_yet_downloaded([item], str(tmp_path)) == []


def test_portal_downloadable_review_folder_archive_skip_is_legacy_opt_in(tmp_path, monkeypatch):
    module = _load_action_module()

    class FakeManager:
        def _case_review_folder_has_files(self, case_info):
            return case_info.get("party") == "蘇建和"

    item = {
        "status": "downloadable",
        "party": "蘇建和",
        "case_number": "114年度重上更二字第000095號",
        "rowid": "DL002",
    }

    monkeypatch.delenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", raising=False)
    collapsed = module._collapse_portal_items(
        [item],
        download_folder=str(tmp_path),
        file_review_manager=FakeManager(),
    )

    assert collapsed["downloadable_raw_count"] == 1
    assert collapsed["downloadable_skipped_count"] == 0
    assert collapsed["downloadable_count"] == 1
    assert collapsed["items"] == [item]

    monkeypatch.setenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "1")
    collapsed = module._collapse_portal_items(
        [item],
        download_folder=str(tmp_path),
        file_review_manager=FakeManager(),
    )

    assert collapsed["downloadable_skipped_count"] == 1
    assert collapsed["downloadable_count"] == 0
    assert collapsed["items"] == []


def test_portal_pending_payment_skips_when_review_files_already_archived(tmp_path):
    module = _load_action_module()
    item = {
        "status": "pending_payment",
        "paystatus": "0",
        "status_name": "法院回覆同意",
        "result_text": "請於【115/05/11 上午】以後至法院領取",
        "row_text": "待繳費，請於法院回覆結果下載繳費單繳費",
        "party": "鑫源企業社",
        "court_case_no": "114年度建字第000016號",
        "case_number": "114.建.000016",
        "rowid": "1059435",
        "payid": "31001961342172",
        "archived_review_files": True,
    }

    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))

    assert collapsed["pending_payment_count"] == 0
    assert collapsed["count"] == 0
    assert collapsed["items"] == []


def test_portal_pending_payment_uses_manager_archive_lookup_fields(tmp_path):
    module = _load_action_module()
    seen = {}

    class FakeManager:
        def _case_review_folder_has_files(self, case_info):
            seen.update(case_info)
            return (
                case_info.get("showyyidno") == "114年度建字第000016號"
                and case_info.get("clnm") == "鑫源企業社"
            )

    item = {
        "status": "pending_payment",
        "paystatus": "0",
        "status_name": "法院回覆同意",
        "row_text": "待繳費，請於法院回覆結果下載繳費單繳費",
        "party": "鑫源企業社",
        "court_case_no": "114年度建字第000016號",
        "case_number": "114.建.000016",
        "rowid": "1059435",
    }

    collapsed = module._collapse_portal_items(
        [item],
        download_folder=str(tmp_path),
        file_review_manager=FakeManager(),
    )

    assert seen["showyyidno"] == "114年度建字第000016號"
    assert seen["clnm"] == "鑫源企業社"
    assert seen["yyidno"] == "114.建.000016"
    assert collapsed["pending_payment_count"] == 0
    assert collapsed["items"] == []


def test_portal_pending_payment_skips_when_payment_registry_has_case_token(tmp_path):
    module = _load_action_module()
    (tmp_path / "payment_registry.json").write_text(
        json.dumps(
            {
                "case:115原交易21:林建豐": {
                    "case_number": "115.原交易.000021",
                    "yyidno": "115.原交易.000021",
                    "party": "林建豐",
                    "files": ["繳費單_林建豐_115.原交易.000021.pdf"],
                    "processed_at": "2026-06-09T14:56:58",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "row_text": "待繳費，請於法院回覆結果下載繳費單繳費",
        "party": "林建豐",
        "court_case_no": "115年度原交易字第000021號",
        "case_number": "115.原交易.000021",
        "rowid": "1075000",
    }

    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))

    assert collapsed["pending_payment_count"] == 0
    assert collapsed["count"] == 0
    assert collapsed["items"] == []


def test_file_review_manager_court_pickup_row_is_not_pending_payment():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    row_json = {
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "鑫源企業社請至本院閱覽紙本卷宗，不另製發繳費單。",
        "clnm": "鑫源企業社",
        "yyidno": "115聲123",
    }

    assert FileReviewManager._is_court_pickup_row(row_json, "") is True
    assert FileReviewManager._is_pending_payment_row(row_json, "") is False


def test_file_review_manager_pending_payment_wins_over_online_download_marker():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    row_json = {
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "請將聲請複製電子卷證費用新台幣200元整於待繳費連結繳費後，書記官確認後會將可進行【線上下載】。",
        "clnm": "林建豐",
        "yyidno": "115.原交易.000021",
    }

    assert (
        FileReviewManager._classify_portal_row_status(
            row_json,
            row_text="線上下載",
            has_download=True,
        )
        == "pending_payment"
    )


def test_general_review_download_payment_print_guard_defaults_off(monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    monkeypatch.delenv("MAGI_FILE_REVIEW_GENERAL_DOWNLOAD_PRINT_PAYMENT_SLIPS", raising=False)

    assert FileReviewManager._allow_payment_slip_print_during_general_download() is False


def test_general_review_download_payment_print_guard_can_be_enabled(monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    monkeypatch.setenv("MAGI_FILE_REVIEW_GENERAL_DOWNLOAD_PRINT_PAYMENT_SLIPS", "true")

    assert FileReviewManager._allow_payment_slip_print_during_general_download() is True


def test_check_and_download_available_guards_general_payment_slip_prints():
    source = (
        Path(__file__).resolve().parents[1]
        / "casper_ecosystem"
        / "law_firm_orchestrators"
        / "file_review_automation.py"
    ).read_text(encoding="utf-8")
    block = source.split("    def check_and_download_available", 1)[1].split(
        "    def _handle_download_popup", 1
    )[0]

    assert "MAGI_FILE_REVIEW_GENERAL_DOWNLOAD_PRINT_PAYMENT_SLIPS" in block
    assert "allow_payment_slip_print = self._allow_payment_slip_print_during_general_download()" in block
    assert block.count("if not allow_payment_slip_print:") >= 2
    assert block.count("一般閱卷下載不自動列印繳費單") >= 2


def test_file_review_manager_court_pickup_wins_over_online_download_marker():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    row_json = {
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "請至本院閱覽紙本卷宗，不另製發繳費單。",
        "clnm": "鑫源企業社",
        "yyidno": "115聲123",
    }

    assert (
        FileReviewManager._classify_portal_row_status(
            row_json,
            row_text="線上下載",
            has_download=True,
        )
        == "court_pickup"
    )


def test_file_review_manager_waiting_or_denied_rows_are_not_court_pickup():
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    waiting = {
        "status": "2",
        "statusnm": "待法院回覆",
        "result": "尚未回覆",
    }
    denied = {
        "status": "4",
        "statusnm": "法院回覆不同意",
        "result": "不同意聲請，原因【已到院閱卷】",
    }
    completed = {
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "已到院閱卷",
    }

    assert FileReviewManager._is_court_pickup_row(waiting, "聲請閱卷") is False
    assert FileReviewManager._is_court_pickup_row(denied, "") is False
    assert FileReviewManager._is_court_pickup_row(completed, "") is False


def test_payment_check_notice_stays_quiet_when_portal_has_no_pending_payment():
    module = _load_action_module()

    assert module._should_emit_payment_check_notice(
        pay_hits=7,
        pay_notified=0,
        portal_pending=0,
        portal_pending_changed=True,
        portal_probe_ok=True,
    ) is False


def test_payment_check_notice_emits_for_real_or_unverified_payment_work():
    module = _load_action_module()

    assert module._should_emit_payment_check_notice(
        pay_hits=0,
        pay_notified=0,
        portal_pending=2,
        portal_pending_changed=True,
        portal_probe_ok=True,
    ) is True
    assert module._should_emit_payment_check_notice(
        pay_hits=1,
        pay_notified=0,
        portal_pending=0,
        portal_pending_changed=False,
        portal_probe_ok=False,
    ) is True
    assert module._should_emit_payment_check_notice(
        pay_hits=0,
        pay_notified=1,
        portal_pending=0,
        portal_pending_changed=False,
        portal_probe_ok=True,
    ) is True


def test_review_check_notice_ignores_download_button_until_archived():
    module = _load_action_module()

    assert module._should_emit_review_check_notice(
        download_email_hits=0,
        pickup_email_hits=0,
        ready_to_download_count=0,
        portal_downloadable=1,
        portal_downloadable_changed=True,
        portal_pickup=0,
        portal_pickup_changed=False,
        scan_errors=0,
        portal_failure_alert=False,
    ) is False


def test_review_check_notice_ignores_download_email_until_archived():
    module = _load_action_module()

    assert module._should_emit_review_check_notice(
        download_email_hits=1,
        pickup_email_hits=0,
        ready_to_download_count=1,
        portal_downloadable=0,
        portal_downloadable_changed=False,
        portal_pickup=0,
        portal_pickup_changed=False,
        scan_errors=0,
        portal_failure_alert=False,
    ) is False


def test_review_check_notice_emits_for_pickup_and_health_issues():
    module = _load_action_module()

    assert module._should_emit_review_check_notice(
        download_email_hits=0,
        pickup_email_hits=1,
        ready_to_download_count=0,
        portal_downloadable=0,
        portal_downloadable_changed=False,
        portal_pickup=0,
        portal_pickup_changed=False,
        scan_errors=0,
        portal_failure_alert=False,
    ) is True
    assert module._should_emit_review_check_notice(
        download_email_hits=0,
        pickup_email_hits=0,
        ready_to_download_count=0,
        portal_downloadable=0,
        portal_downloadable_changed=False,
        portal_pickup=0,
        portal_pickup_changed=False,
        scan_errors=1,
        portal_failure_alert=False,
    ) is True


def test_download_notice_email_is_not_processed_until_download_archive(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Messages:
        def __init__(self, message):
            self.message = message

        def list(self, **kwargs):
            return _Exec({"messages": [{"id": "msg-download"}]})

        def get(self, **kwargs):
            return _Exec(self.message)

    class _Users:
        def __init__(self, message):
            self.message = message

        def messages(self):
            return _Messages(self.message)

    class _Gmail:
        def __init__(self, message):
            self.message = message

        def users(self):
            return _Users(self.message)

    body = "法院完成線上交付核閱通知，可線上下載。案號：115年度原交易字第21號。"
    message = {
        "payload": {
            "headers": [
                {"name": "Subject", "value": "法院完成線上交付核閱通知 115年度原交易字第21號"},
                {"name": "From", "value": "noreply@judicial.gov.tw"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")},
        }
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = _Gmail(message)

    result = mgr._scan_and_process_emails("線上下載", "download")

    assert result["hits"] == 1
    assert len(mgr.ready_to_download) == 1
    assert "msg-download" not in mgr.processed_emails
    assert not (tmp_path / "processed_emails.json").exists()


def test_process_emails_dedupes_same_message_across_queries(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Messages:
        def __init__(self, message):
            self.message = message

        def list(self, **kwargs):
            return _Exec({"messages": [{"id": "msg-payment"}]})

        def get(self, **kwargs):
            return _Exec(self.message)

    class _Users:
        def __init__(self, message):
            self.message = message

        def messages(self):
            return _Messages(self.message)

    class _Gmail:
        def __init__(self, message):
            self.message = message

        def users(self):
            return _Users(self.message)

    body = "法院回覆閱卷聲請結果通知（含繳費單）。案號：115年度原交易字第21號。繳費期限：2026/07/01。附件：繳費單。"
    message = {
        "payload": {
            "headers": [
                {"name": "Subject", "value": "法院回覆閱卷聲請結果通知（含繳費單）"},
                {"name": "From", "value": "noreply@judicial.gov.tw"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")},
        }
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = _Gmail(message)
    downloads = []
    monkeypatch.setattr(
        mgr,
        "_download_email_attachments",
        lambda msg_id, message=None: downloads.append(msg_id) or [str(tmp_path / "payment.pdf")],
    )
    monkeypatch.setattr(mgr, "notify_payment_needed", lambda info: False)

    result = mgr.process_emails()

    assert result["payment_hits"] == 1
    assert downloads == ["msg-payment"]
    assert "msg-payment" not in mgr.processed_emails


def test_process_emails_detects_nested_payment_attachment_from_subject_and_filename(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Attachments:
        def get(self, **kwargs):
            assert kwargs["id"] == "att-payment"
            return _Exec({"data": base64.urlsafe_b64encode(b"%PDF-1.4\nnested payment\n").decode("ascii")})

    class _Messages:
        def __init__(self, message):
            self.message = message

        def list(self, **kwargs):
            return _Exec({"messages": [{"id": "msg-nested-payment"}]})

        def get(self, **kwargs):
            return _Exec(self.message)

        def attachments(self):
            return _Attachments()

    class _Users:
        def __init__(self, message):
            self.message = message

        def messages(self):
            return _Messages(self.message)

    class _Gmail:
        def __init__(self, message):
            self.message = message

        def users(self):
            return _Users(self.message)

    html = """
        <table>
          <tr><td>對象法院</td><td>臺灣臺東地方法院</td></tr>
          <tr><td>當事人</td><td>林建豐</td></tr>
          <tr><td>案號</td><td>115.原交易.000021</td></tr>
          <tr><td>繳費期限</td><td>2026/07/01</td></tr>
        </table>
    """
    message = {
        "snippet": "法院回覆結果，附件為規費繳款單。",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "法院回覆閱卷聲請結果通知（含繳費單）"},
                {"name": "From", "value": "noreply@judicial.gov.tw"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {"data": base64.urlsafe_b64encode(html.encode("utf-8")).decode("ascii")},
                        }
                    ],
                },
                {
                    "filename": "繳費單_林建豐_115.原交易.000021.pdf",
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": "att-payment"},
                },
            ],
        },
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = _Gmail(message)
    notified = []
    monkeypatch.setattr(mgr, "notify_payment_needed", lambda info: notified.append(info) or True)

    result = mgr.process_emails()

    assert result["payment_hits"] == 1
    assert result["payment_notified"] == 1
    assert "msg-nested-payment" in mgr.processed_emails
    assert notified and notified[0].court_case_no == "115年度原交易字第21號"
    assert notified[0].client_name == "林建豐"
    assert Path(notified[0].files[0]).name == "繳費單_林建豐_115.原交易.000021.pdf"


def test_process_emails_text_only_gmail_payment_notice_does_not_block_pdf_dedup(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    assert FileReviewManager._is_payment_notice_text("線 上 列 印 繳 費 單 通 知 信") is True

    class _Exec:
        def __init__(self, data):
            self.data = data

        def execute(self):
            return self.data

    class _Messages:
        def __init__(self, message):
            self.message = message

        def list(self, **kwargs):
            return _Exec({"messages": [{"id": "msg-gmail-text-payment"}]})

        def get(self, **kwargs):
            return _Exec(self.message)

    class _Users:
        def __init__(self, message):
            self.message = message

        def messages(self):
            return _Messages(self.message)

    class _Gmail:
        def __init__(self, message):
            self.message = message

        def users(self):
            return _Users(self.message)

    body = "對象法院 臺灣高等法院 當事人 李秀花 聲請方式 複製電子卷證 案號 115.原聲再.000002 案由 違反公職人員選罷法 回覆內容 請至待繳費下載繳費單"
    message = {
        "snippet": "請至待繳費下載繳費單",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "法院回覆閱卷聲請結果通知（含繳費單）"},
                {"name": "From", "value": "noreply@judicial.gov.tw"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")},
        },
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = _Gmail(message)
    monkeypatch.setattr(mgr, "_download_email_attachments", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        mgr,
        "notify_payment_needed",
        lambda _info: (_ for _ in ()).throw(AssertionError("Gmail notice without PDF must use portal download first")),
    )
    portal_seen = []

    def fake_portal_download():
        portal_seen.extend((item.case_number, list(item.source_message_ids)) for item in mgr.pending_payment_notices)
        for item in mgr.pending_payment_notices:
            for msg_id in item.source_message_ids:
                mgr.processed_emails.add(msg_id)
        return {"attempted": 1, "downloaded": 1, "notified": 1, "errors": []}

    monkeypatch.setattr(mgr, "_download_queued_payment_notices_from_portal", fake_portal_download)

    result = mgr.process_emails()

    assert result["payment_hits"] == 1
    assert result["payment_notified"] == 1
    assert result["payment_portal_attempts"] == 1
    assert result["payment_portal_downloaded"] == 1
    assert result["payment_portal_notified"] == 1
    assert "msg-gmail-text-payment" in mgr.processed_emails
    assert portal_seen == [("115年度原聲再字第2號", ["msg-gmail-text-payment"])]
    assert "web_payment:115年度原聲再字第2號" not in mgr.notified_cases


def test_download_email_attachments_reuses_same_payload_file(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    payload = b"%PDF-1.4\nsame payment slip\n"
    message = {
        "payload": {
            "parts": [
                {
                    "filename": "259420307417.pdf",
                    "mimeType": "application/octet-stream",
                    "body": {
                        "data": base64.urlsafe_b64encode(payload).decode("ascii"),
                    },
                }
            ]
        }
    }
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.gmail_service = object()

    first = mgr._download_email_attachments("msg-payment", message)
    second = mgr._download_email_attachments("msg-payment", message)

    assert first == second
    assert first == [str(tmp_path / "259420307417.pdf")]
    assert sorted(p.name for p in tmp_path.glob("259420307417*.pdf")) == ["259420307417.pdf"]


def test_portal_notify_state_can_record_zero_pending_without_notification(tmp_path):
    module = _load_action_module()
    state_path = tmp_path / ".portal_notify_state.json"

    module._save_portal_notify_state(
        str(state_path),
        portal_downloadable=6,
        portal_pickup=29,
        portal_pending=0,
    )

    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["portal_downloadable"] == 6
    assert data["portal_court_pickup"] == 29
    assert data["portal_pending"] == 0


def test_recent_activity_fingerprint_ignores_processed_at_for_same_download():
    module = _load_action_module()
    base = {
        "source": "download_job",
        "artifact_type": "review_download",
        "party": "林建豐",
        "case_number": "115年度原交易字第21號",
        "detail": "已下載卷宗（2 份）",
        "count": 2,
    }

    first = dict(base, processed_at="2026-06-23T10:00:00")
    second = dict(base, processed_at="2026-06-23T10:05:00")

    assert module._recent_activity_fingerprint(first) == module._recent_activity_fingerprint(second)


def test_payment_pdf_notification_uses_file_caption_without_duplicate_text_push(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewInfo,
        FileReviewManager,
    )

    pdf = tmp_path / "441403005422.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        court="臺灣花蓮地方法院",
        client_name="林建豐",
        court_case_no="115年度原交易字第21號",
        status="待繳費",
        payment_deadline="2026-06-30",
        files=[str(pdf)],
    )

    sent_files = []

    class FakeNotifier:
        def notify_admin_with_files(self, text, file_paths, **kwargs):
            sent_files.append((text, file_paths, kwargs))
            return True

        def notify_admin(self, *_args, **_kwargs):
            raise AssertionError("text fallback should not run after file delivery")

    def fail_text_push(*_args, **_kwargs):
        raise AssertionError("red_phone text push should not run before successful file delivery")

    fake_red_phone = types.SimpleNamespace(
        send_telegram_push_with_status=fail_text_push,
        send_discord_bot_file=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("discord file fallback should not run after file delivery")
        ),
    )
    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_dedup_db = types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.dedup_db", fake_dedup_db)
    monkeypatch.setattr(
        "casper_ecosystem.law_firm_orchestrators.line_notifier.LAFNotifier",
        lambda: FakeNotifier(),
    )

    assert mgr.notify_payment_needed(info) is True

    assert len(sent_files) == 1
    text, file_paths, kwargs = sent_files[0]
    assert text.startswith("💰 繳費單通知")
    assert "林建豐 - 115年度原交易字第21號" in text
    assert len(file_paths) == 1
    sent_path = Path(file_paths[0])
    assert sent_path != pdf
    assert sent_path.name == "繳費單_林建豐_115年度原交易字第21號.pdf"
    assert sent_path.read_bytes() == pdf.read_bytes()
    assert kwargs["topic_key"] == "filereview_payment"


def test_notify_payment_needed_skips_uploaded_payment_proof_even_with_pdf(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewInfo,
        FileReviewManager,
    )

    pdf = tmp_path / "441403005422.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "payment_proof_registry.json").write_text(
        json.dumps(
            {
                "115.上訴.003543": {
                    "uploaded_at": "2026-06-30T14:32:49",
                    "court_code": "TPH",
                    "file": "discord_d528d01e2e9b4c6b8b8567bb2f25e38b.png",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        court="臺灣高等法院",
        client_name="游秀鈴",
        court_case_no="115年度上訴字第3543號",
        status="待繳費",
        payment_deadline="2026-07-02",
        files=[str(pdf)],
    )

    class FakeNotifier:
        def notify_admin_with_files(self, *_args, **_kwargs):
            raise AssertionError("paid payment slip must not be resent")

        def notify_admin(self, *_args, **_kwargs):
            raise AssertionError("paid payment slip must not send fallback text")

    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_red_phone = types.SimpleNamespace(
        send_telegram_push_with_status=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paid payment slip must not send red_phone text")
        ),
        send_discord_bot_file=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paid payment slip must not send Discord fallback")
        ),
    )
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )

    assert mgr.notify_payment_needed(info) is None


def test_payment_slip_download_sends_files_without_duplicate_summary_text(tmp_path, monkeypatch):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    text_notifications = []
    file_notifications = []

    class FakeManager:
        def __init__(self, **_kwargs):
            pass

        def login(self):
            return True

        def navigate_to_file_review(self):
            return None

        def download_all_payment_slips(self, max_days=14, target_case_number=None):
            return [
                {
                    "party": "凡江",
                    "case_number": "115年度原交易字第21號",
                    "all_paths": [str(pdf)],
                }
            ]

        def close(self):
            return None

    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(module, "_ensure_imports", lambda: types.SimpleNamespace(FileReviewManager=FakeManager))
    monkeypatch.setattr(module, "_is_valid_payment_pdf_file", lambda path: path == str(pdf))
    monkeypatch.setattr(module, "_notify", lambda *args, **kwargs: text_notifications.append((args, kwargs)))
    monkeypatch.setattr(module, "_notify_file", lambda *args, **kwargs: file_notifications.append((args, kwargs)) or True)
    monkeypatch.setattr(module, "_mark_payment_file_delivered", lambda *args, **kwargs: None)

    result = module.cmd_download_payment_slips(notify=True)

    assert result["success"] is True
    assert result["count"] == 1
    assert text_notifications == []
    assert len(file_notifications) == 1
    args, kwargs = file_notifications[0]
    sent_path = Path(args[0])
    assert sent_path != pdf
    assert sent_path.name == "繳費單_凡江_115年度原交易字第21號.pdf"
    assert sent_path.read_bytes() == pdf.read_bytes()
    assert kwargs["topic_key"] == "filereview_payment"
    assert kwargs["caption"].startswith("💰 繳費單 PDF 下載完成")


def test_payment_slip_download_stays_quiet_when_no_pending_slips(tmp_path, monkeypatch):
    module = _load_action_module()
    text_notifications = []
    file_notifications = []

    class FakeManager:
        def __init__(self, **_kwargs):
            pass

        def login(self):
            return True

        def navigate_to_file_review(self):
            return None

        def download_all_payment_slips(self, max_days=14, target_case_number=None):
            return []

        def close(self):
            return None

    monkeypatch.setattr(module, "_load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "_get_credentials",
        lambda _cfg: {
            "username": "u",
            "password": "p",
            "download_folder": str(tmp_path),
        },
    )
    monkeypatch.setattr(module, "_get_db_manager", lambda _cfg: None)
    monkeypatch.setattr(module, "_ensure_imports", lambda: types.SimpleNamespace(FileReviewManager=FakeManager))
    monkeypatch.setattr(module, "_notify", lambda *args, **kwargs: text_notifications.append((args, kwargs)))
    monkeypatch.setattr(module, "_notify_file", lambda *args, **kwargs: file_notifications.append((args, kwargs)) or True)

    result = module.cmd_download_payment_slips(notify=True)

    assert result["success"] is True
    assert result["count"] == 0
    assert result["sent"] == 0
    assert result["failed"] == 0
    assert result["delivery"]["suppressed_noop"] is True
    assert text_notifications == []
    assert file_notifications == []


def test_payment_pdf_delivery_failure_is_pending_not_delivered(tmp_path, monkeypatch):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(module, "_notify_file", lambda *args, **kwargs: {"ok": False, "errors": ["tg_down"]})

    result = module._send_payment_pdf_files(
        [str(pdf)],
        download_folder=str(tmp_path),
        caption_prefix="💰 繳費單 PDF 下載完成",
        notify=True,
    )

    assert result["sent"] == 0
    assert result["failed"] == 1
    assert module._payment_file_already_delivered(str(pdf), str(tmp_path)) is False
    state = json.loads((tmp_path / ".payment_pdf_delivery_state.json").read_text(encoding="utf-8"))
    assert state["sent_files"] == {}
    assert len(state["pending_files"]) == 1
    pending = next(iter(state["pending_files"].values()))
    assert pending["attempts"] == 1
    assert pending["last_error"] == "notify_file_returned_false"


def test_existing_payment_pdf_skips_when_notice_key_already_seen(tmp_path, monkeypatch):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_林建豐_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "notified_cases.json").write_text(
        json.dumps(
            {"web_payment:case:115原交易21:林建豐": "2026-05-26T14:01:34"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(module, "_notify_file", lambda *args, **kwargs: calls.append((args, kwargs)) or True)

    result = module._send_payment_pdf_files(
        [str(pdf)],
        download_folder=str(tmp_path),
        caption_prefix="💰 繳費單 PDF 下載完成",
        notify=True,
        notice_keys_by_path={str(pdf): ["web_payment:case:115原交易21:林建豐"]},
    )

    assert result["sent"] == 0
    assert result["failed"] == 0
    assert result["notice_seen"] == 1
    assert calls == []


def test_notify_file_rejects_false_status_dict_and_uses_fallback(tmp_path, monkeypatch):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    fallback_calls = []

    class FakeNotifier:
        def notify_admin_with_files(self, *_args, **_kwargs):
            return {"ok": False, "delivered": False}

    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_red_phone = types.SimpleNamespace(
        send_file_admin=lambda *_args, **_kwargs: {"ok": False, "errors": ["tg_down"]},
        send_discord_bot_file=lambda *args, **kwargs: fallback_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)

    assert module._notify_file(str(pdf), caption="test", topic_key="filereview_payment") is True
    assert len(fallback_calls) == 1


def test_scheduled_check_runs_payment_scan_before_download(monkeypatch):
    module = _load_action_module()
    calls = []
    download_env = {}

    monkeypatch.setenv("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "1")
    monkeypatch.setenv("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP", "1")
    monkeypatch.setattr(module, "cmd_check_emails", lambda **kwargs: calls.append("check_emails") or {"success": True})
    monkeypatch.setattr(
        module,
        "cmd_download_payment_slips",
        lambda **kwargs: calls.append("download_payment_slips") or {"success": True, "delivery": {"sent": 0, "failed": 0}},
    )

    def fake_download_background(**kwargs):
        calls.append("download")
        download_env["case_level"] = module.os.environ.get("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP")
        download_env["button_level"] = module.os.environ.get("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP")
        return {"success": True, "queued": True}

    monkeypatch.setattr(module, "cmd_download_background", fake_download_background)

    result = module.cmd_scheduled_check(notify=True)

    assert result["success"] is True
    assert calls == ["check_emails", "download_payment_slips", "download"]
    assert download_env == {"case_level": "0", "button_level": "0"}
    assert module.os.environ.get("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP") == "1"
    assert module.os.environ.get("MAGI_ENABLE_BUTTON_LEVEL_DOWNLOAD_SKIP") == "1"


def test_file_review_check_summaries_are_quiet_cron_only():
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert '_notify(section_msg, True, topic_key="quiet_cron")' in source
    assert "effective_topic = section_topic" not in source


def test_file_review_cron_uses_complete_scheduled_check():
    source = (MODULE_PATH.parent.parent.parent / "scripts" / "seed_cron_jobs.py").read_text(encoding="utf-8")

    assert '"id": "job_file_review_check"' in source
    assert '"scheduled_check"' in source
    assert '"--task", "download")' not in source


def _roc_compact(days_from_now: int = 3) -> str:
    dt = datetime.now() + timedelta(days=days_from_now)
    return f"{dt.year - 1911:03d}{dt.month:02d}{dt.day:02d}"


def test_processed_payment_registry_suppresses_old_pdf_resend(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    pdf = tmp_path / "繳費單_吳志炳_114.原交易.000049.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "995588",
        "yyidno": "114.原交易.000049",
        "showyyidno": "114年度原交易字第000049號",
        "clnm": "吳志炳",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }
    mgr.payment_registry = {
        "rowid:995588": {
            "processed_at": "2026-04-10T14:04:02",
            "yyidno": "114.原交易.000049",
            "case_number": "114.原交易.000049",
            "rowid": "995588",
            "party": "吳志炳",
            "files": [pdf.name],
            "file_paths": [str(pdf)],
        }
    }

    with patch.object(mgr, "notify_payment_needed", side_effect=AssertionError("must not resend old PDF")):
        assert mgr._notify_payment_if_needed(row, case_info={"party": "吳志炳"}, file_paths=None) is True

    saved = json.loads((tmp_path / "notified_cases.json").read_text(encoding="utf-8"))
    assert "web_payment:case:114原交易49:吳志炳" in saved


def test_payment_notice_requires_actual_pdf_before_dedup(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "lin-row",
        "yyidno": "115.原交易.000021",
        "showyyidno": "115年度原交易字第21號",
        "clnm": "林建豐",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    assert mgr._notify_payment_if_needed(row, case_info={"party": "林建豐"}, file_paths=[]) is False
    notified_path = tmp_path / "notified_cases.json"
    if notified_path.exists():
        saved = json.loads(notified_path.read_text(encoding="utf-8"))
        assert "web_payment:115年度原交易字第21號" not in saved
        assert "web_payment:case:115原交易21:林建豐" not in saved


def test_recent_payment_activity_retries_when_pdf_not_delivered(tmp_path):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_林建豐_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    record = {
        "processed_at": datetime.now(),
        "party": "林建豐",
        "case_number": "115年度原交易字第000021號",
        "detail": "已下載繳費單（1 份）",
        "count": 1,
        "artifact_type": "payment_slip",
        "source": "payment_registry",
        "key": "rowid:1075000",
        "file_paths": [str(pdf)],
    }
    fp = module._recent_activity_fingerprint(record)
    (tmp_path / ".recent_activity_notified.json").write_text(
        json.dumps({
            "version": 1,
            "recent_payment_activity": {fp: "2026-06-22T10:30:41"},
            "recent_review_download_activity": {},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    fresh = module._filter_unnotified_recent_activity(
        [record],
        str(tmp_path),
        "recent_payment_activity",
    )

    assert fresh == [record]


def test_recent_payment_activity_skips_uploaded_payment_proof(tmp_path, monkeypatch):
    module = _load_action_module()
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )
    pdf = tmp_path / "441403005422.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "payment_proof_registry.json").write_text(
        json.dumps(
            {
                "115.上訴.003543": {
                    "uploaded_at": "2026-06-30T14:32:49",
                    "court_code": "TPH",
                    "file": "discord_d528d01e2e9b4c6b8b8567bb2f25e38b.png",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "payment_registry.json").write_text(
        json.dumps(
            {
                "case:115上訴3543": {
                    "processed_at": datetime.now().isoformat(),
                    "case_number": "115.上訴.003543",
                    "party": "游秀鈴",
                    "file_paths": [str(pdf)],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert module._load_recent_payment_activity(str(tmp_path), days=7) == []

    monkeypatch.setattr(
        module,
        "_payment_pdf_text",
        lambda _path: "規費繳款單\n案 號：115 年 上訴 字 003543 號\n應繳款人：喬政翔",
    )

    assert module._load_recent_unregistered_payment_pdfs(str(tmp_path), days=2) == []


def test_payment_notice_rejects_pdf_extension_with_non_pdf_payload(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    bad_pdf = tmp_path / "繳費單_梁志祥_114.交上易.000014.pdf"
    bad_pdf.write_text('{"messageText":"銷帳編號取號失敗"}', encoding="utf-8")
    ignored_dir = tmp_path / "_ignored_downloads" / "20260622"
    ignored_dir.mkdir(parents=True)
    quarantined_pdf = ignored_dir / "繳費單_梁志祥_114.交上易.000014.invalid_artifact.pdf"
    quarantined_pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "liang-row",
        "yyidno": "114.交上易.000014",
        "showyyidno": "114年度交上易字第000014號",
        "clnm": "梁志祥",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    with patch.object(mgr, "notify_payment_needed", side_effect=AssertionError("must not notify invalid PDF")):
        assert mgr._notify_payment_if_needed(
            row,
            case_info={"party": "梁志祥", "case_number": "114.交上易.000014"},
            file_paths=[str(bad_pdf)],
        ) is False
        assert mgr._notify_payment_if_needed(
            row,
            case_info={"party": "梁志祥", "case_number": "114.交上易.000014"},
            file_paths=[str(quarantined_pdf)],
        ) is False

    module = _load_action_module()
    assert module._is_valid_payment_pdf_file(str(quarantined_pdf)) is False

    notified_path = tmp_path / "notified_cases.json"
    if notified_path.exists():
        saved = json.loads(notified_path.read_text(encoding="utf-8"))
        assert "web_payment:case:114交上易14:梁志祥" not in saved


def test_legacy_unpadded_payment_notice_key_suppresses_padded_case(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    (tmp_path / "notified_cases.json").write_text(
        json.dumps({"web_payment:114年度交上易字第14號": "2026-06-10T16:08:26"}, ensure_ascii=False),
        encoding="utf-8",
    )
    pdf = tmp_path / "繳費單_梁志祥_114.交上易.000014.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    row = {
        "rowid": "1086357",
        "yyidno": "114.交上易.000014",
        "showyyidno": "114年度交上易字第000014號",
        "clnm": "梁志祥",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    assert "web_payment:case:114交上易14" in mgr.notified_cases
    with patch.object(mgr, "notify_payment_needed", side_effect=AssertionError("must not resend legacy-notified case")):
        assert mgr._notify_payment_if_needed(
            row,
            case_info={"party": "梁志祥", "case_number": "114.交上易.000014"},
            file_paths=[str(pdf)],
        ) is True


def test_notify_payment_needed_without_pdf_is_not_delivery(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewInfo, FileReviewManager

    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        client_name="林建豐",
        court="臺灣臺東地方法院",
        court_case_no="115年度原交易字第999999號",
        status="待繳費",
        payment_deadline="",
        files=[],
    )

    assert mgr.notify_payment_needed(info) is False


def test_gmail_payment_notice_downloads_portal_pdf_before_marking_email_processed(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewInfo, FileReviewManager

    pdf = tmp_path / "繳費單_李秀花_115年度原聲再字第2號.pdf"
    pdf.write_bytes(b"%PDF-1.4\nportal payment slip\n")
    marked = []
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(
            is_done=lambda *_args, **_kwargs: False,
            mark_done=lambda category, key, metadata=None: marked.append((category, key, metadata)) or True,
        ),
    )

    mgr = FileReviewManager(username="u", password="p", download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        client_name="李秀花",
        court="臺灣高等法院花蓮分院",
        court_case_no="115年度原聲再字第2號",
        status="待繳費",
        payment_deadline="",
        files=[],
        message_id="msg-portal-payment",
        source_message_ids=["msg-portal-payment"],
    )
    assert mgr._queue_pending_payment_notice(info) is True
    monkeypatch.setattr(mgr, "login", lambda: setattr(mgr, "logged_in", True) or True)
    monkeypatch.setattr(mgr, "navigate_to_file_review", lambda: True)
    download_calls = []
    monkeypatch.setattr(
        mgr,
        "download_all_payment_slips",
        lambda **kwargs: download_calls.append(kwargs) or [{
            "case_number": "115年度原聲再字第2號",
            "party": "李秀花",
            "court": "臺灣高等法院花蓮分院",
            "rowid": "row-1",
            "payid": "pay-1",
            "pdf_path": str(pdf),
            "all_paths": [str(pdf)],
        }],
    )
    delivered = []
    monkeypatch.setattr(mgr, "notify_payment_needed", lambda notice: delivered.append(notice) or True)

    stats = mgr._download_queued_payment_notices_from_portal()

    assert stats["attempted"] == 1
    assert stats["downloaded"] == 1
    assert stats["notified"] == 1
    assert "msg-portal-payment" in mgr.processed_emails
    assert download_calls == [{"max_days": 14, "target_case_number": "115年度原聲再字第2號"}]
    assert delivered and delivered[0].files == [str(pdf)]
    assert any(key.startswith("web_payment:case:115原聲再2") for _cat, key, _meta in marked)


def test_payment_slip_download_uses_review_list_frame_helper(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    class _Driver:
        def implicitly_wait(self, _timeout):
            return None

        def find_element(self, *_args, **_kwargs):
            raise AssertionError("download_all_payment_slips should use _open_review_list_v1")

        def execute_script(self, script, *_args):
            if "function getRowJson" in str(script):
                return []
            return None

    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    mgr.driver = _Driver()
    opened = []
    monkeypatch.setattr(mgr, "_open_review_list_v1", lambda: opened.append(True) or True)

    result = mgr.download_all_payment_slips(max_days=14, target_case_number="113年度訴字第253號")

    assert result == []
    assert opened == [True]


def test_notify_payment_needed_does_not_treat_queued_text_as_delivered(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewInfo,
        FileReviewManager,
    )

    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        client_name="凡江",
        court="臺灣花蓮地方法院",
        court_case_no="115年度原交易字第21號",
        status="待繳費",
        payment_deadline="2026-07-01",
        files=[str(pdf)],
    )

    class FakeNotifier:
        def notify_admin_with_files(self, *_args, **_kwargs):
            return False

    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_red_phone = types.SimpleNamespace(
        send_telegram_push_with_status=lambda *_args, **_kwargs: {
            "telegram": False,
            "delivered": False,
            "queued": True,
            "outbox_id": "queued-1",
        }
    )
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )

    assert mgr.notify_payment_needed(info) is False


def test_notify_payment_needed_respects_suppress_notify(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
        FileReviewInfo,
        FileReviewManager,
    )

    pdf = tmp_path / "繳費單_凡江_115.原交易.000021.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    mgr = FileReviewManager(download_folder=str(tmp_path), headless=True)
    info = FileReviewInfo(
        client_name="凡江",
        court="臺灣花蓮地方法院",
        court_case_no="115年度原交易字第21號",
        status="待繳費",
        payment_deadline="2026-07-01",
        files=[str(pdf)],
    )

    class FakeNotifier:
        def notify_admin_with_files(self, *_args, **_kwargs):
            raise AssertionError("suppressed notification should not send files")

        def notify_admin(self, *_args, **_kwargs):
            raise AssertionError("suppressed notification should not send text")

    fake_line_notifier = types.SimpleNamespace(LAFNotifier=lambda: FakeNotifier())
    fake_red_phone = types.SimpleNamespace(
        send_telegram_push_with_status=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("suppressed notification should not use red_phone")
        ),
        send_discord_bot_file=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("suppressed notification should not use Discord fallback")
        ),
    )
    monkeypatch.setenv("MAGI_FILE_REVIEW_SUPPRESS_NOTIFY", "1")
    monkeypatch.setitem(sys.modules, "line_notifier", fake_line_notifier)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_red_phone)
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(is_done=lambda *_args, **_kwargs: False),
    )

    assert mgr.notify_payment_needed(info) is False


def test_archived_payment_slip_suppresses_repeat_download_and_reseeds_registry(tmp_path):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    case_folder = tmp_path / "2025-0133-吳志炳-一審-公共危險"
    review_folder = case_folder / "02_閱卷資料" / "20260515"
    review_folder.mkdir(parents=True)
    pdf = review_folder / "繳費單_吳志炳_114.原交易.000049.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    mgr = FileReviewManager(download_folder=str(tmp_path / "downloads"), headless=True)
    mgr._resolve_case_folder = lambda info: str(case_folder)

    row = {
        "rowid": "fresh-rowid",
        "yyidno": "114.原交易.000049",
        "showyyidno": "114年度原交易字第000049號",
        "clnm": "吳志炳",
        "paylimitdt": _roc_compact(3),
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    assert mgr._is_payment_processed(row) is True
    entry = mgr.payment_registry["rowid:fresh-rowid"]
    assert entry["file_paths"] == [str(pdf)]
    assert entry["party"] == "吳志炳"


def test_portal_pending_payment_skips_legacy_notified_case(tmp_path):
    module = _load_action_module()
    (tmp_path / "notified_cases.json").write_text(
        json.dumps({"web_payment:114年度原交易字第000049號": "2026-04-10T14:04:02"}, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "待繳費",
        "party": "吳志炳",
        "court_case_no": "114年度原交易字第000049號",
        "pay_deadline": _roc_compact(3),
    }

    groups = module._filter_urgent_pending_payments(
        [item],
        days=14,
        download_folder=str(tmp_path),
    )

    assert groups == {"overdue": [], "urgent": [], "unknown": []}


def test_portal_pending_payment_skips_legacy_unpadded_notified_case(tmp_path):
    module = _load_action_module()
    (tmp_path / "notified_cases.json").write_text(
        json.dumps({"web_payment:114年度交上易字第14號": "2026-06-10T16:08:26"}, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "待繳費",
        "party": "梁志祥",
        "court_case_no": "114年度交上易字第000014號",
        "case_number": "114.交上易.000014",
        "pay_deadline": _roc_compact(3),
    }

    groups = module._filter_urgent_pending_payments(
        [item],
        days=14,
        download_folder=str(tmp_path),
    )

    assert groups == {"overdue": [], "urgent": [], "unknown": []}


def test_portal_pending_payment_skips_payment_registry_case(tmp_path):
    module = _load_action_module()
    pdf = tmp_path / "繳費單_吳志炳_114.原交易.000049.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "payment_registry.json").write_text(
        json.dumps({
            "rowid:995588": {
                "case_number": "114.原交易.000049",
                "party": "吳志炳",
                "files": [pdf.name],
                "file_paths": [str(pdf)],
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    item = {
        "status": "pending_payment",
        "paystatus": "2",
        "status_name": "法院回覆同意",
        "result_text": "待繳費",
        "party": "吳志炳",
        "court_case_no": "114年度原交易字第000049號",
        "pay_deadline": _roc_compact(3),
    }

    groups = module._filter_urgent_pending_payments(
        [item],
        days=14,
        download_folder=str(tmp_path),
    )

    assert groups == {"overdue": [], "urgent": [], "unknown": []}


def test_payment_download_error_is_retryable_after_cooldown(tmp_path, monkeypatch):
    from casper_ecosystem.law_firm_orchestrators.file_review_automation import FileReviewManager

    mgr = FileReviewManager(download_folder=str(tmp_path / "downloads"), headless=True)
    monkeypatch.setattr(mgr, "_find_existing_payment_slip_files", lambda row_json: [])
    monkeypatch.setenv("MAGI_EEFILE_PAYMENT_ERROR_COOLDOWN_HOURS", "0")
    row = {
        "rowid": "retry-row",
        "yyidno": "115.原交易.000021",
        "showyyidno": "115年度原交易字第000021號",
        "clnm": "喬○翔",
        "paystatus": "2",
        "status": "3",
        "statusnm": "法院回覆同意",
        "result": "待繳費",
    }

    mgr._mark_payment_download_error(row, reason="payment_slip_download_no_pdf", files=[])

    assert mgr.payment_registry["rowid:retry-row"]["status"] == "invalid_download_cooldown"
    assert mgr._is_payment_processed(row) is False


def test_file_review_archive_success_does_not_also_stage():
    source = (
        Path(__file__).resolve().parents[1]
        / "casper_ecosystem"
        / "law_firm_orchestrators"
        / "file_review_automation.py"
    ).read_text(encoding="utf-8")
    loop = source.split("for fp in remaining_files:", 1)[1].split("# stage", 1)[0]

    assert 'if res.get("ok"):' in loop
    assert "continue" in loop
