"""Shared command-prefix parsing for MAGI message entrypoints."""

from __future__ import annotations

import re


_HEAVY_PREFIX_RE = re.compile(
    r"^\s*[＠@]\s*(?:heavy|重型)(?=$|[\s:：,，、。!！?？\-–—]|[\u4e00-\u9fff])"
    r"\s*[:：,，、。!！?？\-–—]*\s*",
    re.IGNORECASE,
)
_MAGI_PREFIX_RE = re.compile(r"^\s*@\s*magi(?=$|[\s:：,，、。!！?？\-–—])\s*[:：,，、。!！?？\-–—]*\s*", re.IGNORECASE)
_HEAVY_WORD_PREFIX_RE = re.compile(
    r"^\s*(?:[＠@]\s*)?(?:heavy|重型)(?=$|[\s:：,，、。!！?？\-–—]|[\u4e00-\u9fff])"
    r"\s*[:：,，、。!！?？\-–—]*\s*",
    re.IGNORECASE,
)


def normalize_command_prefix_text(message: str) -> str:
    """Normalize prefix-only punctuation without changing the message body."""
    return str(message or "").replace("＠", "@").replace("\u3000", " ")


def split_heavy_prefix(message: str) -> tuple[bool, str]:
    """Return (has_heavy_prefix, message_without_prefix).

    Supports half/full-width @, English/Chinese aliases, optional spaces, and
    common punctuation after the prefix, e.g. ``＠HEAVY請分析`` or ``@重型：摘要``.
    Also accepts MAGI address prefixes such as ``@MAGI @HEAVY 摘要`` while
    preserving the ``@MAGI`` command prefix for downstream command handlers.
    """
    text = normalize_command_prefix_text(message).lstrip()
    magi_match = _MAGI_PREFIX_RE.match(text)
    if magi_match:
        rest = text[magi_match.end():]
        heavy_after_magi = _HEAVY_WORD_PREFIX_RE.match(rest)
        if heavy_after_magi:
            cleaned = rest[heavy_after_magi.end():].strip()
            return True, f"@MAGI {cleaned}".strip()
    match = _HEAVY_PREFIX_RE.match(text)
    if not match:
        return False, text
    return True, text[match.end():].strip()


def strip_heavy_prefix(message: str) -> str:
    return split_heavy_prefix(message)[1]


__all__ = [
    "normalize_command_prefix_text",
    "split_heavy_prefix",
    "strip_heavy_prefix",
]
