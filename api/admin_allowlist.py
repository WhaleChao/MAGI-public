"""
admin_allowlist.py

Centralized admin allowlist loader.

Why:
- Environment variables are easy to misconfigure across restarts (launchd, shells, service wrappers).
- We want a single source of truth so only the real admin can issue system-change commands.

Security:
- This file only *reads* allowlists (no secrets).
- The allowlist file is stored under MAGI/.agent so it stays local to this machine.
"""

from __future__ import annotations
import logging

import json
import os
from pathlib import Path
from typing import Iterable, Set

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 26, exc_info=True)


MAGI_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = Path(os.environ.get("MAGI_AGENT_DIR", str(MAGI_ROOT / ".agent"))).expanduser()
ALLOWLIST_PATH = Path(os.environ.get("MAGI_ADMIN_ALLOWLIST_FILE", str(AGENT_DIR / "admin_allowlist.json"))).expanduser()


def _split_csv(s: str) -> Set[str]:
    return {x.strip() for x in (s or "").split(",") if x and x.strip()}


def _load_file_ids(key: str) -> Set[str]:
    try:
        if not ALLOWLIST_PATH.exists():
            return set()
        data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8")) or {}
        arr = data.get(key) or []
        if not isinstance(arr, list):
            return set()
        return {str(x).strip() for x in arr if str(x).strip()}
    except Exception:
        return set()


def get_discord_admin_ids() -> Set[str]:
    """
    Load Discord admin IDs from:
    - DISCORD_ADMIN_IDS env (CSV)
    - admin_allowlist.json: discord_admin_ids
    """
    ids = set()
    ids |= _split_csv(os.environ.get("DISCORD_ADMIN_IDS", ""))
    ids |= _load_file_ids("discord_admin_ids")
    return {x for x in ids if x}


def get_line_admin_user_ids() -> Set[str]:
    """
    Load LINE admin userIds from:
    - MAGI_ADMIN_LINE_IDS env (CSV)
    - admin_allowlist.json: line_admin_user_ids
    """
    ids = set()
    ids |= _split_csv(os.environ.get("MAGI_ADMIN_LINE_IDS", ""))
    ids |= _load_file_ids("line_admin_user_ids")
    return {x for x in ids if x}


def get_telegram_admin_ids() -> Set[str]:
    """
    Load Telegram admin IDs from:
    - MAGI_ADMIN_TELEGRAM_IDS env (CSV)
    - admin_allowlist.json: telegram_admin_ids
    """
    ids = set()
    ids |= _split_csv(os.environ.get("MAGI_ADMIN_TELEGRAM_IDS", ""))
    ids |= _load_file_ids("telegram_admin_ids")
    return {x for x in ids if x}


def ensure_agent_dir() -> None:
    try:
        AGENT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 91, exc_info=True)
