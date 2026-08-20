from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from scripts import magi_doctor


def test_menubar_ps_fallback_handles_release_path_with_spaces(tmp_path: Path) -> None:
    script = tmp_path / "Application Support" / "MAGI" / "gui" / "magi_menubar.py"
    output = (
        f"  2576 /opt/homebrew/bin/python3 {script}\n"
        "  9999 /opt/homebrew/bin/python3 /tmp/other.py\n"
    )

    assert magi_doctor._menubar_pids_from_ps_output(output, script) == [2576]


def test_menubar_ps_fallback_rejects_unrelated_command(tmp_path: Path) -> None:
    script = tmp_path / "MAGI" / "gui" / "magi_menubar.py"
    output = "  9999 /bin/sh -c echo magi_menubar.py\n"

    assert magi_doctor._menubar_pids_from_ps_output(output, script) == []


def test_project_python_accepts_hash_bound_v3_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "python"
    runtime.write_bytes(b"bound-runtime")
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(runtime))
    monkeypatch.setenv(
        "MAGI_V3_PYTHON_RUNTIME_SHA256",
        hashlib.sha256(runtime.read_bytes()).hexdigest(),
    )

    assert magi_doctor._project_python() == runtime


def test_project_python_rejects_hash_drift(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "python"
    runtime.write_bytes(b"drifted-runtime")
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(runtime))
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_SHA256", "0" * 64)

    assert magi_doctor._project_python() != runtime


def test_project_python_rejects_unbound_v3_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "python"
    runtime.write_bytes(b"unbound-runtime")
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(runtime))
    monkeypatch.delenv("MAGI_V3_PYTHON_RUNTIME_SHA256", raising=False)

    assert magi_doctor._project_python() != runtime


def test_v3_release_detection_requires_release_id_and_release_root(
    tmp_path: Path, monkeypatch
) -> None:
    live = tmp_path / "MAGI" / "releases" / "v3-test"
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-test")
    assert magi_doctor._v3_release_active(live)

    monkeypatch.delenv("MAGI_V3_RELEASE_ID")
    assert not magi_doctor._v3_release_active(live)


