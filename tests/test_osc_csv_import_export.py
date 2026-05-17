# -*- coding: utf-8 -*-
"""Tests for OSC P1 CSV import/export endpoints.

Endpoints under test:
  POST /api/osc/cases/import-csv
  GET  /api/osc/cases/export-csv
  POST /api/osc/clients/import-csv
  GET  /api/osc/clients/export-csv

Strategy: monkey-patch _osc_exec so no real DB is needed.
Login is bypassed via LOGIN_DISABLED=True (same pattern as stamp test).
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

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


def _make_csv(rows: list[dict], fieldnames: list[str] | None = None) -> bytes:
    """Build a utf-8-sig encoded CSV bytes object from list of dicts."""
    buf = io.StringIO()
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


# ──────────────────────────────────────────────────────────────────────────────
# 1. Route registration
# ──────────────────────────────────────────────────────────────────────────────

def test_cases_import_route_registered(app):
    rules = [str(r) for r in app.url_map.iter_rules()]
    assert "/api/osc/cases/import-csv" in rules
    assert "/api/osc/cases/export-csv" in rules
    assert "/api/osc/clients/import-csv" in rules
    assert "/api/osc/clients/export-csv" in rules


# ──────────────────────────────────────────────────────────────────────────────
# 2. Cases import — validation: no file
# ──────────────────────────────────────────────────────────────────────────────

def test_cases_import_validates_no_file(client):
    r = client.post("/api/osc/cases/import-csv", data={})
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False
    assert "file" in body["error"]


# ──────────────────────────────────────────────────────────────────────────────
# 3. Cases import — missing required header
# ──────────────────────────────────────────────────────────────────────────────

def test_cases_import_missing_required_header(client):
    # CSV with no 「當事人」 column
    csv_bytes = _make_csv(
        [{"案件編號": "T001", "案件類型": "民事"}],
        fieldnames=["案件編號", "案件類型"],
    )
    data = {"file": (io.BytesIO(csv_bytes), "test.csv", "text/csv")}
    r = client.post(
        "/api/osc/cases/import-csv",
        data=data,
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False
    assert "當事人" in body["error"]


# ──────────────────────────────────────────────────────────────────────────────
# 4. Cases import — success (mock _osc_exec)
# ──────────────────────────────────────────────────────────────────────────────

def test_cases_import_success(client):
    csv_bytes = _make_csv(
        [{"案件編號": "TEST-001", "當事人": "王小明", "狀態": "進行中"}],
        fieldnames=["案件編號", "當事人", "狀態"],
    )

    def fake_exec(sql, params=(), fetch="all", **kw):
        sql_upper = sql.strip().upper()
        if "SELECT" in sql_upper and "WHERE" in sql_upper:
            # duplicate check — return no row
            return (None, None)
        # INSERT
        return ({"affectedRows": 1}, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        data = {"file": (io.BytesIO(csv_bytes), "cases.csv", "text/csv")}
        r = client.post(
            "/api/osc/cases/import-csv",
            data=data,
            content_type="multipart/form-data",
        )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["imported"] == 1
    assert body["skipped"] == 0


def test_cases_import_blank_case_number_uses_osc_number(client):
    csv_bytes = _make_csv(
        [{"案件編號": "", "當事人": "梁志祥", "狀態": "進行中"}],
        fieldnames=["案件編號", "當事人", "狀態"],
    )
    inserted = {}

    def fake_exec(sql, params=(), fetch="all", **kw):
        sql_upper = sql.strip().upper()
        if "SELECT CASE_NUMBER FROM CASES WHERE CASE_NUMBER LIKE" in sql_upper:
            return ([], None)
        if "SELECT ID FROM CASES WHERE CASE_NUMBER" in sql_upper:
            return (None, None)
        if "INSERT INTO CASES" in sql_upper:
            inserted["params"] = params
            return ({"affectedRows": 1}, None)
        return (None, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        data = {"file": (io.BytesIO(csv_bytes), "cases.csv", "text/csv")}
        r = client.post(
            "/api/osc/cases/import-csv",
            data=data,
            content_type="multipart/form-data",
        )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["imported"] == 1
    params = inserted["params"]
    assert params[1].startswith("20")
    assert "-0001" in params[1]
    assert not params[1].startswith("web-csv-")


# ──────────────────────────────────────────────────────────────────────────────
# 5. Cases export — returns CSV with correct content type
# ──────────────────────────────────────────────────────────────────────────────

def test_cases_export_returns_csv(client):
    fake_rows = [
        {
            "case_number": "C-001",
            "client_name": "陳大文",
            "client_name_en": "",
            "case_type": "民事",
            "case_category": "一般民事",
            "notes": "",
            "case_reason": "借款",
            "status": "進行中",
            "court_case_no": "110年度訴字第1號",
            "court_name": "台北地方法院",
        }
    ]

    def fake_exec(sql, params=(), fetch="all", **kw):
        return (fake_rows, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        r = client.get("/api/osc/cases/export-csv")

    assert r.status_code == 200
    assert "csv" in r.content_type.lower()
    content = r.data.decode("utf-8-sig")
    assert "當事人" in content
    assert "陳大文" in content


# ──────────────────────────────────────────────────────────────────────────────
# 6. Clients import — skips duplicates
# ──────────────────────────────────────────────────────────────────────────────

def test_clients_import_skips_duplicates(client):
    csv_bytes = _make_csv(
        [
            {"姓名": "李四", "電話": "0912345678"},
            {"姓名": "李四", "電話": "0912345678"},  # duplicate
        ],
        fieldnames=["姓名", "電話"],
    )

    call_count = [0]

    def fake_exec(sql, params=(), fetch="all", **kw):
        sql_upper = sql.strip().upper()
        if "SELECT" in sql_upper:
            c = call_count[0]
            call_count[0] += 1
            if c == 0:
                # first row — no duplicate
                return (None, None)
            else:
                # second row — duplicate found
                return ({"id": "webc-existing"}, None)
        # INSERT
        return ({"affectedRows": 1}, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        data = {"file": (io.BytesIO(csv_bytes), "clients.csv", "text/csv")}
        r = client.post(
            "/api/osc/clients/import-csv",
            data=data,
            content_type="multipart/form-data",
        )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["imported"] == 1
    assert body["skipped"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# Smoke test: full import path with 3-row CSV (Flask test_client)
# ──────────────────────────────────────────────────────────────────────────────

def test_cases_import_smoke_three_rows(client):
    """Smoke test: 3-row CSV, 2 succeed, 1 skipped (empty 當事人)."""
    rows = [
        {"案件編號": "S001", "當事人": "甲", "狀態": "進行中"},
        {"案件編號": "S002", "當事人": "乙", "狀態": "進行中"},
        {"案件編號": "S003", "當事人": ""},  # will be skipped
    ]
    csv_bytes = _make_csv(rows, fieldnames=["案件編號", "當事人", "狀態"])

    def fake_exec(sql, params=(), fetch="all", **kw):
        sql_upper = sql.strip().upper()
        if "SELECT" in sql_upper:
            return (None, None)
        return ({"affectedRows": 1}, None)

    with patch("api.blueprints.osc_cases._osc_exec", side_effect=fake_exec):
        data = {"file": (io.BytesIO(csv_bytes), "smoke.csv", "text/csv")}
        r = client.post(
            "/api/osc/cases/import-csv",
            data=data,
            content_type="multipart/form-data",
        )

    body = r.get_json()
    assert body["ok"] is True
    assert body["imported"] == 2
    assert body["skipped"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["row"] == 4  # header=1, row1=2, row2=3, row3=4
