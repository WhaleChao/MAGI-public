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


class _NoProcessRun:
    stdout = ""
    stderr = ""
    returncode = 1


def _silence_process_scan(monkeypatch, audit) -> None:
    monkeypatch.setattr(audit.subprocess, "run", lambda *args, **kwargs: _NoProcessRun())


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


def test_cron_result_policy_does_not_hide_stderr_failure_with_success_json():
    from skills.ops.cron_result_policy import should_log_cron_issue

    stdout = json.dumps({"success": True, "severity": "OK", "alarm_triggered": False})

    assert should_log_cron_issue(1, stdout, "Traceback: boom") is True


def test_operational_audit_ignores_macro_cron_companions(tmp_path, monkeypatch):
    import scripts.ops.audit_operational_hardening as audit

    jobs = [
        {
            "id": "job_worldmonitor_intel",
            "enabled": True,
            "cron": "0 8 * * *",
            "command": "/venv/bin/python skills/worldmonitor-intel/action.py",
        },
        {
            "id": "job_gcal_sync",
            "enabled": True,
            "cron": "0 8 * * *",
            "command": "@MAGI 日曆同步",
        },
    ]
    (tmp_path / "cron_jobs.json").write_text(json.dumps(jobs), encoding="utf-8")

    monkeypatch.setattr(audit, "ROOT", tmp_path)

    report = audit.audit_cron()

    assert report["collision_count"] == 0


def test_operational_audit_flags_duplicate_transcript_indexers(tmp_path, monkeypatch):
    import scripts.ops.audit_operational_hardening as audit

    jobs = [
        {
            "id": "legacy_transcript_index",
            "enabled": True,
            "cron": "0 2 * * *",
            "command": "/venv/bin/python skills/transcript-indexer/action.py --task index",
            "desc": "legacy",
        },
        {
            "id": "job_transcript_indexer",
            "enabled": True,
            "cron": "30 6,21 * * *",
            "command": "/venv/bin/python skills/transcript-indexer/action.py --task index",
            "desc": "canonical",
        },
    ]
    (tmp_path / "cron_jobs.json").write_text(json.dumps(jobs), encoding="utf-8")

    monkeypatch.setattr(audit, "ROOT", tmp_path)
    _silence_process_scan(monkeypatch, audit)

    report = audit.audit_domain_interference()

    assert report["issue_count"] == 1
    assert report["issues"][0]["domain"] == "transcript_indexer"


def test_operational_audit_allows_single_transcript_indexer(tmp_path, monkeypatch):
    import scripts.ops.audit_operational_hardening as audit

    jobs = [
        {
            "id": "legacy_transcript_index",
            "enabled": False,
            "cron": "0 2 * * *",
            "command": "/venv/bin/python skills/transcript-indexer/action.py --task index",
            "desc": "legacy",
        },
        {
            "id": "job_transcript_indexer",
            "enabled": True,
            "cron": "30 6,21 * * *",
            "command": "/venv/bin/python skills/transcript-indexer/action.py --task index",
            "desc": "canonical",
        },
    ]
    (tmp_path / "cron_jobs.json").write_text(json.dumps(jobs), encoding="utf-8")

    monkeypatch.setattr(audit, "ROOT", tmp_path)
    _silence_process_scan(monkeypatch, audit)

    report = audit.audit_domain_interference()

    assert report["ok"] is True
    assert report["issue_count"] == 0


