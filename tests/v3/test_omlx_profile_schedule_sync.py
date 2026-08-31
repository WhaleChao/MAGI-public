from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from scripts.ops.omlx_profile_policy import (
    DAY_SWITCH_CRON,
    NIGHT_SWITCH_CRON,
    expected_profile_for_minutes,
    profile_transition_in_progress,
)
from gui import magi_menubar


def _cron_jobs() -> list[dict]:
    configured = os.environ.get("MAGI_CRON_JOBS_FILE", "").strip()
    path = Path(configured) if configured else Path(__file__).resolve().parents[2] / "cron_jobs.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return payload


def test_model_switch_schedule_matches_profile_policy() -> None:
    jobs = {str(item.get("id")): item for item in _cron_jobs() if isinstance(item, dict)}
    assert jobs["job_omlx_switch_day"]["cron"] == DAY_SWITCH_CRON
    assert jobs["job_omlx_switch_night"]["cron"] == NIGHT_SWITCH_CRON


def test_model_switch_binds_raw_runtime_to_inert_bytecode_cache() -> None:
    switch = (
        Path(__file__).resolve().parents[2] / "config" / "bin" / "omlx_switch_model.sh"
    ).read_text(encoding="utf-8")
    assert switch.count("plist_set_env PYTHONDONTWRITEBYTECODE 1") == 1
    assert switch.count("plist_set_env PYTHONPYCACHEPREFIX /dev/null") == 1
    auto_start = switch.index('if [ "$MODE" = "auto" ]; then')
    first_auto_exit = switch.index("exit 0", auto_start)
    assert auto_start < switch.index("ensure_python_bytecode_policy", auto_start) < first_auto_exit


def test_day_boundary_has_immediate_switch_and_bounded_grace() -> None:
    assert expected_profile_for_minutes(394)[0] == "night"
    assert expected_profile_for_minutes(395)[0] == "day"
    assert profile_transition_in_progress("night", "day", datetime(2026, 8, 2, 6, 35))
    assert profile_transition_in_progress("night", "day", datetime(2026, 8, 2, 6, 44))
    assert not profile_transition_in_progress("night", "day", datetime(2026, 8, 2, 6, 45))


def test_night_boundary_has_immediate_switch_and_bounded_grace() -> None:
    assert expected_profile_for_minutes(1309)[0] == "day"
    assert expected_profile_for_minutes(1310)[0] == "night"
    assert profile_transition_in_progress("day", "night", datetime(2026, 8, 2, 21, 50))
    assert not profile_transition_in_progress("day", "night", datetime(2026, 8, 2, 22, 0))


def test_menubar_uses_waiting_state_only_during_bounded_transition() -> None:
    text_status = magi_menubar._omlx_text_status(
        "gemma-4-26b-a4b-it-4bit",
        "day",
        "e4b",
        "night",
        transitioning=True,
    )
    cache = {
        "omlx_profile": {"expected_profile": "day", "text_status": text_status},
        "engines": {"文字推理": "gemma-4-26b-a4b-it-4bit"},
    }
    assert text_status["icon"] == "🟡"
    assert text_status["transitioning"] is True
    assert magi_menubar._model_state(cache) == "waiting"


def test_menubar_marks_profile_mismatch_red_after_transition_grace() -> None:
    text_status = magi_menubar._omlx_text_status(
        "gemma-4-26b-a4b-it-4bit",
        "day",
        "e4b",
        "night",
        transitioning=False,
    )
    assert text_status["icon"] == "🔴"
    assert text_status["mismatch"] is True
