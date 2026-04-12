from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
class PermissionMode(str, Enum):
    """Operating mode for permission evaluation."""

    STRICT = "strict"
    PERMISSIVE = "permissive"
    ALLOWLIST = "allowlist"

    @classmethod
    def coerce(cls, value: str | "PermissionMode" | None) -> "PermissionMode":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls.STRICT
        normalized = str(value).strip().lower()
        for mode in cls:
            if mode.value == normalized:
                return mode
        raise ValueError(f"Unknown permission mode: {value}")


class PermissionEffect(str, Enum):
    """Effect of a rule."""

    ALLOW = "allow"
    DENY = "deny"

    @classmethod
    def coerce(cls, value: str | "PermissionEffect") -> "PermissionEffect":
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().lower()
        for effect in cls:
            if effect.value == normalized:
                return effect
        raise ValueError(f"Unknown permission effect: {value}")


def _normalize_text(value: str) -> str:
    return " ".join(str(value).strip().split()).lower()


def _normalize_path(value: str) -> str:
    import os

    path = os.path.normpath(str(value).strip())
    if path == ".":
        return ""
    return path


def _match_prefix(value: str, prefix: str) -> bool:
    if not prefix:
        return False
    if value == prefix:
        return True
    if prefix == "/":
        return value.startswith("/")
    return value.startswith(prefix.rstrip("/" + "\\")) and (
        value == prefix.rstrip("/" + "\\")
        or value.startswith(prefix.rstrip("/" + "\\") + "/")
        or value.startswith(prefix.rstrip("/" + "\\") + "\\")
    )


@dataclass(frozen=True)
class PermissionRule:
    """Declarative allow/deny rule for commands or paths."""

    name: str
    effect: PermissionEffect
    command_prefixes: tuple[str, ...] = ()
    command_equals: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    path_equals: tuple[str, ...] = ()
    reason: str = ""
    priority: int = 100

    def matches_command(self, command: str) -> bool:
        command_norm = _normalize_text(command)
        if not command_norm:
            return False
        for exact in self.command_equals:
            if command_norm == _normalize_text(exact):
                return True
        for prefix in self.command_prefixes:
            if command_norm == _normalize_text(prefix):
                return True
            if command_norm.startswith(_normalize_text(prefix) + " "):
                return True
        return False

    def matches_path(self, path: str) -> bool:
        path_norm = _normalize_path(path)
        if not path_norm:
            return False
        for exact in self.path_equals:
            if path_norm == _normalize_path(exact):
                return True
        for prefix in self.path_prefixes:
            if _match_prefix(path_norm, _normalize_path(prefix)):
                return True
        return False

    def matches(self, *, command: str = "", path: str = "") -> bool:
        if command and self.matches_command(command):
            return True
        if path and self.matches_path(path):
            return True
        return False

    def summary(self) -> str:
        parts: list[str] = []
        if self.command_equals:
            parts.append(f"commands={list(self.command_equals)}")
        if self.command_prefixes:
            parts.append(f"command_prefixes={list(self.command_prefixes)}")
        if self.path_equals:
            parts.append(f"paths={list(self.path_equals)}")
        if self.path_prefixes:
            parts.append(f"path_prefixes={list(self.path_prefixes)}")
        return ", ".join(parts) if parts else "catch_all"


@dataclass(frozen=True)
class PermissionDecision:
    """Result of a permission evaluation."""

    allowed: bool
    reason: str
    mode: PermissionMode
    subject_kind: str
    subject: str
    matched_rule: Optional[str] = None
    effect: Optional[PermissionEffect] = None
    details: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "mode": self.mode.value,
            "subject_kind": self.subject_kind,
            "subject": self.subject,
            "matched_rule": self.matched_rule,
            "effect": self.effect.value if self.effect else None,
            "details": list(self.details),
        }
