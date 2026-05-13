from __future__ import annotations

from scripts.ops import model_live_gate as gate


def _probe(port: int, model: str = "", ok: bool = True) -> gate.EndpointProbe:
    return gate.EndpointProbe(port=port, ok=ok, model_id=model, error="" if ok else "down")


def test_day_gate_requires_primary_and_aux(monkeypatch):
    probes = {
        8080: _probe(8080, "gemma-4-e4b-it-4bit"),
        8081: _probe(8081, "modernbert-embed-4bit"),
        8082: _probe(8082, "Phi-4-mini-instruct-4bit"),
        8083: _probe(8083, "SmolLM3-3B-4bit"),
    }
    monkeypatch.setattr(gate, "probe_port", lambda port, timeout=3.0: probes[port])
    monkeypatch.setattr(gate, "active_profile", lambda: "day")
    report = gate.build_report("day")
    assert report.ok is True
    assert report.degraded is False


def test_day_gate_fails_when_question_asks_for_day_but_26b_is_live(monkeypatch):
    probes = {
        8080: _probe(8080, "gemma-4-26b-a4b-it-4bit"),
        8081: _probe(8081, "modernbert-embed-4bit"),
        8082: _probe(8082, "Phi-4-mini-instruct-4bit"),
        8083: _probe(8083, "SmolLM3-3B-4bit"),
    }
    monkeypatch.setattr(gate, "probe_port", lambda port, timeout=3.0: probes[port])
    monkeypatch.setattr(gate, "active_profile", lambda: "day")
    report = gate.build_report("day")
    assert report.ok is False
    assert any("8080 expected E4B" in item for item in report.failures)


def test_night_gate_marks_e4b_as_degraded_fallback(monkeypatch):
    probes = {
        8080: _probe(8080, "gemma-4-e4b-it-4bit"),
        8081: _probe(8081, "modernbert-embed-4bit"),
        8082: _probe(8082, "", ok=False),
        8083: _probe(8083, "", ok=False),
    }
    monkeypatch.setattr(gate, "probe_port", lambda port, timeout=3.0: probes[port])
    monkeypatch.setattr(gate, "active_profile", lambda: "night")
    report = gate.build_report("night")
    assert report.ok is False
    assert report.degraded is True
    assert report.degraded_reason == "night_fell_back_to_e4b"
