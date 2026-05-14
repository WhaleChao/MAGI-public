from __future__ import annotations

import logging

from flask import Flask


def _make_app():
    from api.request_guards import install_request_guards

    app = Flask(__name__)
    app.config.update(SECRET_KEY="test-secret", TESTING=True)
    install_request_guards(app, logger=logging.getLogger("test-request-guards"))

    @app.get("/openclaw")
    def openclaw():
        return "ok"

    @app.get("/dashboard")
    def dashboard():
        return "dashboard"

    @app.get("/health")
    def health():
        return "health"

    @app.post("/line/webhook")
    def line_webhook():
        return "line"

    return app


def test_localhost_cannot_access_retired_legacy_entrypoints():
    app = _make_app()
    client = app.test_client()

    response = client.get("/openclaw", base_url="http://localhost")
    assert response.status_code == 404


def test_public_host_cannot_access_retired_legacy_entrypoints():
    app = _make_app()
    client = app.test_client()

    response = client.get("/openclaw", headers={"X-Forwarded-Host": "magi.example.com"})
    assert response.status_code == 404


def test_cloudflare_tunnel_allows_whitelisted_routes():
    app = _make_app()
    client = app.test_client()

    response = client.post(
        "/line/webhook",
        headers={"Cf-Ray": "ray-id", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 200

    response = client.get(
        "/health",
        headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 200


def test_cloudflare_tunnel_blocks_non_whitelisted_routes():
    app = _make_app()
    client = app.test_client()

    response = client.get(
        "/dashboard",
        headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 403


def test_cloudflare_tunnel_can_expose_web_ui_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("MAGI_ALLOW_CLOUDFLARE_WEB_UI", "1")
    app = _make_app()
    client = app.test_client()

    response = client.get(
        "/dashboard",
        headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 200


def test_cloudflare_web_ui_flag_does_not_expose_retired_legacy_entrypoints(monkeypatch):
    monkeypatch.setenv("MAGI_ALLOW_CLOUDFLARE_WEB_UI", "1")
    app = _make_app()
    client = app.test_client()

    response = client.get(
        "/openclaw",
        headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 404
