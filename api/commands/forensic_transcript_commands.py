"""Discord/chat command registration for court-grade forensic transcript review."""

from __future__ import annotations

import sys
from pathlib import Path

from api.command_registry import CommandContext, CommandRegistry


ROOT = Path(__file__).resolve().parents[2]
LIVE_SCRIPTS = ROOT / "skills" / "forensic-transcript-verifier" / "scripts"


def _start_or_status() -> str:
    if str(LIVE_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(LIVE_SCRIPTS))
    from live_runtime import start_or_status

    return start_or_status()


def _handle_forensic_live(ctx: CommandContext) -> str:
    if str(ctx.role or "").lower() != "admin":
        return "⛔ `勘驗` 為管理員限定的法院級影音工作指令。"
    return _start_or_status()


def register_forensic_transcript_commands(registry: CommandRegistry) -> None:
    """Register one exact single-word command; no substring or fuzzy execution."""

    registry.register(
        _handle_forensic_live,
        name="forensic_transcript_live",
        pattern=r"^\s*勘驗\s*$",
        admin_only=False,
        priority=5,
    )