def test_operational_audit_flags_cron_root_mismatch(tmp_path, monkeypatch):
    import scripts.ops.audit_operational_hardening as audit

    jobs = [
        {
            "id": "job_bad_root",
            "enabled": True,
            "cron": "0 * * * *",
            "command": "/Users/ai/Desktop/MAGI_v2/venv/bin/python3 /Users/ai/Desktop/MAGI_v2/scripts/x.py",
            "desc": "bad",
        }
    ]
    (tmp_path / "cron_jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    monkeypatch.setattr(audit, "ROOT", tmp_path)

    report = audit.audit_runtime_root_consistency()

    assert report["ok"] is False
    assert report["mismatch_count"] == 1


def test_operational_audit_flags_stale_runtime_lock(tmp_path, monkeypatch):
    import scripts.ops.audit_operational_hardening as audit

    lock_dir = tmp_path / ".runtime" / "locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "demo.lock").write_text(
        json.dumps({"domain": "demo", "owner": "test", "pid": 99999999}),
        encoding="utf-8",
    )
    (lock_dir / "demo.lock.json").write_text(
        json.dumps({"domain": "demo", "owner": "test", "pid": 99999999}),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.delenv("MAGI_RUNTIME_DIR", raising=False)

    report = audit.audit_stale_runtime_locks()

    assert report["ok"] is False
    assert report["stale_count"] == 1


def test_operational_audit_treats_lock_body_without_sidecar_as_orphaned_anchor(tmp_path, monkeypatch):
    import scripts.ops.audit_operational_hardening as audit

    lock_dir = tmp_path / ".runtime" / "locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "demo.lock").write_text(
        json.dumps({"domain": "demo", "owner": "test", "pid": 99999999}),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.delenv("MAGI_RUNTIME_DIR", raising=False)

    report = audit.audit_stale_runtime_locks()

    assert report["ok"] is True
    assert report["stale_count"] == 0
    assert report["orphaned_anchor_count"] == 1


def test_operational_audit_requires_laf_gmail_fallback_json_out(tmp_path, monkeypatch):
    import scripts.ops.audit_operational_hardening as audit

    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_laf_gmail_dispatch_scan",
                    "enabled": True,
                    "cron": "*/5 * * * *",
                    "command": "python scripts/ops/laf_gmail_dispatch_scan.py --json-out static/laf_gmail_monitor_state.json",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "ROOT", tmp_path)

    report = audit.audit_laf_gmail_fallback_job()

    assert report["ok"] is True


