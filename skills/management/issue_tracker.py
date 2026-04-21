# -*- coding: utf-8 -*-
"""
Issue Tracker (self-repair loop Phase 1).

Iron Dome Audit: SAFE — writes to .runtime/issue_agenda.jsonl only.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
import os
import re
import time
from typing import Any, Dict, Optional

from api.platforms.runtime_dir import atomic_append_jsonl, root as _rt_root

logger = logging.getLogger("IssueTracker")

_ENABLE = os.environ.get("MAGI_ISSUE_TRACKER_ENABLE", "0") == "1"
_MARKDOWN_LEGACY = os.environ.get("MAGI_ISSUE_TRACKER_MARKDOWN", "0") == "1"
_DEDUP_TTL_SEC = int(os.environ.get("MAGI_ISSUE_TRACKER_DEDUP_TTL_SEC", "300"))

_AGENDA_PATH = _rt_root() / "issue_agenda.jsonl"
_DEDUP_CACHE_PATH = _rt_root() / "issue_agenda_dedup.json"
_LEGACY_MD_PATH = _rt_root() / "issue_agenda_legacy.md"

_SCRUB_PATTERNS = [
    (
        re.compile(
            r"(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*['\"]?[^\s'\"]+['\"]?",
            re.I,
        ),
        r"\1=***",
    ),
    (re.compile(r"\b[A-Z][12]\d{8}\b"), "***ID***"),
    (re.compile(r"\b09\d{2}-?\d{3}-?\d{3}\b"), "***MOBILE***"),
    (re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}\b"), "***NVIDIA_KEY***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{30,}\b"), "***OPENAI_KEY***"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"), "***GOOGLE_KEY***"),
]


def _scrub(text: str) -> str:
    if not text:
        return text
    out = str(text)

    try:
        from skills.engine.pii_scrubber import build_scrubber_from_magi_db

        scrubber = build_scrubber_from_magi_db()
        out = scrubber.scrub(out).scrubbed_text
    except Exception as e:
        logger.debug("DB scrubber unavailable, regex-only: %s", e)

    for pat, repl in _SCRUB_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _dedup_key(command: str, error_msg: str) -> str:
    normalized = re.sub(r"\d+", "N", f"{command}|{error_msg}")[:500]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _is_duplicate(key: str) -> bool:
    now = time.time()
    data = {}  # type: Dict[str, float]
    try:
        if _DEDUP_CACHE_PATH.exists():
            data = _json.loads(_DEDUP_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}

    last = float(data.get(key, 0))
    if now - last < _DEDUP_TTL_SEC:
        return True

    data[key] = now
    cutoff = now - _DEDUP_TTL_SEC * 10
    data = {k: v for k, v in data.items() if v > cutoff}
    try:
        _DEDUP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DEDUP_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(data), encoding="utf-8")
        tmp.replace(_DEDUP_CACHE_PATH)
    except Exception:
        logger.debug("dedup cache write failed", exc_info=True)
    return False


def log_issue(
    command: Any,
    error_msg: Any,
    context: Optional[Any] = None,
    severity: str = "High",
    *,
    source: str = "unknown",
) -> Optional[bool]:
    """
    Append a failure to the JSONL issue agenda.

    Returns True when written, None when disabled or deduplicated, and False
    when the write failed. This function never raises.
    """
    if not _ENABLE:
        return None

    try:
        cmd_s = _scrub(str(command)[:500])
        err_s = _scrub(str(error_msg)[:2000])
        ctx_s = _scrub(str(context)[:1000]) if context else None

        key = _dedup_key(cmd_s, err_s)
        if _is_duplicate(key):
            return None

        record = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "command": cmd_s,
            "error": err_s,
            "context": ctx_s,
            "severity": severity,
            "source": source,
            "dedup_key": key,
        }
        atomic_append_jsonl(_AGENDA_PATH, record, rotate_at=5000, keep_tail=5000)

        if _MARKDOWN_LEGACY:
            _append_markdown_legacy(cmd_s, err_s, ctx_s, severity)

        return True
    except Exception as e:
        logger.error("log_issue write failed: %s", e)
        return False


def _append_markdown_legacy(command: str, error_msg: str, context: Optional[str], severity: str) -> None:
    try:
        _LEGACY_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not _LEGACY_MD_PATH.exists():
            _LEGACY_MD_PATH.write_text(
                "# MAGI Issue Agenda (Legacy Markdown)\n\n"
                "Managed by skills/management/issue_tracker.py\n\n",
                encoding="utf-8",
            )
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = (
            f"\n## Issue ({ts})\n"
            f"- Severity: {severity}\n"
            f"- Command: `{command}`\n"
            f"- Error: `{error_msg}`\n"
        )
        if context:
            entry += f"- Context: {context}\n"
        entry += "---\n"
        with open(_LEGACY_MD_PATH, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        logger.debug("legacy markdown write failed: %s", e)
