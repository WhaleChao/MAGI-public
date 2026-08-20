from __future__ import annotations

import pytest

from scripts.v3_validation.ime_candidate_probe import (
    ImeProbeError,
    _TYPE_PROBE_SCRIPT,
    _close_probe_document_if_present,
    _open_isolated_document,
    _prepare_isolated_document_for_probe,
    _restore_probe_document_count,
    _wait_for_candidate_windows_gone,
    _wait_for_frontmost_application,
    _wait_for_input_source_id,
    _wait_for_textedit_ready,
    _wait_for_textedit_document_count,
    _wait_for_textedit_stopped,
    build_evidence,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 10.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


def test_textedit_readiness_waits_for_front_document_and_window():
    states = iter(["no-document", "not-frontmost", "no-window", "ready"])
    clock = _FakeClock()

    latency_ms = _wait_for_textedit_ready(
        timeout_sec=0.5,
        readiness_reader=lambda: next(states),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert latency_ms == pytest.approx(150.0)
    assert clock.sleeps == pytest.approx([0.05, 0.05, 0.05])


def test_textedit_readiness_retries_transient_apple_event_error():
    attempts = iter([ImeProbeError("window handoff"), "ready"])
    clock = _FakeClock()

    def readiness_reader() -> str:
        result = next(attempts)
        if isinstance(result, BaseException):
            raise result
        return result

    latency_ms = _wait_for_textedit_ready(
        timeout_sec=0.5,
        readiness_reader=readiness_reader,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    assert latency_ms == pytest.approx(50.0)
    assert clock.sleeps == pytest.approx([0.05])


def test_frontmost_restore_waits_for_asynchronous_appkit_activation():
    states = iter(["TextEdit", "TextEdit", "Codex"])
    clock = _FakeClock()

    assert _wait_for_frontmost_application(
        "Codex",
        timeout_sec=0.5,
        frontmost_reader=lambda: next(states),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    ) is True
    assert clock.sleeps == pytest.approx([0.05, 0.05])


def test_input_source_restore_waits_for_asynchronous_tis_selection():
    states = iter(["old-source", "old-source", "restored-source"])
    clock = _FakeClock()

    assert _wait_for_input_source_id(
        "restored-source",
        timeout_sec=0.5,
        source_reader=lambda: next(states),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    ) is True
    assert clock.sleeps == pytest.approx([0.05, 0.05])


def test_candidate_window_baseline_waits_until_prior_ui_disappears():
    states = iter([[{"window_id": 9}], [{"window_id": 9}], []])
    clock = _FakeClock()

    assert _wait_for_candidate_windows_gone(
        {42},
        timeout_sec=0.5,
        window_reader=lambda _pids: next(states),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    ) is True
    assert clock.sleeps == pytest.approx([0.05, 0.05])


def test_candidate_window_baseline_fails_closed_at_bound():
    clock = _FakeClock()

    assert _wait_for_candidate_windows_gone(
        {42},
        timeout_sec=0.2,
        window_reader=lambda _pids: [{"window_id": 9}],
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    ) is False
    assert sum(clock.sleeps) == pytest.approx(0.2)


def test_probe_typing_never_activates_and_types_without_a_readiness_guard():
    assert "activate" not in _TYPE_PROBE_SCRIPT
    assert 'if (count of documents) is 0 then error' in _TYPE_PROBE_SCRIPT
    assert 'if frontmost is false then error' in _TYPE_PROBE_SCRIPT
    assert 'if (count of windows) is 0 then error' in _TYPE_PROBE_SCRIPT


def test_open_isolated_document_accepts_cold_launch_automatic_blank(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_running",
        lambda: False,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._run_osascript",
        lambda *_args: events.append("make"),
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._activate_and_wait_for_textedit_ready",
        lambda: events.append("ready") or 125.0,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_document_count",
        lambda: 2,
    )

    assert _open_isolated_document() == 125.0
    assert events == ["make", "ready"]


def test_open_isolated_document_fails_when_count_does_not_increase(monkeypatch):
    counts = iter([2, 2])
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_running",
        lambda: True,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_document_count",
        lambda: next(counts),
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._run_osascript",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._activate_and_wait_for_textedit_ready",
        lambda: 125.0,
    )

    with pytest.raises(ImeProbeError, match="did not create an isolated document"):
        _open_isolated_document()


def test_probe_selects_input_source_before_creating_editor_context(monkeypatch):
    events: list[str] = []

    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._select_input_source",
        lambda _source: events.append("select") or True,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._current_input_source_id",
        lambda: events.append("confirm") or "org.openvanilla.inputmethod.McBopomofo.McBopomofo.PlainBopomofo",
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._open_isolated_document",
        lambda: events.append("open") or 125.0,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_running", lambda: False
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._activate_and_wait_for_textedit_ready",
        lambda: events.append("focus") or 75.0,
    )

    readiness_ms = _prepare_isolated_document_for_probe()

    assert readiness_ms == 125.0
    assert events == ["select", "confirm", "open", "focus", "confirm", "confirm"]


def test_probe_rebuilds_editor_context_when_textedit_restores_other_source(monkeypatch):
    events: list[str] = []
    sources = iter(
        [
            "org.openvanilla.inputmethod.McBopomofo.McBopomofo.PlainBopomofo",
            "com.apple.keylayout.US",
            "org.openvanilla.inputmethod.McBopomofo.McBopomofo.PlainBopomofo",
        ]
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._select_input_source",
        lambda _source: events.append("select") or True,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._current_input_source_id",
        lambda: events.append("confirm") or next(sources),
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_running", lambda: False
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._open_isolated_document",
        lambda: events.append("open") or 125.0,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._activate_and_wait_for_textedit_ready",
        lambda: events.append("focus") or 75.0,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._wait_for_input_source_id",
        lambda _source: events.append("wait-source") or True,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._restore_probe_document_count",
        lambda _count: events.append("restore-baseline") or True,
    )

    readiness_ms = _prepare_isolated_document_for_probe()

    assert readiness_ms == 125.0
    assert events == [
        "select",
        "confirm",
        "open",
        "focus",
        "confirm",
        "select",
        "wait-source",
        "restore-baseline",
        "open",
        "focus",
        "confirm",
    ]


def test_textedit_readiness_fails_closed_at_bound():
    clock = _FakeClock()

    with pytest.raises(ImeProbeError, match="not-frontmost"):
        _wait_for_textedit_ready(
            timeout_sec=0.2,
            readiness_reader=lambda: "not-frontmost",
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

    assert sum(clock.sleeps) == pytest.approx(0.2)


def test_textedit_shutdown_waits_for_async_process_exit():
    states = iter([True, True, False])
    clock = _FakeClock()

    assert _wait_for_textedit_stopped(
        timeout_sec=0.5,
        running_reader=lambda: next(states),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    ) is True
    assert clock.sleeps == pytest.approx([0.05, 0.05])


def test_textedit_cleanup_waits_for_document_count_postcondition():
    counts = iter([2, 1, 0])
    clock = _FakeClock()

    assert _wait_for_textedit_document_count(
        0,
        timeout_sec=0.5,
        count_reader=lambda: next(counts),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    ) is True
    assert clock.sleeps == pytest.approx([0.05, 0.05])


def test_probe_cleanup_never_sends_keys_without_an_extra_document(monkeypatch):
    close_calls: list[bool] = []
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_running", lambda: True
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_document_count", lambda: 2
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._close_isolated_document",
        lambda: close_calls.append(True) or True,
    )

    assert _close_probe_document_if_present(2) is True
    assert close_calls == []


def test_probe_cleanup_closes_only_the_extra_isolated_document(monkeypatch):
    close_calls: list[bool] = []
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_running", lambda: True
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_document_count", lambda: 3
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._close_isolated_document",
        lambda: close_calls.append(True) or True,
    )

    assert _close_probe_document_if_present(2) is True
    assert close_calls == [True]


