#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Best-effort nightly backfill for LAF client contact fields.

This script scans LAF case folders for intake-form PDFs and uses the existing
parser in `skills.legal.laf` to extract phone/address/email/tax_id. It then
upserts missing client fields via the existing OSC DatabaseManager helper.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "casper_ecosystem" / "law_firm_orchestrators"))
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "legal"))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

from scripts.laf_nightly_audit import NAS_CASE_ROOT, Y_DRIVE_ROOT, _get_db
from skills.legal.laf import _scan_laf_forms_for_client_fields


def _to_local_case_folder(folder: str) -> str:
    raw = str(folder or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.startswith(("Z:/", "z:/")):
        rel = raw.split("/01_案件/", 1)[-1] if "/01_案件/" in raw else raw.split(":/", 1)[-1]
        return os.path.join(NAS_CASE_ROOT, rel)
    if raw.startswith(("Y:/", "y:/")):
        rel = raw.split("/10_結案/", 1)[-1] if "/10_結案/" in raw else raw.split(":/", 1)[-1]
        return os.path.join(Y_DRIVE_ROOT, rel)
    return raw


def _fetch_candidates(db: Any, limit: int) -> List[dict]:
    query = """
        SELECT
            c.`case_number`,
            c.`client_name`,
            c.`folder_path`,
            c.`case_category`,
            c.`case_reason`,
            COALESCE(cl.`phone`, '') AS `phone`,
            COALESCE(cl.`email`, '') AS `email`,
            COALESCE(cl.`address`, '') AS `address`
        FROM `cases` c
        LEFT JOIN `clients` cl
          ON cl.`name` = c.`client_name`
        WHERE (
                c.`case_category` IN ('法律扶助案件', '法扶案件')
                OR c.`case_reason` LIKE '%%法扶%%'
                OR c.`case_reason` LIKE '%%法律扶助%%'
              )
          AND c.`folder_path` IS NOT NULL
          AND c.`folder_path` <> ''
          AND (
                cl.`phone` IS NULL OR cl.`phone` = ''
                OR cl.`email` IS NULL OR cl.`email` = ''
                OR cl.`address` IS NULL OR cl.`address` = ''
              )
        ORDER BY c.`case_number` DESC
        LIMIT %s
    """
    try:
        rows = db.fetch_all(query, (max(1, int(limit)),), as_dict=True) or []
        return rows if isinstance(rows, list) else []
    except Exception:
        fallback = """
            SELECT
                `case_number`,
                `client_name`,
                `folder_path`,
                `case_category`,
                `case_reason`
            FROM `cases`
            WHERE (
                    `case_category` IN ('法律扶助案件', '法扶案件')
                    OR `case_reason` LIKE '%%法扶%%'
                    OR `case_reason` LIKE '%%法律扶助%%'
                  )
              AND `folder_path` IS NOT NULL
              AND `folder_path` <> ''
            ORDER BY `case_number` DESC
            LIMIT %s
        """
        rows = db.fetch_all(fallback, (max(1, int(limit)),), as_dict=True) or []
        return rows if isinstance(rows, list) else []


def run(limit: int) -> dict:
    db = _get_db()
    if not db:
        return {
            "success": True,
            "skipped": True,
            "message": "DB unavailable; laf_deep_extract skipped",
            "scanned": 0,
            "updated": 0,
        }

    candidates = _fetch_candidates(db, limit)
    scanned = 0
    updated = 0
    skipped_missing_folder = 0
    skipped_no_hit = 0
    hits: List[dict] = []
    errors: List[str] = []

    for row in candidates:
        scanned += 1
        client_name = str(row.get("client_name") or "").strip()
        folder = _to_local_case_folder(str(row.get("folder_path") or ""))
        if not folder or not os.path.isdir(folder):
            skipped_missing_folder += 1
            continue

        try:
            fields = _scan_laf_forms_for_client_fields(folder)
        except Exception as exc:
            errors.append(f"{row.get('case_number')}: scan_failed:{type(exc).__name__}")
            continue

        extracted = {
            k: str(v or "").strip()
            for k, v in (fields or {}).items()
            if str(v or "").strip()
        }
        if not extracted:
            skipped_no_hit += 1
            continue

        current_phone = str(row.get("phone") or "").strip()
        current_email = str(row.get("email") or "").strip()
        current_address = str(row.get("address") or "").strip()
        contact_delta = {
            "phone": extracted.get("phone", "") if not current_phone else "",
            "email": extracted.get("email", "") if not current_email else "",
            "address": extracted.get("address", "") if not current_address else "",
        }
        if not any(contact_delta.values()):
            skipped_no_hit += 1
            continue
        delta = {
            **contact_delta,
            "tax_id": extracted.get("tax_id", ""),
        }

        try:
            db.check_and_add_client(
                {
                    "name": client_name,
                    "phone": delta.get("phone", ""),
                    "email": delta.get("email", ""),
                    "address": delta.get("address", ""),
                    "tax_id": delta.get("tax_id", ""),
                }
            )
            updated += 1
            hits.append(
                {
                    "case_number": str(row.get("case_number") or ""),
                    "client_name": client_name,
                    "folder": folder,
                    "filled": {k: v for k, v in delta.items() if v},
                }
            )
        except Exception as exc:
            errors.append(f"{row.get('case_number')}: upsert_failed:{type(exc).__name__}")

    return {
        "success": True,
        "limit": int(limit),
        "scanned": scanned,
        "updated": updated,
        "skipped_missing_folder": skipped_missing_folder,
        "skipped_no_hit": skipped_no_hit,
        "hits": hits[:20],
        "errors": errors[:20],
        "message": f"LAF deep extract completed: scanned={scanned}, updated={updated}, missing_folder={skipped_missing_folder}, no_hit={skipped_no_hit}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Best-effort nightly LAF deep extract backfill")
    parser.add_argument("--limit", type=int, default=8, help="Max candidate cases to scan")
    args = parser.parse_args()
    result = run(max(1, int(args.limit or 8)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
