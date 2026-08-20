from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.v3_cutover.core import (
    GateConfigError,
    Owner,
    Snapshot,
    assess_absolute_window,
    assess_cutover_window,
    assess_snapshot,
    load_gate_config,
)

COVERAGE = frozenset({"process", "pidfile", "port", "launchd", "ownership"})


def snapshot(*owners: Owner, errors: tuple[str, ...] = ()) -> Snapshot:
    return Snapshot(owners=owners, probe_errors=errors, coverage=COVERAGE)


def owner(release: str | None, domain: str, identity: str, *, pid: int | None = None, ambiguous=False) -> Owner:
    return Owner(release, domain, identity, "test", pid=pid, ambiguous=ambiguous)


def test_repository_gate_file_contains_single_active_no_go_rules() -> None:
    root = Path(__file__).resolve().parents[2]
    payload = load_gate_config(root / "config" / "v3_cutover_gates.json")
    assert "v2_process_or_release_owner_still_active_before_v3_start" in payload["automatic_no_go"]
    assert "v2_port_scheduler_writer_or_model_owner_not_released" in payload["automatic_no_go"]


def test_cutover_window_is_a_hard_timezone_aware_gate() -> None:
    window = {"start": "02:00", "end": "04:00"}
    inside = assess_cutover_window(
        window,
        timezone_name="Asia/Taipei",
        now=datetime(2026, 7, 13, 18, 30, tzinfo=timezone.utc),
    )
    outside = assess_cutover_window(
        window,
        timezone_name="Asia/Taipei",
        now=datetime(2026, 7, 14, 5, 23, tzinfo=timezone.utc),
    )

    assert inside["within_window"] is True
    assert inside["observed_at"].startswith("2026-07-14T02:30:00+08:00")
    assert outside["within_window"] is False
    assert outside["reason"] == "outside_cutover_window"


def test_cutover_window_supports_overnight_and_rejects_invalid_clock() -> None:
    assert assess_cutover_window(
        {"start": "23:00", "end": "02:00"},
        timezone_name="UTC",
        now=datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc),
    )["within_window"] is True
    with pytest.raises(GateConfigError, match="HH:MM"):
        assess_cutover_window(
            {"start": "2am", "end": "04:00"},
            timezone_name="UTC",
            now=datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc),
        )


def test_cutover_window_can_be_bound_to_explicit_local_dates() -> None:
    window = {
        "start": "00:00",
        "end": "23:59",
        "allowed_local_dates": ["2026-07-22"],
    }
    inside = assess_cutover_window(
        window,
        timezone_name="Asia/Taipei",
        now=datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc),
    )
    outside = assess_cutover_window(
        window,
        timezone_name="Asia/Taipei",
        now=datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc),
    )

    assert inside["within_window"] is True
    assert inside["allowed_local_dates"] == ["2026-07-22"]
    assert outside["within_window"] is False
    assert outside["reason"] == "outside_cutover_window"


def test_absolute_daytime_window_is_one_day_taipei_and_end_exclusive() -> None:
    window = {
        "starts_at": "2026-07-27T09:00:00+08:00",
        "ends_at": "2026-07-27T18:00:00+08:00",
        "timezone": "Asia/Taipei",
    }
    inside = assess_absolute_window(
        window,
        now=datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc),
    )
    at_end = assess_absolute_window(
        window,
        now=datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc),
    )
    next_day = assess_absolute_window(
        window,
        now=datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc),
    )

    assert inside["within_window"] is True
    assert inside["observed_at"].startswith("2026-07-27T17:00:00+08:00")
    assert at_end["within_window"] is False
    assert next_day["within_window"] is False
    assert next_day["reason"] == "outside_conditional_daytime_window"


@pytest.mark.parametrize(
    ("window", "match"),
    [
        ({"starts_at": "2026-07-27T05:59:00+08:00", "ends_at": "2026-07-27T09:00:00+08:00", "timezone": "Asia/Taipei"}, "06:00-22:00"),
        ({"starts_at": "2026-07-27T09:00:00+08:00", "ends_at": "2026-07-27T22:01:00+08:00", "timezone": "Asia/Taipei"}, "06:00-22:00"),
        ({"starts_at": "2026-07-27T18:00:00+08:00", "ends_at": "2026-07-28T09:00:00+08:00", "timezone": "Asia/Taipei"}, "same-day"),
        ({"starts_at": "2026-07-27T09:00:00Z", "ends_at": "2026-07-27T18:00:00Z", "timezone": "Asia/Taipei"}, "canonical Asia/Taipei"),
    ],
)
def test_absolute_daytime_window_rejects_any_non_daylight_or_noncanonical_policy(
    window: dict[str, str], match: str
) -> None:
    with pytest.raises(GateConfigError, match=match):
        assess_absolute_window(window)


