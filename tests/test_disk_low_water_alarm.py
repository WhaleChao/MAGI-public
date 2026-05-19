from __future__ import annotations

import time

from scripts.ops import disk_low_water_alarm as alarm


def test_high_alert_cooldown_suppresses_repeated_noise(tmp_path, monkeypatch):
    monkeypatch.setattr(alarm, "ALERT_STATE_PATH", tmp_path / "disk_low_state.json")
    monkeypatch.setattr(alarm, "HIGH_ALERT_COOLDOWN_SEC", 6 * 3600)

    assert alarm._should_emit_alert("High", 40.0) is True
    alarm._write_alert_state("High", 40.0, 50.0, emitted=True)

    assert alarm._should_emit_alert("High", 39.5) is False
    assert alarm._should_emit_alert("High", 37.5) is True


def test_critical_alert_uses_shorter_cooldown(tmp_path, monkeypatch):
    state_path = tmp_path / "disk_low_state.json"
    monkeypatch.setattr(alarm, "ALERT_STATE_PATH", state_path)
    monkeypatch.setattr(alarm, "CRITICAL_ALERT_COOLDOWN_SEC", 3600)
    alarm._write_alert_state("Critical", 14.0, 15.0, emitted=True)

    assert alarm._should_emit_alert("Critical", 13.8) is False

    state = alarm._read_alert_state()
    state["ts"] = time.time() - 3601
    state_path.write_text(alarm.json.dumps(state), encoding="utf-8")
    assert alarm._should_emit_alert("Critical", 13.8) is True
