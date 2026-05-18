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
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))


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


def check_public_release_audit(py: str, *, strict: bool) -> Check:
    cmd = [py, "scripts/public_release_audit.py", "--json"]
    if strict:
        cmd.append("--strict")
    ok, payload, raw, elapsed = _run_json(cmd, timeout=60)
    if not ok:
        return Check("public_release_audit", False, "fail", raw, elapsed)
    passed = bool(payload.get("ok"))
    detail = f"errors={payload.get('errors')} warnings={payload.get('warnings')}"
    return Check("public_release_audit", passed, "pass" if passed else "fail", detail, elapsed)


def check_public_cleanroom_install(py: str) -> Check:
    started = time.time()
    head_proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=MAGI_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    head = (head_proc.stdout or "").strip() or "unknown"

    tmp_root = Path(tempfile.mkdtemp(prefix="magi_public_cleanroom_"))
    worktree = tmp_root / "MAGI-public-cleanroom"
    try:
        clone = subprocess.run(
            ["git", "clone", "--local", "--no-hardlinks", "--quiet", str(MAGI_ROOT), str(worktree)],
            cwd=MAGI_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            check=False,
        )
        if clone.returncode != 0:
            return Check(
                "public_cleanroom_install",
                False,
                "fail",
                f"clone failed: {(clone.stdout or '')[-500:]}",
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
            f"head={head} audit=errors:{audit.get('errors')} warnings:{audit.get('warnings')} "
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

    out_dir = Path(os.environ.get("MAGI_DB_BACKUP_DIR", backup_restore.DEFAULT_BACKUP_DIR))
    out_dir.mkdir(parents=True, exist_ok=True)
    backup_payload: dict[str, Any] | None = None
    if not skip_backup:
        ok, payload, raw, elapsed = _run_json(
            [py, "skills/ops/database/backup_restore.py", "--task", "backup", "--target", "local"],
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
            ".runtime/model_live_gate_latest.json",
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
    return Check("model_live_gate", passed, status, detail, elapsed, artifact=str(MAGI_ROOT / ".runtime" / "model_live_gate_latest.json"))


def run_gate(*, json_out: Path, strict_public: bool, skip_backup: bool, skip_db: bool) -> dict[str, Any]:
    py = _python()
    checks = [
        check_doctor(py),
        check_installer_dry_run(py),
        check_public_release_audit(py, strict=strict_public),
        check_public_cleanroom_install(py),
        check_process_hygiene(py),
        check_resource_governor(py),
        check_model_live_gate(py),
        check_stability_observer(py),
    ]
    if not skip_db:
        checks.insert(4, check_db_backup_drill(py, skip_backup=skip_backup))
    passed = sum(1 for c in checks if c.ok)
    failed = len(checks) - passed
    payload = {
        "ok": failed == 0,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(MAGI_ROOT),
        "python": py,
        "summary": {"pass": passed, "fail": failed, "total": len(checks)},
        "checks": [asdict(c) for c in checks],
    }
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["json_out"] = str(json_out)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MAGI commercial-readiness live gate.")
    parser.add_argument("--json-out", default=str(MAGI_ROOT / ".runtime" / "commercial_readiness_live_latest.json"))
    parser.add_argument("--strict-public", action="store_true", help="treat public audit warnings as failures")
    parser.add_argument("--skip-backup", action="store_true", help="verify latest backup only; do not create a new local backup")
    parser.add_argument("--skip-db", action="store_true", help="skip DB backup drill for public/installability-only checkouts")
    args = parser.parse_args()

    payload = run_gate(
        json_out=Path(args.json_out),
        strict_public=bool(args.strict_public),
        skip_backup=bool(args.skip_backup),
        skip_db=bool(args.skip_db),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
