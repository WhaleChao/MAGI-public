from api.tools.contracts import ToolContext, ToolSideEffect
from api.tools.registry import ToolRegistry


def test_schema_rejects_missing_wrong_and_unknown_arguments_before_executor():
    calls = []
    registry = ToolRegistry()
    registry.register_callable(
        "synthetic_read",
        lambda **kwargs: calls.append(kwargs) or "ok",
        input_schema={
            "type": "object",
            "required": ["case_id"],
            "properties": {
                "case_id": {"type": "string"},
                "mode": {"enum": ["brief", "full"]},
            },
            "additionalProperties": False,
        },
    )

    assert registry.execute("synthetic_read", {}).error == "input_validation_failed:missing_required:case_id"
    assert registry.execute("synthetic_read", {"case_id": 7}).error == "input_validation_failed:invalid_type:case_id"
    assert registry.execute("synthetic_read", {"case_id": "C-1", "extra": True}).error == "input_validation_failed:unexpected_property:extra"
    assert registry.execute("synthetic_read", {"case_id": "C-1", "mode": "bad"}).error == "input_validation_failed:invalid_enum:mode"
    assert calls == []


def test_irreversible_tool_rejects_arbitrary_confirmation_token_before_executor():
    calls = []
    registry = ToolRegistry()
    registry.register_callable(
        "synthetic_commit",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
        side_effect=ToolSideEffect.EXTERNAL_COMMIT,
        verification=lambda **_: True,
        input_schema={"type": "object", "required": ["case_id"], "properties": {"case_id": {"type": "string"}}},
    )

    result = registry.execute(
        "synthetic_commit",
        {"case_id": "synthetic-1"},
        ToolContext(confirmation_token="attacker-controlled-token"),
    )

    assert result.success is False
    assert result.error == "confirmation_binding_unavailable"
    assert calls == []


def test_irreversible_tool_accepts_only_spec_bound_confirmation_token():
    calls = []
    registry = ToolRegistry()
    registry.register_callable(
        "synthetic_commit",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
        side_effect=ToolSideEffect.EXTERNAL_COMMIT,
        verification=lambda **_: True,
        metadata={"confirmation_token": "server-issued-token"},
    )

    rejected = registry.execute("synthetic_commit", {}, ToolContext(confirmation_token="wrong-token"))
    accepted = registry.execute("synthetic_commit", {}, ToolContext(confirmation_token="server-issued-token"))

    assert rejected.error == "confirmation_invalid"
    assert accepted.success is True
    assert calls == [{}]


def test_read_tool_without_schema_or_confirmation_remains_compatible():
    registry = ToolRegistry()
    registry.register_callable("synthetic_read", lambda query="": {"query": query})

    result = registry.execute("synthetic_read", {"query": "safe"})

    assert result.success is True
    assert result.output == {"query": "safe"}
