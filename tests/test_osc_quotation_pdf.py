# -*- coding: utf-8 -*-
"""Tests for OSC P2 – 報價單 PDF 匯出 endpoint.

Endpoints under test:
  GET /api/osc/quotations/<row_id>/export-pdf

Strategy: monkey-patch _osc_exec so no real DB is needed.
Login is bypassed via LOGIN_DISABLED=True.
"""
from __future__ import annotations

import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

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


FAKE_QUOTATION = {
    "id": "q-20260428-abc",
    "client_name": "測試客戶",
    "project_name": "測試案件",
    "date": "2026-04-28",
    "expiry": "2026-05-28",
    "items": json.dumps([
        {"name": "法律諮詢", "qty": 2, "unit_price": 3000, "subtotal": 6000},
    ]),
    "subtotal": 6000,
    "discount": 0,
    "tax": 0,
    "total": 6000,
    "status": "draft",
    "notes": "請於期限前確認",
}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_pdf_route_registered(client):
    """The export-pdf route must be registered and return 404 for missing quotation."""
    def fake_exec(sql, params=(), fetch="all"):
        return (None, None) if fetch == "one" else ([], None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        resp = client.get("/api/osc/quotations/nonexistent/export-pdf")
    assert resp.status_code == 404


def test_pdf_404_when_quotation_missing(client):
    """Should return 404 when quotation does not exist."""
    def fake_exec(sql, params=(), fetch="all"):
        return (None, None) if fetch == "one" else ([], None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        resp = client.get("/api/osc/quotations/no-such-id/export-pdf")
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["ok"] is False


def test_pdf_returns_pdf_content_type(client):
    """Should return application/pdf with %PDF magic bytes."""
    def fake_exec(sql, params=(), fetch="all"):
        if fetch == "one" and "quotations" in sql:
            return (FAKE_QUOTATION, None)
        if fetch == "one":
            return (None, None)
        return ([], None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/quotations/q-20260428-abc/export-pdf")

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data[:4] == b"%PDF"


def test_pdf_handles_empty_items(client):
    """Quotation with no items should still generate a valid PDF."""
    row = dict(FAKE_QUOTATION)
    row["items"] = "[]"
    row["notes"] = ""

    def fake_exec(sql, params=(), fetch="all"):
        if fetch == "one" and "quotations" in sql:
            return (row, None)
        if fetch == "one":
            return (None, None)
        return ([], None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/quotations/q-empty/export-pdf")

    assert resp.status_code == 200
    assert resp.data[:4] == b"%PDF"


def test_pdf_handles_chinese_client_name(client):
    """PDF generation with a Chinese client name should not raise encoding errors."""
    row = dict(FAKE_QUOTATION)
    row["client_name"] = "王小明"

    def fake_exec(sql, params=(), fetch="all"):
        if fetch == "one" and "quotations" in sql:
            return (row, None)
        if fetch == "one":
            return (None, None)
        return ([], None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec), \
         patch("api.blueprints.osc_cases._osc_get_setting_value", return_value=""):
        resp = client.get("/api/osc/quotations/q-chinese/export-pdf")

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data[:4] == b"%PDF"
