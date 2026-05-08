"""Regression tests for file-review notification aggregation."""

from __future__ import annotations

import importlib.util
import json
import sys
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


def test_recent_activity_backlog_is_seeded_then_only_new_items_surface(tmp_path):
    module = _load_action_module()
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
    }

    assert module._portal_item_is_court_pickup_ready(item) is True
    assert module._portal_item_is_actionable_pending(item) is False

    collapsed = module._collapse_portal_items([item], download_folder=str(tmp_path))

    assert collapsed["court_pickup_count"] == 1
    assert collapsed["pending_payment_count"] == 0
    assert collapsed["items"][0]["status"] == "court_pickup"


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

    assert FileReviewManager._is_court_pickup_row(waiting, "聲請閱卷") is False
    assert FileReviewManager._is_court_pickup_row(denied, "") is False
