# -*- coding: utf-8 -*-
"""Tests for Layer 3 omlx switch gatekeeper."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ops import omlx_switch_gatekeeper as gk


@pytest.fixture(autouse=True)
def _runtime_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("OMLX_SWITCH_ADMIN_NOTIFY", str(tmp_path / "alert.txt"))
    monkeypatch.setattr(gk, "ADMIN_NOTIFY_FILE", tmp_path / "alert.txt", raising=True)


# ---------- check-paused ------------------------------------------------

def test_check_paused_no_pause_file_returns_zero():
    assert gk.cmd_check_paused(SimpleNamespace()) == 0


def test_check_paused_respects_future_ttl(tmp_path):
    pause = gk._pause_file()
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.write_text(str(int(time.time() + 600)))
    assert gk.cmd_check_paused(SimpleNamespace()) == 1


def test_check_paused_clears_expired_pause_file():
    pause = gk._pause_file()
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.write_text(str(int(time.time() - 10)))   # already expired
    rc = gk.cmd_check_paused(SimpleNamespace())
    assert rc == 0
    assert not pause.exists()   # auto-cleaned


# ---------- check-rss ----------------------------------------------------

def _ps_rss_line(pid: int, rss_kb: int, model_dir: str) -> str:
    return f"  {pid} {rss_kb} /opt/homebrew/bin/omlx serve --model-dir {model_dir} --port 8080"


def test_rss_within_threshold_returns_zero(monkeypatch):
    fake = "\n".join([
        _ps_rss_line(100, 5 * 1024 * 1024, "/Users/ai/.omlx/models-text"),       # 5 GB
        _ps_rss_line(200, 3 * 1024 * 1024, "/Users/ai/.omlx/models-text-phi4"),  # 3 GB
    ])

    class FakeRes:
        stdout = fake

    monkeypatch.setattr(gk.subprocess, "run", lambda *a, **kw: FakeRes())
    rc = gk.cmd_check_rss(SimpleNamespace(max_model_memory_gb=14.0, mode="night"))
    assert rc == 0
    assert not gk._aborts_log().exists()


def test_rss_exceeds_threshold_returns_three_and_appends_abort(monkeypatch):
    # max=14GB, threshold=14*1.3=18.2GB; 19GB process should trip
    fake = _ps_rss_line(100, int(19 * 1024 * 1024), "/Users/ai/.omlx/models-text")

    class FakeRes:
        stdout = fake

    monkeypatch.setattr(gk.subprocess, "run", lambda *a, **kw: FakeRes())
    rc = gk.cmd_check_rss(SimpleNamespace(max_model_memory_gb=14.0, mode="night"))
    assert rc == 3
    records = gk._read_recent_aborts("omlx_rss_exceeded", window_sec=3600)
    assert len(records) == 1
    assert records[0]["offenders_pids"] == [100]


def test_rss_ignores_non_omlx_processes(monkeypatch):
    fake = (
        "  500 10485760 /usr/bin/python3 /some/other/script.py\n"
        + _ps_rss_line(100, 5 * 1024 * 1024, "/Users/ai/.omlx/models-text")
    )

    class FakeRes:
        stdout = fake

    monkeypatch.setattr(gk.subprocess, "run", lambda *a, **kw: FakeRes())
    rc = gk.cmd_check_rss(SimpleNamespace(max_model_memory_gb=14.0, mode="night"))
    assert rc == 0   # 10GB python3 ignored because no 'omlx serve'


# ---------- register-abort + throttle -----------------------------------

def test_register_abort_appends_record():
    gk.cmd_register_abort(SimpleNamespace(reason="mem_insufficient", mode="night", extra=""))
    records = gk._read_recent_aborts("mem_insufficient", window_sec=3600)
    assert len(records) == 1


def test_abort_threshold_triggers_pause_with_ttl(monkeypatch):
    # threshold=3, window=3600
    monkeypatch.setenv("OMLX_SWITCH_ABORT_THRESHOLD", "3")
    monkeypatch.setenv("OMLX_SWITCH_ABORT_WINDOW_SEC", "3600")
    monkeypatch.setenv("OMLX_SWITCH_PAUSE_TTL_SEC", str(6 * 3600))
    for _ in range(3):
        gk.cmd_register_abort(SimpleNamespace(reason="mem_insufficient", mode="night", extra=""))
    pause_file = gk._pause_file()
    assert pause_file.exists()
    until = int(pause_file.read_text().strip())
    now = time.time()
    # should be ~6h from now (allow 60s drift)
    assert (6 * 3600 - 60) < (until - now) < (6 * 3600 + 60)


def test_pause_ttl_capped_at_max(monkeypatch):
    monkeypatch.setenv("OMLX_SWITCH_ABORT_THRESHOLD", "1")
    monkeypatch.setenv("OMLX_SWITCH_PAUSE_TTL_SEC", str(48 * 3600))  # request 48h
    gk.cmd_register_abort(SimpleNamespace(reason="weird", mode="day", extra=""))
    until = int(gk._pause_file().read_text().strip())
    now = time.time()
    # capped at 24h
    assert (until - now) <= gk.MAX_PAUSE_TTL_SEC + 5


def test_below_threshold_no_pause(monkeypatch):
    monkeypatch.setenv("OMLX_SWITCH_ABORT_THRESHOLD", "3")
    gk.cmd_register_abort(SimpleNamespace(reason="mem_insufficient", mode="night", extra=""))
    gk.cmd_register_abort(SimpleNamespace(reason="mem_insufficient", mode="night", extra=""))
    assert not gk._pause_file().exists()


def test_old_aborts_outside_window_not_counted(monkeypatch, tmp_path):
    monkeypatch.setenv("OMLX_SWITCH_ABORT_THRESHOLD", "3")
    monkeypatch.setenv("OMLX_SWITCH_ABORT_WINDOW_SEC", "3600")
    # 手動寫 2 筆 25h 前的 abort + 1 筆現在的
    log = gk._aborts_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with open(log, "w") as f:
        for ts in (now - 25 * 3600, now - 26 * 3600):
            f.write(json.dumps({"ts_epoch": int(ts), "reason": "mem_insufficient"}) + "\n")
    gk.cmd_register_abort(SimpleNamespace(reason="mem_insufficient", mode="night", extra=""))
    # 舊的不算，只剩 1 筆 → 不觸發 pause
    assert not gk._pause_file().exists()


def test_pause_ttl_expires_and_is_auto_cleared():
    # 模擬已過期的 pause
    pause = gk._pause_file()
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.write_text(str(int(time.time() - 100)))
    rc = gk.cmd_check_paused(SimpleNamespace())
    assert rc == 0
    assert not pause.exists()


# ---------- main() routing ----------------------------------------------

def test_main_check_paused_routes():
    rc = gk.main(["check-paused"])
    assert rc == 0


def test_main_register_abort_routes():
    rc = gk.main(["register-abort", "--reason", "test_reason", "--mode", "day"])
    assert rc == 0
