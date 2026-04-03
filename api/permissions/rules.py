from __future__ import annotations

from .models import PermissionEffect, PermissionRule


def allow_command(*, name: str, commands: tuple[str, ...] | list[str], reason: str = "", priority: int = 100) -> PermissionRule:
    return PermissionRule(
        name=name,
        effect=PermissionEffect.ALLOW,
        command_prefixes=tuple(commands),
        reason=reason,
        priority=priority,
    )


def deny_command(*, name: str, commands: tuple[str, ...] | list[str], reason: str = "", priority: int = 100) -> PermissionRule:
    return PermissionRule(
        name=name,
        effect=PermissionEffect.DENY,
        command_prefixes=tuple(commands),
        reason=reason,
        priority=priority,
    )


def allow_path(*, name: str, paths: tuple[str, ...] | list[str], reason: str = "", priority: int = 100) -> PermissionRule:
    return PermissionRule(
        name=name,
        effect=PermissionEffect.ALLOW,
        path_prefixes=tuple(paths),
        reason=reason,
        priority=priority,
    )


def deny_path(*, name: str, paths: tuple[str, ...] | list[str], reason: str = "", priority: int = 100) -> PermissionRule:
    return PermissionRule(
        name=name,
        effect=PermissionEffect.DENY,
        path_prefixes=tuple(paths),
        reason=reason,
        priority=priority,
    )
