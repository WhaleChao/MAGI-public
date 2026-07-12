from __future__ import annotations

import pytest

from api.tools.contracts import ToolContext, ToolSpec, normalize_tool_side_effect
from api.tools.registry import ToolRegistry


def test_tool_spec_exposes_side_effect_and_recovery_contract_metadata():
    spec = ToolSpec(
        name="publish",
        side_effect="external_commit",
        requires_confirmation=True,
        idempotent=False,
        verification=lambda output: bool(output),
        rollback={"handler": lambda output: True},
        retry={"max_attempts": 3},
        health={"endpoint": "/healthz"},
        degraded={"mode": "queue_for_review"},
    )

    contract = spec.as_dict()

    assert contract["side_effect"] == "external_commit"
    assert contract["requires_confirmation"] is True
    assert contract["idempotent"] is False
    assert contract["verification"]["enabled"] is True
    assert contract["rollback"]["enabled"] is True
    assert contract["retry"] == {"max_attempts": 3}
    assert contract["health"] == {"endpoint": "/healthz"}
    assert contract["degraded"] == {"mode": "queue_for_review"}


def test_registry_requires_explicit_permission_and_matching_confirmation_before_execution():
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register_callable(
        "publish",
        lambda: calls.append("executed") or "published",
        permission_tag="case:publish",
        side_effect="external_commit",
        requires_confirmation=True,
        metadata={"confirmation_token": "approve-publish"},
        verification=lambda output: output == "published",
    )

    denied = registry.execute("publish", context=ToolContext(permissions=set()))
    assert denied.success is False
    assert denied.error == "permission_denied"
    assert denied.metadata["contract"]["status"] == "blocked"
    assert denied.metadata["contract"]["retry"]["attempts"] == 0
    assert denied.metadata["contract"]["verification"]["status"] == "not_run"
    assert denied.metadata["contract"]["rollback"]["status"] == "not_available"
    assert calls == []

    unconfirmed = registry.execute(
        "publish",
        context=ToolContext(permissions={"case:publish"}, confirmation_token="wrong"),
    )
    assert unconfirmed.success is False
    assert unconfirmed.error == "confirmation_invalid"
    assert calls == []

    confirmed = registry.execute(
        "publish",
        context=ToolContext(permissions={"case:publish"}, confirmation_token="approve-publish"),
    )
    assert confirmed.success is True
    assert confirmed.output == "published"
    assert confirmed.metadata["contract"]["confirmation"]["status"] == "confirmed"
    assert calls == ["executed"]


def test_registry_retries_idempotent_tool_when_verification_fails_then_succeeds():
    attempts: list[int] = []
    registry = ToolRegistry()
    registry.register_callable(
        "sync",
        lambda: {"attempt": len(attempts) + 1},
        side_effect="reversible_write",
        idempotent=True,
        retry={"max_attempts": 2},
        verification=lambda output: (attempts.append(output["attempt"]), output["attempt"] == 2)[1],
        health={"check": "sync_state"},
        degraded={"mode": "hold"},
    )

    result = registry.execute("sync")

    assert result.success is True
    assert result.output == {"attempt": 2}
    assert attempts == [1, 2]
    assert result.metadata["contract"]["retry"] == {
        "configured_max_attempts": 2,
        "allowed": True,
        "attempts": 2,
    }
    assert result.metadata["contract"]["verification"]["status"] == "passed"
    assert result.metadata["contract"]["health"] == {"check": "sync_state"}
    assert result.metadata["contract"]["degraded"] == {"mode": "hold"}


def test_registry_does_not_retry_non_idempotent_tool_and_rolls_back_failed_verification():
    executions: list[str] = []
    rollbacks: list[dict[str, str]] = []
    registry = ToolRegistry()
    registry.register_callable(
        "delete_remote",
        lambda: executions.append("run") or {"operation": "delete"},
        side_effect="destructive",
        idempotent=False,
        retry={"max_attempts": 3},
        verification=lambda output: False,
        rollback=lambda output: rollbacks.append(output) or {"ok": True},
        metadata={"confirmation_token": "approve-delete"},
    )

    result = registry.execute("delete_remote", context=ToolContext(confirmation_token="approve-delete"))

    assert result.success is False
    assert result.error == "verification_failed"
    assert executions == ["run"]
    assert rollbacks == [{"operation": "delete"}]
    assert result.metadata["contract"]["retry"]["attempts"] == 1
    assert result.metadata["contract"]["rollback"]["status"] == "succeeded"


def test_invalid_side_effect_cannot_silently_downgrade_to_read_only():
    assert normalize_tool_side_effect("unknown-effect") == "destructive"
    with pytest.raises(ValueError, match="unsupported tool side effect"):
        ToolSpec(name="unsafe", side_effect="unknown-effect")


def test_external_commit_is_always_confirmed_and_verified():
    with pytest.raises(ValueError, match="require verification"):
        ToolSpec(name="publish", side_effect="external_commit")

    spec = ToolSpec(name="publish", side_effect="external_commit", verification=lambda output: True)
    assert spec.requires_confirmation is True
