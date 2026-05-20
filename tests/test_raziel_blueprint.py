from __future__ import annotations


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
