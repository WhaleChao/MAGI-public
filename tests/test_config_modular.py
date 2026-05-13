"""Tests for skills.ops.config — modular config validation."""

import os
import pytest


def test_core_required_vars_missing_raises_error(monkeypatch):
    """Missing any CORE_REQUIRED_VARS should raise RuntimeError."""
    monkeypatch.setenv("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("MAGI_LINE_CHANNEL_SECRET", "sec")
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USER", "user")
    # Missing DB_PASSWORD
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.setenv("FLASK_SECRET_KEY", "key")

    from skills.ops.config import validate_config

    with pytest.raises(RuntimeError, match="DB_PASSWORD"):
        validate_config()


def test_core_vars_present_line_disabled_no_warning(monkeypatch):
    """When LINE is disabled, missing LINE credentials should not warn."""
    # Set all CORE required
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "pass")
    monkeypatch.setenv("FLASK_SECRET_KEY", "key")

    # Disable LINE
    monkeypatch.setenv("MAGI_ENABLE_LINE", "0")
    # Don't set LINE credentials
    monkeypatch.delenv("MAGI_LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MAGI_LINE_CHANNEL_SECRET", raising=False)

    from skills.ops.config import validate_config

    warnings = validate_config()
    # Should not have warnings about LINE
    line_warnings = [w for w in warnings if "LINE" in w or "MAGI_LINE" in w]
    assert len(line_warnings) == 0


def test_line_enabled_credentials_missing_returns_warnings(monkeypatch):
    """When LINE is enabled but credentials missing, should return warnings."""
    # Set all CORE required
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "pass")
    monkeypatch.setenv("FLASK_SECRET_KEY", "key")

    # Enable LINE but don't set credentials
    monkeypatch.setenv("MAGI_ENABLE_LINE", "1")
    monkeypatch.delenv("MAGI_LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MAGI_LINE_CHANNEL_SECRET", raising=False)

    from skills.ops.config import validate_config

    warnings = validate_config()
    # Should have warnings about missing LINE credentials
    assert any("MAGI_LINE_CHANNEL_ACCESS_TOKEN" in w for w in warnings)
    assert any("MAGI_LINE_CHANNEL_SECRET" in w for w in warnings)


def test_all_feature_flags_off_no_warnings(monkeypatch):
    """When all feature flags are off, no feature warnings should be returned."""
    # Set all CORE required
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "pass")
    monkeypatch.setenv("FLASK_SECRET_KEY", "key")

    # Disable all optional features
    monkeypatch.setenv("MAGI_ENABLE_LINE", "0")
    monkeypatch.setenv("MAGI_ENABLE_DISCORD", "0")
    monkeypatch.setenv("MAGI_ENABLE_TELEGRAM", "0")
    monkeypatch.setenv("MAGI_ENABLE_REMOTE_DB", "0")

    from skills.ops.config import validate_config

    warnings = validate_config()
    # Only CORE vars should be validated, no feature warnings
    feature_warnings = [w for w in warnings if any(
        x in w for x in [
            "DISCORD_BOT_TOKEN",
            "OPENCLAW_TELEGRAM_BOT_TOKEN",
            "MAGI_REMOTE_DB_HOST",
        ]
    )]
    assert len(feature_warnings) == 0


def test_backward_compat_required_vars_equals_core_required(monkeypatch):
    """REQUIRED_VARS should equal CORE_REQUIRED_VARS for backward compatibility."""
    from skills.ops.config import REQUIRED_VARS, CORE_REQUIRED_VARS

    assert REQUIRED_VARS == CORE_REQUIRED_VARS


def test_feature_enabled_check_accepts_multiple_truthy_values(monkeypatch):
    """_is_feature_enabled should accept 1, true, yes, on as truthy."""
    from skills.ops.config import _is_feature_enabled

    # Set CORE required
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "pass")
    monkeypatch.setenv("FLASK_SECRET_KEY", "key")

    # Test each truthy value
    for truthy_val in ["1", "true", "yes", "on", "TRUE", "YES", "ON"]:
        monkeypatch.setenv("TEST_FLAG", truthy_val)
        assert _is_feature_enabled("TEST_FLAG") is True

    # Test falsy values
    for falsy_val in ["0", "false", "no", "off", "", "FALSE", "NO", "OFF"]:
        monkeypatch.setenv("TEST_FLAG", falsy_val)
        assert _is_feature_enabled("TEST_FLAG") is False


def test_validate_config_returns_list_of_warnings(monkeypatch):
    """validate_config should return a list (of warnings) not raise on missing features."""
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "pass")
    monkeypatch.setenv("FLASK_SECRET_KEY", "key")
    monkeypatch.setenv("MAGI_ENABLE_DISCORD", "1")
    # Missing DISCORD_BOT_TOKEN
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    from skills.ops.config import validate_config

    result = validate_config()
    assert isinstance(result, list)
    assert any("DISCORD_BOT_TOKEN" in w for w in result)
