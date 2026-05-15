from __future__ import annotations

from scripts.ops import scheduled_reboot_guard as guard


def test_scheduled_reboot_requires_explicit_env(monkeypatch):
    monkeypatch.delenv("MAGI_ALLOW_SCHEDULED_REBOOT", raising=False)
    monkeypatch.setattr(guard, "_already_rebooted_today", lambda mode: False)
    monkeypatch.setattr(guard, "_active_magi_blockers", lambda: [])
    monkeypatch.setattr(guard, "_office_unsaved_blockers", lambda: [])

    decision = guard.decide("day", apply=True, force_window=True)

    assert decision.ok_to_reboot is False
    assert "MAGI_ALLOW_SCHEDULED_REBOOT_not_set" in decision.reasons


def test_scheduled_reboot_allows_clean_maintenance_window(monkeypatch):
    monkeypatch.setenv("MAGI_ALLOW_SCHEDULED_REBOOT", "1")
    monkeypatch.setattr(guard, "_already_rebooted_today", lambda mode: False)
    monkeypatch.setattr(guard, "_active_magi_blockers", lambda: [])
    monkeypatch.setattr(guard, "_office_unsaved_blockers", lambda: [])

    decision = guard.decide("night", apply=True, force_window=True)

    assert decision.ok_to_reboot is True
    assert decision.mode == "night"
    assert decision.reasons == []


def test_scheduled_reboot_blocks_active_business_job(monkeypatch):
    monkeypatch.setenv("MAGI_ALLOW_SCHEDULED_REBOOT", "1")
    monkeypatch.setattr(guard, "_already_rebooted_today", lambda mode: False)
    monkeypatch.setattr(
        guard,
        "_active_magi_blockers",
        lambda: [{"pid": "123", "marker": "skills/file-review-orchestrator/action.py", "command": "python action.py"}],
    )
    monkeypatch.setattr(guard, "_office_unsaved_blockers", lambda: [])

    decision = guard.decide("day", apply=True, force_window=True)

    assert decision.ok_to_reboot is False
    assert "active_blockers_present" in decision.reasons
    assert decision.blockers[0]["pid"] == "123"
