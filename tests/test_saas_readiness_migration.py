from __future__ import annotations

from pathlib import Path

from api import saas_readiness


def test_formal_saas_release_contains_versioned_tenant_migration(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    migration = root / "migrations/versions/003_add_tenant_scope.sql"

    assert migration.is_file()
    source = migration.read_text(encoding="utf-8")
    for required in (
        "CREATE TABLE IF NOT EXISTS tenants",
        "CREATE TABLE IF NOT EXISTS tenant_memberships",
        "CREATE TABLE IF NOT EXISTS tenant_api_keys",
        "CREATE TABLE IF NOT EXISTS tenant_storage_roots",
        "CALL magi_add_tenant_scope('users', 'default_tenant_id')",
        "CALL magi_add_tenant_scope('cases', 'tenant_id')",
        "CALL magi_add_tenant_scope('court_judgments', 'tenant_id')",
    ):
        assert required in source

    monkeypatch.setenv("MAGI_SAAS_MODE", "1")
    monkeypatch.setenv("MAGI_DEPLOYMENT_MODE", "formal_saas")
    monkeypatch.setenv("MAGI_TENANT_ID", "magi-primary")
    monkeypatch.setenv("MAGI_TENANT_NAME", "MAGI Primary")
    monkeypatch.setattr(
        saas_readiness,
        "inspect_tenant_schema",
        lambda **_kwargs: {"ok": True, "profiles": [{"ok": True}]},
    )

    payload = saas_readiness.build_saas_readiness(
        root=root,
        db_config={"host": "127.0.0.1", "user": "magi", "password": "strong-password"},
    )
    migration_check = next(
        check for check in payload["checks"] if check["key"] == "tenant_schema_migration"
    )

    assert migration_check["ok"] is True
    assert migration_check["status"] == "pass"
