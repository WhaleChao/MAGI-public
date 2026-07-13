"""Shared OSC case-folder taxonomy.

Keep canonical folder labels, legacy aliases, and category-specific numbering
in one place so case creation, folder repairs, and PDF scans do not drift.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

JUDGMENT_FOLDER_LABEL = "判決書或終局裁定及處分"
LEGACY_JUDGMENT_FOLDER_LABEL = "判決書"
JUDGMENT_DRIVE_LABEL = "法院判決"
COURT_NOTICE_FOLDER_LABEL = "法院通知或程序裁定"
CLOSING_FOLDER_LABEL = "結案資料"
DEFAULT_JUDGMENT_FOLDER_PREFIX = 10

CATEGORY_FOLDER_MAP = {
    "一般案件": "一般案件",
    "法律扶助案件": "法扶案件",
    "指定辯護案件": "指定辯護案件",
    "無償案件": "無償案件",
}

TYPE_FOLDER_MAP = {
    "刑事": "刑事",
    "民事": "民事",
    "行政": "行政",
    "消費者債務清理": "消費者債務清理",
    "法律顧問": "法律顧問",
    "非訟": "非訟",
}

JUDGMENT_FOLDER_PREFIX_BY_CATEGORY = {
    "一般案件": 8,
    "法律扶助案件": 10,
    "指定辯護案件": 8,
    "無償案件": 9,
}

# Includes historical prefixes seen in migrated Drive/NAS data, not only the
# prefixes emitted by current case creation.
JUDGMENT_FOLDER_REPAIR_PREFIXES = (3, 4, 7, 8, 9, 10)

COURT_NOTICE_FOLDER_ALIASES = (
    COURT_NOTICE_FOLDER_LABEL,
    "法院通知及程序裁定",
    "法院通知與程序裁定",
    "法院通知",
    "程序裁定",
    "法院_通知",
    "法院_傳票",
)

PDF_ARCHIVED_NAME_FOLDER_LABELS = (
    COURT_NOTICE_FOLDER_LABEL,
    JUDGMENT_FOLDER_LABEL,
    LEGACY_JUDGMENT_FOLDER_LABEL,
    "對方歷次書狀",
    "對造歷次書狀",
)


def numbered_folder_name(prefix: int | str, label: str) -> str:
    return f"{int(prefix):02d}_{str(label or '').strip()}"


def judgment_folder_name(prefix: int | str = DEFAULT_JUDGMENT_FOLDER_PREFIX) -> str:
    """Return the canonical numbered folder name for final court documents."""
    return numbered_folder_name(prefix, JUDGMENT_FOLDER_LABEL)


def legacy_judgment_folder_name(prefix: int | str = DEFAULT_JUDGMENT_FOLDER_PREFIX) -> str:
    """Return the legacy numbered folder name accepted only as an alias."""
    return numbered_folder_name(prefix, LEGACY_JUDGMENT_FOLDER_LABEL)


def strip_number_prefix(name: str) -> str:
    return re.sub(r"^\d+_", "", str(name or "").strip())


def judgment_folder_aliases(
    prefix: int | str = DEFAULT_JUDGMENT_FOLDER_PREFIX,
    *,
    include_plain: bool = True,
) -> tuple[str, ...]:
    """Canonical folder first, followed by legacy read aliases."""
    aliases = [judgment_folder_name(prefix), legacy_judgment_folder_name(prefix)]
    if include_plain:
        aliases.extend([JUDGMENT_FOLDER_LABEL, LEGACY_JUDGMENT_FOLDER_LABEL])
    return tuple(dict.fromkeys(aliases))


def legacy_judgment_folder_names(
    prefixes: Iterable[int] = JUDGMENT_FOLDER_REPAIR_PREFIXES,
    *,
    include_plain: bool = True,
) -> tuple[str, ...]:
    names = [legacy_judgment_folder_name(prefix) for prefix in prefixes]
    if include_plain:
        names.append(LEGACY_JUDGMENT_FOLDER_LABEL)
    return tuple(dict.fromkeys(names))


def canonical_name_for_legacy_judgment_folder(
    name: str,
    *,
    prefixes: Iterable[int] = JUDGMENT_FOLDER_REPAIR_PREFIXES,
) -> str:
    text = str(name or "").strip()
    if text == LEGACY_JUDGMENT_FOLDER_LABEL:
        return JUDGMENT_FOLDER_LABEL
    for prefix in prefixes:
        if text == legacy_judgment_folder_name(prefix):
            return judgment_folder_name(prefix)
    return ""


def canonicalize_case_subfolder_name(name: str) -> str:
    """Normalize legacy judgment folder aliases; leave all other names intact."""
    return canonical_name_for_legacy_judgment_folder(name) or str(name or "").strip()


CASE_SUBFOLDERS: dict[str, tuple[str, ...]] = {
    "一般案件": (
        "01_委任契約書",
        "02_我方歷次書狀",
        "03_對方歷次書狀",
        "04_閱卷資料",
        "05_證據資料",
        "06_筆錄",
        "07_法院通知或程序裁定",
        judgment_folder_name(8),
        "09_回執",
        "10_信件往返",
    ),
    "法律扶助案件": (
        "01_法扶資料",
        "02_開辦資料",
        "03_結案資料",
        "04_我方歷次書狀",
        "05_對方歷次書狀",
        "06_閱卷資料",
        "07_證據資料",
        "08_筆錄",
        "09_法院通知或程序裁定",
        judgment_folder_name(10),
        "11_回執",
        "12_信件往返",
    ),
    "指定辯護案件": (
        "01_我方歷次書狀",
        "02_對方歷次書狀",
        "03_結案資料",
        "04_閱卷資料",
        "05_證據資料",
        "06_筆錄",
        "07_法院通知或程序裁定",
        judgment_folder_name(8),
        "09_回執",
        "10_信件往返",
    ),
    "無償案件": (
        "01_無償委任資料",
        "02_我方歷次書狀",
        "03_對方歷次書狀",
        "04_結案資料",
        "05_閱卷資料",
        "06_證據資料",
        "07_筆錄",
        "08_法院通知或程序裁定",
        judgment_folder_name(9),
        "10_回執",
        "11_信件往返",
    ),
}


def case_subfolders(case_category: str = "一般案件") -> tuple[str, ...]:
    return CASE_SUBFOLDERS.get(case_category, CASE_SUBFOLDERS["一般案件"])


def closing_folder_names(*, include_plain: bool = True) -> tuple[str, ...]:
    names: list[str] = []
    for folders in CASE_SUBFOLDERS.values():
        names.extend(name for name in folders if strip_number_prefix(name) == CLOSING_FOLDER_LABEL)
    if include_plain:
        names.append(CLOSING_FOLDER_LABEL)
    return tuple(dict.fromkeys(names))


def is_judgment_folder_segment(segment: str, *, prefixes: tuple[int, ...] = JUDGMENT_FOLDER_REPAIR_PREFIXES) -> bool:
    text = str(segment or "").strip()
    clean = strip_number_prefix(text)
    if clean in {JUDGMENT_FOLDER_LABEL, LEGACY_JUDGMENT_FOLDER_LABEL, JUDGMENT_DRIVE_LABEL}:
        return True
    return any(text in judgment_folder_aliases(prefix, include_plain=False) for prefix in prefixes)


def path_has_judgment_folder(value: str, *, prefixes: tuple[int, ...] = JUDGMENT_FOLDER_REPAIR_PREFIXES) -> bool:
    parts = [p for p in str(value or "").replace("\\", "/").split("/") if p]
    return any(is_judgment_folder_segment(part, prefixes=prefixes) for part in parts)


def judgment_folder_matches(target: str, folder_name: str) -> bool:
    target_clean = strip_number_prefix(target)
    folder_clean = strip_number_prefix(folder_name)
    if is_judgment_folder_segment(target_clean) and is_judgment_folder_segment(folder_clean):
        return True
    return target_clean in folder_clean or folder_clean in target_clean


def sort_judgment_folders_first(folders: Iterable[str]) -> list[str]:
    items = list(folders)
    order = {name: idx for idx, name in enumerate(items)}

    def _rank(name: str) -> tuple[int, int]:
        clean = strip_number_prefix(name)
        if JUDGMENT_FOLDER_LABEL in clean:
            return (0, order.get(name, 0))
        if LEGACY_JUDGMENT_FOLDER_LABEL in clean:
            return (1, order.get(name, 0))
        return (2, order.get(name, 0))

    return sorted(items, key=_rank)


def judgment_folder_candidates(
    case_folder: Path,
    prefix: int | str = DEFAULT_JUDGMENT_FOLDER_PREFIX,
    *,
    include_plain: bool = True,
) -> tuple[Path, ...]:
    return tuple(Path(case_folder) / name for name in judgment_folder_aliases(prefix, include_plain=include_plain))


def first_existing_judgment_folder(
    case_folder: Path,
    prefix: int | str = DEFAULT_JUDGMENT_FOLDER_PREFIX,
    *,
    include_plain: bool = True,
) -> Path:
    for candidate in judgment_folder_candidates(case_folder, prefix, include_plain=include_plain):
        if candidate.is_dir():
            return candidate
    return Path(case_folder) / judgment_folder_name(prefix)
