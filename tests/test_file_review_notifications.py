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
    with patch("builtins.print"), patch(
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
