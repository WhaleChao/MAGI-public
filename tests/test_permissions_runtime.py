"""Tests for the lightweight permission foundation."""

from api.permissions import (
    PermissionEnforcer,
    PermissionMode,
    PermissionPolicy,
    allow_command,
    allow_path,
    deny_command,
    deny_path,
)


def test_default_behavior_denies_unknown_command_and_path():
    enforcer = PermissionEnforcer()

    command_decision = enforcer.evaluate_command("run anything")
    path_decision = enforcer.evaluate_path("/Users/ai/Desktop/MAGI_v2/tmp/output.txt")

    assert command_decision.allowed is False
    assert path_decision.allowed is False
    assert command_decision.mode == PermissionMode.STRICT
    assert path_decision.mode == PermissionMode.STRICT
    assert "default_deny" in command_decision.reason
    assert "default_deny" in path_decision.reason


def test_path_rules_allow_and_deny_by_prefix():
    policy = PermissionPolicy.from_rules(
        [
            allow_path(
                name="allow-static",
                paths=("/Users/ai/Desktop/MAGI_v2/static",),
                reason="static artifacts are safe",
                priority=10,
            ),
            deny_path(
                name="deny-secrets",
                paths=("/Users/ai/Desktop/MAGI_v2/static/secrets",),
                reason="secrets must remain blocked",
                priority=1,
            ),
        ]
    )
    enforcer = PermissionEnforcer(policy=policy)

    allowed = enforcer.evaluate_path("/Users/ai/Desktop/MAGI_v2/static/reports/report.md")
    denied = enforcer.evaluate_path("/Users/ai/Desktop/MAGI_v2/static/secrets/token.txt")

    assert allowed.allowed is True
    assert allowed.matched_rule == "allow-static"
    assert "allow_rule[allow-static]" in allowed.reason

    assert denied.allowed is False
    assert denied.matched_rule == "deny-secrets"
    assert "deny_rule[deny-secrets]" in denied.reason


def test_denied_command_rule_blocks_command():
    policy = PermissionPolicy.from_rules(
        [
            deny_command(
                name="deny-rm",
                commands=("rm", "rm -rf"),
                reason="destructive shell commands are blocked",
                priority=1,
            ),
            allow_command(
                name="allow-safe",
                commands=("python",),
                reason="python commands are allowed",
                priority=10,
            ),
        ]
    )
    enforcer = PermissionEnforcer(policy=policy)

    decision = enforcer.evaluate_command("rm -rf /tmp/test")

    assert decision.allowed is False
    assert decision.matched_rule == "deny-rm"
    assert decision.effect.value == "deny"
    assert "destructive shell commands" in decision.reason


def test_explicit_permissive_mode_allows_unknown_subjects():
    permissive = PermissionEnforcer(mode=PermissionMode.PERMISSIVE)

    command_decision = permissive.evaluate_command("mystery command")
    path_decision = permissive.evaluate_path("/unclassified/path.txt")

    assert command_decision.allowed is True
    assert path_decision.allowed is True
    assert command_decision.mode == PermissionMode.PERMISSIVE
    assert path_decision.mode == PermissionMode.PERMISSIVE
    assert "default_allow" in command_decision.reason
    assert "default_allow" in path_decision.reason


def test_policy_mode_override_keeps_strict_default():
    strict_policy = PermissionPolicy(mode=PermissionMode.ALLOWLIST)
    enforcer = PermissionEnforcer(policy=strict_policy)

    decision = enforcer.evaluate_command("unknown")

    assert decision.mode == PermissionMode.ALLOWLIST
    assert decision.allowed is False
    assert "default_deny" in decision.reason
