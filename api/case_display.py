"""Canonical case display helpers shared by legal workflow modules."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Mapping, Sequence

_NAME_FIXES = str.maketrans({"餘": "余"})
_CASE_FOLDER_RE = re.compile(r"^(?P<case>\d{4}-\d{4})-(?P<rest>.+)$")


def normalize_person_name(value: str) -> str:
    """Normalize names for fuzzy comparison, not for display."""
    text = re.sub(r"\s+", "", str(value or "").strip())
    return text.translate(_NAME_FIXES)


def is_unusable_client_label(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if "○" in text or "當事人" in text or text.startswith("["):
        return True
    if text.startswith("-"):
        return True
    if any(token in text for token in ("案情", "文件", "卷宗", "附件", "信件", "資料夾")):
        return True
    return len(text) > 30


def _path_parts(path_text: str) -> list[str]:
    return [part for part in str(path_text or "").replace("\\", "/").split("/") if part]


def folder_client_name(
    record: Mapping[str, object] | None,
    *,
    folder_keys: Sequence[str] = ("folder_path", "folder", "case_folder", "case_root", "dst"),
) -> str:
    """Extract the client name from a MAGI/OSC case folder path when available."""
    record = record or {}
    case_number = str(record.get("case_number") or "").strip()
    for key in folder_keys:
        raw_path = str(record.get(key) or "").strip()
        if not raw_path:
            continue
        for part in reversed(_path_parts(raw_path)):
            match = _CASE_FOLDER_RE.match(part)
            if not match:
                continue
            if case_number and not part.startswith(f"{case_number}-"):
                continue
            rest = match.group("rest")
            pieces = rest.split("-")
            candidate = "-".join(pieces[:-2]).strip() if len(pieces) >= 3 else pieces[0].strip()
            if candidate and not is_unusable_client_label(candidate):
                return candidate
    return ""


def should_trust_folder_client_name(db_name: str, folder_name: str) -> bool:
    if not folder_name:
        return False
    if is_unusable_client_label(db_name):
        return True
    db_key = normalize_person_name(db_name)
    folder_key = normalize_person_name(folder_name)
    if not db_key or not folder_key or db_key == folder_key:
        return False
    if len(db_key) == len(folder_key) and 2 <= len(db_key) <= 12:
        diff_count = sum(1 for a, b in zip(db_key, folder_key) if a != b)
        return diff_count <= max(1, len(db_key) // 4)
    if abs(len(db_key) - len(folder_key)) <= 1:
        return SequenceMatcher(None, db_key, folder_key).ratio() >= 0.82
    return False


def display_client_name(
    record: Mapping[str, object] | None,
    *,
    name_keys: Iterable[str] = ("client_name", "party", "name"),
    folder_keys: Sequence[str] = ("folder_path", "folder", "case_folder", "case_root", "dst"),
) -> str:
    record = record or {}
    raw = ""
    for key in name_keys:
        value = str(record.get(key) or "").strip()
        if value:
            raw = value
            break
    folder_name = folder_client_name(record, folder_keys=folder_keys)
    if should_trust_folder_client_name(raw, folder_name):
        return folder_name
    return "" if is_unusable_client_label(raw) else (raw or folder_name)


def display_case_label(
    record: Mapping[str, object] | None,
    *,
    name_keys: Iterable[str] = ("client_name", "party", "name"),
    case_keys: Iterable[str] = ("court_case_number", "court_case_no", "case_number"),
) -> str:
    record = record or {}
    name = display_client_name(record, name_keys=name_keys)
    case_no = ""
    for key in case_keys:
        value = str(record.get(key) or "").strip()
        if value:
            case_no = value
            break
    parts = [part for part in (name, case_no) if part]
    return "｜".join(parts) if parts else "未判斷案件"
