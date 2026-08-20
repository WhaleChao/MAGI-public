from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from api.domains.judgment_value_filter import SKIP_SUMMARY, classify_judgment_record
from api.domains.judicial_api_backlog import (
    build_backlog_interpretation,
    format_backlog_notice,
)


ROOT = Path(__file__).resolve().parents[1]


def _load_judgment_module(tmp_path: Path):
    os.environ["JUDGMENT_CACHE_ROOT"] = str(tmp_path / "cache")
    shared = tmp_path / "shared"
    os.environ["MAGI_SHARED_STATE_DIR"] = str(shared)
    os.environ["MAGI_JUDGMENTS_JSON_PATH"] = str(
        shared / "agent" / "judgment-collector" / "judgments.json"
    )
    name = "judgment_collector_quality_test"
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "skills" / "judgment-collector" / "action.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_judicial_archive_module():
    name = "judicial_archive_error_provenance_test"
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "skills" / "judicial-flow-search-archive" / "action.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_case_reason_does_not_rescue_pure_fee_order():
    decision = classify_judgment_record(
        jid="TPDV,115,補,123,20260728,1",
        court_name="臺灣臺北地方法院",
        case_number="115年度補字第123號",
        case_reason="侵權行為",
        title="裁定",
        full_text=(
            "臺灣臺北地方法院民事裁定\n"
            "原告應於本裁定送達後五日內補繳裁判費，逾期不繳即駁回其訴。"
        ),
    )
    assert decision.disposition == SKIP_SUMMARY
    assert decision.reason == "fee_or_correction_order"


def test_daily_crawl_distinguishes_upstream_outage_from_program_failure(tmp_path):
    module = _load_judgment_module(tmp_path)

    assert module._all_daily_failures_are_retryable_upstream(
        [
            {"error": "judgment search failed: HTTP Error 500: INTERNAL SERVER ERROR"},
            {"error": "upstream returned 503 Service Unavailable"},
        ]
    )
    assert not module._all_daily_failures_are_retryable_upstream(
        [{"error": "TypeError: parser contract changed"}]
    )


def test_skill_wrapper_failure_preserves_inner_http_error(tmp_path):
    module = _load_judgment_module(tmp_path)
    parsed = module._parse_skill_output(
        {
            "success": False,
            "error": "Action execution failed",
            "trace": [
                {
                    "rc": 1,
                    "stdout": (
                        '{"success":false,"error":"judicial search failed",'
                        '"detail":"http 403"}'
                    ),
                    "stderr": "",
                }
            ],
        }
    )
    assert parsed["success"] is False
    assert "http 403" in parsed["error"]
    assert module._all_daily_failures_are_retryable_upstream(
        [{"error": parsed["error"]}]
    )

    archive = _load_judicial_archive_module()
    archive_parsed = archive._parse_skill_output(
        {
            "success": False,
            "error": "Action execution failed",
            "trace": [
                {
                    "rc": 1,
                    "stdout": (
                        '{"success":false,"error":"judicial search failed",'
                        '"detail":"http 403"}'
                    ),
                    "stderr": "",
                }
            ],
        }
    )
    assert archive_parsed["success"] is False
    assert "http 403" in archive_parsed["error"]


def test_extractive_summary_rejects_fact_fragment_and_keeps_grounded_reason(tmp_path):
    module = _load_judgment_module(tmp_path)
    fact_only = (
        "臺灣臺北地方法院刑事判決\n主文\n被告有罪。\n理由\n"
        "經查被告於民國一百十五年一月一日到場，證人亦到場陳述。"
    )
    assert module._extractive_judgment_summary(fact_only, "傷害") == ""

    reason = (
        "臺灣臺北地方法院民事判決\n主文\n原告之訴駁回。\n事實及理由\n"
        "按民法第184條規定，侵權行為之成立，應以行為人有故意或過失、"
        "權利受侵害及二者間具有相當因果關係為要件。"
        "本院認為原告就損害與被告行為間之因果關係，依民事訴訟法第277條規定"
        "應負舉證責任，原告未提出足以證明之資料，故其請求不得准許。"
        "中華民國115年7月28日"
    )
    summary = module._extractive_judgment_summary(reason, "侵權行為")
    assert "## 實務見解" in summary
    assert "因果關係" in summary
    assert module._summary_practical_value_failure(
        summary, reason, "侵權行為"
    ) == ""


def test_backlog_notice_distinguishes_round_result_from_project_completion():
    interpretation = build_backlog_interpretation(
        backlog_before=677,
        backlog_remaining=437,
        handled=240,
        db_upserts=127,
        archive_upserts=127,
        summarized=48,
        usable_summaries=11,
        rejected_summaries=37,
        vector_ingested=0,
        summary_mode="extractive",
        vector_enabled=False,
        oldest_age_hours=6.6,
        newest_age_hours=6.5,
        skipped_low_value=90,
        skipped_missing_text=18,
        max_docs=240,
        runs_per_day=5,
    )
    notice = format_backlog_notice(
        "司法院裁判資料整理：本輪結果", interpretation
    )
    assert "本輪已讀取 240 件" in notice
    assert "摘要嘗試 48、通過 11、淘汰 37" in notice
    assert "不是向量服務故障" in notice
    assert "不等於通譯專案" not in notice
    assert "白天整理完成" not in notice


def test_day_process_fails_closed_before_marking_files_when_db_is_unavailable(
    tmp_path,
):
    module = _load_judgment_module(tmp_path)
    raw = tmp_path / "raw.json"
    raw.write_text('{"payload":{"JID":"TEST"}}', encoding="utf-8")
    saved = []
    module._iter_jdg_raw_files = lambda: [str(raw)]
    module._load_json_file = lambda *args, **kwargs: {"processed": {}}
    module._jdg_backlog_status = lambda processed: {
        "backlog_count": 1,
        "oldest_backlog_age_hours": 0,
        "newest_backlog_age_hours": 0,
        "raw_total": 1,
        "unreadable_count": 0,
    }
    module._save_json_file = lambda *args, **kwargs: saved.append(args)
    module._get_db = lambda: None

    result = module.official_api_day_process(force=True, notify=False)

    assert result["success"] is False
    assert result["error"] == "database_unavailable"
    assert result["handled"] == 0
    assert saved == []
