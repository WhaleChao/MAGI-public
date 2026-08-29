#!/usr/bin/env python3
"""MAGI commercial-readiness live gate.

This gate is intentionally conservative but non-destructive. It verifies the
pieces needed before calling a MAGI checkout commercially usable:

- beginner install/doctor paths are present
- public-release audit is clean
- long-running service hygiene is clean
- local DB backup works and restore remains confirmation-gated, unless skipped
  for a public installability-only checkout
- stability observer can produce a current snapshot

The script does not restore a production DB and does not submit any portal
forms. It writes a JSON report under .runtime by default.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
_RUNTIME_OVERRIDE = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
RUNTIME_DIR = Path(_RUNTIME_OVERRIDE or MAGI_ROOT / ".runtime").expanduser()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))
_DOTENV_LOADED = False


def _load_runtime_env() -> Check | None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return None
    configured = os.environ.get("MAGI_ENV_FILE", "").strip()
    expected = os.environ.get("MAGI_ENV_FILE_SHA256", "").strip().lower()
    path = Path(configured).expanduser() if configured else MAGI_ROOT / ".env"
    if _installed_release():
        if not configured or not expected:
            return Check(
                "runtime_env_binding",
                False,
                "fail",
                "missing deployed MAGI_ENV_FILE or MAGI_ENV_FILE_SHA256 binding",
            )
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            return Check(
                "runtime_env_binding",
                False,
                "fail",
                "deployed environment binding is not a regular absolute file",
            )
        if not _SHA256_RE.fullmatch(expected) or _sha256(path) != expected:
            return Check(
                "runtime_env_binding",
                False,
                "fail",
                "deployed environment binding digest mismatch",
            )
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
    except Exception as exc:
        return Check(
            "runtime_env_binding",
            False,
            "fail",
            f"environment binding could not be loaded: {type(exc).__name__}",
        )
    _DOTENV_LOADED = True
    if _installed_release():
        return Check(
            "runtime_env_binding",
            True,
            "pass",
            f"env={path.name} sha256={expected}",
            artifact=str(path),
        )
    return None


def _is_git_worktree(root: Path) -> bool:
    return (root / ".git").exists()


def _installed_release() -> bool:
    return (
        os.environ.get("MAGI_V3_DEPLOYMENT_MODE", "").strip().lower()
        == "production"
        and (MAGI_ROOT / "release-manifest.json").is_file()
        and (MAGI_ROOT / "RELEASE_COMPLETE.json").is_file()
    )


def public_source_root() -> Path:
    """Return the git checkout used for public release/installability checks.

    Installed runtime trees intentionally contain private caches and usually do
    not include .git. Public release checks must inspect the candidate source
    checkout, while live health checks continue to run against MAGI_ROOT.
    """

    for env_name in ("MAGI_PUBLIC_SOURCE_ROOT_DIR", "MAGI_SOURCE_ROOT_DIR"):
        raw = os.environ.get(env_name)
        if not raw:
            continue
        candidate = Path(raw).expanduser().resolve()
        if _is_git_worktree(candidate):
            return candidate

    if _is_git_worktree(MAGI_ROOT):
        return MAGI_ROOT

    for candidate in (
        Path.home() / "Desktop" / "MAGI_v3",
        Path.home() / "Library" / "Application Support" / "MAGI" / "source" / "MAGI_v3",
    ):
        candidate = candidate.resolve()
        if _is_git_worktree(candidate):
            return candidate

    return MAGI_ROOT


@dataclass
class Check:
    name: str
    ok: bool
    status: str
    detail: str = ""
    elapsed_sec: float = 0.0
    artifact: str = ""


def _python() -> str:
    candidate = MAGI_ROOT / "venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    candidate = MAGI_ROOT / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable or "python3"


def _run_json(
    cmd: list[str],
    *,
    timeout: int = 120,
    allow_nonzero: bool = False,
    cwd: Path | None = None,
) -> tuple[bool, dict[str, Any], str, float]:
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=cwd or MAGI_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    elapsed = round(time.time() - started, 3)
    raw = (proc.stdout or "").strip()
    parsed: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            # Some legacy scripts print logs before JSON. Try the last JSON object.
            idx = raw.rfind("\n{")
            if idx >= 0:
                try:
                    parsed = json.loads(raw[idx + 1 :])
                except Exception:
                    parsed = {}
    ok = (proc.returncode == 0 or allow_nonzero) and bool(parsed)
    return ok, parsed, raw[-2000:], elapsed


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def check_deployment_bindings() -> Check:
    """Verify non-secret bindings required by an installed V3 release.

    A manual shell does not inherit the launchd service environment.  Do not
    guess a cron file from a checkout (that would weaken release binding), and
    never scrape another process environment.  Instead accept only the three
    explicitly deployed bindings and emit a machine-readable failure when they
    are absent or drifted.  This turns a previous import traceback into an
    actionable gate result.
    """

    required = (
        "MAGI_CRON_JOBS_FILE",
        "MAGI_CRON_JOBS_SHA256",
        "MAGI_CRON_JOBS_SOURCE_SHA256",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        return Check(
            "deployment_bindings",
            False,
            "fail",
            "missing deployed binding: " + ", ".join(missing),
        )
    path = Path(os.environ["MAGI_CRON_JOBS_FILE"]).expanduser()
    digest = os.environ["MAGI_CRON_JOBS_SHA256"].strip().lower()
    source_digest = os.environ["MAGI_CRON_JOBS_SOURCE_SHA256"].strip().lower()
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        return Check("deployment_bindings", False, "fail", "deployed cron binding is not a regular absolute file")
    if not _SHA256_RE.fullmatch(digest) or not _SHA256_RE.fullmatch(source_digest):
        return Check("deployment_bindings", False, "fail", "deployed cron digest binding is invalid")
    actual = _sha256(path)
    if actual != digest:
        return Check("deployment_bindings", False, "fail", "deployed cron binding digest mismatch")
    return Check(
        "deployment_bindings",
        True,
        "pass",
        f"cron={path.name} sha256={actual} source_sha256={source_digest}",
        artifact=str(path),
    )


def check_doctor(py: str) -> Check:
    ok, payload, raw, elapsed = _run_json([py, "scripts/magi_doctor.py", "--json"], timeout=45)
    if not ok:
        return Check("doctor", False, "fail", raw, elapsed)
    summary = payload.get("summary") or {}
    # Warnings are acceptable for optional accelerators on public/new installs;
    # failures are not.
    passed = int(summary.get("fail") or 0) == 0 and bool(payload.get("ok"))
    return Check("doctor", passed, "pass" if passed else "fail", json.dumps(summary, ensure_ascii=False), elapsed)


def check_installer_dry_run(py: str) -> Check:
    ok, payload, raw, elapsed = _run_json(
        [py, "scripts/install_magi.py", "--dry-run", "--no-optional", "--json"],
        timeout=45,
    )
    if not ok:
        return Check("installer_dry_run", False, "fail", raw, elapsed)
    steps = [str(s.get("name") or "") for s in payload.get("plan") or [] if isinstance(s, dict)]
    needed = {"create_venv", "install_core", "seed_cron_jobs", "doctor"}
    passed = bool(payload.get("ok")) and needed.issubset(set(steps))
    return Check("installer_dry_run", passed, "pass" if passed else "fail", ",".join(steps), elapsed)


def check_public_release_audit(
    py: str,
    *,
    strict: bool,
    audit_root: Path | None = None,
    require_git: bool = True,
) -> Check:
    source_root = (audit_root or public_source_root()).resolve()
    if require_git and not _is_git_worktree(source_root):
        return Check(
            "public_release_audit",
            False,
            "fail",
            f"source checkout is not a git worktree: {source_root}",
        )
    cmd = [py, "scripts/public_release_audit.py", "--json", "--root", str(source_root)]
    if strict:
        cmd.extend(["--public-isolation", "--strict"])
    ok, payload, raw, elapsed = _run_json(cmd, timeout=60, cwd=source_root)
    if not ok:
        return Check("public_release_audit", False, "fail", raw, elapsed)
    passed = bool(payload.get("ok"))
    detail = f"errors={payload.get('errors')} warnings={payload.get('warnings')}"
    return Check("public_release_audit", passed, "pass" if passed else "fail", detail, elapsed)


def _snapshot_current_worktree(dest: Path, *, source_root: Path | None = None) -> dict[str, Any]:
    """Copy the current candidate tree, including uncommitted non-ignored files."""
    source_root = (source_root or public_source_root()).resolve()
    proc = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--modified",
            "--others",
            "--exclude-standard",
        ],
        cwd=source_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or b"git ls-files failed").decode("utf-8", errors="replace")[-500:])

    seen: set[str] = set()
    copied = 0
    skipped = 0
    dest.mkdir(parents=True, exist_ok=True)
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="surrogateescape")
        if rel in seen:
            continue
        seen.add(rel)
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts or rel_path.parts[:1] == (".git",):
            skipped += 1
            continue
        src = source_root / rel_path
        target = dest / rel_path
        if not src.exists() or src.is_dir():
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            if target.exists() or target.is_symlink():
                target.unlink()
            os.symlink(os.readlink(src), target)
        else:
            shutil.copy2(src, target)
        copied += 1
    return {"copied_files": copied, "skipped_files": skipped}


def check_public_cleanroom_install(py: str) -> Check:
    started = time.time()
    source_root = public_source_root()
    if not _is_git_worktree(source_root):
        return Check(
            "public_cleanroom_install",
            False,
            "fail",
            f"source checkout is not a git worktree: {source_root}",
            round(time.time() - started, 3),
        )
    head_proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=source_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    head = (head_proc.stdout or "").strip() or "unknown"

    tmp_root = Path(tempfile.mkdtemp(prefix="magi_public_cleanroom_"))
    worktree = tmp_root / "MAGI-public-cleanroom"
    try:
        try:
            snapshot = _snapshot_current_worktree(worktree, source_root=source_root)
        except Exception as exc:
            return Check(
                "public_cleanroom_install",
                False,
                "fail",
                f"worktree snapshot failed: {type(exc).__name__}: {exc}",
                round(time.time() - started, 3),
            )

        ok, audit, raw, _elapsed = _run_json(
            [
                py,
                "scripts/public_release_audit.py",
                "--public-isolation",
                "--strict",
                "--json",
            ],
            timeout=180,
            cwd=worktree,
        )
        if not ok or not audit.get("ok"):
            detail = raw or json.dumps(audit, ensure_ascii=False)
            return Check(
                "public_cleanroom_install",
                False,
                "fail",
                "cleanroom public audit failed: " + detail[-700:],
                round(time.time() - started, 3),
            )

        output = worktree / ".runtime" / "customer_install_cleanroom_latest.json"
        ok, wizard, raw, _elapsed = _run_json(
            [
                py,
                "scripts/customer_install_wizard.py",
                "--public",
                "--no-live",
                "--skip-readiness",
                "--no-optional",
                "--json",
                "--output",
                str(output),
            ],
            timeout=240,
            cwd=worktree,
        )
        summary = wizard.get("summary") if isinstance(wizard.get("summary"), dict) else {}
        passed = ok and bool(wizard.get("ok")) and int(summary.get("fail") or 0) == 0
        detail = (
            f"source={source_root} head={head} files={snapshot.get('copied_files')} "
            f"audit=errors:{audit.get('errors')} warnings:{audit.get('warnings')} "
            f"wizard_status={wizard.get('status')} pass={summary.get('pass')} skipped={summary.get('skipped')}"
        )
        if not passed:
            detail += " " + (raw or json.dumps(wizard, ensure_ascii=False))[-700:]
        return Check(
            "public_cleanroom_install",
            passed,
            "pass" if passed else "fail",
            detail,
            round(time.time() - started, 3),
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def check_process_hygiene(py: str) -> Check:
    ok, payload, raw, elapsed = _run_json([py, "skills/process-hygiene/action.py", "--task", "scan"], timeout=45)
    if not ok:
        return Check("process_hygiene", False, "fail", raw, elapsed)
    passed = bool(payload.get("healthy")) and int(payload.get("total_issues") or 0) == 0
    return Check("process_hygiene", passed, "pass" if passed else "fail", payload.get("message", "")[:500], elapsed)


def check_db_backup_drill(py: str, *, skip_backup: bool) -> Check:
    try:
        from skills.ops.database import backup_restore
    except Exception as exc:
        return Check("db_backup_drill", False, "fail", f"import failed: {type(exc).__name__}: {exc}")

    configured = os.environ.get("MAGI_DB_BACKUP_DIR", "").strip()
    shared_state = os.environ.get("MAGI_SHARED_STATE_DIR", "").strip()
    out_dir = Path(
        configured
        or (str(Path(shared_state) / "db-backups" / "law_firm_data") if shared_state else "")
        or backup_restore.DEFAULT_BACKUP_DIR
    ).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    backup_payload: dict[str, Any] | None = None
    if not skip_backup:
        ok, payload, raw, elapsed = _run_json(
            [
                py,
                "skills/ops/database/backup_restore.py",
                "--task",
                "backup",
                "--target",
                "local",
                "--output-dir",
                str(out_dir),
            ],
            timeout=420,
        )
        if not ok or not payload.get("ok"):
            return Check("db_backup_drill", False, "fail", raw or json.dumps(payload, ensure_ascii=False), elapsed)
        backup_payload = payload

    backups = backup_restore.run_list(out_dir, 5)
    local_items = [row for row in backups.get("items", []) if row.get("target") in {"local", "local_pre_restore"}]
    if not local_items:
        return Check("db_backup_drill", False, "fail", f"no local backups in {out_dir}")

    latest = Path(str(local_items[0].get("path") or ""))
    if not latest.exists():
        return Check("db_backup_drill", False, "fail", f"backup missing: {latest}")

    try:
        with gzip.open(latest, "rb") as f:
            while f.read(1024 * 1024):
                pass
    except Exception as exc:
        return Check("db_backup_drill", False, "fail", f"gzip verification failed: {type(exc).__name__}: {exc}", artifact=str(latest))

    expected_sha = str(local_items[0].get("sha256") or "").strip()
    actual_sha = _sha256(latest)
    if expected_sha and expected_sha != actual_sha:
        return Check("db_backup_drill", False, "fail", "sha256 mismatch", artifact=str(latest))

    restore_gate = backup_restore.run_restore(
        file_path=latest,
        restore_target="local",
        out_dir=out_dir,
        pre_backup=False,
        keep_days=30,
        confirmed=False,
    )
    if restore_gate.get("error") != "confirm_required":
        return Check("db_backup_drill", False, "fail", "restore confirmation gate missing", artifact=str(latest))

    detail = f"backup={latest.name} bytes={latest.stat().st_size} restore_gate=confirm_required"
    if backup_payload:
        detail += f" created_items={len(backup_payload.get('items') or [])}"
    return Check("db_backup_drill", True, "pass", detail, artifact=str(latest))


def check_stability_observer(py: str) -> Check:
    ok, payload, raw, elapsed = _run_json(
        [py, "scripts/ops/observe_stability_24h.py", "--once", "--hours", "24", "--interval-sec", "30"],
        timeout=90,
    )
    if not ok:
        return Check("stability_observer_once", False, "fail", raw, elapsed)
    passed = bool(payload.get("success"))
    artifact = str(payload.get("snapshot_path") or payload.get("txt_export") or "")
    return Check("stability_observer_once", passed, "pass" if passed else "fail", "24h window snapshot generated", elapsed, artifact)


def check_resource_governor(py: str) -> Check:
    ok, payload, raw, elapsed = _run_json(
        [py, "scripts/ops/resource_governor.py", "--json", "status"],
        timeout=45,
        allow_nonzero=True,
    )
    if not ok:
        return Check("resource_governor", False, "fail", raw, elapsed)
    level = str(payload.get("level") or "unknown")
    snap = payload.get("snapshot") or {}
    detail = (
        f"level={level} disk_free={snap.get('disk_free_gb')}GB "
        f"swap={snap.get('swap_used_gb')}GB free_plus_inactive={snap.get('free_plus_inactive_gb')}GB"
    )
    # throttle/core_only are operational warnings, not release blockers by themselves.
    passed = level != "critical"
    return Check("resource_governor", passed, "pass" if level == "normal" else ("warn" if passed else "fail"), detail, elapsed)


def check_model_live_gate(py: str) -> Check:
    ok, payload, raw, elapsed = _run_json(
        [
            py,
            "scripts/ops/model_live_gate.py",
            "--expect",
            "auto",
            "--json",
            "--json-out",
            str(RUNTIME_DIR / "model_live_gate_latest.json"),
        ],
        timeout=45,
    )
    if not ok:
        return Check("model_live_gate", False, "fail", raw, elapsed)
    endpoints = payload.get("endpoints") or []
    endpoint_text = ", ".join(
        f"{e.get('port')}={e.get('model_id') or 'down'}"
        for e in endpoints
        if isinstance(e, dict)
    )
    passed = bool(payload.get("ok"))
    status = "pass" if passed and not payload.get("degraded") else ("warn" if passed else "fail")
    detail = (
        f"expected={payload.get('expected_profile')} active={payload.get('active_profile')} "
        f"degraded={payload.get('degraded')} endpoints=[{endpoint_text}]"
    )
    if payload.get("failures"):
        detail += " failures=" + "; ".join(str(x) for x in payload.get("failures") or [])
    return Check("model_live_gate", passed, status, detail, elapsed, artifact=str(RUNTIME_DIR / "model_live_gate_latest.json"))


def check_live_conflict_audit(py: str) -> Check:
    started = time.time()
    try:
        from scripts.ops.business_module_live_check import audit_live_conflicts
    except Exception as exc:
        return Check("live_conflict_audit", False, "fail", f"import failed: {type(exc).__name__}: {exc}")
    payload = audit_live_conflicts(MAGI_ROOT)
    elapsed = round(time.time() - started, 3)
    errors = int(payload.get("error_count") or 0)
    warnings = int(payload.get("warning_count") or 0)
    detail = f"errors={errors} warnings={warnings}"
    passed = bool(payload.get("ok"))
    return Check("live_conflict_audit", passed, "pass" if passed else "fail", detail, elapsed)


def check_formal_saas_readiness(py: str) -> Check:
    started = time.time()
    try:
        from api.saas_readiness import build_saas_readiness
    except Exception as exc:
        return Check("formal_saas_readiness", False, "fail", f"import failed: {type(exc).__name__}: {exc}")
    payload = build_saas_readiness(root=MAGI_ROOT, db_config={
        "host": os.environ.get("DB_HOST") or os.environ.get("MYSQL_HOST") or "",
        "port": int(os.environ.get("DB_PORT") or os.environ.get("MYSQL_PORT") or "3306"),
        "user": os.environ.get("DB_USER") or os.environ.get("MYSQL_USER") or "",
        "password": os.environ.get("DB_PASSWORD") or os.environ.get("MYSQL_PASSWORD") or "",
        "database": os.environ.get("DB_NAME") or "magi_brain",
    })
    elapsed = round(time.time() - started, 3)
    failed = int((payload.get("summary") or {}).get("failed_required") or 0)
    mode = str(payload.get("mode") or "")
    detail = f"mode={mode} failed_required={failed} failed_keys={','.join(payload.get('failed_keys') or [])}"
    if mode != "formal_saas":
        return Check("formal_saas_readiness", True, "warn", "formal SaaS mode not enabled; " + detail, elapsed)
    return Check("formal_saas_readiness", bool(payload.get("ok")), "pass" if payload.get("ok") else "fail", detail, elapsed)


def live_validation_commands(py: str | None = None) -> dict[str, list[str]]:
    py = py or _python()
    def _artifact(name: str) -> str:
        return str(RUNTIME_DIR / name) if _RUNTIME_OVERRIDE else f".runtime/{name}"

    return {
        "production_live": [
            py,
            "scripts/ops/run_test_suite.py",
            "--suite",
            "production-live",
            "--json-out",
            _artifact("production_live_latest.json"),
        ],
        "business_modules": [
            py,
            "scripts/ops/business_module_live_check.py",
            "--json",
            "--json-out",
            _artifact("business_module_live_check_latest.json"),
        ],
        "conflict_audit": [
            py,
            "scripts/ops/business_module_live_check.py",
            "--conflict-audit",
            "--json-out",
            _artifact("live_conflict_audit_latest.json"),
        ],
        "manual_probe": [
            "curl",
            "-fsS",
            "http://127.0.0.1:${MAGI_SERVER_PORT:-5002}/health",
        ],
    }


def run_gate(*, json_out: Path, strict_public: bool, skip_backup: bool, skip_db: bool) -> dict[str, Any]:
    # The scheduler body campaign must exercise this real entrypoint without
    # touching host services.  The production gate below intentionally probes
    # live model/process state, so it is not a valid Seatbelt fixture.  Keep a
    # small, explicit release-gate fixture branch here rather than letting the
    # offline validator accidentally certify host-dependent checks (or fail on
    # sandbox-denied localhost access).
    if (
        os.environ.get("MAGI_V3_REALISM_SANDBOX") == "1"
        and os.environ.get("MAGI_V3_SCHEDULE_FIXTURE") == "1"
    ):
        py = _python()
        checks = [
            check_doctor(py),
            check_installer_dry_run(py),
            # A release bundle intentionally has no .git directory.  The
            # normal live gate must audit the public git checkout, while the
            # Seatbelt body must audit the exact release tree being executed.
            # Use public_release_audit's bounded filesystem walker explicitly
            # instead of applying the old git-worktree precondition here.
            check_public_release_audit(
                py,
                strict=True,
                audit_root=MAGI_ROOT,
                require_git=False,
            ),
        ]
        passed = sum(1 for check in checks if check.ok)
        payload = {
            "schema": "magi.v3.commercial-readiness-schedule-fixture/v1",
            "schedule_fixture": True,
            "ok": passed == len(checks),
            "summary": {"pass": passed, "fail": len(checks) - passed, "total": len(checks)},
            "checks": [asdict(check) for check in checks],
            "omitted_host_checks": [
                "formal_saas_readiness",
                "live_conflict_audit",
                "process_hygiene",
                "resource_governor",
                "model_live_gate",
                "stability_observer_once",
            ],
        }
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["json_out"] = str(json_out)
        return payload
    runtime_binding = _load_runtime_env()
    py = _python()
    binding = check_deployment_bindings() if _installed_release() else None
    binding_checks = [item for item in (binding, runtime_binding) if item is not None]
    if any(not item.ok for item in binding_checks):
        # These probes import release-bound scheduler/runtime configuration.
        # Report every omitted verification explicitly instead of allowing an
        # unbound import to terminate the whole gate with a traceback.
        blocked = "blocked: deployment_bindings must pass before release-bound checks"
        checks = [
            *binding_checks,
            Check("doctor", False, "fail", blocked),
            Check("live_conflict_audit", False, "fail", blocked),
            Check("process_hygiene", False, "fail", blocked),
            Check("resource_governor", False, "fail", blocked),
            Check("model_live_gate", False, "fail", blocked),
            Check("stability_observer_once", False, "fail", blocked),
        ]
        if not skip_db:
            checks.append(Check("db_backup_drill", False, "fail", blocked))
        payload = {
            "ok": False,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "root": str(MAGI_ROOT),
            "public_source_root": str(public_source_root()),
            "python": py,
            "summary": {"pass": 0, "fail": len(checks), "total": len(checks)},
            "checks": [asdict(c) for c in checks],
            "live_validation_commands": live_validation_commands(py),
        }
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["json_out"] = str(json_out)
        return payload

    checks = binding_checks + [check_doctor(py)]
    # Production autonomy must not depend on a mutable Git checkout.  Source
    # packaging remains mandatory for source/strict-public release runs.
    if not _installed_release() or strict_public:
        checks.extend(
            [
                check_installer_dry_run(py),
                check_public_release_audit(py, strict=strict_public),
                check_public_cleanroom_install(py),
            ]
        )
    else:
        checks.append(
            Check(
                "immutable_release_contract",
                True,
                "pass",
                "sealed production release; source packaging is not a runtime dependency",
            )
        )
    checks.extend(
        [
            check_formal_saas_readiness(py),
            check_live_conflict_audit(py),
            check_process_hygiene(py),
            check_resource_governor(py),
            check_model_live_gate(py),
            check_stability_observer(py),
        ]
    )
    if not skip_db:
        checks.insert(4, check_db_backup_drill(py, skip_backup=skip_backup))
    passed = sum(1 for c in checks if c.ok)
    failed = len(checks) - passed
    payload = {
        "ok": failed == 0,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(MAGI_ROOT),
        "public_source_root": str(public_source_root()),
        "python": py,
        "summary": {"pass": passed, "fail": failed, "total": len(checks)},
        "checks": [asdict(c) for c in checks],
        "live_validation_commands": live_validation_commands(py),
    }
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["json_out"] = str(json_out)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MAGI commercial-readiness live gate.")
    parser.add_argument("--json-out", default=str(RUNTIME_DIR / "commercial_readiness_live_latest.json"))
    parser.add_argument("--strict-public", action="store_true", help="treat public audit warnings as failures")
    parser.add_argument("--skip-backup", action="store_true", help="verify latest backup only; do not create a new local backup")
    parser.add_argument("--skip-db", action="store_true", help="skip DB backup drill for public/installability-only checkouts")
    args = parser.parse_args()

    payload = run_gate(
        json_out=(
            RUNTIME_DIR / Path(*Path(args.json_out).parts[1:])
            if _RUNTIME_OVERRIDE and not Path(args.json_out).is_absolute() and Path(args.json_out).parts and Path(args.json_out).parts[0] == ".runtime"
            else Path(args.json_out)
        ),
        strict_public=bool(args.strict_public),
        skip_backup=bool(args.skip_backup),
        skip_db=bool(args.skip_db),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
