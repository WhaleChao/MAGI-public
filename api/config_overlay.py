"""
MAGI Config Overlay — 統一的 config.json + .env 合併層
======================================================
所有模組讀取 config.json 後，都應經過此 overlay 填補空欄位。
原則：.env 優先，config.json 作為結構模板。

Usage:
    from api.config_overlay import apply_env_overlay

    with open("config.json") as f:
        config = json.load(f)
    config = apply_env_overlay(config)
    # 現在 config 中的空 credential 欄位已被 .env 值填補
"""

from __future__ import annotations

import os
import logging

logger = logging.getLogger("magi.config_overlay")

# ── .env 變數名 → config.json 欄位的映射表 ──
# 格式: (config_json_path, env_var_names)
_ENV_OVERLAY_MAP = {
    # LINE
    ("line", "channel_access_token"): ["MAGI_LINE_CHANNEL_ACCESS_TOKEN"],
    ("line", "channel_secret"): ["MAGI_LINE_CHANNEL_SECRET"],

    # Discord webhooks
    ("discord", "webhook_url"): ["MAGI_DISCORD_WEBHOOK_LEGALBRIDGE", "MAGI_DISCORD_WEBHOOK_URL"],
    ("discord", "webhook_url_laf"): ["MAGI_DISCORD_WEBHOOK_LEGALBRIDGE_LAF", "MAGI_DISCORD_WEBHOOK_URL_LAF"],

    # Judicial webhooks
    ("judicial", "webhook_url_record"): ["MAGI_JUDICIAL_WEBHOOK_RECORD"],
    ("judicial", "webhook_url_review_payment"): ["MAGI_JUDICIAL_WEBHOOK_PAYMENT", "MAGI_WEBHOOK_URL_REVIEW_PAYMENT"],
    ("judicial", "webhook_url_review_ready"): ["MAGI_JUDICIAL_WEBHOOK_READY"],

    # Judicial credentials
    ("judicial", "record_username"): ["MAGI_JUDICIAL_RECORD_USERNAME"],
    ("judicial", "record_password"): ["MAGI_JUDICIAL_RECORD_PASSWORD"],
    ("judicial", "eefile_username"): ["MAGI_JUDICIAL_EEFILE_USERNAME"],
    ("judicial", "eefile_password"): ["MAGI_JUDICIAL_EEFILE_PASSWORD"],

    # LAF credentials
    ("laf", "username"): ["MAGI_LAF_USERNAME"],
    ("laf", "password"): ["MAGI_LAF_PASSWORD"],
}

# 頂層 key
_TOP_LEVEL_OVERLAY = {
    "gemini_api_key": ["MAGI_GEMINI_API_KEY", "GEMINI_API_KEY"],
    "judicial_api_user": ["MAGI_JUDICIAL_API_USER"],
    "judicial_api_pass": ["MAGI_JUDICIAL_API_PASS"],
    "discord_webhook_url": ["MAGI_DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"],
    "discord_webhook_url_laf": ["MAGI_DISCORD_WEBHOOK_LEGALBRIDGE_LAF"],
    "discord_filescan_webhook_url": ["MAGI_DISCORD_FILESCAN_WEBHOOK_URL"],
    "discord_todos_webhook_url": ["MAGI_DISCORD_TODOS_WEBHOOK_URL"],
    "discord_checklist_webhook_url": ["MAGI_DISCORD_CHECKLIST_WEBHOOK_URL"],
}

# DB profile overlay
_DB_PASSWORD_VARS = ["OSC_DB_PASSWORD", "DB_PASSWORD", "MAGI_REMOTE_DB_PASSWORD"]
_DB_USER_VARS = ["OSC_DB_USER", "DB_USER", "MAGI_REMOTE_DB_USER"]


def _first_env(*var_names: str) -> str:
    """Return the first non-empty env var value."""
    for name in var_names:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def apply_env_overlay(config: dict) -> dict:
    """
    Apply .env overlay to a config dict loaded from config.json.

    Fills empty credential fields with values from environment variables.
    Does NOT overwrite non-empty values (config.json wins if populated).

    Returns the same dict (mutated in place) for convenience.
    """
    if not isinstance(config, dict):
        return config

    # 1. Nested overlays (section.field)
    for (section, field), env_vars in _ENV_OVERLAY_MAP.items():
        sub = config.get(section)
        if not isinstance(sub, dict):
            continue
        current = str(sub.get(field, "")).strip()
        if not current:
            env_val = _first_env(*env_vars)
            if env_val:
                sub[field] = env_val
                logger.debug("overlay: %s.%s ← env", section, field)

    # 2. Top-level overlays
    for field, env_vars in _TOP_LEVEL_OVERLAY.items():
        current = str(config.get(field, "")).strip()
        if not current:
            env_val = _first_env(*env_vars)
            if env_val:
                config[field] = env_val
                logger.debug("overlay: %s ← env", field)

    # 3. mariadb_profiles password/user overlay
    db_pass = _first_env(*_DB_PASSWORD_VARS)
    db_user = _first_env(*_DB_USER_VARS)
    for prof in config.get("mariadb_profiles", []):
        pcfg = prof.get("config")
        if not isinstance(pcfg, dict):
            continue
        if db_pass and not pcfg.get("password", "").strip():
            pcfg["password"] = db_pass
        if db_user and not pcfg.get("user", "").strip():
            pcfg["user"] = db_user

    # 4. magi_brain password overlay
    mb = config.get("magi_brain")
    if isinstance(mb, dict):
        if not mb.get("password", "").strip():
            mb_pass = _first_env("DB_PASSWORD", *_DB_PASSWORD_VARS)
            if mb_pass:
                mb["password"] = mb_pass

    return config
