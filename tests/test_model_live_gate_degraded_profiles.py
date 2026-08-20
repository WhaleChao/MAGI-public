from pathlib import Path

from scripts.ops import model_live_gate


def _probe(port: int, model_id: str = "") -> model_live_gate.EndpointProbe:
    return model_live_gate.EndpointProbe(port=port, ok=bool(model_id), model_id=model_id)


def _night_probes(main_model: str):
    probes = {
        8080: _probe(8080, main_model),
        8081: _probe(8081, "modernbert-embed-4bit"),
        8082: _probe(8082),
        8083: _probe(8083),
    }
    return lambda port: probes[port]


def test_night_e4b_declared_last_resort_is_degraded_not_failed(monkeypatch):
    monkeypatch.setattr(model_live_gate, "active_profile", lambda: "night-e4b-degraded")
    monkeypatch.setattr(
        model_live_gate,
        "probe_port",
        _night_probes("gemma-4-e4b-it-4bit"),
    )

    report = model_live_gate.build_report("night")

    assert report.ok is True
    assert report.failures == []
    assert report.degraded is True
    assert report.degraded_reason == "night_fell_back_to_e4b"
    assert any("last-resort" in warning for warning in report.warnings)


def test_night_e4b_without_declared_degraded_profile_still_fails(monkeypatch):
    monkeypatch.setattr(model_live_gate, "active_profile", lambda: "night")
    monkeypatch.setattr(
        model_live_gate,
        "probe_port",
        _night_probes("gemma-4-e4b-it-4bit"),
    )

    report = model_live_gate.build_report("night")

    assert report.ok is False
    assert any("8080 expected" in failure for failure in report.failures)


def test_night_declared_fallback_does_not_hide_unreachable_8080(monkeypatch):
    monkeypatch.setattr(model_live_gate, "active_profile", lambda: "night-e4b-degraded")
    monkeypatch.setattr(model_live_gate, "probe_port", _night_probes(""))

    report = model_live_gate.build_report("night")

    assert report.ok is False
    assert any("8080 expected" in failure for failure in report.failures)


def test_switch_applies_cooldown_to_night_e4b_last_resort():
    source = (
        Path(__file__).resolve().parents[1] / "config/bin/omlx_switch_model.sh"
    ).read_text(encoding="utf-8")

    assert '[ "$active_profile_auto" = "night-e4b-degraded" ]' in source
    assert "night E4B 最後保底冷卻中" in source
    function = source[
        source.index("start_night_e4b_last_resort()") :
        source.index("start_night_12b_fallback()")
    ]
    assert 'date +%s > "$NIGHT_FALLBACK_STAMP_FILE"' in function


def test_switch_preserves_healthy_e4b_under_resource_pressure():
    source = (
        Path(__file__).resolve().parents[1] / "config/bin/omlx_switch_model.sh"
    ).read_text(encoding="utf-8")

    assert 'NIGHT_FALLBACK_RETRY_SEC="${MAGI_NIGHT_FALLBACK_RETRY_SEC:-21600}"' in source
    assert "preserve_current_e4b_for_night()" in source
    assert 'if preserve_current_e4b_for_night "本機資源低水位，暫不啟動 26B/12B"' in source
    assert "未中斷 8080" in source
    assert "MAGI_V3_PYTHON_RUNTIME_REALPATH" in source
