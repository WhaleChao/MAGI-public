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
  - /dashboard/pixel
  - /dashboard/nerv

The module is intentionally dependency-light and does not import server.py.
"""

from __future__ import annotations

import html
from pathlib import Path

from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user, login_required

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


@dashboard_pages_bp.route("/dashboard/pixel")
@login_required
def dashboard_pixel():
    return render_template("dashboard_pixel.html", user=current_user)


@dashboard_pages_bp.route("/dashboard/nerv")
@login_required
def dashboard_nerv():
    return render_template("dashboard_nerv.html", user=current_user)
