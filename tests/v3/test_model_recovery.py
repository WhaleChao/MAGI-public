from __future__ import annotations

import json
import os

from magi_v3.model_recovery import assess_omlx_recovery


def _write_gate(tmp_path, *, profile: str, model: str, ok: bool = True):
    expected = "day" if profile.startswith("day") else "night"
    path = tmp_path / "model_live_gate_latest.json"
    path.write_text(
        json.dumps(
            {
                "ok": ok,
                "expected_profile": expected,
                "active_profile": profile,
                "failures": [],
                "endpoints": [{"port": 8080, "ok": True, "model_id": model}],
            }
        ),
        encoding="utf-8",
    )
    os.utime(path, (200.0, 200.0))
    return path


def test_newer_declared_night_e4b_gate_proves_recovery(tmp_path):
    _write_gate(tmp_path, profile="night-e4b-degraded", model="gemma-4-e4b-it-4bit")
    result = assess_omlx_recovery(
        tmp_path, job_id="job_omlx_switch_night", failed_at=100.0
    )
    assert result["recovered"] is True


def test_wrong_model_or_stale_gate_fails_closed(tmp_path):
    _write_gate(tmp_path, profile="night-12b-degraded", model="gemma-4-e4b-it-4bit")
    wrong = assess_omlx_recovery(
        tmp_path, job_id="job_omlx_switch_night", failed_at=100.0
    )
    stale = assess_omlx_recovery(
        tmp_path, job_id="job_omlx_switch_night", failed_at=300.0
    )
    assert wrong["recovered"] is False
    assert stale["recovered"] is False


def test_day_gate_cannot_recover_night_switch(tmp_path):
    _write_gate(tmp_path, profile="day", model="gemma-4-e4b-it-4bit")
    result = assess_omlx_recovery(
        tmp_path, job_id="job_omlx_switch_night", failed_at=100.0
    )
    assert result == {"recovered": False, "reason": "wrong_day_night_profile"}
