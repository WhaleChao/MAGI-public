from __future__ import annotations

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


def test_check_authorization_without_login_manager_is_not_authenticated():
    app = Flask(__name__)

    with app.test_request_context("/"):
        allowed, reason = check_authorization("viewer")

    assert allowed is False
    assert reason == "not_authenticated"
