from __future__ import annotations

import importlib.util
import os
import sys
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(tmp_path: Path):
    os.environ["JUDGMENT_CACHE_ROOT"] = str(tmp_path / "cache")
    os.environ["MAGI_SHARED_STATE_DIR"] = str(tmp_path / "shared")
    name = "judgment_collector_gap_fill_rc634"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "skills" / "judgment-collector" / "action.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OFFICIAL_TEXT = (
    "臺灣臺北地方法院民事判決\n主文：原告之訴駁回。\n"
    "按民法第184條規定，侵權行為之成立，應以行為人有故意或過失、"
    "權利受侵害及二者間具有相當因果關係為要件。\n"
    "本院認為原告依民事訴訟法第277條應就損害與行為間之因果關係"
    "負舉證責任；本件未提出足以證明相當因果關係之資料，故其請求不得准許。\n"
    "中華民國115年1月1日\n法官 王小明"
)


class _Connection:
    def close(self):
        return None


def test_cached_jdoc_failures_are_not_misreported_as_completed(tmp_path):
    module = _load(tmp_path)
    assert module._jdg_raw_wrapper_state('{"payload":{"JID":"x"}}') == "success"
    assert module._jdg_raw_wrapper_state('{"payload":{"error":"查無資料"}}') == "removed"
    assert module._jdg_raw_wrapper_state('{"payload":{"error":"服務暫時失敗"}}') == "failed"
    assert module._jdg_raw_wrapper_state("not-json") == "invalid"


def test_pipeline_reports_source_pull_debt_separately_from_local_backlog(tmp_path):
    from scripts.ops.check_judicial_api_pipeline import latest_pull_summary

    path = tmp_path / "pull_state.json"
    path.write_text(json.dumps({"runs": [
        {
            "ts": "2026-08-22T01:00:00+08:00",
            "source_listed_count": 4252,
            "source_completed_count": 1200,
            "source_remaining_count": 3052,
        },
        {"ts": "2026-08-21T01:00:00+08:00", "source_remaining_count": 3652},
    ]}), encoding="utf-8")
    report = latest_pull_summary(path)
    assert report["source_listed_count"] == 4252
    assert report["source_completed_count"] == 1200
    assert report["source_remaining_count"] == 3052
    assert report["previous_source_remaining_count"] == 3652


def test_existing_daily_crawl_schedule_enables_bounded_mcp_gap_fill_without_new_job():
    from scripts.seed_cron_jobs import deterministic_legacy_replacements

    jobs = {row["id"]: row for row in deterministic_legacy_replacements(ROOT, ROOT / "venv/bin/python3")}
    command = jobs["job_1770705679"]["command"]
    assert "JUDGMENT_MCP_GAP_FILL_ENABLE=1" in command
    assert "JUDGMENT_MCP_GAP_MAX_RESULTS_PER=5" in command
    assert "JUDGMENT_MCP_GAP_TIME_BUDGET_SEC=480" in command
    assert "job_judgment_mcp_official_gap_fill" not in jobs


def test_dispatch_policy_binds_the_reviewed_single_job_cron_source():
    policy = json.loads(
        (ROOT / "config" / "v3_schedule_dispatch_policy.json").read_text(encoding="utf-8")
    )
    assert policy["cron_jobs_sha256"] == (
        "8c584cb38cf21a4df5d15b3da47dccbcc43b4d57b1ec20e4bbd7362eaacf539b"
    )


def test_mcp_gap_fill_stores_only_strict_official_fulltext_and_rebuilds_summary(tmp_path, monkeypatch):
    module = _load(tmp_path)
    monkeypatch.setattr(module, "_scan_active_cases", lambda max_cases=0: [
        {"name": "案件", "db_case_reason": "侵權行為", "db_case_type": "民事"}
    ])
    monkeypatch.setattr(module, "_ensure_court_judgments_table", lambda _conn: None)
    stored = []
    monkeypatch.setattr(module, "_upsert_court_judgment_by_jid", lambda _conn, **kwargs: stored.append(kwargs) or True)
    jid = "TPD,115,訴,12,20260101,1"

    def search(query, **kwargs):
        assert query == "侵權行為"
        assert kwargs["fulltext_limit"] == 5
        return {
            "success": True,
            "items": [
                {
                    "jid": jid,
                    "title": "臺灣臺北地方法院115年度訴字第12號",
                    "court": "臺灣臺北地方法院",
                    "case_reason": "侵權行為",
                    "judgment_date": "民國115年1月1日",
                    "full_text": OFFICIAL_TEXT,
                    "summary_full": "供應商摘要不得直接保存",
                    "source_url": "https://judgment.judicial.gov.tw/FJUD/data.aspx?ty=JD&id=TPD%2C115%2C%E8%A8%B4%2C12%2C20260101%2C1&ot=in",
                    "official_origin": True,
                },
                {
                    "jid": "TPD,115,訴,13,20260101,1",
                    "full_text": OFFICIAL_TEXT,
                    "source_url": "https://example.invalid/fake",
                    "official_origin": True,
                },
            ],
        }

    result = module.mcp_official_gap_fill(
        max_reasons=20,
        max_results_per=5,
        search_fn=search,
        connection=_Connection(),
    )
    assert result["success"] is True
    assert result["verified_fulltext_count"] == 1
    assert result["stored_count"] == 1
    assert result["rejected_by_code"] == {"unofficial_source_url": 1}
    assert result["pii_included"] is False
    assert len(stored) == 1
    assert stored[0]["jid"] == jid
    assert stored[0]["full_text"] == OFFICIAL_TEXT
    assert "供應商摘要" not in stored[0]["summary"]
    assert "## 實務見解" in stored[0]["summary"]


def test_mcp_gap_fill_output_never_contains_case_or_jid(tmp_path, monkeypatch):
    module = _load(tmp_path)
    monkeypatch.setattr(module, "_scan_active_cases", lambda max_cases=0: [
        {"name": "2026-0001-王小明-侵權行為", "db_case_reason": "侵權行為", "db_case_type": "民事"}
    ])
    monkeypatch.setattr(module, "_ensure_court_judgments_table", lambda _conn: None)
    monkeypatch.setattr(module, "_upsert_court_judgment_by_jid", lambda *_args, **_kwargs: True)
    result = module.mcp_official_gap_fill(
        search_fn=lambda *_args, **_kwargs: {"success": False},
        connection=_Connection(),
    )
    rendered = str(result)
    assert "王小明" not in rendered
    assert "2026-0001" not in rendered
    assert result["pii_included"] is False
