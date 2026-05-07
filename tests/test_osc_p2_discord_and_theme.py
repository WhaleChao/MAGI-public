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
