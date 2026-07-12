from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from api import startup


def test_export_file_meta_uses_paperclip_file_content_route(monkeypatch, tmp_path):
    docx_path = tmp_path / "委任狀.docx"
    docx_path.write_text("placeholder", encoding="utf-8")

    monkeypatch.setattr(startup, "_load_public_base_url", lambda: "https://magi.example.test")
    meta = startup._export_file_meta(str(docx_path))

    parsed = urlparse(meta["url"])
    assert parsed.path == "/api/osc/files/content"
    assert "static/exports" not in meta["url"]
    assert unquote(parse_qs(parsed.query)["path"][0]) == str(Path(docx_path).resolve())
