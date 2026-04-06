"""
Dashboard / Intel / OpenClaw page routes
========================================

First modularization slice for the page layer that was previously embedded in
`api/server.py`.

This blueprint keeps the existing behavior for:
  - /static/worldmonitor_reports -> /intel
  - /worldmonitor -> /intel
  - /openclaw -> /dashboard/nerv
  - /intel -> worldmonitor report index
  - /dashboard
  - /dashboard/nerv

The module is intentionally dependency-light and does not import server.py.
"""

from __future__ import annotations

import html
from pathlib import Path

from flask import Blueprint, Response, redirect, render_template, request, url_for
from flask_login import current_user, login_required

import requests as _requests

dashboard_pages_bp = Blueprint("dashboard_pages", __name__)

_MAGI_ROOT = Path(__file__).resolve().parents[2]
_WORLDMONITOR_REPORT_DIR = _MAGI_ROOT / "static" / "worldmonitor_reports"


def _iter_worldmonitor_reports(limit: int = 20) -> list[dict[str, str]]:
    reports: list[dict[str, str]] = []
    if not _WORLDMONITOR_REPORT_DIR.is_dir():
        return reports
    for entry in sorted(_WORLDMONITOR_REPORT_DIR.iterdir(), reverse=True):
        if len(reports) >= limit:
            break
        if not entry.is_file() or entry.suffix.lower() != ".md":
            continue
        try:
            content = entry.read_text(encoding="utf-8")[:5000]
        except Exception:
            content = "(讀取失敗)"
        reports.append({"name": entry.name, "content": content})
    return reports


def _render_worldmonitor_page() -> tuple[str, int]:
    reports = _iter_worldmonitor_reports()
    if not reports:
        return "<h2>🌐 全球情報面板</h2><p>尚無報告。</p>", 200

    parts = ["<h2>🌐 全球情報面板</h2>"]
    for report in reports:
        parts.append(f"<h3>{html.escape(report['name'])}</h3>")
        parts.append(f"<pre>{html.escape(report['content'])}</pre><hr>")
    return "\n".join(parts), 200


@dashboard_pages_bp.route("/static/worldmonitor_reports")
@dashboard_pages_bp.route("/static/worldmonitor_reports/")
def worldmonitor_reports_redirect():
    return redirect("/intel")


@dashboard_pages_bp.route("/worldmonitor")
@dashboard_pages_bp.route("/worldmonitor/")
def worldmonitor_entry():
    return redirect("/intel")


@dashboard_pages_bp.route("/openclaw")
@dashboard_pages_bp.route("/openclaw-gateway")
def openclaw_entry():
    return redirect(url_for("dashboard_pages.dashboard_nerv"))


@dashboard_pages_bp.route("/intel")
@login_required
def intel_panel():
    return _render_worldmonitor_page()


@dashboard_pages_bp.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=current_user)


@dashboard_pages_bp.route("/dashboard/nerv")
@login_required
def dashboard_nerv():
    return render_template("dashboard_nerv.html", user=current_user)


@dashboard_pages_bp.route("/dashboard/website")
@login_required
def dashboard_website():
    """個人網站後台管理（反向代理到 localhost:8088）"""
    return render_template("dashboard_website.html", user=current_user)


# --- Website admin reverse proxy ---
_ADMIN_BASE = "http://127.0.0.1:8088"
_PROXY_PREFIX = "/wa"


@dashboard_pages_bp.route(f"{_PROXY_PREFIX}/", defaults={"path": ""})
@dashboard_pages_bp.route(f"{_PROXY_PREFIX}/<path:path>", methods=["GET", "POST"])
def website_admin_proxy(path):
    """Reverse-proxy website admin server so it works over Tailscale funnel."""
    url = f"{_ADMIN_BASE}/{path}"
    try:
        if request.method == "POST":
            resp = _requests.post(
                url,
                data=request.get_data(),
                headers={k: v for k, v in request.headers if k.lower() not in ("host", "content-length")},
                cookies=request.cookies,
                timeout=30,
                allow_redirects=False,
            )
        else:
            resp = _requests.get(
                url,
                headers={k: v for k, v in request.headers if k.lower() not in ("host",)},
                cookies=request.cookies,
                timeout=15,
                allow_redirects=False,
            )
        excluded = {"transfer-encoding", "content-encoding", "content-length", "connection"}
        headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded]
        return Response(resp.content, status=resp.status_code, headers=headers)
    except _requests.ConnectionError:
        return Response("後台伺服器未啟動", status=503, content_type="text/plain; charset=utf-8")
