# -*- coding: utf-8 -*-
"""
keychain_manager.py
===================
macOS Keychain Services 集中密碼管理模組。

將 .env 中的敏感值遷移到 macOS Keychain，消除明文密碼。
支援 keychain: 前綴自動解析，啟動時透明替換。

整合點：
- config_loader / startup：啟動時自動解析 keychain: 前綴
- scripts/migrate_secrets_to_keychain.py：一次性遷移腳本
- .env 改為只存路徑指標，不存明文
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
from typing import Optional

logger = logging.getLogger("KeychainManager")

SERVICE_PREFIX = "com.magi"
IS_MACOS = platform.system() == "Darwin"

# 敏感 key 清單（自動偵測需要遷移的項目）
SENSITIVE_KEY_PATTERNS = [
    "TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL",
    "PRIVATE_KEY", "ACCESS_KEY",
]


def is_available() -> bool:
    """Check if macOS Keychain is available."""
    if not IS_MACOS:
        return False
    try:
        result = subprocess.run(
            ["security", "help"],
            capture_output=True, timeout=3,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# Core Keychain Operations
# ---------------------------------------------------------------------------

def get_secret(key: str) -> Optional[str]:
    """
    從 macOS Keychain 讀取密鑰。

    Args:
        key: 密鑰名稱（會自動加上 com.magi. 前綴）

    Returns:
        密鑰值，或 None（找不到或非 macOS）
    """
    if not IS_MACOS:
        return None

    service = f"{SERVICE_PREFIX}.{key}"
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def set_secret(key: str, value: str, label: str = "") -> bool:
    """
    儲存密鑰到 macOS Keychain。

    Args:
        key: 密鑰名稱
        value: 密鑰值
        label: 顯示標籤（可選）

    Returns:
        True if saved successfully
    """
    if not IS_MACOS:
        logger.warning("Keychain not available on this platform")
        return False

    service = f"{SERVICE_PREFIX}.{key}"

    # 先刪除舊的（如果存在）
    subprocess.run(
        ["security", "delete-generic-password", "-s", service],
        capture_output=True, timeout=5,
    )

    cmd = [
        "security", "add-generic-password",
        "-s", service,
        "-a", "magi",
        "-w", value,
        "-U",  # Update if exists
    ]
    if label:
        cmd += ["-l", label]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=5)
        if result.returncode == 0:
            logger.info("Keychain: saved %s", key)
            return True
        logger.warning("Keychain: failed to save %s (rc=%d)", key, result.returncode)
        return False
    except subprocess.SubprocessError as e:
        logger.error("Keychain: error saving %s: %s", key, e)
        return False


def delete_secret(key: str) -> bool:
    """從 Keychain 刪除密鑰。"""
    if not IS_MACOS:
        return False

    service = f"{SERVICE_PREFIX}.{key}"
    try:
        result = subprocess.run(
            ["security", "delete-generic-password", "-s", service],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except subprocess.SubprocessError:
        return False


def list_secrets() -> list[str]:
    """列出所有 MAGI Keychain 密鑰名稱。"""
    if not IS_MACOS:
        return []

    try:
        result = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []

        import re
        # Parse service names from dump
        services = re.findall(
            rf'"svce"<blob>="{SERVICE_PREFIX}\.([^"]+)"',
            result.stdout,
        )
        return sorted(set(services))
    except subprocess.SubprocessError:
        return []


# ---------------------------------------------------------------------------
# Config Integration
# ---------------------------------------------------------------------------

def resolve_keychain_value(value: str) -> str:
    """
    解析 keychain: 前綴值。

    例："keychain:discord_token" → 從 Keychain 讀取的實際值
    非 keychain: 前綴的值原樣返回。

    Args:
        value: 可能含 keychain: 前綴的值

    Returns:
        解析後的實際值
    """
    if not isinstance(value, str):
        return value
    if not value.startswith("keychain:"):
        return value

    keychain_key = value[len("keychain:"):]
    secret = get_secret(keychain_key)
    if secret is not None:
        return secret

    logger.warning("Keychain: key '%s' not found, returning original value", keychain_key)
    return value


def load_config_with_keychain(config: dict) -> dict:
    """
    自動解析 config dict 中的 keychain: 前綴值。

    遞迴處理巢狀 dict。

    Args:
        config: 原始設定 dict

    Returns:
        解析後的設定 dict（新物件，不修改原始）
    """
    resolved = {}
    for key, value in config.items():
        if isinstance(value, str):
            resolved[key] = resolve_keychain_value(value)
        elif isinstance(value, dict):
            resolved[key] = load_config_with_keychain(value)
        elif isinstance(value, list):
            resolved[key] = [
                resolve_keychain_value(v) if isinstance(v, str) else v
                for v in value
            ]
        else:
            resolved[key] = value
    return resolved


def resolve_env_keychain() -> dict[str, str]:
    """
    解析所有 os.environ 中 keychain: 前綴的環境變數。

    Returns:
        {env_key: resolved_value} for all resolved keys
    """
    resolved = {}
    for key, value in os.environ.items():
        if value.startswith("keychain:"):
            secret = resolve_keychain_value(value)
            if secret != value:
                os.environ[key] = secret
                resolved[key] = secret
                logger.info("Keychain: resolved env var %s", key)
    return resolved


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def _is_sensitive_key(key: str) -> bool:
    """判斷環境變數 key 是否為敏感項目。"""
    key_upper = key.upper()
    return any(pat in key_upper for pat in SENSITIVE_KEY_PATTERNS)


def migrate_env_to_keychain(
    env_path: str = ".env",
    dry_run: bool = True,
) -> dict[str, str]:
    """
    一次性遷移：將 .env 中的敏感值搬到 Keychain。

    遷移後 .env 改為：
    DISCORD_TOKEN=keychain:discord_token

    Args:
        env_path: .env 檔案路徑
        dry_run: True 只列出要遷移的項目，不實際執行

    Returns:
        {env_key: keychain_key} 已遷移的項目
    """
    if not os.path.isfile(env_path):
        logger.warning("migrate: .env not found at %s", env_path)
        return {}

    migrated = {}
    lines = []

    with open(env_path, "r", encoding="utf-8") as f:
        original_lines = f.readlines()

    for line in original_lines:
        stripped = line.strip()
        if "=" not in stripped or stripped.startswith("#"):
            lines.append(line)
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()

        # Skip already migrated
        if value.startswith("keychain:"):
            lines.append(line)
            continue

        # Skip empty or placeholder values
        if not value or value.startswith("your_") or value == '""':
            lines.append(line)
            continue

        if _is_sensitive_key(key):
            keychain_key = key.lower()
            if dry_run:
                logger.info("migrate (dry-run): %s → keychain:%s", key, keychain_key)
            else:
                if set_secret(keychain_key, value, label=f"MAGI: {key}"):
                    lines.append(f"{key}=keychain:{keychain_key}\n")
                    migrated[key] = keychain_key
                    logger.info("migrate: %s → keychain:%s", key, keychain_key)
                    continue
                else:
                    logger.error("migrate: failed to save %s to Keychain", key)

            migrated[key] = keychain_key
            lines.append(line)  # Keep original in dry-run
        else:
            lines.append(line)

    # Write updated .env (only if not dry-run)
    if not dry_run and migrated:
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        logger.info("migrate: updated .env with %d keychain references", len(migrated))

    return migrated


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if "--list" in sys.argv:
        secrets = list_secrets()
        print(f"MAGI Keychain entries: {len(secrets)}")
        for s in secrets:
            print(f"  - {s}")

    elif "--migrate-dry" in sys.argv:
        env_path = sys.argv[sys.argv.index("--migrate-dry") + 1] if len(sys.argv) > sys.argv.index("--migrate-dry") + 1 else ".env"
        migrated = migrate_env_to_keychain(env_path, dry_run=True)
        print(f"\nWould migrate {len(migrated)} keys:")
        for k, v in migrated.items():
            print(f"  {k} → keychain:{v}")

    elif "--migrate" in sys.argv:
        env_path = sys.argv[sys.argv.index("--migrate") + 1] if len(sys.argv) > sys.argv.index("--migrate") + 1 else ".env"
        print("⚠️  This will modify your .env file and store secrets in macOS Keychain.")
        confirm = input("Proceed? (yes/no): ")
        if confirm.lower() == "yes":
            migrated = migrate_env_to_keychain(env_path, dry_run=False)
            print(f"\nMigrated {len(migrated)} keys to Keychain.")
        else:
            print("Cancelled.")

    elif "--test" in sys.argv:
        print(f"Keychain available: {is_available()}")
        test_key = "_magi_test"
        ok = set_secret(test_key, "test_value_123", label="MAGI Test")
        print(f"Set test key: {ok}")
        val = get_secret(test_key)
        print(f"Get test key: {val}")
        delete_secret(test_key)
        print(f"Deleted test key")
        val2 = get_secret(test_key)
        print(f"Get after delete: {val2}")

    else:
        print("Usage:")
        print("  python keychain_manager.py --list        # List MAGI Keychain entries")
        print("  python keychain_manager.py --migrate-dry # Preview migration")
        print("  python keychain_manager.py --migrate     # Execute migration")
        print("  python keychain_manager.py --test        # Test Keychain operations")
