from __future__ import annotations

from flask import Flask

from api.app_factory import install_error_handlers


def _app() -> Flask:
    app = Flask(__name__)
    app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=False)
    install_error_handlers(app)

    @app.route("/boom")
    def _boom():
        raise RuntimeError("boom")

    return app


def test_500_for_direct_browser_request_is_readable_html():
    client = _app().test_client()

    response = client.get("/boom", headers={"Accept": "text/html"})

    assert response.status_code == 500
    assert response.content_type.startswith("text/html")
    assert "系統暫時忙碌" in response.get_data(as_text=True)


def test_500_for_api_request_stays_json():
    client = _app().test_client()

    response = client.get("/boom", headers={"Accept": "application/json"})

    assert response.status_code == 500
    assert response.is_json
    assert response.get_json()["error"] == "internal_server_error"
