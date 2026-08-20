"""CLI for fail-closed V3 cutover planning and ownership preflight."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from scripts.v3_release_gate import DEFAULT_MAX_EVIDENCE_AGE_HOURS, evaluate_evidence

from .core import (
    CutoverError,
    assess_absolute_window,
    assess_cutover_window,
    assess_snapshot,
    load_gate_config,
)
from .mutation import PreparedCutoverExecutor, PreparedRollbackExecutor, load_prepared_plan
from .planning import create_prepared_plan
from .probe import DEFAULT_PORTS, collect_snapshot, discover_release_spec
from .workflow import WORKFLOWS, build_workflow, simulate_workflow

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GATES = ROOT / "config" / "v3_cutover_gates.json"
DEFAULT_LIVE_BASE = Path.home() / "Library" / "Application Support" / "MAGI" / "runtime"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MAGI V3 single-active cutover guard")
    parser.add_argument("--gates", type=Path, default=DEFAULT_GATES)
    parser.add_argument("--v2-root", type=Path, default=DEFAULT_LIVE_BASE / "MAGI_v2")
    parser.add_argument("--v3-root", type=Path, default=DEFAULT_LIVE_BASE / "MAGI_v3")
    parser.add_argument("--v2-namespace", default="magi-v2-production")
    parser.add_argument("--v3-namespace", default="magi-v3-production")
    parser.add_argument("--port", action="append", type=int, dest="ports")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="emit a non-mutating handoff plan")
    plan.add_argument("workflow", choices=sorted(WORKFLOWS))

    preflight = subparsers.add_parser("preflight", help="run read-only ownership probes")
    preflight.add_argument("--expect", choices=("zero", "v2", "v3"), required=True)
    preflight.add_argument("--evidence-dir", type=Path, required=True)
    preflight.add_argument("--campaign-id", required=True)
    preflight.add_argument("--release-sha", required=True)
    preflight.add_argument("--hardware-id", required=True)
    preflight.add_argument("--gate-config-sha256", required=True)
    preflight.add_argument(
        "--max-evidence-age-hours", type=float, default=DEFAULT_MAX_EVIDENCE_AGE_HOURS
    )

    simulate = subparsers.add_parser("simulate", help="fault-inject an in-memory handoff")
    simulate.add_argument("workflow", choices=sorted(WORKFLOWS))
    simulate.add_argument("--initial-release", choices=("v2", "v3"))
    simulate.add_argument(
        "--residual",
        action="append",
        default=[],
        metavar="RELEASE:DOMAIN",
        help="retain an owner after stop, e.g. v2:scheduler",
    )
    prepare = subparsers.add_parser(
        "prepare-plan", help="write a hash-bound cutover or rollback plan"
    )
    prepare.add_argument("operation", choices=("cutover", "rollback"))
    prepare.add_argument(
        "--execution-purpose",
        choices=("atomic_drill", "final_cutover"),
        required=True,
    )
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--token-file", type=Path, required=True)
    prepare.add_argument("--pre-cutover-report", type=Path, required=True)
    prepare.add_argument("--deploy-prepared-marker", type=Path, required=True)
    prepare.add_argument("--release-manifest", type=Path, required=True)
    prepare.add_argument(
        "--v2-launchagent",
        action="append",
        default=[],
        metavar="LABEL=PLIST",
    )
    prepare.add_argument(
        "--v3-install-directory",
        type=Path,
        default=Path.home() / "Library" / "LaunchAgents",
    )
    prepare.add_argument(
        "--laf-dedup-source",
        action="append",
        type=Path,
        default=[],
        help="V2 processed Gmail JSON path; required for cutover and repeatable",
    )
    prepare.add_argument(
        "--laf-dedup-manifest-output",
        type=Path,
        help="new owner-only manifest path created only after V2 reaches ownership zero",
    )
    prepare.add_argument(
        "--laf-dedup-db-env-file",
        type=Path,
        help="owner-only DB environment file; contents are never emitted in reports",
    )
    prepare.add_argument("--pdf-namer-source", type=Path)
    prepare.add_argument("--pdf-namer-destination", type=Path)
    prepare.add_argument("--pdf-namer-manifest", type=Path)
    prepare.add_argument("--mutable-state-source-root", type=Path)
    prepare.add_argument("--mutable-state-target-shared-root", type=Path)
    prepare.add_argument("--mutable-state-dry-run-receipt", type=Path)
    prepare.add_argument("--mutable-state-prepare-receipt", type=Path)
    prepare.add_argument("--mutable-state-staging-root", type=Path)
    prepare.add_argument(
        "--final-pre-cutover-report",
        type=Path,
        help=(
            "fresh final GO report path; required for final cutover so the plan "
            "and report can bind each other without a circular hash dependency"
        ),
    )
    execute = subparsers.add_parser(
        "execute",
        help="execute one fully gated, hash-bound cutover or rollback",
    )
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--plan-sha256", required=True)
    execute.add_argument("--token-file", type=Path, required=True)
    execute.add_argument(
        "--report-output",
        type=Path,
        required=True,
        help="new owner-only 0400 JSON execution receipt",
    )
    return parser


def _residuals(values: list[str]) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for value in values:
        release, separator, domain = value.partition(":")
        if not separator or release not in {"v2", "v3"} or not domain:
            raise CutoverError(f"invalid residual injection: {value}")
        result.setdefault(release, []).append(domain)
    return {key: tuple(items) for key, items in result.items()}


def _evidence_report(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Reject the removed boolean/status-map evidence shortcut."""

    del args, kwargs
    raise CutoverError("boolean evidence JSON is disabled; use the context-bound release gate")


