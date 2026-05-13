# -*- coding: utf-8 -*-
"""Tests for Layer 1 omlx heartbeat reaper."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from scripts.ops import omlx_heartbeat_reaper as hb


def _p(pid, start_epoch, model_dir=None, port=None, cmdline=""):
    return hb.OmlxProc(
        pid=pid, start_epoch=start_epoch,
        cmdline=cmdline or f"omlx serve --model-dir {model_dir} --port {port}",
        model_dir=model_dir, port=port,
    )


# ---------- find_duplicates --------------------------------------------

def test_no_duplicates_returns_empty():
    procs = [
        _p(100, 1000.0, "models-text", 8080),
        _p(200, 1010.0, "models-text-phi4", 8082),
        _p(300, 1020.0, "models-text-smol", 8083),
    ]
    assert hb.find_duplicates(procs) == []


def test_duplicates_keep_oldest_kill_rest():
    procs = [
        _p(100, 1000.0, "models-text", 8080),   # oldest
        _p(200, 1500.0, "models-text", 8080),   # newer duplicate
        _p(300, 1800.0, "models-text", 8080),   # newest duplicate
    ]
    kill = hb.find_duplicates(procs)
    assert sorted(p.pid for p in kill) == [200, 300]


def test_never_kill_oldest_pid_even_if_pid_is_larger():
    # pid 順序與 start_epoch 無關 — 嚴格用 start_epoch 判斷
    procs = [
        _p(999, 500.0, "models-text", 8080),    # oldest (smaller start_epoch)
        _p(100, 1500.0, "models-text", 8080),   # newer
    ]
    kill = hb.find_duplicates(procs)
    assert [p.pid for p in kill] == [100]


def test_procs_without_model_dir_not_in_group():
    procs = [
        _p(100, 1000.0, None, None, "omlx serve --weird"),
        _p(200, 1100.0, "models-text", 8080),
    ]
    assert hb.find_duplicates(procs) == []


def test_never_kill_self_or_parent(monkeypatch):
    monkeypatch.setattr(os, "getpid", lambda: 200)
    monkeypatch.setattr(os, "getppid", lambda: 100)
    procs = [
        _p(50, 500.0, "models-text", 8080),
        _p(100, 1000.0, "models-text", 8080),   # parent
        _p(200, 1100.0, "models-text", 8080),   # self
    ]
    kill = hb.find_duplicates(procs)
    # oldest is 50, 100 and 200 filtered out
    assert kill == []


# ---------- run() shadow mode ------------------------------------------

def test_run_shadow_no_ops_when_no_duplicates(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "DECISION_PATH", tmp_path / "decisions.jsonl")
    procs = [_p(100, 1000.0, "models-text", 8080)]
    monkeypatch.setattr(hb, "_list_omlx_serves", lambda: procs)
    rc = hb.run("shadow", expected_ports=3, mode_name="DAY")
    assert rc == 0
    line = (tmp_path / "decisions.jsonl").read_text().strip()
    rec = json.loads(line)
    assert rec["action"] == "no_op"
    assert rec["mode"] == "shadow"


def test_run_shadow_logs_would_kill_but_does_not_kill(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "DECISION_PATH", tmp_path / "decisions.jsonl")
    procs = [
        _p(100, 1000.0, "models-text", 8080),
        _p(200, 1500.0, "models-text", 8080),
        _p(300, 1600.0, "models-text", 8080),
        _p(400, 1700.0, "models-text", 8080),   # force actual_count>upper_limit
        _p(500, 1800.0, "models-text", 8080),
        _p(600, 1900.0, "models-text", 8080),
        _p(700, 2000.0, "models-text", 8080),
        _p(800, 2100.0, "models-text", 8080),   # 8 total, upper=7
    ]
    monkeypatch.setattr(hb, "_list_omlx_serves", lambda: procs)
    killed: list = []
    monkeypatch.setattr(hb, "_kill_one", lambda p: killed.append(p.pid) or {})
    rc = hb.run("shadow", expected_ports=3, mode_name="DAY")
    assert rc == 0
    assert killed == []
    rec = json.loads((tmp_path / "decisions.jsonl").read_text().strip())
    assert rec["action"] == "would_kill"
    assert sorted([d["pid"] for d in rec["duplicates"]]) == [200, 300, 400, 500, 600, 700, 800]


def test_run_enforce_actually_kills(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "DECISION_PATH", tmp_path / "decisions.jsonl")
    procs = [
        _p(i, 1000.0 + i, "models-text", 8080) for i in (100, 200, 300, 400, 500, 600, 700, 800)
    ]
    monkeypatch.setattr(hb, "_list_omlx_serves", lambda: procs)
    killed_pids: list = []

    def fake_kill(p):
        killed_pids.append(p.pid)
        return {"pid": p.pid, "died": True, "sigterm_sent": True, "sigkill_sent": False}

    monkeypatch.setattr(hb, "_kill_one", fake_kill)
    rc = hb.run("enforce", expected_ports=3, mode_name="DAY")
    assert rc == 0
    # oldest is 100; kill 200..800
    assert 100 not in killed_pids
    assert sorted(killed_pids) == [200, 300, 400, 500, 600, 700, 800]
    rec = json.loads((tmp_path / "decisions.jsonl").read_text().strip())
    assert rec["action"] == "killed"
    assert all(o["died"] for o in rec["outcomes"])


def test_run_below_upper_limit_no_kill_even_if_duplicates_exist(monkeypatch, tmp_path):
    # 避免正常日間 3 個 port + parent/worker 波動被誤殺
    monkeypatch.setattr(hb, "DECISION_PATH", tmp_path / "decisions.jsonl")
    procs = [
        _p(100, 1000.0, "models-text", 8080),
        _p(200, 1500.0, "models-text", 8080),   # duplicate but total=2, upper=7
    ]
    monkeypatch.setattr(hb, "_list_omlx_serves", lambda: procs)
    rc = hb.run("shadow", expected_ports=3, mode_name="DAY")
    assert rc == 0
    rec = json.loads((tmp_path / "decisions.jsonl").read_text().strip())
    assert rec["action"] == "no_op"


def test_run_invalid_mode_defaults_to_shadow(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "DECISION_PATH", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(hb, "_list_omlx_serves", lambda: [])
    monkeypatch.setenv("OMLX_HEARTBEAT_KILL_MODE", "bogus")
    rc = hb.main(["--expected-ports", "1", "--mode-name", "NIGHT"])
    assert rc == 0


# ---------- _kill_one safety ------------------------------------------

def test_kill_one_handles_dead_process_gracefully(monkeypatch):
    proc = _p(999999, 1000.0, "models-text", 8080)
    calls = {"n": 0}

    def fake_kill(pid, sig):
        # first call SIGTERM → ProcessLookupError (already dead)
        calls["n"] += 1
        raise ProcessLookupError()

    monkeypatch.setattr(hb.os, "kill", fake_kill)
    out = hb._kill_one(proc, grace_sec=0.01)
    assert out["died"] is True
    assert out["error"] is None


def test_kill_one_sends_sigkill_if_sigterm_ignored(monkeypatch):
    proc = _p(12345, 1000.0, "models-text", 8080)
    signals_sent: list = []

    def fake_kill(pid, sig):
        signals_sent.append((pid, sig))
        # SIGTERM → success; 0 probes → process still alive; SIGKILL → success
        if sig == 0:
            return  # alive
        return

    def fake_sleep(_):
        pass

    monkeypatch.setattr(hb.os, "kill", fake_kill)
    monkeypatch.setattr(hb.time, "sleep", fake_sleep)

    # after SIGKILL, the final probe should still "succeed" (= alive)
    # but _kill_one checks after sleep(0.5)
    out = hb._kill_one(proc, grace_sec=0.05)
    assert out["sigterm_sent"] is True
    assert out["sigkill_sent"] is True
    # last probe → alive, so died=False
    assert out["died"] is False


# ---------- parse etime --------------------------------------------------

def test_parse_etime_formats():
    assert hb._parse_etime("00:30") == 30
    assert hb._parse_etime("01:15") == 75
    assert hb._parse_etime("02:30:00") == 9000
    assert hb._parse_etime("1-00:00:00") == 86400
    assert hb._parse_etime("garbage") is None
    assert hb._parse_etime("") is None


# ---------- list_omlx_serves: parsing ps -------------------------------

def test_list_omlx_serves_parses_ps_output(monkeypatch):
    fake_stdout = (
        "  100     00:30 /usr/bin/python3 /opt/homebrew/opt/omlx/bin/omlx serve --model-dir /Users/ai/.omlx/models-text --port 8080\n"
        "  200     01:00 /usr/bin/python3 /opt/homebrew/opt/omlx/bin/omlx serve --model-dir /Users/ai/.omlx/models-text-phi4 --port 8082\n"
        "  300     00:10 /some/other/command\n"
    )

    class FakeRes:
        stdout = fake_stdout

    monkeypatch.setattr(hb.subprocess, "run", lambda *a, **kw: FakeRes())
    procs = hb._list_omlx_serves()
    pids = [p.pid for p in procs]
    assert 100 in pids and 200 in pids and 300 not in pids
    p100 = next(p for p in procs if p.pid == 100)
    assert p100.model_dir == "/Users/ai/.omlx/models-text"
    assert p100.port == 8080
