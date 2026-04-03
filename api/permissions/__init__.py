"""Lightweight permission foundation for MAGI."""

from .models import PermissionDecision, PermissionEffect, PermissionMode, PermissionRule
from .policy import PermissionPolicy
from .enforcer import PermissionEnforcer
from .rules import (
    allow_command,
    allow_path,
    deny_command,
    deny_path,
)

__all__ = [
    "PermissionDecision",
    "PermissionEffect",
    "PermissionMode",
    "PermissionRule",
    "PermissionPolicy",
    "PermissionEnforcer",
    "allow_command",
    "allow_path",
    "deny_command",
    "deny_path",
]
