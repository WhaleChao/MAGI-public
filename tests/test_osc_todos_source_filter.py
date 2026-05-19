# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask
from flask_login import LoginManager

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def client(monkeypatch):
    from api.blueprints import osc_cases

    calls: list[tuple[str, tuple, str]] = []

    def fake_exec(sql, params=(), fetch="all"):
        calls.append((sql, params, fetch))
        return ([], None)

    monkeypatch.setattr(osc_cases, "_osc_exec", fake_exec)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["LOGIN_DISABLED"] = True
    app.secret_key = "test"
    LoginManager().init_app(app)
    app.register_blueprint(osc_cases.osc_bp)
    test_client = app.test_client()
    test_client.calls = calls
    return test_client


def test_todos_source_osc_excludes_google_calendar_import(client):
    resp = client.get("/api/osc/todos?source=osc&limit=5")

    assert resp.status_code == 200
    sql = client.calls[-1][0]
    assert "source_file NOT LIKE 'gcal_import%%'" in sql
    assert "COALESCE(todo_type, '') <> '行事曆事件'" in sql


def test_todos_source_gcal_returns_calendar_imports_and_calendar_todos(client):
    resp = client.get("/api/osc/todos?source=gcal&limit=5")

    assert resp.status_code == 200
    sql = client.calls[-1][0]
    assert "source_file LIKE 'gcal_import%%'" in sql
    assert "todo_type='行事曆事件'" in sql
    assert "source_file NOT LIKE" not in sql


def test_todos_without_source_keeps_legacy_all_sources(client):
    resp = client.get("/api/osc/todos?limit=5")

    assert resp.status_code == 200
    sql = client.calls[-1][0]
    assert "gcal_import" not in sql
