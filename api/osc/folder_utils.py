"""OSC case folder creation utilities shared by Web API and desktop callers."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from api.laf_case_classifier import clean_laf_case_reason
from api.osc.case_folder_schema import (
    CASE_SUBFOLDERS,
    CATEGORY_FOLDER_MAP,
    TYPE_FOLDER_MAP,
    case_subfolders,
)

SUBFOLDERS = {category: list(folders) for category, folders in CASE_SUBFOLDERS.items()}

_ILLEGAL_CHARS = re.compile(r'[<>:"|?*\\/]')
_ATTACHED_CIVIL_TOKENS = ("刑事附帶民事", "附帶民事", "附民")
_TEST_SENTINEL_TOKENS = ("2026-9998", "測試消債")
def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _ordinary_test_mode() -> bool:
    return _env_truthy("MAGI_TEST_MODE") and not _env_truthy("MAGI_ENABLE_LIVE_TESTS")


def _looks_like_live_case_root(path: str) -> bool:
    norm = str(path or "").replace("\\", "/")
    return (
        norm.startswith("/Volumes/")
        or norm.startswith("/Network/")
        or norm.startswith("Z:/")
        or norm.startswith("Y:/")
        or norm.startswith("//")
        or "/Library/CloudStorage/SynologyDrive" in norm
    )


def _assert_test_safe_case_folder_path(path: str) -> None:
    if not _ordinary_test_mode():
        return
    norm = str(path or "").replace("\\", "/")
    for token in _TEST_SENTINEL_TOKENS:
        if token in norm:
            raise RuntimeError(f"Refusing to create sentinel test case folder in ordinary pytest: {token}")
    if _looks_like_live_case_root(norm):
        raise RuntimeError(f"Refusing to create folder under live NAS/Drive root in ordinary pytest: {path}")


def sanitize_folder_name(name: str) -> str:
    return _ILLEGAL_CHARS.sub("_", name)


def build_case_folder_name(
    case_number: str,
    client_name: str,
    case_type: str = "",
    case_category: str = "",
    case_stage: str = "",
    case_reason: str = "",
) -> str:
    case_reason = clean_laf_case_reason(case_reason)
    if case_type == "消費者債務清理" or "消費者債務清理" in (case_reason or ""):
        reason = case_reason or ""
        if "更生" not in reason and "清算" not in reason:
            reason = "更生"
        parts = [case_number, client_name, "消費者債務清理", reason]
    else:
        parts = [case_number, client_name, case_stage or case_type, case_reason]
    return sanitize_folder_name("-".join(filter(None, parts)))


def resolve_type_folder(case_type: str = "", case_stage: str = "", case_reason: str = "") -> str:
    """Resolve the second-level case folder from explicit type first, then safe fallbacks."""
    case_reason = clean_laf_case_reason(case_reason)
    explicit = (case_type or "").strip()
    text = " ".join(filter(None, [explicit, (case_stage or "").strip(), (case_reason or "").strip()]))

    if any(token in text for token in _ATTACHED_CIVIL_TOKENS):
        return "民事"
    if "消費者債務清理" in explicit:
        return "消費者債務清理"
    if explicit in TYPE_FOLDER_MAP:
        return TYPE_FOLDER_MAP[explicit]
    for token in ("民事", "刑事", "行政", "非訟", "法律顧問"):
        if token in explicit:
            return TYPE_FOLDER_MAP[token]
    if "消費者債務清理" in text:
        return "消費者債務清理"
    return "其他"


def build_full_case_path(
    base_path: str,
    case_number: str,
    client_name: str,
    case_type: str = "",
    case_category: str = "",
    case_stage: str = "",
    case_reason: str = "",
) -> str:
    category_folder = CATEGORY_FOLDER_MAP.get(case_category, "其他案件")
    type_folder = resolve_type_folder(case_type, case_stage, case_reason)
    folder_name = build_case_folder_name(
        case_number,
        client_name,
        case_type,
        case_category,
        case_stage,
        case_reason,
    )
    return os.path.join(base_path, category_folder, type_folder, folder_name)


def _create_folder_structure_unchecked(base_path: str, case_category: str = "一般案件") -> dict:
    try:
        os.makedirs(base_path, exist_ok=True)
        base = Path(base_path)
        created = []
        for name in case_subfolders(case_category):
            fp = base / name
            os.makedirs(fp, exist_ok=True)
            gitkeep = fp / ".gitkeep"
            if not gitkeep.exists():
                gitkeep.write_text(
                    f"# {name} - 建立於 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    encoding="utf-8",
                )
            created.append(name)
        return {"ok": True, "path": base_path, "subfolders": created}
    except OSError as e:
        return {"ok": False, "error": f"OSError: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def create_folder_structure(base_path: str, case_category: str = "一般案件") -> dict:
    _assert_test_safe_case_folder_path(base_path)
    return _create_folder_structure_unchecked(base_path, case_category)


__all__ = [
    "SUBFOLDERS",
    "CATEGORY_FOLDER_MAP",
    "TYPE_FOLDER_MAP",
    "sanitize_folder_name",
    "build_case_folder_name",
    "resolve_type_folder",
    "build_full_case_path",
    "create_folder_structure",
]
