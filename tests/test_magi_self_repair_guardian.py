from __future__ import annotations

import os
import time
from pathlib import Path

from scripts.ops import magi_self_repair_guardian as guardian


def test_guardian_does_not_feed_back_its_previous_artifact():
    issues = guardian._collect_health_issues(
        {
            "runtime_health": {
                "failed": [
                    {
                        "path": ".runtime/magi_self_repair_guardian_latest.json",
                        "reason": "ok=false",
                    },
                    {"path": ".runtime/other_latest.json", "reason": "ok=false"},
                ]
            }
        }
    )

    assert [issue["evidence"]["path"] for issue in issues] == [".runtime/other_latest.json"]


def test_guardian_keeps_observed_health_failures_informational():
    issues = guardian._collect_health_issues(
        {
            "runtime_health": {
                "observed_failed": [
                    {
                        "path": ".runtime/manual_probe_latest.json",
                        "reason": "ok=false",
                    }
                ]
            }
        }
    )

    assert len(issues) == 1
    assert issues[0]["severity"] == "info"
    assert guardian._open_issue(issues[0]) is False


def test_guardian_ignores_only_its_own_previous_cron_failure():
    own = guardian._collect_doctor_issues(
        {"checks": [{"name": "cron_state_failures", "status": "fail", "detail": "job_magi_self_repair_guardian:success=False rc=1"}]}
    )
    mixed = guardian._collect_doctor_issues(
        {"checks": [{"name": "cron_state_failures", "status": "fail", "detail": "job_magi_self_repair_guardian:success=False; job_real_work:success=False"}]}
    )

    assert own == []
    assert [issue["id"] for issue in mixed] == ["doctor:cron_state_failures"]


def test_guardian_observes_unowned_tmp_residue_without_failing_or_deleting(tmp_path: Path):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    residue = tmp_dir / "magi_old_report.json"
    residue.write_text("{}", encoding="utf-8")
    old_ts = time.time() - 3600
    os.utime(residue, (old_ts, old_ts))

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="audit",
        include_doctor=False,
        include_function_health=False,
        tmp_dir=tmp_dir,
        tmp_min_age_minutes=1,
    )

    assert report["ok"] is True
    assert residue.exists()
    assert report["summary"]["safe_auto_repair_available_count"] == 0
    assert report["summary"]["human_required_count"] == 0
    assert report["issues"][0]["id"] == "tmp:magi_unowned_observed"
    assert report["issues"][0]["severity"] == "info"
    assert report["actions"] == []


def test_guardian_repair_safe_deletes_only_stale_magi_tmp_artifacts(tmp_path: Path):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    residue = tmp_dir / "magi_old_report.json"
    residue.write_text("old", encoding="utf-8")
    marker = residue.with_name(residue.name + guardian._TMP_OWNERSHIP_SENTINEL)
    marker.write_text(guardian._TMP_OWNERSHIP_TEXT, encoding="utf-8")
    keep = tmp_dir / "not_magi_old_dir"
    keep.mkdir()
    old_ts = time.time() - 3600
    os.utime(residue, (old_ts, old_ts))
    os.utime(keep, (old_ts, old_ts))

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="repair-safe",
        include_doctor=False,
        include_function_health=False,
        tmp_dir=tmp_dir,
        tmp_min_age_minutes=1,
    )

    assert report["ok"] is True
    assert not residue.exists()
    assert not marker.exists()
    assert keep.exists()
    assert report["summary"]["applied_action_count"] == 1
    assert report["verifications"][0]["ok"] is True


def test_guardian_repair_safe_processes_every_eligible_candidate(tmp_path: Path):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    old_ts = time.time() - 3600
    residues = []
    for index in range(55):
        residue = tmp_dir / f"magi_old_{index}.json"
        residue.write_text("old", encoding="utf-8")
        marker = residue.with_name(residue.name + guardian._TMP_OWNERSHIP_SENTINEL)
        marker.write_text(guardian._TMP_OWNERSHIP_TEXT, encoding="utf-8")
        os.utime(residue, (old_ts, old_ts))
        residues.append(residue)

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="repair-safe",
        include_doctor=False,
        include_function_health=False,
        tmp_dir=tmp_dir,
        tmp_min_age_minutes=1,
    )

    assert report["ok"] is True
    assert all(not residue.exists() for residue in residues)
    action = next(action for action in report["actions"] if action["kind"] == "safe_cleanup")
    assert len(action["results"]) == 55
    assert report["summary"]["partial_action_count"] == 0


def test_guardian_repair_safe_never_deletes_unowned_directories(tmp_path: Path):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    residue_dir = tmp_dir / "magi_old_dir"
    residue_dir.mkdir()
    (residue_dir / "payload.txt").write_text("old", encoding="utf-8")
    old_ts = time.time() - 3600
    os.utime(residue_dir, (old_ts, old_ts))

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="repair-safe",
        include_doctor=False,
        include_function_health=False,
        tmp_dir=tmp_dir,
        tmp_min_age_minutes=1,
    )

    assert residue_dir.exists()
    assert report["ok"] is True
    assert report["summary"]["applied_action_count"] == 0
    assert report["requires_human"] == []
    assert report["issues"][0]["id"] == "tmp:magi_unowned_observed"


