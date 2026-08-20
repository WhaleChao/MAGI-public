#!/usr/bin/env python3
"""Refresh MAGI OAuth tokens before running a Google-dependent job."""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path


MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from scripts.ops import token_health_check  # noqa: E402


def _short_failure_summary(item: dict) -> str:
    name = str(item.get("name") or "?")
    status = str(item.get("status") or "?")
    parts = [f"{name}:{status}"]
    if "refresh_token_present" in item:
        parts.append(f"refresh_token_present={bool(item.get('refresh_token_present'))}")
    if item.get("scopes_ok") is False:
        missing = item.get("missing_scopes") if isinstance(item.get("missing_scopes"), list) else []
        parts.append(f"scopes_ok=False missing_scopes={len(missing)}")
    if item.get("account_mismatch"):
        parts.append("account_mismatch=True")
    action = str(item.get("next_action") or item.get("message") or "").strip()
    if action:
        parts.append(f"next_action={action[:160]}")
    return " ".join(parts)


def _parse_env_prefix(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    idx = 0
    if idx < len(argv) and argv[idx] == "--":
        idx += 1
    while idx < len(argv):
        item = argv[idx]
        if item == "--":
            idx += 1
            break
        if "=" not in item or item.startswith("="):
            break
        key, value = item.split("=", 1)
        if not key.replace("_", "").isalnum() or key[0].isdigit():
            break
        env[key] = value
        idx += 1
    return env, argv[idx:]


def _parse_required_checks(argv: list[str]) -> tuple[list[str], list[str]]:
    required: list[str] = []
    idx = 0
    while idx < len(argv) and argv[idx] == "--require":
        if idx + 1 >= len(argv):
            raise ValueError("missing value after --require")
        name = str(argv[idx + 1] or "").strip()
        if not name:
            raise ValueError("empty value after --require")
        if name not in required:
            required.append(name)
        idx += 2
    return required, argv[idx:]


def _gate_failures(report: dict, required: list[str]) -> list[dict]:
    checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    failures = report.get("failures") if isinstance(report.get("failures"), list) else []
    if not required:
        return [item for item in failures if isinstance(item, dict)]

    known = {
        str(item.get("name") or "").strip()
        for item in checks
        if isinstance(item, dict)
    }
    selected = [
        item
        for item in failures
        if isinstance(item, dict)
        and str(item.get("name") or "").strip() in required
    ]
    for name in required:
        if name not in known:
            selected.append(
                {
                    "name": name,
                    "status": "unknown_required_check",
                    "message": "required token check was not discovered",
                }
            )
    return selected


def main(argv: list[str]) -> int:
    try:
        required, remaining = _parse_required_checks(argv)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    env_prefix, command = _parse_env_prefix(remaining)
    if not command:
        print("missing command after token refresh wrapper", file=sys.stderr)
        return 2

    report = token_health_check.build_report(refresh=True, threshold_days=7.0)
    gate_failures = _gate_failures(report, required)
    report["execution_gate"] = {
        "required_checks": required,
        "ok": not gate_failures,
        "failure_names": [
            str(item.get("name") or "")
            for item in gate_failures
            if isinstance(item, dict)
        ],
    }
    token_health_check._atomic_write_text(
        token_health_check.DEFAULT_REPORT_PATH,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )
    if gate_failures:
        summary = "; ".join(
            _short_failure_summary(item)
            for item in gate_failures[:4]
            if isinstance(item, dict)
        )
        print(f"token refresh gate failed: {summary or 'unknown'}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env.update(env_prefix)
    os.execvpe(command[0], command, env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
