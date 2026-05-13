"""Tests for config.json env var overlay in osc.py ConfigManager."""

import json
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_env_overlay_discord_webhook(monkeypatch, tmp_path):
    """Env vars should override config.json discord webhook values."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "discord_webhook_url": "old_url",
        "discord_filescan_webhook_url": "",
        "discord_checklist_webhook_url": "",
        "canonical_windows_base_path": "Z:/test",
    }))

    monkeypatch.setenv("MAGI_DISCORD_WEBHOOK_URL", "new_env_url")

    from casper_ecosystem.law_firm_orchestrators.osc import ConfigManager
    cm = ConfigManager(config_file=str(config_file))
    config = cm.load_config()

    assert config["discord_webhook_url"] == "new_env_url"


def test_env_overlay_laf_credentials(monkeypatch, tmp_path):
    """Env vars should override LAF username/password."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "laf": {"username": "", "password": "", "enabled": True},
        "canonical_windows_base_path": "Z:/test",
    }))

    monkeypatch.setenv("MAGI_LAF_USERNAME", "env_user")
    monkeypatch.setenv("MAGI_LAF_PASSWORD", "env_pass")

    from casper_ecosystem.law_firm_orchestrators.osc import ConfigManager
    cm = ConfigManager(config_file=str(config_file))
    config = cm.load_config()

    assert config["laf"]["username"] == "env_user"
    assert config["laf"]["password"] == "env_pass"


def test_env_overlay_judicial_credentials(monkeypatch, tmp_path):
    """Env vars should override judicial record/eefile credentials."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "judicial": {
            "record_username": "",
            "record_password": "",
            "eefile_username": "",
            "eefile_password": "",
            "webhook_url_record": "",
        },
        "canonical_windows_base_path": "Z:/test",
    }))

    monkeypatch.setenv("MAGI_JUDICIAL_RECORD_USERNAME", "j_user")
    monkeypatch.setenv("MAGI_JUDICIAL_RECORD_PASSWORD", "j_pass")
    monkeypatch.setenv("MAGI_JUDICIAL_EEFILE_USERNAME", "e_user")
    monkeypatch.setenv("MAGI_JUDICIAL_EEFILE_PASSWORD", "e_pass")
    monkeypatch.setenv("MAGI_JUDICIAL_WEBHOOK_RECORD", "https://hook.example.com")

    from casper_ecosystem.law_firm_orchestrators.osc import ConfigManager
    cm = ConfigManager(config_file=str(config_file))
    config = cm.load_config()

    assert config["judicial"]["record_username"] == "j_user"
    assert config["judicial"]["record_password"] == "j_pass"
    assert config["judicial"]["eefile_username"] == "e_user"
    assert config["judicial"]["eefile_password"] == "e_pass"
    assert config["judicial"]["webhook_url_record"] == "https://hook.example.com"


def test_config_json_fallback_when_no_env(monkeypatch, tmp_path):
    """Without env vars, config.json values should be used as-is."""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "discord_webhook_url": "json_webhook",
        "laf": {"username": "json_user", "password": "json_pass"},
        "canonical_windows_base_path": "Z:/test",
    }))

    # Ensure overlay env vars are NOT set
    for key in ["MAGI_DISCORD_WEBHOOK_URL", "MAGI_LAF_USERNAME", "MAGI_LAF_PASSWORD"]:
        monkeypatch.delenv(key, raising=False)

    from casper_ecosystem.law_firm_orchestrators.osc import ConfigManager
    cm = ConfigManager(config_file=str(config_file))
    config = cm.load_config()

    assert config["discord_webhook_url"] == "json_webhook"
    assert config["laf"]["username"] == "json_user"
