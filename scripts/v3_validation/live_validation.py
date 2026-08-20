from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .inventory import load_and_validate_runtime_inventory
from .paths import LIVE_PLAN_SCHEMA_PATH, LIVE_REPORT_SCHEMA_PATH
from .route_reviews import RouteMethodKey, load_route_method_reviews, require_reviewed_route_method
from .schema import ContractValidationError, load_json, validate_json
from .side_effects import evaluate_side_effect


def plan_sha256(plan: dict[str, Any]) -> str:
    body = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_live_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate an isolated plan without opening sockets or executing checks."""

    validate_json(plan, load_json(LIVE_PLAN_SCHEMA_PATH), label="LIVE validation plan")
    check_ids = [row["check_id"] for row in plan["checks"]]
    if len(check_ids) != len(set(check_ids)):
        raise ContractValidationError("LIVE validation plan has duplicate check_id values")
    inventory = load_and_validate_runtime_inventory()
    inventory_keys = {
        RouteMethodKey(row["service"], row["rule"], method, row["endpoint"])
        for row in inventory["coverage"]
        for method in row["methods"]
    }
    reviews = load_route_method_reviews(expected_inventory_fingerprint=inventory["fingerprint"])
    executable = 0
    for row in plan["checks"]:
        route = row["route"]
        key = RouteMethodKey(route["service"], route["rule"], route["method"], route["endpoint"])
        if key not in inventory_keys:
            raise ContractValidationError(
                f"LIVE validation plan check {row['check_id']!r} is not pinned to the runtime inventory"
            )
        review = require_reviewed_route_method(
            service=key.service,
            rule=key.rule,
            method=key.method,
            endpoint=key.endpoint,
            reviews=reviews,
        )
        if row["side_effect_class"] != review.side_effect_class:
            raise ContractValidationError(
                f"LIVE validation plan check {row['check_id']!r} side-effect mismatch: "
                f"reviewed={review.side_effect_class!r}, declared={row['side_effect_class']!r}"
            )
        if row["execution_mode"] != "live_probe":
            continue
        decision = evaluate_side_effect(review.side_effect_class, phase="isolated_live_validation")
        if not decision.allowed or not decision.execute:
            raise ContractValidationError(
                f"LIVE validation plan check {row['check_id']!r} is not a safe live probe: {decision.reason}"
            )
        executable += 1
    return {
        "ok": True,
        "plan_id": plan["plan_id"],
        "checks": len(plan["checks"]),
        "live_probes": executable,
        "offline_only": len(plan["checks"]) - executable,
        "reviewed_checks": len(plan["checks"]),
        "plan_sha256": plan_sha256(plan),
    }


def load_live_plan(path: str | Path) -> dict[str, Any]:
    plan = load_json(path)
    validate_live_plan(plan)
    return plan


def validate_live_report(report: dict[str, Any]) -> dict[str, Any]:
    """Validate report structure and internal counts; this does not certify evidence."""

    validate_json(report, load_json(LIVE_REPORT_SCHEMA_PATH), label="LIVE validation report")
    check_ids = [row["check_id"] for row in report["checks"]]
    if len(check_ids) != len(set(check_ids)):
        raise ContractValidationError("LIVE validation report has duplicate check_id values")
    if _timestamp(report["finished_at"]) < _timestamp(report["started_at"]):
        raise ContractValidationError("LIVE validation report finished_at precedes started_at")
    counts = Counter(row["status"] for row in report["checks"])
    actual = {
        "total": len(report["checks"]),
        "passed": counts["passed"],
        "failed": counts["failed"],
        "blocked": counts["blocked"],
        "skipped": counts["skipped"],
    }
    if report["summary"] != actual:
        raise ContractValidationError(
            f"LIVE validation report summary mismatch: declared {report['summary']}, actual {actual}"
        )
    if report["status"] == "passed" and (actual["failed"] or actual["blocked"] or actual["skipped"]):
        raise ContractValidationError("passed LIVE report cannot contain failed, blocked, or skipped checks")
    if any(row["status"] == "passed" and row["error"] is not None for row in report["checks"]):
        raise ContractValidationError("passed LIVE report checks must have null error")
    if report["status"] == "failed" and not actual["failed"]:
        raise ContractValidationError("failed LIVE report must contain at least one failed check")
    if report["status"] == "blocked" and not actual["blocked"]:
        raise ContractValidationError("blocked LIVE report must contain at least one blocked check")
    return {
        "schema_valid": True,
        "evidence_passed": False,
        "report_id": report["report_id"],
        "status": report["status"],
        "summary": actual,
    }


def validate_live_report_against_plan(report: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Jointly validate report bindings and decide whether evidence actually passed."""

    result = validate_live_report(report)
    validate_live_plan(plan)
    expected_hash = plan_sha256(plan)
    if report["plan_id"] != plan["plan_id"]:
        raise ContractValidationError("LIVE report plan_id does not match the supplied plan")
    if report["plan_sha256"] != expected_hash:
        raise ContractValidationError("LIVE report plan_sha256 does not match the supplied plan")
    plan_ids = {row["check_id"] for row in plan["checks"]}
    report_ids = {row["check_id"] for row in report["checks"]}
    if report_ids != plan_ids:
        raise ContractValidationError(
            f"LIVE report check IDs do not match plan: missing={sorted(plan_ids - report_ids)}, "
            f"extra={sorted(report_ids - plan_ids)}"
        )
    if report["environment_attestation"] != plan["environment"]:
        raise ContractValidationError("LIVE report environment attestation does not match plan")
    if _timestamp(report["started_at"]) < _timestamp(plan["created_at"]):
        raise ContractValidationError("LIVE report started before its plan was created")
    evidence_passed = report["status"] == "passed" and not report["unproven_gaps"]
    return {**result, "evidence_passed": evidence_passed, "plan_sha256": expected_hash}


