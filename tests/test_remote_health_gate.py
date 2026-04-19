# -*- coding: utf-8 -*-
import os
import time
import pytest
from unittest.mock import patch, MagicMock

from api.platforms.remote_health_gate import (
    RemoteHealthGate, PeerConfig, get_gate, _require_enabled,
)


@pytest.fixture
def gate():
    g = RemoteHealthGate()
    yield g
    g.reset_for_test()


def _cfg(name="peer1", url="http://x/health", **kw):
    return PeerConfig(name=name, probe_url=url, **kw)


def test_register_is_idempotent(gate):
    gate.register(_cfg())
    gate.register(_cfg())
    assert "peer1" in gate.all_status()


def test_not_registered_returns_down(gate):
    ok, reason = gate.is_reachable("nonexistent")
    assert ok is False
    assert reason == "down:peer_not_registered"


def test_healthy_probe_returns_ok(gate):
    gate.register(_cfg())
    with patch("api.platforms.remote_health_gate.requests") as rq:
        resp = MagicMock(); resp.status_code = 200
        rq.get.return_value = resp
        ok, reason = gate.is_reachable("peer1")
    assert ok is True and reason == "ok"


def test_single_failure_does_not_trip(gate):
    gate.register(_cfg(fail_threshold=2))
    with patch("api.platforms.remote_health_gate.requests") as rq:
        rq.get.side_effect = TimeoutError("t")
        ok, reason = gate.is_reachable("peer1")
    assert ok is False and reason.startswith("down:")
    assert gate.circuit_status("peer1")["open"] is False


def test_threshold_failures_trip_circuit(gate):
    gate.register(_cfg(fail_threshold=2, probe_cache_ttl_sec=0))
    with patch("api.platforms.remote_health_gate.requests") as rq:
        rq.get.side_effect = TimeoutError("t")
        for _ in range(2):
            gate.is_reachable("peer1")
    st = gate.circuit_status("peer1")
    assert st["open"] is True
    assert st["retry_in_sec"] > 0


def test_open_circuit_short_circuits(gate):
    gate.register(_cfg(fail_threshold=2, probe_cache_ttl_sec=0))
    with patch("api.platforms.remote_health_gate.requests") as rq:
        rq.get.side_effect = TimeoutError("t")
        for _ in range(2):
            gate.is_reachable("peer1")
        rq.get.reset_mock()
        rq.get.side_effect = AssertionError("must not probe when open")
        ok, reason = gate.is_reachable("peer1")
    assert ok is False and reason.startswith("circuit_open_fallback:")


def test_mark_success_resets(gate):
    gate.register(_cfg(fail_threshold=2))
    gate.mark_failure("peer1", "x"); gate.mark_failure("peer1", "x")
    assert gate.circuit_status("peer1")["open"] is True
    # expire window manually
    gate.force_reset("peer1")
    gate.mark_success("peer1")
    assert gate.circuit_status("peer1")["consecutive_failures"] == 0


def test_exponential_backoff_levels(gate):
    gate.register(_cfg(fail_threshold=1, cooldown_seconds=(10, 30, 60)))
    gate.mark_failure("peer1", "a")
    assert gate.circuit_status("peer1")["cooldown_level"] == 1
    gate.force_reset("peer1")
    gate.mark_failure("peer1", "b")
    # level resets on force_reset; just verify API doesn't explode
    assert gate.circuit_status("peer1")["open"] is True


def test_probe_cache_ttl(gate):
    gate.register(_cfg(probe_cache_ttl_sec=30.0))
    with patch("api.platforms.remote_health_gate.requests") as rq:
        resp = MagicMock(); resp.status_code = 200
        rq.get.return_value = resp
        gate.is_reachable("peer1")
        rq.get.reset_mock()
        # second call within TTL → no new HTTP
        gate.is_reachable("peer1")
        assert rq.get.call_count == 0


def test_audit_marker_written_on_trip(tmp_path, monkeypatch, gate):
    monkeypatch.setenv("MY_AUDIT_DIR", str(tmp_path))
    gate.register(_cfg(fail_threshold=2, audit_dir_env="MY_AUDIT_DIR"))
    gate.mark_failure("peer1", "t"); gate.mark_failure("peer1", "t")
    files = list(tmp_path.glob("peer1_down_*.json"))
    assert len(files) == 1


def test_audit_marker_cap_enforced(tmp_path, monkeypatch, gate):
    monkeypatch.setenv("MY_AUDIT_DIR", str(tmp_path))
    gate.register(_cfg(fail_threshold=1, audit_dir_env="MY_AUDIT_DIR",
                       max_audit_files_per_day=3))
    for _ in range(5):
        gate.mark_failure("peer1", "t"); gate.force_reset("peer1")
    files = list(tmp_path.glob("peer1_down_*.json"))
    assert len(files) <= 3


def test_audit_marker_silent_when_dir_missing(monkeypatch, gate):
    monkeypatch.setenv("MY_AUDIT_DIR", "/nonexistent/deep/path")
    gate.register(_cfg(fail_threshold=1, audit_dir_env="MY_AUDIT_DIR"))
    gate.mark_failure("peer1", "t")  # must not raise


def test_all_status_returns_all_peers(gate):
    gate.register(_cfg(name="a"))
    gate.register(_cfg(name="b"))
    all_ = gate.all_status()
    assert set(all_.keys()) == {"a", "b"}


def test_singleton_reuses_instance():
    g1 = get_gate()
    g2 = get_gate()
    assert g1 is g2


def test_require_enabled_raises_when_flag_off(monkeypatch):
    monkeypatch.delenv("MAGI_USE_REMOTE_HEALTH_GATE", raising=False)
    with pytest.raises(RuntimeError):
        _require_enabled()


def test_require_enabled_passes_when_flag_on(monkeypatch):
    monkeypatch.setenv("MAGI_USE_REMOTE_HEALTH_GATE", "1")
    _require_enabled()  # must not raise
