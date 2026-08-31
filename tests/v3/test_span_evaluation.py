from __future__ import annotations

from magi_v3.span_evaluation import SpanExpectation, evaluate_spans
from magi_v3.telemetry import TRACE_SCHEMA


def _span(name: str, **attributes):
    return {"schema": TRACE_SCHEMA, "name": name, "attributes": attributes}


def test_behavior_evaluation_proves_calls_absence_retries_receipt_and_terminal() -> None:
    result = evaluate_spans(
        [
            _span("magi.agent.invoke", **{"magi.tool.name": "laf_attachment_probe", "magi.retry.count": 1}),
            _span("magi.receipt.commit", **{"magi.receipt.sha256": "a" * 64, "magi.outcome": "passed"}),
        ],
        SpanExpectation(
            required_span_names=("magi.agent.invoke", "magi.receipt.commit"),
            forbidden_span_names=("magi.shell.exec",),
            required_tool_names=("laf_attachment_probe",),
            forbidden_tool_names=("raw_sql",),
            max_retry_count=1,
            require_receipt=True,
            terminal_outcome="passed",
        ),
    )
    assert result["ok"] is True


def test_behavior_evaluation_reports_every_policy_failure() -> None:
    result = evaluate_spans(
        [_span("magi.shell.exec", **{"magi.tool.name": "raw_sql", "magi.retry.count": 4, "magi.outcome": "failed"})],
        SpanExpectation(
            required_span_names=("magi.agent.invoke",),
            forbidden_span_names=("magi.shell.exec",),
            forbidden_tool_names=("raw_sql",),
            max_retry_count=1,
            require_receipt=True,
            terminal_outcome="passed",
        ),
    )
    assert result["ok"] is False
    assert "retry_limit_exceeded" in result["failures"]
    assert "receipt_missing" in result["failures"]
    assert "terminal_outcome_mismatch:passed" in result["failures"]
