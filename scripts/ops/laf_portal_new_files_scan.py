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


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="法扶官網附件補抓（每 6 小時）")
    parser.add_argument("--only-laf-no", default="", help="只檢查指定法扶案號（live probe 用）")
    parser.add_argument("--apply", action="store_true", help="正式下載並搬檔；預設只比對、不下載")
    parser.add_argument("--dry-run", action="store_true", help="強制只比對官網與本地檔案，不下載歸檔")
    parser.add_argument(
        "--json-out",
        default=str(ROOT / "static" / "laf_portal_new_files_latest.json"),
        help="輸出最新狀態 JSON 路徑",
    )
    args = parser.parse_args(argv)

    apply_enabled = (
        bool(args.apply)
        or str(os.environ.get("MAGI_LAF_PORTAL_APPLY") or "").strip().lower() in {"1", "true", "yes", "on"}
    )
    dry_run = True if bool(args.dry_run) else not apply_enabled

    # Importing laf_nightly_audit used to probe DB failover and NAS mounts. This
    # wrapper is a bounded scan runner, so imports must stay side-effect free.
    os.environ.setdefault("MAGI_SKIP_IMPORT_PROBES", "1")

    from casper_ecosystem.law_firm_orchestrators import laf_nightly_audit

    result = laf_nightly_audit.run_portal_new_files_scan(
        only_laf_no=args.only_laf_no.strip(),
        auto_download=not dry_run,
    )
    result["dry_run"] = dry_run
    result["apply"] = apply_enabled and not dry_run
    if args.json_out:
        _write_json(Path(args.json_out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
