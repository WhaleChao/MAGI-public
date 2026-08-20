from pathlib import Path

from scripts.ops import input_method_watchdog
from scripts.ops.input_method_watchdog import check_once


def test_input_method_watchdog_keeps_normal_process_healthy(tmp_path):
    result = check_once(
        state_path=tmp_path / "state.json",
        processes=[{"pid": 123, "rss_kb": 64 * 1024, "cpu": 2.5}],
        restart=False,
    )

    assert result["status"] == "healthy"
    assert result["strikes"] == 0


def test_input_method_watchdog_requires_consecutive_strikes(tmp_path):
    state = tmp_path / "state.json"
    process = [{"pid": 123, "rss_kb": 700 * 1024, "cpu": 1.0}]

    first = check_once(state_path=state, processes=process, restart=False)
    second = check_once(state_path=state, processes=process, restart=False)
    normal = check_once(
        state_path=state,
        processes=[{"pid": 123, "rss_kb": 64 * 1024, "cpu": 1.0}],
        restart=False,
    )

    assert first["strikes"] == 1
    assert second["strikes"] == 2
    assert normal["strikes"] == 0


def test_input_method_watchdog_tracks_missing_process_as_a_strike(tmp_path):
    result = check_once(state_path=tmp_path / "state.json", processes=[], restart=False)

    assert result == {
        "ok": True,
        "status": "watching",
        "checked_at": result["checked_at"],
        "reason": "input_method_process_missing",
        "strikes": 1,
        "restart_count": 0,
    }


def test_input_method_watchdog_recovers_missing_candidate_services(monkeypatch, tmp_path):
    restarted = []
    monkeypatch.setattr(
        input_method_watchdog,
        "_restart_input_stack",
        lambda *, reset_text_services: restarted.append(reset_text_services),
    )
    monkeypatch.setattr(input_method_watchdog.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(input_method_watchdog, "_wait_for_exit", lambda _pid: None)
    state = tmp_path / "state.json"
    process = [{"pid": 123, "rss_kb": 64 * 1024, "cpu": 1.0}]

    first = check_once(
        state_path=state,
        processes=process,
        text_services_ok=False,
        strikes_required=2,
    )
    second = check_once(
        state_path=state,
        processes=process,
        text_services_ok=False,
        strikes_required=2,
    )

    assert first["status"] == "watching"
    assert second["status"] == "restarted"
    assert restarted == [True]


def test_input_method_watchdog_records_whether_candidate_window_is_expected(tmp_path):
    result = check_once(
        state_path=tmp_path / "state.json",
        processes=[{"pid": 123, "rss_kb": 64 * 1024, "cpu": 1.0}],
        text_services_ok=True,
        input_source_id=input_method_watchdog.TARGET_INPUT_SOURCE_ID,
        restart=False,
    )

    assert result["bopomofo_selected"] is True
    assert result["candidate_window_expected"] is True


def test_input_method_watchdog_does_not_treat_intentional_us_source_as_process_failure(tmp_path):
    result = check_once(
        state_path=tmp_path / "state.json",
        processes=[{"pid": 123, "rss_kb": 64 * 1024, "cpu": 1.0}],
        text_services_ok=True,
        input_source_id="com.apple.keylayout.US",
        restart=False,
    )

    assert result["status"] == "healthy"
    assert result["reason"] == ""
    assert result["bopomofo_selected"] is False
    assert result["candidate_window_expected"] is False


def test_input_method_main_one_shot_selects_before_check(monkeypatch, tmp_path):
    monkeypatch.setattr(input_method_watchdog, "_select_input_source", lambda: True)
    monkeypatch.setattr(input_method_watchdog.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        input_method_watchdog,
        "check_once",
        lambda **_kwargs: {
            "ok": True,
            "status": "healthy",
            "input_source_id": input_method_watchdog.TARGET_INPUT_SOURCE_ID,
        },
    )

    assert input_method_watchdog.main(["--select-bopomofo-once", "--no-restart"]) == 0
