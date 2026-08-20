#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = MAGI_ROOT / "config" / "test_matrix.json"
_RUNTIME_OVERRIDE = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
RUNTIME_DIR = Path(_RUNTIME_OVERRIDE or MAGI_ROOT / ".runtime").expanduser()


def load_bound_test_environment() -> str:
    """Load the verified V3 dotenv for direct/manual suite execution.

    Services normally inherit these values from the supervisor.  A human or
    acceptance harness can invoke this runner with only the launchd bindings,
    though, so the runner must apply the same hash-bound input contract before
    deciding that a required credential is missing.  Explicit process values
    continue to win and legacy/V2 invocations without a bound file are left
    unchanged.
    """

    raw_path = os.environ.get("MAGI_ENV_FILE", "").strip()
    expected_sha256 = os.environ.get("MAGI_ENV_FILE_SHA256", "").strip().lower()
    if not raw_path and not expected_sha256:
        return ""
    if not raw_path or not expected_sha256:
        raise RuntimeError("bound test environment requires both path and SHA-256")
    if str(MAGI_ROOT) not in sys.path:
        sys.path.insert(0, str(MAGI_ROOT))
    from dotenv import dotenv_values
    from magi_v3.service_runtime import verify_environment_file

    verified = verify_environment_file(Path(raw_path), expected_sha256)
    loaded = dotenv_values(dotenv_path=verified, encoding="utf-8", interpolate=True)
    if not isinstance(loaded, dict):
        raise RuntimeError("bound test environment is invalid")
    for key, value in loaded.items():
        if not isinstance(key, str) or not key or "\x00" in key:
            raise RuntimeError("bound test environment contains an invalid variable name")
        if value is None:
            continue
        if not isinstance(value, str) or "\x00" in value:
            raise RuntimeError(f"bound test environment variable {key} is invalid")
        os.environ.setdefault(key, value)
    return str(verified)


