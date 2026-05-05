from __future__ import annotations

from pathlib import Path
import json

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
    assert response.status_code == 302
    assert response.location.endswith("/magi-adjust")


def test_dashboard_pages_render_with_login_required(tmp_path, monkeypatch):
    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    (template_dir / "dashboard.html").write_text("dashboard {{ user.id }}", encoding="utf-8")
    (template_dir / "dashboard_nerv.html").write_text("nerv {{ user.id }}", encoding="utf-8")
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
    assert {"name": "Paperclip", "url": "/osc"} in data["shortcuts"]


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
        "🌐 全球情報面板\n{% for report in reports %}{{ report.name }} {{ report.content }}\n{% endfor %}", 
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
    assert "🌐 全球情報面板" in body
    assert "beta.md" in body or "alpha.md" in body
    assert "Beta report" in body or "Alpha report" in body


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
    assert payload["crawl_targets"][0]["note"] == "每日目標"
    assert payload["digests"][0]["namespace"] == "通譯"


def test_worldmonitor_cron_is_daily():
    cron_path = Path(__file__).resolve().parents[1] / "cron_jobs.json"
    jobs = json.loads(cron_path.read_text(encoding="utf-8"))
    job = next(item for item in jobs if item.get("id") == "job_worldmonitor_intel")

    assert job["cron"] == "0 8 * * *"
    assert "每日" in job["desc"]
