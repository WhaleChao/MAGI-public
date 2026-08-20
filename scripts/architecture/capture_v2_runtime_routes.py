#!/usr/bin/env python3
"""Safely capture the authoritative V2 Flask route signatures.

Runtime registration is authoritative because V2 includes routes registered by
functions and computed blueprint prefixes that a literal AST scan cannot see.
This command disables known background work, writes no normal server log, and
stubs the eager oMLX probe before importing either Flask app.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any


SAFETY_ENV = {
    "MAGI_TEST_MODE": "1",
    "MAGI_NO_DELETE": "1",
    "MAGI_ENABLE_LIVE_TESTS": "0",
    "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
    "MAGI_DISABLE_BACKGROUND_THREADS": "1",
    "MAGI_DISABLE_BACKGROUND_WORKERS": "1",
    "MAGI_DISABLE_SCHEDULERS": "1",
    "MAGI_DRIVE_SYNC_CREATE_ON_CASE_FOLDER": "0",
    "MAGI_DRIVE_SYNC_ENABLE_WRITE": "0",
    "MAGI_GMAIL_ENABLE_SEND": "0",
    "MAGI_LAF_PORTAL_ENABLE_WRITE": "0",
    "MAGI_PORTAL_ENABLE_WRITE": "0",
    "MAGI_NAS_ENABLE_WRITE": "0",
    "MAGI_ALLOW_SYNOLOGY_DRIVE_FOLDER_CREATE": "0",
    "MAGI_ENABLE_NAS_FSWATCHER": "0",
    "MAGI_LAF_PORTAL_RETRY_ON_START": "0",
    "MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK": "0",
}


class _NullRotatingFileHandler(logging.NullHandler):
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        super().__init__()


def _prepare_safe_import(root: Path) -> None:
    from dotenv import load_dotenv

    load_dotenv(root / ".env", override=False)
    os.environ.update(SAFETY_ENV)

    # V2 modules call load_dotenv(override=True) during import.  Keep the
    # safety flags authoritative after the initial configuration load.
    import dotenv

    dotenv.load_dotenv = lambda *_args, **_kwargs: False

    import skills.ops.health_probes as health_probes

    health_probes.probe_omlx_models = lambda **_kwargs: {
        "pass": False,
        "error": "inventory_capture_skipped",
    }
    logging.handlers.RotatingFileHandler = _NullRotatingFileHandler


def _capture(service: str, app: Any) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda item: (item.rule, item.endpoint, sorted(item.methods))):
        if rule.endpoint == "static":
            continue
        routes.append(
            {
                "service": service,
                "rule": rule.rule,
                "methods": sorted(set(rule.methods) - {"HEAD", "OPTIONS"}),
                "endpoint": rule.endpoint,
            }
        )
    return routes


def capture(root: Path) -> dict[str, Any]:
    os.chdir(root)
    root_text = str(root)
    sys.path = [root_text, *(entry for entry in sys.path if entry != root_text)]
    # The workstation bootstrap may preload the active V2 runtime through a
    # .pth hook.  A release capture must import the explicitly requested tree,
    # otherwise a dirty candidate can falsely appear identical to LIVE.
    for name in list(sys.modules):
        if name in {"api", "skills", "magi_v3", "casper_ecosystem"} or name.startswith(
            ("api.", "skills.", "magi_v3.", "casper_ecosystem.")
        ):
            sys.modules.pop(name, None)
    _prepare_safe_import(root)
    from api.server import app as server_app
    from api.tools_api import app as tools_app

    services = {
        "5002": _capture("5002", server_app),
        "5003": _capture("5003", tools_app),
    }
    return {
        "schema_version": 1,
        "source": "flask_app_url_map_with_side_effect_guards",
        "counts": {
            "5002": len(services["5002"]),
            "5003": len(services["5003"]),
            "total": sum(len(routes) for routes in services.values()),
        },
        "services": services,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(capture(args.root.resolve()), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output or not args.output.exists():
            print("runtime route snapshot is missing")
            return 1
        if args.output.read_text(encoding="utf-8") != rendered:
            print("V2 runtime route snapshot is stale; review and regenerate it")
            return 1
        print("V2 runtime route snapshot is current")
        return 0
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