def test_guardian_requires_human_for_laf_upload_staging_without_sentinel(tmp_path: Path):
    tmp_dir = tmp_path / "tmp"
    run_dir = tmp_dir / "magi_laf_upload_pdf" / "20260710_120000_1130919-T-057_closing"
    run_dir.mkdir(parents=True)
    (run_dir / "upload.pdf").write_text("staging", encoding="utf-8")
    recent_ts = time.time() - 24 * 3600
    os.utime(run_dir, (recent_ts, recent_ts))

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="audit",
        include_doctor=False,
        include_function_health=False,
        tmp_dir=tmp_dir,
        tmp_min_age_minutes=1,
    )

    assert report["ok"] is False
    assert run_dir.exists()
    assert report["requires_human"][0]["id"] == "tmp:magi_unowned_residue"
    assert "sentinel" in report["requires_human"][0]["evidence"]["candidates"][0]["reason"]


def test_guardian_repair_safe_never_removes_expired_laf_upload_staging_without_sentinel(tmp_path: Path):
    tmp_dir = tmp_path / "tmp"
    run_dir = tmp_dir / "magi_laf_upload_pdf" / "20260601_120000_1130919-T-057_closing"
    run_dir.mkdir(parents=True)
    (run_dir / "upload.pdf").write_text("staging", encoding="utf-8")
    expired_ts = time.time() - 15 * 24 * 3600
    os.utime(run_dir, (expired_ts, expired_ts))

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="repair-safe",
        include_doctor=False,
        include_function_health=False,
        tmp_dir=tmp_dir,
        tmp_min_age_minutes=1,
    )

    assert report["ok"] is False
    assert run_dir.exists()
    assert report["summary"]["applied_action_count"] == 0


def test_guardian_repair_safe_removes_expired_owned_laf_upload_staging(tmp_path: Path):
    tmp_dir = tmp_path / "tmp"
    run_dir = tmp_dir / "magi_laf_upload_pdf" / "20260601_120000_1130919-T-057_closing"
    run_dir.mkdir(parents=True)
    (run_dir / guardian._TMP_OWNERSHIP_SENTINEL).write_text(
        guardian._TMP_OWNERSHIP_TEXT,
        encoding="utf-8",
    )
    (run_dir / "upload.pdf").write_text("staging", encoding="utf-8")
    expired_ts = time.time() - 15 * 24 * 3600
    os.utime(run_dir, (expired_ts, expired_ts))

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="repair-safe",
        include_doctor=False,
        include_function_health=False,
        tmp_dir=tmp_dir,
        tmp_min_age_minutes=1,
    )

    assert report["ok"] is True
    assert not run_dir.exists()


def test_guardian_verification_failure_makes_report_not_ok(tmp_path: Path, monkeypatch):
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    residue = tmp_dir / "magi_stuck.json"
    residue.write_text("old", encoding="utf-8")
    marker = residue.with_name(residue.name + guardian._TMP_OWNERSHIP_SENTINEL)
    marker.write_text(guardian._TMP_OWNERSHIP_TEXT, encoding="utf-8")
    old_ts = time.time() - 3600
    os.utime(residue, (old_ts, old_ts))

    original_candidates = guardian._tmp_candidates
    calls = 0

    def fake_candidates(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_candidates(**kwargs)
        return [{"path": str(residue), "eligible": True}]

    def fake_apply(*, issues, tmp_dir):
        issues[0]["status"] = "resolved"
        return [{"id": "delete_stale_tmp_magi_artifacts", "kind": "safe_cleanup", "status": "applied"}]

    monkeypatch.setattr(guardian, "_tmp_candidates", fake_candidates)
    monkeypatch.setattr(guardian, "_apply_safe_repairs", fake_apply)

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="repair-safe",
        include_doctor=False,
        include_function_health=False,
        tmp_dir=tmp_dir,
        tmp_min_age_minutes=1,
    )

    assert report["ok"] is False
    assert report["summary"]["verification_failed_count"] == 1


def test_guardian_function_health_failures_require_human(tmp_path: Path, monkeypatch):
    def fake_health_report(**kwargs):
        return {
            "ok": False,
            "summary": {
                "failed_health_count": 1,
                "stale_health_count": 0,
                "missing_health_count": 0,
            },
            "runtime_health": {
                "failed": [
                    {
                        "path": ".runtime/business_module_live_check_latest.json",
                        "reason": "ok=false",
                        "contract": "json_ok",
                    }
                ],
                "missing": [],
                "stale": [],
                "observed_failed": [],
                "observed_stale": [],
            },
        }

    monkeypatch.setattr(guardian, "_run_function_health_report", fake_health_report)

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="repair-safe",
        include_doctor=False,
        include_function_health=True,
        tmp_dir=tmp_path / "tmp",
    )

    assert report["ok"] is False
    assert report["summary"]["human_required_count"] == 1
    assert report["requires_human"][0]["id"].startswith("function_health:failed:")
    assert all(action["kind"] != "safe_cleanup" for action in report["actions"])


def test_guardian_doctor_warning_requires_human(tmp_path: Path, monkeypatch):
    def fake_doctor_report(**kwargs):
        return {
            "ok": True,
            "status": "warn",
            "summary": {"pass": 1, "warn": 1, "fail": 0},
            "checks": [
                {
                    "name": "launchagents",
                    "status": "warn",
                    "detail": "no MAGI LaunchAgent plist found",
                    "fix": "Confirm launchd install.",
                }
            ],
        }

    monkeypatch.setattr(guardian, "_run_doctor_report", fake_doctor_report)

    report = guardian.build_report(
        root=tmp_path,
        runtime_dir=tmp_path / ".runtime",
        mode="audit",
        include_doctor=True,
        include_function_health=False,
        tmp_dir=tmp_path / "tmp",
    )

    assert report["ok"] is False
    assert report["summary"]["human_required_count"] == 1
    assert report["requires_human"][0]["id"] == "doctor:launchagents"
