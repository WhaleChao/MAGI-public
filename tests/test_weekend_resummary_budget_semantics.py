from __future__ import annotations

import json
import sys

from scripts import weekend_resummary
from skills.ops.cron_result_policy import (
    classify_cron_result,
    terminal_schedule_deferral_reason,
)


def test_nim_daily_budget_stops_immediately_as_checkpointed_deferral() -> None:
    assert weekend_resummary._nim_daily_budget_exhausted(
        "provider:nim_daily_budget_exceeded:500/500"
    )
    assert weekend_resummary._nim_daily_budget_exhausted(
        "provider:nim_background_budget_reserved:475/475;daily=500"
    )
    assert not weekend_resummary._nim_daily_budget_exhausted("provider timeout")

    payload = weekend_resummary._budget_deferred_result(processed=117, total=300)
    rendered = json.dumps(payload, ensure_ascii=False)
    classified = classify_cron_result(0, rendered, "")

    assert payload["checkpoint_saved"] is True
    assert payload["processed_this_run"] == 117
    assert payload["next_action"] == "continue_on_next_scheduled_run"
    assert classified.success is False
    assert classified.status == "deferred"
    assert classified.error == "nim_daily_budget_exhausted"
    assert terminal_schedule_deferral_reason(rendered) == "nim_daily_budget_exhausted"


def test_budget_deferral_does_not_hide_a_real_process_failure() -> None:
    payload = weekend_resummary._budget_deferred_result(processed=1, total=2)
    classified = classify_cron_result(2, json.dumps(payload), "fatal provider error")

    assert classified.success is False
    assert classified.status == "failed"


def test_quality_failure_cannot_be_published_as_batch_success() -> None:
    payload = weekend_resummary._quality_partial_result(
        succeeded=0,
        failed=3,
        total=3,
    )
    classified = classify_cron_result(0, json.dumps(payload), "")

    assert payload["checkpoint_saved"] is True
    assert payload["success"] is False
    assert payload["retryable"] is True
    assert payload["failed_this_run"] == 3
    assert classified.success is False
    assert classified.status in {"partial_retry_pending", "failed"}


def test_legacy_zero_exit_budget_marker_is_reconcilable_without_deleting_evidence() -> None:
    legacy_error = (
        "117/300 FAIL: provider:nim_daily_budget_exceeded:500/500\n"
        "週末 NIM 重摘要完成"
    )

    assert terminal_schedule_deferral_reason("", legacy_error) == (
        "nim_daily_budget_exhausted"
    )


def test_utf8_byte_large_but_character_short_sources_are_terminal(monkeypatch, tmp_path) -> None:
    normalized = tmp_path / "normalized"
    for index in range(3):
        folder = normalized / f"d{index}"
        folder.mkdir(parents=True)
        # 999 CJK characters exceed 1,000 bytes but remain below the 1,000
        # character quality floor.
        (folder / f"case-{index}.txt").write_text("判" * 999, encoding="utf-8")

    state_path = tmp_path / "resummary_state.json"
    monkeypatch.setattr(weekend_resummary, "NORM_ROOT", normalized)
    monkeypatch.setattr(weekend_resummary, "STATE_PATH", state_path)
    monkeypatch.setattr(weekend_resummary, "_acquire_lock", lambda: True)
    monkeypatch.setattr(weekend_resummary, "_release_lock", lambda: None)
    monkeypatch.setattr(weekend_resummary, "_notify_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(weekend_resummary, "_record_scheduler_success", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(weekend_resummary, "_load_judgments_reasons", lambda: {})

    def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("short immutable source must not call the provider")

    monkeypatch.setattr(weekend_resummary, "_source_bound_summarize", forbidden_provider)
    monkeypatch.setattr(
        sys,
        "argv",
        ["weekend_resummary", "--limit", "3", "--delay", "0"],
    )

    assert weekend_resummary.main() == 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["nim_done"]) == 3
    assert all(
        row["reviewed_no_insight"] is True
        and row["reason"] == "reviewed:source_too_short"
        for row in state["nim_done"].values()
    )
    stats = next(iter(state["stats"].values()))
    assert stats["total"] == 3
    assert stats["success"] == 3
    assert stats["fail"] == 0
