from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.v3_validation.__main__ import main as validation_main
from scripts.v3_validation.live_validation import (
    load_live_plan,
    load_live_report,
    plan_sha256,
    validate_live_campaign_reports,
    validate_live_plan,
    validate_live_report,
    validate_live_report_against_plan,
)
from scripts.v3_validation.paths import ROUTE_METHOD_REVIEW_PATH
from scripts.v3_validation.schema import ContractValidationError, load_json


LIVE_DIR = Path(__file__).parent / "compat" / "live"


def test_safe_plan_is_validated_but_not_executed() -> None:
    plan = load_live_plan(LIVE_DIR / "isolated-plan.json")
    summary = validate_live_plan(plan)
    assert summary == {
        "ok": True,
        "plan_id": "v3-isolated-smoke-v1",
        "checks": 2,
        "live_probes": 1,
        "offline_only": 1,
        "reviewed_checks": 2,
        "plan_sha256": "82acad31b82626ef6beedbfe7a8cdc3867098328affe639d0dd39f32498a0543",
    }


def test_live_probe_with_write_side_effect_is_rejected() -> None:
    plan = load_json(LIVE_DIR / "isolated-plan.json")
    plan["checks"][1]["execution_mode"] = "live_probe"
    with pytest.raises(ContractValidationError):
        validate_live_plan(plan)


def test_live_probe_cannot_relabel_reviewed_webhook_post_as_read_only() -> None:
    plan = load_json(LIVE_DIR / "isolated-plan.json")
    plan["checks"] = [
        {
            "check_id": "mislabeled-webhook",
            "route": {
                "service": "5002",
                "rule": "/line/webhook",
                "method": "POST",
                "endpoint": "callback",
            },
            "side_effect_class": "read_only",
            "execution_mode": "live_probe",
            "timeout_sec": 3,
            "assertions": ["must not execute"],
        }
    ]
    with pytest.raises(ContractValidationError, match="side-effect mismatch"):
        validate_live_plan(plan)


def test_supplementally_reviewed_route_method_cannot_claim_read_only() -> None:
    plan = load_json(LIVE_DIR / "isolated-plan.json")
    # The primary review file preserves its historical review gap, while the
    # default loader merges the audited supplement that completes 431/431.
    # The LIVE plan must therefore enforce the merged external-commit class;
    # treating this row as still unreviewed would test a state that no longer
    # exists in the executable review set.
    supplementally_reviewed = next(
        row
        for row in load_json(ROUTE_METHOD_REVIEW_PATH)["unreviewed"]
        if row["service"] == "5003"
    )
    plan["checks"] = [
        {
            "check_id": "unreviewed-live-validation",
            "route": {
                "service": supplementally_reviewed["service"],
                "rule": supplementally_reviewed["rule"],
                "method": supplementally_reviewed["method"],
                "endpoint": supplementally_reviewed["endpoint"],
            },
            "side_effect_class": "read_only",
            "execution_mode": "live_probe",
            "timeout_sec": 3,
            "assertions": ["must have explicit review"],
        }
    ]
    with pytest.raises(ContractValidationError, match="side-effect mismatch"):
        validate_live_plan(plan)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("ports", "main"), 5002),
        (("production_db_allowed",), True),
        (("channel_delivery_allowed",), True),
        (("writers_enabled",), True),
    ],
)
def test_plan_rejects_production_ports_and_external_write_flags(path: tuple[str, ...], value: object) -> None:
    plan = load_json(LIVE_DIR / "isolated-plan.json")
    target = plan["environment"]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ContractValidationError):
        validate_live_plan(plan)


def test_incomplete_report_explicitly_records_unproven_gaps() -> None:
    plan = load_live_plan(LIVE_DIR / "isolated-plan.json")
    report = load_live_report(LIVE_DIR / "isolated-report.json")
    result = validate_live_report_against_plan(report, plan)
    assert result["status"] == "incomplete"
    assert result["schema_valid"] is True
    assert result["evidence_passed"] is False
    assert result["summary"] == {"total": 2, "passed": 0, "failed": 0, "blocked": 1, "skipped": 1}
    assert report["production_touched"] is False
    assert report["unproven_gaps"]


