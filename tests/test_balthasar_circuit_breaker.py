# -*- coding: utf-8 -*-
"""Balthasar circuit breaker + Synology Drive fallback audit marker.

Scenario (2026-04-19): Tailscale peer MAGI_BALTHASAR_IP:5002 (Balthasar) unreachable;
every captcha/date_extract probe wasted ~1.2s on timeout. With circuit breaker the
first 2 failures arm the circuit, and subsequent calls short-circuit to "down"
within μs. A JSON audit marker is dropped into Synology Drive for lawyer visibility.

User instruction: "接不上時請讓他可以用synology drive作FALLBACK，結案的就先不管了"
Closed cases are already filtered upstream by `include_closed=False`.
"""
from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from skills.bridge import inference_gateway as _ig
from skills.bridge.inference_gateway import InferenceGateway


@pytest.fixture(autouse=True)
def _reset_balthasar_cb():
    """Reset circuit breaker state before every test to prevent cross-contamination."""
    with _ig._BALTHASAR_CB_STATE["lock"]:
        _ig._BALTHASAR_CB_STATE["down_until"] = 0.0
        _ig._BALTHASAR_CB_STATE["consecutive_failures"] = 0
        _ig._BALTHASAR_CB_STATE["last_reason"] = ""
        _ig._BALTHASAR_CB_STATE["last_tripped_at"] = 0.0
    yield
    with _ig._BALTHASAR_CB_STATE["lock"]:
        _ig._BALTHASAR_CB_STATE["down_until"] = 0.0
        _ig._BALTHASAR_CB_STATE["consecutive_failures"] = 0


def _make_gw() -> InferenceGateway:
    gw = InferenceGateway()
    # Sanitize: force a fresh mock session so we don't hit real Tailscale.
    gw.session = MagicMock()
    return gw


def test_healthy_probe_returns_ok_and_does_not_trip():
    gw = _make_gw()
    response = MagicMock()
    response.status_code = 200
    gw.session.get.return_value = response

    ok, reason = gw._can_try_remote_balthasar()
    assert ok is True
    assert reason == "ok"
    status = gw.balthasar_circuit_status()
    assert status["open"] is False
    assert status["consecutive_failures"] == 0


def test_single_failure_does_not_trip_circuit_by_default():
    gw = _make_gw()
    gw.session.get.side_effect = TimeoutError("ConnectTimeoutError: 1.2s")

    ok, reason = gw._can_try_remote_balthasar()
    assert ok is False
    assert reason.startswith("down:")
    status = gw.balthasar_circuit_status()
    # Default threshold=2, so 1 failure must not open the circuit
    assert status["open"] is False
    assert status["consecutive_failures"] == 1


def test_threshold_failures_trip_circuit_and_short_circuit_later_calls(tmp_path, monkeypatch):
    """Default threshold=2: two timeouts trip the circuit; third call must NOT hit the network."""
    monkeypatch.setenv("SYNOLOGY_BALTHASAR_FALLBACK_DIR", str(tmp_path))
    gw = _make_gw()
    gw.session.get.side_effect = TimeoutError("ConnectTimeoutError: 1.2s")

    for _ in range(2):
        gw._can_try_remote_balthasar()

    status = gw.balthasar_circuit_status()
    assert status["open"] is True
    assert status["consecutive_failures"] == 2
    assert status["retry_in_sec"] > 0

    # Third call: must be short-circuited before reaching session.get
    gw.session.get.reset_mock()
    gw.session.get.side_effect = AssertionError("must not hit network when circuit open")
    ok, reason = gw._can_try_remote_balthasar()
    assert ok is False
    assert reason.startswith("circuit_open_synology_fallback:retry_in_")
    assert gw.session.get.call_count == 0


def test_circuit_trip_writes_synology_audit_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNOLOGY_BALTHASAR_FALLBACK_DIR", str(tmp_path))
    gw = _make_gw()
    gw.session.get.side_effect = TimeoutError("ConnectTimeoutError(MAGI_BALTHASAR_IP port=5002)")

    for _ in range(2):
        gw._can_try_remote_balthasar()

    files = list(tmp_path.glob("balthasar_down_*.json"))
    assert len(files) == 1, f"expected exactly 1 audit marker, found: {files}"
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert "ts" in payload
    assert payload["balthasar_url"].startswith("http://")
    assert "ConnectTimeoutError" in payload["reason"]
    assert payload["ttl_sec"] >= 30
    assert payload["consecutive_failures"] >= 2
    # Human-readable note must mention Synology fallback + closed-case skip rule
    assert "Balthasar" in payload["note"]


