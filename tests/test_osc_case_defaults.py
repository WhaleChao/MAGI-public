from __future__ import annotations

from api.osc.case_defaults import (
    case_uses_consumer_debt_lawyer,
    db_settings_getter,
    default_case_lawyer,
    normalize_case_lawyer,
)


def test_default_case_lawyer_uses_debt_setting_for_consumer_debt():
    values = {
        "default_lawyer": "一般承辦",
        "default_debt_lawyer": "消債承辦",
    }

    assert default_case_lawyer(
        case_type="消費者債務清理",
        case_reason="更生",
        settings_getter=lambda key, default="": values.get(key, default),
    ) == "消債承辦"


def test_default_case_lawyer_uses_regular_setting_for_non_debt():
    values = {
        "default_lawyer": "一般承辦",
        "default_debt_lawyer": "消債承辦",
    }

    assert default_case_lawyer(
        case_type="民事",
        case_reason="拆屋還地",
        settings_getter=lambda key, default="": values.get(key, default),
    ) == "一般承辦"


def test_normalize_case_lawyer_replaces_demo_placeholder():
    values = {"default_lawyer": "一般承辦"}

    assert normalize_case_lawyer(
        "範例律師",
        case_type="民事",
        settings_getter=lambda key, default="": values.get(key, default),
    ) == "一般承辦"


def test_missing_settings_never_fall_back_to_demo_placeholder():
    assert normalize_case_lawyer("範例律師", case_type="民事", env={}) == ""


def test_debt_detection_includes_short_markers():
    assert case_uses_consumer_debt_lawyer("法律扶助案件", "更生")
    assert case_uses_consumer_debt_lawyer("消債")


def test_db_settings_getter_reads_legacy_db_manager():
    class FakeDB:
        def fetch_one(self, sql, params, as_dict=True):
            assert "settings" in sql
            assert params == ("default_lawyer",)
            return {"value": "一般承辦"}

    assert db_settings_getter(FakeDB())("default_lawyer", "") == "一般承辦"
