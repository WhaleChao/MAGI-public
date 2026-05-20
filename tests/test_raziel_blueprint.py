from __future__ import annotations

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
    ]

    combined = "\n".join(path.read_text(encoding="utf-8") for path in visible_templates)

    assert "判決捕捉與分類" in combined
    assert "拉結爾" not in combined
