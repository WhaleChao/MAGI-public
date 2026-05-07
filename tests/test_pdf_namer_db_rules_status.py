# -*- coding: utf-8 -*-
"""Tests for pdf-namer doc_rules DB/cache status reporting."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOADER_PATH = ROOT / "skills" / "pdf-namer" / "training_loader.py"


def _load_training_loader(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, str(LOADER_PATH))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _write_bundle(mod, path: Path, rules, *, generated_at: str | None = None, schema_version: int | None = None):
    payload = {
        "schema_version": int(
            schema_version if schema_version is not None else mod._RULES_BUNDLE_SCHEMA_VERSION
        ),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "rules_count": len(rules),
        "checksum": mod._compute_rules_checksum(rules),
        "rules": rules,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_db_credentials(monkeypatch):
    for key in (
        "OSC_DB_PASSWORD",
        "MAGI_REMOTE_DB_PASSWORD",
        "DB_PASSWORD",
        "MAGI_PDF_NAMER_ALLOW_EMPTY_DB_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)


def test_missing_credentials_uses_valid_bundled_rules_without_degrade(monkeypatch, tmp_path):
    bundle_path = tmp_path / "db_rules_cache.json"
    monkeypatch.setenv("MAGI_PDF_NAMER_LOAD_DOTENV", "0")
    monkeypatch.setenv("MAGI_PDF_NAMER_RULES_BUNDLE_PATH", str(bundle_path))
    _clear_db_credentials(monkeypatch)

    mod = _load_training_loader("pdf_namer_training_loader_missing_creds")
    rules_payload = [{
        "doc_type": "判決",
        "filename_template": "{date} 判決",
        "archive_destination_type": "判決書",
        "description": "test",
    }]
    _write_bundle(mod, bundle_path, rules_payload)

    rules = mod.load_doc_rules_from_db()
    status = mod.get_doc_rules_status()

    assert len(rules) == 1
    assert status["source"] == "bundled_cache"
    assert status["degraded"] is False
    assert "missing_db_credentials" in str(status["reason"])


def test_bundled_rules_missing_is_degraded(monkeypatch, tmp_path):
    bundle_path = tmp_path / "missing_bundle.json"
    monkeypatch.setenv("MAGI_PDF_NAMER_LOAD_DOTENV", "0")
    monkeypatch.setenv("MAGI_PDF_NAMER_RULES_BUNDLE_PATH", str(bundle_path))
    _clear_db_credentials(monkeypatch)

    mod = _load_training_loader("pdf_namer_training_loader_missing_bundle")
    _ = mod.load_doc_rules_from_db()
    status = mod.get_doc_rules_status()

    assert status["source"] == "unavailable"
    assert status["degraded"] is True
    assert "bundled_cache_missing" in str(status["reason"])


def test_bundled_rules_stale_is_degraded(monkeypatch, tmp_path):
    bundle_path = tmp_path / "stale_bundle.json"
    monkeypatch.setenv("MAGI_PDF_NAMER_LOAD_DOTENV", "0")
    monkeypatch.setenv("MAGI_PDF_NAMER_RULES_BUNDLE_PATH", str(bundle_path))
    monkeypatch.setenv("MAGI_PDF_NAMER_RULES_BUNDLE_MAX_AGE_DAYS", "1")
    _clear_db_credentials(monkeypatch)

    mod = _load_training_loader("pdf_namer_training_loader_stale_bundle")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    _write_bundle(mod, bundle_path, [{"doc_type": "判決"}], generated_at=old_ts)
    _ = mod.load_doc_rules_from_db()
    status = mod.get_doc_rules_status()

    assert status["source"] == "bundled_cache"
    assert status["degraded"] is True
    assert "bundled_cache_stale" in str(status["reason"])


def test_bundled_rules_schema_mismatch_is_degraded(monkeypatch, tmp_path):
    bundle_path = tmp_path / "schema_mismatch_bundle.json"
    monkeypatch.setenv("MAGI_PDF_NAMER_LOAD_DOTENV", "0")
    monkeypatch.setenv("MAGI_PDF_NAMER_RULES_BUNDLE_PATH", str(bundle_path))
    _clear_db_credentials(monkeypatch)

    mod = _load_training_loader("pdf_namer_training_loader_schema_mismatch")
    _write_bundle(mod, bundle_path, [{"doc_type": "判決"}], schema_version=999)
    _ = mod.load_doc_rules_from_db()
    status = mod.get_doc_rules_status()

    assert status["source"] == "unavailable"
    assert status["degraded"] is True
    assert "bundled_cache_schema_mismatch" in str(status["reason"])


def test_db_available_reports_db_source_not_degraded(monkeypatch):
    monkeypatch.setenv("MAGI_PDF_NAMER_LOAD_DOTENV", "0")

    mod = _load_training_loader("pdf_namer_training_loader_db_available")

    class _Cursor:
        def execute(self, _sql, _params=None):
            return None

        def fetchall(self):
            return [{"doc_type": "判決", "filename_template": "{date} 判決", "archive_destination_type": "判決書", "description": ""}]

        def close(self):
            return None

    class _Conn:
        def cursor(self, dictionary=True):
            assert dictionary is True
            return _Cursor()

        def close(self):
            return None

    monkeypatch.setattr(mod, "_get_db_connection", lambda: _Conn())
    monkeypatch.setattr(mod, "_cache_rules", lambda _rules: None)

    rules = mod.load_doc_rules_from_db()
    status = mod.get_doc_rules_status()

    assert len(rules) == 1
    assert status["source"] == "db"
    assert status["degraded"] is False
