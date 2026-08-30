from __future__ import annotations

import json

import pytest

from magi_v3.telemetry import (
    JsonlSpanExporter,
    OtlpHttpJsonExporter,
    TelemetryError,
    TraceContext,
    Tracer,
    current_trace_id,
    new_trace_context,
)


def test_w3c_trace_context_round_trip_and_child_linkage(tmp_path) -> None:
    parent = new_trace_context(trace_id="a" * 32)
    parsed = TraceContext.parse(parent.traceparent)
    assert parsed == parent
    tracer = Tracer("magi.test", exporter=JsonlSpanExporter(tmp_path / "spans.jsonl"))
    with tracer.start_span(
        "magi.agent.invoke",
        parent=parent,
        attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "MAGI"},
    ) as span:
        assert current_trace_id() == "a" * 32
        span.set_attribute("magi.outcome", "passed")
    row = json.loads((tmp_path / "spans.jsonl").read_text(encoding="utf-8"))
    assert row["trace_id"] == parent.trace_id
    assert row["parent_span_id"] == parent.span_id
    assert row["attributes"]["gen_ai.operation.name"] == "invoke_agent"


def test_span_rejects_content_and_paths_as_attributes(tmp_path) -> None:
    tracer = Tracer("magi.test", exporter=JsonlSpanExporter(tmp_path / "spans.jsonl"))
    with pytest.raises(TelemetryError, match="not allowlisted"):
        tracer.start_span("magi.agent.invoke", attributes={"prompt": "private case"})
    with pytest.raises(TelemetryError, match="safe categorical"):
        tracer.start_span("magi.agent.invoke", attributes={"magi.tool.name": "/" + "Users/person/case.pdf"})


def test_invalid_traceparent_is_not_accepted() -> None:
    assert TraceContext.parse("00-" + "0" * 32 + "-" + "1" * 16 + "-01") is None
    assert TraceContext.parse("future-format") is None


def test_otlp_exporter_is_loopback_only() -> None:
    assert OtlpHttpJsonExporter("http://127.0.0.1:4318/v1/traces").endpoint.endswith("/v1/traces")
    with pytest.raises(TelemetryError, match="loopback"):
        OtlpHttpJsonExporter("https://telemetry.example.test/v1/traces")
