#!/usr/bin/env python3
"""Read-only LIVE quality probe for NVIDIA source-bound judgment summaries."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_SYNTHETIC_SOURCE = """臺灣測試法院民事判決
案由：侵權行為損害賠償
主文
原告之訴駁回。
理由

按民法第184條第1項前段規定，因故意或過失不法侵害他人之權利者，負損害賠償責任；侵權行為損害賠償請求權之成立，應由請求權人就故意或過失、權利受侵害及相當因果關係等構成要件負舉證責任。

本院認為，測試行為與所稱損害之間欠缺相當因果關係，現有測試資料亦不足證明權利確受侵害，故不能認定損害賠償責任成立。

中華民國115年7月31日
"""


def _load_runtime_env() -> None:
    env_path = Path(
        os.environ.get("MAGI_ENV_FILE", "").strip()
        or Path.home()
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "MAGI_v3"
        / "shared"
        / "external"
        / ".env"
    ).expanduser()
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except Exception:
        # Minimal dotenv fallback.  It intentionally does not expand shell
        # expressions and never prints secret values.
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip("\"'")


def _fetch_candidates(scan_limit: int) -> list[dict[str, Any]]:
    from api.db_helper import _default_config, get_cursor

    sql = (
        "SELECT id, case_type, case_number, full_text "
        "FROM court_judgments "
        "WHERE full_text IS NOT NULL AND CHAR_LENGTH(full_text) >= 1200 "
        "AND case_number IS NOT NULL "
        "AND (jid LIKE 'TPS%' OR jid LIKE 'TPH%' "
        "OR case_number NOT REGEXP %s) "
        "ORDER BY id DESC LIMIT %s"
    )
    low_value = (
        "司促字|促字第|司票字|票字第|補字第|附民字|續收字|"
        "司催字|司消債核字|司執字|司繼字|司聲字|全字第|"
        "暫字第|拍字第|司拍字"
    )
    config = _default_config()
    config["database"] = os.environ.get("MAGI_CASES_DB_NAME", "law_firm_data")
    with get_cursor(config=config, dictionary=True) as (_conn, cur):
        cur.execute(sql, (low_value, max(10, int(scan_limit))))
        return list(cur.fetchall() or [])


def _fetch_rows_by_ids(row_ids: list[int]) -> list[dict[str, Any]]:
    from api.db_helper import _default_config, get_cursor

    ids = list(dict.fromkeys(int(value) for value in row_ids if int(value) > 0))
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))
    sql = (
        "SELECT id, case_type, case_number, full_text "
        f"FROM court_judgments WHERE id IN ({placeholders}) "
        "AND full_text IS NOT NULL AND CHAR_LENGTH(full_text) >= 1200 "
        "ORDER BY id DESC"
    )
    config = _default_config()
    config["database"] = os.environ.get("MAGI_CASES_DB_NAME", "law_firm_data")
    with get_cursor(config=config, dictionary=True) as (_conn, cur):
        cur.execute(sql, tuple(ids))
        rows = list(cur.fetchall() or [])
    by_id = {int(row.get("id") or 0): row for row in rows}
    return [by_id[value] for value in ids if value in by_id]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--scan-limit", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--json-out", required=True)
    parser.add_argument(
        "--row-id",
        action="append",
        type=int,
        default=[],
        help="Probe an exact court_judgments row; repeat for multiple rows.",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use a non-sensitive fictional judgment and do not read the DB",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Inspect real candidate spans locally without calling NVIDIA",
    )
    args = parser.parse_args()
    _load_runtime_env()

    from api.domains.judgment_nvidia_summary import summarize_with_nvidia
    from api.domains.judgment_summary_quality import (
        infer_case_issue,
        rank_practice_candidates,
    )

    selected: list[tuple[dict[str, Any], str]] = []
    if args.synthetic:
        selected.append(
            (
                {
                    "id": 0,
                    "case_type": "民事",
                    "case_number": "115年度測字第1號",
                    "full_text": _SYNTHETIC_SOURCE,
                },
                "侵權行為損害賠償",
            )
        )
    else:
        reason_families: set[str] = set()
        source_rows = (
            _fetch_rows_by_ids(args.row_id)
            if args.row_id
            else _fetch_candidates(args.scan_limit)
        )
        for row in source_rows:
            full_text = str(row.get("full_text") or "")
            case_reason = infer_case_issue(
                full_text,
                str(row.get("case_number") or ""),
                str(row.get("case_type") or ""),
            )
            candidates = rank_practice_candidates(full_text, case_reason)
            if not any(candidate.kind == "rule" for candidate in candidates):
                continue
            family = hashlib.sha256(case_reason.encode("utf-8")).hexdigest()[:12]
            if (
                not args.row_id
                and family in reason_families
                and len(selected) + 3 < args.limit
            ):
                continue
            reason_families.add(family)
            selected.append((row, case_reason))
            if len(selected) >= max(1, int(args.limit)):
                break

    records: list[dict[str, Any]] = []
    for row, case_reason in selected:
        full_text = str(row.get("full_text") or "")
        if args.local_only:
            candidates = rank_practice_candidates(full_text, case_reason)
            records.append(
                {
                    "row_id": int(row.get("id") or 0),
                    "source_sha256": hashlib.sha256(
                        full_text.encode("utf-8")
                    ).hexdigest(),
                    "issue_sha256": hashlib.sha256(
                        case_reason.encode("utf-8")
                    ).hexdigest(),
                    "success": True,
                    "candidate_count": len(candidates),
                    "rule_candidates": sum(
                        1 for candidate in candidates if candidate.kind == "rule"
                    ),
                    "application_candidates": sum(
                        1
                        for candidate in candidates
                        if candidate.kind == "application"
                    ),
                    "external_call": False,
                }
            )
            continue
        result = summarize_with_nvidia(
            full_text,
            case_reason,
            timeout_sec=args.timeout,
        )
        records.append(
            {
                "row_id": int(row.get("id") or 0),
                "source_sha256": hashlib.sha256(
                    full_text.encode("utf-8")
                ).hexdigest(),
                "issue_sha256": hashlib.sha256(
                    case_reason.encode("utf-8")
                ).hexdigest(),
                **result.audit_dict(),
            }
        )

    accepted = sum(1 for row in records if row.get("success"))
    reviewed_no_insight = sum(
        1 for row in records if row.get("reviewed_no_insight")
    )
    failures = [
        row
        for row in records
        if not row.get("success") and not row.get("reviewed_no_insight")
    ]
    report = {
        "schema_version": 1,
        "checked_at": datetime.now().astimezone().isoformat(),
        "mode": (
            "synthetic_live_nvidia_source_selector"
            if args.synthetic
            else (
                "local_candidate_audit"
                if args.local_only
                else "read_only_live_nvidia_source_selector"
            )
        ),
        "requested": int(args.limit),
        "selected": len(selected),
        "accepted": accepted,
        "reviewed_no_insight": reviewed_no_insight,
        "failed": len(failures),
        "success": bool(selected) and accepted > 0 and not failures,
        "records": records,
    }
    out = Path(args.json_out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