def resolve_runtime_output(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute() or not _RUNTIME_OVERRIDE:
        return path
    relative = Path(*path.parts[1:]) if path.parts and path.parts[0] == ".runtime" else path
    return RUNTIME_DIR / relative


@dataclass
class CheckResult:
    id: str
    name: str
    ok: bool
    skipped: bool = False
    returncode: int | None = None
    elapsed_sec: float = 0.0
    command: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    message: str = ""


@dataclass
class SuiteReport:
    suite: str
    ok: bool
    generated_at: str
    root: str
    matrix: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    elapsed_sec: float = 0.0
    results: list[dict[str, Any]] = field(default_factory=list)


def load_matrix(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Matrix not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Matrix is not valid JSON: {path}: {exc}")
    suites = data.get("suites")
    if not isinstance(suites, dict) or not suites:
        raise SystemExit(f"Matrix has no suites: {path}")
    return data


def resolve_command(command: list[Any]) -> list[str]:
    resolved: list[str] = []
    for part in command:
        text = str(part)
        text = text.replace("{python}", sys.executable)
        text = text.replace("{root}", str(MAGI_ROOT))
        if _RUNTIME_OVERRIDE and (text == ".runtime" or text.startswith(".runtime/")):
            text = str(resolve_runtime_output(text))
        resolved.append(text)
    return resolved


def should_skip(check: dict[str, Any]) -> tuple[bool, str]:
    require_env = check.get("require_env")
    if isinstance(require_env, str):
        require_env = [require_env]
    if isinstance(require_env, list):
        missing = [str(name) for name in require_env if not os.environ.get(str(name))]
        if missing:
            return True, "missing env: " + ", ".join(missing)
    return False, ""


def tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def run_check(check: dict[str, Any], *, dry_run: bool, suite: str = "") -> CheckResult:
    cid = str(check.get("id") or check.get("name") or "unnamed")
    name = str(check.get("name") or cid)
    command = resolve_command(check.get("command") or [])
    if not command:
        return CheckResult(id=cid, name=name, ok=False, message="empty command")

    skip, reason = should_skip(check)
    if skip:
        return CheckResult(id=cid, name=name, ok=True, skipped=True, command=command, message=reason)
    if dry_run:
        return CheckResult(id=cid, name=name, ok=True, skipped=True, command=command, message="dry-run")

    env = os.environ.copy()
    if "live" in (suite or "").lower():
        env["MAGI_ENABLE_LIVE_TESTS"] = "1"
    extra_env = check.get("env")
    if isinstance(extra_env, dict):
        env.update({str(k): str(v) for k, v in extra_env.items()})
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(MAGI_ROOT) + (os.pathsep + current_pythonpath if current_pythonpath else "")

    timeout = int(check.get("timeout_sec") or 300)
    start = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=str(MAGI_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        elapsed = round(time.time() - start, 3)
        ok_codes = check.get("ok_returncodes", [0])
        if not isinstance(ok_codes, list):
            ok_codes = [ok_codes]
        ok = proc.returncode in {int(code) for code in ok_codes}
        return CheckResult(
            id=cid,
            name=name,
            ok=ok,
            returncode=proc.returncode,
            elapsed_sec=elapsed,
            command=command,
            stdout_tail=tail(proc.stdout),
            stderr_tail=tail(proc.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.time() - start, 3)
        return CheckResult(
            id=cid,
            name=name,
            ok=False,
            returncode=None,
            elapsed_sec=elapsed,
            command=command,
            stdout_tail=tail(exc.stdout or ""),
            stderr_tail=tail(exc.stderr or ""),
            message=f"timeout after {timeout}s",
        )


def list_suites(matrix: dict[str, Any]) -> None:
    for suite, spec in matrix["suites"].items():
        checks = spec.get("checks") if isinstance(spec, dict) else []
        print(f"{suite}: {len(checks or [])} checks - {spec.get('description', '')}")


def run_suite(matrix: dict[str, Any], matrix_path: Path, suite: str, *, dry_run: bool) -> SuiteReport:
    suites = matrix["suites"]
    if suite not in suites:
        known = ", ".join(sorted(suites))
        raise SystemExit(f"Unknown suite '{suite}'. Known suites: {known}")
    spec = suites[suite]
    checks = spec.get("checks")
    if not isinstance(checks, list) or not checks:
        raise SystemExit(f"Suite '{suite}' has no checks")

    report = SuiteReport(
        suite=suite,
        ok=True,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        root=str(MAGI_ROOT),
        matrix=str(matrix_path),
    )
    start = time.time()
    print(f"MAGI test suite: {suite}")
    print(f"checks: {len(checks)}")
    print(f"dry_run: {dry_run}")
    for check in checks:
        result = run_check(check, dry_run=dry_run, suite=suite)
        report.total += 1
        if result.skipped:
            report.skipped += 1
            status = "SKIP"
        elif result.ok:
            report.passed += 1
            status = "PASS"
        else:
            report.failed += 1
            report.ok = False
            status = "FAIL"
        report.results.append(asdict(result))
        detail = result.message or f"exit={result.returncode}"
        print(f"[{status}] {result.id} ({result.elapsed_sec:.2f}s) {detail}")
        if not result.ok and not result.skipped:
            if result.stdout_tail:
                print("  stdout_tail:")
                print("\n".join("    " + line for line in result.stdout_tail.splitlines()[-40:]))
            if result.stderr_tail:
                print("  stderr_tail:")
                print("\n".join("    " + line for line in result.stderr_tail.splitlines()[-40:]))
    report.elapsed_sec = round(time.time() - start, 3)
    report.ok = report.failed == 0
    print(
        f"summary: total={report.total} passed={report.passed} "
        f"failed={report.failed} skipped={report.skipped}"
    )
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run MAGI test suites from config/test_matrix.json.")
    parser.add_argument("--matrix", default=str(DEFAULT_MATRIX), help="Path to test matrix JSON.")
    parser.add_argument("--suite", default="ci", help="Suite name to run.")
    parser.add_argument("--list", action="store_true", help="List suites and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve checks without executing commands.")
    parser.add_argument("--json-out", help="Write suite result JSON.")
    args = parser.parse_args(argv)

    try:
        load_bound_test_environment()
    except Exception as exc:
        raise SystemExit(f"Bound test environment verification failed: {exc}") from exc

    matrix_path = Path(args.matrix)
    if not matrix_path.is_absolute():
        matrix_path = MAGI_ROOT / matrix_path
    matrix = load_matrix(matrix_path)

    if args.list:
        list_suites(matrix)
        return 0

    report = run_suite(matrix, matrix_path, args.suite, dry_run=args.dry_run)
    if args.json_out:
        out_path = resolve_runtime_output(args.json_out)
        if not out_path.is_absolute():
            out_path = MAGI_ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json_out: {out_path}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
