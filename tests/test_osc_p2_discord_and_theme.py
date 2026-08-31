# -*- coding: utf-8 -*-
"""Tests for OSC P2: Discord webhook test endpoint + Theme toggle assets."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask
from flask_login import LoginManager


@pytest.fixture
def app():
    a = Flask(__name__)
    a.config["TESTING"] = True
    a.config["LOGIN_DISABLED"] = True
    a.secret_key = "test"
    LoginManager().init_app(a)
    from api.blueprints.osc_settings import osc_settings_bp
    a.register_blueprint(osc_settings_bp)
    return a


@pytest.fixture
def client(app):
    return app.test_client()


# ── Discord webhook endpoint ──────────────────────────────────────────


def test_discord_test_route_registered(app):
    rules = [str(r) for r in app.url_map.iter_rules()]
    assert "/api/osc/discord/test" in rules


def test_discord_test_requires_url(client, monkeypatch):
    """無 webhook_url 且 settings 也沒 fallback → 400"""
    from api.blueprints import osc_settings as bp

    def fake_helpers():
        def fake_exec(*a, **kw):
            return (None, None)
        return (fake_exec, lambda v: str(v or "").strip(), lambda *a, **kw: None)

    monkeypatch.setattr(bp, "_get_osc_helpers", fake_helpers)

    r = client.post("/api/osc/discord/test", json={})
    assert r.status_code == 400
    assert "webhook_url required" in r.get_json()["error"]


def test_discord_test_rejects_invalid_url(client):
    r = client.post(
        "/api/osc/discord/test",
        json={"webhook_url": "https://example.com/not-discord"},
    )
    assert r.status_code == 400
    assert "invalid Discord webhook URL" in r.get_json()["error"]


def test_discord_test_success_with_valid_url(client):
    fake_resp = MagicMock()
    fake_resp.status = 204  # Discord webhook returns 204 No Content on success
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None

    with patch("urllib.request.urlopen", return_value=fake_resp):
        r = client.post(
            "/api/osc/discord/test",
            json={
                "webhook_url": "https://discord.com/api/webhooks/123/abc",
                "message": "Test from pytest",
            },
        )

    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["status_code"] == 204


def test_discord_test_falls_back_to_settings(client, monkeypatch):
    """payload 無 webhook_url 時，從 settings.discord_webhook_url 撈"""
    from api.blueprints import osc_settings as bp

    def fake_helpers():
        def fake_exec(sql, params=None, fetch=None):
            if "discord_webhook_url" in sql:
                return ({"value": "https://discord.com/api/webhooks/999/xyz"}, None)
            return (None, None)
        return (fake_exec, lambda v: str(v or "").strip(), lambda *a, **kw: None)

    monkeypatch.setattr(bp, "_get_osc_helpers", fake_helpers)

    fake_resp = MagicMock()
    fake_resp.status = 204
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: None

    with patch("urllib.request.urlopen", return_value=fake_resp):
        r = client.post("/api/osc/discord/test", json={})

    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_discord_test_handles_http_error(client):
    import urllib.error

    err = urllib.error.HTTPError(
        url="https://discord.com/api/webhooks/x/y",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=None,
    )
    with patch("urllib.request.urlopen", side_effect=err):
        r = client.post(
            "/api/osc/discord/test",
            json={"webhook_url": "https://discord.com/api/webhooks/x/y"},
        )
    assert r.status_code == 502
    body = r.get_json()
    assert body["ok"] is False
    assert "404" in body["error"]


# ── Theme toggle assets ────────────────────────────────────────────


def test_theme_dark_css_present():
    """osc-theme.css must contain .theme-dark class with dark color overrides."""
    css = (ROOT / "static/osc/osc-theme.css").read_text(encoding="utf-8")
    assert "body.theme-dark" in css, "Missing body.theme-dark class"
    assert "#0f172a" in css or "--bg" in css.split("body.theme-dark")[1][:500], \
        "Dark theme should override --bg"


def test_file_manager_dark_css_overrides_white_panels():
    """The file manager must not fall back to white panels in dark theme."""
    css = (ROOT / "static/osc/file-manager.css").read_text(encoding="utf-8")
    assert "body.theme-dark #fileManager" in css
    dark_block = css.split("body.theme-dark #fileManager", 1)[1]
    assert "--card-bg: #0f172a" in dark_block
    assert "body.theme-dark #fileManager .fm-main" in css
    assert "body.theme-dark #fileManager .fm-table tbody tr td" in css
    assert "background: #0f172a" in css
    assert "color: #e2e8f0" in css


def test_file_manager_mobile_css_keeps_entries_and_actions_visible():
    """Phone layouts must show file rows/actions instead of hiding table columns."""
    css = (ROOT / "static/osc/file-manager.css").read_text(encoding="utf-8")
    polish = (ROOT / "static/osc/osc-polish.css").read_text(encoding="utf-8")
    assert "Mobile-first repairs for the NAS file manager" in css
    assert "#fileManager .fm-table td" in css
    assert "display: block !important" in css
    assert ".fm-file-actions" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert ".fm-preview-pdf" in css
    assert ".fm-preview-mobile-help" in css
    assert "#fileManager .fm-action-btn" in polish
    assert "min-height: 40px" in polish


def test_file_manager_mobile_js_focuses_file_pane_and_has_pdf_fallback():
    """Opening a folder on phones should move users to files and make PDF preview obvious."""
    js = (ROOT / "static/osc/tabs/file_manager.js").read_text(encoding="utf-8")
    utils = (ROOT / "static/osc/osc-utils.js").read_text(encoding="utf-8")
    assert "FM_MOBILE_QUERY" in js
    assert "focusFilePaneOnMobile" in js
    assert "focusFilePaneOnMobile();" in js
    assert "_renderFilePdfPreview" in utils
    assert "PDF 可在此上下滑動預覽" in utils
    assert "開新分頁" in utils


def test_file_manager_preview_and_download_reject_login_redirects():
    """File routes must not render or download the login page after session expiry."""
    js = (ROOT / "static/osc/tabs/file_manager.js").read_text(encoding="utf-8")
    utils = (ROOT / "static/osc/osc-utils.js").read_text(encoding="utf-8")
    html = (ROOT / "templates/osc.html").read_text(encoding="utf-8")

    assert "file_manager.js?v=20260831-nas-listing-stability-v1" in html
    assert "osc-utils.js?v=20260814-readonly-fetch-retry-v1" in html
    assert "function isFileAuthRedirect" in utils
    assert 'response.type === "opaqueredirect"' in utils
    assert "response.status === 401" in utils
    assert 'redirect: "manual"' in utils
    assert "function startFileDownload" in utils
    assert "function openFilePreview" in utils
    assert 'method: "HEAD"' in utils
    assert "data-fm-preview-download" in utils
    assert "function isFileAuthRedirect" not in js


def test_theme_toggle_button_in_osc_html():
    """osc.html must contain themeToggleBtn in header."""
    html = (ROOT / "templates/osc.html").read_text(encoding="utf-8")
    assert 'id="themeToggleBtn"' in html, "Missing #themeToggleBtn in osc.html"


def test_theme_toggle_init_in_events_js():
    """osc-events.js must call initThemeToggle in boot."""
    js = (ROOT / "static/osc/osc-events.js").read_text(encoding="utf-8")
    assert "function initThemeToggle" in js, "Missing initThemeToggle definition"
    assert "initThemeToggle()" in js, "initThemeToggle not called"
    assert "magi.osc.theme" in js, "Missing localStorage key for theme persistence"


def test_user_facing_theme_names_are_day_and_night_everywhere():
    shared = (ROOT / "static/magi-theme.js").read_text(encoding="utf-8")
    osc = (ROOT / "templates/osc.html").read_text(encoding="utf-8")
    golem = (ROOT / "templates/golem_console.html").read_text(encoding="utf-8")
    menubar = (ROOT / "gui/magi_menubar.py").read_text(encoding="utf-8")

    assert 'label: "夜", next: "日"' in shared
    assert 'label: "日", next: "夜"' in shared
    assert "切換日／夜" in osc
    assert "切換日／夜" in golem
    assert "切換為日" in menubar
    assert "切換為夜" in menubar
    for old_name in ("賽博朋克", "森林極簡"):
        assert old_name not in shared
        assert old_name not in menubar


def test_shared_theme_completes_previews_reports_and_dashboard_modals():
    css = (ROOT / "static/magi-theme.css").read_text(encoding="utf-8")
    dashboard = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")

    assert ".magi-modal-layer" in css
    assert ".magi-modal-surface" in css
    assert ".fm-preview-table td" in css
    assert ".report-card, .empty-state" in css
    assert ".msg.user, .msg.casper, .msg.sys" in css
    assert dashboard.count('class="magi-modal-layer"') == 2
    assert dashboard.count('class="magi-modal-surface"') == 2


def test_shared_cyber_theme_is_not_a_black_backdrop_with_white_or_dark_text_panels():
    css = (ROOT / "static/magi-theme.css").read_text(encoding="utf-8")
    login = (ROOT / "templates/login.html").read_text(encoding="utf-8")

    assert "--magi-accent-2: #ff3bd4" in css
    assert "--magi-accent-3: #ffe36e" in css
    assert "--magi-panel: #061522" in css
    assert "--magi-ink: #edfeff" in css
    assert '.site-auth .auth-card' in css
    assert "linear-gradient(155deg" in css
    assert ".site-auth .auth-input" in css
    assert "color: #f2feff !important" in css
    assert ":where(input, select, textarea)::placeholder" in css
    assert "SECURE OPERATIONS GATEWAY" in login
    assert "20260730-rc173" in login


def test_osc_mobile_google_like_affordance_contract():
    """Phone OSC should keep content reachable and global search should actually run."""
    html = (ROOT / "templates/osc.html").read_text(encoding="utf-8")
    components = (ROOT / "static/osc/osc-components.css").read_text(encoding="utf-8")
    responsive = (ROOT / "static/osc/osc-responsive.css").read_text(encoding="utf-8")
    file_manager = (ROOT / "static/osc/file-manager.css").read_text(encoding="utf-8")
    events = (ROOT / "static/osc/osc-events.js").read_text(encoding="utf-8")

    assert "osc-responsive.css?v=20260619-google-mobile-v1" in html
    assert "osc-events.js?v=20260815-deep-link-v1" in html
    assert "mobileStatusBadge" in html
    assert ".table-wrap {\n    overflow-x: auto;" in components
    assert "-webkit-overflow-scrolling: touch;" in components
    assert ".table-wrap table {\n        min-width: 680px;" in responsive
    assert ".sort-bar .sort-dir-btn" in responsive
    assert "min-height: 44px;" in responsive
    assert ".fm-toolbar .btn-mini" in file_manager
    assert "min-height: 44px;" in file_manager
    assert "function runGlobalCaseSearch" in events
    search_block = events.split("function runGlobalCaseSearch", 1)[1].split("function initGlobalSearch", 1)[0]
    assert "loadCases" in search_block


# ── Discord admin UI assets ───────────────────────────────────────


def test_discord_section_in_admin_html():
    html = (ROOT / "templates/partials/osc/admin.html").read_text(encoding="utf-8")
    assert 'id="discordWebhookSection"' in html
    assert 'id="discordWebhookUrl"' in html
    assert 'id="discordWebhookSaveBtn"' in html
    assert 'id="discordWebhookTestBtn"' in html


def test_discord_handlers_in_admin_js():
    js = (ROOT / "static/osc/tabs/admin.js").read_text(encoding="utf-8")
    assert "function loadDiscordWebhook" in js
    assert "function saveDiscordWebhook" in js
    assert "function testDiscordWebhook" in js
