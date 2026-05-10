from __future__ import annotations

import json
import time

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
        json.dumps({"job_recovered": {"last_run": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 1800))}}),
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
    assert "已恢復或過期的重複失敗" in out
    assert "job_recovered" in out
    assert "job_stale" in out


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
