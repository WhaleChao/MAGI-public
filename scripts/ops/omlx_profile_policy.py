#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single source of truth for MAGI oMLX day/night profile windows."""

from __future__ import annotations

from datetime import datetime

DAY_START_MINUTE = 6 * 60 + 35
DAY_END_MINUTE = 21 * 60 + 50
DAY_SWITCH_CRON = "35,55 6 * * *"
NIGHT_SWITCH_CRON = "50 21 * * *"
TRANSITION_GRACE_MINUTES = 10
DAY_MODEL_KEYWORD = "e4b"
DAY_FALLBACK_MODEL_KEYWORD = "e4b"
NIGHT_MODEL_KEYWORD = "26b"
NIGHT_FALLBACK_MODEL_KEYWORD = "12b"


def expected_profile_for_minutes(minutes: int) -> tuple[str, str]:
    """Return the expected profile and model keyword for minutes after midnight."""
    if DAY_START_MINUTE <= minutes < DAY_END_MINUTE:
        return "day", DAY_MODEL_KEYWORD
    return "night", NIGHT_MODEL_KEYWORD


def expected_profile_now(now: datetime | None = None) -> tuple[str, str]:
    """Return the expected profile and model keyword for local time."""
    now = now or datetime.now()
    return expected_profile_for_minutes(now.hour * 60 + now.minute)


def profile_transition_in_progress(
    active_profile: str,
    expected_profile: str,
    now: datetime | None = None,
) -> bool:
    """Return whether a profile mismatch is inside a scheduled switch grace period."""
    active_base = str(active_profile or "").strip().lower().split("-", 1)[0]
    expected = str(expected_profile or "").strip().lower()
    if not active_base or not expected or active_base == expected:
        return False
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    boundary = DAY_START_MINUTE if expected == "day" else DAY_END_MINUTE
    return boundary <= minutes < boundary + TRANSITION_GRACE_MINUTES
