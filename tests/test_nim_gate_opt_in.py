# -*- coding: utf-8 -*-
import pytest
from unittest.mock import patch

import skills.bridge.nim_heavy as _nim
from api.platform import remote_health_gate as _rhg


@pytest.fixture(autouse=True)
def _clean_state():
    # reset legacy NIM CB state
    with _nim._cb_lock:
        _nim._cb_state["consecutive_429"] = 0
        _nim._cb_state["cooldown_until_ts"] = 0
        _nim._cb_state["last_error"] = ""
    # reset new gate
    g = _rhg.get_gate()
    g.reset_for_test()
    yield
    g.reset_for_test()


def test_flag_off_uses_legacy_path(monkeypatch):
    monkeypatch.delenv("MAGI_USE_REMOTE_HEALTH_GATE", raising=False)
    ok, msg = _nim._cb_can_call()
    assert ok is True
    # gate must NOT have nvidia_nim registered
    assert "nvidia_nim" not in _rhg.get_gate().all_status()


def test_flag_on_registers_nvidia_nim(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    ok, msg = _nim._cb_can_call()
    assert ok is True  # probe_url=None → always reachable
    assert "nvidia_nim" in _rhg.get_gate().all_status()


def test_flag_on_gate_trips_after_mark_failure(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    # register first
    _nim._cb_can_call()
    gate = _rhg.get_gate()
    # manually mark 3 failures (fail_threshold=3)
    for _ in range(3):
        gate.mark_failure("nvidia_nim", "429")
    ok, _ = _nim._cb_can_call()
    assert ok is False
    # legacy state should NOT be touched
    assert _nim._cb_state["consecutive_429"] == 0


def test_flag_on_gate_exception_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    with patch("api.platform.remote_health_gate.get_gate", side_effect=RuntimeError("boom")):
        ok, msg = _nim._cb_can_call()
    # legacy path ran; no crash
    assert isinstance(ok, bool)


def test_flag_on_legacy_cb_not_touched_on_gate_path(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    gate = _rhg.get_gate()
    # pre-register and trip
    from api.platform.remote_health_gate import PeerConfig
    gate.register(PeerConfig(name="nvidia_nim", probe_url=None, fail_threshold=3, cooldown_seconds=(60, 120, 300)))
    for _ in range(3):
        gate.mark_failure("nvidia_nim", "429")
    ok, _ = _nim._cb_can_call()
    assert ok is False
    # legacy NIM CB consecutive_429 must remain 0
    assert _nim._cb_state["consecutive_429"] == 0
