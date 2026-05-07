from __future__ import annotations


def test_default_legal_web_engine_is_selenium(monkeypatch):
    from skills.engine.legal_web_adapter import resolve_legal_web_engine

    monkeypatch.delenv("MAGI_USE_SCRAPLING", raising=False)
    monkeypatch.delenv("MAGI_LEGAL_WEB_ENGINE", raising=False)

    profile = resolve_legal_web_engine("laf_portal")

    assert profile["requested_engine"] == "selenium"
    assert profile["selected_engine"] == "selenium"
    assert profile["fallback_reason"] == ""


def test_scrapling_request_falls_back_for_interactive_legal_flow(monkeypatch):
    from skills.engine.legal_web_adapter import resolve_legal_web_engine

    monkeypatch.setenv("MAGI_USE_SCRAPLING", "1")

    profile = resolve_legal_web_engine("judicial_portal")

    assert profile["requested_engine"] == "scrapling"
    assert profile["selected_engine"] == "selenium"
    assert profile["fallback_reason"] == "interactive_flow_requires_browser_automation"


def test_component_override_can_force_selenium(monkeypatch):
    from skills.engine.legal_web_adapter import resolve_legal_web_engine

    monkeypatch.setenv("MAGI_USE_SCRAPLING", "1")
    monkeypatch.setenv("MAGI_WEB_ENGINE_LAF_PORTAL", "selenium")

    profile = resolve_legal_web_engine("laf_portal")

    assert profile["requested_engine"] == "selenium"
    assert profile["selected_engine"] == "selenium"
