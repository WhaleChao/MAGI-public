from __future__ import annotations

from dataclasses import dataclass, field

from .models import PermissionMode, PermissionRule


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """Immutable permission policy."""

    mode: PermissionMode = PermissionMode.STRICT
    rules: tuple[PermissionRule, ...] = field(default_factory=tuple)
    default_deny_reason: str = "default_deny: no matching allow rule"
    default_allow_reason: str = "default_allow: no matching deny rule"

    @classmethod
    def from_rules(
        cls,
        rules: list[PermissionRule] | tuple[PermissionRule, ...],
        *,
        mode: str | PermissionMode | None = None,
    ) -> "PermissionPolicy":
        return cls(mode=PermissionMode.coerce(mode), rules=tuple(rules))

    def with_mode(self, mode: str | PermissionMode) -> "PermissionPolicy":
        return PermissionPolicy(
            mode=PermissionMode.coerce(mode),
            rules=self.rules,
            default_deny_reason=self.default_deny_reason,
            default_allow_reason=self.default_allow_reason,
        )

    def with_rules(self, rules: list[PermissionRule] | tuple[PermissionRule, ...]) -> "PermissionPolicy":
        return PermissionPolicy(
            mode=self.mode,
            rules=tuple(rules),
            default_deny_reason=self.default_deny_reason,
            default_allow_reason=self.default_allow_reason,
        )