def test_probe_cleanup_removes_cold_launch_blank_and_explicit_document(monkeypatch):
    counts = iter([2, 1, 0])
    close_calls: list[bool] = []
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_running",
        lambda: True,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_document_count",
        lambda: next(counts),
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._close_isolated_document",
        lambda: close_calls.append(True) or True,
    )

    assert _restore_probe_document_count(0) is True
    assert close_calls == [True, True]


def test_probe_cleanup_never_crosses_existing_document_baseline(monkeypatch):
    close_calls: list[bool] = []
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_running",
        lambda: True,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._textedit_document_count",
        lambda: 2,
    )
    monkeypatch.setattr(
        "scripts.v3_validation.ime_candidate_probe._close_isolated_document",
        lambda: close_calls.append(True) or True,
    )

    assert _restore_probe_document_count(2) is True
    assert close_calls == []


def test_candidate_probe_evidence_requires_every_real_window_cycle():
    evidence = build_evidence(
        [
            {
                "cycle": 1,
                "detected": True,
                "latency_ms": 80.0,
                "window_count": 1,
                "readiness_ms": 150.0,
            },
            {
                "cycle": 2,
                "detected": True,
                "latency_ms": 120.0,
                "window_count": 1,
                "readiness_ms": 50.0,
            },
            {
                "cycle": 3,
                "detected": True,
                "latency_ms": 90.0,
                "window_count": 1,
                "readiness_ms": 100.0,
            },
        ],
        pressure_mb=256,
        memory_free_before=63.0,
        memory_free_during=59.0,
        services_healthy=True,
    )

    assert evidence["status"] == "passed"
    assert evidence["measurements"]["candidate_windows_detected"] == 3
    assert evidence["measurements"]["candidate_window_failures"] == 0
    assert evidence["measurements"]["candidate_latency_p95_ms"] == 120.0
    assert evidence["measurements"]["document_readiness_max_ms"] == 150.0
    assert evidence["temporary_native_ui_performed"] is True
    assert evidence["external_write_performed"] is False


def test_candidate_probe_evidence_fails_closed_on_one_missing_window():
    evidence = build_evidence(
        [
            {"cycle": 1, "detected": True, "latency_ms": 80.0, "window_count": 1},
            {"cycle": 2, "detected": False, "latency_ms": 2000.0, "window_count": 0},
        ],
        pressure_mb=256,
        memory_free_before=63.0,
        memory_free_during=59.0,
        services_healthy=True,
    )

    assert evidence["status"] == "failed"
    assert evidence["measurements"]["candidate_window_failures"] == 1


@pytest.mark.parametrize(
    "override",
    [
        {"pressure_mb": 0},
        {"cleanup_verified": False},
        {"input_source_restored": False},
        {"frontmost_application_restored": False},
        {"textedit_state_restored": False},
    ],
)
def test_candidate_probe_evidence_fails_closed_when_native_restoration_is_unproven(override):
    kwargs = {
        "pressure_mb": 256,
        "memory_free_before": 63.0,
        "memory_free_during": 59.0,
        "services_healthy": True,
        "cleanup_verified": True,
        "input_source_restored": True,
        "frontmost_application_restored": True,
        "textedit_state_restored": True,
    }
    kwargs.update(override)
    evidence = build_evidence(
        [
            {
                "cycle": 1,
                "detected": True,
                "latency_ms": 80.0,
                "window_count": 1,
                "preexisting_window_count": 0,
                "new_candidate_windows": [{"window_id": 99}],
            }
        ],
        **kwargs,
    )

    assert evidence["status"] == "failed"
