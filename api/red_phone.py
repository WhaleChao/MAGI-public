"""Compatibility wrapper for legacy `api.red_phone` imports."""

from __future__ import annotations

from skills.ops.red_phone import *  # noqa: F401,F403
from skills.ops.red_phone import alert_admin as _alert_admin


def notify(message: str, channel: str = "system", severity: str = "info", **kwargs):
    """Legacy notify() API mapped to RED PHONE alert delivery."""
    topic_key = str(kwargs.pop("topic_key", "") or channel or "system").strip()
    return _alert_admin(message, severity=severity, topic_key=topic_key, **kwargs)
