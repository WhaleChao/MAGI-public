from __future__ import annotations

from urllib.parse import parse_qs, urlparse
from flask import Flask
from flask_login import LoginManager, UserMixin

from contextlib import contextmanager


@contextmanager
def _fake_cursor():
    class _Cursor:
        def __init__(self):
            self.last_sql = ""

        def execute(self, sql="", *_args, **_kwargs):
            self.last_sql = str(sql or "")
            return None

        def fetchone(self):
            if "COUNT(*)" in self.last_sql.upper():
                return (1,)
            return {
                "id": 1,
                "username": "tester",
                "password_hash": "hashed",
                "role": "user",
            }

    class _Conn:
        def commit(self):
            return None

    yield (_Conn(), _Cursor())


class _User(UserMixin):
    def __init__(self, user_id: str):
        self.id = user_id


def _make_app():
    from api.blueprints.dashboard_pages import dashboard_pages_bp

    app = Flask(__name__, template_folder="templates")
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


def test_protected_routes_redirect_to_login_with_next():
    from api.server import app

    client = app.test_client()
    protected_paths = [
        "/osc",
        "/mobile",
        "/app",
        "/dashboard/nerv",
        "/golem",
        "/research",
        "/dashboard/website",
        "/wa/",
    ]

    for path in protected_paths:
        response = client.get(path, base_url="http://localhost", follow_redirects=False)
        assert response.status_code == 302, path
        parsed = urlparse(response.headers["Location"])
        assert parsed.path == "/login", path
        query = parse_qs(parsed.query)
        assert query.get("next", [None])[0] == path, path


def test_mobile_app_entry_forces_clean_login():
    from api.server import app

    client = app.test_client()

    response = client.get("/mobile-app", base_url="http://localhost", follow_redirects=False)
    assert response.status_code == 302
    parsed = urlparse(response.headers["Location"])
    assert parsed.path == "/login"
    query = parse_qs(parsed.query)
    assert query.get("next", [None])[0] == "/mobile"
    assert query.get("mobile_app", [None])[0] == "1"


