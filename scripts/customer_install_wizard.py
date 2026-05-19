#!/usr/bin/env python3
"""One-command installer wizard for external MAGI customers.

The wizard is intentionally safe by default: without --yes it only reports the
plan. With --yes it creates the local env file, installs dependencies, seeds
local jobs, and runs the public/commercial readiness checks that can be
validated without the customer's private credentials.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = REPO_ROOT / ".runtime" / "customer_install_wizard_latest.json"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass
class WizardStep:
    key: str
    title: str
    status: str
    detail: str = ""
    required: bool = True
    command: list[str] = field(default_factory=list)
    elapsed_sec: float = 0.0
    next_action: str = ""
    output_tail: str = ""


STEP_TIMEOUTS = {
    "create_venv": 120,
    "upgrade_pip": 300,
    "install_core": 1800,
    "install_optional": 3600,
    "seed_cron_jobs": 180,
    "doctor": 180,
}


def _status_from_bool(ok: bool, *, required: bool = True) -> str:
    if ok:
        return "pass"
    return "fail" if required else "warn"


def _command_text(command: list[str]) -> str:
    return " ".join(str(part) for part in command)


def _run_command(command: list[str], *, timeout: int, required: bool = True) -> WizardStep:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        ok = proc.returncode == 0
        return WizardStep(
            key=Path(command[0]).name if command else "command",
            title=_command_text(command),
            status=_status_from_bool(ok, required=required),
            detail=f"exit={proc.returncode}",
            required=required,
            command=command,
            elapsed_sec=round(time.monotonic() - started, 3),
            output_tail=(proc.stdout or "")[-4000:],
        )
    except subprocess.TimeoutExpired as exc:
        return WizardStep(
            key=Path(command[0]).name if command else "command",
            title=_command_text(command),
            status="fail" if required else "warn",
            detail=f"timeout after {timeout}s",
            required=required,
            command=command,
            elapsed_sec=round(time.monotonic() - started, 3),
            output_tail=((exc.stdout or "") + (exc.stderr or ""))[-4000:] if isinstance(exc.stdout, str) else "",
        )
    except Exception as exc:
        return WizardStep(
            key=Path(command[0]).name if command else "command",
            title=_command_text(command),
            status="fail" if required else "warn",
            detail=str(exc),
            required=required,
            command=command,
            elapsed_sec=round(time.monotonic() - started, 3),
        )


def _summarize(steps: list[WizardStep]) -> dict[str, int]:
    return {
        "pass": sum(1 for step in steps if step.status == "pass"),
        "warn": sum(1 for step in steps if step.status == "warn"),
        "fail": sum(1 for step in steps if step.status == "fail"),
        "skipped": sum(1 for step in steps if step.status == "skipped"),
        "pending": sum(1 for step in steps if step.status == "pending"),
        "total": len(steps),
    }


def _write_report(payload: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _env_step(*, public: bool, require_config: bool, write_env: bool) -> WizardStep:
    from scripts import first_run_setup

    write_result: dict[str, Any] | None = None
    if write_env:
        write_result = first_run_setup._write_env_from_example()
        if not write_result.get("ok"):
            return WizardStep(
                "env",
                "Create local .env",
                "fail",
                str(write_result.get("error", "unable to create .env")),
                next_action="Confirm .env.example exists and is readable.",
            )

    checklist = first_run_setup.build_first_run_checklist(public_mode=public)
    missing = []
    for item in checklist["items"]:
        if item["key"] == "required_env" and item["status"] != "pass":
            missing = [part.strip() for part in item["detail"].replace("缺少：", "").split(",") if part.strip()]
            break

    if require_config and missing:
        status = "fail"
        next_action = "Fill the missing .env values, then rerun the wizard."
    elif missing:
        status = "warn"
        next_action = "Fill .env before production go-live. The installer can still prepare local files."
    else:
        status = "pass"
        next_action = ""

    detail = "created .env" if write_result and write_result.get("created") else "checked .env"
    if missing:
        detail += "; missing " + ", ".join(missing)
    return WizardStep(
        "env",
        "Create and check local .env",
        status,
        detail,
        required=require_config,
        next_action=next_action,
    )


def _preflight_step(*, live: bool) -> WizardStep:
    from scripts.magi_doctor import collect_report

    report = collect_report(live=live)
    summary = report.get("summary", {})
    failed = int(summary.get("fail", 0))
    warned = int(summary.get("warn", 0))
    if failed:
        status = "fail"
    elif warned:
        status = "warn"
    else:
        status = "pass"
    return WizardStep(
        "preflight",
        "Detect this computer",
        status,
        json.dumps(summary, ensure_ascii=False),
        output_tail=json.dumps(report, ensure_ascii=False)[-4000:],
        next_action="Resolve failed doctor checks before installing." if failed else "",
    )


def _install_steps(*, execute: bool, include_optional: bool) -> list[WizardStep]:
    from scripts.install_magi import build_install_plan

    steps: list[WizardStep] = []
    for plan_step in build_install_plan(include_optional=include_optional):
        command = [str(part) for part in plan_step.command]
        if not execute:
            steps.append(
                WizardStep(
                    f"install:{plan_step.name}",
                    plan_step.description or plan_step.name,
                    "skipped",
                    "dry-run; pass --yes to execute",
                    required=plan_step.required,
                    command=command,
                    next_action="Run the wizard with --yes when ready.",
                )
            )
            continue
        result = _run_command(
            command,
            timeout=STEP_TIMEOUTS.get(plan_step.name, 600),
            required=plan_step.required,
        )
        result.key = f"install:{plan_step.name}"
        result.title = plan_step.description or plan_step.name
        result.required = plan_step.required
        steps.append(result)
        if result.status == "fail" and plan_step.required:
            break
    return steps


def _public_audit_step(*, public: bool) -> WizardStep:
    if not public:
        return WizardStep(
            "public_audit",
            "Public isolation audit",
            "skipped",
            "not in public mode",
            required=False,
            next_action="Use --public before preparing an external customer release.",
        )
    command = [sys.executable, str(REPO_ROOT / "scripts" / "public_release_audit.py"), "--public-isolation", "--strict"]
    result = _run_command(command, timeout=240, required=True)
    result.key = "public_audit"
    result.title = "Public isolation audit"
    result.next_action = "Remove tracked runtime/private data before sharing the repository." if result.status == "fail" else ""
    return result


def _readiness_step(*, skip: bool, skip_db: bool, live: bool) -> WizardStep:
    if skip:
        return WizardStep("readiness", "Commercial readiness gate", "skipped", "skipped by option", required=False)
    if not live:
        return WizardStep(
            "readiness",
            "Commercial readiness gate",
            "skipped",
            "--no-live skips readiness probes",
            required=False,
            next_action="Run without --no-live before production go-live.",
        )
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "ops" / "commercial_readiness_live.py"),
        "--strict-public",
        "--json-out",
        str(REPO_ROOT / ".runtime" / "commercial_readiness_customer_install_latest.json"),
    ]
    if skip_db:
        command.append("--skip-db")
    result = _run_command(command, timeout=360, required=True)
    result.key = "readiness"
    result.title = "Commercial readiness gate"
    result.next_action = "Review the readiness report before go-live." if result.status != "pass" else ""
    return result


def _service_step(*, install_service: bool, execute: bool) -> WizardStep:
    if not install_service:
        return WizardStep(
            "service",
            "Install background service",
            "skipped",
            "not requested",
            required=False,
            next_action="After .env is complete, rerun with --install-service --yes or start daemon.py manually.",
        )
    if not execute:
        return WizardStep(
            "service",
            "Install background service",
            "skipped",
            "--install-service requires --yes",
            required=False,
            next_action="Run with --install-service --yes after checking .env.",
        )
    command = [sys.executable, str(REPO_ROOT / "scripts" / "install_service.py")]
    result = _run_command(command, timeout=240, required=False)
    result.key = "service"
    result.title = "Install background service"
    result.next_action = "Start MAGI manually with python3 daemon.py if service install is not available." if result.status != "pass" else ""
    return result


def build_next_steps(*, execute: bool, require_config: bool, summary: dict[str, int]) -> list[str]:
    next_steps = []
    if not execute:
        next_steps.append("Run: python3 scripts/customer_install_wizard.py --public --yes")
    if summary["warn"] or require_config:
        next_steps.append("Open .env and fill customer-specific DB, model, storage, OAuth, and messaging values.")
    next_steps.append("Run: python3 scripts/magi_doctor.py --json")
    next_steps.append("Run: python3 scripts/ops/commercial_readiness_live.py --strict-public")
    next_steps.append("Start MAGI with python3 daemon.py, or install the background service after configuration.")
    return next_steps


def run_wizard(args: argparse.Namespace) -> dict[str, Any]:
    steps: list[WizardStep] = []
    live = not args.no_live
    execute = bool(args.yes)

    steps.append(_preflight_step(live=live))
    steps.append(_env_step(public=args.public, require_config=args.require_config, write_env=execute and not args.no_write_env))
    steps.extend(_install_steps(execute=execute, include_optional=not args.no_optional))
    steps.append(_public_audit_step(public=args.public))
    readiness_skip = args.skip_readiness or not args.check_live
    steps.append(_readiness_step(skip=readiness_skip, skip_db=not args.with_db, live=live))
    steps.append(_service_step(install_service=args.install_service, execute=execute))

    summary = _summarize(steps)
    ok = summary["fail"] == 0
    status = "pass" if ok and summary["warn"] == 0 else ("warn" if ok else "fail")
    payload = {
        "ok": ok,
        "status": status,
        "mode": "install" if execute else "dry-run",
        "public": args.public,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo": str(REPO_ROOT),
        "summary": summary,
        "steps": [asdict(step) for step in steps],
        "next_steps": build_next_steps(execute=execute, require_config=args.require_config, summary=summary),
    }
    _write_report(payload, args.output)
    payload["report_path"] = str(args.output)
    return payload


def _print_human(payload: dict[str, Any]) -> None:
    print(f"MAGI customer install wizard: {payload['status'].upper()} ({payload['mode']})")
    print(f"Report: {payload['report_path']}")
    for step in payload["steps"]:
        print(f"- {step['status'].upper():7} {step['title']}: {step['detail']}")
        if step.get("next_action"):
            print(f"          next: {step['next_action']}")
    print("Next commands:")
    for command in payload["next_steps"]:
        print(f"  {command}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MAGI's full customer self-install wizard.")
    parser.add_argument("--yes", action="store_true", help="execute install steps; default is dry-run")
    parser.add_argument("--public", action="store_true", help="enforce public-release isolation checks")
    parser.add_argument("--require-config", action="store_true", help="fail if customer-specific .env values are still missing")
    parser.add_argument("--no-write-env", action="store_true", help="do not create .env automatically")
    parser.add_argument("--no-optional", action="store_true", help="skip optional model acceleration dependencies")
    parser.add_argument("--with-db", action="store_true", help="include DB checks in readiness gate")
    parser.add_argument("--skip-readiness", action="store_true", help="skip commercial readiness gate")
    parser.add_argument("--check-live", action="store_true", help="run live readiness checks even in dry-run mode")
    parser.add_argument("--install-service", action="store_true", help="install MAGI as a background service after setup")
    parser.add_argument("--no-live", action="store_true", help="skip localhost live probes")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT, help="write JSON report here")
    args = parser.parse_args(argv)

    payload = run_wizard(args)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
