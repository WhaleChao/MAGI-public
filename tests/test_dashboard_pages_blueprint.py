from __future__ import annotations

from pathlib import Path

from flask import Flask
from flask_login import LoginManager, UserMixin


class _User(UserMixin):
    def __init__(self, user_id: str):
        self.id = user_id


def _make_app(template_dir: Path):
    from api.blueprints.dashboard_pages import dashboard_pages_bp

    app = Flask(__name__, template_folder=str(template_dir))
    app.config.update(SECRET_KEY="test-secret", TESTING=True)
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "login"

    @login_manager.request_loader
    def _load_user(request):
        user_id = (request.headers.get("X-User-ID") or "").strip()
        return _User(user_id) if user_id else None

    app.register_blueprint(dashboard_pages_bp)
    return app


def test_redirect_routes_point_to_existing_page_targets(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_pixel.html", "dashboard_nerv.html"):
        (template_dir / name).write_text("{{ user.id }}", encoding="utf-8")

    app = _make_app(template_dir)
    client = app.test_client()

    response = client.get("/static/worldmonitor_reports", follow_redirects=False)
    assert response.status_code == 302
    assert response.location.endswith("/intel")

    response = client.get("/worldmonitor", follow_redirects=False)
    assert response.status_code == 302
    assert response.location.endswith("/intel")

    response = client.get("/openclaw", follow_redirects=False)
    assert response.status_code == 302
    assert response.location.endswith("/dashboard/nerv")


def test_dashboard_pages_render_with_login_required(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "dashboard.html").write_text("dashboard {{ user.id }}", encoding="utf-8")
    (template_dir / "dashboard_pixel.html").write_text("pixel {{ user.id }}", encoding="utf-8")
    (template_dir / "dashboard_nerv.html").write_text("nerv {{ user.id }}", encoding="utf-8")

    app = _make_app(template_dir)
    client = app.test_client()

    response = client.get("/dashboard", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"dashboard u1" in response.data

    response = client.get("/dashboard/pixel", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"pixel u1" in response.data

    response = client.get("/dashboard/nerv", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"nerv u1" in response.data


def test_intel_page_lists_recent_reports(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_pixel.html", "dashboard_nerv.html"):
        (template_dir / name).write_text("{{ user.id }}", encoding="utf-8")

    from api.blueprints import dashboard_pages as mod

    reports_dir = tmp_path / "worldmonitor_reports"
    reports_dir.mkdir()
    (reports_dir / "alpha.md").write_text("Alpha report", encoding="utf-8")
    (reports_dir / "beta.md").write_text("Beta report", encoding="utf-8")
    monkeypatch.setattr(mod, "_WORLDMONITOR_REPORT_DIR", reports_dir)

    app = _make_app(template_dir)
    client = app.test_client()
    response = client.get("/intel", headers={"X-User-ID": "u1"})

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "🌐 全球情報面板" in body
    assert "beta.md" in body or "alpha.md" in body
    assert "Beta report" in body or "Alpha report" in body