def test_report_rejects_summary_mismatch() -> None:
    report = load_json(LIVE_DIR / "isolated-report.json")
    report["summary"]["passed"] = 1
    with pytest.raises(ContractValidationError, match="summary mismatch"):
        validate_live_report(report)


def test_passed_report_cannot_hide_unproven_or_nonpassing_checks() -> None:
    report = copy.deepcopy(load_json(LIVE_DIR / "isolated-report.json"))
    report["status"] = "passed"
    with pytest.raises(ContractValidationError):
        validate_live_report(report)


def _passed_report() -> tuple[dict, dict]:
    plan = load_json(LIVE_DIR / "isolated-plan.json")
    report = copy.deepcopy(load_json(LIVE_DIR / "isolated-report.json"))
    report["status"] = "passed"
    report["plan_sha256"] = plan_sha256(plan)
    report["summary"] = {"total": 2, "passed": 2, "failed": 0, "blocked": 0, "skipped": 0}
    report["checks"] = [
        {
            "check_id": row["check_id"],
            "status": "passed",
            "duration_ms": 1,
            "evidence": [f"verified:{row['check_id']}"],
            "error": None,
        }
        for row in plan["checks"]
    ]
    report["unproven_gaps"] = []
    return plan, report


def test_passed_report_requires_and_accepts_exact_plan_binding() -> None:
    plan, report = _passed_report()
    result = validate_live_report_against_plan(report, plan)
    assert result["schema_valid"] is True
    assert result["evidence_passed"] is True


def test_three_reset_separated_live_reports_complete_within_one_window() -> None:
    plan, template = _passed_report()
    reports = []
    for index, (started, finished) in enumerate(
        (
            ("2026-07-22T08:11:00+08:00", "2026-07-22T08:21:00+08:00"),
            ("2026-07-22T08:31:00+08:00", "2026-07-22T08:41:00+08:00"),
            ("2026-07-22T08:51:00+08:00", "2026-07-22T09:01:00+08:00"),
        ),
        start=1,
    ):
        report = copy.deepcopy(template)
        report["report_id"] = f"isolated-live-pass-{index}"
        report["started_at"] = started
        report["finished_at"] = finished
        reports.append(report)
    policy = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "v3_validation_campaign.json").read_text()
    )["isolated_live_validation"]

    result = validate_live_campaign_reports(reports, plan, policy)

    assert result["evidence_passed"] is True
    assert result["completed_runs"] == 3
    assert result["minimum_reset_minutes"] == 10
    assert result["allowed_local_dates"] == ["2026-07-22"]


def test_live_campaign_rejects_report_outside_release_bound_date() -> None:
    plan, template = _passed_report()
    policy = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "v3_validation_campaign.json").read_text()
    )["isolated_live_validation"]
    reports = []
    for index, (started, finished) in enumerate(
        (
            ("2026-07-21T08:11:00+08:00", "2026-07-21T08:21:00+08:00"),
            ("2026-07-21T08:31:00+08:00", "2026-07-21T08:41:00+08:00"),
            ("2026-07-21T08:51:00+08:00", "2026-07-21T09:01:00+08:00"),
        ),
        start=1,
    ):
        report = copy.deepcopy(template)
        report["report_id"] = f"isolated-live-wrong-date-{index}"
        report["started_at"] = started
        report["finished_at"] = finished
        reports.append(report)
    with pytest.raises(ContractValidationError, match="release-bound allowed window"):
        validate_live_campaign_reports(reports, plan, policy)


