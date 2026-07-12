#!/usr/bin/env python3
"""Repair non-canonical OSC client ids in the live DB.

Original OSC client ids are sequential C0001, C0002, ...
Some web/LAF paths once generated UUID/webc/random-hex ids.  This script
renumbers only those non-canonical rows and updates known client_id references.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.osc.client_ids import is_canonical_client_id, next_client_id_from_existing
from api.osc.utils import _osc_exec


@dataclass
class RepairItem:
    old_id: str
    new_id: str
    name: str


def _fetch_bad_clients() -> list[dict]:
    rows, _ = _osc_exec(
        """
        SELECT id, name, notes
        FROM clients
        WHERE NOT (id REGEXP '^C[0-9]{4,6}$') OR id REGEXP '^C[0-9]{7,}$'
        ORDER BY COALESCE(created_date, updated_date), id
        """,
        fetch="all",
    )
    return rows or []


def _fetch_reference_columns() -> list[tuple[str, str]]:
    rows, _ = _osc_exec(
        """
        SELECT TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND COLUMN_NAME = 'client_id'
          AND TABLE_NAME <> 'clients'
        ORDER BY TABLE_NAME, COLUMN_NAME
        """,
        fetch="all",
    )
    return [(str(r["TABLE_NAME"]), str(r["COLUMN_NAME"])) for r in rows or []]


def build_repair_plan() -> list[RepairItem]:
    existing_rows, _ = _osc_exec("SELECT id FROM clients WHERE id LIKE 'C%'", fetch="all")
    existing = [r.get("id") for r in existing_rows or [] if is_canonical_client_id(r.get("id"))]
    plan: list[RepairItem] = []
    for row in _fetch_bad_clients():
        old_id = str(row.get("id") or "").strip()
        if not old_id or is_canonical_client_id(old_id):
            continue
        new_id = next_client_id_from_existing(existing)
        existing.append(new_id)
        plan.append(RepairItem(old_id=old_id, new_id=new_id, name=str(row.get("name") or "")))
    return plan


def execute_plan(plan: list[RepairItem]) -> None:
    refs = _fetch_reference_columns()
    for item in plan:
        for table, column in refs:
            _osc_exec(
                f"UPDATE `{table}` SET `{column}`=%s WHERE `{column}`=%s",
                (item.new_id, item.old_id),
                fetch="none",
            )
        _osc_exec(
            """
            UPDATE clients
            SET id=%s,
                notes=TRIM(CONCAT(COALESCE(notes, ''), '\n[系統修復] 原當事人編號：', %s))
            WHERE id=%s
            """,
            (item.new_id, item.old_id, item.old_id),
            fetch="none",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="actually update the DB")
    args = parser.parse_args()
    plan = build_repair_plan()
    print(f"repair_count={len(plan)}")
    for item in plan:
        print(f"{item.old_id} -> {item.new_id} | {item.name}")
    if args.execute and plan:
        execute_plan(plan)
        print("executed=true")
    else:
        print("executed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
