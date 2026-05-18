# -*- coding: utf-8 -*-
"""Tests for R3 cron_state migration."""

from __future__ import annotations

import json
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


def test_flag_off_writes_last_run_to_cron_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "0")
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "* * * * *", "command": "echo a", "desc": "", "enabled": True}
    ])
    s.jobs[0]["last_run"] = "2026-04-19T00:00:00"
    s._save_jobs()
    payload = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert payload[0]["last_run"] == "2026-04-19T00:00:00"


def test_flag_on_clears_last_run_in_cron_jobs(tmp_runtime, tmp_path, monkeypatch):
    s = _make_scheduler(tmp_path, monkeypatch, [
        {"id": "j1", "cron": "* * * * *", "command": "echo a", "desc": "", "enabled": True}
    ])
    s.jobs[0]["last_run"] = "2026-04-19T00:00:00"
    s._save_jobs()
    payload = json.loads((tmp_path / "cron_jobs.json").read_text())
    assert payload[0]["last_run"] is None
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
    assert payload[0]["last_run"] is None
    assert payload[0]["last_run_minute"] is None
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