def test_live_campaign_rejects_missing_run_and_short_reset() -> None:
    plan, template = _passed_report()
    policy = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "v3_validation_campaign.json").read_text()
    )["isolated_live_validation"]
    with pytest.raises(ContractValidationError, match="exactly 3"):
        validate_live_campaign_reports([template], plan, policy)

    reports = []
    for index, (started, finished) in enumerate(
        (
            ("2026-07-22T08:11:00+08:00", "2026-07-22T08:21:00+08:00"),
            ("2026-07-22T08:25:00+08:00", "2026-07-22T08:35:00+08:00"),
            ("2026-07-22T08:45:00+08:00", "2026-07-22T08:55:00+08:00"),
        ),
        start=1,
    ):
        report = copy.deepcopy(template)
        report["report_id"] = f"isolated-live-short-reset-{index}"
        report["started_at"] = started
        report["finished_at"] = finished
        reports.append(report)
    with pytest.raises(ContractValidationError, match="reset interval"):
        validate_live_campaign_reports(reports, plan, policy)


def test_passed_report_rejects_empty_checks_and_evidence() -> None:
    plan, report = _passed_report()
    report["checks"] = []
    report["summary"] = {"total": 0, "passed": 0, "failed": 0, "blocked": 0, "skipped": 0}
    with pytest.raises(ContractValidationError):
        validate_live_report_against_plan(report, plan)

    _, report = _passed_report()
    report["checks"][0]["error"] = "hidden error"
    with pytest.raises(ContractValidationError, match="null error"):
        validate_live_report_against_plan(report, plan)

    _, report = _passed_report()
    report["checks"][0]["evidence"] = []
    with pytest.raises(ContractValidationError):
        validate_live_report_against_plan(report, plan)


@pytest.mark.parametrize("field", ["plan_id", "plan_sha256"])
def test_report_rejects_wrong_plan_binding(field: str) -> None:
    plan, report = _passed_report()
    report[field] = "f" * 64 if field == "plan_sha256" else "different-plan"
    with pytest.raises(ContractValidationError, match=field):
        validate_live_report_against_plan(report, plan)


def test_report_rejects_missing_or_extra_check_ids_and_reversed_time() -> None:
    plan, report = _passed_report()
    report["checks"][0]["check_id"] = "invented-check"
    with pytest.raises(ContractValidationError, match="check IDs"):
        validate_live_report_against_plan(report, plan)

    _, report = _passed_report()
    report["finished_at"] = "2026-07-14T02:10:59Z"
    report["started_at"] = "2026-07-14T02:11:00Z"
    with pytest.raises(ContractValidationError, match="finished_at"):
        validate_live_report_against_plan(report, plan)


def test_cli_incomplete_report_is_schema_valid_but_nonzero(capsys) -> None:
    code = validation_main(
        ["report", str(LIVE_DIR / "isolated-report.json"), "--plan", str(LIVE_DIR / "isolated-plan.json")]
    )
    output = capsys.readouterr().out
    assert code == 2
    assert '"schema_valid": true' in output
    assert '"evidence_passed": false' in output


def test_cli_failed_report_is_nonzero_and_joint_passed_report_is_zero(tmp_path: Path, capsys) -> None:
    _, passed = _passed_report()
    passed_path = tmp_path / "passed.json"
    passed_path.write_text(json.dumps(passed), encoding="utf-8")
    code = validation_main(
        ["report", str(passed_path), "--plan", str(LIVE_DIR / "isolated-plan.json")]
    )
    assert code == 0
    assert '"evidence_passed": true' in capsys.readouterr().out

    failed = copy.deepcopy(passed)
    failed["status"] = "failed"
    failed["summary"] = {"total": 2, "passed": 1, "failed": 1, "blocked": 0, "skipped": 0}
    failed["checks"][0]["status"] = "failed"
    failed["checks"][0]["error"] = "probe failed"
    failed["unproven_gaps"] = ["probe failed"]
    failed_path = tmp_path / "failed.json"
    failed_path.write_text(json.dumps(failed), encoding="utf-8")
    code = validation_main(
        ["report", str(failed_path), "--plan", str(LIVE_DIR / "isolated-plan.json")]
    )
    assert code == 2
    assert '"evidence_passed": false' in capsys.readouterr().out
