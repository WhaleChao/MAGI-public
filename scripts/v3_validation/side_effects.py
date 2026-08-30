from __future__ import annotations

from dataclasses import dataclass


SIDE_EFFECT_CLASSES = frozenset(
    {"none", "read_only", "local_draft", "reversible_write", "external_commit", "destructive"}
)
VALIDATION_PHASES = frozenset({"offline_replay", "isolated_live_validation"})


@dataclass(frozen=True)
class SideEffectDecision:
    allowed: bool
    execute: bool
    reason: str
    side_effect_class: str
    phase: str


def evaluate_side_effect(
    side_effect_class: str,
    *,
    phase: str = "offline_replay",
    sandboxed: bool = False,
    allow_sandbox_writes: bool = False,
    dry_run: bool = False,
) -> SideEffectDecision:
    """Fail-closed validation policy.

    Offline replay never executes an operation. Isolated live validation may
    execute only none/read_only by default. Local/safely reversible writes need
    an explicit sandbox opt-in; external commits and destructive operations are
    unconditionally blocked by this harness.
    """

    effect = str(side_effect_class or "").strip().lower()
    if effect not in SIDE_EFFECT_CLASSES:
        return SideEffectDecision(False, False, "unknown_side_effect_class", effect, phase)
    if phase not in VALIDATION_PHASES:
        return SideEffectDecision(False, False, "unknown_validation_phase", effect, phase)
    if phase == "offline_replay":
        return SideEffectDecision(True, False, "offline_replay_never_executes", effect, phase)
    if effect in {"none", "read_only"}:
        return SideEffectDecision(True, True, "read_only_live_probe", effect, phase)
    if effect in {"external_commit", "destructive"}:
        return SideEffectDecision(False, False, "external_or_destructive_write_forbidden", effect, phase)
    if effect in {"local_draft", "reversible_write"} and sandboxed and allow_sandbox_writes:
        return SideEffectDecision(True, not dry_run, "explicit_isolated_sandbox", effect, phase)
    return SideEffectDecision(False, False, "write_blocked_by_default", effect, phase)
