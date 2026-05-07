# -*- coding: utf-8 -*-
"""Tests for OSC P1 Checklist endpoints.

Endpoints under test:
  GET    /api/osc/checklists/legal-aid
  POST   /api/osc/checklists/legal-aid
  PUT    /api/osc/checklists/legal-aid/<id>
  DELETE /api/osc/checklists/legal-aid/<id>
  POST   /api/osc/checklists/legal-aid/seed
  GET    /api/osc/checklists/case
  POST   /api/osc/checklists/case
  PUT    /api/osc/checklists/case/<id>
  DELETE /api/osc/checklists/case/<id>

Strategy: monkey-patch _osc_exec so no real DB is needed.
Login bypassed via LOGIN_DISABLED=True.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, call

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_login import LoginManager


@pytest.fixture
def app():
    a = Flask(__name__)
    a.config["TESTING"] = True
    a.config["LOGIN_DISABLED"] = True
    a.secret_key = "test"
    lm = LoginManager()
    lm.init_app(a)
    from api.blueprints.osc_cases import osc_bp
    a.register_blueprint(osc_bp)
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ── 1. Route registration ─────────────────────────────────────────────────────

def test_laf_checklist_routes_registered(app):
    rules = [str(r) for r in app.url_map.iter_rules()]
    assert "/api/osc/checklists/legal-aid" in rules
    assert "/api/osc/checklists/legal-aid/<int:row_id>" in rules
    assert "/api/osc/checklists/legal-aid/seed" in rules
    assert "/api/osc/checklists/case" in rules
    assert "/api/osc/checklists/case/<int:row_id>" in rules


# ── 2. GET requires case_number ───────────────────────────────────────────────

def test_laf_checklist_get_requires_case_number(client):
    r = client.get("/api/osc/checklists/legal-aid")
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False
    assert "case_number" in body["error"]


# ── 3. POST creates item with auto key when item_key is empty ─────────────────

def test_laf_checklist_post_creates_with_auto_key(client):
    insert_calls = []

    def fake_exec(sql, params=(), fetch="all", **kw):
        sql_u = sql.strip().upper()
        if "INSERT" in sql_u:
            insert_calls.append(params)
            return (None, None)
        if "SELECT" in sql_u:
            # return the new row id
            return ((42,), None)
        return (None, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        r = client.post(
            "/api/osc/checklists/legal-aid",
            json={"case_number": "2026-0001", "item_label": "自訂項目"},
        )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["id"] == 42
    # item_key should start with 'custom_'
    assert body["item_key"].startswith("custom_")
    # verify INSERT was called once
    assert len(insert_calls) == 1


# ── 4. seed inserts default items (ON DUPLICATE KEY skips existing) ───────────

def test_laf_checklist_seed_inserts_defaults(client):
    from api.blueprints.osc_cases import _laf_default_checklist_items
    expected_count = len(_laf_default_checklist_items())

    insert_calls = []

    def fake_exec(sql, params=(), fetch="all", **kw):
        sql_u = sql.strip().upper()
        if "SELECT" in sql_u:
            # simulate no existing row → triggers INSERT
            return (None, None)
        if "INSERT" in sql_u:
            insert_calls.append(params)
        return (None, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        r = client.post(
            "/api/osc/checklists/legal-aid/seed",
            json={"case_number": "2026-0001"},
        )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["inserted_count"] == expected_count
    assert body["skipped_count"] == 0
    assert len(insert_calls) == expected_count


# ── 5. PUT updates status ─────────────────────────────────────────────────────

def test_laf_checklist_put_updates_status(client):
    executed_sqls = []

    def fake_exec(sql, params=(), fetch="all", **kw):
        executed_sqls.append(sql.strip())
        return (None, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        r = client.put(
            "/api/osc/checklists/legal-aid/7",
            json={"status": "已備齊"},
        )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert any("UPDATE legal_aid_checklists" in s for s in executed_sqls)


# ── 6. DELETE removes row ─────────────────────────────────────────────────────

def test_laf_checklist_delete_removes_row(client):
    executed = []

    def fake_exec(sql, params=(), fetch="all", **kw):
        executed.append((sql.strip(), params))
        return (None, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        r = client.delete("/api/osc/checklists/legal-aid/9")

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert any("DELETE FROM legal_aid_checklists" in s for s, _ in executed)
    assert any(9 in p for s, p in executed)


# ── 7. GET case checklist only returns is_active=1 rows ──────────────────────

def test_case_checklist_get_filtered_by_active(client):
    # fake returns: SQL must include is_active=1
    executed_sqls = []

    def fake_exec(sql, params=(), fetch="all", **kw):
        executed_sqls.append(sql)
        return ([], None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        r = client.get("/api/osc/checklists/case?case_number=2026-0001")

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    # verify the query filters by is_active=1
    assert any("is_active=1" in s for s in executed_sqls)


# ── 8. DELETE case checklist is soft (is_active=0) ───────────────────────────

def test_case_checklist_delete_is_soft(client):
    executed = []

    def fake_exec(sql, params=(), fetch="all", **kw):
        executed.append((sql.strip(), params))
        return (None, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        r = client.delete("/api/osc/checklists/case/5")

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    # Must UPDATE is_active=0, not DELETE
    assert not any("DELETE" in s.upper() for s, _ in executed)
    assert any("UPDATE case_checklists" in s and "is_active=0" in s for s, _ in executed)
