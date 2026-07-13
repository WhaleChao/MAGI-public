from __future__ import annotations

import json
import sys
import time
import types

from skills.ops import self_repair_reporter


def test_self_repair_reporter_quiets_recovered_and_stale_groups(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    agenda = runtime / "issue_agenda.jsonl"
    state = runtime / "self_repair_last_report.json"
    cron_state = runtime / "cron_state.json"
    now = time.time()

    rows = [
        {
            "ts": now - 3600,
            "command": "cron:job_active",
            "error": "exit=1 stderr=Traceback: boom",
            "source": "discord_bot.cron_scheduler",
            "severity": "High",
        },
        {
            "ts": now - 3500,
            "command": "cron:job_active",
            "error": "exit=1 stderr=Traceback: boom",
            "source": "discord_bot.cron_scheduler",
            "severity": "High",
        },
        {
            "ts": now - 7200,
            "command": "cron:job_recovered",
            "error": "exit=1 stderr=Traceback: old",
            "source": "discord_bot.cron_scheduler",
            "severity": "High",
        },
        {
            "ts": now - 72 * 3600,
            "command": "cron:job_stale",
            "error": "exit=1 stderr=Traceback: old",
            "source": "discord_bot.cron_scheduler",
            "severity": "High",
        },
    ]
    agenda.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    cron_state.write_text(
        json.dumps({"job_recovered": {"last_success_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 1800))}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(self_repair_reporter, "_AGENDA_PATH", agenda)
    monkeypatch.setattr(self_repair_reporter, "_STATE_PATH", state)
    monkeypatch.setattr(self_repair_reporter, "_LOOKBACK_DAYS", 7)
    monkeypatch.setattr(self_repair_reporter, "_STALE_HOURS", 48)
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    result = self_repair_reporter.run_report(dry_run=True, force=True)
    out = capsys.readouterr().out

    assert result["active_groups_count"] == 1
    assert result["recovered_groups_count"] == 1
    assert result["stale_groups_count"] == 1
    assert "job_active" in out
    assert "job_active" in out
    assert "追蹤碼" in out
    assert "job_recovered" not in out
    assert "job_stale" not in out


def test_self_repair_reporter_dry_run_does_not_write_state(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    agenda = runtime / "issue_agenda.jsonl"
    state = runtime / "self_repair_last_report.json"
    agenda.write_text("", encoding="utf-8")

    monkeypatch.setattr(self_repair_reporter, "_AGENDA_PATH", agenda)
    monkeypatch.setattr(self_repair_reporter, "_STATE_PATH", state)
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    result = self_repair_reporter.run_report(dry_run=True, force=True)

    assert result["success"] is True
    assert not state.exists()


def test_self_repair_reporter_never_uses_untrusted_command_as_job_label():
    assert self_repair_reporter._job_label(
        "python action.py --task 1130919-T-057?token=secret"
    ) == "unknown"


def test_self_repair_reporter_uses_success_time_not_dispatch_time(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    agenda = runtime / "issue_agenda.jsonl"
    state = runtime / "self_repair_last_report.json"
    cron_state = runtime / "cron_state.json"
    now = time.time()
    agenda.write_text(
        json.dumps(
            {
                "ts": now - 3600,
                "command": "cron:job_failed_again",
                "error": "exit=1 stderr=Traceback: boom",
                "source": "discord_bot.cron_scheduler",
                "severity": "High",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cron_state.write_text(
        json.dumps(
            {
                "job_failed_again": {
                    "last_run": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 300)),
                    "last_success_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 7200)),
                    "last_success": False,
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(self_repair_reporter, "_AGENDA_PATH", agenda)
    monkeypatch.setattr(self_repair_reporter, "_STATE_PATH", state)
    monkeypatch.setattr(self_repair_reporter, "_LOOKBACK_DAYS", 7)
    monkeypatch.setattr(self_repair_reporter, "_STALE_HOURS", 48)
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    result = self_repair_reporter.run_report(dry_run=True, force=True)

    assert result["active_groups_count"] == 1
    assert result["recovered_groups_count"] == 0


def test_self_repair_reporter_guardian_unresolved_blocks_ordinary_cron_recovery(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    agenda = runtime / "issue_agenda.jsonl"
    state = runtime / "self_repair_last_report.json"
    now = time.time()
    agenda.write_text(
        json.dumps(
            {
                "ts": now - 3600,
                "command": "cron:job_guardian_open",
                "error": "exit=1 stderr=Traceback: boom",
                "source": "discord_bot.cron_scheduler",
                "severity": "High",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps({"job_guardian_open": {"last_success_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 60))}}),
        encoding="utf-8",
    )
    (runtime / "magi_self_repair_guardian_latest.json").write_text(
        json.dumps({"ok": False, "unresolved_issue_ids": ["function_health:failed:cron:job_guardian_open"]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(self_repair_reporter, "_AGENDA_PATH", agenda)
    monkeypatch.setattr(self_repair_reporter, "_STATE_PATH", state)
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    result = self_repair_reporter.run_report(dry_run=True, force=True)

    assert result["active_groups_count"] == 1
    assert result["recovered_groups_count"] == 0


def test_incomplete_operational_audit_cannot_mark_issue_recovered(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    latest = runtime / "operational_hardening_audit_latest.json"
    latest.write_text(json.dumps({"cron": {}, "gmail_monitor": {}}, ensure_ascii=False), encoding="utf-8")
    issue_ts = time.time() - 60
    monkeypatch.setattr(self_repair_reporter, "_STATE_PATH", runtime / "self_repair_last_report.json")

    assert self_repair_reporter._latest_operational_audit_is_green(issue_ts) is False


def test_self_repair_reporter_redacts_before_notification(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    agenda = runtime / "issue_agenda.jsonl"
    state = runtime / "self_repair_last_report.json"
    agenda.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "command": "cron:job_safe_report",
                "error": "Traceback case=1130919-T-057 path=/Users/ai/案件/王小明 token=secret-value?q=private",
                "source": "discord_bot.cron_scheduler",
                "severity": "High",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    sent = []
    fake_module = types.SimpleNamespace(alert_admin=lambda message, **_kwargs: sent.append(message) or True)
    monkeypatch.setitem(sys.modules, "skills.ops.red_phone", fake_module)
    monkeypatch.setattr(self_repair_reporter, "_AGENDA_PATH", agenda)
    monkeypatch.setattr(self_repair_reporter, "_STATE_PATH", state)
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    result = self_repair_reporter.run_report(dry_run=False, force=True)

    assert result["sent"] is True
    assert sent == [result["report_text"]]
    assert "job_safe_report" in sent[0]
    assert "追蹤碼" in sent[0]
    for secret in ("1130919", "/Users/ai", "王小明", "secret-value", "private"):
        assert secret not in sent[0]


def test_self_repair_reporter_marks_fixed_nightly_timeout(monkeypatch):
    now = time.time()
    groups = self_repair_reporter._group_records(
        [
            {
                "ts": now - 120,
                "command": "cron:job_nightly_autopilot",
                "error": "exit=-9 stderr=judicial_api_night_thread: 等待 49 秒到 00:00 服務時段",
                "source": "discord_bot.cron_scheduler",
                "severity": "High",
            }
        ]
    )
    monkeypatch.setattr(self_repair_reporter, "_load_cron_last_run_ts", lambda: {})
    monkeypatch.setattr(self_repair_reporter, "_load_cron_job_map", lambda: {"job_nightly_autopilot": {"timeout_sec": 28800}})
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    self_repair_reporter._annotate_group_status(groups, now_ts=now)
    group = next(iter(groups.values()))

    assert group["status"] == "recovered"
    assert "8 小時" in group["status_reason"]


def test_self_repair_reporter_marks_fixed_weekend_bookmark_timeout(monkeypatch):
    now = time.time()
    groups = self_repair_reporter._group_records(
        [
            {
                "ts": now - 120,
                "command": "cron:job_weekend_bookmark",
                "error": "exit=-15 stderr=stdout_tail=掃描卷宗 PDF",
                "source": "discord_bot.cron_scheduler",
                "severity": "High",
            }
        ]
    )
    monkeypatch.setattr(self_repair_reporter, "_load_cron_last_run_ts", lambda: {})
    monkeypatch.setattr(self_repair_reporter, "_load_cron_job_map", lambda: {"job_weekend_bookmark": {"timeout_sec": 21600}})
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    self_repair_reporter._annotate_group_status(groups, now_ts=now)
    group = next(iter(groups.values()))

    assert group["status"] == "recovered"
    assert "6 小時" in group["status_reason"]


def test_self_repair_reporter_marks_disabled_cron_job_recovered(monkeypatch):
    now = time.time()
    groups = self_repair_reporter._group_records(
        [
            {
                "ts": now - 120,
                "command": "cron:job_disabled",
                "error": "exit=-15 stderr=ReadTimeoutError",
                "source": "discord_bot.cron_scheduler",
                "severity": "High",
            }
        ]
    )
    monkeypatch.setattr(self_repair_reporter, "_load_cron_last_run_ts", lambda: {})
    monkeypatch.setattr(self_repair_reporter, "_load_cron_job_map", lambda: {"job_disabled": {"enabled": False}})
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    self_repair_reporter._annotate_group_status(groups, now_ts=now)
    group = next(iter(groups.values()))

    assert group["status"] == "recovered"
    assert "已停用" in group["status_reason"]


def test_self_repair_reporter_marks_green_operational_audit_recovered(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    state = runtime / "self_repair_last_report.json"
    latest = runtime / "operational_hardening_audit_latest.json"
    latest.write_text(
        json.dumps(
            {
                "cron": {"parse_failure_count": 0, "collision_count": 0},
                "gmail_monitor": {"ok": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    now = time.time()
    groups = self_repair_reporter._group_records(
        [
            {
                "ts": now - 120,
                "command": "cron:job_operational_hardening_audit",
                "error": "exit=1 stderr= stdout_tail={\"cron_collisions\": 4}",
                "source": "discord_bot.cron_scheduler",
                "severity": "High",
            }
        ]
    )
    monkeypatch.setattr(self_repair_reporter, "_STATE_PATH", state)
    monkeypatch.setattr(self_repair_reporter, "_load_cron_last_run_ts", lambda: {})
    monkeypatch.setattr(
        self_repair_reporter,
        "_load_cron_job_map",
        lambda: {"job_operational_hardening_audit": {"enabled": True}},
    )
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    self_repair_reporter._annotate_group_status(groups, now_ts=now)
    group = next(iter(groups.values()))

    assert group["status"] == "recovered"
    assert "轉綠" in group["status_reason"]


def test_self_repair_reporter_labels_resource_governor_memory_pressure(tmp_path, monkeypatch, capsys):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    agenda = runtime / "issue_agenda.jsonl"
    state = runtime / "self_repair_last_report.json"
    now = time.time()
    payload = {
        "ok": False,
        "level": "critical",
        "reasons": ["swap_used>24GB", "free_plus_inactive<2GB"],
        "actions": ["notify_operator"],
        "snapshot": {
            "disk_free_gb": 140.76,
            "disk_total_gb": 460.43,
            "swap_used_gb": 25.39,
            "free_gb": 0.55,
            "inactive_gb": 1.34,
            "free_plus_inactive_gb": 1.89,
            "memory_free_percent": 14.0,
            "timestamp": "2026-07-10 05:20:35",
        },
    }
    row = {
        "ts": now - 300,
        "command": "cron:job_resource_governor",
        "error": f"exit=2 stderr= stdout_tail={json.dumps(payload, ensure_ascii=False)}",
        "source": "discord_bot.cron_scheduler",
        "severity": "High",
    }
    rows = [row, {**row, "ts": now - 120}]
    agenda.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in rows), encoding="utf-8")

    monkeypatch.setattr(self_repair_reporter, "_AGENDA_PATH", agenda)
    monkeypatch.setattr(self_repair_reporter, "_STATE_PATH", state)
    monkeypatch.setattr(self_repair_reporter, "_LOOKBACK_DAYS", 7)
    monkeypatch.setattr(self_repair_reporter, "_STALE_HOURS", 48)
    monkeypatch.setattr(self_repair_reporter, "_current_omlx_models", lambda: [])

    result = self_repair_reporter.run_report(dry_run=True, force=True)
    out = capsys.readouterr().out

    assert result["active_groups_count"] == 1
    assert "job_resource_governor" in out
    assert "記憶體壓力" in out
    assert "追蹤碼" in out
    assert "swap 25.39GB" not in out
    assert "台灣時間" not in out
    assert "GeneralError" not in out
    assert "UTC" not in out
