# -*- coding: utf-8 -*-
"""Tests for Layer 2 memory_watchdog."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scripts.ops import memory_watchdog as mw


@pytest.fixture(autouse=True)
def _runtime_dir_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path / "runtime"))
    # 模組 import 時已算過 DECISION_LOG，需同步 patch
    from api.platforms import runtime_dir as rd
    monkeypatch.setattr(mw, "DECISION_LOG", rd.metrics("memory_watchdog_decisions"), raising=True)


def _reading(swap_gb: float, free_gb: float, inactive_gb: float) -> mw.MemoryReading:
    return mw.MemoryReading(swap_used_gb=swap_gb, free_gb=free_gb, inactive_gb=inactive_gb)


def _p(pid: int, rss_mb: int, cmd: str) -> mw.Proc:
    return mw.Proc(pid=pid, rss_bytes=rss_mb * 1024 * 1024, cmdline=cmd)


# ---------- is_memory_pressure ------------------------------------------

def test_no_pressure_when_swap_and_mem_healthy():
    r = _reading(swap_gb=1.0, free_gb=5.0, inactive_gb=5.0)
    assert mw.is_memory_pressure(r) is False


def test_pressure_when_swap_exceeds_threshold():
    r = _reading(swap_gb=10.0, free_gb=5.0, inactive_gb=5.0)
    assert mw.is_memory_pressure(r) is True


def test_pressure_when_free_plus_inactive_below_threshold():
    r = _reading(swap_gb=1.0, free_gb=0.5, inactive_gb=1.0)   # total 1.5 < 2 GB
    assert mw.is_memory_pressure(r) is True


# ---------- WatchdogState.record_reading --------------------------------

def test_state_consecutive_counter_resets_on_healthy_reading():
    state = mw.WatchdogState()
    assert state.record_reading(_reading(10, 5, 5)) is None   # 1
    assert state.record_reading(_reading(10, 5, 5)) is None   # 2
    # healthy → reset
    assert state.record_reading(_reading(1, 5, 5)) is None
    assert state.consecutive_pressure == 0


def test_state_triggers_after_consecutive_pressure(monkeypatch):
    monkeypatch.setattr(mw, "TRIGGER_CONSECUTIVE", 3, raising=True)
    state = mw.WatchdogState()
    procs = [_p(500, 800, "/venv/bin/python3 api/server.py")]
    monkeypatch.setattr(mw, "list_magi_procs", lambda: procs)
    assert state.record_reading(_reading(10, 5, 5)) is None   # 1
    assert state.record_reading(_reading(10, 5, 5)) is None   # 2
    target = state.record_reading(_reading(10, 5, 5))         # 3 → trigger
    assert target is not None
    assert target.pid == 500


def test_state_respects_action_cooldown(monkeypatch):
    monkeypatch.setattr(mw, "TRIGGER_CONSECUTIVE", 1, raising=True)
    monkeypatch.setattr(mw, "ACTION_COOLDOWN_SEC", 999, raising=True)
    state = mw.WatchdogState()
    state.last_action_at = time.time()   # just acted
    procs = [_p(500, 800, "/venv/bin/python3 api/server.py")]
    monkeypatch.setattr(mw, "list_magi_procs", lambda: procs)
    assert state.record_reading(_reading(10, 5, 5)) is None


# ---------- list_magi_procs 紅線 -----------------------------------------

def test_list_magi_procs_excludes_never_kill_entries(monkeypatch):
    fake_ps = (
        "  100 100000 /usr/bin/python3 /Users/ai/Desktop/MAGI_v2/daemon.py\n"
        "  200 200000 /opt/homebrew/bin/cloudflared tunnel run\n"
        "  300 300000 /System/Library/loginwindow\n"
        "  400 400000 /venv/bin/python3 api/server.py\n"
        "  500 500000 /venv/bin/python3 api/tools_api.py\n"
        "  600 600000 omlx serve --model-dir /Users/ai/.omlx/models-text --port 8080\n"
    )

    class FakeRes:
        stdout = fake_ps

    monkeypatch.setattr(mw.subprocess, "run", lambda *a, **kw: FakeRes())
    procs = mw.list_magi_procs()
    pids = {p.pid for p in procs}
    # daemon / cloudflared / loginwindow / omlx → excluded
    assert 100 not in pids
    assert 200 not in pids
    assert 300 not in pids
    assert 600 not in pids
    # api/server + api/tools_api → included, sorted by RSS desc
    assert 500 in pids and 400 in pids
    assert procs[0].pid == 500     # larger RSS first
    assert procs[1].pid == 400


def test_list_magi_procs_ignores_non_magi_python(monkeypatch):
    fake_ps = (
        "  100 100000 /venv/bin/python3 /Users/ai/Desktop/other_tool/worker.py\n"
        "  200 200000 /venv/bin/python3 api/server.py\n"
    )

    class FakeRes:
        stdout = fake_ps

    monkeypatch.setattr(mw.subprocess, "run", lambda *a, **kw: FakeRes())
    procs = mw.list_magi_procs()
    assert [p.pid for p in procs] == [200]


# ---------- _do_action shadow / enforce --------------------------------

def test_shadow_mode_does_not_kill(monkeypatch):
    proc = _p(500, 800, "/venv/bin/python3 api/server.py")
    r = _reading(10, 5, 5)
    killed: list = []
    monkeypatch.setattr(mw.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    rec = mw._do_action(proc, r, mode="shadow")
    assert rec["action"] == "would_kill"
    assert killed == []
    # decision log 應寫入
    line = mw.DECISION_LOG.read_text().strip().splitlines()[-1]
    assert json.loads(line)["action"] == "would_kill"


def test_enforce_mode_sends_sigterm(monkeypatch):
    proc = _p(500, 800, "/venv/bin/python3 api/server.py")
    r = _reading(10, 5, 5)
    killed: list = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr(mw.os, "kill", fake_kill)
    rec = mw._do_action(proc, r, mode="enforce")
    assert rec["action"] == "killed"
    assert rec["sigterm_sent"] is True
    assert killed == [(500, 15)]  # SIGTERM = 15


def test_enforce_handles_already_dead(monkeypatch):
    proc = _p(999999, 800, "/venv/bin/python3 api/server.py")
    r = _reading(10, 5, 5)

    def raise_lookup(*_a, **_kw):
        raise ProcessLookupError()

    monkeypatch.setattr(mw.os, "kill", raise_lookup)
    rec = mw._do_action(proc, r, mode="enforce")
    assert rec["action"] == "target_gone"


# ---------- run_once integration ----------------------------------------

def test_run_once_happy_path_writes_healthy_log(monkeypatch):
    monkeypatch.setattr(mw, "read_memory", lambda: _reading(1, 5, 5))
    state = mw.WatchdogState()
    result = mw.run_once(state)
    assert result["pressure"] is False


def test_run_once_triggers_shadow_kill(monkeypatch):
    monkeypatch.setattr(mw, "TRIGGER_CONSECUTIVE", 1, raising=True)
    monkeypatch.setattr(mw, "read_memory", lambda: _reading(10, 5, 5))
    monkeypatch.setattr(mw, "list_magi_procs",
                        lambda: [_p(400, 600, "/venv/bin/python3 api/server.py")])
    monkeypatch.setenv("MAGI_WATCHDOG_KILL_MODE", "shadow")
    killed: list = []
    monkeypatch.setattr(mw.os, "kill", lambda *a: killed.append(a))
    state = mw.WatchdogState()
    rec = mw.run_once(state)
    assert rec["action"] == "would_kill"
    assert killed == []


def test_kill_mode_invalid_defaults_to_shadow(monkeypatch):
    monkeypatch.setenv("MAGI_WATCHDOG_KILL_MODE", "bogus")
    assert mw._kill_mode() == "shadow"


# ---------- read_memory parsing -----------------------------------------

def test_read_memory_parses_vm_stat_and_swap(monkeypatch):
    fake_vm_stat = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               100.\n"
        "Pages active:                             500.\n"
        "Pages inactive:                          2000.\n"
        "Pages wired down:                         300.\n"
    )
    fake_swap = "vm.swapusage: total = 12288.00M  used = 5432.16M  free = 6855.84M  (encrypted)\n"

    class FakeVM:
        stdout = fake_vm_stat

    class FakeSwap:
        stdout = fake_swap

    def fake_run(cmd, **kw):
        if cmd[0] == "vm_stat":
            return FakeVM()
        if cmd[0] == "sysctl":
            return FakeSwap()
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(mw.subprocess, "run", fake_run)
    r = mw.read_memory()
    # 100 pages * 16384 bytes = 1.6 MB ≈ 0.00156 GB
    assert r.free_gb < 0.01
    # 2000 pages * 16384 = ~31 MB
    assert r.inactive_gb < 0.1
    # swap used 5432 MB = 5.30 GB
    assert 5.2 < r.swap_used_gb < 5.4
