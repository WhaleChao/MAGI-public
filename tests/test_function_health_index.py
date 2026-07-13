from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.ops import function_health_index as index


NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    _write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def test_build_index_discovers_inventory_and_health_findings(tmp_path: Path):
    _write(
        tmp_path / "api" / "blueprints" / "demo.py",
        """
from flask import Blueprint
bp = Blueprint("demo", __name__)

@bp.route("/api/osc/cases", methods=["GET", "POST"])
def cases():
    pass

@bp.route("/health")
def health():
    pass
""",
    )
    _write(
        tmp_path / "api" / "server.py",
        """
@app.route("/telegram/webhook", methods=["POST"])
def telegram():
    pass
""",
    )
    _write(
        tmp_path / "skills" / "translator" / "SKILL.md",
        "---\nname: translator\ndescription: Translate text\n---\n",
    )
    _write(tmp_path / "skills" / "translator" / "action.py", "print('ok')\n")
    _write(tmp_path / "skills" / "action-only" / "action.py", "print('ok')\n")
    _write(tmp_path / "skills" / "doc-only" / "SKILL.md", "# Doc only\n")

    _write_json(
        tmp_path / "config" / "test_matrix.json",
        {
            "suites": {
                "ci": {
                    "checks": [
                        {
                            "id": "function_health_index",
                            "name": "Function health index",
                            "command": [
                                "{python}",
                                "scripts/ops/function_health_index.py",
                                "--json-out",
                                ".runtime/function_health_index_ci_latest.json",
                            ],
                        },
                        {
                            "id": "health",
                            "name": "Health",
                            "command": [
                                "{python}",
                                "scripts/ops/demo.py",
                                "--json-out",
                                ".runtime/missing_check_latest.json",
                            ],
                        }
                    ]
                }
            }
        },
    )
    _write_json(
        tmp_path / "cron_jobs.json",
        [
            {
                "id": "job_ok",
                "enabled": True,
                "cron": "0 * * * *",
                "command": "python scripts/ops/demo.py --json-out .runtime/job_ok_latest.json",
            },
            {
                "id": "job_missing",
                "enabled": True,
                "cron": "0 8 * * *",
                "command": "@MAGI 系統狀態",
            },
            {
                "id": "job_stale",
                "enabled": True,
                "cron": "0 8 * * *",
                "command": "python scripts/ops/stale.py",
            },
            {
                "id": "job_disabled",
                "enabled": False,
                "cron": "0 8 * * *",
                "command": "python scripts/ops/disabled.py",
            },
        ],
    )
    _write_json(
        tmp_path / ".runtime" / "cron_state.json",
        {
            "job_ok": {
                "last_dispatch_at": "2026-06-29T11:00:00+00:00",
                "last_start_at": "2026-06-29T11:00:01+00:00",
                "last_complete_at": "2026-06-29T11:00:03+00:00",
                "last_success_at": "2026-06-29T11:00:03+00:00",
                "last_success": True,
                "returncode": 0,
                "timed_out": False,
            },
            "job_stale": {
                "last_dispatch_at": "2026-06-29T08:00:00+00:00",
                "last_success_at": "2026-06-01T08:00:00+00:00",
                "last_success": True,
                "returncode": 0,
            },
        },
    )
    _write_json(tmp_path / ".runtime" / "job_ok_latest.json", {"ok": False, "generated_at": NOW.isoformat()})
    _write_json(tmp_path / ".runtime" / "old_latest.json", {"ok": True, "generated_at": "2026-06-20T00:00:00+00:00"})

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        max_health_age_hours=24,
        include_static=False,
    )

    assert report["ok"] is False
    assert report["api_routes"]["domains"]["osc"]["count"] == 1
    assert report["api_routes"]["domains"]["health"]["count"] == 1
    assert report["api_routes"]["domains"]["webhooks"]["count"] == 1
    assert report["skills"]["total"] == 3
    assert "action-only" in report["skills"]["missing_skill_md"]
    assert "doc-only" in report["skills"]["missing_action"]
    assert set(report["contracts"]["failure_taxonomy"]["GeneralError"]) >= {
        "auth_required",
        "login_failed",
        "path_missing",
        "external_service",
        "validation_failed",
        "unknown",
    }
    translator_contract = next(
        item for item in report["contracts"]["skills"] if item["name"] == "translator"
    )
    assert translator_contract["entrypoint"] == "skills/translator/action.py"
    assert translator_contract["input_hints"]
    assert translator_contract["failure_categories"]
    assert translator_contract["live_check_hint"]
    assert any(item["id"] == "job_missing" for item in report["cron_jobs"]["missing_state"])
    assert any(item["id"] == "job_stale" for item in report["cron_jobs"]["stale"])
    assert any(item["path"] == ".runtime/job_ok_latest.json" for item in report["runtime_health"]["failed"])
    assert report["summary"]["observed_stale_health_count"] == 0
    assert any(
        item["path"] == ".runtime/old_latest.json"
        for item in report["runtime_health"]["artifact_hygiene"]["archived_observed_stale"]
    )
    assert any(item["path"] == ".runtime/missing_check_latest.json" for item in report["runtime_health"]["missing"])
    assert not any(
        item["path"] == ".runtime/function_health_index_ci_latest.json"
        for item in report["runtime_health"]["expected"]
    )
    assert report["intelligence_snapshot"]["schema_version"] == 1
    assert report["intelligence_snapshot"]["summary"]["core_function_count"] >= 10


