# -*- coding: utf-8 -*-
"""Tests for OSC P3 Auto Backup / Restore endpoints.

Endpoints under test:
  GET    /api/osc/backups
  POST   /api/osc/backups
  POST   /api/osc/backups/<filename>/restore
  DELETE /api/osc/backups/<filename>

Strategy: monkey-patch _osc_exec so no real DB is needed.
Login is bypassed via LOGIN_DISABLED=True (same pattern as csv import test).
"""
from __future__ import annotations

import json
import os
import sys
import time
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


def _fake_osc_exec(sql, params=(), fetch="all"):
    """Return empty result set for any query."""
    if fetch == "all":
        return [], None
    if fetch == "one":
        return None, None
    return None, None


def _fake_osc_exec_with_rows(rows_by_table):
    """Factory: returns rows for SELECT * FROM <table>."""
    def _exec(sql, params=(), fetch="all"):
        sql_upper = sql.strip().upper()
        if "SELECT *" in sql_upper or "SELECT COUNT" in sql_upper:
            for tbl, rows in rows_by_table.items():
                if f"`{tbl.upper()}`" in sql_upper or f" {tbl.upper()} " in sql_upper or tbl.upper() in sql_upper:
                    if "COUNT(*)" in sql_upper:
                        return {"cnt": 0}, None
                    return rows, None
        return ([], None) if fetch == "all" else (None, None)
    return _exec


# ── Test 1: Route registration ─────────────────────────────────────────────────

def test_backup_routes_registered(app):
    """All 4 backup routes must be registered."""
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert "/api/osc/backups" in rules, "GET/POST /api/osc/backups missing"
    # Check restore and delete exist (parameterised routes)
    restore_found = any("/api/osc/backups/" in r and "restore" in r for r in rules)
    delete_found = any("/api/osc/backups/" in r and "restore" not in r and "<" in r for r in rules)
    assert restore_found, "/api/osc/backups/<filename>/restore missing"
    assert delete_found, "/api/osc/backups/<filename> DELETE missing"


# ── Test 2: Create backup writes file ─────────────────────────────────────────

