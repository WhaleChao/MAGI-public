"""Shared case defaults for OSC/Paperclip case records.

The source of truth is runtime settings/environment.  Demo placeholders are
never valid production case data.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

DEMO_LAWYER_VALUES = frozenset(
    {
        "範例律師",
        "示範律師",
        "測試律師",
        "Sample Lawyer",
        "Demo Lawyer",
    }
)

_DEBT_MARKERS = ("消費者債務清理", "消債", "更生", "清算")
_DEBT_SETTING_KEYS = ("default_debt_lawyer", "consumer_debt_lawyer", "debt_lawyer", "default_specialist")
_REGULAR_SETTING_KEYS = ("default_lawyer", "lawyer_name")
_DEBT_ENV_KEYS = ("MAGI_DEFAULT_DEBT_LAWYER", "MAGI_CONSUMER_DEBT_LAWYER")
_REGULAR_ENV_KEYS = ("MAGI_DEFAULT_LAWYER", "MAGI_PUBLIC_LAWYER_NAME", "MAGI_LAWYER_NAME")


def is_demo_lawyer(value: Any) -> bool:
    return str(value or "").strip() in DEMO_LAWYER_VALUES


def case_uses_consumer_debt_lawyer(*values: Any) -> bool:
    text = " ".join(str(value or "") for value in values)
    return any(marker in text for marker in _DEBT_MARKERS)


def _valid_lawyer(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in DEMO_LAWYER_VALUES:
        return ""
    return text


def _read_setting(settings_getter: Callable[[str, str], Any] | None, key: str) -> str:
    if not settings_getter:
        return ""
    try:
        return _valid_lawyer(settings_getter(key, ""))
    except TypeError:
        try:
            return _valid_lawyer(settings_getter(key))
        except Exception:
            return ""
    except Exception:
        return ""


def _read_env(env: Mapping[str, str] | None, key: str) -> str:
    source = env if env is not None else os.environ
    try:
        return _valid_lawyer(source.get(key, ""))
    except Exception:
        return ""


def default_case_lawyer(
    *,
    case_type: Any = "",
    case_reason: Any = "",
    case_category: Any = "",
    settings_getter: Callable[[str, str], Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return the configured default case lawyer, or ``""`` if unavailable."""
    is_debt = case_uses_consumer_debt_lawyer(case_type, case_reason, case_category)
    setting_keys = _DEBT_SETTING_KEYS if is_debt else _REGULAR_SETTING_KEYS
    env_keys = _DEBT_ENV_KEYS if is_debt else _REGULAR_ENV_KEYS
    for key in setting_keys:
        value = _read_setting(settings_getter, key)
        if value:
            return value
    for key in env_keys:
        value = _read_env(env, key)
        if value:
            return value
    return ""


def db_settings_getter(db: Any) -> Callable[[str, str], str]:
    """Build a best-effort settings getter for legacy DB manager objects."""

    def _getter(key: str, default: str = "") -> str:
        setting_key = str(key or "").strip()
        if not setting_key or db is None:
            return str(default or "")
        sql = "SELECT value FROM settings WHERE `key`=%s LIMIT 1"
        row = None
        try:
            if hasattr(db, "fetch_one"):
                try:
                    row = db.fetch_one(sql, (setting_key,), as_dict=True)
                except TypeError:
                    row = db.fetch_one(sql, (setting_key,))
            elif hasattr(db, "execute"):
                try:
                    row = db.execute(sql, (setting_key,), fetch="one")
                except TypeError:
                    row = db.execute(sql, (setting_key,))
        except Exception:
            row = None
        if isinstance(row, dict):
            value = row.get("value")
        elif isinstance(row, (list, tuple)) and row:
            value = row[0]
        else:
            value = None
        text = _valid_lawyer(value)
        return text if text else str(default or "")

    return _getter


def normalize_case_lawyer(
    value: Any,
    *,
    allow_default: bool = True,
    case_type: Any = "",
    case_reason: Any = "",
    case_category: Any = "",
    settings_getter: Callable[[str, str], Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Clean placeholder lawyer names and optionally fill configured default."""
    text = _valid_lawyer(value)
    if text:
        return text
    if not allow_default:
        return ""
    return default_case_lawyer(
        case_type=case_type,
        case_reason=case_reason,
        case_category=case_category,
        settings_getter=settings_getter,
        env=env,
    )
