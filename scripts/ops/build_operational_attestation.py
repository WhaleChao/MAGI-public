#!/usr/bin/env python3
"""Build a release-bound operations attestation from real ledger and DR evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.ledger import JobLedger
from magi_v3.observability import support_bundle, verify_dr_report


RELEASE_RE = re.compile(r"^v3-[A-Za-z0-9._-]{3,96}$")


def _regular_json(path: Path, *, label: str) -> dict[str, Any]:
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} must be an absolute regular file")
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def build_attestation(
    *,
    ledger_path: Path,
    dr_report: Mapping[str, Any],
    release_id: str,
    limit: int,
    max_rpo_seconds: int,
    max_rto_seconds: int,
) -> dict[str, Any]:
    if RELEASE_RE.fullmatch(release_id) is None:
        raise ValueError("release_id is invalid")
    records = JobLedger(ledger_path).recent_jobs(limit=limit)
    bundle = support_bundle(records, max_records=limit)
    slo = dict(bundle["slo"])
    slo["ok"] = bool(
        slo.get("sample_size", 0) > 0
        and slo.get("terminal_failure_count") == 0
        and not slo.get("receipt_missing_trace_ids")
    )
    dr = verify_dr_report(
        dr_report,
        max_rpo_seconds=max_rpo_seconds,
        max_rto_seconds=max_rto_seconds,
    )
    verified = bool(slo["ok"] and dr["verified"])
    return {
        "schema_version": 1,
        "release_id": release_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": verified,
        "status": "passed" if verified else "not_attested",
        "slo": slo,
        "support_bundle": bundle,
        "dr": dr,
    }


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    target = path.expanduser()
    if not target.is_absolute() or target.is_symlink():
        raise ValueError("output must be an absolute non-symlink path")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--dr-report", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-rpo-seconds", type=int, default=86400)
    parser.add_argument("--max-rto-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        if not args.ledger.is_absolute() or args.ledger.is_symlink() or not args.ledger.is_file():
            raise ValueError("ledger must be an absolute regular file")
        dr_report = _regular_json(args.dr_report, label="DR report")
        payload = build_attestation(
            ledger_path=args.ledger,
            dr_report=dr_report,
            release_id=args.release_id,
            limit=args.limit,
            max_rpo_seconds=args.max_rpo_seconds,
            max_rto_seconds=args.max_rto_seconds,
        )
        _atomic_write(args.json_out, payload)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": payload["ok"], "status": payload["status"], "json_out": str(args.json_out)}, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
