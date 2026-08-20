#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run PDF/document golden regressions declared in a manifest.

Every manifest case is executable.  Golden regressions are acceptance evidence,
not a backlog, so a case without a pytest nodeid fails validation.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = MAGI_ROOT / "tests" / "golden" / "document_regression_manifest.json"
PYTHON = sys.executable

REQUIRED_CASE_FIELDS = {"id", "category", "title", "status", "expected_behavior"}
VALID_STATUSES = {"automated"}


def load_manifest(path: str | Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    data["_manifest_path"] = str(manifest_path)
    return data


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(manifest.get("schema_version") or 0) != 1:
        errors.append("schema_version must be 1")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty list")
        cases = []

    required_categories = manifest.get("required_categories") or []
    if not isinstance(required_categories, list):
        errors.append("required_categories must be a list")
        required_categories = []

    seen_ids: set[str] = set()
    seen_categories: set[str] = set()
    automated_count = 0
    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = sorted(field for field in REQUIRED_CASE_FIELDS if not case.get(field))
        if missing:
            errors.append(f"{prefix} missing required fields: {', '.join(missing)}")
        case_id = str(case.get("id") or "")
        if case_id in seen_ids:
            errors.append(f"{prefix} duplicate id: {case_id}")
        seen_ids.add(case_id)
        seen_categories.add(str(case.get("category") or ""))

        status = str(case.get("status") or "")
        if status not in VALID_STATUSES:
            errors.append(f"{prefix} has invalid status: {status}")
        if status == "automated":
            automated_count += 1
            if not case.get("pytest"):
                errors.append(f"{prefix} automated case requires pytest nodeid")

    for category in required_categories:
        if category not in seen_categories:
            errors.append(f"required category has no case: {category}")
    if automated_count < 1:
        errors.append("manifest must include at least one automated case")
    return errors


def _select_cases(
    manifest: dict[str, Any],
    *,
    case_ids: set[str] | None = None,
    categories: set[str] | None = None,
    automated_only: bool = False,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for case in manifest.get("cases") or []:
        if case_ids and str(case.get("id") or "") not in case_ids:
            continue
        if categories and str(case.get("category") or "") not in categories:
            continue
        if automated_only and case.get("status") != "automated":
            continue
        selected.append(case)
    return selected


def _run_pytest(nodeid: str, timeout: int) -> dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            [PYTHON, "-m", "pytest", "-q", nodeid],
            cwd=str(MAGI_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "nodeid": nodeid,
            "ok": False,
            "rc": -1,
            "elapsed_sec": round(time.time() - started, 2),
            "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr": f"timeout ({timeout}s)",
        }
    except Exception as exc:  # pragma: no cover - defensive runner path
        return {
            "nodeid": nodeid,
            "ok": False,
            "rc": -2,
            "elapsed_sec": round(time.time() - started, 2),
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "nodeid": nodeid,
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "elapsed_sec": round(time.time() - started, 2),
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def run_manifest(
    manifest_path: str | Path = DEFAULT_MANIFEST,
    *,
    case_ids: set[str] | None = None,
    categories: set[str] | None = None,
    automated_only: bool = False,
    timeout: int = 180,
    dry_run: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    validation_errors = validate_manifest(manifest)
    selected = _select_cases(
        manifest,
        case_ids=case_ids,
        categories=categories,
        automated_only=automated_only,
    )

    results: list[dict[str, Any]] = []
    if not validation_errors:
        for case in selected:
            result: dict[str, Any] = {
                "id": case.get("id"),
                "category": case.get("category"),
                "title": case.get("title"),
                "status": case.get("status"),
            }
            if case.get("status") == "automated":
                nodeid = str(case.get("pytest") or "")
                if dry_run:
                    result.update({"ok": True, "skipped": True, "reason": "dry_run", "nodeid": nodeid})
                else:
                    pytest_result = _run_pytest(nodeid, timeout)
                    result.update(pytest_result)
            else:  # pragma: no cover - validation rejects this before execution
                result.update({"ok": False, "skipped": False, "reason": "not_automated"})
            results.append(result)

    failed = [r for r in results if not r.get("ok")]
    skipped = [r for r in results if r.get("skipped")]
    automated = [r for r in results if r.get("status") == "automated"]
    return {
        "ok": not validation_errors and not failed,
        "manifest": str(manifest_path),
        "validation_errors": validation_errors,
        "selected": len(selected),
        "automated": len(automated),
        "skipped": len(skipped),
        "failed": len(failed),
        "results": results,
    }


def _csv_set(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run PDF/document golden regression manifest cases")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--case", dest="case_ids", default="", help="Comma-separated case ids")
    parser.add_argument("--categories", default="", help="Comma-separated categories")
    parser.add_argument("--automated-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Validate and list selected tests without running pytest")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    report = run_manifest(
        args.manifest,
        case_ids=_csv_set(args.case_ids) or None,
        categories=_csv_set(args.categories) or None,
        automated_only=args.automated_only,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )

    print("=== PDF/Document Golden Regression ===")
    print(
        f"selected={report['selected']} automated={report['automated']} "
        f"skipped={report['skipped']} failed={report['failed']}"
    )
    for error in report.get("validation_errors") or []:
        print(f"[manifest-error] {error}")
    for result in report.get("results") or []:
        label = "PASS" if result.get("ok") and not result.get("skipped") else "SKIP" if result.get("skipped") else "FAIL"
        print(f"[{label}] {result.get('id')} - {result.get('title')}")
        if result.get("reason"):
            print(f"  reason: {result.get('reason')}")
        if not result.get("ok"):
            stdout = (result.get("stdout") or "").strip()
            stderr = (result.get("stderr") or "").strip()
            if stdout:
                print(stdout)
            if stderr:
                print(stderr, file=sys.stderr)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON report saved: {out}")

    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
