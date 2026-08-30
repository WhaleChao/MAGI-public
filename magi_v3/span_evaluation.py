"""Behavioral evaluation over PII-safe MAGI span records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .telemetry import TRACE_SCHEMA


@dataclass(frozen=True, slots=True)
class SpanExpectation:
    required_span_names: tuple[str, ...] = ()
    forbidden_span_names: tuple[str, ...] = ()
    required_tool_names: tuple[str, ...] = ()
    forbidden_tool_names: tuple[str, ...] = ()
    max_retry_count: int | None = None
    require_receipt: bool = False
    terminal_outcome: str | None = None


def evaluate_spans(
    spans: Iterable[Mapping[str, Any]],
    expectation: SpanExpectation,
) -> dict[str, Any]:
    """Evaluate calls, forbidden behavior, retries, receipts and terminal state."""

    rows = [dict(row) for row in spans]
    malformed = [index for index, row in enumerate(rows) if row.get("schema") != TRACE_SCHEMA]
    names = [str(row.get("name") or "") for row in rows]
    tools = [
        str((row.get("attributes") or {}).get("magi.tool.name") or "")
        for row in rows
        if isinstance(row.get("attributes"), Mapping)
    ]
    retries = [
        int((row.get("attributes") or {}).get("magi.retry.count") or 0)
        for row in rows
        if isinstance(row.get("attributes"), Mapping)
        and type((row.get("attributes") or {}).get("magi.retry.count") or 0) is int
    ]
    receipts = [
        str((row.get("attributes") or {}).get("magi.receipt.sha256") or "")
        for row in rows
        if isinstance(row.get("attributes"), Mapping)
    ]
    outcomes = [
        str((row.get("attributes") or {}).get("magi.outcome") or "")
        for row in rows
        if isinstance(row.get("attributes"), Mapping)
    ]
    failures: list[str] = []
    failures.extend(f"malformed_span:{index}" for index in malformed)
    for name in expectation.required_span_names:
        if name not in names:
            failures.append(f"required_span_missing:{name}")
    for name in expectation.forbidden_span_names:
        if name in names:
            failures.append(f"forbidden_span_observed:{name}")
    for tool in expectation.required_tool_names:
        if tool not in tools:
            failures.append(f"required_tool_missing:{tool}")
    for tool in expectation.forbidden_tool_names:
        if tool in tools:
            failures.append(f"forbidden_tool_observed:{tool}")
    if expectation.max_retry_count is not None and max(retries or [0]) > expectation.max_retry_count:
        failures.append("retry_limit_exceeded")
    if expectation.require_receipt and not any(len(value) == 64 for value in receipts):
        failures.append("receipt_missing")
    if expectation.terminal_outcome is not None and (not outcomes or outcomes[-1] != expectation.terminal_outcome):
        failures.append(f"terminal_outcome_mismatch:{expectation.terminal_outcome}")
    return {
        "ok": not failures,
        "span_count": len(rows),
        "observed_span_names": names,
        "observed_tool_names": [tool for tool in tools if tool],
        "max_retry_count": max(retries or [0]),
        "receipt_observed": any(len(value) == 64 for value in receipts),
        "terminal_outcome": outcomes[-1] if outcomes else "",
        "failures": failures,
    }
