from __future__ import annotations


def test_single_host_readiness_does_not_block_internal_use(tmp_path, monkeypatch):
    from api.saas_readiness import build_saas_readiness

    monkeypatch.delenv("MAGI_SAAS_MODE", raising=False)
    monkeypatch.delenv("MAGI_DEPLOYMENT_MODE", raising=False)
    monkeypatch.setenv("MAGI_PUBLIC_SOURCE_ROOT_DIR", str(tmp_path))

    result = build_saas_readiness(root=tmp_path, db_config={"host": "127.0.0.1", "user": "u", "password": "p"})

    assert result["ok"] is True
    assert result["status"] == "single_host"
    assert result["mode"] == "single_host"


def test_formal_saas_readiness_fails_closed_when_required_controls_missing(tmp_path, monkeypatch):
    from api.saas_readiness import build_saas_readiness

    monkeypatch.setenv("MAGI_SAAS_MODE", "1")
    monkeypatch.setenv("MAGI_PUBLIC_SOURCE_ROOT_DIR", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "short")
    monkeypatch.setenv("MAGI_API_KEY", "short")
    monkeypatch.delenv("MAGI_TENANT_ID", raising=False)
    monkeypatch.delenv("MAGI_TENANT_NAME", raising=False)
    monkeypatch.delenv("MAGI_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)

    result = build_saas_readiness(root=tmp_path, db_config={"host": "127.0.0.1", "user": "u", "password": "weak"})

    assert result["ok"] is False
    assert result["status"] == "not_ready"
    assert {
        "tenant_identity",
        "https_public_base",
        "strong_flask_secret",
        "strong_api_key",
        "db_config",
    } <= set(result["failed_keys"])


def test_formal_saas_readiness_passes_with_complete_controls(tmp_path, monkeypatch):
    import api.saas_readiness as readiness

    migration = tmp_path / "migrations" / "versions" / "003_add_tenant_scope.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("-- UP\nSELECT 1;\n-- DOWN\nSELECT 1;\n", encoding="utf-8")

    monkeypatch.setenv("MAGI_SAAS_MODE", "1")
    monkeypatch.setenv("MAGI_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("MAGI_TENANT_NAME", "Alpha Law")
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")
    monkeypatch.setenv("MAGI_FORCE_HTTPS", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "f" * 40)
    monkeypatch.setenv("MAGI_API_KEY", "a" * 40)
    monkeypatch.setenv("MAGI_PUBLIC_SOURCE_ROOT_DIR", str(tmp_path))
    monkeypatch.delenv("MAGI_ALLOW_PUBLIC_REGISTRATION", raising=False)
    monkeypatch.delenv("MAGI_ALLOW_CLOUDFLARE_WEB_UI", raising=False)
    monkeypatch.setattr(
        readiness,
        "inspect_tenant_schema",
        lambda **_kwargs: {"ok": True, "tenant_id": "tenant-alpha", "profiles": [{"label": "auth", "ok": True}]},
    )

    result = readiness.build_saas_readiness(
        root=tmp_path,
        db_config={"host": "127.0.0.1", "user": "tenant_user", "password": "d" * 20},
        app_config={"SESSION_COOKIE_SECURE": True},
    )

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["summary"]["failed_required"] == 0
    assert any(item["key"] == "tenant_schema_applied" and item["ok"] for item in result["checks"])
