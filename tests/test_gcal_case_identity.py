from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GCAL_SYNC = ROOT / "skills" / "osc-orchestrator" / "gcal_sync.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gcal_sync_case_identity_test", GCAL_SYNC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_infer_case_identity_from_client_name(monkeypatch):
    mod = _load_module()
    mod._CASE_IDENTITY_CACHE = None

    def fake_exec(sql, params=(), fetch="all"):
        if "FROM cases" in sql and "WHERE case_number=%s" in sql:
            return None, None
        return [
            {"case_number": "2026-0035", "client_name": "陳鏈棠", "start_date": "2026-04-14", "approval_date": None},
            {"case_number": "2026-0036", "client_name": "王小明", "start_date": "2026-04-01", "approval_date": None},
        ], None

    monkeypatch.setattr(mod, "_osc_exec_sql", fake_exec)

    assert mod._infer_case_identity("陳鏈棠面談＠全家宜蘭縣府店", "") == ("2026-0035", "陳鏈棠")


def test_infer_case_identity_does_not_attach_name_event_before_case_start(monkeypatch):
    mod = _load_module()
    mod._CASE_IDENTITY_CACHE = None

    def fake_exec(sql, params=(), fetch="all"):
        return [
            {"case_number": "2026-0035", "client_name": "陳鏈棠", "start_date": "2026-04-14", "approval_date": None},
        ], None

    monkeypatch.setattr(mod, "_osc_exec_sql", fake_exec)

    assert mod._infer_case_identity("陳鏈棠面談＠全家宜蘭縣府店", "", "2026-04-01") == ("", "")


def test_infer_case_identity_prefers_explicit_case_number(monkeypatch):
    mod = _load_module()

    def fake_exec(sql, params=(), fetch="all"):
        if "WHERE case_number=%s" in sql:
            return {"case_number": "2026-0035", "client_name": "陳鏈棠"}, None
        return [], None

    monkeypatch.setattr(mod, "_osc_exec_sql", fake_exec)

    assert mod._infer_case_identity("[2026-0035] 任意事項", "") == ("2026-0035", "陳鏈棠")


def test_extract_leading_osc_case_number_only_accepts_prefix():
    mod = _load_module()

    assert mod._extract_leading_osc_case_number("[2026-0035] 陳鏈棠面談", "") == "2026-0035"
    assert mod._extract_leading_osc_case_number("2026-0035：陳鏈棠面談", "") == "2026-0035"
    assert mod._extract_leading_osc_case_number("法扶 2026-0035 開庭", "") == ""


def test_infer_osc_owned_event_identity_does_not_use_client_name_fallback(monkeypatch):
    mod = _load_module()

    def fake_exec(sql, params=(), fetch="all"):
        if "WHERE case_number=%s" in sql:
            return {"case_number": params[0], "client_name": "陳鏈棠"}, None
        raise AssertionError(sql)

    monkeypatch.setattr(mod, "_osc_exec_sql", fake_exec)

    assert mod._infer_osc_owned_event_identity("[2026-0035] 任意事項", "") == ("2026-0035", "陳鏈棠")
    assert mod._infer_osc_owned_event_identity("陳鏈棠面談＠全家宜蘭縣府店", "") == ("", "")


def test_infer_laf_reportable_event_identity_allows_countable_laf_events():
    mod = _load_module()
    mod._LAF_IDENTITY_CACHE = [
        {
            "case_number": "2026-0035",
            "client_name": "陳鏈棠",
            "laf_case_no": "1150409-I-004",
            "start_date": "2026-04-09",
            "case_reason": "消債",
        }
    ]

    assert mod._infer_laf_reportable_event_identity("陳鏈棠來所面談", "", "2026-05-01") == ("2026-0035", "陳鏈棠")
    assert mod._infer_laf_reportable_event_identity("陳鏈棠生日", "", "2026-05-01") == ("", "")


def test_infer_laf_reportable_event_identity_skips_ambiguous_client_events():
    mod = _load_module()
    mod._LAF_IDENTITY_CACHE = [
        {
            "case_number": "2026-0035",
            "client_name": "陳鏈棠",
            "laf_case_no": "1150409-I-004",
            "start_date": "2026-04-09",
            "case_reason": "消債",
        },
        {
            "case_number": "2026-0036",
            "client_name": "陳鏈棠",
            "laf_case_no": "1150410-I-005",
            "start_date": "2026-04-10",
            "case_reason": "監護",
        },
    ]

    assert mod._infer_laf_reportable_event_identity("陳鏈棠來所面談", "", "2026-05-01") == ("", "")
    assert mod._infer_laf_reportable_event_identity("陳鏈棠消債來所面談", "", "2026-05-01") == ("2026-0035", "陳鏈棠")


def test_infer_laf_reportable_event_identity_prefers_db_laf_case_over_same_name_regular_case():
    mod = _load_module()
    mod._LAF_IDENTITY_CACHE = [
        {
            "case_number": "2026-0035",
            "client_name": "陳鏈棠",
            "laf_case_no": "1150409-I-004",
            "start_date": "2026-04-09",
            "case_reason": "消債",
            "case_category": "法律扶助案件",
            "legal_aid_status": "進行中",
        },
        {
            "case_number": "2026-0099",
            "client_name": "陳鏈棠",
            "laf_case_no": "",
            "start_date": "2026-04-01",
            "case_reason": "民事損害賠償",
            "case_category": "一般案件",
            "legal_aid_status": "",
        },
    ]

    assert mod._infer_laf_reportable_event_identity("陳鏈棠來所面談", "", "2026-05-01") == ("2026-0035", "陳鏈棠")
