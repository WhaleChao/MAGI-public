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
            {"case_number": "2026-0035", "client_name": "陳鏈棠"},
            {"case_number": "2026-0036", "client_name": "王小明"},
        ], None

    monkeypatch.setattr(mod, "_osc_exec_sql", fake_exec)

    assert mod._infer_case_identity("陳鏈棠面談＠全家宜蘭縣府店", "") == ("2026-0035", "陳鏈棠")


def test_infer_case_identity_prefers_explicit_case_number(monkeypatch):
    mod = _load_module()

    def fake_exec(sql, params=(), fetch="all"):
        if "WHERE case_number=%s" in sql:
            return {"case_number": "2026-0035", "client_name": "陳鏈棠"}, None
        return [], None

    monkeypatch.setattr(mod, "_osc_exec_sql", fake_exec)

    assert mod._infer_case_identity("[2026-0035] 任意事項", "") == ("2026-0035", "陳鏈棠")
