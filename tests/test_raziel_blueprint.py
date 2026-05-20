from __future__ import annotations

import os
import zipfile
from pathlib import Path


def test_raziel_terms_from_boolean_query_keeps_positive_terms():
    from api.blueprints.raziel import _terms_from_query

    terms = _terms_from_query('"會員代表大會" AND 類推適用 AND 民法第56條 NOT 草案')

    assert terms == ["會員代表大會", "類推適用", "民法第56條"]


def test_raziel_public_config_never_returns_api_key():
    from api.blueprints.raziel import _public_config

    public = _public_config(
        {
            "keyword_query": "通譯",
            "nvidia_api_key": "secret-value",
            "nvidia_model": "meta/llama-3.1-405b-instruct",
        }
    )

    assert public["has_nvidia_api_key"] is True
    assert "nvidia_api_key" not in public


def test_judgment_classifier_visible_text_uses_function_name():
    root = Path(__file__).resolve().parents[1]
    visible_templates = [
        root / "templates" / "golem_console.html",
        root / "templates" / "research.html",
        root / "templates" / "research_judgment_classifier.html",
        root / "templates" / "partials" / "osc" / "raziel.html",
        root / "static" / "osc" / "tabs" / "raziel.js",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8") for path in visible_templates)

    assert "判決捕捉與分類" in combined
    assert "拉結爾" not in combined
    assert "專案資料夾" not in combined
    assert "最高法院_通譯_TXT" not in combined


def test_raziel_delivery_zip_splits_when_limit_is_small(tmp_path, monkeypatch):
    from api.blueprints import raziel as mod

    root = tmp_path / "judgments"
    complete = root / "完整812"
    (complete / "TXT").mkdir(parents=True)
    (complete / "PDF").mkdir(parents=True)
    (complete / "TXT" / "a.txt").write_text("判決原文" * 80, encoding="utf-8")
    (complete / "PDF" / "a.pdf").write_bytes(b"%PDF-1.4\n" + os.urandom(4096))
    (complete / "最高法院_通譯_分類表.csv").write_text("title,result\nA,ok\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_RAZIEL_ROOT", str(root))

    manifest = mod._write_delivery_zip({"keyword_text_dir_name": "TXT", "keyword_pdf_dir_name": "PDF"}, split_bytes=512)

    assert manifest["ok"] is True
    assert manifest["split"] is True
    assert manifest["file_count"] >= 3
    assert len(manifest["parts"]) >= 2
    assert all(Path(part["path"]).exists() for part in manifest["parts"])


def test_raziel_delivery_zip_uses_generic_archive_names(tmp_path, monkeypatch):
    from api.blueprints import raziel as mod

    root = tmp_path / "judgments"
    complete = root / "完整812"
    (complete / "TXT").mkdir(parents=True)
    (complete / "依關鍵字原文").mkdir(parents=True)
    (complete / "TXT" / "a.txt").write_text("判決原文", encoding="utf-8")
    (complete / "依關鍵字原文" / "keyword.txt").write_text("關鍵字原文", encoding="utf-8")
    (complete / "最高法院_通譯_分類表.xlsx").write_bytes(b"fake-xlsx")
    (complete / "通譯812補抓分析報告.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MAGI_RAZIEL_ROOT", str(root))

    manifest = mod._write_delivery_zip(
        {"keyword_text_dir_name": "依關鍵字原文", "keyword_pdf_dir_name": "依關鍵字PDF"},
        split_bytes=1024 * 1024,
    )

    assert manifest["split"] is False
    zip_path = Path(manifest["parts"][0]["path"])
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    assert "判決捕捉與分類_交付資料/分類表.xlsx" in names
    assert "判決捕捉與分類_交付資料/補抓分析報告.json" in names
    assert "判決捕捉與分類_交付資料/判決原文_TXT/a.txt" in names
    assert "判決捕捉與分類_交付資料/依關鍵字原文/keyword.txt" in names
    assert not any("通譯" in name or "完整812" in name for name in names)


def test_raziel_delivery_zip_uses_generic_keyword_folder_names(tmp_path, monkeypatch):
    from api.blueprints import raziel as mod

    root = tmp_path / "judgments"
    keyword_dir = root / "完整812" / "依關鍵字原文" / "通譯"
    keyword_dir.mkdir(parents=True)
    (keyword_dir / "a.txt").write_text("關鍵字命中原文", encoding="utf-8")
    monkeypatch.setenv("MAGI_RAZIEL_ROOT", str(root))

    manifest = mod._write_delivery_zip({"keyword_text_dir_name": "依關鍵字原文"}, split_bytes=1024 * 1024)

    with zipfile.ZipFile(manifest["parts"][0]["path"]) as zf:
        names = zf.namelist()
        mapping = zf.read("判決捕捉與分類_交付資料/關鍵字資料夾對照表.json").decode("utf-8")

    assert "判決捕捉與分類_交付資料/依關鍵字原文/關鍵字01/a.txt" in names
    assert not any("/通譯/" in name for name in names)
    assert '"原關鍵字": "通譯"' in mapping
