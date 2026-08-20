#!/usr/bin/env python3
"""Verify that the declared self-host core dependencies import every service."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _environment() -> dict[str, str]:
    state = Path(tempfile.gettempdir()) / "magi-selfhost-minimal-import"
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT),
        "MAGI_DEPLOYMENT_MODE": "selfhost",
        "MAGI_AGENT_DIR": str(state / "agent"),
        "MAGI_LOG_DIR": str(state / "logs"),
        "MAGI_RUNTIME_DIR": str(state / "runtime"),
        "MAGI_MUTABLE_STATIC_DIR": str(state / "static"),
        "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
        "MAGI_SKIP_IMPORT_PROBES": "1",
        "MAGI_TOOLS_HEALTH_PROBE_MODEL": "0",
        "MAGI_TOOLS_API_WARMUP_ON_START": "0",
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "3306",
        "DB_USER": "magi_import_check",
        "DB_PASSWORD": "not-used-by-import-check",
        "DB_NAME": "magi",
        "FLASK_SECRET_KEY": "import-check-only",
        "MAGI_API_KEY": "import-check-only",
    })
    for feature in (
        "LEGAL_AID",
        "COURT_PORTAL",
        "GOOGLE_CALENDAR",
        "GOOGLE_DRIVE",
        "MESSAGING",
        "ACCOUNTING",
        "JUDGMENT_LIBRARY",
        "KNOWLEDGE",
        "DOCUMENTS",
        "MARKET",
        "RESEARCH",
        "LOCAL_MODELS",
        "TRANSLATION",
        "DEVELOPMENT",
        "REMOTE_ACCESS",
    ):
        env[f"MAGI_FEATURE_{feature}"] = "0"
    return env


def main() -> int:
    findings: list[dict[str, object]] = []
    for module in ("daemon", "api.server", "api.tools_api"):
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=ROOT,
            env=_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=45,
            check=False,
        )
        findings.append({
            "module": module,
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "output_tail": (completed.stdout or "")[-1200:],
        })
    payload = {
        "schema": "magi.selfhost.minimal-import/v1",
        "ok": all(item["ok"] for item in findings),
        "python": sys.version.split()[0],
        "findings": findings,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
