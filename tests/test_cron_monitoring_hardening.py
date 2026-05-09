from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest


def _cron_jobs_text_or_skip() -> str:
    path = Path("cron_jobs.json")
    if not path.exists():
        pytest.skip("cron_jobs.json is local runtime state and is not present in clean CI checkouts")
    return path.read_text(encoding="utf-8")


def _cron_jobs_or_skip() -> list[dict]:
    return json.loads(_cron_jobs_text_or_skip())


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


def test_nightly_health_report_honors_top_level_skipped_steps(tmp_path, monkeypatch):
    import scripts.nightly_health_report as report
    from datetime import datetime

    today = datetime.now().strftime("%Y%m%d")
    run_dir = tmp_path / f"{today}_220000_nightly"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "task": "nightly",
                "ok": True,
                "details": {
                    "steps": {
                        "judicial_api_night_pull": {
                            "ok": False,
                            "skipped": True,
                            "reason": "disabled_by_operator",
                        }
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(report, "AUTOPILOT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(report, "DELIVERY_LOG", str(tmp_path / "missing.jsonl"))

    text = report.generate_report()
    assert "⏭️ 司法院 API 夜間拉取：disabled_by_operator" in text
    assert "有 1 個步驟失敗" not in text


def test_nightly_health_report_reclassifies_local_backup_mode_db_steps(tmp_path, monkeypatch):
    import scripts.nightly_health_report as report
    from datetime import datetime

    today = datetime.now().strftime("%Y%m%d")
    run_dir = tmp_path / f"{today}_220001_nightly"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "task": "nightly",
                "ok": True,
                "details": {
                    "steps": {
                        "db_bidirectional_sync": {
                            "ok": False,
                            "parsed": {"ok": False, "error": "remote unavailable"},
                        },
                        "db_daily_backup": {
                            "ok": False,
                            "parsed": {
                                "ok": False,
                                "target": "both",
                                "items": [{"ok": True, "path": "/tmp/db.sql.gz"}],
                                "errors": ["local: db unreachable"],
                            },
                        },
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MAGI_ENABLE_DB_BIDIR_SYNC", "0")
    monkeypatch.setattr(report, "AUTOPILOT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(report, "DELIVERY_LOG", str(tmp_path / "missing.jsonl"))

    text = report.generate_report()
    assert "⏭️ DB 雙向同步：目前採本機備份模式" in text
    assert "✅ DB 每日備份：已有 DB 備份檔落地" in text
    assert "有 1 個步驟失敗" not in text


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


def test_nightly_db_defaults_are_local_backup_without_bidir_sync():
    source = Path("skills/magi-autopilot/action.py").read_text(encoding="utf-8")
    defaults = source[source.index("MAGI_ENABLE_DB_BIDIR_SYNC") - 80 : source.index("# Nightly 可以做較完整")]

    assert 'os.environ.setdefault("MAGI_ENABLE_DB_BIDIR_SYNC", "0")' in defaults
    assert 'os.environ.setdefault("MAGI_ENABLE_DB_DAILY_BACKUP", "1")' in defaults
    assert 'os.environ.setdefault("MAGI_DB_BACKUP_TARGET", "local")' in defaults
    assert 'os.environ.get("MAGI_ENABLE_DB_BIDIR_SYNC", "0")' in source
    assert 'os.environ.get("MAGI_DB_BACKUP_TARGET", "local")' in source


def test_cron_uses_repo_omlx_switch_and_single_health_report_time():
    jobs = _cron_jobs_text_or_skip()
    parsed_jobs = json.loads(jobs)
    by_id = {job["id"]: job for job in parsed_jobs}

    assert "/Users/ai/Library/Application Support/MAGI/bin/omlx_switch_model.sh" not in jobs
    assert "/Users/ai/Desktop/MAGI_v2/config/bin/omlx_switch_model.sh" in jobs
    assert '"id": "job_health_report"' in jobs
    assert '"cron": "30 6 * * *"' in jobs
    assert by_id["job_omlx_profile_guard"]["cron"] == "*/15 * * * *"
    assert "omlx_switch_model.sh auto" in by_id["job_omlx_profile_guard"]["command"]
    assert by_id["job_omlx_profile_guard"]["timeout_sec"] >= 1800
    assert by_id["job_distill_train_gemma"]["enabled"] is True
    assert "validation-gated" in by_id["job_distill_train_gemma"]["desc"]
    assert "MAGI_PDF_NAMER_DOCLING_ENABLED=1" in by_id["pdfnamer_docling_layout"]["command"]


def test_omlx_auto_switch_checks_real_api_model_and_2150_boundary():
    source = Path("config/bin/omlx_switch_model.sh").read_text(encoding="utf-8")

    assert "current_total_min" in source
    assert '"$current_total_min" -lt 1310' in source
    assert "current_model_api" in source
    assert "127.0.0.1:8080/v1/models" in source
    assert 'echo "$current_model_api" | grep -qi "$EXPECTED_MODEL_KEYWORD"' in source
    assert 'launchctl enable "gui/$UID_NUM/com.magi.omlx"' in source
    assert "wait_model_ready 8080 \"e4b\"" in source
    assert "wait_model_ready 8080 \"26b\"" in source


def test_daemon_self_heals_omlx_profile_without_active_profile_lie():
    source = Path("daemon.py").read_text(encoding="utf-8")
    reviewer_block = source[source.index("# 2.55 oMLX 三哲人審查員") : source.index("# 2.6 OpenClaw cron bridge")]

    assert "def _ensure_omlx_time_profile_async" in source
    assert "omlx_switch_model.sh" in source
    assert '"auto"' in source
    assert "if not _is_omlx_night_window():" in reviewer_block
    assert "active_profile" not in reviewer_block


def test_omlx_restore_installer_uses_canonical_repo_switch():
    source = Path("scripts/install_omlx_restore.py").read_text(encoding="utf-8")

    assert "LABEL = \"com.magi.omlx-restore\"" in source
    assert "config\" / \"bin\" / \"omlx_switch_model.sh\"" in source
    assert "Application Support\" / \"MAGI\" / \"bin\" / \"omlx_switch_model.sh\"" not in source
    assert "sleep 90 && exec" in source
    assert "run_with_env.py" in source


def test_judicial_daytime_cron_batches_are_bounded():
    jobs = _cron_jobs_or_skip()
    by_id = {job["id"]: job for job in jobs}
    expected_caps = {
        "job_judicial_api_morning": (200, 80, 7200),
        "job_judicial_api_noon": (220, 120, 7200),
        "job_judicial_api_afternoon": (220, 120, 7200),
        "job_judicial_api_evening": (180, 80, 7200),
        "job_judicial_api_backlog_clear": (80, 30, 1800),
    }

    for job_id, (max_docs, summarize_max, timeout_sec) in expected_caps.items():
        job = by_id[job_id]
        match = re.search(r"official_api_day_process (\{.*?\})'", job["command"])
        assert match, job_id
        payload = json.loads(match.group(1).replace(r"\"", '"'))
        assert payload["max_docs"] == max_docs
        assert payload["summarize_max"] == summarize_max
        assert job["timeout_sec"] == timeout_sec


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
