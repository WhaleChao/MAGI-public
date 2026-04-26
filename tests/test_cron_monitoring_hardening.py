from __future__ import annotations

import importlib
import json
from pathlib import Path


def test_cron_result_policy_suppresses_structured_success_payload():
    from skills.ops.cron_result_policy import should_log_cron_issue

    stdout = json.dumps(
        {
            "success": True,
            "severity": "OK",
            "alarm_triggered": False,
            "free_gb": 78.07,
        },
        ensure_ascii=False,
    )

    assert should_log_cron_issue(255, stdout, "") is False


def test_cron_result_policy_keeps_real_failure():
    from skills.ops.cron_result_policy import should_log_cron_issue

    assert should_log_cron_issue(1, "", "Traceback: boom") is True


def test_nightly_health_report_surfaces_top_level_autopilot_failure(tmp_path, monkeypatch):
    import scripts.nightly_health_report as report
    from datetime import datetime, timedelta

    # 用動態「昨日」目錄名（_find_latest_nightly_run 只認今日/昨日；hard-coded 日期會
    # 在 today 滾過後失效）
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    run_dir = tmp_path / f"{yesterday}_220114_nightly"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "ok": False,
                "summary": "執行失敗（請看 report.json）",
                "details": {
                    "error": "UnboundLocalError: cannot access local variable '_user_active_defer'",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(report, "AUTOPILOT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(report, "DELIVERY_LOG", str(tmp_path / "missing.jsonl"))

    parsed = report._parse_step_results(str(run_dir))
    text = report.generate_report()

    assert parsed["_nightly_run"]["ok"] is False
    assert "夜間主流程" in text
    assert "UnboundLocalError" in text
    assert "無步驟資料可供判定" not in text


def test_autopilot_user_active_defer_defined_before_first_call():
    source = Path("skills/magi-autopilot/action.py").read_text(encoding="utf-8")
    run_start = source.index("def run_nightly")
    first_definition = source.index("def _user_active_defer", run_start)
    first_call = source.index('if _user_active_defer("judicial_api_nightly_process")', run_start)

    assert first_definition < first_call


def test_obsidian_known_malformed_pdf_hints_include_fitz_xref_errors():
    import skills.obsidian.action as action

    action = importlib.reload(action)
    path = Path("bad.pdf")

    assert action._is_known_malformed_pdf_skip(
        path,
        "Syntax Error: Couldn't find trailer dictionary; Couldn't read xref table",
    )
    assert action._is_known_malformed_pdf_skip(
        path,
        "PDF extraction error: Failed to open file '/tmp/bad.pdf'",
    )
