# -*- coding: utf-8 -*-
import pytest
from unittest.mock import MagicMock, patch

from skills.bridge import inference_gateway as _ig
from skills.bridge.inference_gateway import InferenceGateway
from api.platforms import remote_health_gate as _rhg


@pytest.fixture(autouse=True)
def _clean_state():
    # reset legacy state
    with _ig._BALTHASAR_CB_STATE["lock"]:
        _ig._BALTHASAR_CB_STATE["down_until"] = 0.0
        _ig._BALTHASAR_CB_STATE["consecutive_failures"] = 0
    # reset new gate
    g = _rhg.get_gate()
    g.reset_for_test()
    yield
    g.reset_for_test()


def _gw():
    gw = InferenceGateway()
    gw.session = MagicMock()
    return gw


def test_flag_off_uses_legacy_path(monkeypatch):
    monkeypatch.delenv("MAGI_USE_REMOTE_HEALTH_GATE", raising=False)
    gw = _gw()
    resp = MagicMock(); resp.status_code = 200
    gw.session.get.return_value = resp
    ok, reason = gw._can_try_remote_balthasar()
    assert ok is True and reason == "ok"
    # gate must NOT have a peer registered
    assert "balthasar" not in _rhg.get_gate().all_status()


def test_flag_on_uses_new_gate(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    gw = _gw()
    with patch("api.platforms.remote_health_gate.requests") as rq:
        resp = MagicMock(); resp.status_code = 200
        rq.get.return_value = resp
        ok, reason = gw._can_try_remote_balthasar()
    assert ok is True and reason == "ok"
    assert "balthasar" in _rhg.get_gate().all_status()


def test_flag_on_force_local_still_wins(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    monkeypatch.setenv("INFERENCE_FORCE_LOCAL", "1")
    gw = _gw()
    ok, reason = gw._can_try_remote_balthasar()
    assert ok is False and reason == "force_local"


def test_flag_on_gate_failure_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    gw = _gw()
    # force gate import to fail by mocking
    resp = MagicMock(); resp.status_code = 200
    gw.session.get.return_value = resp
    with patch("api.platforms.remote_health_gate.get_gate", side_effect=RuntimeError("boom")):
        ok, reason = gw._can_try_remote_balthasar()
    # legacy path ran → returned ok
    assert ok is True


def test_flag_on_trips_gate_not_legacy(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    gw = _gw()
    with patch("api.platforms.remote_health_gate.requests") as rq:
        rq.get.side_effect = TimeoutError("t")
        for _ in range(3):
            gw._can_try_remote_balthasar()
    # gate should show open
    assert _rhg.get_gate().circuit_status("balthasar")["open"] is True
    # legacy state should NOT have been touched by this flow
    assert _ig._BALTHASAR_CB_STATE["consecutive_failures"] == 0
