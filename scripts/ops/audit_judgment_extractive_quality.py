#!/usr/bin/env python3
"""Read-only acceptance audit for the source-bound judgment extractor."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from api.domains.judgment_summary_quality import (  # noqa: E402
    build_extractive_practice_summary,
    evaluate_practice_summary,
    infer_case_issue,
)


LOW_VALUE_RE = (
    "司促字|促字第|司票字|票字第|補字第|附民字|續收字|司催字|"
    "司消債核字|司執字|司繼字|司聲字|全字第|暫字第|拍字第|司拍字"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--start-id", type=int, default=1)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(Path(os.environ.get("MAGI_ENV_FILE") or ROOT / ".env"))
    except Exception:
        pass
    import mysql.connector

    conn = mysql.connector.connect(
        host=os.environ.get("OSC_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("OSC_DB_PORT", "3306")),
        user=os.environ.get("OSC_DB_USER", "python_user"),
        password=os.environ.get("OSC_DB_PASSWORD", ""),
        database="law_firm_data",
        connection_timeout=5,
    )
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, jid, case_type, case_number, full_text
        FROM court_judgments
        WHERE id >= %s
          AND CHAR_LENGTH(COALESCE(full_text, '')) >= 1200
          AND (summary IS NULL OR summary = '')
          AND case_number IS NOT NULL
          AND (jid LIKE 'TPS%%' OR jid LIKE 'TPH%%' OR case_number NOT REGEXP %s)
        ORDER BY id ASC
        LIMIT %s
        """,
        (max(1, args.start_id), LOW_VALUE_RE, max(1, args.limit)),
    )
    rows = list(cur.fetchall() or [])
    cur.close()
    conn.close()

    reason_counts: dict[str, int] = {}
    issue_counts: dict[str, int] = {}
    samples: list[dict] = []
    accepted = 0
    for row in rows:
        source = str(row.get("full_text") or "")
        issue = infer_case_issue(
            source,
            str(row.get("case_number") or ""),
            str(row.get("case_type") or ""),
        )
        summary = build_extractive_practice_summary(source, issue)
        quality = evaluate_practice_summary(summary, source, issue)
        reason = quality.reason or "accepted"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        issue_counts[issue] = issue_counts.get(issue, 0) + 1
        accepted += int(quality.ok)
        if len(samples) < 12:
            samples.append(
                {
                    "id": int(row["id"]),
                    "jid": str(row.get("jid") or ""),
                    "case_number": str(row.get("case_number") or ""),
                    "inferred_issue": issue,
                    "accepted": bool(quality.ok),
                    "quality_score": int(quality.score),
                    "reason": reason,
                    "summary_preview": summary[:260] if quality.ok else "",
                }
            )
    report = {
        "ok": True,
        "read_only": True,
        "examined": len(rows),
        "accepted": accepted,
        "rejected": len(rows) - accepted,
        "acceptance_pct": round(accepted * 100.0 / max(len(rows), 1), 1),
        "reason_counts": reason_counts,
        "inferred_issue_counts": issue_counts,
        "samples": samples,
    }
    if args.json_out:
        out = Path(args.json_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
