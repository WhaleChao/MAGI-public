# -*- coding: utf-8 -*-
"""Tests for skills/ops/keychain_manager.py — macOS Keychain integration."""

import os
from unittest.mock import patch, MagicMock

import pytest

from skills.ops.keychain_manager import (
    get_secret,
    set_secret,
    delete_secret,
    resolve_keychain_value,
    load_config_with_keychain,
    migrate_env_to_keychain,
    _is_sensitive_key,
    SERVICE_PREFIX,
)


class TestIsSensitiveKey:
    def test_token(self):
        assert _is_sensitive_key("DISCORD_TOKEN") is True

    def test_secret(self):
        assert _is_sensitive_key("LINE_CHANNEL_SECRET") is True

    def test_password(self):
        assert _is_sensitive_key("DB_PASSWORD") is True

    def test_api_key(self):
        assert _is_sensitive_key("OPENAI_API_KEY") is True

    def test_non_sensitive(self):
        assert _is_sensitive_key("FLASK_DEBUG") is False

    def test_non_sensitive_host(self):
        assert _is_sensitive_key("DB_HOST") is False


class TestGetSecret:
    @patch("skills.ops.keychain_manager.IS_MACOS", True)
    @patch("skills.ops.keychain_manager.subprocess.run")
    def test_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="my_secret_value\n")
        assert get_secret("test_key") == "my_secret_value"
        cmd = mock_run.call_args[0][0]
        assert f"{SERVICE_PREFIX}.test_key" in cmd

    @patch("skills.ops.keychain_manager.IS_MACOS", True)
    @patch("skills.ops.keychain_manager.subprocess.run")
    def test_not_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=44)
        assert get_secret("nonexistent") is None

    @patch("skills.ops.keychain_manager.IS_MACOS", False)
    def test_not_macos(self):
        assert get_secret("test_key") is None


class TestSetSecret:
    @patch("skills.ops.keychain_manager.IS_MACOS", True)
    @patch("skills.ops.keychain_manager.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert set_secret("test_key", "test_value") is True

    @patch("skills.ops.keychain_manager.IS_MACOS", False)
    def test_not_macos(self):
        assert set_secret("test_key", "test_value") is False


class TestResolveKeychainValue:
    def test_non_keychain_value(self):
        assert resolve_keychain_value("plain_text") == "plain_text"

    def test_non_string(self):
        assert resolve_keychain_value(42) == 42

    @patch("skills.ops.keychain_manager.get_secret")
    def test_keychain_prefix(self, mock_get):
        mock_get.return_value = "actual_secret"
        assert resolve_keychain_value("keychain:my_key") == "actual_secret"

    @patch("skills.ops.keychain_manager.get_secret")
    def test_keychain_not_found(self, mock_get):
        mock_get.return_value = None
        result = resolve_keychain_value("keychain:missing_key")
        assert result == "keychain:missing_key"  # Returns original


class TestLoadConfigWithKeychain:
    @patch("skills.ops.keychain_manager.get_secret")
    def test_resolves_nested(self, mock_get):
        mock_get.return_value = "resolved_secret"
        config = {
            "db": {
                "host": "localhost",
                "password": "keychain:db_pass",
            },
            "token": "keychain:api_token",
            "debug": True,
        }
        resolved = load_config_with_keychain(config)
        assert resolved["db"]["password"] == "resolved_secret"
        assert resolved["token"] == "resolved_secret"
        assert resolved["db"]["host"] == "localhost"
        assert resolved["debug"] is True

    def test_no_keychain_values(self):
        config = {"host": "localhost", "port": 3306}
        resolved = load_config_with_keychain(config)
        assert resolved == config


class TestMigrateEnvToKeychain:
    def test_dry_run(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "DB_HOST=localhost\n"
            "DB_PASSWORD=my_secret_pass\n"
            "DISCORD_TOKEN=xoxb-12345\n"
            "FLASK_DEBUG=1\n"
        )
        migrated = migrate_env_to_keychain(str(env_file), dry_run=True)
        assert "DB_PASSWORD" in migrated
        assert "DISCORD_TOKEN" in migrated
        assert "DB_HOST" not in migrated
        assert "FLASK_DEBUG" not in migrated

    def test_skips_already_migrated(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("DB_PASSWORD=keychain:db_password\n")
        migrated = migrate_env_to_keychain(str(env_file), dry_run=True)
        assert len(migrated) == 0

    def test_skips_empty_values(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("DB_PASSWORD=\nDISCORD_TOKEN=your_token_here\n")
        migrated = migrate_env_to_keychain(str(env_file), dry_run=True)
        assert len(migrated) == 0

    def test_missing_env_file(self):
        migrated = migrate_env_to_keychain("/nonexistent/.env", dry_run=True)
        assert migrated == {}
