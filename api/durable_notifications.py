"""Durable, recipient-bound outbox for cross-process asynchronous replies."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from api.platforms import runtime_dir


def _path() -> Path:
    return runtime_dir.root() / "durable_user_outbox.json"


def _load() -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("durable notification outbox is malformed")
    return value


def _platform(value: str) -> str:
    return str(value or "").strip().lower()


def enqueue(*, user_id: str, platform: str, text: str, dedupe_key: str) -> str:
    """Write one exact-recipient message; never infer or broadcast a recipient."""
    recipient = str(user_id or "").strip()
    channel = _platform(platform)
    key = str(dedupe_key or "").strip()
    if not recipient or not channel or not key:
        raise ValueError("durable notification requires recipient, platform and key")
    path = _path()
    with runtime_dir._append_lock(path):
        rows = _load()
        found = next((row for row in rows if row.get("dedupe_key") == key), None)
        if found:
            return str(found["id"])
        event_id = uuid.uuid4().hex
        rows.append(
            {
                "id": event_id,
                "dedupe_key": key,
                "user_id": recipient,
                "platform": channel,
                "text": str(text or "")[:12000],
                "created_at_epoch": time.time(),
                "delivered_at_epoch": None,
            }
        )
        runtime_dir.atomic_write_json(path, rows[-500:])
        return event_id


def claim_for_user(*, user_id: str, platform: str) -> list[dict[str, str]]:
    """Atomically claim undelivered messages for one exact recipient/channel."""
    recipient = str(user_id or "").strip()
    channel = _platform(platform)
    if not recipient or not channel:
        return []
    path = _path()
    with runtime_dir._append_lock(path):
        rows = _load()
        now = time.time()
        claimed: list[dict[str, str]] = []
        for row in rows:
            if (
                row.get("user_id") == recipient
                and _platform(str(row.get("platform") or "")) == channel
                and row.get("delivered_at_epoch") is None
            ):
                row["delivered_at_epoch"] = now
                claimed.append({"id": str(row["id"]), "text": str(row.get("text") or "")})
        if claimed:
            runtime_dir.atomic_write_json(path, rows[-500:])
        return claimed
