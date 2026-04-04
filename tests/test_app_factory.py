from __future__ import annotations

import pytest


def test_create_base_app_applies_cookie_hardening(monkeypatch):
    from api import app_factory

    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.delenv("MAGI_FORCE_HTTPS", raising=False)

    app = app_factory.create_base_app()

    assert app.secret_key == "test-secret"
    assert app.config["TEMPLATES_AUTO_RELOAD"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config.get("SESSION_COOKIE_SECURE") in {None, False}


def test_create_base_app_supports_secure_cookie(monkeypatch):
    from api import app_factory

    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.setenv("MAGI_FORCE_HTTPS", "1")

    app = app_factory.create_base_app()

    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_create_base_app_requires_secret(monkeypatch):
    from api import app_factory

    monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
        app_factory.create_base_app()


def test_init_login_manager_sets_login_view(monkeypatch):
    from api import app_factory

    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    app = app_factory.create_base_app()

    login_manager = app_factory.init_login_manager(app)

    assert login_manager.login_view == "login"


def test_register_core_blueprints_exposes_dashboard_routes(monkeypatch):
    from api import app_factory

    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    app = app_factory.create_base_app()
    app_factory.init_login_manager(app)
    app_factory.register_core_blueprints(app)

    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/dashboard" in routes
    assert "/dashboard/nerv" in routes
    assert "/intel" in routes
    assert "/openclaw" in routes


def test_server_registers_runtime_blueprint_routes():
    from api import server

    routes = {rule.rule for rule in server.app.url_map.iter_rules()}
    assert "/ops/process-monitor" in routes
    assert "/api/ops/process-monitor" in routes
    assert "/api/memory/stats" in routes
    assert "/api/memory/recall" in routes
    assert "/api/memory/remember" in routes
    assert "/api/memory/obsidian-sync" in routes
    assert "/api/osc/chat" in routes
    assert "/api/osc/poll" in routes
    assert "/api/osc/judgments_legacy" in routes
    assert "/dashboard/nerv/api/health" in routes
    assert "/api/system-test" in routes
    assert "/api/self-repair" in routes
    assert "/api/nerv/skills" in routes
    assert "/api/status" in routes
    assert "/api/live-log" in routes
    assert "/health" in routes
    assert "/api/transcribe" in routes