def test_absolute_daytime_window_rejects_non_taipei_timezone() -> None:
    with pytest.raises(GateConfigError, match="Asia/Taipei"):
        assess_absolute_window(
            {
                "starts_at": "2026-07-27T09:00:00+08:00",
                "ends_at": "2026-07-27T18:00:00+08:00",
                "timezone": "UTC",
            },
        )


def test_gate_loader_fails_closed_when_single_active_rules_are_missing(tmp_path: Path) -> None:
    path = tmp_path / "gates.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "window": {"start": "02:00", "end": "04:00"},
                "automatic_no_go": [],
                "required_evidence": ["required"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GateConfigError, match="missing single-active"):
        load_gate_config(path)


def test_gate_loader_rejects_empty_required_evidence(tmp_path: Path) -> None:
    path = tmp_path / "gates.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "window": {"start": "02:00", "end": "04:00"},
                "automatic_no_go": [
                    "more_than_one_writer_or_scheduler_owner",
                    "v2_process_or_release_owner_still_active_before_v3_start",
                    "v2_port_scheduler_writer_or_model_owner_not_released",
                ],
                "required_evidence": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GateConfigError, match="non-empty"):
        load_gate_config(path)


def test_one_release_with_one_owner_per_singleton_domain_is_safe() -> None:
    assessment = assess_snapshot(
        snapshot(
            owner("v2", "scheduler", "scheduler", pid=10),
            owner("v2", "writer", "writer", pid=11),
            owner("v2", "browser", "browser", pid=12),
            owner("v2", "model", "model", pid=13),
            owner("v2", "port", "5002", pid=10),
        ),
        expected="v2",
    )
    assert assessment.go is True
    assert assessment.state == "v2_active"


def test_same_pid_seen_by_pidfile_and_launchd_is_one_owner() -> None:
    assessment = assess_snapshot(
        snapshot(
            Owner("v2", "scheduler", "pidfile", "pidfile", pid=20),
            Owner("v2", "scheduler", "launchd", "launchd", pid=20),
        ),
        expected="v2",
    )
    assert assessment.go is True
    assert assessment.domain_owners["scheduler"] == ("v2:pid:20",)


@pytest.mark.parametrize(
    "domain",
    [
        "scheduler",
        "writer",
        "browser",
        "model",
        "ingress",
        "gateway",
        "webhook",
        "discord_consumer",
        "file_watcher",
        "notification_sender",
    ],
)
def test_multiple_singleton_domain_owners_are_no_go(domain: str) -> None:
    assessment = assess_snapshot(
        snapshot(owner("v2", domain, "first", pid=30), owner("v2", domain, "second", pid=31))
    )
    assert assessment.go is False
    assert any(f"multiple {domain} owners" in reason for reason in assessment.reasons)


def test_simultaneous_v2_v3_is_no_go_even_on_different_ports() -> None:
    assessment = assess_snapshot(
        snapshot(owner("v2", "port", "5002", pid=40), owner("v3", "port", "5102", pid=41))
    )
    assert assessment.go is False
    assert assessment.state == "unsafe_mixed"
    assert any("simultaneous active releases" in reason for reason in assessment.reasons)


def test_unknown_port_listener_is_no_go() -> None:
    assessment = assess_snapshot(snapshot(owner(None, "port", "5002", pid=50, ambiguous=True)))
    assert assessment.go is False
    assert any("unclassified active owner" in reason for reason in assessment.reasons)


def test_zero_verification_rejects_any_residual_owner() -> None:
    assessment = assess_snapshot(snapshot(owner("v2", "release", "daemon", pid=60)), expected="zero")
    assert assessment.go is False
    assert any("residual owners remain after stop" in reason for reason in assessment.reasons)


def test_missing_probe_coverage_and_probe_error_are_no_go() -> None:
    assessment = assess_snapshot(
        Snapshot(coverage=frozenset({"process"}), probe_errors=("launchctl timed out",)), expected="zero"
    )
    assert assessment.go is False
    assert any("coverage missing" in reason for reason in assessment.reasons)
    assert any("launchctl timed out" in reason for reason in assessment.reasons)
