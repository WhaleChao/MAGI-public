from __future__ import annotations

from datetime import datetime
from pathlib import Path

from scripts.ops import model_live_gate as gate
from scripts.ops.omlx_profile_policy import expected_profile_for_minutes, expected_profile_now


def _probe(port: int, model: str = "", ok: bool = True) -> gate.EndpointProbe:
    return gate.EndpointProbe(port=port, ok=ok, model_id=model, error="" if ok else "down")


def test_omlx_profile_policy_boundaries_are_single_source_of_truth():
    assert expected_profile_for_minutes(6 * 60 + 34) == ("night", "26b")
    assert expected_profile_for_minutes(6 * 60 + 35) == ("day", "e4b")
    assert expected_profile_for_minutes(21 * 60 + 49) == ("day", "e4b")
    assert expected_profile_for_minutes(21 * 60 + 50) == ("night", "26b")
    assert expected_profile_now(datetime(2026, 5, 27, 6, 35)) == ("day", "e4b")


def test_daemon_does_not_keep_a_second_omlx_window_definition():
    source = Path("daemon.py").read_text(encoding="utf-8")
    assert "from scripts.ops.omlx_profile_policy import expected_profile_now" in source
    assert '("day", "e4b")' not in source
    assert "415 <= minutes" not in source


def test_gemma4_overlay_wrapper_does_not_double_prepend_serve():
    source = Path("scripts/ops/prepare_omlx_gemma4_unified_runtime.py").read_text(encoding="utf-8")
    assert '-m omlx.cli "$@"' in source
    assert '-m omlx.cli serve "$@"' not in source


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
    assert report.next_actions == []
    assert report.restart_hint == ""
    assert report.profile_hint == ""


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


def test_day_gate_failure_includes_actionable_runtime_hints(monkeypatch):
    probes = {
        8080: _probe(8080, "", ok=False),
        8081: _probe(8081, "modernbert-embed-4bit"),
        8082: _probe(8082, "", ok=False),
        8083: _probe(8083, "", ok=False),
    }
    monkeypatch.setattr(gate, "probe_port", lambda port, timeout=3.0: probes[port])
    monkeypatch.setattr(gate, "active_profile", lambda: "night")

    report = gate.build_report("day")

    assert report.ok is False
    assert any("8080 expected E4B" in item for item in report.failures)
    assert any("config/bin/omlx_switch_model.sh auto" in item for item in report.next_actions)
    assert any("8082/Phi-4" in item and "8083/SmolLM" in item for item in report.next_actions)
    assert "8080 is unreachable" in report.restart_hint
    assert "Expected profile=day" in report.profile_hint
    assert "active_profile=night" in report.profile_hint


def test_day_gate_treats_e4b_as_normal_primary(monkeypatch):
    probes = {
        8080: _probe(8080, "gemma-4-e4b-it-4bit"),
        8081: _probe(8081, "modernbert-embed-4bit"),
        8082: _probe(8082, "Phi-4-mini-instruct-4bit"),
        8083: _probe(8083, "SmolLM3-3B-4bit"),
    }
    monkeypatch.setattr(gate, "probe_port", lambda port, timeout=3.0: probes[port])
    monkeypatch.setattr(gate, "active_profile", lambda: "day-e4b-degraded")
    report = gate.build_report("day")
    assert report.ok is True
    assert report.degraded is False


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


def test_night_gate_accepts_12b_as_degraded_fallback(monkeypatch):
    probes = {
        8080: _probe(8080, "gemma-4-12B-it-4bit"),
        8081: _probe(8081, "modernbert-embed-4bit"),
        8082: _probe(8082, "", ok=False),
        8083: _probe(8083, "", ok=False),
    }
    monkeypatch.setattr(gate, "probe_port", lambda port, timeout=3.0: probes[port])
    monkeypatch.setattr(gate, "active_profile", lambda: "night-12b-degraded")
    report = gate.build_report("night")
    assert report.ok is True
    assert report.degraded is True
    assert report.degraded_reason == "night_fell_back_to_12b"


def test_night_switch_uses_12b_before_e4b_last_resort():
    source = Path("config/bin/omlx_switch_model.sh").read_text(encoding="utf-8")
    night_start = source.index("  night)")
    status_start = source.index("  status)", night_start)
    night_block = source[night_start:status_start]
    assert "start_night_12b_fallback" in source
    assert "start_night_e4b_last_resort" in source
    assert "night_fallback_cooldown_active" in source
    assert "night-12b-degraded" in source
    assert 'start_night_12b_fallback "26B 記憶體不足"' in night_block
    assert 'start_night_e4b_last_resort "26B 記憶體不足"' not in night_block
