"""Regression tests for memory provenance parsing and trust labels."""

from api.session.provenance import (
    build_source_signature,
    default_confidence_for_source,
    parse_source_provenance,
    render_provenance_badge,
)


def test_parse_source_provenance_preserves_explicit_fields():
    prov = parse_source_provenance("chatlog|platform=LINE|role=user|verified=0|conf=0.82")

    assert prov.source_type == "chatlog"
    assert prov.role == "user"
    assert prov.verified is False
    assert prov.confidence == 0.82
    assert prov.metadata["platform"] == "LINE"
    assert prov.trust_label == "原始對話"


def test_build_source_signature_merges_structured_metadata():
    source = build_source_signature(
        "user_chat_123",
        source_type="user_confirmed",
        verified=True,
        confidence=0.94,
        role="user",
        source_id="abc123",
        metadata={"platform": "Discord", "thread": "dm"},
    )

    assert source.startswith("user_confirmed|")
    assert "verified=1" in source
    assert "conf=0.94" in source
    assert "role=user" in source
    assert "source_id=abc123" in source
    assert "platform=Discord" in source
    assert "thread=dm" in source


def test_default_confidence_reflects_source_risk():
    assert default_confidence_for_source("user_rule") == 0.98
    assert default_confidence_for_source("chatlog", role="assistant") == 0.18
    assert default_confidence_for_source("chatlog", role="user") == 0.82


def test_derived_memory_is_downgraded_to_hint():
    prov = parse_source_provenance(
        "assistant_generated|derived_from=assistant_reply|conf=0.80|role=assistant"
    )

    assert prov.verified is False
    assert prov.confidence <= 0.20
    assert prov.trust_label == "衍生線索"
    assert render_provenance_badge(prov.raw_source).startswith("衍生線索｜信心 ")