def test_successful_probe_after_trip_resets_circuit(tmp_path, monkeypatch):
    """When the circuit TTL expires and a probe succeeds, counters must reset fully."""
    monkeypatch.setenv("SYNOLOGY_BALTHASAR_FALLBACK_DIR", str(tmp_path))
    gw = _make_gw()
    # Trip the circuit
    gw.session.get.side_effect = TimeoutError("tailscale_offline")
    for _ in range(2):
        gw._can_try_remote_balthasar()
    assert gw.balthasar_circuit_status()["open"] is True

    # Manually expire the circuit window (simulate TTL expiry)
    with _ig._BALTHASAR_CB_STATE["lock"]:
        _ig._BALTHASAR_CB_STATE["down_until"] = 0.0

    # Probe recovers
    ok_response = MagicMock()
    ok_response.status_code = 200
    gw.session.get.side_effect = None
    gw.session.get.return_value = ok_response

    ok, reason = gw._can_try_remote_balthasar()
    assert ok is True
    assert reason == "ok"
    status = gw.balthasar_circuit_status()
    assert status["open"] is False
    assert status["consecutive_failures"] == 0


def test_env_threshold_override(monkeypatch, tmp_path):
    """BALTHASAR_CB_FAIL_THRESHOLD=1 should trip on the first failure."""
    monkeypatch.setenv("BALTHASAR_CB_FAIL_THRESHOLD", "1")
    monkeypatch.setenv("SYNOLOGY_BALTHASAR_FALLBACK_DIR", str(tmp_path))
    gw = _make_gw()
    gw.session.get.side_effect = TimeoutError("peer_unreachable")

    gw._can_try_remote_balthasar()
    assert gw.balthasar_circuit_status()["open"] is True


def test_synology_marker_silent_when_mount_missing(monkeypatch):
    """If Synology Drive path does not exist, we must not raise — just log."""
    # Explicit missing path; parent does not exist
    monkeypatch.setenv("SYNOLOGY_BALTHASAR_FALLBACK_DIR", "/nonexistent/synology/path/deep/nested")
    monkeypatch.setenv("BALTHASAR_CB_FAIL_THRESHOLD", "1")
    gw = _make_gw()
    gw.session.get.side_effect = TimeoutError("peer_unreachable")

    # Must not raise
    ok, _ = gw._can_try_remote_balthasar()
    assert ok is False
    # Circuit still tripped even without Synology Drive
    assert gw.balthasar_circuit_status()["open"] is True


def test_resolve_synology_dir_prefers_env():
    gw = _make_gw()
    with patch.dict(os.environ, {"SYNOLOGY_BALTHASAR_FALLBACK_DIR": "/custom/path"}):
        assert gw._resolve_synology_fallback_dir() == "/custom/path"


def test_force_local_bypasses_circuit_entirely(monkeypatch):
    """INFERENCE_FORCE_LOCAL=1 always returns force_local; never hits probe or CB."""
    monkeypatch.setenv("INFERENCE_FORCE_LOCAL", "1")
    gw = _make_gw()
    gw.session.get.side_effect = AssertionError("must not probe when force_local")

    ok, reason = gw._can_try_remote_balthasar()
    assert ok is False
    assert reason == "force_local"


def test_non_200_response_counts_as_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BALTHASAR_CB_FAIL_THRESHOLD", "2")
    monkeypatch.setenv("SYNOLOGY_BALTHASAR_FALLBACK_DIR", str(tmp_path))
    gw = _make_gw()
    response = MagicMock()
    response.status_code = 503
    gw.session.get.return_value = response

    for _ in range(2):
        ok, reason = gw._can_try_remote_balthasar()
        assert ok is False

    status = gw.balthasar_circuit_status()
    assert status["open"] is True
    assert "health_status_503" in status["last_reason"]
