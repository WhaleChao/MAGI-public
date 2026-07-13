# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_auth_pages_keep_mobile_login_form_visible():
    login_html = (ROOT / "templates" / "login.html").read_text(encoding="utf-8")
    register_html = (ROOT / "templates" / "register.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "magi-site.css").read_text(encoding="utf-8")

    assert '<body class="site-auth auth-page">' in login_html
    assert '<form method="post" class="auth-form"' in login_html
    assert 'name="username"' in login_html
    assert 'placeholder="輸入帳號"' in login_html
    assert 'name="password"' in login_html
    assert 'placeholder="輸入密碼"' in login_html
    assert 'name="next" value="{{ next_target or \'\' }}"' in login_html
    assert 'name="mobile_app" value="1"' in login_html
    assert "magi-site.css') }}?v=20260619-auth-mobile-v3" in login_html
    assert "magi-site.css') }}?v=20260619-auth-mobile-v3" in register_html

    assert "<time class=\"auth-clock\"" in login_html
    assert 'class="auth-shell"' in login_html
    assert 'class="auth-card"' in login_html
    assert '<html lang="zh-Hant">' in register_html
    assert "font-family: -apple-system" in login_html
    assert ".auth-clock" in login_html

    assert ".site-auth ." in css
    assert ".site-auth .auth-shell {" in css
    assert ".site-auth .auth-card {" in css
    assert ".site-auth .auth-form {" in css
    assert ".site-auth .auth-field {" in css
    assert ".site-auth .auth-label {" in css
    assert ".site-auth .auth-input {" in css
    assert ".site-auth .auth-submit {" in css
    assert ".site-auth .auth-link {" in css
    assert "min-height: var(--magi-touch-target)" in css
    assert "width: min(360px, calc(100vw - 28px)) !important;" in css
    assert "overflow-x: hidden" in css


def test_key_pages_share_mobile_viewport_and_site_css_contract():
    templates = [
        "login.html",
        "register.html",
        "golem_console.html",
        "research.html",
        "mobile_home.html",
        "dashboard_nerv.html",
        "dashboard_website.html",
    ]
    body_classes = {
        "golem_console.html": "site-console",
        "research.html": "site-console",
        "mobile_home.html": "site-mobile",
        "dashboard_nerv.html": "site-nerv",
        "dashboard_website.html": "site-website",
        "login.html": "site-auth",
        "register.html": "site-auth",
    }
    css = (ROOT / "static" / "magi-site.css").read_text(encoding="utf-8")

    for name in templates:
        html = (ROOT / "templates" / name).read_text(encoding="utf-8")
        assert "name=\"viewport\"" in html
        assert "width=device-width" in html
        assert "initial-scale=1" in html
        assert body_classes[name] in html
        assert "magi-site.css" in html
        assert "overflow-x: hidden" in css
        assert "--magi-touch-target: 44px" in css
        assert ".site-ops .links," in css or ".site-console .topbar" in css
        assert ".site-nerv .nerv-header" in css


def test_service_worker_uses_network_first_and_skips_login_cache():
    sw = (ROOT / "static" / "mobile" / "sw.js").read_text(encoding="utf-8")

    assert "shouldSkipCache" in sw
    assert "cache.put(cacheKey(request), response.clone())" in sw
    assert "async function networkFirst" in sw
    assert "if (isAuthPath(url.pathname)) {" in sw
    assert 'event.respondWith(networkFirst(event));' in sw
    assert '"/login"' in sw
    assert '"/register"' in sw
