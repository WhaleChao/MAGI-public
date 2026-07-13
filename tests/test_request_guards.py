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

    @app.get("/osc")
    def osc():
        return "osc"

    @app.get("/status")
    def status():
        return "status"

    @app.get("/api/osc/dashboard")
    def osc_dashboard_api():
        return "osc-api"

    @app.post("/api/system-test")
    def system_test_api():
        return "system-test"

    @app.get("/unknown")
    def unknown():
        return "unknown"

    @app.get("/health")
    def health():
        return "health"

    @app.get("/livez")
    def livez():
        return "livez"

    @app.get("/readyz")
    def readyz():
        return "readyz"

    @app.get("/saas-readyz")
    def saas_readyz():
        return "saas-readyz"

    @app.get("/login")
    def login():
        return "login"

    @app.get("/register")
    def register():
        return "register"

    @app.get("/static/magi-site.css")
    def magi_site_css():
        return "css"

    @app.get("/lottery")
    def lottery():
        return "lottery"

    @app.post("/api/lottery/draw")
    def lottery_draw():
        return "draw"

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

    for path in ("/livez", "/readyz", "/saas-readyz"):
        response = client.get(
            path,
            headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
        )
        assert response.status_code == 200, path

    response = client.get(
        "/login",
        headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 200

    response = client.get(
        "/register",
        headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 200

    response = client.get(
        "/static/magi-site.css",
        headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 200

    response = client.get(
        "/lottery",
        headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 200

    response = client.post(
        "/api/lottery/draw",
        headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
    )
    assert response.status_code == 200


def test_cloudflare_tunnel_allows_authenticated_web_ui_surface():
    app = _make_app()
    client = app.test_client()

    for method, path in (
        ("get", "/dashboard"),
        ("get", "/osc"),
        ("get", "/status"),
        ("get", "/api/osc/dashboard"),
        ("post", "/api/system-test"),
    ):
        response = getattr(client, method)(
            path,
            headers={"Cf-Connecting-Ip": "1.2.3.4", "X-Forwarded-Host": "demo.trycloudflare.com"},
        )
        assert response.status_code == 200, path


def test_cloudflare_tunnel_still_blocks_unknown_routes():
    app = _make_app()
    client = app.test_client()

    response = client.get(
        "/unknown",
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