def validate_live_campaign_reports(
    reports: list[dict[str, Any]],
    plan: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Require three clean, reset-separated isolated runs inside one 24-hour campaign."""

    required_runs = policy.get("required_runs")
    reset_minutes = policy.get("minimum_reset_minutes")
    completion_hours = policy.get("completion_window_hours")
    maximum_duration_minutes = policy.get("maximum_duration_minutes")
    allowed_window = policy.get("allowed_window")
    allowed_local_dates = policy.get("allowed_local_dates")
    if (
        not isinstance(required_runs, int)
        or isinstance(required_runs, bool)
        or required_runs < 3
        or not isinstance(reset_minutes, int)
        or isinstance(reset_minutes, bool)
        or reset_minutes < 10
        or not isinstance(completion_hours, int)
        or isinstance(completion_hours, bool)
        or not 1 <= completion_hours <= 24
        or not isinstance(maximum_duration_minutes, int)
        or isinstance(maximum_duration_minutes, bool)
        or maximum_duration_minutes <= 0
        or not isinstance(allowed_window, dict)
        or not isinstance(allowed_local_dates, list)
        or not allowed_local_dates
        or any(not isinstance(value, str) for value in allowed_local_dates)
    ):
        raise ContractValidationError("isolated LIVE campaign policy is invalid")
    if len(reports) != required_runs:
        raise ContractValidationError(
            f"isolated LIVE campaign requires exactly {required_runs} reports"
        )
    report_ids = [str(report.get("report_id") or "") for report in reports]
    if len(set(report_ids)) != required_runs:
        raise ContractValidationError("isolated LIVE campaign report IDs must be unique")

    taipei = ZoneInfo("Asia/Taipei")
    try:
        window_start_hour, window_start_minute = map(
            int, str(allowed_window["start"]).split(":", 1)
        )
        window_end_hour, window_end_minute = map(
            int, str(allowed_window["end"]).split(":", 1)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractValidationError("isolated LIVE allowed window is invalid") from exc
    window_start = window_start_hour * 60 + window_start_minute
    window_end = window_end_hour * 60 + window_end_minute
    if not 0 <= window_start < window_end <= 24 * 60:
        raise ContractValidationError("isolated LIVE allowed window is invalid")
    try:
        parsed_local_dates = [date.fromisoformat(value) for value in allowed_local_dates]
    except ValueError as exc:
        raise ContractValidationError("isolated LIVE allowed local dates are invalid") from exc
    if (
        allowed_local_dates != [value.isoformat() for value in parsed_local_dates]
        or len(set(parsed_local_dates)) != len(parsed_local_dates)
    ):
        raise ContractValidationError("isolated LIVE allowed local dates are invalid")
    allowed_local_date_set = set(parsed_local_dates)

    intervals: list[tuple[datetime, datetime]] = []
    for report in reports:
        result = validate_live_report_against_plan(report, plan)
        if result["evidence_passed"] is not True:
            raise ContractValidationError("every isolated LIVE report must pass without gaps")
        started = _timestamp(report["started_at"])
        finished = _timestamp(report["finished_at"])
        duration = finished - started
        if duration <= timedelta(0) or duration > timedelta(minutes=maximum_duration_minutes):
            raise ContractValidationError("isolated LIVE run duration exceeds its bound")
        local_start = started.astimezone(taipei)
        local_finish = finished.astimezone(taipei)
        start_minute = local_start.hour * 60 + local_start.minute
        finish_minute = local_finish.hour * 60 + local_finish.minute
        if (
            local_start.date() != local_finish.date()
            or local_start.date() not in allowed_local_date_set
            or start_minute < window_start
            or finish_minute > window_end
        ):
            raise ContractValidationError(
                "isolated LIVE run is outside the release-bound allowed window"
            )
        intervals.append((started, finished))

    if intervals != sorted(intervals):
        raise ContractValidationError("isolated LIVE reports must be chronological")
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] - previous[1] < timedelta(minutes=reset_minutes):
            raise ContractValidationError("isolated LIVE reset interval is too short")
    if intervals[-1][1] - intervals[0][0] > timedelta(hours=completion_hours):
        raise ContractValidationError("isolated LIVE campaign exceeded its completion window")
    return {
        "schema_version": 1,
        "evidence_passed": True,
        "required_runs": required_runs,
        "completed_runs": len(reports),
        "minimum_reset_minutes": reset_minutes,
        "completion_window_hours": completion_hours,
        "allowed_local_dates": allowed_local_dates,
        "started_at": reports[0]["started_at"],
        "finished_at": reports[-1]["finished_at"],
        "report_ids": report_ids,
    }


def load_live_report(path: str | Path, *, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    report = load_json(path)
    if plan is None:
        validate_live_report(report)
    else:
        validate_live_report_against_plan(report, plan)
    return report
