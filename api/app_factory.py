from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, request
from flask_login import LoginManager

from api.blueprints.dashboard_pages import dashboard_pages_bp
from api.blueprints.osc_accounting import osc_accounting_bp
from api.blueprints.osc_debt import osc_debt_bp
from api.blueprints.osc_settings import osc_settings_bp


def create_base_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if os.environ.get("MAGI_FORCE_HTTPS", "").strip().lower() in {"1", "true", "yes"}:
        app.config["SESSION_COOKIE_SECURE"] = True

    try:
        app.secret_key = os.environ["FLASK_SECRET_KEY"]
    except KeyError as exc:
        raise RuntimeError("Missing required env var: FLASK_SECRET_KEY. Set it in .env") from exc
    return app


def install_error_handlers(app: Flask) -> Flask:
    @app.errorhandler(500)
    def handle_500(e):
        return jsonify({"error": "internal_server_error", "message": "系統暫時忙碌，請稍後再試"}), 500

    return app


def install_security_headers(app: Flask) -> Flask:
    @app.after_request
    def _add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;",
        )
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if not request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app


def install_csrf(app: Flask, logger: logging.Logger | None = None) -> Flask:
    try:
        from api.csrf_guard import middleware_apply_csrf

        middleware_apply_csrf(app)
        if logger:
            logger.info("CSRF protection enabled")
    except Exception as exc:
        if logger:
            logger.warning("CSRF protection not loaded: %s", exc)
    return app


def init_login_manager(app: Flask) -> LoginManager:
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "login"
    return login_manager


def register_core_blueprints(app: Flask) -> Flask:
    app.register_blueprint(osc_settings_bp)
    app.register_blueprint(osc_accounting_bp)
    app.register_blueprint(osc_debt_bp)
    app.register_blueprint(dashboard_pages_bp)
    return app

