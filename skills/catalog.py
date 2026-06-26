from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path


SKILL_HIDDEN_NAMES = {".versions", "__pycache__"}
SKILL_GENERATED_PREFIXES = ("code-",)
SKILL_SHIM_ALIASES = {
    "iron_dome": "iron-dome",
    "osc_orchestrator": "osc-orchestrator",
}
DEFINITION_GENERATED_PREFIXES = ("run_code_",)
DEFINITION_SHIM_NAMES = {
    "run_iron_dome",
    "run_osc_orchestrator",
}
DEPRECATED_AUTOROUTE_SKILLS = {
    "pdf_annotate",
}
DEPRECATED_SKILL_DIRS = {
    "pdf-annotator",
}


def is_runtime_skill_dir_name(name: str, *, include_generated: bool = False) -> bool:
    if not name:
        return False
    if name.startswith(".") or name in SKILL_HIDDEN_NAMES:
        return False
    if (not include_generated) and name.startswith(SKILL_GENERATED_PREFIXES):
        return False
    return True


def canonical_skill_dir_name(name: str) -> str:
    """Return the canonical directory name for known shim aliases."""
    return SKILL_SHIM_ALIASES.get(str(name or "").strip(), str(name or "").strip())


def is_public_skill_dir_name(
    name: str,
    *,
    include_generated: bool = False,
    include_shims: bool = False,
) -> bool:
    if not is_runtime_skill_dir_name(name, include_generated=include_generated):
        return False
    if (not include_shims) and name in SKILL_SHIM_ALIASES:
        return False
    return True


def is_deprecated_skill_dir_name(name: str) -> bool:
    return str(name or "").strip() in DEPRECATED_SKILL_DIRS


def is_public_definition_tool(
    tool: dict,
    *,
    include_generated: bool = False,
    include_shims: bool = False,
    include_deprecated: bool = True,
) -> bool:
    if not isinstance(tool, dict):
        return False
    name = str(tool.get("name") or "").strip()
    if not name:
        return False
    if (not include_generated) and name.startswith(DEFINITION_GENERATED_PREFIXES):
        return False
    if (not include_shims) and name in DEFINITION_SHIM_NAMES:
        return False
    if (not include_deprecated) and name in DEPRECATED_AUTOROUTE_SKILLS:
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
        if not is_public_skill_dir_name(entry.name, include_generated=include_generated):
            continue
        if require_skill_md and not (entry / "SKILL.md").exists():
            continue
        if runnable_only and not (entry / "action.py").exists():
            continue
        yield entry
