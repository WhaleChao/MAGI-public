from pathlib import Path

from api import startup
from skills.ops import export_text


def test_export_text_publishes_authenticated_web_route(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    monkeypatch.setattr(startup, "EXPORTS_DIR", str(exports))
    monkeypatch.setattr(
        startup,
        "_load_public_base_url",
        lambda: "https://aimac-mini.tail6738b7.ts.net/",
    )

    result = startup._export_text_to_static("股票報告", prefix="market briefing")

    assert result["success"] is True
    assert result["url"].startswith(
        "https://aimac-mini.tail6738b7.ts.net/exports/"
    )
    assert "/static/exports/" not in result["url"]
    assert Path(result["path"]).read_text(encoding="utf-8").strip() == "股票報告"


def test_existing_export_file_uses_authenticated_web_route(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    report = exports / "daily reports" / "台股 報告.txt"
    report.parent.mkdir(parents=True)
    report.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(startup, "EXPORTS_DIR", str(exports))
    monkeypatch.setattr(
        startup,
        "_load_public_base_url",
        lambda: "https://aimac-mini.tail6738b7.ts.net",
    )

    url = startup._public_url_for_local_file(str(report))

    assert url == (
        "https://aimac-mini.tail6738b7.ts.net/exports/"
        "daily%20reports/%E5%8F%B0%E8%82%A1%20%E5%A0%B1%E5%91%8A.txt"
    )
    assert "/static/exports/" not in url


def test_non_export_static_asset_keeps_static_route(tmp_path, monkeypatch):
    fake_root = tmp_path / "magi"
    fake_api = fake_root / "api"
    static_dir = fake_root / "static"
    asset = static_dir / "preview image.png"
    fake_api.mkdir(parents=True)
    static_dir.mkdir()
    asset.write_bytes(b"png")
    monkeypatch.setattr(startup, "__file__", str(fake_api / "startup.py"))
    monkeypatch.setattr(
        startup,
        "_load_public_base_url",
        lambda: "https://aimac-mini.tail6738b7.ts.net",
    )

    url = startup._public_url_for_local_file(str(asset))

    assert url == (
        "https://aimac-mini.tail6738b7.ts.net/static/"
        "preview%20image.png"
    )


def test_shared_export_helper_uses_authenticated_web_route(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    monkeypatch.setattr(export_text, "EXPORTS_DIR", str(exports))
    monkeypatch.setattr(
        export_text,
        "_load_public_base_url",
        lambda: "https://aimac-mini.tail6738b7.ts.net/",
    )

    result = export_text.export_txt("股票報告", prefix="market briefing")

    assert result["success"] is True
    assert result["url"].startswith(
        "https://aimac-mini.tail6738b7.ts.net/exports/"
    )
    assert "/static/exports/" not in result["url"]
    assert "%20" in result["url"]
    assert Path(result["path"]).read_text(encoding="utf-8").strip() == "股票報告"


def test_authenticated_export_route_serves_shared_export_dir(tmp_path, monkeypatch):
    from api import server

    exports = tmp_path / "static" / "exports"
    exports.mkdir(parents=True)
    report = exports / "market_briefing_20260727_085239_24b58b3cc7.txt"
    report.write_text("股票報告可讀", encoding="utf-8")
    monkeypatch.setattr(server, "EXPORTS_DIR", str(exports))
    monkeypatch.setattr(
        server.login_manager,
        "_user_callback",
        lambda user_id: server.User(user_id, "tester", "admin"),
    )

    client = server.app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = "1"
        session["_fresh"] = True

    response = client.get(f"/exports/{report.name}")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "股票報告可讀"
