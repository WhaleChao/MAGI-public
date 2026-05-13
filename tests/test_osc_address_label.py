# -*- coding: utf-8 -*-
"""Tests for OSC P2 – 地址標籤 PNG 預覽 + 下載 endpoint.

Endpoints under test:
  GET /api/osc/cases/<row_id>/address-label?mode=preview|download&recipient=court|defendant|laf

Strategy: monkey-patch _osc_exec so no real DB is needed.
Login is bypassed via LOGIN_DISABLED=True.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_login import LoginManager


# ── Fixtures ──────────────────────────────────────────────────────────────────

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


FAKE_CASE = {
    "id": 1,
    "case_number": "114-刑-001",
    "client_name": "測試當事人",
    "court_name": "臺灣臺北地方法院",
    "laf_branch": "臺北分會",
    "notes": "",
}


def _make_exec(case_row=None, court_row=None, opp_rows=None, branch_row=None):
    """Return a fake _osc_exec that serves configured rows."""
    def fake_exec(sql, params=(), fetch="all"):
        sql_lower = sql.lower()
        if "from cases" in sql_lower:
            if fetch == "one":
                return (case_row, None)
            return ([case_row] if case_row else [], None)
        if "from courts" in sql_lower:
            if fetch == "one":
                return (court_row, None)
            return ([court_row] if court_row else [], None)
        if "from opponents" in sql_lower:
            if fetch == "all":
                return (opp_rows if opp_rows is not None else [], None)
            if fetch == "one":
                return ((opp_rows[0] if opp_rows else None), None)
        if "from legal_aid_branches" in sql_lower:
            if fetch == "one":
                return (branch_row, None)
            return ([branch_row] if branch_row else [], None)
        return ([] if fetch == "all" else None, None)
    return fake_exec


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_address_label_route_registered(client):
    """The address-label route must be registered and return 404 for missing case."""
    def fake_exec(sql, params=(), fetch="all"):
        if "from cases" in sql.lower():
            return (None, None) if fetch == "one" else ([], None)
        return ([], None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/cases/9999/address-label?recipient=court")
    assert resp.status_code == 404


def test_address_label_404_when_case_missing(client):
    """Should return 404 when case does not exist."""
    def fake_exec(sql, params=(), fetch="all"):
        if "from cases" in sql.lower():
            return (None, None) if fetch == "one" else ([], None)
        return ([], None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/cases/9999/address-label?recipient=court")
    assert resp.status_code == 404


def test_address_label_400_when_recipient_invalid(client):
    """Should return 400 for unknown recipient."""
    with patch("api.blueprints.osc_cases._osc_exec", side_effect=_make_exec(FAKE_CASE)), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/cases/1/address-label?recipient=unknown")
    assert resp.status_code == 400


def test_address_label_preview_inline_disposition(client):
    """mode=preview should set Content-Disposition: inline."""
    court_row = {"address": "台北市重慶南路一段126號"}
    with patch("api.blueprints.osc_cases._osc_exec",
               side_effect=_make_exec(FAKE_CASE, court_row=court_row)), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/cases/1/address-label?mode=preview&recipient=court")
    assert resp.status_code == 200
    cd = resp.headers.get("Content-Disposition", "")
    assert "inline" in cd


def test_address_label_download_attachment_disposition(client):
    """mode=download should set Content-Disposition: attachment."""
    court_row = {"address": "台北市重慶南路一段126號"}
    with patch("api.blueprints.osc_cases._osc_exec",
               side_effect=_make_exec(FAKE_CASE, court_row=court_row)), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/cases/1/address-label?mode=download&recipient=court")
    assert resp.status_code == 200
    cd = resp.headers.get("Content-Disposition", "")
    assert "attachment" in cd


def test_address_label_returns_png(client):
    """Response should be a valid PNG (magic bytes \\x89PNG)."""
    court_row = {"address": "台北市重慶南路一段126號"}
    with patch("api.blueprints.osc_cases._osc_exec",
               side_effect=_make_exec(FAKE_CASE, court_row=court_row)), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/cases/1/address-label?mode=preview&recipient=court")
    assert resp.status_code == 200
    assert resp.data[:4] == b"\x89PNG"


def test_address_label_400_when_no_court_address_for_court_recipient(client):
    """court recipient with empty court_name should return 400."""
    case_no_court = dict(FAKE_CASE)
    case_no_court["court_name"] = ""
    with patch("api.blueprints.osc_cases._osc_exec",
               side_effect=_make_exec(case_no_court)), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/cases/1/address-label?mode=preview&recipient=court")
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False
