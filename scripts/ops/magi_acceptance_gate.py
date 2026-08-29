#!/usr/bin/env python3
"""MAGI acceptance boundary gate.

This is the single verdict layer above the existing health tools.  Individual
tools still own their domain checks; this script normalizes their results into
GREEN / YELLOW / RED and refuses to treat unclassified warnings as acceptance.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# The acceptance gate imports product modules before checking repository
# cleanliness.  Prevent those imports (and the child pytest gate) from making
# __pycache__ files inside an otherwise immutable verification worktree.
sys.dont_write_bytecode = True

MAGI_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LIVE_RUNTIME_ROOT = (
    Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3"
)
_RUNTIME_OVERRIDE = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
RUNTIME_DIR = Path(_RUNTIME_OVERRIDE or MAGI_ROOT / ".runtime").expanduser()
DEFAULT_JSON_OUT = RUNTIME_DIR / "magi_acceptance_latest.json"
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.platforms import safe_process
from magi_v3.external_inputs import live_shared_state_environment


@dataclass
class GateResult:
    id: str
    name: str
    ok: bool
    status: str
    blocking: bool = True
    elapsed_sec: float = 0.0
    detail: str = ""
    command: list[str] = field(default_factory=list)
    artifact: str = ""
    payload: dict[str, Any] | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class CommandResult:
    command: list[str]
    returncode: int | None
    elapsed_sec: float
    stdout: str = ""
    stderr: str = ""
    payload: dict[str, Any] | None = None
    timed_out: bool = False
    cleanup_failed: bool = False


PROFILE_GATES: dict[str, list[str]] = {
    "quick": [
        "repo_clean",
        "residue_audit",
        "runtime_fingerprint",
        "doctor",
    ],
    "full": [
        "repo_clean",
        "residue_audit",
        "runtime_fingerprint",
        "doctor",
        "live_conflict_audit",
        "function_health",
        "operational_attestation",
        "cross_surface_pytest",
    ],
    "live": [
        "repo_clean",
        "residue_audit",
        "runtime_fingerprint",
        "doctor",
        "live_conflict_audit",
        "function_health",
        "operational_attestation",
        "cross_surface_pytest",
        "model_live_gate",
        "self_repair_guardian",
        "business_modules_live",
    ],
    "weekly-deep": [
        "repo_clean",
        "residue_audit",
        "runtime_fingerprint",
        "doctor",
        "live_conflict_audit",
        "function_health",
        "operational_attestation",
        "cross_surface_pytest",
        "model_live_gate",
        "self_repair_guardian",
        "business_modules_live",
        "disk_cleanup_dry_run",
        "production_live_suite",
    ],
    "commercial": [
        "repo_clean",
        "residue_audit",
        "runtime_fingerprint",
        "doctor",
        "live_conflict_audit",
        "function_health",
        "operational_attestation",
        "cross_surface_pytest",
        "model_live_gate",
        "self_repair_guardian",
        "business_modules_live",
    ],
}

COMMIT_READY_PROFILES = {"full", "live", "weekly-deep", "commercial"}

# Cross-surface pytest is an isolated unit/contract gate.  A LIVE acceptance
# process carries hash-bound deployment variables, but leaking those into
# tests that intentionally replace REPO_ROOT with temporary fixtures makes
# the fixtures resolve the production cron snapshot and fail for the wrong
# reason.  Explicit empty overrides preserve fail-closed production binding
# while keeping the child test process hermetic.
CROSS_SURFACE_TEST_ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "MAGI_GOOGLE_CALENDAR_TOKEN_PATH": "",
    "MAGI_CRON_JOBS_FILE": "",
    "MAGI_CRON_JOBS_SHA256": "",
    "MAGI_CRON_JOBS_SOURCE_SHA256": "",
    "MAGI_CRON_DEFINITIONS_IMMUTABLE": "",
    "MAGI_ROOT_DIR": "",
    "MAGI_V3_RELEASE_ID": "",
    "MAGI_RUNTIME_DIR": "",
    "MAGI_SOURCE_ROOT_DIR": "",
}

# Keep this inventory limited to tests that are shipped in the immutable
# release bundle.  Referencing historical source-only test names makes every
# installed-release acceptance fail before it exercises a single assertion.
CROSS_SURFACE_TESTS = (
    "tests/test_osc_web_smoke.py",
    "tests/test_osc_p2_discord_and_theme.py",
    "tests/v3/test_magi_doctor_v3_runtime.py",
    "tests/test_business_module_live_check.py",
    "tests/test_laf_portal_new_files_scan.py",
    "tests/test_live_gate_deployment_paths.py",
)


def _python() -> str:
    candidate = MAGI_ROOT / "venv" / "bin" / "python3"
    if candidate.exists():
        return str(candidate)
    candidate = MAGI_ROOT / "venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable or "python3"


def _live_runtime_root() -> Path:
    explicit = str(os.environ.get("MAGI_LIVE_RUNTIME_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    release_id = str(os.environ.get("MAGI_V3_RELEASE_ID") or "").strip()
    bound_root = str(os.environ.get("MAGI_ROOT_DIR") or "").strip()
    if release_id.startswith("v3-") and bound_root:
        candidate = Path(bound_root).expanduser()
        if candidate.name == release_id:
            return candidate

    marker_path = Path(
        str(os.environ.get("MAGI_V3_ACTIVE_RELEASE_MARKER") or "").strip()
        or Path.home()
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "active-release.json"
    ).expanduser()
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker_root = Path(str(marker.get("release_root") or "")).expanduser()
        marker_id = str(marker.get("release_id") or "")
        if (
            marker.get("release") == "v3"
            and marker_id.startswith("v3-")
            and marker_root.name == marker_id
            and marker_root.is_dir()
        ):
            return marker_root
    except Exception:
        pass
    return DEFAULT_LIVE_RUNTIME_ROOT


def _active_release_marker_path() -> Path:
    return Path(
        str(os.environ.get("MAGI_V3_ACTIVE_RELEASE_MARKER") or "").strip()
        or Path.home()
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "active-release.json"
    ).expanduser()


def _live_runtime_state_dir(live_root: Path) -> Path:
    """Resolve mutable runtime state without confusing it with release code."""
    if _RUNTIME_OVERRIDE:
        return RUNTIME_DIR
    try:
        if live_root.resolve() == MAGI_ROOT.resolve():
            return MAGI_ROOT / ".runtime"
    except Exception:
        pass
    marker = _active_release_marker_path()
    canonical = marker.parent / "MAGI_v3" / "shared" / "runtime"
    if canonical.is_dir():
        return canonical
    return live_root / ".runtime"


def _doctor_environment() -> dict[str, str]:
    """Bind doctor to the same release and mutable state as this gate."""
    live_root = _live_runtime_root()
    runtime_dir = _live_runtime_state_dir(live_root)
    environment = {
        "MAGI_RUNTIME_DIR": str(runtime_dir),
        "MAGI_V3_ACTIVE_RELEASE_MARKER": str(_active_release_marker_path()),
    }
    shared_root = runtime_dir.parent if runtime_dir.name == "runtime" else None
    if shared_root is not None:
        environment.update(live_shared_state_environment(shared_root))
    if live_root.name.startswith("v3-") and "/releases/v3-" in live_root.as_posix():
        environment.update(
            {
                "MAGI_V3_RELEASE_ID": live_root.name,
                "MAGI_ROOT": str(live_root),
                "MAGI_ROOT_DIR": str(live_root),
                "MAGI_LIVE_RUNTIME_ROOT": str(live_root),
            }
        )
    return environment


def _source_root() -> Path | None:
    for env_name in ("MAGI_SOURCE_ROOT_DIR", "MAGI_PUBLIC_SOURCE_ROOT_DIR"):
        raw = str(os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if (candidate / ".git").exists():
            return candidate.resolve()

    if (MAGI_ROOT / ".git").exists():
        return MAGI_ROOT.resolve()

    for candidate in (
        Path.home() / "Desktop" / "MAGI_v3",
        Path.home() / "Library" / "Application Support" / "MAGI" / "source" / "MAGI_v3",
    ):
        if (candidate / ".git").exists():
            return candidate.resolve()
    return None


def _live_runtime_artifact(name: str) -> Path:
    if _RUNTIME_OVERRIDE:
        return RUNTIME_DIR / name
    live_root = _live_runtime_root()
    try:
        if live_root.exists() and live_root.resolve() != MAGI_ROOT.resolve():
            return live_root / ".runtime" / name
    except Exception:
        pass
    return MAGI_ROOT / ".runtime" / name


def _tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else text[-limit:]


def parse_json_from_output(raw: str) -> dict[str, Any] | None:
    """Return the largest JSON object embedded in command output."""
    if not raw:
        return None
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    found_span = -1
    for idx, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(raw[idx:])
        except Exception:
            continue
        if isinstance(value, dict) and end > found_span:
            found = value
            found_span = end
    return found


def _run_command(
    command: list[str],
    *,
    timeout_sec: int,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> CommandResult:
    started = time.time()
    run_env = os.environ.copy()
    run_env["PYTHONPATH"] = str(MAGI_ROOT) + (
        os.pathsep + run_env["PYTHONPATH"] if run_env.get("PYTHONPATH") else ""
    )
    if env:
        run_env.update(env)
    try:
        proc = safe_process.run(
            command,
            cwd=cwd or MAGI_ROOT,
            timeout_sec=timeout_sec,
            env_extra=run_env,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        return CommandResult(
            command=command,
            returncode=proc.returncode,
            elapsed_sec=round(time.time() - started, 3),
            stdout=stdout,
            stderr=stderr,
            payload=parse_json_from_output(stdout) or parse_json_from_output(stderr),
            timed_out=proc.timed_out,
        )
    except Exception as exc:
        return CommandResult(
            command=command,
            returncode=None,
            elapsed_sec=round(time.time() - started, 3),
            stderr=f"{type(exc).__name__}: {exc}",
            cleanup_failed=bool(getattr(exc, "safe_process_cleanup_failed", False)),
        )


def _command_gate(
    gate_id: str,
    name: str,
    command: list[str],
    *,
    timeout_sec: int,
    evaluator: Callable[[CommandResult], tuple[bool, str, str]] | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    dry_run: bool = False,
    artifact: str = "",
) -> GateResult:
    if dry_run:
        return GateResult(
            id=gate_id,
            name=name,
            ok=True,
            status="skip",
            blocking=False,
            detail="dry-run",
            command=command,
            artifact=artifact,
        )
    result = _run_command(command, timeout_sec=timeout_sec, env=env, cwd=cwd)
    if result.cleanup_failed:
        return GateResult(
            id=gate_id,
            name=name,
            ok=False,
            status="fail",
            elapsed_sec=result.elapsed_sec,
            detail="process cleanup failed after timeout",
            command=command,
            stdout_tail=_tail(result.stdout),
            stderr_tail=_tail(result.stderr),
            artifact=artifact,
        )
    if result.timed_out:
        return GateResult(
            id=gate_id,
            name=name,
            ok=False,
            status="fail",
            elapsed_sec=result.elapsed_sec,
            detail=f"timeout after {timeout_sec}s",
            command=command,
            stdout_tail=_tail(result.stdout),
            stderr_tail=_tail(result.stderr),
            artifact=artifact,
        )
    if evaluator:
        ok, status, detail = evaluator(result)
    else:
        ok = result.returncode == 0
        status = "pass" if ok else "fail"
        detail = f"exit={result.returncode}"
    return GateResult(
        id=gate_id,
        name=name,
        ok=ok,
        status=status,
        elapsed_sec=result.elapsed_sec,
        detail=detail,
        command=command,
        artifact=artifact,
        payload=result.payload,
        stdout_tail=_tail(result.stdout),
        stderr_tail=_tail(result.stderr),
    )


def check_repo_clean(*, allow_dirty: bool = False) -> GateResult:
    started = time.time()
    source_root = _source_root()
    if source_root is None:
        return GateResult(
            "repo_clean",
            "Source Git worktree clean",
            False,
            "fail",
            elapsed_sec=round(time.time() - started, 3),
            detail="source checkout missing; set MAGI_SOURCE_ROOT_DIR",
        )
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=source_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    elapsed = round(time.time() - started, 3)
    if proc.returncode != 0:
        return GateResult(
            "repo_clean",
            "Source Git worktree clean",
            False,
            "fail",
            elapsed_sec=elapsed,
            detail=_tail(proc.stderr or proc.stdout),
        )
    dirty = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if dirty and allow_dirty:
        return GateResult(
            "repo_clean",
            "Source Git worktree clean",
            True,
            "warn",
            blocking=False,
            elapsed_sec=elapsed,
            detail=f"dirty worktree allowed for validation: {len(dirty)} path(s)",
            payload={"dirty": dirty[:30]},
        )
    return GateResult(
        "repo_clean",
        "Source Git worktree clean",
        not dirty,
        "pass" if not dirty else "fail",
        elapsed_sec=elapsed,
        detail="clean" if not dirty else f"dirty paths: {', '.join(dirty[:8])}",
        payload={"dirty": dirty[:30]},
    )


def check_residue() -> GateResult:
    started = time.time()
    findings: list[dict[str, str]] = []
    git_dir = MAGI_ROOT / ".git"
    if git_dir.exists():
        for path in sorted(git_dir.rglob("*.lock")):
            findings.append({"kind": "git_lock", "path": str(path)})
        pack_dir = git_dir / "objects" / "pack"
        if pack_dir.exists():
            for path in sorted(pack_dir.glob("tmp_pack_*")):
                findings.append({"kind": "git_tmp_pack", "path": str(path)})

    selenium_cache = Path.home() / ".cache" / "selenium"
    if selenium_cache.is_symlink() and not selenium_cache.exists():
        findings.append(
            {
                "kind": "dangling_symlink",
                "path": str(selenium_cache),
                "target": str(selenium_cache.resolve(strict=False)),
            }
        )

    ok = not findings
    return GateResult(
        "residue_audit",
        "Local residue audit",
        ok,
        "pass" if ok else "fail",
        elapsed_sec=round(time.time() - started, 3),
        detail="clean" if ok else f"{len(findings)} residue item(s)",
        payload={"findings": findings[:50]},
    )


def check_runtime_fingerprint() -> GateResult:
    started = time.time()
    try:
        from scripts import magi_doctor

        source_root = _source_root()
        if source_root is None:
            return GateResult(
                "runtime_fingerprint",
                "Source/live runtime fingerprint",
                False,
                "fail",
                elapsed_sec=round(time.time() - started, 3),
                detail="source checkout missing; set MAGI_SOURCE_ROOT_DIR",
            )
        live_root = _live_runtime_root()
        if not live_root.exists():
            return GateResult(
                "runtime_fingerprint",
                "Source/live runtime fingerprint",
                False,
                "fail",
                elapsed_sec=round(time.time() - started, 3),
                detail=f"live runtime missing: {live_root}",
            )
        if live_root.resolve() == source_root.resolve():
            return GateResult(
                "runtime_fingerprint",
                "Source/live runtime fingerprint",
                True,
                "pass",
                elapsed_sec=round(time.time() - started, 3),
                detail="same root",
            )
        fingerprint = magi_doctor._runtime_root_fingerprint(source_root, live_root)
        counts = {
            "file_mismatches": len(fingerprint.get("file_mismatches") or []),
            "missing": len(fingerprint.get("missing") or []),
            "cron_mismatches": len(fingerprint.get("cron_mismatches") or []),
        }
        ok = not any(counts.values())
        return GateResult(
            "runtime_fingerprint",
            "Source/live runtime fingerprint",
            ok,
            "pass" if ok else "fail",
            elapsed_sec=round(time.time() - started, 3),
            detail="matched" if ok else json.dumps(counts, ensure_ascii=False),
            payload=fingerprint,
        )
    except Exception as exc:
        return GateResult(
            "runtime_fingerprint",
            "Source/live runtime fingerprint",
            False,
            "fail",
            elapsed_sec=round(time.time() - started, 3),
            detail=str(exc),
        )


def check_operational_attestation(*, required: bool = False) -> GateResult:
    """Require release-bound SLO/support/DR evidence without inventing it."""
    started = time.time()
    artifact = _live_runtime_artifact("operational_attestation_latest.json")
    if not artifact.is_file() or artifact.is_symlink():
        return GateResult(
            "operational_attestation",
            "Release-bound operations SLO/DR/support attestation",
            not required,
            "fail" if required else "skip",
            blocking=required,
            elapsed_sec=round(time.time() - started, 3),
            detail="not_attested: release-bound operational evidence is unavailable",
            artifact=str(artifact),
        )
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        release_id = str(os.environ.get("MAGI_V3_RELEASE_ID") or _live_runtime_root().name)
        required_keys = {"schema_version", "release_id", "slo", "support_bundle", "dr"}
        if not isinstance(payload, dict) or not required_keys <= set(payload):
            raise ValueError("attestation contract is incomplete")
        slo = payload["slo"]
        support = payload["support_bundle"]
        dr = payload["dr"]
        valid = (
            payload["schema_version"] == 1
            and payload["release_id"] == release_id
            and isinstance(slo, dict)
            and isinstance(support, dict)
            and isinstance(dr, dict)
            and slo.get("ok") is True
            and type(slo.get("sample_size")) is int
            and slo.get("sample_size") > 0
            and support.get("schema_version") == 1
            and support.get("deidentified") is True
            and all(
                dr.get(key) is True
                for key in ("verified", "restore_receipt_valid", "rpo_ok", "rto_ok")
            )
        )
        return GateResult(
            "operational_attestation",
            "Release-bound operations SLO/DR/support attestation",
            valid,
            "pass" if valid else "fail",
            elapsed_sec=round(time.time() - started, 3),
            detail="verified" if valid else "invalid_attestation_contract",
            artifact=str(artifact),
            payload={"release_id": payload.get("release_id"), "slo_sample_size": slo.get("sample_size"), "dr_verified": dr.get("verified")},
        )
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        return GateResult(
            "operational_attestation",
            "Release-bound operations SLO/DR/support attestation",
            False,
            "fail",
            blocking=True,
            elapsed_sec=round(time.time() - started, 3),
            detail=f"invalid_attestation: evidence cannot be verified ({type(exc).__name__})",
            artifact=str(artifact),
        )
def evaluate_doctor(result: CommandResult) -> tuple[bool, str, str]:
    payload = result.payload or {}
    summary = payload.get("summary") or {}
    warn = int(summary.get("warn") or 0)
    fail = int(summary.get("fail") or 0)
    ok = result.returncode == 0 and bool(payload.get("ok")) and str(payload.get("status")) == "pass" and warn == 0 and fail == 0
    return ok, "pass" if ok else "fail", f"status={payload.get('status')} pass={summary.get('pass')} warn={warn} fail={fail}"


def evaluate_function_health(result: CommandResult) -> tuple[bool, str, str]:
    payload = result.payload or {}
    summary = payload.get("summary") or {}
    failed = int(summary.get("failed_health_count") or 0)
    stale = int(summary.get("stale_health_count") or 0)
    missing = int(summary.get("missing_health_count") or 0)
    ok = result.returncode == 0 and bool(payload.get("ok")) and failed == 0 and stale == 0 and missing == 0
    return ok, "pass" if ok else "fail", f"failed={failed} stale={stale} missing={missing}"


def evaluate_business_live(result: CommandResult) -> tuple[bool, str, str]:
    payload = result.payload
    contract_ok, contract_detail = _json_boolean_contract(payload)
    if not contract_ok:
        return False, "fail", contract_detail
    assert isinstance(payload, dict)
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return False, "fail", "missing_results_contract"
    failed = []
    for item in results:
        if not isinstance(item, dict):
            return False, "fail", "invalid_result_contract"
        item_value = item.get("ok", item.get("success"))
        if type(item_value) is not bool:
            return False, "fail", f"non_boolean_result_contract:{item.get('name') or 'unnamed'}"
        if not item_value:
            failed.append(str(item.get("name") or "unnamed"))
    ok = result.returncode == 0 and contract_ok and not failed
    return ok, "pass" if ok else "fail", "all green" if ok else "failed=" + ",".join(failed[:10])


def evaluate_conflict_audit(result: CommandResult) -> tuple[bool, str, str]:
    payload = result.payload or {}
    errors = int(payload.get("error_count") or 0)
    warnings = int(payload.get("warning_count") or 0)
    ok = result.returncode == 0 and bool(payload.get("ok")) and errors == 0 and warnings == 0
    return ok, "pass" if ok else "fail", f"errors={errors} warnings={warnings}"


def evaluate_self_repair_guardian(result: CommandResult) -> tuple[bool, str, str]:
    payload = result.payload
    contract_ok, contract_detail = _json_boolean_contract(payload)
    if not contract_ok:
        return False, "fail", contract_detail
    assert isinstance(payload, dict)
    requires_human = payload.get("requires_human")
    if not isinstance(requires_human, list):
        return False, "fail", "missing_requires_human_contract"
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return False, "fail", "missing_summary_contract"
    try:
        errors = int(summary.get("error_count") or 0)
        warnings = int(summary.get("warning_count") or 0)
    except (TypeError, ValueError):
        return False, "fail", "invalid_summary_contract"
    ok = result.returncode == 0 and contract_ok and not requires_human and errors == 0 and warnings == 0
    return ok, "pass" if ok else "fail", f"requires_human={len(requires_human)} warnings={warnings} errors={errors}"


def evaluate_generic_json_ok(result: CommandResult) -> tuple[bool, str, str]:
    contract_ok, contract_detail = _json_boolean_contract(result.payload)
    ok = result.returncode == 0 and contract_ok
    return ok, "pass" if ok else "fail", f"exit={result.returncode} {contract_detail}"


def _json_boolean_contract(payload: Any) -> tuple[bool, str]:
    """Require a non-empty JSON object with an explicit boolean success flag."""
    if not isinstance(payload, dict) or not payload:
        return False, "missing_json_object_contract"
    present = [key for key in ("ok", "success") if key in payload]
    if not present:
        return False, "missing_ok_success_contract"
    if any(type(payload[key]) is not bool for key in present):
        return False, "non_boolean_ok_success_contract"
    if not all(bool(payload[key]) for key in present):
        return False, "failed_ok_success_contract"
    return True, "ok=true"


def _write_function_health_matrix() -> Path:
    matrix_path = MAGI_ROOT / "config" / "test_matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    filtered = copy.deepcopy(matrix)
    suites = filtered.get("suites") if isinstance(filtered.get("suites"), dict) else {}
    for suite_name in list(suites):
        if str(suite_name).startswith("acceptance-"):
            suites.pop(suite_name, None)
    out = RUNTIME_DIR / "magi_acceptance_function_health_matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(filtered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def _gate_command_factory(gate_id: str, *, dry_run: bool) -> GateResult:
    py = _python()
    if gate_id == "doctor":
        return _command_gate(
            "doctor",
            "MAGI doctor 0 warn/0 fail",
            [py, "scripts/magi_doctor.py", "--json"],
            timeout_sec=60,
            evaluator=evaluate_doctor,
            env={"MAGI_RUNTIME_DIR": str(RUNTIME_DIR if _RUNTIME_OVERRIDE else _live_runtime_root() / ".runtime")},
            dry_run=dry_run,
        )
    if gate_id == "live_conflict_audit":
        artifact_path = _live_runtime_artifact("live_conflict_audit_ci_latest.json")
        return _command_gate(
            "live_conflict_audit",
            "Fast live conflict audit",
            [
                py,
                "scripts/ops/business_module_live_check.py",
                "--conflict-audit",
                "--strict-conflicts",
                "--json-out",
                str(artifact_path),
            ],
            timeout_sec=120,
            evaluator=evaluate_conflict_audit,
            dry_run=dry_run,
            artifact=str(artifact_path),
        )
    if gate_id == "function_health":
        artifact = str(RUNTIME_DIR / "magi_acceptance_function_health_latest.json") if _RUNTIME_OVERRIDE else ".runtime/magi_acceptance_function_health_latest.json"
        matrix_path = RUNTIME_DIR / "magi_acceptance_function_health_matrix.json"
        if not dry_run:
            matrix_path = _write_function_health_matrix()
        return _command_gate(
            "function_health",
            "Function health active artifacts",
            [
                py,
                "scripts/ops/function_health_index.py",
                "--compact",
                "--matrix",
                str(matrix_path),
                "--fail-on-health",
                "--json-out",
                artifact,
            ],
            timeout_sec=90,
            evaluator=evaluate_function_health,
            dry_run=dry_run,
            artifact=artifact,
        )
    if gate_id == "cross_surface_pytest":
        test_root = _source_root() or MAGI_ROOT
        return _command_gate(
            "cross_surface_pytest",
            "Cross-surface regression pytest",
            [
                py,
                "-m",
                "pytest",
                "-q",
                *(str(test_root / relative) for relative in CROSS_SURFACE_TESTS),
            ],
            timeout_sec=180,
            # The gate itself runs from the immutable LIVE release, while
            # these contract tests belong to the hash-bound source checkout.
            # Force imports to resolve from that checkout; otherwise pytest
            # collects source tests but imports production-bound modules from
            # the release cwd, leaking LIVE environment semantics into unit
            # fixtures and producing a false multi-module failure.
            env={
                **{key: "" for key in os.environ if key.startswith("MAGI_")},
                **CROSS_SURFACE_TEST_ENV,
                "PYTHONPATH": str(test_root),
            },
            cwd=test_root,
            dry_run=dry_run,
        )
    if gate_id == "model_live_gate":
        artifact = str(RUNTIME_DIR / "magi_acceptance_model_live_gate_latest.json") if _RUNTIME_OVERRIDE else ".runtime/magi_acceptance_model_live_gate_latest.json"
        return _command_gate(
            "model_live_gate",
            "Day/night model live gate",
            [py, "scripts/ops/model_live_gate.py", "--expect", "auto", "--json", "--json-out", artifact],
            timeout_sec=90,
            evaluator=evaluate_generic_json_ok,
            dry_run=dry_run,
            artifact=artifact,
        )
    if gate_id == "self_repair_guardian":
        return _command_gate(
            "self_repair_guardian",
            "Self-repair guardian no human-required issues",
            [py, "scripts/ops/magi_self_repair_guardian.py", "--mode", "audit", "--compact"],
            timeout_sec=180,
            evaluator=evaluate_self_repair_guardian,
            dry_run=dry_run,
        )
    if gate_id == "business_modules_live":
        artifact = str(RUNTIME_DIR / "magi_acceptance_business_live_latest.json") if _RUNTIME_OVERRIDE else ".runtime/magi_acceptance_business_live_latest.json"
        return _command_gate(
            "business_modules_live",
            "Business modules LIVE",
            [py, "scripts/ops/business_module_live_check.py", "--json-out", artifact],
            timeout_sec=1200,
            evaluator=evaluate_business_live,
            env={**_doctor_environment(), "MAGI_BUSINESS_LIVE_CHECK_NOTIFY": "0"},
            dry_run=dry_run,
            artifact=artifact,
        )
    if gate_id == "disk_cleanup_dry_run":
        return _command_gate(
            "disk_cleanup_dry_run",
            "Disk cleanup dry-run",
            [py, "scripts/ops/disk_cleanup_healthcheck.py", "--dry-run"],
            timeout_sec=300,
            evaluator=evaluate_generic_json_ok,
            dry_run=dry_run,
        )
    if gate_id == "production_live_suite":
        artifact = str(RUNTIME_DIR / "magi_acceptance_production_live_latest.json") if _RUNTIME_OVERRIDE else ".runtime/magi_acceptance_production_live_latest.json"
        return _command_gate(
            "production_live_suite",
            "Production-live suite",
            [py, "scripts/ops/run_test_suite.py", "--suite", "production-live", "--json-out", artifact],
            timeout_sec=2400,
            evaluator=evaluate_generic_json_ok,
            env={"MAGI_BUSINESS_LIVE_CHECK_NOTIFY": "0"},
            dry_run=dry_run,
            artifact=artifact,
        )
    raise KeyError(f"unknown command gate: {gate_id}")


def run_gate(
    gate_id: str, *, allow_dirty: bool, dry_run: bool, require_operational_attestation: bool = False
) -> GateResult:
    if gate_id == "repo_clean":
        return check_repo_clean(allow_dirty=allow_dirty)
    if gate_id == "residue_audit":
        return check_residue()
    if gate_id == "runtime_fingerprint":
        return check_runtime_fingerprint()
    if gate_id == "operational_attestation":
        return check_operational_attestation(required=require_operational_attestation)
    return _gate_command_factory(gate_id, dry_run=dry_run)


def verdict(gates: list[GateResult]) -> str:
    if any((not gate.ok) and gate.blocking for gate in gates):
        return "RED"
    if any(gate.status in {"warn", "skip"} for gate in gates):
        return "YELLOW"
    return "GREEN"


def build_report(profile: str, gates: list[GateResult], *, allow_dirty: bool, dry_run: bool) -> dict[str, Any]:
    state = verdict(gates)
    commit_ready = state == "GREEN" and profile in COMMIT_READY_PROFILES and not allow_dirty and not dry_run
    failed = [gate.id for gate in gates if not gate.ok and gate.blocking]
    warnings = [gate.id for gate in gates if gate.status == "warn"]
    skipped = [gate.id for gate in gates if gate.status == "skip"]
    return {
        "version": "1.1",
        "profile": profile,
        "status": state,
        "ok": state == "GREEN",
        "commit_ready": commit_ready,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(MAGI_ROOT),
        "boundary": {
            "green_requires": [
                "source repo clean",
                "source/live runtime fingerprint matched",
                "doctor has 0 warn and 0 fail",
                "no local residue such as stale git locks or dangling cache symlinks",
                "fast live conflict audit has 0 warning and 0 error on full/live profiles",
                "all profile gates pass",
                "no unclassified warnings",
            ],
            "profiles": PROFILE_GATES,
        },
        "summary": {
            "total": len(gates),
            "passed": sum(1 for gate in gates if gate.ok and gate.status == "pass"),
            "warnings": len(warnings),
            "failed": len(failed),
            "skipped": len(skipped),
            "failed_gates": failed,
            "warning_gates": warnings,
            "skipped_gates": skipped,
        },
        "gates": [asdict(gate) for gate in gates],
    }


def run_acceptance(profile: str, *, allow_dirty: bool = False, dry_run: bool = False) -> dict[str, Any]:
    if profile not in PROFILE_GATES:
        known = ", ".join(sorted(PROFILE_GATES))
        raise SystemExit(f"Unknown profile '{profile}'. Known profiles: {known}")
    gates: list[GateResult] = []
    gate_ids = PROFILE_GATES[profile]
    for index, gate_id in enumerate(gate_ids):
        result = run_gate(
            gate_id,
            allow_dirty=allow_dirty,
            dry_run=dry_run,
            require_operational_attestation=profile == "commercial",
        )
        gates.append(result)
        if (
            profile in {"live", "weekly-deep"}
            and gate_id in {"repo_clean", "runtime_fingerprint"}
            and not result.ok
        ):
            for blocked_id in gate_ids[index + 1 :]:
                gates.append(
                    GateResult(
                        id=blocked_id,
                        name=blocked_id,
                        ok=True,
                        status="skip",
                        blocking=False,
                        detail=f"blocked_by_drift:{gate_id}",
                    )
                )
            break
    return build_report(profile, gates, allow_dirty=allow_dirty, dry_run=dry_run)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute():
        if _RUNTIME_OVERRIDE:
            relative = Path(*path.parts[1:]) if path.parts and path.parts[0] == ".runtime" else path
            path = RUNTIME_DIR / relative
        else:
            path = MAGI_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _print_human(report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    print(
        "MAGI Acceptance "
        f"profile={report.get('profile')} status={report.get('status')} "
        f"commit_ready={report.get('commit_ready')} "
        f"passed={summary.get('passed')} warnings={summary.get('warnings')} "
        f"failed={summary.get('failed')} skipped={summary.get('skipped')}"
    )
    for gate in report.get("gates") or []:
        marker = "PASS" if gate.get("ok") and gate.get("status") == "pass" else gate.get("status", "").upper()
        print(f"[{marker}] {gate.get('id')}: {gate.get('detail')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the MAGI acceptance boundary gate.")
    parser.add_argument("--profile", choices=sorted(PROFILE_GATES), default="quick")
    parser.add_argument("--json", action="store_true", help="Print full JSON report.")
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT), help="Write JSON report path.")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow a dirty worktree as a non-blocking warning.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve command gates without executing them.")
    args = parser.parse_args(argv)

    report = run_acceptance(args.profile, allow_dirty=args.allow_dirty, dry_run=args.dry_run)
    if args.json_out:
        _write_json(Path(args.json_out), report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(report)
    return 0 if report.get("status") == "GREEN" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