def test_operational_audit_flags_unmanaged_cloudflared_port(tmp_path, monkeypatch):
    import scripts.ops.audit_operational_hardening as audit

    (tmp_path / "cron_jobs.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.delenv("MAGI_ENABLE_CLOUDFLARE_WEBHOOK", raising=False)

    class _Proc:
        stdout = "40654 /opt/homebrew/bin/cloudflared tunnel --url http://127.0.0.1:5002 --no-autoupdate\n"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(audit.subprocess, "run", lambda *args, **kwargs: _Proc())

    report = audit.audit_domain_interference()

    assert report["issue_count"] == 1
    assert report["issues"][0]["domain"] == "cloudflare_quick_tunnel"
    assert report["issues"][0]["processes"][0]["port"] == "5002"


def test_startup_prefers_stable_webhook_base_and_proxy_port(monkeypatch):
    import api.startup as startup

    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://aimac-mini.example.ts.net")
    monkeypatch.setenv("MAGI_LINE_WEBHOOK_ENDPOINT", "https://temporary-host.trycloudflare.com/line/webhook")
    monkeypatch.setenv("MAGI_SERVER_PORT", "5002")
    monkeypatch.delenv("MAGI_WEBHOOK_PROXY_PORT", raising=False)
    monkeypatch.delenv("MAGI_TAILSCALE_PORT", raising=False)

    assert startup._stable_webhook_base_url() == "https://aimac-mini.example.ts.net/"
    assert startup._magi_webhook_port() == "18790"

    monkeypatch.setenv("MAGI_WEBHOOK_PROXY_PORT", "18791")
    assert startup._magi_webhook_port() == "18791"


def test_startup_stops_only_unmanaged_cloudflared_ports(monkeypatch):
    import api.startup as startup

    calls = []

    class _Proc:
        stdout = "\n".join(
            [
                "40654 /opt/homebrew/bin/cloudflared tunnel --url http://127.0.0.1:5002 --no-autoupdate",
                "11353 /opt/homebrew/bin/cloudflared tunnel --url http://127.0.0.1:5014 --no-autoupdate",
            ]
        )
        stderr = ""
        returncode = 0

    def _fake_run(argv, *args, **kwargs):
        calls.append(argv)
        if argv[:2] == ["pgrep", "-fl"]:
            return _Proc()

        class _Killed:
            stdout = ""
            stderr = ""
            returncode = 0

        return _Killed()

    monkeypatch.setattr(startup.subprocess, "run", _fake_run)

    startup._stop_unmanaged_cloudflared_tunnels(allowed_ports={"5014"})

    assert ["kill", "40654"] in calls
    assert ["kill", "11353"] not in calls


def test_discord_line_self_heal_prefers_stable_webhook_before_quick_tunnel():
    source = Path("api/discord_bot.py").read_text(encoding="utf-8")

    stable_pos = source.index("_stable_webhook_base_url")
    tunnel_pos = source.index("cloudflared tunnel --url")

    assert stable_pos < tunnel_pos
    assert "MAGI_ENABLE_CLOUDFLARE_WEBHOOK" in source


def test_operational_audit_treats_runtime_cache_as_generated(monkeypatch):
    import scripts.ops.audit_operational_hardening as audit

    class _Proc:
        stdout = "\n".join(
            [
                " M json/processed_laf_emails.json",
                " M skills/pdf-namer/db_rules_cache.json",
                " M static/knowledge_lint_latest.json",
                " M static/translator_ape_latest.json",
            ]
        )
        stderr = ""

    monkeypatch.setattr(audit.subprocess, "run", lambda *args, **kwargs: _Proc())

    report = audit.audit_git()

    assert report["dirty_count"] == 0
    assert report["generated_or_runtime_count"] == 4


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


def test_nightly_health_report_explains_resource_guard_skip(tmp_path, monkeypatch):
    import scripts.nightly_health_report as report
    import time

    guard_log = tmp_path / "resource_guarded_run.jsonl"
    guard_log.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "job_id": "job_nightly_autopilot",
                "blocked": True,
                "block_reasons": ["resource_level>=throttle:throttle"],
                "decision": {
                    "level": "throttle",
                    "snapshot": {
                        "disk_free_gb": 39.37,
                        "disk_total_gb": 460.43,
                        "swap_used_gb": 15.57,
                        "free_plus_inactive_gb": 7.73,
                    },
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    cron_state = tmp_path / "cron_state.json"
    cron_state.write_text(
        json.dumps({"job_nightly_autopilot": {"last_run": "2026-05-16T22:00:53"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(report, "AUTOPILOT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(report, "DELIVERY_LOG", str(tmp_path / "missing.jsonl"))
    monkeypatch.setattr(report, "RESOURCE_GUARD_LOG", str(guard_log))
    monkeypatch.setattr(report, "CRON_STATE_PATH", str(cron_state))

    text = report.generate_report()

    assert "資源守門" in text
    assert "level=throttle" in text
    assert "resource_level>=throttle:throttle" in text
    assert "磁碟可用 39.37/460.43GB" in text
    assert "夜間主流程已由資源守門略過" in text
    assert "夜間任務可能未執行" not in text


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
    expected_switch = str(Path.cwd() / "config" / "bin" / "omlx_switch_model.sh")

    assert "/Users/ai/Library/Application Support/MAGI/bin/omlx_switch_model.sh" not in jobs
    assert expected_switch in jobs
    assert '"id": "job_health_report"' in jobs
    assert '"cron": "30 6 * * *"' in jobs
    assert by_id["job_omlx_profile_guard"]["cron"] == "*/15 * * * *"
    assert "omlx_switch_model.sh" in by_id["job_omlx_profile_guard"]["command"]
    assert by_id["job_omlx_profile_guard"]["command"].endswith(" auto")
    assert by_id["job_omlx_profile_guard"]["timeout_sec"] >= 1800
    assert by_id["job_distill_train_gemma"]["enabled"] is True
    assert "validation-gated" in by_id["job_distill_train_gemma"]["desc"]
    assert "MAGI_PDF_NAMER_DOCLING_ENABLED=1" in by_id["pdfnamer_docling_layout"]["command"]


def test_seed_cron_jobs_installs_disk_maintenance_jobs(tmp_path):
    import scripts.seed_cron_jobs as seed

    result = seed.seed_jobs(tmp_path, python_path=tmp_path / "venv" / "bin" / "python3")
    jobs = json.loads((tmp_path / "cron_jobs.json").read_text(encoding="utf-8"))
    by_id = {job["id"]: job for job in jobs}

    assert result["ok"] is True
    assert by_id["job_disk_low_water_alarm"]["enabled"] is True
    assert "disk_low_water_alarm.py" in by_id["job_disk_low_water_alarm"]["command"]
    assert by_id["job_empty_case_shell_cleanup"]["enabled"] is True
    assert by_id["job_empty_case_shell_cleanup"]["cron"] == "8,23,38,53 * * * *"
    assert by_id["job_empty_case_shell_cleanup"]["no_catchup"] is True
    assert "--max-seconds 240" in by_id["job_empty_case_shell_cleanup"]["command"]
    assert "cleanup_synology_empty_case_shells.py" in by_id["job_empty_case_shell_cleanup"]["command"]
    assert by_id["job_slow_archive_closed_cases"]["enabled"] is True
    assert by_id["job_slow_archive_closed_cases"]["no_catchup"] is True
    assert by_id["job_slow_archive_closed_cases"]["cron"] == "40 5 * * *"
    assert "start_slow_archive_closed_cases.py" in by_id["job_slow_archive_closed_cases"]["command"]
    assert "--limit 3" in by_id["job_slow_archive_closed_cases"]["command"]
    assert "--min-size-mb 0" in by_id["job_slow_archive_closed_cases"]["command"]
    assert "--bwlimit-mbps 80" in by_id["job_slow_archive_closed_cases"]["command"]
    assert "--rsync-timeout-sec 600" in by_id["job_slow_archive_closed_cases"]["command"]
    assert "--case-number 2025-0002" not in by_id["job_slow_archive_closed_cases"]["command"]
    assert by_id["job_drive_case_sync_bidirectional"]["enabled"] is True
    assert "--matched-case-limit 24" in by_id["job_drive_case_sync_bidirectional"]["command"]
    assert "--download-limit 80" in by_id["job_drive_case_sync_bidirectional"]["command"]
    assert "--upload-limit 80" in by_id["job_drive_case_sync_bidirectional"]["command"]
    assert "--max-download-bytes 1500000000" in by_id["job_drive_case_sync_bidirectional"]["command"]
    assert by_id["job_drive_case_sync_all_files"]["enabled"] is True
    assert by_id["job_drive_case_sync_all_files"]["cron"] == "12 1,7,13,19 * * *"
    assert "--direct-all-case-limit 96" in by_id["job_drive_case_sync_all_files"]["command"]
    assert "--download-limit 240" in by_id["job_drive_case_sync_all_files"]["command"]
    assert "--upload-limit 240" in by_id["job_drive_case_sync_all_files"]["command"]
    assert "--max-download-bytes 3000000000" in by_id["job_drive_case_sync_all_files"]["command"]
    from api.platforms.safe_process import parse_cron_command

    all_files_argv = parse_cron_command(by_id["job_drive_case_sync_all_files"]["command"])
    token_gate_idx = next(
        idx for idx, arg in enumerate(all_files_argv) if str(arg).endswith("run_after_token_refresh.py")
    )
    token_gate_separator_idx = all_files_argv.index("--", token_gate_idx)
    assert all_files_argv[token_gate_separator_idx + 1] == str(tmp_path / "venv" / "bin" / "python3")
    assert all_files_argv[token_gate_separator_idx + 2].endswith("run_with_env.py")
    assert all_files_argv[token_gate_separator_idx + 3] == "MAGI_DRIVE_SYNC_LOCAL_SCAN_TIMEOUT_SEC=8"
    assert all_files_argv[token_gate_separator_idx + 4] == "MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC=20"
    assert by_id["job_disk_cleanup_healthcheck"]["no_catchup"] is True
    assert "MAGI_DISK_CLEANUP_DRY_RUN=0" in by_id["job_disk_cleanup_healthcheck"]["command"]
    assert by_id["job_nas_recycle_heavy_cleanup"]["cron"] == "20 4 * * *"
    assert by_id["job_nas_recycle_heavy_cleanup"]["no_catchup"] is True
    assert "MAGI_DISK_NAS_RECYCLE_HEAVY_ENABLE=1" in by_id["job_nas_recycle_heavy_cleanup"]["command"]
    assert "weekly_cache_cleanup.py" in by_id["job_weekly_cache_cleanup"]["command"]
    assert by_id["job_reboot_before_day_model_switch"]["enabled"] is False
    assert "scheduled_reboot_guard.py" in by_id["job_reboot_before_day_model_switch"]["command"]
    assert by_id["job_reboot_before_night_model_switch"]["enabled"] is False
    assert "MAGI_ALLOW_SCHEDULED_REBOOT=1" in by_id["job_reboot_before_night_model_switch"]["command"]
    assert by_id["job_nightly_bookmark_regex"]["enabled"] is True
    assert by_id["job_nightly_bookmark_regex"]["no_catchup"] is True
    assert "--enqueue-ocr-followups" in by_id["job_nightly_bookmark_regex"]["command"]
    assert by_id["job_pdf_bookmark_label_repair"]["enabled"] is True
    assert by_id["job_pdf_bookmark_label_repair"]["no_catchup"] is True
    assert by_id["job_pdf_bookmark_label_repair"]["cron"] == "35 4 * * *"
    assert "repair_pdf_bookmark_labels.py" in by_id["job_pdf_bookmark_label_repair"]["command"]
    assert "--apply --limit 12" in by_id["job_pdf_bookmark_label_repair"]["command"]
    assert "--per-file-timeout 90" in by_id["job_pdf_bookmark_label_repair"]["command"]
    assert "--max-file-mb 80" in by_id["job_pdf_bookmark_label_repair"]["command"]
    assert by_id["job_pdf_bookmark_large_volume_repair"]["enabled"] is True
    assert by_id["job_pdf_bookmark_large_volume_repair"]["no_catchup"] is True
    assert by_id["job_pdf_bookmark_large_volume_repair"]["cron"] == "55 4 * * *"
    assert "repair_pdf_bookmark_labels.py" in by_id["job_pdf_bookmark_large_volume_repair"]["command"]
    assert "--apply --limit 1" in by_id["job_pdf_bookmark_large_volume_repair"]["command"]
    assert "--per-file-timeout 900" in by_id["job_pdf_bookmark_large_volume_repair"]["command"]
    assert "--min-file-mb 80" in by_id["job_pdf_bookmark_large_volume_repair"]["command"]
    assert "--max-file-mb 1600" in by_id["job_pdf_bookmark_large_volume_repair"]["command"]
    assert by_id["job_weekend_bookmark"]["timeout_sec"] == 21600
    assert "--single-doc-fastpath" in by_id["job_weekend_bookmark"]["command"]
    assert by_id["job_nas_pdf_ocr_worker_offpeak"]["cron"] == "45 1,3,5,22 * * *"
    assert "--batch 1" in by_id["job_nas_pdf_ocr_worker_offpeak"]["command"]
    assert "--require-free-inactive-gb 4" in by_id["job_nas_pdf_ocr_worker_offpeak"]["command"]


def test_seed_cron_jobs_disables_deprecated_pdf_annotator(tmp_path):
    import scripts.seed_cron_jobs as seed

    legacy = {
        "id": "job_1772867062892_e33b6a",
        "cron": "10 2 * * *",
        "command": "python3 skills/pdf-annotator/action.py --task annotate",
        "desc": "PDF 自動標籤（舊）",
        "enabled": True,
    }
    (tmp_path / "cron_jobs.json").write_text(json.dumps([legacy], ensure_ascii=False), encoding="utf-8")

    result = seed.seed_jobs(tmp_path, python_path=tmp_path / "venv" / "bin" / "python3")
    jobs = json.loads((tmp_path / "cron_jobs.json").read_text(encoding="utf-8"))
    by_id = {job["id"]: job for job in jobs}

    assert result["ok"] is True
    assert by_id["job_1772867062892_e33b6a"]["enabled"] is False
    assert by_id["job_1772867062892_e33b6a"]["no_catchup"] is True
    assert by_id["job_1772867062892_e33b6a"]["desc"].startswith("已停用：")


def test_discord_cron_scheduler_dispatches_without_blocking_loop():
    source = Path("api/discord_bot.py").read_text(encoding="utf-8")

    assert "_execute_scheduled_job" in source
    assert "asyncio.create_task" in source
    assert "_CRON_RUNNING_TASKS" in source
    assert "skip overlapping launch" in source
    assert "SCHEDULER_LOCK_NAME" in source


def test_discord_cron_scheduler_parses_quoted_commands_before_blocking_prefixes():
    source = Path("api/discord_bot.py").read_text(encoding="utf-8")

    assert "parse_cron_command(command)" in source
    assert "command.strip().startswith" not in source


def test_daemon_cron_fallback_dispatches_without_blocking_loop():
    source = Path("daemon.py").read_text(encoding="utf-8")

    assert "ThreadPoolExecutor" in source
    assert "magi-cron-fallback" in source
    assert "executor.submit(_run_fallback_job, job)" in source
    assert "skip overlapping launch" in source
    assert "SCHEDULER_LOCK_NAME" in source


def test_background_task_lock_audit_contracts_are_green():
    import scripts.ops.audit_operational_hardening as audit

    report = audit.audit_background_task_locks()

    assert report["ok"] is True
    assert report["failure_count"] == 0
    names = {check["name"] for check in report["checks"]}
    assert "pdf_in_place_mutation_guard" in names
    assert "nas_ocr_queue_worker_lock" in names


def test_seed_cron_jobs_parse_runtime_paths_with_spaces(tmp_path):
    import scripts.seed_cron_jobs as seed
    from api.platforms.safe_process import parse_cron_command

    repo_root = tmp_path / "Workspace Home" / "MAGI_v2"
    python_path = repo_root / "Library" / "Application Support" / "MAGI" / "bin" / "python3"

    jobs = [
        seed.worldmonitor_job(repo_root=repo_root, python_path=python_path),
        *seed.business_jobs(repo_root=repo_root, python_path=python_path),
        *seed.operational_jobs(repo_root=repo_root, python_path=python_path),
    ]

    for job in jobs:
        argv = parse_cron_command(job["command"])
        assert argv, f"failed to parse command for {job['id']}"
        assert not any("&&" in arg for arg in argv), f"unexpected shell token in {job['id']}"

    worldmonitor = next(job for job in jobs if job["id"] == "job_worldmonitor_intel")
    assert parse_cron_command(worldmonitor["command"])[0] == str(python_path)


def test_seed_cron_jobs_installs_monthly_accounting_bonus_job(tmp_path):
    import scripts.seed_cron_jobs as seed

    result = seed.seed_jobs(tmp_path, python_path=tmp_path / "venv" / "bin" / "python3")
    jobs = json.loads((tmp_path / "cron_jobs.json").read_text(encoding="utf-8"))
    by_id = {job["id"]: job for job in jobs}

    job = by_id["job_accounting_monthly_bonus"]
    assert result["ok"] is True
    assert job["enabled"] is True
    assert job["cron"] == "0 12 * * *"
    assert job["no_catchup"] is True
    assert "accounting_monthly_bonus.py" in job["command"]
    assert "--commit" in job["command"]
    assert "--refresh-import" in job["command"]
    assert "--catch-up" in job["command"]
    assert "--export-xlsx" in job["command"]


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


def test_share_tunnel_watchdog_respects_stable_non_cloudflare_base():
    source = Path("api/startup.py").read_text(encoding="utf-8")
    share_block = source[source.index("def _ensure_paperclip_share_tunnel") : source.index("def _paperclip_share_tunnel_watchdog")]

    assert "def _paperclip_share_public_base_is_managed_tunnel" in source
    assert "if public_ok and not _paperclip_share_public_base_is_managed_tunnel():" in share_block
    assert ".trycloudflare.com" in source


def test_judicial_daytime_cron_batches_are_bounded():
    jobs = _cron_jobs_or_skip()
    by_id = {job["id"]: job for job in jobs}
    expected_caps = {
        "job_judicial_api_morning": (240, 48, 2400, "extractive", True, False),
        "job_judicial_api_noon": (240, 48, 2400, "extractive", True, False),
        "job_judicial_api_afternoon": (240, 48, 2400, "extractive", True, False),
        "job_judicial_api_evening": (240, 48, 2400, "extractive", True, False),
        "job_judicial_api_backlog_clear": (240, 48, 2400, "extractive", True, False),
    }

    for job_id, (max_docs, summarize_max, timeout_sec, summary_mode, skip_assets, vector_ingest) in expected_caps.items():
        job = by_id[job_id]
        assert job["enabled"] is True
        match = re.search(r"official_api_day_process (\{.*?\})(?:'|\")", job["command"])
        assert match, job_id
        payload = json.loads(match.group(1).replace(r"\"", '"'))
        assert payload["max_docs"] == max_docs
        assert payload["summarize_max"] == summarize_max
        assert payload["summary_mode"] == summary_mode
        assert payload["skip_assets"] is skip_assets
        assert payload["vector_ingest"] is vector_ingest
        assert job["timeout_sec"] == timeout_sec


def test_judicial_night_pull_is_deduped_and_locked():
    source = Path("skills/judgment-collector/action.py").read_text(encoding="utf-8")

    assert "judicial_api_night_pull.lock" in source
    assert "judicial_api_night_pull_already_running" in source
    assert '_env("JUDICIAL_API_REFRESH_EXISTING", "0")' in source


def test_local_nightly_autopilot_timeout_covers_midnight_pull():
    jobs = _cron_jobs_or_skip()
    by_id = {job["id"]: job for job in jobs}

    assert by_id["job_nightly_autopilot"]["timeout_sec"] >= 28800
    assert by_id["job_nightly_autopilot"]["resource_block_at"] == "core_only"
    assert "--block-at core_only" in by_id["job_nightly_autopilot"]["command"]


def test_cron_scheduler_has_hardcoded_timeouts_for_runtime_only_jobs():
    source = Path("api/discord_bot.py").read_text(encoding="utf-8")

    assert '"job_nightly_autopilot": 28800' in source
    assert '"job_weekend_bookmark": 21600' in source


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
