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


def _parse_env_prefix(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    idx = 0
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


def main(argv: list[str]) -> int:
    env_prefix, command = _parse_env_prefix(argv)
    if not command:
        print("missing command after token refresh wrapper", file=sys.stderr)
        return 2

    report = token_health_check.build_report(refresh=True, threshold_days=7.0)
    token_health_check._atomic_write_text(
        token_health_check.DEFAULT_REPORT_PATH,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )
    if not report.get("ok"):
        failures = report.get("failures") if isinstance(report.get("failures"), list) else []
        summary = ", ".join(
            f"{item.get('name')}:{item.get('status')}" for item in failures[:4] if isinstance(item, dict)
        )
        print(f"token refresh gate failed: {summary or 'unknown'}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env.update(env_prefix)
    os.execvpe(command[0], command, env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
