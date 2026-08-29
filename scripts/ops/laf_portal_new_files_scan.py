#!/usr/bin/env python3
"""Run the standalone LAF portal attachment sweep.

This is the six-hour companion to the nightly LAF audit.  It checks the
official LAF portal download area, compares against local archived files, and
downloads only missing official attachments when ``--apply`` is set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
MUTABLE_STATIC_DIR = Path(
    os.environ.get("MAGI_MUTABLE_STATIC_DIR", "").strip() or ROOT / "static"
).expanduser()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _preserve_last_successful_missing_state(path: Path, payload: dict) -> dict:
    """Keep known missing attachments visible when a later portal read fails."""
    if payload.get("ok") is not False or not path.is_file():
        return payload
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return payload
    if not isinstance(previous, dict) or previous.get("ok") is not True:
        return payload
    # A portal scan performed with an empty case inventory is not a valid
    # successful scan.  Older code wrote every portal row as ``case_unmapped``
    # and counted it as missing; never preserve that synthetic result as real
    # missing evidence.
    if (
        int(previous.get("scanned_cases") or 0) == 0
        and int(previous.get("matched_or_missing_cases") or 0) > 0
    ):
        return payload
    merged = dict(payload)
    merged["portal_still_missing"] = int(previous.get("portal_still_missing") or 0)
    merged["portal_new_files"] = (
        previous.get("portal_new_files")
        if isinstance(previous.get("portal_new_files"), list)
        else []
    )
    merged["matched_or_missing_cases"] = int(
        previous.get("matched_or_missing_cases") or len(merged["portal_new_files"])
    )
    merged["stale_last_success"] = True
    merged["last_successful_checked_at"] = previous.get("checked_at") or ""
    merged["last_successful_status"] = previous.get("status") or ""
    merged["last_successful_action_required"] = bool(previous.get("action_required"))
    merged["last_successful_mapping_unverified_cases"] = int(
        previous.get("portal_mapping_unverified_cases") or 0
    )
    merged["last_successful_mapping_unverified_files"] = int(
        previous.get("portal_mapping_unverified_files") or 0
    )
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="法扶官網附件補抓（每 6 小時）")
    parser.add_argument("--only-laf-no", default="", help="只檢查指定法扶案號（live probe 用）")
    parser.add_argument("--apply", action="store_true", help="正式下載並搬檔；預設只比對、不下載")
    parser.add_argument("--dry-run", action="store_true", help="強制只比對官網與本地檔案，不下載歸檔")
    parser.add_argument(
        "--json-out",
        default=str(MUTABLE_STATIC_DIR / "laf_portal_new_files_latest.json"),
        help="輸出最新狀態 JSON 路徑",
    )
    args = parser.parse_args(argv)

    apply_enabled = (
        bool(args.apply)
        or str(os.environ.get("MAGI_LAF_PORTAL_APPLY") or "").strip().lower() in {"1", "true", "yes", "on"}
    )
    dry_run = True if bool(args.dry_run) else not apply_enabled

    # Importing laf_nightly_audit used to probe DB failover and NAS mounts. This
    # wrapper is a bounded scan runner, so the import itself must stay
    # side-effect free.  The flag must not leak into the actual scan, however:
    # case_path_mapper also reads it before the read-only mount-table authority
    # check.  Leaving it set made every real SMB case folder appear to be a
    # File Provider-only copy and produced false ``mapping_unverified`` rows.
    previous_skip_import_probes = os.environ.get("MAGI_SKIP_IMPORT_PROBES")
    os.environ["MAGI_SKIP_IMPORT_PROBES"] = "1"
    try:
        from casper_ecosystem.law_firm_orchestrators import laf_nightly_audit
    finally:
        if previous_skip_import_probes is None:
            os.environ.pop("MAGI_SKIP_IMPORT_PROBES", None)
        else:
            os.environ["MAGI_SKIP_IMPORT_PROBES"] = previous_skip_import_probes

    result = laf_nightly_audit.run_portal_new_files_scan(
        only_laf_no=args.only_laf_no.strip(),
        auto_download=not dry_run,
    )
    result["dry_run"] = dry_run
    result["apply"] = apply_enabled and not dry_run
    if args.json_out:
        output = Path(args.json_out)
        result = _preserve_last_successful_missing_state(output, result)
        _write_json(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
