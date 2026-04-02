from __future__ import annotations


def test_line_clients_disabled_when_feature_flag_off(monkeypatch):
    from api import line_compat

    monkeypatch.setenv("MAGI_ENABLE_LINE", "0")
    line_bot_api, handler, enabled, reason = line_compat.build_line_clients("token", "secret")

    assert enabled is False
    assert "disabled" in reason
    assert hasattr(handler, "add")
    assert line_bot_api is not None


def test_line_feature_enabled_accepts_truthy_values(monkeypatch):
    from api.line_compat import line_feature_enabled

    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("MAGI_ENABLE_LINE", value)
        assert line_feature_enabled() is True

    for value in ("0", "false", "FALSE", "no", "off", ""):
        monkeypatch.setenv("MAGI_ENABLE_LINE", value)
        assert line_feature_enabled() is False
