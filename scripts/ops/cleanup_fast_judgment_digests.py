#!/usr/bin/env python3
"""Downgrade low-quality judgment digests so they cannot be cited as insights.

The daily Judicial API flow may create extractive fast digests to keep backlog
processing cheap.  Those rows are useful for locating source text, but they are
not authoritative summaries.  This script clears such summaries while leaving
full_text and source metadata intact.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from osc import DatabaseManager  # noqa: E402

FAST_PATTERN = "%抽取式快篩%"


def _db() -> DatabaseManager:
    return DatabaseManager(
        {
            "host": os.environ.get("OSC_DB_HOST", "127.0.0.1"),
            "port": int(os.environ.get("OSC_DB_PORT", "3307") or "3307"),
            "user": os.environ.get("OSC_DB_USER", "python_user"),
            "password": os.environ.get("OSC_DB_PASSWORD", ""),
            "database": os.environ.get("OSC_DB_NAME", "law_firm_data"),
        }
    )


def _count(db: DatabaseManager, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = db.execute(sql, params, fetch="one") or {}
    return int(row.get("c") or 0)


def _backup_rows(db: DatabaseManager, out_dir: Path) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"fast_judgment_digest_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl.gz"
    rows = db.execute(
        """
        SELECT id, jid, court_name, case_number, source_url, LEFT(COALESCE(summary, ''), 600) AS summary_prefix
        FROM court_judgments
        WHERE COALESCE(summary, '') LIKE %s
           OR (COALESCE(source_url, '') LIKE %s AND CHAR_LENGTH(COALESCE(summary, '')) > 0 AND CHAR_LENGTH(COALESCE(summary, '')) < 280)
        ORDER BY id
        """,
        (FAST_PATTERN, "%dr-lawbot.com%"),
        fetch="all",
    ) or []
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return str(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean low-quality judgment fast digests")
    parser.add_argument("--apply", action="store_true", help="Write changes to DB")
    parser.add_argument(
        "--backup-dir",
        default=str(ROOT / ".runtime" / "judgment_quality_cleanup"),
        help="Directory for JSONL.gz backups",
    )
    args = parser.parse_args()

    db = _db()
    before = {
        "court_fast": _count(db, "SELECT COUNT(*) AS c FROM court_judgments WHERE COALESCE(summary, '') LIKE %s", (FAST_PATTERN,)),
        "court_tlr_short": _count(
            db,
            """
            SELECT COUNT(*) AS c
            FROM court_judgments
            WHERE COALESCE(source_url, '') LIKE %s
              AND CHAR_LENGTH(COALESCE(summary, '')) > 0
              AND CHAR_LENGTH(COALESCE(summary, '')) < 280
            """,
            ("%dr-lawbot.com%",),
        ),
        "archive_fast": _count(db, "SELECT COUNT(*) AS c FROM judgment_archive WHERE COALESCE(summary_text, '') LIKE %s", (FAST_PATTERN,)),
        "legal_fast": _count(
            db,
            """
            SELECT COUNT(*) AS c
            FROM legal_insights
            WHERE CONCAT(COALESCE(insight_text, ''), COALESCE(raw_text, '')) LIKE %s
            """,
            (FAST_PATTERN,),
        ),
    }
    report: dict[str, Any] = {"ok": True, "applied": bool(args.apply), "before": before}
    if args.apply:
        report["backup_path"] = _backup_rows(db, Path(args.backup_dir))
        report["updated_court_fast"] = db.execute(
            "UPDATE court_judgments SET summary='' WHERE COALESCE(summary, '') LIKE %s",
            (FAST_PATTERN,),
        )
        report["updated_court_tlr_short"] = db.execute(
            """
            UPDATE court_judgments
            SET summary=''
            WHERE COALESCE(source_url, '') LIKE %s
              AND CHAR_LENGTH(COALESCE(summary, '')) > 0
              AND CHAR_LENGTH(COALESCE(summary, '')) < 280
            """,
            ("%dr-lawbot.com%",),
        )
        report["updated_archive_fast"] = db.execute(
            "UPDATE judgment_archive SET summary_text=NULL, is_degraded=1 WHERE COALESCE(summary_text, '') LIKE %s",
            (FAST_PATTERN,),
        )
        report["deleted_legal_fast"] = db.execute(
            """
            DELETE FROM legal_insights
            WHERE CONCAT(COALESCE(insight_text, ''), COALESCE(raw_text, '')) LIKE %s
            """,
            (FAST_PATTERN,),
        )
        after = {
            "court_fast": _count(db, "SELECT COUNT(*) AS c FROM court_judgments WHERE COALESCE(summary, '') LIKE %s", (FAST_PATTERN,)),
            "court_tlr_short": _count(
                db,
                """
                SELECT COUNT(*) AS c
                FROM court_judgments
                WHERE COALESCE(source_url, '') LIKE %s
                  AND CHAR_LENGTH(COALESCE(summary, '')) > 0
                  AND CHAR_LENGTH(COALESCE(summary, '')) < 280
                """,
                ("%dr-lawbot.com%",),
            ),
            "archive_fast": _count(db, "SELECT COUNT(*) AS c FROM judgment_archive WHERE COALESCE(summary_text, '') LIKE %s", (FAST_PATTERN,)),
            "legal_fast": _count(
                db,
                """
                SELECT COUNT(*) AS c
                FROM legal_insights
                WHERE CONCAT(COALESCE(insight_text, ''), COALESCE(raw_text, '')) LIKE %s
                """,
                (FAST_PATTERN,),
            ),
        }
        report["after"] = after
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
