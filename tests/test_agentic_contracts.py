from __future__ import annotations

import json

import pytest

from api.agentic import (
    ConfirmationRequirement,
    Constraint,
    Entity,
    IntentEnvelope,
    MissingField,
    SideEffectLevel,
)


def test_intent_envelope_exposes_structured_fields_and_lookup():
    envelope = IntentEnvelope(
        intent="case.search",
        utterance="找王小明的案件",
        entities=(
            Entity("party", "王小明", kind="person", confidence=0.92, source="user"),
            Entity("party", "陳小華", kind="person"),
        ),
        constraints=(Constraint("court", "臺北地院"),),
        missing_fields=(MissingField("date_range", prompt="請提供日期範圍"),),
        confidence=0.87,
        side_effect=SideEffectLevel.READ,
        request_id="req-1",
    )

    assert envelope.complete is False
    assert [item.value for item in envelope.entities_named("party")] == ["王小明", "陳小華"]
    assert envelope.side_effect is SideEffectLevel.READ
    assert envelope.requires_confirmation is False


@pytest.mark.parametrize("value", [-0.01, 1.01, float("inf"), float("nan")])
def test_confidence_must_be_finite_and_in_range(value):
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        Entity("case", "x", confidence=value)


def test_boolean_is_not_accepted_as_confidence():
    with pytest.raises(TypeError, match="must be a number"):
        IntentEnvelope(intent="chat", confidence=True)


def test_contracts_reject_non_json_values_and_non_string_mapping_keys():
    with pytest.raises(TypeError, match="JSON-compatible"):
        Entity("when", object())
    with pytest.raises(TypeError, match="keys must be strings"):
        IntentEnvelope(intent="chat", metadata={1: "bad"})


def test_missing_field_names_are_unique():
    with pytest.raises(ValueError, match="unique names"):
        IntentEnvelope(
            intent="document.create",
            missing_fields=(MissingField("case_id"), MissingField("case_id")),
        )


def test_required_confirmation_needs_reason_and_can_be_confirmed_immutably():
    with pytest.raises(ValueError, match="reason"):
        ConfirmationRequirement(required=True)

    requirement = ConfirmationRequirement(required=True, reason="writes calendar")
    confirmed = requirement.confirm("confirm-7")

    assert requirement.pending is True
    assert confirmed.pending is False
    assert confirmed.confirmation_id == "confirm-7"


def test_confirmation_cannot_be_marked_when_not_required():
    with pytest.raises(ValueError, match="not required"):
        ConfirmationRequirement(confirmed=True)


def test_intent_json_round_trip_preserves_unicode_and_nested_values():
    original = IntentEnvelope(
        intent="calendar.create",
        utterance="新增明天下午的會議",
        entities=(Entity("attendees", ["甲", "乙"], kind="people"),),
        constraints=(Constraint("duration", 60, operator="lte"),),
        confidence=0.9,
        side_effect=SideEffectLevel.WRITE,
        confirmation=ConfirmationRequirement(required=True, reason="external write", prompt="確定新增？"),
        metadata={"nested": {"enabled": True}},
    )

    payload = original.to_json()
    restored = IntentEnvelope.from_json(payload)

    assert "新增明天" in payload
    assert restored == original
    assert json.loads(payload)["side_effect"] == "write"


def test_unknown_schema_version_is_rejected():
    with pytest.raises(ValueError, match="unsupported intent schema"):
        IntentEnvelope.from_dict({"schema_version": 99, "intent": "chat"})


def test_invalid_enum_value_has_clear_error():
    with pytest.raises(ValueError, match="side_effect must be one of"):
        IntentEnvelope(intent="chat", side_effect="network")
