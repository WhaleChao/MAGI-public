from __future__ import annotations

from scripts.v3_validation.g8_maintenance_safety import (
    V2_RESTORE_ENDPOINTS,
    eligible_v2_process_groups,
    parse_ps_rows,
    reverify_group,
)


ROOT = "/sealed/runtime/MAGI_v2"


def _rows(text: str):
    return parse_ps_rows(text)


def _policy(**changes):
    policy = {
        "runtime_marker": ROOT,
        "verified_v2_markers": ("com.magi.daemon",),
        "caller_pid": 100,
        "caller_pgid": 100,
        "caller_session": 10,
        "protected_pids": (),
    }
    policy.update(changes)
    return policy


def test_weekend_resummary_in_callers_session_is_never_selected() -> None:
    rows = _rows(
        "100 1 100 10 /bin/zsh g8-wrapper\n"
        f"201 1 201 10 {ROOT}/venv/bin/python skills/weekend_resummary/action.py\n"
    )
    assert eligible_v2_process_groups(rows, **_policy()) == ()


def test_independent_verified_v2_group_is_selected_and_reverified() -> None:
    rows = _rows(
        "100 1 100 10 /bin/zsh g8-wrapper\n"
        f"201 1 201 22 {ROOT}/venv/bin/python api/server.py\n"
        f"202 201 201 22 {ROOT}/venv/bin/python worker.py\n"
    )
    assert eligible_v2_process_groups(rows, **_policy()) == (201,)
    assert reverify_group(rows, 201, **_policy()) is True


def test_nonleader_or_unverified_group_is_not_selected() -> None:
    rows = _rows(
        "100 1 100 10 /bin/zsh g8-wrapper\n"
        f"201 1 299 22 {ROOT}/venv/bin/python api/server.py\n"
        "299 1 299 22 /usr/bin/unrelated-service\n"
    )
    assert eligible_v2_process_groups(rows, **_policy()) == ()


def test_restore_contract_includes_share_gateway_5014() -> None:
    assert V2_RESTORE_ENDPOINTS == (
        (5002, "/readyz"),
        (5003, "/health"),
        (5014, "/health"),
        (8088, "/health"),
    )
