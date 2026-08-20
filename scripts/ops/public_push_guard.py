#!/usr/bin/env python3
"""Guard clean MAGI public/private pushes with strict release checks."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _remote_url(remote: str) -> str:
    proc = _git(["remote", "get-url", remote])
    return proc.stdout.strip()


def _branch_name() -> str:
    proc = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    return proc.stdout.strip()


def _status_lines() -> list[str]:
    proc = _git(["status", "--porcelain"])
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _run_audit(profile: str) -> tuple[int, dict[str, Any], str]:
    cmd = [sys.executable, "scripts/public_release_audit.py", "--strict", "--json"]
    if profile == "public":
        cmd.insert(2, "--public-isolation")
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    try:
        payload: dict[str, Any] = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"ok": False, "parse_error": True}
    return proc.returncode, payload, (proc.stdout or "")[-1200:]


def check_remote_push(remote: str, *, profile: str = "public") -> dict[str, Any]:
    url = _remote_url(remote)
    branch = _branch_name()
    dirty = _status_lines()
    if profile == "public":
        remote_ok = remote == "public" or "MAGI-public" in url
    elif profile == "private":
        remote_ok = remote in {"origin", "private"} or "MAGI-v2" in url or "MAGI_v2" in url
    else:
        raise ValueError(f"unknown profile: {profile}")
    audit_code, audit, audit_tail = _run_audit(profile)

    checks = {
        "remote_matches_profile": remote_ok,
        "working_tree_clean": not dirty,
        "release_audit_strict": audit_code == 0 and bool(audit.get("ok")),
    }
    return {
        "ok": all(checks.values()),
        "profile": profile,
        "remote": remote,
        "remote_url": url,
        "branch": branch,
        "dirty_count": len(dirty),
        "dirty_sample": dirty[:20],
        "checks": checks,
        "audit": {
            "returncode": audit_code,
            "errors": audit.get("errors"),
            "warnings": audit.get("warnings"),
            "ok": audit.get("ok"),
        },
        "audit_output_tail": "" if checks["release_audit_strict"] else audit_tail,
    }


def check_public_push(remote: str) -> dict[str, Any]:
    return check_remote_push(remote, profile="public")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a branch is safe to push to a MAGI GitHub remote.")
    parser.add_argument("--remote", default="public", help="Public git remote name.")
    parser.add_argument("--profile", choices=("public", "private"), default="public", help="Remote safety profile.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    result = check_remote_push(args.remote, profile=args.profile)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("MAGI push guard:", "PASS" if result["ok"] else "FAIL")
        print("profile:", result["profile"])
        print("remote:", result["remote"], result["remote_url"])
        print("branch:", result["branch"])
        print("dirty files:", result["dirty_count"])
        print("audit:", result["audit"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
