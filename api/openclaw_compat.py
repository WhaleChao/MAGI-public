from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_openclaw_config() -> dict[str, Any]:
    candidates = [
        Path(os.environ.get("OPENCLAW_CONFIG_PATH", "")).expanduser()
        if os.environ.get("OPENCLAW_CONFIG_PATH")
        else None,
        Path.home() / ".openclaw" / "openclaw.json",
        Path.home() / ".openclaw" / "workspace" / "ai_config.json",
    ]
    for path in candidates:
        if not path:
            continue
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {}


def get_legacy_telegram_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    data = cfg if isinstance(cfg, dict) else {}

    token = (
        os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")
        or os.environ.get("MAGI_TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    notify_to: list[str] = []

    telegram_sections = [
        data.get("telegram"),
        ((data.get("channels") or {}).get("telegram") if isinstance(data.get("channels"), dict) else None),
    ]
    for section in telegram_sections:
        if not isinstance(section, dict):
            continue
        if not token:
            token = str(
                section.get("bot_token")
                or section.get("botToken")
                or section.get("token")
                or ""
            ).strip()
        raw_notify = (
            section.get("notify_to")
            or section.get("notifyTo")
            or section.get("admin_ids")
            or section.get("adminIds")
            or []
        )
        if isinstance(raw_notify, str):
            raw_items = [x.strip() for x in raw_notify.split(",") if x.strip()]
        elif isinstance(raw_notify, list):
            raw_items = [str(x).strip() for x in raw_notify if str(x).strip()]
        else:
            raw_items = []
        notify_to.extend(raw_items)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in notify_to:
        if item and item not in seen:
            seen.add(item)
            deduped.append(item)

    return {
        "bot_token": token,
        "notify_to": deduped,
    }


__all__ = ["get_legacy_telegram_settings", "load_openclaw_config"]
