from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .fixtures import load_replay_fixture
from .inventory import load_and_validate_runtime_inventory
from .live_validation import (
    load_live_plan,
    validate_live_plan,
    validate_live_campaign_reports,
    validate_live_report,
    validate_live_report_against_plan,
)
from .schema import load_json


def _write(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.v3_validation",
        description="Offline-only MAGI V3 contract and replay validator (never executes probes).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory", help="validate the pinned 347-route inventory")
    fixtures = commands.add_parser("fixtures", help="validate anonymized replay fixture files")
    fixtures.add_argument("paths", type=Path, nargs="+")
    plan = commands.add_parser("plan", help="validate a LIVE plan without executing it")
    plan.add_argument("path", type=Path)
    report = commands.add_parser("report", help="validate a LIVE report without certifying its evidence")
    report.add_argument("path", type=Path)
    report.add_argument("--plan", type=Path, help="required before a passed report can certify evidence")
    campaign = commands.add_parser(
        "live-campaign", help="validate all isolated LIVE reports against the 24-hour policy"
    )
    campaign.add_argument("paths", type=Path, nargs="+")
    campaign.add_argument("--plan", type=Path, required=True)
    campaign.add_argument("--campaign-config", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inventory":
            result = load_and_validate_runtime_inventory()
            payload = {
                "inventory_valid": result["inventory_valid"],
                "implementation_coverage_complete": result["implementation_coverage_complete"],
                "counts": result["counts"],
                "fingerprint": result["fingerprint"],
                "review_summary": result["review_summary"],
            }
            _write(payload)
            return 0 if result["implementation_coverage_complete"] else 2
        if args.command == "fixtures":
            fixture_ids = [load_replay_fixture(path)["fixture_id"] for path in args.paths]
            if len(fixture_ids) != len(set(fixture_ids)):
                raise ValueError("duplicate fixture_id values")
            _write({"schema_valid": True, "validated": len(fixture_ids), "fixture_ids": fixture_ids})
            return 0
        if args.command == "plan":
            result = validate_live_plan(load_live_plan(args.path))
            _write({"schema_valid": True, "safe_plan": True, **result})
            return 0
        if args.command == "report":
            report = load_json(args.path)
            structural = validate_live_report(report)
            if args.plan is None:
                result = {**structural, "evidence_passed": False, "binding_error": "--plan is required"}
            else:
                result = validate_live_report_against_plan(report, load_live_plan(args.plan))
            _write(result)
            return 0 if result["evidence_passed"] else 2
        if args.command == "live-campaign":
            campaign_config = load_json(args.campaign_config)
            result = validate_live_campaign_reports(
                [load_json(path) for path in args.paths],
                load_live_plan(args.plan),
                campaign_config["isolated_live_validation"],
            )
            _write(result)
            return 0
    except Exception as exc:
        _write({"schema_valid": False, "evidence_passed": False, "error": str(exc)})
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
