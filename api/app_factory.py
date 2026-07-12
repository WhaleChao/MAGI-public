from __future__ import annotations

import logging
import os
from html import escape

from flask import Flask, Response, abort, jsonify, request
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

from api.blueprints.dashboard_pages import dashboard_pages_bp
from api.blueprints.golem_console import golem_console_bp
from api.blueprints.lottery import lottery_bp
from api.blueprints.osc_accounting import osc_accounting_bp
from api.blueprints.osc_debt import osc_debt_bp
from api.blueprints.osc_pdf import osc_pdf_bp
from api.blueprints.raziel import raziel_bp
from api.blueprints.osc_settings import osc_settings_bp


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _configured_https_public_url() -> bool:
    for name in ("MAGI_PUBLIC_BASE_URL", "MAGI_BASE_URL", "MAGI_EXTERNAL_BASE_URL"):
        value = (os.environ.get(name) or "").strip().lower()
        if value.startswith("https://"):
            return True
    return False


def _formal_deployment_mode() -> bool:
    mode = (os.environ.get("MAGI_DEPLOYMENT_MODE") or "").strip().lower()
    return mode in {"production", "prod", "saas", "formal_saas", "managed_saas", "multi_tenant_saas"}


def _https_enforced() -> bool:
    if _env_truthy("MAGI_FORCE_HTTPS"):
        return True
    if _env_truthy("MAGI_SECURE_COOKIES") and _configured_https_public_url():
        return True
    return _formal_deployment_mode() and _configured_https_public_url()


_SENSITIVE_STATIC_PREFIXES = (
    "/static/exports/",
    "/static/runtime/",
    "/static/reports/",
    "/static/test_reports/",
)
_SENSITIVE_STATIC_NAMES = (
    "file_review_auto_state.json",
    "guardian_control.json",
    "magi_status.json",
    "laf_gmail_monitor_state.json",
    "process_guardian_state.json",
    "external_chat_metrics.jsonl",
)
_SENSITIVE_STATIC_SUFFIXES = (
    "_latest.json",
    "_state.json",
    ".jsonl",
    ".sqlite",
    ".db",
)


def _is_sensitive_static_request(path: str) -> bool:
    normalized = (path or "").strip().lower()
    if not normalized.startswith("/static/"):
        return False
    if any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in _SENSITIVE_STATIC_PREFIXES):
        return True
    tail = normalized.rsplit("/", 1)[-1]
    return tail in _SENSITIVE_STATIC_NAMES or tail.endswith(_SENSITIVE_STATIC_SUFFIXES)


def create_base_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    if _env_truthy("MAGI_TRUST_PROXY_HEADERS"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if _https_enforced():
        app.config["SESSION_COOKIE_SECURE"] = True

    try:
        app.secret_key = os.environ["FLASK_SECRET_KEY"]
    except KeyError as exc:
        raise RuntimeError("Missing required env var: FLASK_SECRET_KEY. Set it in .env") from exc

    @app.before_request
    def _block_sensitive_static_files():
        if _is_sensitive_static_request(request.path):
            abort(404)

    return app


def install_error_handlers(app: Flask) -> Flask:
    @app.errorhandler(500)
    def handle_500(e):
        accept = str(request.headers.get("Accept") or "")
        wants_json = (
            request.is_json
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in accept
            or "text/html" not in accept
        )
        if not wants_json:
            body = f"""<!doctype html>
<html lang="zh-TW">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MAGI 系統暫時忙碌</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f5f7fb; color:#1f2937; margin:0; }}
    main {{ max-width: 640px; margin: 14vh auto; padding: 28px; background:white; border:1px solid #d8dee9; border-radius:12px; box-shadow:0 12px 30px rgba(15,23,42,.08); }}
    h1 {{ font-size: 22px; margin:0 0 12px; }}
    p {{ line-height:1.7; margin:0 0 18px; }}
    button {{ border:1px solid #0ea5e9; background:#0ea5e9; color:white; border-radius:8px; padding:9px 14px; cursor:pointer; }}
  </style>
</head>
<body>
  <main>
    <h1>系統暫時忙碌</h1>
    <p>{escape("操作沒有完成，請回上一頁稍後再試；若重複發生，請通知 MAGI 檢查服務紀錄。")}</p>
    <button onclick="history.length > 1 ? history.back() : location.href='/dashboard'">返回上一頁</button>
  </main>
</body>
</html>"""
            return Response(body, status=500, content_type="text/html; charset=utf-8")
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
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "frame-src 'self' blob:; "
            "object-src 'self' blob:;",
        )
        if _https_enforced() or _env_truthy("MAGI_ENABLE_HSTS"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if not request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app


def install_csrf(app: Flask, logger: logging.Optional[Logger] = None) -> Flask:
    try:
        from api.csrf_guard import middleware_apply_csrf

        middleware_apply_csrf(app)
        if logger:
            logger.info("CSRF protection enabled")
    except Exception as exc:
        fail_open = os.environ.get("MAGI_CSRF_FAIL_OPEN", "").strip().lower() in {"1", "true", "yes", "on"}
        if logger:
            logger.warning("CSRF protection not loaded: %s", exc)
        if not fail_open:
            raise RuntimeError("CSRF protection failed to initialize") from exc
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
    app.register_blueprint(osc_pdf_bp)
    app.register_blueprint(raziel_bp)
    app.register_blueprint(golem_console_bp)
    app.register_blueprint(lottery_bp)
    app.register_blueprint(dashboard_pages_bp)
    return app