def test_live_runtime_root_falls_back_to_active_v3_release(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "MAGI" / "releases" / "v3-test"
    monkeypatch.delenv("MAGI_LIVE_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-test")
    monkeypatch.setenv("MAGI_ROOT_DIR", str(release))
    monkeypatch.setenv("MAGI_ROOT", str(tmp_path / "legacy-MAGI"))

    assert magi_doctor._live_runtime_root() == release


def test_live_runtime_root_ignores_stale_legacy_override_for_active_release(
    tmp_path: Path, monkeypatch
) -> None:
    active = tmp_path / "MAGI" / "releases" / "v3-current"
    stale = tmp_path / "MAGI" / "releases" / "v3-old"
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-current")
    monkeypatch.setenv("MAGI_ROOT_DIR", str(active))
    monkeypatch.setenv("MAGI_ROOT", str(active))
    monkeypatch.setenv("MAGI_LIVE_RUNTIME_ROOT", str(stale))

    assert magi_doctor._live_runtime_root() == active


def test_live_runtime_root_rejects_mismatched_release_binding(
    tmp_path: Path, monkeypatch
) -> None:
    declared = tmp_path / "MAGI" / "releases" / "v3-other"
    legacy = tmp_path / "MAGI" / "releases" / "v3-legacy"
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-current")
    monkeypatch.setenv("MAGI_ROOT_DIR", str(declared))
    monkeypatch.setenv("MAGI_ROOT", str(declared))
    monkeypatch.setenv("MAGI_LIVE_RUNTIME_ROOT", str(legacy))

    assert magi_doctor._live_runtime_root() == legacy


def test_cron_doctor_treats_partial_and_financial_replay_as_waiting(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    jobs = tmp_path / "cron_jobs.json"
    jobs.write_text(
        json.dumps(
            [
                {"id": "job_drive_case_sync_all_files", "enabled": True},
                {"id": "job_accounting_monthly_bonus", "enabled": True},
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_drive_case_sync_all_files": {
                    "last_returncode": 75,
                    "last_status": "partial",
                    "last_dispatch_at": "2026-07-29T04:00:00+00:00",
                },
                "job_accounting_monthly_bonus": {
                    "last_returncode": 1,
                    "last_status": "failed",
                    "last_error": "PermissionError while writing sealed export",
                    "last_dispatch_at": "2026-07-29T04:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    checks = magi_doctor._cron_state_checks(
        runtime_dir=runtime,
        cron_jobs_path=jobs,
    )
    failures = next(check for check in checks if check.name == "cron_state_failures")

    assert failures.status == "pass"
    assert "waiting=job_drive_case_sync_all_files,job_accounting_monthly_bonus" in failures.detail


def test_cron_doctor_treats_durable_business_retry_as_waiting(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    jobs = tmp_path / "cron_jobs.json"
    jobs.write_text(
        json.dumps([{"id": "job_business", "enabled": True}]), encoding="utf-8"
    )
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_business": {
                    "last_returncode": 1,
                    "last_status": "failed",
                    "last_success": False,
                    "last_error": "worker exited",
                    "last_dispatch_at": "2026-07-29T04:00:00+00:00",
                    "v3_retry": {"status": "queued", "attempt": 1},
                }
            }
        ),
        encoding="utf-8",
    )

    checks = magi_doctor._cron_state_checks(runtime_dir=runtime, cron_jobs_path=jobs)
    failures = next(check for check in checks if check.name == "cron_state_failures")

    assert failures.status == "pass"
    assert "waiting=job_business" in failures.detail


def test_cron_resource_governor_critical_result_is_not_execution_failure(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    jobs = tmp_path / "cron_jobs.json"
    jobs.write_text(
        json.dumps([{"id": "job_resource_governor", "enabled": True}]),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_resource_governor": {
                    "last_returncode": 2,
                    "last_status": "failed",
                    "last_success": False,
                    "last_stdout_tail": (
                        '{"ok": false, "level": "critical", '
                        '"snapshot": {"free_plus_inactive_gb": 1.77}}'
                    ),
                    "last_dispatch_at": "2026-07-29T04:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )

    checks = magi_doctor._cron_state_checks(
        runtime_dir=runtime,
        cron_jobs_path=jobs,
    )
    failures = next(check for check in checks if check.name == "cron_state_failures")
    assert failures.status == "pass"
    assert "waiting=job_resource_governor" in failures.detail


def test_cron_doctor_does_not_report_previous_failure_while_retry_is_running(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    jobs = tmp_path / "cron_jobs.json"
    jobs.write_text(
        json.dumps([{"id": "job_drive_case_sync_all_files", "enabled": True}]),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_drive_case_sync_all_files": {
                    "last_returncode": 143,
                    "last_success": False,
                    "last_status": "failed",
                    "last_dispatch_at": "2026-08-06T06:32:19+00:00",
                    "last_complete_at": "2026-08-06T06:16:06+00:00",
                    "v3_pending_occurrence": {
                        "job_id": "job_drive_case_sync_all_files",
                        "status": "running",
                        "claimed_at": "2026-08-06T06:32:19+00:00",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    checks = magi_doctor._cron_state_checks(
        runtime_dir=runtime,
        cron_jobs_path=jobs,
    )
    failures = next(check for check in checks if check.name == "cron_state_failures")

    assert failures.status == "pass"
    assert "waiting=job_drive_case_sync_all_files" in failures.detail


def test_cron_doctor_keeps_failure_after_occurrence_finishes(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    jobs = tmp_path / "cron_jobs.json"
    jobs.write_text(
        json.dumps([{"id": "job_drive_case_sync_all_files", "enabled": True}]),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_drive_case_sync_all_files": {
                    "last_returncode": 143,
                    "last_success": False,
                    "last_status": "failed",
                    "v3_pending_occurrence": {
                        "job_id": "job_drive_case_sync_all_files",
                        "status": "completed",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    checks = magi_doctor._cron_state_checks(
        runtime_dir=runtime,
        cron_jobs_path=jobs,
    )
    failures = next(check for check in checks if check.name == "cron_state_failures")

    assert failures.status == "fail"


def test_cron_doctor_treats_fresh_long_owner_as_running(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    jobs = tmp_path / "cron_jobs.json"
    jobs.write_text(
        json.dumps(
            [
                {
                    "id": "job_weekend_bookmark",
                    "enabled": True,
                    "timeout_sec": 19800,
                }
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_weekend_bookmark": {
                    "last_returncode": 143,
                    "last_success": False,
                    "last_status": "failed",
                    "last_dispatch_at": "2026-08-08T06:30:00+00:00",
                    "last_start_at": "2026-08-08T06:30:01+00:00",
                    "last_complete_at": "2026-08-08T05:44:49+00:00",
                }
            }
        ),
        encoding="utf-8",
    )

    checks = magi_doctor._cron_state_checks(
        runtime_dir=runtime,
        cron_jobs_path=jobs,
        now=magi_doctor._parse_dt("2026-08-08T06:31:00+00:00"),
    )
    failures = next(check for check in checks if check.name == "cron_state_failures")

    assert failures.status == "pass"
    assert "waiting=job_weekend_bookmark" in failures.detail


def test_cron_doctor_reports_long_owner_after_timeout(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    jobs = tmp_path / "cron_jobs.json"
    jobs.write_text(
        json.dumps(
            [
                {
                    "id": "job_weekend_bookmark",
                    "enabled": True,
                    "timeout_sec": 60,
                }
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_weekend_bookmark": {
                    "last_returncode": 143,
                    "last_success": False,
                    "last_status": "failed",
                    "last_dispatch_at": "2026-08-08T06:30:00+00:00",
                    "last_start_at": "2026-08-08T06:30:01+00:00",
                    "last_complete_at": "2026-08-08T05:44:49+00:00",
                }
            }
        ),
        encoding="utf-8",
    )

    checks = magi_doctor._cron_state_checks(
        runtime_dir=runtime,
        cron_jobs_path=jobs,
        now=magi_doctor._parse_dt("2026-08-08T06:32:02+00:00"),
    )
    failures = next(check for check in checks if check.name == "cron_state_failures")

    assert failures.status == "fail"
    assert "job_weekend_bookmark" in failures.detail


def test_doctor_accepts_only_release_bound_new_gate_for_old_omlx_failure(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    jobs = tmp_path / "cron_jobs.json"
    jobs.write_text(
        json.dumps(
            [
                {
                    "id": "job_omlx_switch_night",
                    "enabled": True,
                    "command": "bash config/bin/omlx_switch_model.sh night",
                }
            ]
        ),
        encoding="utf-8",
    )
    (runtime / "cron_state.json").write_text(
        json.dumps(
            {
                "job_omlx_switch_night": {
                    "last_returncode": 4,
                    "last_success": False,
                    "last_status": "failed",
                    "last_failure_at": "2000-01-01T00:00:00+00:00",
                    "command_sha256": "1" * 64,
                }
            }
        ),
        encoding="utf-8",
    )
    gate = runtime / "model_live_gate_latest.json"
    gate.write_text(
        json.dumps(
            {
                "ok": True,
                "expected_profile": "day",
                "active_profile": "day",
                "failures": [],
                "endpoints": [
                    {"port": 8080, "ok": True, "model_id": "gemma-4-e4b-it-4bit"}
                ],
            }
        ),
        encoding="utf-8",
    )
    marker = tmp_path / "active-release.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "magi.v3.active-release/v1",
                "release_id": "v3-new",
                "committed_at": "2000-01-02T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    os.utime(gate, (946857600.0, 946857600.0))
    monkeypatch.setenv("MAGI_V3_ACTIVE_RELEASE_MARKER", str(marker))
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "v3-new")

    checks = magi_doctor._cron_state_checks(
        runtime_dir=runtime,
        cron_jobs_path=jobs,
    )
    failures = next(check for check in checks if check.name == "cron_state_failures")
    assert failures.status == "pass"
    assert "recovered=job_omlx_switch_night" in failures.detail
