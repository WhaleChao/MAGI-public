from __future__ import annotations

from dataclasses import dataclass

from .models import PermissionDecision, PermissionEffect, PermissionMode, PermissionRule
from .policy import PermissionPolicy


@dataclass(slots=True)
class PermissionEnforcer:
    """Evaluate permissions using a policy + declarative rules."""

    policy: PermissionPolicy = PermissionPolicy()

    def __init__(
        self,
        policy: PermissionPolicy | None = None,
        *,
        mode: str | PermissionMode | None = None,
        rules: list[PermissionRule] | tuple[PermissionRule, ...] | None = None,
    ) -> None:
        if policy is None:
            base = PermissionPolicy()
            if mode is not None:
                base = base.with_mode(mode)
            if rules is not None:
                base = base.with_rules(rules)
            policy = base
        object.__setattr__(self, "policy", policy)

    def evaluate_command(self, command: str) -> PermissionDecision:
        return self._evaluate(subject_kind="command", subject=command)

    def evaluate_path(self, path: str) -> PermissionDecision:
        return self._evaluate(subject_kind="path", subject=path)

    def can_command(self, command: str) -> bool:
        return self.evaluate_command(command).allowed

    def can_path(self, path: str) -> bool:
        return self.evaluate_path(path).allowed

    def _evaluate(self, *, subject_kind: str, subject: str) -> PermissionDecision:
        matches: list[PermissionRule] = []
        for rule in self.policy.rules:
            if subject_kind == "command" and rule.matches_command(subject):
                matches.append(rule)
            elif subject_kind == "path" and rule.matches_path(subject):
                matches.append(rule)

        if matches:
            matches.sort(key=lambda rule: (rule.priority, 0 if rule.effect == PermissionEffect.DENY else 1, rule.name))
            deny = next((rule for rule in matches if rule.effect == PermissionEffect.DENY), None)
            if deny is not None:
                return PermissionDecision(
                    allowed=False,
                    reason=self._format_rule_reason(deny, default=False),
                    mode=self.policy.mode,
                    subject_kind=subject_kind,
                    subject=subject,
                    matched_rule=deny.name,
                    effect=deny.effect,
                    details=(deny.summary(),),
                )
            allow = matches[0]
            return PermissionDecision(
                allowed=True,
                reason=self._format_rule_reason(allow, default=True),
                mode=self.policy.mode,
                subject_kind=subject_kind,
                subject=subject,
                matched_rule=allow.name,
                effect=allow.effect,
                details=(allow.summary(),),
            )

        if self.policy.mode == PermissionMode.PERMISSIVE:
            return PermissionDecision(
                allowed=True,
                reason=self.policy.default_allow_reason,
                mode=self.policy.mode,
                subject_kind=subject_kind,
                subject=subject,
            )

        return PermissionDecision(
            allowed=False,
            reason=self.policy.default_deny_reason,
            mode=self.policy.mode,
            subject_kind=subject_kind,
            subject=subject,
        )

    def _format_rule_reason(self, rule: PermissionRule, *, default: bool) -> str:
        prefix = "allow" if default else "deny"
        suffix = f": {rule.reason}" if rule.reason else ""
        return f"{prefix}_rule[{rule.name}]{suffix}"
