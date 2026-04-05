"""Tests for api.routing.route_explanations — route trace collector."""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.routing.route_explanations import RouteExplanationCollector


def test_empty_collector():
    c = RouteExplanationCollector()
    assert len(c) == 0
    assert c.dispatched_skill is None
    assert not c.had_dispatch
    assert c.as_trace() == []


def test_record_dispatch():
    c = RouteExplanationCollector()
    c.record(
        skill="pdf-namer",
        confidence=0.85,
        dispatched=True,
        reason="DIRECT tier",
        intent="CMD",
        method="embedding",
    )
    assert len(c) == 1
    assert c.dispatched_skill == "pdf-namer"
    assert c.had_dispatch


def test_record_rejection():
    c = RouteExplanationCollector()
    c.record_rejection(
        skill="calendar",
        confidence=0.45,
        reason="below threshold",
        intent="CHAT",
        min_required=0.78,
    )
    assert len(c) == 1
    assert c.dispatched_skill is None
    assert not c.had_dispatch


def test_multiple_records():
    c = RouteExplanationCollector()
    c.record_rejection(
        skill="calendar",
        confidence=0.45,
        reason="below threshold",
    )
    c.record(
        skill="pdf-namer",
        confidence=0.85,
        dispatched=True,
        reason="DIRECT match",
    )
    assert len(c) == 2
    assert c.dispatched_skill == "pdf-namer"
    assert c.had_dispatch


def test_as_trace_format():
    c = RouteExplanationCollector()
    c.record(
        skill="worldmonitor-intel",
        confidence=0.72,
        dispatched=True,
        reason="GUIDED tier + CMD",
        intent="CMD",
        method="embedding",
        min_required=0.55,
    )
    trace = c.as_trace()
    assert len(trace) == 1
    assert trace[0]["skill"] == "worldmonitor-intel"
    assert trace[0]["confidence"] == 0.72
    assert trace[0]["dispatched"] is True


def test_summary_text():
    c = RouteExplanationCollector()
    c.record(
        skill="pdf-namer",
        confidence=0.85,
        dispatched=True,
        reason="DIRECT",
    )
    c.record_rejection(
        skill="calendar",
        confidence=0.45,
        reason="generic word",
    )
    summary = c.summary()
    assert "pdf-namer" in summary
    assert "calendar" in summary


def test_no_records_summary():
    c = RouteExplanationCollector()
    assert "無路由記錄" in c.summary()