def _write_execution_report(path: Path, report: dict[str, Any]) -> None:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise CutoverError("execution report output must be a canonical absolute new path")
    raw.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = raw.parent.lstat()
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise CutoverError("execution report parent is unsafe")
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        descriptor = os.open(
            raw,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
    except OSError as exc:
        raise CutoverError(f"execution report output is unavailable: {exc}") from exc
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(raw.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def run(argv: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    args = _parser().parse_args(argv)
    gates = load_gate_config(args.gates)
    conditional_window = gates.get("conditional_daytime_window")
    conditional_required = (
        gates.get("conditional_daytime_authorization_required") is True
    )
    effective_window = conditional_window if conditional_required else gates["window"]
    base = {
        "schema_version": 1,
        "command": args.command,
        "mutation_performed": False,
        "gates": str(args.gates.expanduser().resolve()),
        "cutover_window": effective_window,
        "cutover_window_mode": (
            "conditional_daytime" if conditional_required else "legacy_recurring"
        ),
    }
    if args.command == "plan":
        steps = build_workflow(args.workflow)
        return 0, {**base, "workflow": args.workflow, "steps": [step.to_dict() for step in steps]}
    if args.command == "simulate":
        report = simulate_workflow(
            args.workflow,
            initial_release=args.initial_release,
            residual_after_stop=_residuals(args.residual),
        )
        return (0 if report["ok"] else 2), {**base, **report}

    if args.command == "prepare-plan":
        if args.operation == "cutover" and not all(
            (
                args.mutable_state_source_root,
                args.mutable_state_target_shared_root,
                args.mutable_state_dry_run_receipt,
                args.mutable_state_prepare_receipt,
                args.mutable_state_staging_root,
            )
        ):
            raise CutoverError("cutover requires the exact mutable-state handoff bindings")
        report = create_prepared_plan(
            operation=args.operation,
            execution_purpose=args.execution_purpose,
            output=args.output,
            token_file=args.token_file,
            gate_config=args.gates,
            pre_cutover_report=args.pre_cutover_report,
            deploy_prepared_marker=args.deploy_prepared_marker,
            release_manifest=args.release_manifest,
            v2_launchagents=args.v2_launchagent,
            v3_install_directory=args.v3_install_directory,
            laf_dedup_sources=args.laf_dedup_source,
            laf_dedup_manifest_output=args.laf_dedup_manifest_output,
            laf_dedup_db_env_file=args.laf_dedup_db_env_file,
            pdf_namer_source=args.pdf_namer_source,
            pdf_namer_destination=args.pdf_namer_destination,
            pdf_namer_manifest=args.pdf_namer_manifest,
            mutable_state_source_root=args.mutable_state_source_root,
            mutable_state_target_shared_root=args.mutable_state_target_shared_root,
            mutable_state_dry_run_receipt=args.mutable_state_dry_run_receipt,
            mutable_state_prepare_receipt=args.mutable_state_prepare_receipt,
            mutable_state_staging_root=args.mutable_state_staging_root,
            final_pre_cutover_report=args.final_pre_cutover_report,
        )
        return 0, {**base, **report}

    if args.command == "execute":
        plan = load_prepared_plan(
            args.plan,
            args.plan_sha256,
            require_mutable_state_handoff=True,
            allow_completed_handoff_outputs=True,
        )
        selected_gates = args.gates.expanduser().resolve(strict=True)
        if plan.gate_config.path != selected_gates:
            raise CutoverError("execute plan gate config does not match --gates")

        def current_snapshot():
            # Execution must classify V3 against the immutable release bound
            # by the plan, never an independently supplied/mutable CLI path.
            v3_release_root = plan.release_manifest.path.parent
            specs = (
                discover_release_spec("v2", args.v2_root, args.v2_namespace),
                discover_release_spec(
                    "v3",
                    v3_release_root,
                    args.v3_namespace,
                    runtime_root=DEFAULT_LIVE_BASE / "MAGI_v3",
                    # A stopped V3 has no PID files.  Installed launchd labels
                    # and ownership are mandatory; role PIDs are consumed when
                    # present after bootstrap.
                    pidfiles_required=False,
                    launchd_labels_required=False,
                ),
            )
            return collect_snapshot(specs, ports=args.ports or DEFAULT_PORTS)

        executor_type = (
            PreparedRollbackExecutor
            if plan.operation == "v3_to_v2_rollback"
            else PreparedCutoverExecutor
        )
        executor = executor_type(
            plan,
            token_file=args.token_file,
            snapshot_collector=current_snapshot,
        )
        report = executor.execute()
        completed = {**base, **report}
        _write_execution_report(args.report_output, completed)
        return (0 if report["ok"] else 2), completed

    specs = (
        discover_release_spec("v2", args.v2_root, args.v2_namespace),
        discover_release_spec(
            "v3",
            args.v3_root,
            args.v3_namespace,
            runtime_root=DEFAULT_LIVE_BASE / "MAGI_v3",
            pidfiles_required=False,
            launchd_labels_required=False,
        ),
    )
    snapshot = collect_snapshot(specs, ports=args.ports or DEFAULT_PORTS)
    assessment = assess_snapshot(snapshot, expected=args.expect)
    window_assessment = (
        assess_absolute_window(conditional_window)
        if conditional_required
        else assess_cutover_window(
            gates["window"], timezone_name=str(gates.get("timezone") or "UTC")
        )
    )
    try:
        actual_gate_digest = hashlib.sha256(args.gates.expanduser().read_bytes()).hexdigest()
        if actual_gate_digest != args.gate_config_sha256:
            raise ValueError("--gate-config-sha256 does not match the selected gate config")
        expected_context = {
            "campaign_id": args.campaign_id,
            "release_sha": args.release_sha,
            "hardware_id": args.hardware_id,
            "gate_config_sha256": args.gate_config_sha256,
        }
        evidence = evaluate_evidence(
            gates,
            args.evidence_dir.expanduser(),
            expected_context=expected_context,
            max_age_hours=args.max_evidence_age_hours,
        )
    except (OSError, ValueError) as exc:
        raise CutoverError(f"release gate refused preflight: {exc}") from exc
    go = assessment.go and evidence["decision"] == "GO" and window_assessment["within_window"]
    return (0 if go else 2), {
        **base,
        "go": go,
        "snapshot": snapshot.to_dict(),
        "assessment": assessment.to_dict(),
        "evidence": evidence,
        "window_assessment": window_assessment,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        code, report = run(argv)
    except CutoverError as exc:
        code, report = 2, {"ok": False, "mutation_performed": False, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
