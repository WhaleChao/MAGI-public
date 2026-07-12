from __future__ import annotations

import logging

from flask import Flask

from api.authz import check_authorization, require_api_key


def test_require_api_key_without_login_manager_returns_401_instead_of_500(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    monkeypatch.setenv("MAGI_API_KEY", "test-key")

    @app.route("/protected")
    @require_api_key
    def protected():
        return {"ok": True}

    client = app.test_client()
    response = client.get("/protected")

    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized: missing API key"


def test_require_api_key_rejects_query_string_key_by_default(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    monkeypatch.setenv("MAGI_API_KEY", "test-key")
    monkeypatch.delenv("MAGI_ALLOW_QUERY_API_KEY", raising=False)

    @app.route("/protected")
    @require_api_key
    def protected():
        return {"ok": True}

    response = app.test_client().get("/protected?api_key=test-key")

    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized: missing API key"


def test_require_api_key_query_string_compat_requires_explicit_opt_in(monkeypatch, caplog):
    import api.authz as authz

    app = Flask(__name__)
    app.config["TESTING"] = True
    monkeypatch.setenv("MAGI_API_KEY", "test-key")
    monkeypatch.setenv("MAGI_ALLOW_QUERY_API_KEY", "1")
    monkeypatch.setattr(authz, "_QUERY_API_KEY_WARNING_EMITTED", False)

    @app.route("/protected")
    @require_api_key
    def protected():
        return {"ok": True}

    with caplog.at_level(logging.WARNING, logger="api.authz"):
        response = app.test_client().get("/protected?api_key=test-key")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert "query string API key accepted" in caplog.text


def test_require_api_key_in_formal_saas_requires_tenant_header(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    monkeypatch.setenv("MAGI_API_KEY", "test-key")
    monkeypatch.setenv("MAGI_SAAS_MODE", "1")
    monkeypatch.setenv("MAGI_TENANT_ID", "tenant-alpha")

    @app.route("/protected")
    @require_api_key
    def protected():
        return {"ok": True}

    client = app.test_client()
    response = client.get("/protected", headers={"X-API-Key": "test-key"})
    assert response.status_code == 403
    assert response.get_json()["error"] == "forbidden: tenant mismatch"

    response = client.get("/protected?tenant_id=tenant-alpha", headers={"X-API-Key": "test-key"})
    assert response.status_code == 403
    assert response.get_json()["error"] == "forbidden: tenant mismatch"

    response = client.get(
        "/protected",
        headers={"X-API-Key": "test-key", "X-Tenant-ID": "tenant-alpha"},
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_check_authorization_without_login_manager_is_not_authenticated():
    app = Flask(__name__)

    with app.test_request_context("/"):
        allowed, reason = check_authorization("viewer")

    assert allowed is False
    assert reason == "not_authenticated"
