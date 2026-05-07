from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path


SKILL_HIDDEN_NAMES = {".versions", "__pycache__"}
SKILL_GENERATED_PREFIXES = ("code-",)


def is_runtime_skill_dir_name(name: str, *, include_generated: bool = False) -> bool:
    if not name:
        return False
    if name.startswith(".") or name in SKILL_HIDDEN_NAMES:
        return False
    if (not include_generated) and name.startswith(SKILL_GENERATED_PREFIXES):
        return False
    return True


def iter_top_level_skill_dirs(
    root: str | Path,
    *,
    require_skill_md: bool = True,
    runnable_only: bool = False,
    include_generated: bool = False,
) -> Iterator[Path]:
    root_path = Path(root)
    if not root_path.exists():
        return

    for entry in sorted(root_path.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if not is_runtime_skill_dir_name(entry.name, include_generated=include_generated):
            continue
        if require_skill_md and not (entry / "SKILL.md").exists():
            continue
        if runnable_only and not (entry / "action.py").exists():
            continue
        yield entry
