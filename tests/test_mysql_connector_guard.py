# -*- coding: utf-8 -*-
"""Stability guard tests for mysql-connector on macOS/Python 3.14."""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

from api.mysql_connector_guard import install_mysql_cext_blocker, patch_mysql_connector_for_stability


REPO_ROOT = Path(__file__).resolve().parents[1]


def _install_fake_mysql(monkeypatch):
    calls = []

    def connect(*args, **kwargs):
        calls.append(dict(kwargs))
        return kwargs

    mysql_mod = types.ModuleType("mysql")
    connector_mod = types.ModuleType("mysql.connector")
    connector_mod.connect = connect  # type: ignore[attr-defined]
    mysql_mod.connector = connector_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mysql", mysql_mod)
    monkeypatch.setitem(sys.modules, "mysql.connector", connector_mod)
    return connector_mod, calls


def test_mysql_guard_forces_pure_python_even_when_old_code_requests_cext(monkeypatch):
    connector_mod, calls = _install_fake_mysql(monkeypatch)
    monkeypatch.setenv("MAGI_MYSQL_USE_PURE", "1")
    monkeypatch.delenv("MAGI_MYSQL_ALLOW_CEXT", raising=False)

    assert patch_mysql_connector_for_stability() is True
    connector_mod.connect(use_pure=False)  # type: ignore[attr-defined]

    assert calls[-1]["use_pure"] is True


def test_mysql_guard_allows_cext_only_by_explicit_opt_in(monkeypatch):
    connector_mod, calls = _install_fake_mysql(monkeypatch)
    monkeypatch.setenv("MAGI_MYSQL_USE_PURE", "1")
    monkeypatch.setenv("MAGI_MYSQL_ALLOW_CEXT", "1")

    assert patch_mysql_connector_for_stability() is True
    connector_mod.connect(use_pure=False)  # type: ignore[attr-defined]

    assert calls[-1]["use_pure"] is False


def test_sitecustomize_blocks_mysql_cext_before_connector_import():
    env = os.environ.copy()
    env.pop("MAGI_MYSQL_ALLOW_CEXT", None)
    env["PYTHONPATH"] = f"{REPO_ROOT}:{env.get('PYTHONPATH', '')}"
    script = """
import sys
import mysql.connector
print('cext_loaded=' + str('_mysql_connector' in sys.modules))
print('guarded=' + str(bool(getattr(mysql.connector.connect, '__magi_mysql_guard__', False))))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=True,
    )
    assert "cext_loaded=False" in proc.stdout
    assert "guarded=True" in proc.stdout


def test_importing_db_helper_does_not_load_mysql_cext():
    env = os.environ.copy()
    env.pop("MAGI_MYSQL_ALLOW_CEXT", None)
    script = """
import sys
import api.db_helper
print('cext_loaded=' + str('_mysql_connector' in sys.modules))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=True,
    )
    assert "cext_loaded=False" in proc.stdout


def test_install_mysql_cext_blocker_is_idempotent():
    assert install_mysql_cext_blocker() is True
    assert install_mysql_cext_blocker() is True
