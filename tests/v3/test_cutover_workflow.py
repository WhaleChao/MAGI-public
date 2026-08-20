from __future__ import annotations

import pytest

from scripts.v3_cutover.core import CutoverError
from scripts.v3_cutover.workflow import authorize_mutation, build_workflow, simulate_workflow


@pytest.mark.parametrize("workflow", ["live-validation", "cutover", "rollback"])
def test_every_start_is_preceded_by_verified_zero_owners(workflow: str) -> None:
    steps = build_workflow(workflow)
    for index, step in enumerate(steps):
        if step.action == "start":
            assert index > 0
            assert steps[index - 1].action == "verify"
            assert steps[index - 1].expected == "zero"


def test_live_validation_has_required_stop_validate_stop_restore_sequence() -> None:
    steps = build_workflow("live-validation")
    assert [(step.action, step.release, step.expected) for step in steps] == [
        ("stop", "v2", None),
        ("verify", None, "zero"),
        ("start", "v3", None),
        ("verify", None, "v3"),
        ("stop", "v3", None),
        ("verify", None, "zero"),
        ("start", "v2", None),
        ("verify", None, "v2"),
    ]


@pytest.mark.parametrize("workflow", ["live-validation", "cutover", "rollback"])
def test_clean_simulation_completes_with_one_active_release(workflow: str) -> None:
    report = simulate_workflow(workflow)
    assert report["ok"] is True
    assert report["simulation_only"] is True
    expected = "v2_active" if workflow in {"live-validation", "rollback"} else "v3_active"
    assert report["final"]["state"] == expected


@pytest.mark.parametrize("domain", ["scheduler", "writer", "browser", "model", "port"])
def test_v2_residual_owner_stops_validation_before_v3_start(domain: str) -> None:
    report = simulate_workflow("live-validation", residual_after_stop={"v2": (domain,)})
    assert report["ok"] is False
    assert report["events"][-1]["action"] == "verify"
    assert report["events"][-1]["expected"] == "zero"
    assert all(not (event["action"] == "start" and event["release"] == "v3") for event in report["events"])


def test_v3_residual_owner_stops_rollback_before_v2_start() -> None:
    report = simulate_workflow("rollback", residual_after_stop={"v3": ("writer",)})
    assert report["ok"] is False
    assert all(not (event["action"] == "start" and event["release"] == "v2") for event in report["events"])


def test_phase_one_mutation_is_disabled_for_every_token_combination() -> None:
    token = "a" * 32
    for provided, expected in (
        (token, token),
        (None, token),
        ("a" * 31, token),
        ("b" * 32, token),
        (token, "short"),
    ):
        with pytest.raises(CutoverError, match="mutation disabled"):
            authorize_mutation(provided, expected)