def test_root_mobile_webview_launch_goes_to_login_not_dashboard():
    from api.server import app

    client = app.test_client()
    response = client.get(
        "/",
        base_url="http://localhost",
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Version/4.0 Chrome/124.0 Mobile Safari/537.36 wv",
            "X-Requested-With": "tw.local.magi.mobile",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    parsed = urlparse(response.headers["Location"])
    assert parsed.path == "/login"
    query = parse_qs(parsed.query)
    assert query.get("next", [None])[0] == "/mobile"
    assert query.get("mobile_app", [None])[0] == "1"


def test_stale_authenticated_mobile_home_session_reauths():
    app = _make_app()

    @app.route("/login")
    def login():
        return "login"

    client = app.test_client()
    response = client.get(
        "/mobile",
        headers={
            "X-User-ID": "u1",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Version/4.0 Chrome/124.0 Mobile Safari/537.36 wv",
            "X-Requested-With": "tw.local.magi.mobile",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    parsed = urlparse(response.headers["Location"])
    assert parsed.path == "/login"
    query = parse_qs(parsed.query)
    assert query.get("next", [None])[0] == "/mobile"
    assert query.get("mobile_app", [None])[0] == "1"


def test_stale_authenticated_mobile_dashboard_session_reauths():
    app = _make_app()

    @app.route("/login")
    def login():
        return "login"

    client = app.test_client()
    response = client.get(
        "/dashboard/nerv",
        headers={
            "X-User-ID": "u1",
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Version/4.0 Chrome/124.0 Mobile Safari/537.36 wv",
            "X-Requested-With": "tw.local.magi.mobile",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    parsed = urlparse(response.headers["Location"])
    assert parsed.path == "/login"
    query = parse_qs(parsed.query)
    assert query.get("next", [None])[0] == "/mobile"
    assert query.get("mobile_app", [None])[0] == "1"


def test_login_next_target_is_sanitized(monkeypatch):
    import api.server

    monkeypatch.setattr("api.db_helper.get_cursor", lambda *_, **__: _fake_cursor())
    monkeypatch.setattr(api.server, "check_password_hash", lambda *_args, **_kwargs: True)

    client = api.server.app.test_client()

    safe_resp = client.post(
        "/login",
        data={
            "username": "tester",
            "password": "any",
            "next": "/research?source=osc#top",
        },
        follow_redirects=False,
    )
    assert safe_resp.status_code == 302
    safe_location = urlparse(safe_resp.headers["Location"])
    assert safe_location.path == "/research"
    assert parse_qs(safe_location.query).get("source", [None])[0] == "osc"
    assert safe_location.fragment == "top"

    unsafe_resp = client.post(
        "/login",
        data={
            "username": "tester",
            "password": "any",
            "next": "https://evil.example.com",
        },
        follow_redirects=False,
    )
    assert unsafe_resp.status_code == 302
    assert unsafe_resp.headers["Location"] == "/dashboard"


def test_register_redirects_to_login_with_sanitized_next(monkeypatch):
    import api.server

    monkeypatch.setenv("MAGI_ALLOW_PUBLIC_REGISTRATION", "1")
    monkeypatch.setattr("api.db_helper.get_cursor", lambda *_, **__: _fake_cursor())

    def _fake_generate_hash(value: str):
        return f"hashed:{value}"
    monkeypatch.setattr(api.server, "generate_password_hash", _fake_generate_hash)

    client = api.server.app.test_client()
    response = client.post(
        "/register",
        data={
            "username": "new_user",
            "password": "secret",
            "next": "https://evil.example.com",
        },
        follow_redirects=False,
    )
    parsed = urlparse(response.headers["Location"])
    assert parsed.path == "/login"
    query = parse_qs(parsed.query)
    assert query.get("next", [None])[0] == "/dashboard"


def test_mobile_manifest_and_config_contract(monkeypatch):
    from api.blueprints import dashboard_pages as mod

    monkeypatch.setattr(
        mod,
        "_build_mobile_app_config",
        lambda: {
            "app_name": "MAGI Mobile",
            "base_url": "https://magi.tailnet.internal",
            "tailscale_dns": "magi.tailnet.internal",
            "tailscale_ip": "100.64.1.2",
            "tailscale_online": True,
            "routes": [
                {"label": "Paperclip", "path": "/osc", "kind": "core"},
                {"label": "手機後台", "path": "/mobile-admin", "kind": "admin"},
            ],
            "android_package": "tw.local.magi.mobile",
            "ios_bundle_id": "tw.local.magi.mobile",
        },
    )

    app = _make_app()
    # manifest has no login gate and should provide valid app contract
    manifest_resp = app.test_client().get("/mobile/manifest.webmanifest")
    assert manifest_resp.status_code == 200
    manifest = manifest_resp.get_json()
    assert manifest["start_url"] == "/mobile"
    assert manifest["scope"] == "/mobile"
    assert manifest["shortcuts"] == [
        {"name": "Paperclip", "url": "/osc"},
        {"name": "手機後台", "url": "/mobile-admin"},
    ]

    sw_resp = app.test_client().get("/mobile/sw.js")
    assert sw_resp.status_code == 200
    assert sw_resp.headers["Service-Worker-Allowed"] == "/mobile"

    config_resp = app.test_client().get(
        "/mobile/config.json",
        headers={"X-User-ID": "u1"},
        follow_redirects=False,
    )
    assert config_resp.status_code == 200
    config = config_resp.get_json()
    assert config["base_url"].startswith("https://")
    assert "tailnet.internal" in config["base_url"]


def test_capacitor_app_starts_from_mobile_login_entry():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "mobile_app" / "capacitor.config.json").read_text(encoding="utf-8"))
    android_config = json.loads(
        (root / "mobile_app" / "android" / "app" / "src" / "main" / "assets" / "capacitor.config.json").read_text(encoding="utf-8")
    )
    manifest = (root / "mobile_app" / "android" / "app" / "src" / "main" / "AndroidManifest.xml").read_text(
        encoding="utf-8"
    )

    assert config["server"]["url"].endswith("/mobile-app")
    assert android_config["server"]["url"].endswith("/mobile-app")
    assert config["server"] == android_config["server"]
    assert ".ts.net" not in config["server"]["url"]
    if config["server"]["url"].startswith("http://"):
        assert config["server"]["cleartext"] is True
        assert 'android:usesCleartextTraffic="true"' in manifest


def test_mobile_configure_script_resolves_env_url(monkeypatch):
    from scripts import configure_mobile_app

    monkeypatch.delenv("MAGI_MOBILE_APP_URL", raising=False)
    monkeypatch.delenv("MAGI_CAPACITOR_SERVER_URL", raising=False)
    monkeypatch.delenv("MAGI_MOBILE_BASE_URL", raising=False)
    monkeypatch.setenv("MAGI_PUBLIC_BASE_URL", "https://magi.example.test")

    assert configure_mobile_app._configured_mobile_url() == "https://magi.example.test/mobile-app"

    monkeypatch.setenv("MAGI_MOBILE_APP_URL", "https://mobile.example.test/mobile-app")
    assert configure_mobile_app._configured_mobile_url() == "https://mobile.example.test/mobile-app"
