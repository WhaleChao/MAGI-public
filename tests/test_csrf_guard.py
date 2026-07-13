from __future__ import annotations

from flask import Flask, jsonify

from api.csrf_guard import CSRF_COOKIE_NAME, CSRF_TOKEN_HEADER, csrf_exempt, middleware_apply_csrf


def _make_app() -> Flask:
    app = Flask(__name__)

    @app.post("/decorated-webhook")
    @csrf_exempt
    def decorated_webhook():
        return jsonify({"ok": True})

    @app.post("/protected")
    def protected():
        return jsonify({"ok": True})

    @app.post("/api/protected")
    def api_protected():
        return jsonify({"ok": True})

    middleware_apply_csrf(app)
    return app


def test_csrf_exempt_decorator_is_honored_before_view_execution():
    client = _make_app().test_client()

    response = client.post("/decorated-webhook", environ_base={"REMOTE_ADDR": "203.0.113.10"})

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_state_changing_request_without_csrf_token_is_rejected():
    client = _make_app().test_client()

    response = client.post("/protected", environ_base={"REMOTE_ADDR": "203.0.113.10"})

    assert response.status_code == 403
    assert response.get_json()["code"] == "csrf_validation_failed"


def test_state_changing_request_with_double_submit_token_is_allowed():
    client = _make_app().test_client()
    token = "token-123"

    client.set_cookie(CSRF_COOKIE_NAME, token)
    response = client.post(
        "/protected",
        headers={CSRF_TOKEN_HEADER: token},
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_localhost_api_request_is_not_automatically_csrf_exempt():
    client = _make_app().test_client()

    response = client.post("/api/protected", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    assert response.status_code == 403
    assert response.get_json()["code"] == "csrf_validation_failed"


def test_api_request_with_valid_api_key_is_csrf_exempt(monkeypatch):
    monkeypatch.setenv("MAGI_API_KEY", "test-key")
    client = _make_app().test_client()

    response = client.post(
        "/api/protected",
        headers={"X-API-Key": "test-key"},
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_explicit_csrf_test_mode_is_exempt():
    app = _make_app()
    app.config["MAGI_CSRF_TEST_MODE"] = True
    client = app.test_client()

    response = client.post("/protected", environ_base={"REMOTE_ADDR": "203.0.113.10"})

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_cli_csrf_exemption_requires_explicit_opt_in(monkeypatch):
    client = _make_app().test_client()
    headers = {"X-MAGI-Client": "cli", "X-MAGI-CLI": "1"}

    response = client.post("/api/protected", headers=headers, environ_base={"REMOTE_ADDR": "203.0.113.10"})
    assert response.status_code == 403

    monkeypatch.setenv("MAGI_CSRF_ALLOW_CLI", "1")
    response = client.post("/api/protected", headers=headers, environ_base={"REMOTE_ADDR": "203.0.113.10"})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
