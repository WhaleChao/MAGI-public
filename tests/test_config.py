"""Tests for skills.ops.config — startup validation."""

import os
import pytest


def test_validate_config_passes_with_all_vars(monkeypatch):
    """Should pass when all required env vars are set."""
    monkeypatch.setenv("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("MAGI_LINE_CHANNEL_SECRET", "sec")
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "pass")
    monkeypatch.setenv("FLASK_SECRET_KEY", "key")

    from skills.ops.config import validate_config
    result = validate_config()
    assert result == []


def test_validate_config_fails_on_missing_var(monkeypatch):
    """Should raise RuntimeError when a required var is missing."""
    monkeypatch.setenv("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("MAGI_LINE_CHANNEL_SECRET", "sec")
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("FLASK_SECRET_KEY", "key")
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    from skills.ops.config import validate_config
    with pytest.raises(RuntimeError, match="DB_PASSWORD"):
        validate_config()


def test_validate_config_fails_on_empty_var(monkeypatch):
    """Should raise RuntimeError when a required var is empty string."""
    monkeypatch.setenv("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("MAGI_LINE_CHANNEL_SECRET", "sec")
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_USER", "user")
    monkeypatch.setenv("DB_PASSWORD", "")
    monkeypatch.setenv("FLASK_SECRET_KEY", "key")

    from skills.ops.config import validate_config
    with pytest.raises(RuntimeError, match="DB_PASSWORD"):
        validate_config()