def test_matrix_only_artifacts_are_durable_acceptance_evidence(tmp_path: Path):
    _write_json(
        tmp_path / "config" / "test_matrix.json",
        {
            "suites": {
                "release": {
                    "checks": [
                        {
                            "id": "release_gate",
                            "command": [
                                "{python}",
                                "release.py",
                                "--json-out",
                                ".runtime/release_latest.json",
                            ],
                        }
                    ]
                }
            }
        },
    )
    _write_json(tmp_path / "cron_jobs.json", [])
    _write_json(
        tmp_path / ".runtime" / "release_latest.json",
        {"ok": True, "generated_at": "2026-01-01T00:00:00+00:00"},
    )

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        max_health_age_hours=24,
        include_static=False,
    )

    artifact = next(
        item for item in report["runtime_health"]["files"]
        if item["path"] == ".runtime/release_latest.json"
    )
    assert artifact["status"] == "ok"
    assert report["runtime_health"]["stale"] == []


def test_legacy_business_report_ignores_notification_only_failure(tmp_path: Path):
    path = tmp_path / ".runtime" / "business_module_live_check_latest.json"
    _write_json(
        path,
        {
            "ok": False,
            "success": False,
            "results": [
                {"name": "laf_portal_live", "ok": True},
                {"name": "file_review_self_test", "ok": True},
                {"name": "transcript_self_test", "ok": True},
                {
                    "name": "notification_delivery",
                    "ok": False,
                    "business_impact": False,
                    "error": "notification_delivery_failed",
                },
            ],
        },
    )

    result = index.evaluate_health_file(path, tmp_path, NOW, 72)

    assert result["status"] == "ok"
    assert result["contract"] == "business_impact_results"


def test_legacy_business_report_still_fails_for_business_check_failure(tmp_path: Path):
    path = tmp_path / ".runtime" / "business_module_live_check_latest.json"
    _write_json(
        path,
        {
            "ok": False,
            "results": [
                {"name": "laf_portal_live", "ok": False},
                {
                    "name": "notification_delivery",
                    "ok": False,
                    "business_impact": False,
                },
            ],
        },
    )

    result = index.evaluate_health_file(path, tmp_path, NOW, 72)

    assert result["status"] == "failed"
    assert result["contract"] == "ok"


def test_cron_health_uses_completion_not_dispatch_only(tmp_path: Path):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})
    _write_json(
        tmp_path / "cron_jobs.json",
        [
            {"id": "dispatch_only", "enabled": True, "cron": "0 * * * *", "command": "echo dispatch"},
            {"id": "failed_rc", "enabled": True, "cron": "0 * * * *", "command": "echo fail"},
            {"id": "timed_out", "enabled": True, "cron": "0 * * * *", "command": "echo timeout"},
            {"id": "success", "enabled": True, "cron": "0 * * * *", "command": "echo ok"},
        ],
    )
    _write_json(
        tmp_path / ".runtime" / "cron_state.json",
        {
            "dispatch_only": {"last_run": NOW.isoformat(), "last_dispatch_at": NOW.isoformat()},
            "failed_rc": {"last_success": False, "returncode": 2, "last_result_at": NOW.isoformat()},
            "timed_out": {"last_success": False, "returncode": None, "timed_out": True, "last_result_at": NOW.isoformat()},
            "success": {"last_success": True, "returncode": 0, "last_success_at": NOW.isoformat()},
        },
    )

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        include_static=False,
    )

    failed = {item["id"]: item["reason"] for item in report["cron_jobs"]["failed"]}
    missing = {item["id"]: item["reason"] for item in report["cron_jobs"]["missing_state"]}
    assert failed["failed_rc"] == "returncode=2"
    assert failed["timed_out"] == "timed_out=true"
    assert missing["dispatch_only"] == "missing last_success_at after dispatch"
    assert "success" not in failed
    assert "success" not in missing


