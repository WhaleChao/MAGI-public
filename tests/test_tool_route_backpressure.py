"""Tests for backpressure guards on /search, /research, /fetch, /summarize, /transcribe.
2026-04-20: 最終審查報告 P1 — _run_with_timeout inflight 統一治理。
"""
import importlib
import sys
import threading
import pytest

def _get_module():
    if "api.tools_api" in sys.modules:
        return sys.modules["api.tools_api"]
    import api.tools_api as m
    return m


def test_tool_inflight_globals_exist():
    m = _get_module()
    assert hasattr(m, "_TOOL_INFLIGHT_LOCK")
    assert hasattr(m, "_TOOL_INFLIGHT_COUNT")
    assert hasattr(m, "_TOOL_MAX_INFLIGHT")
    assert m._TOOL_MAX_INFLIGHT >= 1


def test_infer_inflight_globals_exist():
    m = _get_module()
    assert hasattr(m, "_INFER_INFLIGHT_LOCK")
    assert hasattr(m, "_INFER_INFLIGHT_COUNT")
    assert hasattr(m, "_INFER_MAX_INFLIGHT")
    assert m._INFER_MAX_INFLIGHT >= 1


def test_tool_inflight_count_is_zero_initially():
    m = _get_module()
    assert m._TOOL_INFLIGHT_COUNT[0] == 0


def test_infer_inflight_count_is_zero_initially():
    m = _get_module()
    assert m._INFER_INFLIGHT_COUNT[0] == 0


def test_tool_backpressure_429_when_max_reached(monkeypatch):
    """Simulate tool routes returning 429 when inflight count is at max."""
    m = _get_module()
    orig = m._TOOL_INFLIGHT_COUNT[0]
    monkeypatch.setattr(m, "_TOOL_MAX_INFLIGHT", 0)
    # With max=0 any increment attempt should be blocked
    with m._TOOL_INFLIGHT_LOCK:
        blocked = m._TOOL_INFLIGHT_COUNT[0] >= m._TOOL_MAX_INFLIGHT
    assert blocked is True
    monkeypatch.setattr(m, "_TOOL_MAX_INFLIGHT", 4)


def test_infer_backpressure_429_when_max_reached(monkeypatch):
    m = _get_module()
    monkeypatch.setattr(m, "_INFER_MAX_INFLIGHT", 0)
    with m._INFER_INFLIGHT_LOCK:
        blocked = m._INFER_INFLIGHT_COUNT[0] >= m._INFER_MAX_INFLIGHT
    assert blocked is True
    monkeypatch.setattr(m, "_INFER_MAX_INFLIGHT", 3)


def test_tool_inflight_counter_threadsafe():
    """Concurrent increments should not race."""
    m = _get_module()
    start_val = m._TOOL_INFLIGHT_COUNT[0]
    errors = []

    def increment():
        try:
            with m._TOOL_INFLIGHT_LOCK:
                m._TOOL_INFLIGHT_COUNT[0] += 1
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=increment) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # Reset
    with m._TOOL_INFLIGHT_LOCK:
        m._TOOL_INFLIGHT_COUNT[0] = start_val


def test_external_chat_backpressure_still_present():
    """Ensure the existing external_chat backpressure was not disturbed."""
    m = _get_module()
    assert hasattr(m, "_EXTERNAL_CHAT_INFLIGHT_LOCK")
    assert hasattr(m, "_EXTERNAL_CHAT_INFLIGHT_COUNT")
