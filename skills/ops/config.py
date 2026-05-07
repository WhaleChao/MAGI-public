"""
MAGI configuration validation.

Validates required environment variables at startup to fail fast
instead of discovering missing config mid-operation.

P0-05: Config 分為 core required 與 feature-scoped required。
核心啟動僅需 DB + Flask secret，通道 credentials 改為各通道自行驗證。
"""

import logging
import os

logger = logging.getLogger("MAGIConfig")

# ============================
# Core required — 缺少任何一個將阻止核心啟動
# ============================
CORE_REQUIRED_VARS = [
    "DB_HOST",
    "DB_USER",
    "DB_PASSWORD",
    "FLASK_SECRET_KEY",
]

# ============================
# Feature-scoped required — 僅在對應功能啟用時才需要
# 格式: (env_var, feature_name, enable_flag)
#   - 只有當 enable_flag 為 None（永遠檢查）或 enable_flag env var 有值時才驗證
# ============================
FEATURE_SCOPED_VARS = [
    # LINE 通道
    ("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "LINE Bot", "MAGI_ENABLE_LINE"),
    ("MAGI_LINE_CHANNEL_SECRET", "LINE Bot", "MAGI_ENABLE_LINE"),
    # Discord 通道
    ("DISCORD_BOT_TOKEN", "Discord Bot", "MAGI_ENABLE_DISCORD"),
    # Telegram 通道
    ("OPENCLAW_TELEGRAM_BOT_TOKEN", "Telegram Bot", "MAGI_ENABLE_TELEGRAM"),
    # Remote DB (federation)
    ("MAGI_REMOTE_DB_HOST", "Remote DB Sync", "MAGI_ENABLE_REMOTE_DB"),
]

# Optional but recommended — warn if missing.
RECOMMENDED_VARS = [
    "MAGI_CLOUDFLARED_PATH",
    "MAGI_OMLX_SUMMARY_MODEL",
]


def _is_feature_enabled(enable_flag: Optional[str]) -> bool:
    """Check if a feature flag is set (truthy value)."""
    if enable_flag is None:
        return True
    val = os.environ.get(enable_flag, "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def validate_config() -> list[str]:
    """
    Check all required env vars are set.
    Returns list of warnings. Raises RuntimeError if core vars are missing.

    核心變數缺少 → 拋出 RuntimeError，阻止啟動。
    通道變數缺少 → 僅 warning，不阻止核心啟動。
    """
    # 1. Core required
    core_missing = [v for v in CORE_REQUIRED_VARS if not os.environ.get(v)]
    if core_missing:
        raise RuntimeError(
            f"Missing CORE required environment variables: {core_missing}. "
            "Set them in .env before starting MAGI."
        )

    warnings = []

    # 2. Feature-scoped required
    for var, feature, flag in FEATURE_SCOPED_VARS:
        if _is_feature_enabled(flag) and not os.environ.get(var):
            msg = f"Feature '{feature}' is enabled but {var} is not set — this channel will not work."
            logger.warning(msg)
            warnings.append(msg)

    # 3. Recommended vars
    for v in RECOMMENDED_VARS:
        if not os.environ.get(v):
            logger.warning("Recommended env var %s is not set", v)

    return warnings


# ============================
# Backward compatibility: 保留 REQUIRED_VARS 供外部引用
# ============================
REQUIRED_VARS = CORE_REQUIRED_VARS