def test_distill_validation_gate_rejection_is_not_ops_failure(tmp_path: Path):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})
    _write_json(
        tmp_path / "cron_jobs.json",
        [
            {
                "id": "job_distill_train_gemma",
                "enabled": True,
                "cron": "0 11 * * 0",
                "command": "python scripts/nightly_distill_gemma.py",
            }
        ],
    )
    _write_json(
        tmp_path / ".runtime" / "cron_state.json",
        {
            "job_distill_train_gemma": {
                "last_success": False,
                "returncode": 1,
                "last_result_at": NOW.isoformat(),
                "last_error": "Validation gate failed: channel_marker_leak insufficient_traditional_chinese",
            }
        },
    )

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        include_static=False,
    )

    assert report["cron_jobs"]["failed"] == []
    assert report["cron_jobs"]["missing_state"] == []
    entry = report["cron_jobs"]["entries"][0]
    assert entry["health_status"] == "blocked_by_validation_gate"


def test_operational_audit_newer_green_artifact_recovers_historic_cron_failure(tmp_path: Path):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})
    _write_json(
        tmp_path / "cron_jobs.json",
        [{"id": "job_operational_hardening_audit", "enabled": True, "cron": "50 8 * * *", "command": "python audit.py"}],
    )
    _write_json(
        tmp_path / ".runtime" / "cron_state.json",
        {
            "job_operational_hardening_audit": {
                "last_success": False,
                "returncode": 1,
                "last_failure_at": "2020-01-01T00:00:00+00:00",
            }
        },
    )
    artifact = tmp_path / ".runtime" / "operational_hardening_audit_latest.json"
    _write_json(artifact, {"cron": {"parse_failure_count": 0, "collision_count": 0}, "gmail_monitor": {"ok": True}})

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        include_static=False,
    )

    assert report["cron_jobs"]["failed"] == []
    assert report["cron_jobs"]["missing_state"] == []
    assert report["cron_jobs"]["entries"][0]["health_status"] == "recovered_by_operational_audit"


def test_recent_cron_deployment_waits_for_first_scheduled_run_without_hiding_later_failure(tmp_path: Path):
    cron_path = tmp_path / "cron_jobs.json"
    _write_json(
        cron_path,
        [
            {
                "id": "job_new_daily_check",
                "enabled": True,
                "cron": "10 3 * * *",
                "command": "python check.py --json-out .runtime/new_daily_check_latest.json",
            }
        ],
    )
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})
    _write_json(tmp_path / ".runtime" / "cron_state.json", {})
    os.utime(cron_path, (NOW.timestamp(), NOW.timestamp()))

    initializing = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        include_static=False,
    )
    overdue = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW + timedelta(hours=index._cron_stale_threshold_hours("10 3 * * *") + 1),
        include_static=False,
    )

    assert initializing["ok"] is True
    assert initializing["cron_jobs"]["entries"][0]["health_status"] == "awaiting_first_scheduled_run"
    assert initializing["summary"]["pending_initial_run_count"] == 1
    assert overdue["ok"] is False
    assert overdue["cron_jobs"]["missing_state"] == [{"id": "job_new_daily_check", "reason": "missing cron_state entry"}]


def test_disabled_cron_job_output_is_not_required_health_evidence(tmp_path: Path):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})
    _write_json(
        tmp_path / "cron_jobs.json",
        [
            {
                "id": "job_retired",
                "enabled": False,
                "cron": "10 3 * * *",
                "command": "python retired.py --json-out .runtime/retired_latest.json",
            }
        ],
    )
    _write_json(tmp_path / ".runtime" / "cron_state.json", {})

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        include_static=False,
    )

    assert report["ok"] is True
    assert not any(item["path"] == ".runtime/retired_latest.json" for item in report["runtime_health"]["expected"])


