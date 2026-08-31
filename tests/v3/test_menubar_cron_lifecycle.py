from __future__ import annotations

from datetime import datetime

from gui import magi_menubar


NOW = datetime.fromisoformat("2026-08-30T06:30:00+08:00")


def _job(job_id: str = "job-test") -> dict:
    return {
        "id": job_id,
        "enabled": True,
        "cron": "*/10 * * * *",
        "desc": job_id,
        "command": "python worker.py",
    }


def _details(monkeypatch, job: dict, state: dict) -> dict:
    monkeypatch.setattr(
        magi_menubar,
        "_active_release_cron_boundary",
        lambda: (None, ""),
    )
    return magi_menubar._cron_details_from_state(
        [job],
        {job["id"]: state},
        now=NOW,
    )[0]


def test_historical_resource_deferral_is_not_current_pending(monkeypatch) -> None:
    detail = _details(
        monkeypatch,
        _job(),
        {
            "last_status": "deferred",
            "returncode": 75,
            "last_error": "resource_guard_skipped",
            "last_result_at": "2026-08-30T06:20:00+08:00",
        },
    )

    assert detail["status"] == "history"
    assert detail["historical_deferred"] is True
    summary = magi_menubar._cron_summary(1, True, [detail])
    assert summary["pending"] == 0
    assert summary["state"] == "ok"


def test_claimed_deferred_occurrence_is_current_pending(monkeypatch) -> None:
    detail = _details(
        monkeypatch,
        _job(),
        {
            "last_status": "deferred",
            "returncode": 75,
            "last_error": "resource_guard_skipped",
            "last_result_at": "2026-08-30T06:20:00+08:00",
            "v3_pending_occurrence": {"status": "queued"},
        },
    )

    assert detail["status"] == "deferred"
    assert detail["wait_reason"] == "resource"
    assert magi_menubar._cron_summary(1, True, [detail])["pending"] == 1


def test_active_retry_is_current_pending_without_pending_occurrence(monkeypatch) -> None:
    detail = _details(
        monkeypatch,
        _job(),
        {
            "last_status": "deferred",
            "returncode": 75,
            "last_result_at": "2026-08-30T06:20:00+08:00",
            "v3_retry": {"status": "running"},
        },
    )

    assert detail["status"] == "deferred"
    assert detail["wait_reason"] == "auto_repair"


def test_success_is_not_reclassified_by_historical_partial_stdout(monkeypatch) -> None:
    detail = _details(
        monkeypatch,
        _job(),
        {
            "last_status": "success",
            "last_success": True,
            "returncode": 0,
            "last_success_at": "2026-08-30T06:20:00+08:00",
            "last_stdout_tail": '{"status": "partial"}',
        },
    )

    assert detail["status"] == "ok"
    assert detail["historical_deferred"] is False


def test_review_required_candidate_remains_visible_without_retry(monkeypatch) -> None:
    job = _job("job_distill_train_gemma")
    job["command"] = "python scripts/nightly_distill_gemma.py"
    detail = _details(
        monkeypatch,
        job,
        {
            "last_status": "deferred",
            "returncode": 1,
            "last_error": "candidate_rejected",
            "last_review_required": True,
            "last_result_at": "2026-08-30T06:20:00+08:00",
        },
    )

    assert detail["status"] == "deferred"
    assert detail["wait_reason"] == "candidate_rejected"


def test_normal_queued_occurrence_is_in_pending_summary(monkeypatch) -> None:
    detail = _details(
        monkeypatch,
        _job(),
        {
            "last_status": "success",
            "last_success": True,
            "returncode": 0,
            "last_success_at": "2026-08-30T06:20:00+08:00",
            "v3_pending_occurrence": {"status": "queued"},
        },
    )

    assert detail["status"] == "queued"
    summary = magi_menubar._cron_summary(1, True, [detail])
    assert summary["pending"] == 1
    assert summary["label"] == "1個啟用・1個待續跑・運作正常"
