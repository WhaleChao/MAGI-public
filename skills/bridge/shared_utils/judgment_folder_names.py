"""Shared naming helpers for final judgment/ruling case folders."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

JUDGMENT_FOLDER_LABEL = "判決書或終局裁定及處分"
LEGACY_JUDGMENT_FOLDER_LABEL = "判決書"
JUDGMENT_DRIVE_LABEL = "法院判決"
DEFAULT_JUDGMENT_FOLDER_PREFIX = 10


def judgment_folder_name(prefix: int | str = DEFAULT_JUDGMENT_FOLDER_PREFIX) -> str:
    """Return the canonical numbered folder name for final court documents."""
    number = int(prefix)
    return f"{number:02d}_{JUDGMENT_FOLDER_LABEL}"


def legacy_judgment_folder_name(prefix: int | str = DEFAULT_JUDGMENT_FOLDER_PREFIX) -> str:
    """Return the pre-2026 legacy numbered folder name."""
    number = int(prefix)
    return f"{number:02d}_{LEGACY_JUDGMENT_FOLDER_LABEL}"


def judgment_folder_aliases(
    prefix: int | str = DEFAULT_JUDGMENT_FOLDER_PREFIX,
    *,
    include_plain: bool = True,
) -> tuple[str, ...]:
    """Canonical folder first, followed by legacy aliases accepted for reads."""
    aliases = [judgment_folder_name(prefix), legacy_judgment_folder_name(prefix)]
    if include_plain:
        aliases.extend([JUDGMENT_FOLDER_LABEL, LEGACY_JUDGMENT_FOLDER_LABEL])
    return tuple(dict.fromkeys(aliases))


def strip_number_prefix(name: str) -> str:
    return re.sub(r"^\d+_", "", str(name or "").strip())


def is_judgment_folder_segment(segment: str, *, prefixes: tuple[int, ...] = (8, 9, 10)) -> bool:
    text = str(segment or "").strip()
    clean = strip_number_prefix(text)
    if clean in {JUDGMENT_FOLDER_LABEL, LEGACY_JUDGMENT_FOLDER_LABEL, JUDGMENT_DRIVE_LABEL}:
        return True
    return any(text in judgment_folder_aliases(prefix, include_plain=False) for prefix in prefixes)


def path_has_judgment_folder(value: str, *, prefixes: tuple[int, ...] = (8, 9, 10)) -> bool:
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