def test_cron_run_within_declared_timeout_is_not_marked_stale(tmp_path: Path):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})
    _write_json(
        tmp_path / "cron_jobs.json",
        [{"id": "job_long_running", "enabled": True, "cron": "0 22 * * *", "timeout_sec": 28800, "command": "python nightly.py"}],
    )
    _write_json(
        tmp_path / ".runtime" / "cron_state.json",
        {
            "job_long_running": {
                "last_dispatch_at": NOW.isoformat(),
                "last_complete_at": (NOW - timedelta(days=3)).isoformat(),
                "last_success_at": (NOW - timedelta(days=3)).isoformat(),
                "last_success": True,
                "returncode": 0,
            }
        },
    )

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW + timedelta(minutes=5),
        include_static=False,
    )

    assert report["ok"] is True
    assert report["cron_jobs"]["stale"] == []
    assert report["cron_jobs"]["entries"][0]["health_status"] == "running_within_timeout"
    assert report["summary"]["running_cron_job_count"] == 1


def test_parse_dt_interprets_naive_scheduler_timestamp_as_local_time(monkeypatch):
    local_tz = timezone(timedelta(hours=8))

    class LocalNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 10, 8, 0, tzinfo=local_tz)

    monkeypatch.setattr(index, "datetime", LocalNow)

    assert index._parse_dt("2026-07-10T08:50:00") == datetime(2026, 7, 10, 0, 50, tzinfo=timezone.utc)


def test_skipped_runtime_artifact_is_not_ok(tmp_path: Path):
    path = tmp_path / ".runtime" / "optional_health_latest.json"
    _write_json(path, {"status": "skipped", "generated_at": NOW.isoformat(), "skipped": True})

    result = index.evaluate_health_file(path, tmp_path, NOW, max_age_hours=24)

    assert result["status"] == "skipped"
    assert result["ok"] is False
    assert result["contract"] == "skipped"


def test_expected_health_ignores_manual_acceptance_suite_outputs(tmp_path: Path):
    matrix = {
        "suites": {
            "acceptance-live": {
                "checks": [
                    {
                        "id": "live",
                        "command": ["python", "gate.py", "--json-out", ".runtime/magi_acceptance_live_latest.json"],
                    }
                ]
            },
            "ci": {
                "checks": [
                    {
                        "id": "ci",
                        "command": ["python", "check.py", "--json-out", ".runtime/ci_latest.json"],
                    }
                ]
            },
        }
    }

    expected = index._expected_health_paths_from_matrix(matrix, tmp_path, tmp_path / ".runtime")

    paths = {item["path"] for item in expected}
    assert ".runtime/ci_latest.json" in paths
    assert ".runtime/magi_acceptance_live_latest.json" not in paths


def test_function_health_does_not_fail_on_its_own_previous_cron_result(tmp_path: Path):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})
    _write_json(
        tmp_path / "cron_jobs.json",
        [
            {
                "id": "job_function_health_index",
                "enabled": True,
                "cron": "10 6 * * *",
                "command": "python scripts/ops/function_health_index.py --json-out .runtime/function_health_index_latest.json",
            }
        ],
    )
    _write_json(
        tmp_path / ".runtime" / "cron_state.json",
        {"job_function_health_index": {"last_success": False, "returncode": 1, "last_run": NOW.isoformat()}},
    )
    _write_json(tmp_path / ".runtime" / "function_health_index_latest.json", {"ok": False})

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        include_static=False,
    )

    paths = {item["path"] for item in report["runtime_health"]["failed"]}
    assert "cron:job_function_health_index" not in paths
    assert ".runtime/function_health_index_latest.json" not in paths


def test_cli_writes_json_and_does_not_fail_without_fail_on_health(tmp_path: Path, capsys):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})

    out = tmp_path / ".runtime" / "function_health_index_latest.json"
    rc = index.main([
        "--root",
        str(tmp_path),
        "--json-out",
        str(out),
        "--no-static",
        "--compact",
    ])

    assert rc == 0
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["test_suite_count"] == 1
    assert payload["summary"]["missing_health_count"] >= 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["runtime_dir"] == ".runtime"


