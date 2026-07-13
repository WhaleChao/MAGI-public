# -*- coding: utf-8 -*-
"""Tests for R3 cron_state migration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
import pytest


@pytest.fixture
def tmp_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    yield tmp_path


def _make_scheduler(tmp_path, monkeypatch, jobs):
    # 把 JOB_FILE 指到 tmp
    from skills.ops import cron_scheduler as cs
    fn = tmp_path / "cron_jobs.json"
    fn.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))
    monkeypatch.setattr(cs, "JOB_FILE", str(fn))
    return cs.CronScheduler()


def test_cron_jobs_definition_write_strips_runtime_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "0")
    s = _make_scheduler(tmp_path, monkeypatch, [
        {
            "id": "j1",
            "cron": "* * * * *",
            "command": "echo a",
            "desc": "",
            "enabled": True,
            "last_run": "old",
            "last_success_at": "old",
            "returncode": 0,
            "timed_out": False,
            "stdout": "runtime",
        }
    ])
    s.jobs[0]["last_run"] = "2026-04-19T00:00:00"
    s.jobs[0]["last_success_at"] = "2026-04-19T00:00:00"
    s.jobs[0]["returncode"] = 0
    s._save_jobs()
    payload = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert payload[0]["id"] == "j1"
    assert "last_run" not in payload[0]
    assert "last_success_at" not in payload[0]
    assert "returncode" not in payload[0]
    assert "timed_out" not in payload[0]
    assert "stdout" not in payload[0]


def test_flag_on_clears_last_run_in_cron_jobs(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "* * * * *", "command": "echo a", "desc": "", "enabled": True}
    ])
    s.jobs[0]["last_run"] = "2026-04-19T00:00:00"
    s._save_jobs()
    payload = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert "last_run" not in payload[0]
    assert "last_run_minute" not in payload[0]
    assert s.jobs[0]["last_run"] == "2026-04-19T00:00:00"


def test_mark_job_run_writes_runtime_state_without_dirtying_cron_jobs(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "35 7 * * *", "command": "echo a", "desc": "", "enabled": True}
    ])

    assert s.mark_job_run("j1") is True

    from api.platforms import runtime_dir as rd
    state = json.loads(rd.cron_state().read_text())
    payload = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert state["j1"]["last_run_minute"]
    assert state["j1"]["last_dispatch_at"]
    assert "last_run" not in payload[0]
    assert "last_run_minute" not in payload[0]
    assert s.jobs[0]["last_run_minute"] == state["j1"]["last_run_minute"]


def test_cron_state_load_when_legacy_has_last_run(tmp_runtime, tmp_path, monkeypatch):
    # legacy cron_jobs.json 有 last_run，state 尚未建立；_load_jobs 不應清掉 last_run（只在寫才清）
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "* * * * *", "command": "echo a",
         "desc": "", "enabled": True, "last_run": "2026-04-18T09:00:00"}
    ])
    assert s.jobs[0]["last_run"] == "2026-04-18T09:00:00"


def test_cron_state_overrides_legacy(tmp_runtime, tmp_path, monkeypatch):
    # state 有值就蓋 legacy
    from api.platforms import runtime_dir as rd
    rd.atomic_write_json(rd.cron_state(), {"j1": {
        "last_run": "2026-04-19T10:00:00",
        "last_run_minute": "2026-04-19 10:00",
    }})
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "* * * * *", "command": "echo a",
         "desc": "", "enabled": True, "last_run": "2026-04-18T09:00:00"}
    ])
    assert s.jobs[0]["last_run"] == "2026-04-19T10:00:00"


def test_check_due_writes_state(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "* * * * *", "command": "echo a",
         "desc": "", "enabled": True}
    ])
    due = s.check_due_jobs()
    assert len(due) == 1
    from api.platforms import runtime_dir as rd
    state = json.loads(rd.cron_state().read_text())
    assert "j1" in state and state["j1"]["last_run"]


def test_mark_job_result_writes_success_and_failure_without_dirtying_cron_jobs(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "35 7 * * *", "command": "echo a", "desc": "", "enabled": True}
    ])

    assert s.mark_job_result("j1", success=True, returncode=0, duration_sec=1.234) is True

    from api.platforms import runtime_dir as rd
    state = json.loads(rd.cron_state().read_text())
    payload = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert state["j1"]["last_success"] is True
    assert state["j1"]["last_success_at"]
    assert state["j1"]["returncode"] == 0
    assert state["j1"]["last_returncode"] == 0
    assert state["j1"]["last_complete_at"]
    assert "last_run" not in payload[0]

    assert s.mark_job_result(
        "j1",
        success=False,
        returncode=2,
        error="boom",
        stdout_tail="hello",
        stderr_tail="trace",
    ) is True
    state = json.loads(rd.cron_state().read_text())
    assert state["j1"]["last_success"] is False
    assert state["j1"]["last_failure_at"]
    assert state["j1"]["last_error"] == "boom"
    assert state["j1"]["returncode"] == 2

    assert s.mark_job_result("j1", success=True, returncode=0) is True
    state = json.loads(rd.cron_state().read_text())
    assert state["j1"]["last_error"] == ""
    assert state["j1"]["last_stdout_tail"] == ""
    assert state["j1"]["last_stderr_tail"] == ""


def test_mark_job_result_redacts_case_identity_credentials_and_paths(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "35 7 * * *", "command": "echo a", "desc": "", "enabled": True}
    ])

    raw = (
        '{"client_name":"王小明","case_number":"2026-0050",'
        '"court_case_no":"115年度訴字第123號",'
        '"folder_path":"/Users/ai/Library/Application Support/MAGI/private",'
        '"token":"secret-value"}'
    )
    assert s.mark_job_result(
        "j1",
        success=False,
        returncode=2,
        error=raw,
        stdout_tail=raw,
        stderr_tail="mail=test@example.com phone=0912-345-678",
    ) is True

    from api.platforms import runtime_dir as rd
    payload = json.loads(rd.cron_state().read_text())["j1"]
    persisted = json.dumps(payload, ensure_ascii=False)
    for secret in ("王小明", "2026-0050", "115年度訴字第123號", "/Users/ai", "secret-value", "test@example.com", "0912-345-678"):
        assert secret not in persisted
    assert "<REDACTED>" in persisted


def test_dispatch_start_complete_are_distinct_runtime_markers(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "35 7 * * *", "command": "echo a", "desc": "", "enabled": True}
    ])

    assert s.mark_job_dispatched("j1") is True
    assert s.mark_job_started("j1") is True
    assert s.mark_job_complete("j1", success=True, returncode=0) is True

    from api.platforms import runtime_dir as rd
    state = json.loads(rd.cron_state().read_text())
    assert state["j1"]["last_dispatch_at"]
    assert state["j1"]["last_start_at"]
    assert state["j1"]["last_complete_at"]
    payload = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert not any(key.startswith("last_") for key in payload[0])


def test_reconcile_incomplete_jobs_marks_expired_dispatch_interrupted(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "0 1 * * *", "command": "python3 task.py", "desc": "", "enabled": True, "timeout_sec": 60}
    ])
    dispatched = datetime(2026, 7, 11, 1, 0, 0)
    s.mark_job_dispatched("j1", when=dispatched)

    reconciled = s.reconcile_incomplete_jobs(now=dispatched + timedelta(minutes=2))

    assert reconciled == ["j1"]
    from api.platforms import runtime_dir as rd
    state = json.loads(rd.cron_state().read_text())["j1"]
    assert state["last_success"] is False
    assert state["last_timed_out"] is True
    assert state["last_returncode"] == 130
    assert state["last_error"] == "scheduler_completion_missing_after_timeout"


def test_reconcile_incomplete_jobs_leaves_fresh_dispatch_running(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "0 1 * * *", "command": "python3 task.py", "desc": "", "enabled": True, "timeout_sec": 600}
    ])
    dispatched = datetime(2026, 7, 11, 1, 0, 0)
    s.mark_job_dispatched("j1", when=dispatched)

    assert s.reconcile_incomplete_jobs(now=dispatched + timedelta(minutes=2)) == []


def test_flag_off_does_not_create_cron_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "0")
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path / "rt"))
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "* * * * *", "command": "echo a",
         "desc": "", "enabled": True}
    ])
    s.check_due_jobs()
    assert not (tmp_path / "rt" / "cron_state.json").exists()


def test_duplicate_id_does_not_double_write(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "* * * * *", "command": "echo a",
         "desc": "", "enabled": True}
    ])
    s.check_due_jobs()
    s.check_due_jobs()   # 同分鐘 no-op
    from api.platforms import runtime_dir as rd
    state = json.loads(rd.cron_state().read_text())
    assert set(state.keys()) == {"j1"}


def test_disabled_job_not_in_state(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "* * * * *", "command": "echo a",
         "desc": "", "enabled": False}
    ])
    s.check_due_jobs()
    from api.platforms import runtime_dir as rd
    p = rd.cron_state()
    state = json.loads(p.read_text()) if p.exists() else {}
    assert "j1" not in state
