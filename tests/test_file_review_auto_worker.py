"""Regression tests for the unattended FileReview owner retry cadence."""

from __future__ import annotations

from skills.ops import file_review_auto_worker as worker


def test_portal_defer_uses_bounded_early_retry_and_backoff(monkeypatch):
    monkeypatch.setattr(worker, "_PORTAL_RETRY_BASE_SEC", 60)
    monkeypatch.setattr(worker, "_PORTAL_RETRY_MAX_SEC", 300)

    assert worker._next_cycle_delay(
        {"portal_probe_deferred": True}, 0
    ) == (60, 1)
    assert worker._next_cycle_delay(
        {"portal_probe_deferred": True}, 1
    ) == (120, 2)
    assert worker._next_cycle_delay(
        {"portal_probe_deferred": True}, 4
    ) == (300, 5)


def test_non_deferred_cycle_resets_retry_and_keeps_normal_cadence(monkeypatch):
    monkeypatch.setattr(worker, "INTERVAL_SEC", 600)

    assert worker._next_cycle_delay(
        {"portal_probe_deferred": False}, 4
    ) == (600, 0)