def test_intelligence_snapshot_schema_uses_unit_live_and_token_evidence(tmp_path: Path):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})
    _write(tmp_path / "tests" / "test_osc_events_refresh.py", "def test_calendar(): pass\n")
    _write(tmp_path / "tests" / "test_function_health_index.py", "def test_health(): pass\n")
    _write_json(
        tmp_path / ".runtime" / "business_module_live_check_latest.json",
        {
            "ok": True,
            "generated_at": NOW.isoformat(),
            "results": [
                {"name": "calendar_todo_status_live", "ok": True, "message": "calendar ready"},
                {"name": "nas_mounts_live", "ok": True},
            ],
        },
    )
    _write_json(
        tmp_path / ".runtime" / "token_health" / "token_health_latest.json",
        {
            "ok": True,
            "generated_at": NOW.isoformat(),
            "checks": [
                {"name": "google_calendar", "kind": "google_oauth", "ok": True, "status": "ok"},
                {"name": "google_drive_sync_readonly", "kind": "google_oauth", "ok": True, "status": "ok"},
                {"name": "google_drive_sync_write", "kind": "google_oauth", "ok": True, "status": "ok"},
            ],
        },
    )

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        include_static=False,
    )

    snapshot = report["intelligence_snapshot"]
    assert snapshot["schema_version"] == 1
    assert snapshot["summary"]["core_function_count"] == len(index.CORE_FUNCTION_CONTRACTS)
    for feature in snapshot["core_functions"]:
        assert {"last_unit_test", "last_live_check", "token_status_hint", "status", "manual_section_hint"} <= set(feature)
        assert feature["manual_section_hint"]

    calendar = next(item for item in snapshot["core_functions"] if item["id"] == "calendar_todos")
    assert calendar["last_unit_test"]["status"] == "covered"
    assert calendar["last_live_check"]["status"] == "ok"
    assert calendar["last_live_check"]["check_id"] == "calendar_todo_status_live"
    assert calendar["token_status_hint"]["status"] == "ok"
    assert calendar["status"] == "verified_live"


def test_cli_writes_standalone_intelligence_snapshot(tmp_path: Path, capsys):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})
    out = tmp_path / ".runtime" / "function_health_index_latest.json"
    snapshot_out = tmp_path / ".runtime" / "magi_health_intelligence_snapshot_latest.json"

    rc = index.main([
        "--root",
        str(tmp_path),
        "--json-out",
        str(out),
        "--snapshot-out",
        str(snapshot_out),
        "--no-static",
        "--compact",
    ])

    assert rc == 0
    assert snapshot_out.exists()
    payload = json.loads(snapshot_out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["core_functions"][0]["last_unit_test"]
    assert json.loads(capsys.readouterr().out)["intelligence_snapshot"]["schema_version"] == 1


def test_fail_on_health_returns_nonzero_for_missing_health(tmp_path: Path):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})

    rc = index.main([
        "--root",
        str(tmp_path),
        "--no-static",
        "--compact",
        "--fail-on-health",
    ])

    assert rc == 1


def test_contract_summary_contains_repo_core_skill_and_tool_contracts(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    report = index.build_index(
        root=repo_root,
        matrix_path=repo_root / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        max_health_age_hours=0,
        include_static=False,
    )

    skill_names = {item["name"] for item in report["contracts"]["skills"]}
    direct_names = {item["name"] for item in report["contracts"]["direct_handlers"]}
    api_tool_names = {item["name"] for item in report["contracts"]["api_tools"]}

    assert {
        "laf-orchestrator",
        "file-review-orchestrator",
        "transcript-downloader",
        "pdf-namer",
        "osc-orchestrator",
    }.issubset(skill_names)
    assert "web_search" in direct_names
    assert {"search", "research", "fetch"}.issubset(api_tool_names)
    assert report["contracts"]["summary"]["missing_core_contracts"] == []

    core_contracts = [
        item for item in report["contracts"]["skills"] if item["name"] in {
            "laf-orchestrator",
            "file-review-orchestrator",
            "transcript-downloader",
            "pdf-namer",
            "osc-orchestrator",
        }
    ]
    core_contracts += [item for item in report["contracts"]["direct_handlers"] if item["name"] == "web_search"]
    core_contracts += [item for item in report["contracts"]["api_tools"] if item["name"] in {"search", "research", "fetch"}]

    for contract in core_contracts:
        assert contract["name"]
        assert contract["entrypoint"]
        assert contract["input_hints"]
        assert contract["failure_categories"]
        assert contract["live_check_hint"]
