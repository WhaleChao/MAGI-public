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


def test_nightly_health_report_prefers_nightly_over_later_self_test(tmp_path, monkeypatch):
    import scripts.nightly_health_report as report
    from datetime import datetime

    today = datetime.now().strftime("%Y%m%d")
    nightly_dir = tmp_path / f"{today}_010000_nightly"
    self_test_dir = tmp_path / f"{today}_101710_self_test"
    nightly_dir.mkdir()
    self_test_dir.mkdir()
    (nightly_dir / "report.json").write_text(
        json.dumps(
            {
                "ok": True,
                "details": {
                    "steps": {
                        "pdf_nightly_train": {"ok": True, "parsed": {"message": "trained"}},
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (self_test_dir / "report.json").write_text(
        json.dumps({"task": "self_test", "ok": True, "details": {"db_schema_guard": {"ok": True}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(report, "AUTOPILOT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(report, "DELIVERY_LOG", str(tmp_path / "missing.jsonl"))

    assert report._find_latest_nightly_run() == str(nightly_dir)
    text = report.generate_report()
    assert "PDF 視覺訓練" in text
    assert "無法解析步驟結果" not in text
    assert "無步驟資料可供判定" not in text


def test_nightly_health_report_handles_self_test_without_parse_warning(tmp_path, monkeypatch):
    import scripts.nightly_health_report as report
    from datetime import datetime

    today = datetime.now().strftime("%Y%m%d")
    run_dir = tmp_path / f"{today}_101710_self_test"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps({"task": "self_test", "ok": True, "details": {"db_schema_guard": {"ok": True}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(report, "AUTOPILOT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(report, "DELIVERY_LOG", str(tmp_path / "missing.jsonl"))

    text = report.generate_report()
    assert "self_test" in text
    assert "無法解析步驟結果" not in text
    assert "無步驟資料可供判定" not in text


def test_autopilot_user_active_defer_defined_before_first_call():
    source = Path("skills/magi-autopilot/action.py").read_text(encoding="utf-8")
    run_start = source.index("def run_nightly")
    first_definition = source.index("def _user_active_defer", run_start)
    first_call = source.index('if _user_active_defer("judicial_api_nightly_process")', run_start)

    assert first_definition < first_call


def test_daemon_autopilot_orphan_grace_matches_nightly_timeout():
    source = Path("daemon.py").read_text(encoding="utf-8")

    assert '"MAGI_ORPHAN_GRACE_AUTOPILOT_SEC", "21600"' in source
    assert '"skills/magi-autopilot/action.py"' in source


def test_daemon_force_reaper_still_respects_worker_grace():
    source = Path("daemon.py").read_text(encoding="utf-8")
    phase3 = source[source.index("if _is_worker_cmd(cmd):") : source.index("# Phase 4: Stale")]

    assert "force only widens PPID matching" in phase3
    assert "if etimes < _grace_for_cmd(cmd):" in phase3
    assert "if (not force) and etimes < _grace_for_cmd(cmd):" not in phase3


def test_autopilot_sigterm_waits_for_kill_reason_file():
    source = Path("skills/magi-autopilot/action.py").read_text(encoding="utf-8")
    read_reason = source[source.index("def _read_kill_reason") : source.index("def _term_handler")]

    assert "for _ in range(5):" in read_reason
    assert "time.sleep(0.1)" in read_reason


def test_single_machine_policy_skips_distributed_probe_paths():
    brain = Path("skills/brain_manager/action.py").read_text(encoding="utf-8")
    autopilot = Path("skills/magi-autopilot/action.py").read_text(encoding="utf-8")
    melchior = Path("skills/bridge/melchior_client.py").read_text(encoding="utf-8")

    assert "MAGI_SINGLE_MACHINE" in brain
    assert "if not _distributed_enabled():" in brain
    assert "distributed disabled by MAGI_SINGLE_MACHINE/MAGI_AVOID_DISTRIBUTED" in brain
    assert "single_machine_skipped" in autopilot
    assert "MAGI_SINGLE_MACHINE" in melchior


def test_single_machine_schema_guard_uses_local_osc_env_first():
    source = Path("skills/magi-autopilot/action.py").read_text(encoding="utf-8")
    guard = source[source.index("def _db_schema_chk_nb_guard") : source.index("def _remember_run_event")]

    assert "OSC_ENV_LOCAL" in guard
    assert '"casper_service"' in guard
    assert "Studio_Local,Home_Local_Test,Studio_VPN_Remote" in guard


def test_cron_uses_repo_omlx_switch_and_single_health_report_time():
    jobs = Path("cron_jobs.json").read_text(encoding="utf-8")

    assert "/Users/ai/Library/Application Support/MAGI/bin/omlx_switch_model.sh" not in jobs
    assert "/Users/ai/Desktop/MAGI_v2/config/bin/omlx_switch_model.sh" in jobs
    assert '"id": "job_health_report"' in jobs
    assert '"cron": "30 6 * * *"' in jobs


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
