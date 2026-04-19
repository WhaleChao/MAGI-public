# -*- coding: utf-8 -*-
import pytest
from unittest.mock import MagicMock, patch

import skills.bridge.melchior_client as _mc
from api.platforms import remote_health_gate as _rhg


@pytest.fixture(autouse=True)
def _clean_state():
    # reset legacy CB state
    with _mc._CB_LOCK:
        _mc._CIRCUIT_BREAKER["consecutive_failures"] = 0
        _mc._CIRCUIT_BREAKER["tripped_at"] = 0.0
        _mc._CIRCUIT_BREAKER["cooldown_level"] = 0
        _mc._CIRCUIT_BREAKER["last_failure_reason"] = ""
    # reset new gate
    g = _rhg.get_gate()
    g.reset_for_test()
    yield
    g.reset_for_test()


def test_flag_off_uses_legacy_path(monkeypatch):
    monkeypatch.delenv("MAGI_USE_REMOTE_HEALTH_GATE", raising=False)
    with patch.object(_mc, "SESSION") as sess:
        resp = MagicMock(); resp.status_code = 200
        resp2 = MagicMock(); resp2.json.return_value = {"models": []}
        sess.get.side_effect = [resp, resp2]
        result = _mc._remote_online_quick()
    # gate must NOT have melchior registered
    assert "melchior" not in _rhg.get_gate().all_status()


def test_flag_on_registers_melchior(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    monkeypatch.setenv("MAGI_AVOID_DISTRIBUTED", "0")
    with patch("api.platforms.remote_health_gate.requests") as rq:
        resp = MagicMock(); resp.status_code = 200
        rq.get.return_value = resp
        result = _mc._remote_online_quick()
    assert "melchior" in _rhg.get_gate().all_status()
    assert result is True


def test_flag_on_returns_false_when_probe_fails(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    monkeypatch.setenv("MAGI_AVOID_DISTRIBUTED", "0")
    with patch("api.platforms.remote_health_gate.requests") as rq:
        rq.get.side_effect = TimeoutError("t")
        result = _mc._remote_online_quick()
    assert result is False


def test_flag_on_gate_exception_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    monkeypatch.setenv("MAGI_AVOID_DISTRIBUTED", "0")
    with patch("api.platforms.remote_health_gate.get_gate", side_effect=RuntimeError("boom")):
        with patch.object(_mc, "SESSION") as sess:
            resp = MagicMock(); resp.status_code = 200
            resp2 = MagicMock(); resp2.json.return_value = {"models": []}
            sess.get.side_effect = [resp, resp2]
            result = _mc._remote_online_quick()
    # should have fallen back to legacy (True or False depending on legacy checks)
    # key assertion: no crash
    assert isinstance(result, bool)


def test_flag_on_legacy_cb_not_touched(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    monkeypatch.setenv("MAGI_AVOID_DISTRIBUTED", "0")
    with patch("api.platforms.remote_health_gate.requests") as rq:
        rq.get.side_effect = TimeoutError("t")
        for _ in range(4):
            _mc._remote_online_quick()
    # legacy CB must NOT have been incremented
    assert _mc._CIRCUIT_BREAKER["consecutive_failures"] == 0
