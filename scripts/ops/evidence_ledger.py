#!/usr/bin/env python3
"""Operate MAGI's release-bound Evidence Ledger without touching services."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from magi_v3.evidence_ledger import EvidenceEnvelope, EvidenceLedger, EvidenceLedgerError


DEFAULT_RUNTIME = Path(
    os.environ.get("MAGI_RUNTIME_DIR", "").strip()
    or Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3" / "shared" / "runtime"
).expanduser()
DEFAULT_LEDGER = DEFAULT_RUNTIME / "evidence" / "evidence-ledger.sqlite3"
DEFAULT_MARKER = Path(
    os.environ.get("MAGI_V3_ACTIVE_RELEASE_MARKER", "").strip()
    or Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "active-release.json"
).expanduser()


def _read_json(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise EvidenceLedgerError(f"{label} must be a regular file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceLedgerError(f"{label} is invalid JSON") from exc


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI release-bound Evidence Ledger")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    commands = parser.add_subparsers(dest="command", required=True)

    bind = commands.add_parser("bind-active", help="Bind the canonical active-release marker")
    bind.add_argument("--marker", type=Path, default=DEFAULT_MARKER)
    bind.add_argument("--release-manifest", type=Path)

    append = commands.add_parser("append", help="Append one EvidenceEnvelope v2 JSON")
    append.add_argument("--envelope", type=Path, required=True)

    latest = commands.add_parser("latest", help="Read a subject's active-release projection")
    latest.add_argument("--subject", required=True)
    latest.add_argument("--include-historical-release", action="store_true")

    history = commands.add_parser("history", help="Read bounded subject history")
    history.add_argument("--subject", required=True)
    history.add_argument("--limit", type=int, default=100)

    project = commands.add_parser("project", help="Write a legacy *_latest.json projection")
    project.add_argument("--subject", required=True)
    project.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    ledger = EvidenceLedger(args.ledger)
    ledger.initialize()
    try:
        if args.command == "bind-active":
            result = ledger.bind_active_marker(
                args.marker,
                release_manifest_path=args.release_manifest,
            )
        elif args.command == "append":
            value = _read_json(args.envelope, "evidence envelope")
            if not isinstance(value, dict):
                raise EvidenceLedgerError("evidence envelope must be an object")
            result = ledger.append(EvidenceEnvelope.from_mapping(value))
        elif args.command == "latest":
            result = ledger.latest(
                args.subject,
                active_release_only=not args.include_historical_release,
            )
        elif args.command == "history":
            result = ledger.history(args.subject, limit=args.limit)
        else:
            result = ledger.project_legacy_latest(args.subject, args.output)
    except EvidenceLedgerError as exc:
        _print({"ok": False, "error": str(exc)})
        return 1
    _print({"ok": result is not None, "result": result})
    return 0 if result is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
