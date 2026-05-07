"""
MAGI Command Registry
=====================
可擴展的指令路由表，取代 orchestrator._handle_command 中的巨型 if-elif 鏈。

用法：
    registry = CommandRegistry()

    @registry.command(keywords=["畫", "draw", "generate image"], pattern=r"(畫|draw|generate)")
    def handle_draw(ctx: CommandContext) -> str:
        ...
        return "圖片已生成"

    # dispatch
    result = registry.dispatch(ctx)
    if result is not None:
        return result  # handled
    # else: fallback to legacy _handle_command
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

logger = logging.getLogger("CommandRegistry")


@dataclass
class CommandContext:
    """All context a command handler needs."""
    user_id: str
    message: str
    msg_lower: str
    role: str
    platform: str
    orchestrator: object  # Orchestrator instance (avoid circular import)
    extra: dict = field(default_factory=dict)


@dataclass
class _CommandEntry:
    name: str
    handler: Callable[[CommandContext], Optional[str]]
    keywords: list[str] = field(default_factory=list)
    pattern: Optional[re.Pattern] = None
    admin_only: bool = False
    priority: int = 100  # lower = checked first


class CommandRegistry:
    def __init__(self):
        self._commands: list[_CommandEntry] = []

    def command(
        self,
        name: str = "",
        keywords: Sequence[str] = (),
        pattern: str = "",
        admin_only: bool = False,
        priority: int = 100,
    ):
        """Decorator to register a command handler."""
        compiled = re.compile(pattern, re.IGNORECASE) if pattern else None

        def decorator(fn: Callable[[CommandContext], Optional[str]]):
            entry = _CommandEntry(
                name=name or fn.__name__,
                handler=fn,
                keywords=list(keywords),
                pattern=compiled,
                admin_only=admin_only,
                priority=priority,
            )
            self._commands.append(entry)
            self._commands.sort(key=lambda e: e.priority)
            return fn

        return decorator

    def register(
        self,
        fn: Callable[[CommandContext], Optional[str]],
        name: str = "",
        keywords: Sequence[str] = (),
        pattern: str = "",
        admin_only: bool = False,
        priority: int = 100,
    ):
        """Imperative registration (alternative to decorator)."""
        compiled = re.compile(pattern, re.IGNORECASE) if pattern else None
        entry = _CommandEntry(
            name=name or fn.__name__,
            handler=fn,
            keywords=list(keywords),
            pattern=compiled,
            admin_only=admin_only,
            priority=priority,
        )
        self._commands.append(entry)
        self._commands.sort(key=lambda e: e.priority)

    def dispatch(self, ctx: CommandContext) -> Optional[str]:
        """
        Try to dispatch a command. Returns the response string if handled,
        or None if no command matched (caller should fall back).
        """
        for entry in self._commands:
            if entry.admin_only and ctx.role != "admin":
                continue
            matched = False
            if entry.keywords:
                for kw in entry.keywords:
                    if kw in ctx.msg_lower:
                        matched = True
                        break
            if not matched and entry.pattern:
                if entry.pattern.search(ctx.msg_lower):
                    matched = True
            if matched:
                try:
                    result = entry.handler(ctx)
                    if result is not None:
                        return result
                    # handler returned None → didn't actually handle, continue
                except Exception as e:
                    logger.error("Command '%s' failed: %s", entry.name, e)
                    return f"⚠️ 指令執行失敗：{e}"
        return None

    def list_commands(self) -> list[dict]:
        """Return registered commands for introspection."""
        return [
            {
                "name": e.name,
                "keywords": e.keywords,
                "pattern": e.pattern.pattern if e.pattern else None,
                "admin_only": e.admin_only,
                "priority": e.priority,
            }
            for e in self._commands
        ]
