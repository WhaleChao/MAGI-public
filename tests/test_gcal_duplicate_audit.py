# -*- coding: utf-8 -*-
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "audit_gcal_duplicates.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("audit_gcal_duplicates_test", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_group_confidence_high_same_calendar_case_date_time():
    mod = _load_module()
    rows = [
        {"calendar_id": "primary", "case_key": "2025-0081", "date": "2026-05-20", "time": "10:00"},
        {"calendar_id": "primary", "case_key": "2025-0081", "date": "2026-05-20", "time": "10:00"},
    ]
    conf, reason = mod._group_confidence(rows)
    assert conf == "high"
    assert reason == "same_calendar_same_case_kind_date_time"


def test_group_confidence_low_cross_calendar():
    mod = _load_module()
    rows = [
        {"calendar_id": "primary", "case_key": "2025-0081", "date": "2026-05-20", "time": "10:00"},
        {"calendar_id": "team@example.com", "case_key": "2025-0081", "date": "2026-05-20", "time": "10:00"},
    ]
    conf, _reason = mod._group_confidence(rows)
    assert conf == "low"


def test_eligible_for_delete_requires_high_confidence_same_calendar_valid_case():
    mod = _load_module()
    high_group = {"confidence": "high", "same_calendar": True, "valid_case_key": True}
    cross_group = {"confidence": "high", "same_calendar": False, "valid_case_key": True}
    weak_case = {"confidence": "high", "same_calendar": True, "valid_case_key": False}
    assert mod._eligible_for_delete(high_group, "high") is True
    assert mod._eligible_for_delete(cross_group, "high") is False
    assert mod._eligible_for_delete(weak_case, "high") is False

