from __future__ import annotations

import json
import os
import sys
import types
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


def test_tailscale_serve_uses_launchd_safe_absolute_cli(monkeypatch):
    from api.blueprints import dashboard_pages as pages

    seen = {}

    class Result:
        returncode = 0
        stdout = json.dumps(
            {
                "Web": {
                    "aimac-mini.tailnet.test:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:5002"}}
                    }
                }
            }
        )

    monkeypatch.setattr(pages.shutil, "which", lambda _name: None)

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(pages.subprocess, "run", fake_run)

    assert pages._load_tailscale_serve_url() == "https://aimac-mini.tailnet.test"
    assert seen["command"] == [
        "/opt/homebrew/bin/tailscale",
        "serve",
        "status",
        "--json",
    ]
    assert seen["kwargs"]["timeout"] == 2


def test_redirect_routes_point_to_existing_page_targets(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_nerv.html"):
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
    assert response.status_code == 404


def test_dashboard_pages_render_with_login_required(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "dashboard.html").write_text("dashboard {{ user.id }}", encoding="utf-8")
    (template_dir / "dashboard_nerv.html").write_text("nerv {{ user.id }}", encoding="utf-8")
    (template_dir / "dashboard_beginner.html").write_text(
        "beginner {{ user.id }} {{ dashboard.page_label|default('') }}",
        encoding="utf-8",
    )
    (template_dir / "golem_console.html").write_text("golem {{ user.id }}", encoding="utf-8")
    (template_dir / "research.html").write_text("research {{ research.namespace_count }}", encoding="utf-8")
    (template_dir / "mobile_home.html").write_text("mobile {{ mobile.base_url }} {{ user.id }}", encoding="utf-8")
    (template_dir / "mobile_admin.html").write_text("mobile-admin {{ mobile.base_url }} {{ user.id }}", encoding="utf-8")
    monkeypatch.setattr(
        "api.blueprints.dashboard_pages._build_mobile_app_config",
        lambda: {"base_url": "https://magi.tailnet.test", "routes": []},
    )

    app = _make_app(template_dir)
    client = app.test_client()

    response = client.get("/dashboard", headers={"X-User-ID": "u1"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.location.endswith("/golem")

    response = client.get("/dashboard/legacy", headers={"X-User-ID": "u1"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.location.endswith("/golem")

    response = client.get("/golem", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"golem u1" in response.data

    response = client.get("/dashboard/nerv", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"nerv u1" in response.data

    response = client.get("/dashboard/beginner", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"beginner u1" in response.data

    response = client.get("/start", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"beginner u1" in response.data

    response = client.get("/status", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert "beginner u1 MAGI 系統檢測" in response.get_data(as_text=True)

    response = client.get("/dashboard/status", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert "beginner u1 MAGI 系統檢測" in response.get_data(as_text=True)

    response = client.get("/nerv", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"nerv u1" in response.data

    response = client.get("/magi-adjust", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"nerv u1" in response.data

    response = client.get("/research", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"research" in response.data

    response = client.get("/mobile", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"mobile https://magi.tailnet.test" in response.data

    response = client.get("/mobile-admin", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert b"mobile-admin https://magi.tailnet.test" in response.data


def test_maintenance_manual_assets_require_login_and_are_exact(tmp_path, monkeypatch):
    import api.blueprints.dashboard_pages as pages

    root = tmp_path / "release"
    assets = root / "magi_v3" / "manual_assets"
    assets.mkdir(parents=True)
    payloads = {
        "MAGI_V3_維修百科全書_rc627.html": b"<!doctype html><title>MAGI manual</title>",
        "MAGI_V3_維修百科全書_rc627.pdf": b"%PDF-1.4\n%%EOF\n",
        "MAGI_V3_維修百科全書_rc627.md": "# 維修百科\n".encode(),
        "MAGI_V3_原始碼索引_rc627.json": b'{"schema":"magi.source-index/v1"}',
    }
    for name, data in payloads.items():
        (assets / name).write_bytes(data)
    monkeypatch.setattr(pages, "_MAGI_ROOT", root)

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    app = _make_app(template_dir)

    @app.route("/login")
    def login():
        return "login"

    client = app.test_client()
    for url in ("/manual", "/manual/pdf", "/manual/markdown", "/manual/source-index.json"):
        assert client.get(url).status_code in {302, 401}

    expected = {
        "/manual": payloads["MAGI_V3_維修百科全書_rc627.html"],
        "/manual/pdf": payloads["MAGI_V3_維修百科全書_rc627.pdf"],
        "/manual/markdown": payloads["MAGI_V3_維修百科全書_rc627.md"],
        "/manual/source-index.json": payloads["MAGI_V3_原始碼索引_rc627.json"],
    }
    for url, body in expected.items():
        response = client.get(url, headers={"X-User-ID": "u1"})
        assert response.status_code == 200
        assert response.data == body
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_dashboard_templates_expose_system_detection_entry():
    root = Path(__file__).resolve().parents[1]

    golem = (root / "templates" / "golem_console.html").read_text(encoding="utf-8")
    beginner = (root / "templates" / "dashboard_beginner.html").read_text(encoding="utf-8")
    legacy_dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
    status_page = (root / "templates" / "dashboard_beginner.html").read_text(encoding="utf-8")
    adjust_page = (root / "templates" / "dashboard_nerv.html").read_text(encoding="utf-8")

    assert 'href="/status">系統檢測' in golem
    assert 'href="/osc?tab=todos"' in golem
    assert 'href="https://calendar.google.com/calendar/u/0/r" target="_blank" rel="noopener noreferrer"' in golem
    assert 'href="/sentencing-trends">判決趨勢' in golem
    assert "dashboard.capabilities_action" in beginner
    assert 'href="/status">系統檢測' in beginner
    assert "[ 系統檢測 / 狀態中心 ]" in legacy_dashboard
    assert "dashboard.page_title" in status_page
    assert "dashboard.quick_links" in status_page
    assert "MAGI 調整" in adjust_page


def test_status_dashboard_health_poll_handles_login_redirect():
    root = Path(__file__).resolve().parents[1]
    status_page = (root / "templates" / "dashboard_nerv.html").read_text(encoding="utf-8")

    assert "redirect: 'manual'" in status_page
    assert "credentials: 'same-origin'" in status_page
    assert "cache: 'no-store'" in status_page
    assert "/status/api/health?_=${Date.now()}" in status_page
    assert "healthPollInFlight" in status_page
    assert "scheduleHealthPoll" in status_page
    assert "visibilitychange" in status_page
    assert "window.addEventListener('focus', refreshHealthNow)" in status_page
    assert 'id="health-refresh-state"' in status_page
    assert "資料：尚未同步" in status_page
    assert "需要重新登入" in status_page


def test_status_dashboard_uses_live_health_over_stale_reports(tmp_path, monkeypatch):
    import api.blueprints.dashboard_pages as pages

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "system_test_report.json").write_text(
        json.dumps(
            {
                "total": 12,
                "passed": 11,
                "failed": 1,
                "timestamp": "2026-07-02T02:31:17",
                "tests": [
                    {
                        "id": "autopilot_schedule",
                        "label": "夜間排程 (AUTOPILOT)",
                        "pass": False,
                        "detail": "cron_jobs.json 有 80 個任務，但 discord_bot.py 未運行",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (static_dir / "integration_smoke_latest.json").write_text(
        json.dumps(
            {
                "overall_ok": False,
                "generated_at": "2000-01-01T00:00:00",
                "checks": [{"name": "old_failure", "ok": False, "summary": "historical failure"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (static_dir / "magi_status.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(pages, "_MAGI_ROOT", tmp_path)
    monkeypatch.setattr(
        pages,
        "_load_live_health_snapshot",
        lambda: {"status": "operational", "operational_health": {"ok": True}},
    )

    dashboard = pages._build_status_dashboard()

    assert dashboard["readiness"]["status"] == "pass"
    assert "即時 health" in dashboard["readiness"]["detail"]
    assert dashboard["failed_smoke"] == []
    schedule = next(item for item in dashboard["capabilities"] if item["name"] == "排程與背景任務")
    assert schedule["status"] == "pass"
    assert "已恢復" in schedule["detail"]
    smoke = next(item for item in dashboard["evidence"] if item["name"] == "整合 smoke")
    assert smoke["status"] == "warn"
    assert "歷史報告已過期" in smoke["summary"]
    system = next(item for item in dashboard["evidence"] if item["name"] == "系統自測")
    assert system["status"] == "warn"
    assert "確認恢復" in system["summary"]


def test_status_dashboard_accepts_readyz_contract(tmp_path, monkeypatch):
    import api.blueprints.dashboard_pages as pages

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "system_test_report.json").write_text("{}", encoding="utf-8")
    (static_dir / "integration_smoke_latest.json").write_text("{}", encoding="utf-8")
    (static_dir / "magi_status.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pages, "_MAGI_ROOT", tmp_path)
    monkeypatch.setattr(pages, "_load_live_health_snapshot", lambda: {"ok": True, "status": "ready"})

    dashboard = pages._build_status_dashboard()

    assert dashboard["readiness"]["status"] == "pass"
    live = next(item for item in dashboard["evidence"] if item["name"] == "即時 health")
    assert live["status"] == "pass"
    assert live["summary"] == "目前 ready"


def test_status_dashboard_uses_current_scheduler_owner_and_count(tmp_path, monkeypatch):
    import api.blueprints.dashboard_pages as pages

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "system_test_report.json").write_text(
        json.dumps(
            {
                "tests": [
                    {
                        "id": "autopilot_schedule",
                        "pass": True,
                        "detail": "discord_bot.py 運行中 (pid=71516)",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (static_dir / "integration_smoke_latest.json").write_text("{}", encoding="utf-8")
    (static_dir / "magi_status.json").write_text("{}", encoding="utf-8")
    lock_dir = tmp_path / ".runtime" / "locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "cron_scheduler_owner.lock.json").write_text(
        json.dumps({"pid": os.getpid(), "owner": "discord_internal_cron"}),
        encoding="utf-8",
    )
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps([{"enabled": True}, {"enabled": True}, {"enabled": False}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(pages, "_MAGI_ROOT", tmp_path)
    monkeypatch.setattr(pages, "_load_live_health_snapshot", lambda: {"ok": True, "status": "ready"})

    dashboard = pages._build_status_dashboard()

    schedule = next(item for item in dashboard["capabilities"] if item["name"] == "排程與背景任務")
    assert schedule["status"] == "pass"
    assert f"pid={os.getpid()}" in schedule["detail"]
    assert "2 個任務啟用" in schedule["detail"]
    assert "71516" not in schedule["detail"]


def test_dashboard_and_mobile_pages_require_login_when_unauthenticated(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in (
        "dashboard.html",
        "dashboard_nerv.html",
        "dashboard_beginner.html",
        "golem_console.html",
        "research.html",
        "mobile_home.html",
        "mobile_admin.html",
    ):
        (template_dir / name).write_text("protected", encoding="utf-8")
    monkeypatch.setattr(
        "api.blueprints.dashboard_pages._build_mobile_app_config",
        lambda: {"base_url": "https://magi.tailnet.test", "routes": []},
    )

    app = _make_app(template_dir)

    @app.route("/login")
    def login():
        return "login"

    client = app.test_client()
    protected_paths = [
        "/dashboard",
        "/dashboard/beginner",
        "/start",
        "/status",
        "/dashboard/status",
        "/dashboard/nerv",
        "/golem",
        "/research",
        "/mobile",
        "/app",
        "/mobile-admin",
        "/app-admin",
        "/dashboard/website",
        "/wa/",
    ]
    for path in protected_paths:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302, path
        assert "/login" in response.location, path
        assert "next=" in response.location, path


def test_mobile_config_and_manifest_routes(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_nerv.html"):
        (template_dir / name).write_text("{{ user.id }}", encoding="utf-8")
    monkeypatch.setattr(
        "api.blueprints.dashboard_pages._build_mobile_app_config",
        lambda: {
            "app_name": "MAGI Mobile",
            "base_url": "https://magi.tailnet.test",
            "tailscale_dns": "magi.tailnet.test",
            "tailscale_ip": "100.64.1.2",
            "tailscale_online": True,
            "android_package": "tw.local.magi.mobile",
            "ios_bundle_id": "tw.local.magi.mobile",
            "routes": [
                {"label": "Paperclip", "path": "/osc", "kind": "core"},
                {"label": "手機後台", "path": "/mobile-admin", "kind": "admin"},
            ],
        },
    )

    app = _make_app(template_dir)
    client = app.test_client()

    response = client.get("/mobile/config.json", headers={"X-User-ID": "u1"})
    assert response.status_code == 200
    assert response.get_json()["base_url"] == "https://magi.tailnet.test"

    response = client.get("/mobile/manifest.webmanifest")
    assert response.status_code == 200
    data = response.get_json()
    assert data["start_url"] == "/mobile"
    assert data["scope"] == "/mobile"
    assert {"name": "Paperclip", "url": "/osc"} in data["shortcuts"]

    response = client.get("/mobile/sw.js")
    assert response.status_code == 200
    assert response.headers["Service-Worker-Allowed"] == "/mobile"


def test_pixel_dashboard_route_is_removed(tmp_path):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_nerv.html"):
        (template_dir / name).write_text("{{ user.id }}", encoding="utf-8")

    app = _make_app(template_dir)
    client = app.test_client()

    response = client.get("/dashboard/pixel", headers={"X-User-ID": "u1"}, follow_redirects=False)
    assert response.status_code == 404


def test_intel_page_lists_recent_reports(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_nerv.html"):
        (template_dir / name).write_text("{{ user.id }}", encoding="utf-8")
    
    # Add the missing intel.html mock template
    (template_dir / "intel.html").write_text(
        "🌐 全球情報總覽\n{% for report in reports %}{{ report.name }} {{ report.content }}\n{% endfor %}",
        encoding="utf-8"
    )

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
    assert "🌐 全球情報總覽" in body
    assert "beta.md" in body or "alpha.md" in body
    assert "Beta report" in body or "Alpha report" in body


def test_intel_refresh_runs_local_worldmonitor_skill(tmp_path, monkeypatch):
    from api.blueprints import dashboard_pages as mod

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_nerv.html", "intel.html"):
        (template_dir / name).write_text("{{ user.id }}", encoding="utf-8")

    root = tmp_path / "magi"
    action_path = root / "skills" / "worldmonitor-intel" / "action.py"
    action_path.parent.mkdir(parents=True)
    action_path.write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_MAGI_ROOT", root)

    calls = []

    class _Result:
        returncode = 0
        stdout = "updated"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Result()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    app = _make_app(template_dir)
    client = app.test_client()
    response = client.post("/api/intel/refresh", headers={"X-User-ID": "u1"}, follow_redirects=False)

    assert response.status_code == 302
    assert response.location.endswith("/intel?refresh=ok")
    assert calls
    assert calls[0][0][-4:] == ["--task", "collect", "--no-reasoning", "--plain-output"]
    assert calls[0][1]["cwd"] == str(root)


def test_intel_refresh_returns_json_for_ajax(tmp_path, monkeypatch):
    from api.blueprints import dashboard_pages as mod

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_nerv.html", "intel.html"):
        (template_dir / name).write_text("{{ user.id }}", encoding="utf-8")

    root = tmp_path / "magi"
    action_path = root / "skills" / "worldmonitor-intel" / "action.py"
    action_path.parent.mkdir(parents=True)
    action_path.write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_MAGI_ROOT", root)
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "updated", "stderr": ""})(),
    )

    app = _make_app(template_dir)
    client = app.test_client()
    response = client.post(
        "/api/intel/refresh",
        headers={"X-User-ID": "u1", "Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "message": "updated"}


def test_legacy_api_skills_run_delegates_to_canonical_helper(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_nerv.html", "intel.html"):
        (template_dir / name).write_text("{{ user.id }}", encoding="utf-8")

    calls = []
    fake_tools_api = types.ModuleType("api.tools_api")

    def _fake_run_skill_from_payload(data, *, user_id="api"):
        from flask import jsonify

        calls.append({"data": dict(data), "user_id": user_id})
        return jsonify({"success": True, "skill": data.get("skill"), "task": data.get("task")}), 200

    fake_tools_api._run_skill_from_payload = _fake_run_skill_from_payload
    monkeypatch.setitem(sys.modules, "api.tools_api", fake_tools_api)

    app = _make_app(template_dir)
    client = app.test_client()
    response = client.post(
        "/api/skills/run",
        data={"skill": "worldmonitor-intel", "task": "collect"},
        headers={"X-User-ID": "u1"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert calls == [{"data": {"skill": "worldmonitor-intel", "task": "collect"}, "user_id": "u1"}]


def test_legacy_api_skills_run_error_names_canonical_endpoint(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    for name in ("dashboard.html", "dashboard_nerv.html", "intel.html"):
        (template_dir / name).write_text("{{ user.id }}", encoding="utf-8")

    fake_tools_api = types.ModuleType("api.tools_api")

    def _boom(data, *, user_id="api"):
        raise RuntimeError("canonical unavailable")

    fake_tools_api._run_skill_from_payload = _boom
    monkeypatch.setitem(sys.modules, "api.tools_api", fake_tools_api)

    app = _make_app(template_dir)
    client = app.test_client()
    response = client.post(
        "/api/skills/run",
        json={"skill": "translator", "task": "help"},
        headers={"X-User-ID": "u1"},
    )

    assert response.status_code == 503
    assert response.get_json()["canonical_endpoint"] == "/skills/run"


def test_intel_reports_are_sorted_by_filename_time_and_skip_placeholder(tmp_path, monkeypatch):
    from api.blueprints import dashboard_pages as mod

    reports_dir = tmp_path / "worldmonitor_reports"
    reports_dir.mkdir()
    (reports_dir / "intel_20260504_092000.md").write_text("早上的完整報告", encoding="utf-8")
    (reports_dir / "intel_20260504_122537.md").write_text("payload", encoding="utf-8")
    (reports_dir / "intel_20260504_123000.md").write_text("[推理失敗] HTTP 404", encoding="utf-8")
    (reports_dir / "intel_20260504_124000.md").write_text("AP News: FAIL (fetch failed)", encoding="utf-8")
    (reports_dir / "intel_20260504_125000.md").write_text("市場資料：DEGRADED (FINNHUB_API_KEY 未設定，市場行情已停用)", encoding="utf-8")
    (reports_dir / "intel_20260504_132537.md").write_text("下午的完整報告", encoding="utf-8")
    monkeypatch.setattr(mod, "_WORLDMONITOR_REPORT_DIR", reports_dir)

    reports = mod._iter_worldmonitor_reports()

    assert [r["name"] for r in reports] == [
        "intel_20260504_132537.md",
        "intel_20260504_092000.md",
    ]
    assert reports[0]["date_display"] == "2026-05-04 13:25"
    visible_content = "\n".join(r["content"] for r in reports)
    assert "payload" not in visible_content
    assert "[推理失敗]" not in visible_content
    assert "AP News: FAIL" not in visible_content
    assert "FINNHUB_API_KEY 未設定" not in visible_content
    assert reports[0]["is_placeholder"] is False
    assert reports[1]["is_placeholder"] is False


def test_intel_report_loads_readable_sections_and_source_links(tmp_path, monkeypatch):
    from api.blueprints import dashboard_pages as mod

    reports_dir = tmp_path / "worldmonitor_reports"
    reports_dir.mkdir()
    report = reports_dir / "intel_20260505_080000.md"
    report.write_text(
        """# MAGI 全球情報摘要
**時間**: 2026-05-05 08:00:00
**新聞來源**: 1 篇 | **市場**: 0 檔

---

## 重大事件概述
- [BBC World] [測試新聞：摘要](https://example.com/news)

---
## 🩺 來源健康狀態
- 新聞來源：1/1 成功
- BBC World: OK (1 篇)

---
<details><summary>原始資料</summary>
- raw markdown should not be primary UI
</details>
""",
        encoding="utf-8",
    )
    report.with_suffix(".json").write_text(
        json.dumps(
            {
                "news_items": [
                    {
                        "source": "BBC World",
                        "title": "測試新聞",
                        "summary": "這是一段摘要",
                        "link": "https://example.com/news",
                    }
                ],
                "news_statuses": [{"source": "BBC World", "ok": True, "count": 1}],
                "market_status": {"ok": False, "detail": "未設定"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_WORLDMONITOR_REPORT_DIR", reports_dir)

    reports = mod._iter_worldmonitor_reports()

    assert reports[0]["sections"][0]["title"] == "重大事件概述"
    assert reports[0]["sections"][0]["items"] == ["[BBC World] 測試新聞：摘要"]
    assert reports[0]["news_items"][0]["link"] == "https://example.com/news"
    assert reports[0]["source_health"][0] == "新聞來源：1/1 成功"


def test_intel_report_parser_handles_numbered_markdown_sections():
    from api.blueprints import dashboard_pages as mod

    parsed = mod._parse_worldmonitor_markdown(
        """# 🌐 MAGI 全球情報摘要
**時間**: 2026-05-07 08:00:00

---

## 1. 重大事件概述
1. [BBC World] 第一則新聞：摘要
2. [NHK Asia] 第二則新聞：摘要

## 2. 對台灣與亞太的潛在影響
- 供應鏈與能源價格需觀察。

## 🩺 來源健康狀態
1. 新聞來源：2/2 成功
"""
    )

    assert parsed["sections"][0]["title"] == "重大事件概述"
    assert parsed["sections"][0]["items"] == ["[BBC World] 第一則新聞：摘要", "[NHK Asia] 第二則新聞：摘要"]
    assert parsed["source_health"] == ["新聞來源：2/2 成功"]


def test_research_dashboard_loads_namespaces_crawler_targets_and_digests(tmp_path, monkeypatch):
    from api.blueprints import dashboard_pages as mod

    root = tmp_path / "magi"
    ns_dir = root / ".runtime" / "research_brief" / "namespaces"
    ns_dir.mkdir(parents=True)
    (ns_dir / "通譯.json").write_text(
        '{"namespace":"通譯","topic_key":"research_interpretation","keywords":["司法通譯"],'
        '"sources":[{"url":"https://example.test/feed","type":"rss","lang":"zh-Hant","note":"測試來源"}]}',
        encoding="utf-8",
    )
    (root / "_crawl_targets.json").write_text(
        '{"targets":[{"url":"https://example.test/daily","note":"每日目標"}]}',
        encoding="utf-8",
    )
    digest_path = root / ".runtime" / "research_brief" / "last_digest.jsonl"
    digest_path.write_text('{"namespace":"通譯","count":2,"ts":"2026-05-05T00:00:00Z"}\n', encoding="utf-8")
    monkeypatch.setattr(mod, "_MAGI_ROOT", root)

    payload = mod._load_research_dashboard()

    assert payload["namespace_count"] == 1
    assert payload["source_total"] == 1
    assert payload["namespaces"][0]["topic_key"] == "research_interpretation"
    assert payload["namespaces"][0]["sources"][0]["is_feed"] is True
    assert payload["namespaces"][0]["sources"][0]["open_url"].startswith("/research/rss-preview?")
    assert payload["crawl_targets"][0]["note"] == "每日目標"
    assert payload["digests"][0]["namespace"] == "通譯"


def test_research_rss_preview_parses_feed_instead_of_showing_xml(tmp_path, monkeypatch):
    from api.blueprints import dashboard_pages as mod

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "rss_preview.html").write_text(
        "{{ feed.title }} {% for item in feed['items'] %}{{ item.title }} {{ item.link }} {% endfor %}",
        encoding="utf-8",
    )
    (template_dir / "research.html").write_text(
        "{% for ns in research.namespaces %}{% for source in ns.sources %}{{ source.open_url }} {% endfor %}{% endfor %}",
        encoding="utf-8",
    )
    root = tmp_path / "magi"
    ns_dir = root / ".runtime" / "research_brief" / "namespaces"
    ns_dir.mkdir(parents=True)
    (ns_dir / "通譯.json").write_text(
        json.dumps({
            "namespace": "通譯",
            "sources": [{"url": "https://criticallink.org/feed/", "type": "rss", "note": "Critical Link"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_MAGI_ROOT", root)

    rss_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Critical Link International</title>
<item><title>Second International Conference</title><link>https://criticallink.org/event/</link>
<description><![CDATA[<p>Tokyo conference summary.</p>]]></description></item>
</channel></rss>"""

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return rss_xml

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *_a, **_k: _Response())

    app = _make_app(template_dir)
    client = app.test_client()

    research_response = client.get("/research", headers={"X-User-ID": "u1"})
    assert research_response.status_code == 200
    research_body = research_response.get_data(as_text=True)
    assert "/research/rss-preview?" in research_body
    assert "https://criticallink.org/feed/" not in research_body

    preview_response = client.get(
        "/research/rss-preview?url=https%3A%2F%2Fcriticallink.org%2Ffeed%2F",
        headers={"X-User-ID": "u1"},
    )
    assert preview_response.status_code == 200
    body = preview_response.get_data(as_text=True)
    assert "Critical Link International" in body
    assert "Second International Conference" in body
    assert "<?xml" not in body


def test_worldmonitor_cron_is_daily():
    import shlex

    from magi_v3.external_inputs import load_bound_cron_jobs

    root = Path(__file__).resolve().parents[1]
    jobs = list(load_bound_cron_jobs(root, missing_source_default=False).jobs)
    job = next(item for item in jobs if item.get("id") == "job_worldmonitor_intel")

    assert job["cron"] == "0 8 * * *"
    argv = shlex.split(job["command"])
    action_index = next(
        idx for idx, item in enumerate(argv) if item.endswith("worldmonitor-intel/action.py")
    )
    assert argv[action_index + 1 :] == [
        "--task",
        "collect",
        "--no-reasoning",
        "--plain-output",
    ]
    assert "每日" in job["desc"]
