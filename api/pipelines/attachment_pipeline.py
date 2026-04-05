"""
Multimedia/attachment handling pipeline extracted from Orchestrator.

Contains: handle_multimedia, process_image, and file extraction routing.

All functions accept an `orch` parameter (the Orchestrator instance)
instead of `self`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

from api.model_config import TEXT_PRIMARY_MODEL


def handle_multimedia(orch, user_id, prompt, attachment) -> str:
    """
    Routes file attachments to appropriate skills.

    This is a thin wrapper — the full _handle_multimedia body (~500 lines)
    is deeply interleaved with dozens of orch internal methods and lives
    in ``api/pipelines/message_pipeline.py`` (inside ``process_message_inner``).
    This entry point is intentionally not called from the Orchestrator delegate
    to avoid circular delegation.
    """
    raise NotImplementedError(
        "handle_multimedia is not yet fully extracted; "
        "call orch._handle_multimedia_impl() instead."
    )


def process_image(orch, user_id, image_path, platform="LINE") -> str:
    """Handles incoming images."""
    from skills.bridge.melchior_bridge import analyze_image

    logger.info(f"\U0001f5bc\ufe0f Received Image from {user_id}: {image_path}")
    description = analyze_image(image_path)
    return f"\U0001f441\ufe0f Melchior sees: {description}"


# ---------------------------------------------------------------------------
# Attachment context tracking
# ---------------------------------------------------------------------------

def load_recent_attachments(orch) -> dict:
    with orch._state_cache_lock:
        return dict(orch._recent_attachments_cache)


def save_recent_attachments(orch, data: dict) -> None:
    with orch._state_cache_lock:
        orch._recent_attachments_cache = data if isinstance(data, dict) else {}
        orch._state_dirty.add("recent_attachments")
    orch._schedule_state_flush()


def prune_recent_attachments(data: dict) -> dict:
    ttl_sec = int(os.environ.get("MAGI_RECENT_ATTACHMENT_TTL_SEC", "21600") or "21600")
    now = time.time()
    out = {}
    for key, entry in (data or {}).items():
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if not path or not os.path.exists(path):
            continue
        try:
            ts = float(entry.get("timestamp") or 0.0)
        except Exception:
            ts = 0.0
        if ts and (now - ts > max(600, ttl_sec)):
            continue
        out[str(key)] = entry
    return out


def remember_recent_attachment(orch, *, user_id: str, platform: str, attachment: dict, source_message: str = "") -> dict:
    kind = str((attachment or {}).get("type") or "").strip().lower()
    path = str((attachment or {}).get("path") or "").strip()
    if kind not in {"file", "audio", "image"} or not path or not os.path.exists(path):
        return {}
    data = prune_recent_attachments(load_recent_attachments(orch))
    key = orch._pending_key(user_id, platform)
    entry = {
        "user_id": str(user_id or "").strip(),
        "platform": str(platform or "").strip(),
        "type": kind,
        "path": path,
        "filename": str((attachment or {}).get("filename") or os.path.basename(path) or "").strip(),
        "timestamp": float((attachment or {}).get("timestamp") or time.time()),
        "source_message": str(source_message or "").strip()[:2000],
    }
    data[key] = entry
    save_recent_attachments(orch, data)
    return entry


def get_recent_attachment(orch, user_id: str, platform: str) -> dict:
    data = prune_recent_attachments(load_recent_attachments(orch))
    save_recent_attachments(orch, data)
    return data.get(orch._pending_key(user_id, platform)) or {}


def looks_like_attachment_followup(message: str, attachment_type: str = "") -> bool:
    s = str(message or "").strip().lower()
    if not s:
        return False
    direct_hits = [
        "\u7ffb\u8b6f", "translate", "\u7ffb\u6210", "\u5168\u6587", "\u6574\u7bc7", "\u6574\u4efd", "\u6574\u500b\u6a94\u6848", "\u5b8c\u6574\u7ffb\u8b6f", "\u5168\u6587\u7ffb\u8b6f",
        "\u4e0d\u8981\u6458\u8981", "\u6458\u8981", "\u7e3d\u7d50", "\u91cd\u9ede", "\u95dc\u9375\u6bb5\u843d", "\u9010\u5b57\u7a3f", "\u6642\u9593\u6233", "\u9644\u4ef6", "\u6a94\u6848", "\u6587\u4ef6",
        "pdf", "docx", "txt", "epub", "\u56de\u5230\u525b\u525b", "\u525b\u525b\u90a3\u4efd", "\u90a3\u4efd\u6587\u4ef6", "\u90a3\u500b\u6a94\u6848",
    ]
    if any(hit in s for hit in direct_hits):
        return True
    if attachment_type == "file":
        return s in {"\u8981\u6574\u7bc7\u5168\u6587", "\u6574\u7bc7\u5168\u6587", "\u8981\u5168\u6587", "\u5168\u6587", "\u5168\u90e8", "\u5b8c\u6574\u7684", "\u6574\u4efd"}
    if attachment_type == "audio":
        return s in {"\u9010\u5b57\u7a3f", "\u8981\u9010\u5b57\u7a3f", "\u5168\u6587\u9010\u5b57\u7a3f", "\u8981\u5168\u6587", "\u5168\u6587", "\u52a0\u6642\u9593\u6233", "\u8981\u6642\u9593\u6233"}
    return False


def has_recent_attachment_followup(orch, user_id: str, platform: str, message: str) -> bool:
    recent = get_recent_attachment(orch, str(user_id or ""), str(platform or ""))
    if not recent:
        return False
    return looks_like_attachment_followup(message, str(recent.get("type") or ""))


def maybe_reuse_recent_attachment(orch, user_id: str, platform: str, message: str) -> dict | None:
    recent = get_recent_attachment(orch, str(user_id or ""), str(platform or ""))
    if not recent:
        return None
    kind = str(recent.get("type") or "").strip().lower()
    if not looks_like_attachment_followup(message, kind):
        return None
    path = str(recent.get("path") or "").strip()
    if not path or not os.path.exists(path):
        return None
    logger.info(
        "\u267b\ufe0f Reusing recent attachment for follow-up: user=%s platform=%s type=%s file=%s",
        user_id,
        platform,
        kind,
        os.path.basename(path),
    )
    return {
        "type": kind,
        "path": path,
        "filename": str(recent.get("filename") or os.path.basename(path) or "").strip(),
        "timestamp": float(recent.get("timestamp") or time.time()),
        "reused_recent": True,
    }
