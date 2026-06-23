#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single source of truth for MAGI oMLX day/night profile windows."""

from __future__ import annotations

from datetime import datetime

DAY_START_MINUTE = 6 * 60 + 35
DAY_END_MINUTE = 21 * 60 + 50
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
