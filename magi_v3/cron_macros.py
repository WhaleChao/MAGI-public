"""Deterministic production entrypoints for exact scheduled MAGI macros.

Only exact prompts with a reviewed script equivalent belong here.  All other
macros continue through the conversational orchestrator, so ordinary user
language is not silently reinterpreted as an executable command.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CronMacroEntrypoint:
    prompt: str
    entrypoint: str
    arguments: tuple[str, ...]

    def argv(self, release_root: Path, python: str) -> tuple[str, ...]:
        script = (release_root / self.entrypoint).resolve(strict=True)
        script.relative_to(release_root.resolve(strict=True))
        return (python, str(script), *self.arguments)


_EXACT_MACROS = {
    "系統狀態": CronMacroEntrypoint(
        prompt="系統狀態",
        entrypoint="scripts/ops/system_diagnostic_report.py",
        arguments=(),
    ),
    "自動巡檢": CronMacroEntrypoint(
        prompt="自動巡檢",
        entrypoint="scripts/ops/system_diagnostic_report.py",
        arguments=(),
    ),
}


def resolve_exact_cron_macro(prompt: str) -> CronMacroEntrypoint | None:
    """Resolve only an exact reviewed scheduler prompt."""

    return _EXACT_MACROS.get(str(prompt or "").strip())