def test_backup_create_writes_file(tmp_path, app, client):
    """POST /api/osc/backups writes a JSON file to BACKUP_DIR."""
    import api.blueprints.osc_cases as mod

    fake_rows = [{"id": 1, "case_number": "T001", "client_name": "王小明"}]

    with patch.object(mod, "_osc_backup_dir", return_value=tmp_path), \
         patch.object(mod, "_osc_exec", side_effect=_fake_osc_exec_with_rows({"cases": fake_rows})):

        resp = client.post(
            "/api/osc/backups",
            data=json.dumps({"label": "test"}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["filename"].startswith("backup_")
    assert body["filename"].endswith(".json")

    # File must exist in tmp_path
    written = list(tmp_path.glob("backup_*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert "tables" in payload
    assert "table_counts" in payload


# ── Test 3: Prune to 7 ────────────────────────────────────────────────────────

def test_backup_create_prunes_to_seven(tmp_path, app, client):
    """If 8 backup files exist before create, oldest is pruned so max=7."""
    import api.blueprints.osc_cases as mod

    # Create 8 fake backup files with distinct mtimes
    for i in range(8):
        p = tmp_path / f"backup_2026010{i}_120000_old.json"
        p.write_text(json.dumps({"version": 1, "tables": {}, "table_counts": {}}), encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))  # mtime ascending

    oldest = tmp_path / "backup_20260100_120000_old.json"

    with patch.object(mod, "_osc_backup_dir", return_value=tmp_path), \
         patch.object(mod, "_osc_exec", side_effect=_fake_osc_exec):

        resp = client.post(
            "/api/osc/backups",
            data=json.dumps({"label": "prune_test"}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    remaining = list(tmp_path.glob("backup_*.json"))
    assert len(remaining) <= 7, f"Expected ≤7 backups, got {len(remaining)}"


# ── Test 4: List returns metadata ─────────────────────────────────────────────

def test_backup_list_returns_metadata(tmp_path, app, client):
    """GET /api/osc/backups returns size and table_counts for each file."""
    import api.blueprints.osc_cases as mod

    fake_payload = {
        "version": 1,
        "created_at": "2026-04-28T03:00:00+08:00",
        "label": "auto",
        "tables": {"cases": [{"id": 1}]},
        "table_counts": {"cases": 1},
    }
    p = tmp_path / "backup_20260428_030000_auto.json"
    p.write_text(json.dumps(fake_payload, ensure_ascii=False), encoding="utf-8")

    with patch.object(mod, "_osc_backup_dir", return_value=tmp_path):
        resp = client.get("/api/osc/backups")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    items = body["items"]
    assert len(items) == 1
    assert items[0]["filename"] == "backup_20260428_030000_auto.json"
    assert items[0]["size_bytes"] > 0
    assert items[0]["table_counts"] == {"cases": 1}


# ── Test 5: dry_run does not INSERT ───────────────────────────────────────────

def test_backup_restore_dry_run_no_writes(tmp_path, app, client):
    """dry_run=true must not issue any INSERT statement."""
    import api.blueprints.osc_cases as mod

    fake_payload = {
        "version": 1,
        "tables": {"cases": [{"id": 1, "case_number": "T001"}]},
        "table_counts": {"cases": 1},
    }
    p = tmp_path / "backup_20260428_030000_dryrun.json"
    p.write_text(json.dumps(fake_payload, ensure_ascii=False), encoding="utf-8")

    executed_sqls = []

    def capturing_exec(sql, params=(), fetch="all"):
        executed_sqls.append(sql)
        if "COUNT(*)" in sql.upper():
            return {"cnt": 1}, None
        return [], None

    with patch.object(mod, "_osc_backup_dir", return_value=tmp_path), \
         patch.object(mod, "_osc_exec", side_effect=capturing_exec):

        resp = client.post(
            f"/api/osc/backups/backup_20260428_030000_dryrun.json/restore",
            data=json.dumps({"dry_run": True}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["mode"] == "dry_run"

    insert_sqls = [s for s in executed_sqls if "INSERT" in s.upper()]
    assert len(insert_sqls) == 0, f"dry_run must not INSERT; got: {insert_sqls}"


# ── Test 6: Restore uses INSERT IGNORE ────────────────────────────────────────

def test_backup_restore_uses_insert_ignore(tmp_path, app, client):
    """confirm=true must use INSERT IGNORE (not INSERT INTO without IGNORE)."""
    import api.blueprints.osc_cases as mod

    fake_payload = {
        "version": 1,
        "tables": {"cases": [{"id": 1, "case_number": "T001"}]},
        "table_counts": {"cases": 1},
    }
    p = tmp_path / "backup_20260428_030000_confirm.json"
    p.write_text(json.dumps(fake_payload, ensure_ascii=False), encoding="utf-8")

    executed_sqls = []

    def capturing_exec(sql, params=(), fetch="all"):
        executed_sqls.append(sql)
        mock_result = MagicMock()
        mock_result.rowcount = 1
        return mock_result, None

    with patch.object(mod, "_osc_backup_dir", return_value=tmp_path), \
         patch.object(mod, "_osc_exec", side_effect=capturing_exec):

        resp = client.post(
            f"/api/osc/backups/backup_20260428_030000_confirm.json/restore",
            data=json.dumps({"confirm": True}),
            content_type="application/json",
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["mode"] == "restore"

    insert_sqls = [s for s in executed_sqls if "INSERT" in s.upper()]
    assert len(insert_sqls) >= 1, "At least one INSERT must be issued"
    for sql in insert_sqls:
        assert "IGNORE" in sql.upper(), f"INSERT must use IGNORE, got: {sql}"
