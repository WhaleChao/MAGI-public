# -*- coding: utf-8 -*-
"""Tests for the OSC-style LAF consumer-debt required checklist."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

from flask import Flask

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _install_flask_login_stub():
    if "flask_login" in sys.modules:
        return
    mod = types.ModuleType("flask_login")
    mod.login_required = lambda fn: fn
    mod.current_user = types.SimpleNamespace(id="test-user", role="tester")
    sys.modules["flask_login"] = mod


def _build_app():
    _install_flask_login_stub()
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"

    from api.blueprints.osc_cases import osc_bp

    app.register_blueprint(osc_bp)
    return app


def test_debt_required_get_returns_osc_spec_and_laf_number_candidates():
    app = _build_app()

    def fake_exec(sql, params=(), fetch="all", **_kw):
        if "FROM cases" in sql:
            return {
                "id": 17,
                "case_number": "113消債更字第1號",
                "case_type": "消債",
                "laf_case_no": "",
            }, {"host": "test"}
        if "FROM legal_aid_checklists" in sql:
            return [
                {
                    "id": 3,
                    "case_number": "113消債更字第1號",
                    "item_key": "household_reg_parents",
                    "item_label": "父母之全戶戶籍謄本(記事勿省略)",
                    "status": "待補",
                    "notes": "",
                }
            ], {"host": "test"}
        return None, {"host": "test"}

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec), patch(
        "api.blueprints.osc_cases._laf_number_candidates_for_case",
        return_value={"candidates": ["1150320-E-014"], "source": "接案通知書", "scanned_roots": []},
    ):
        r = app.test_client().get("/api/osc/checklists/debt-required?case_number=113消債更字第1號")

    body = r.get_json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["spec"]["status_options"] == ["待補", "已繳", "免附"]
    assert any(t["key"] == "dependents_parents" for t in body["spec"]["toggles"])
    assert any(section["title"] == "扶養父母資料" for section in body["spec"]["sections"])
    assert body["laf_number_candidates"]["candidates"] == ["1150320-E-014"]


def test_debt_required_save_upserts_visible_items_and_prunes_inactive_debt_rows():
    app = _build_app()
    inserts = []
    deletes = []

    def fake_exec(sql, params=(), fetch="all", **_kw):
        if sql.lstrip().startswith("INSERT INTO legal_aid_checklists"):
            inserts.append(params)
        if sql.lstrip().startswith("SELECT item_key FROM legal_aid_checklists"):
            return [
                {"item_key": "household_reg_self"},
                {"item_key": "tax_list_self"},
                {"item_key": "custom_item_9"},
                {"item_key": "unrelated_existing_key"},
            ], {"host": "test"}
        if sql.lstrip().startswith("DELETE FROM legal_aid_checklists"):
            deletes.append(params)
        return None, {"host": "test"}

    payload = {
        "case_number": "113消債更字第1號",
        "items": [
            {"item_key": "household_reg_self", "item_label": "最近一個月戶籍謄本", "status": "已繳"},
            {"item_key": "custom_item_1", "item_label": "補充說明書", "status": "待補", "notes": "請簽名"},
        ],
    }
    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        r = app.test_client().post("/api/osc/checklists/debt-required/save", json=payload)

    body = r.get_json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["saved_count"] == 2
    assert len(inserts) == 2
    assert ("113消債更字第1號", "tax_list_self") in deletes
    assert ("113消債更字第1號", "custom_item_9") in deletes
    assert ("113消債更字第1號", "unrelated_existing_key") not in deletes


def test_laf_number_sync_uses_single_candidate_when_manual_number_is_empty():
    app = _build_app()
    updates = []

    def fake_exec(sql, params=(), fetch="all", **_kw):
        if "FROM cases WHERE id" in sql:
            return {"id": 17, "case_number": "113消債更字第1號", "folder_path": "/tmp/case"}, {"host": "test"}
        if sql.lstrip().startswith("UPDATE cases"):
            updates.append(params)
        return None, {"host": "test"}

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec), patch(
        "api.blueprints.osc_cases._laf_number_candidates_for_case",
        return_value={"candidates": ["1150320-E-014"], "source": "接案通知書", "scanned_roots": []},
    ):
        r = app.test_client().post("/api/osc/cases/17/laf-number/sync", json={})

    body = r.get_json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["laf_case_no"] == "1150320-E-014"
    assert updates == [("1150320-E-014", "1150320-E-014", 17)]
