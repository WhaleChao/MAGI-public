# -*- coding: utf-8 -*-
import os
import sys


_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "skills", "osc-orchestrator"))

from osc_headless.gcal_dedup import (  # type: ignore
    build_dedup_key_from_gcal_event,
    build_dedup_key_from_todo,
    classify_event_kind,
    confidence_for_match,
    is_invalid_case_key,
    normalize_case_key,
)


def test_invalid_case_key_rejects_bare_year():
    assert is_invalid_case_key("2025") is True
    assert is_invalid_case_key("2026") is True
    assert is_invalid_case_key("2025-0081") is False


def test_normalize_case_key_prefers_standard_case_number():
    case_key, source = normalize_case_key({"case_number": " 2025-0081 "})
    assert case_key == "2025-0081"
    assert source == "case_number"


def test_build_dedup_key_from_todo_stable_for_noise_variants():
    todo_a = {
        "case_number": "2025-0081",
        "todo_type": "開庭",
        "todo_date": "2026-05-20",
        "todo_time": "10:00:00",
        "description": "⚖️ 開庭 2025-0081 — 花蓮地院",
    }
    todo_b = {
        "case_number": "2025-0081",
        "todo_type": "開庭",
        "todo_date": "2026-05-20",
        "todo_time": "10:00",
        "description": "[事務所] 開庭 花蓮地院 2025-0081",
    }
    assert build_dedup_key_from_todo(todo_a) == build_dedup_key_from_todo(todo_b)


def test_build_dedup_key_from_gcal_event_contains_case_kind_date_time():
    event = {
        "summary": "⚖️ 王大明 2025-0081 開庭",
        "description": "地點：花蓮地院",
        "start": {"dateTime": "2026-05-20T10:00:00+08:00"},
    }
    key = build_dedup_key_from_gcal_event(event)
    assert "case:2025-0081" in key
    assert "kind:hearing" in key
    assert "date:2026-05-20" in key
    assert "time:10:00" in key


def test_confidence_for_match_high_and_low_cases():
    a = {
        "case_number": "2025-0081",
        "todo_type": "開庭",
        "todo_date": "2026-05-20",
        "todo_time": "10:00",
        "summary": "王大明 開庭",
    }
    b = {
        "summary": "⚖️ 王大明 2025-0081 開庭",
        "start": {"dateTime": "2026-05-20T10:00:00+08:00"},
    }
    c = {
        "summary": "⚖️ 王大明 2025-0081 開庭",
        "start": {"dateTime": "2026-05-20T15:00:00+08:00"},
    }
    assert confidence_for_match(a, b) == "high"
    assert confidence_for_match(a, c) in {"low", "medium"}


def test_classify_event_kind_keywords():
    assert classify_event_kind("準備程序 開庭") == "hearing"
    assert classify_event_kind("應於10日內補正") == "deadline"
    assert classify_event_kind("律見 會議") == "meeting"

