from __future__ import annotations

from api import line_compat
from scripts.ops import token_health_check


def _clear_line_environment(monkeypatch):
    for name in (
        "MAGI_ENABLE_LINE",
        "MAGI_LINE_CHANNEL_ACCESS_TOKEN",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "MAGI_LINE_CHANNEL_SECRET",
        "LINE_CHANNEL_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_line_is_disabled_by_default_when_credentials_are_absent(monkeypatch):
    _clear_line_environment(monkeypatch)

    assert line_compat.line_feature_enabled() is False
    line_checks = {
        spec.name: token_health_check.check_api_key(spec)
        for spec in token_health_check._discover_api_keys()
        if spec.name in {"line_messaging", "line_channel_secret"}
    }
    assert set(line_checks) == {"line_messaging", "line_channel_secret"}
    assert all(item["status"] == "skipped" for item in line_checks.values())
    assert all(item["ok"] is True for item in line_checks.values())


def test_line_is_required_only_after_explicit_enable(monkeypatch):
    _clear_line_environment(monkeypatch)
    monkeypatch.setenv("MAGI_ENABLE_LINE", "1")

    assert line_compat.line_feature_enabled() is True
    line_checks = {
        spec.name: token_health_check.check_api_key(spec)
        for spec in token_health_check._discover_api_keys()
        if spec.name in {"line_messaging", "line_channel_secret"}
    }
    assert all(item["status"] == "missing_key" for item in line_checks.values())
    assert all(item["ok"] is False for item in line_checks.values())


def test_existing_credential_pair_enables_line_when_flag_is_absent(monkeypatch):
    _clear_line_environment(monkeypatch)
    monkeypatch.setenv("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "configured-token")
    monkeypatch.setenv("MAGI_LINE_CHANNEL_SECRET", "configured-secret")

    assert line_compat.line_feature_enabled() is True


def test_explicit_disable_wins_over_existing_credentials(monkeypatch):
    _clear_line_environment(monkeypatch)
    monkeypatch.setenv("MAGI_ENABLE_LINE", "0")
    monkeypatch.setenv("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "configured-token")
    monkeypatch.setenv("MAGI_LINE_CHANNEL_SECRET", "configured-secret")

    assert line_compat.line_feature_enabled() is False
