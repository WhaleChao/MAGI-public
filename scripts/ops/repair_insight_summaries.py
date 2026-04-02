#!/usr/bin/env python3
"""
repair_insight_summaries.py
===========================
掃描 legal_insights 表中 insight_text 為原始判決片段（非摘要）的條目，
呼叫 insight-refine 技能重新產生摘要，並更新資料庫。

Usage:
    python repair_insight_summaries.py [--dry-run] [--limit 10] [--timeout 420]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import sys
import time
from typing import Optional

sys.path.insert(0, os.environ.get("MAGI_ROOT", _MAGI_ROOT))
from skills.bridge.inference_gateway import InferenceGateway

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("repair-insights")

# ---------------------------------------------------------------------------
# DB connection (reuse judgment-collector's config resolution)
# ---------------------------------------------------------------------------

def _get_db():
    import pymysql
    host = os.environ.get("OSC_DB_HOST") or os.environ.get("MAGI_DB_HOST") or "localhost"
    user = os.environ.get("OSC_DB_USER") or os.environ.get("MAGI_DB_USER") or ""
    pwd = os.environ.get("OSC_DB_PASSWORD") or os.environ.get("MAGI_DB_PASSWORD") or ""
    dbname = os.environ.get("OSC_DB_NAME") or os.environ.get("MAGI_DB_NAME") or ""
    port = int(os.environ.get("OSC_DB_PORT") or os.environ.get("MAGI_DB_PORT") or "3306")
    return pymysql.connect(host=host, user=user, password=pwd, database=dbname,
                           port=port, charset="utf8mb4", autocommit=True)


def _is_raw_unsummarized(text: str) -> bool:
    """Detect entries that contain raw judgment text instead of proper summaries."""
    s = (text or "").strip()
    if not s:
        return True
    if len(s) < 15:
        return True
    # Raw text patterns — these are the 107 broken entries
    raw_patterns = [
        "判決連結：\n",
        "判決連結：\nhttps://",
        "[判決文件 第",
        "判決連結：\nSCDM,",
        "判決連結：\nTPSM,",
    ]
    for pat in raw_patterns:
        if s.startswith(pat):
            return True
    # Known degraded markers
    degraded = ["（系統降級回覆）", "(系統降級回覆)", "摘要失敗，前 20 行預覽",
                "請稍後再試", "模型忙碌", "逾時", "timeout"]
    if any(f in s for f in degraded):
        return True
    return False


def _extract_raw_text_from_insight(text: str) -> str:
    """Extract the raw judgment excerpt from the broken insight_text."""
    s = (text or "").strip()
    # Remove "判決連結：\n..." prefix to get the actual legal text
    if "實務見解：\n" in s:
        return s.split("實務見解：\n", 1)[1].strip()
    if "[判決文件" in s:
        # Remove the header line
        lines = s.split("\n")
        return "\n".join(lines[1:]).strip()
    return s


def _summarize_via_gateway(raw_text: str, case_reason: str, timeout_sec: int = 420) -> Optional[dict]:
    """Use unified gateway summarization with degraded metadata."""
    try:
        gateway = InferenceGateway()
        result = gateway.dispatch(
            prompt=raw_text,
            task_type="repair_insight_summary",
            timeout=timeout_sec,
            force_quality=os.environ.get("MAGI_REPAIR_FORCE_QUALITY", "0").strip().lower() in {"1", "true", "yes", "on"},
        )
        if not isinstance(result, dict) or not result.get("success"):
            return None
        summary = (result.get("summary") or result.get("response") or result.get("text") or "").strip()
        if not summary:
            return None
        return {
            "summary": summary,
            "is_degraded": bool(result.get("degraded", False) or _is_raw_unsummarized(summary)),
            "route": str(result.get("route") or ""),
            "error": str(result.get("error") or ""),
        }
    except Exception as e:
        logger.warning("Summarize via gateway failed: %s", e)
        return None


def _update_insight(cur, rid: int, summary: str, is_degraded: bool) -> None:
    """Best-effort backward compatible DB update."""
    try:
        cur.execute(
            "UPDATE legal_insights SET insight_text = %s, is_degraded = %s WHERE id = %s",
            (summary, 1 if is_degraded else 0, rid),
        )
    except Exception as e:
        logger.warning("Update with is_degraded failed, fallback to text-only update: %s", e)
        cur.execute(
            "UPDATE legal_insights SET insight_text = %s WHERE id = %s",
            (summary, rid),
        )


def _ensure_legal_insights_degraded_column(cur) -> None:
    try:
        cur.execute("ALTER TABLE legal_insights ADD COLUMN is_degraded TINYINT(1) NOT NULL DEFAULT 0")
    except Exception as e:
        logger.info("is_degraded column exists or cannot be altered now: %s", e)


def main():
    parser = argparse.ArgumentParser(description="Repair broken legal insight summaries")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't update DB")
    parser.add_argument("--limit", type=int, default=10, help="Max entries to repair per run")
    parser.add_argument("--timeout", type=int, default=420, help="Timeout per summarization (sec)")
    args = parser.parse_args()

    # Load env
    env_path = os.path.join(os.environ.get("MAGI_ROOT", _MAGI_ROOT), ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip()

    conn = _get_db()
    cur = conn.cursor(cursor=__import__("pymysql").cursors.DictCursor)
    _ensure_legal_insights_degraded_column(cur)

    # Find broken entries
    cur.execute("""
        SELECT id, case_number, document_name, case_reason, insight_text, raw_text
        FROM legal_insights
        ORDER BY id ASC
    """)
    all_rows = cur.fetchall()

    broken = [r for r in all_rows if _is_raw_unsummarized(r.get("insight_text", ""))]
    logger.info("Total insights: %d, broken: %d, limit: %d", len(all_rows), len(broken), args.limit)

    repaired = 0
    failed = 0
    skipped = 0

    for row in broken[:args.limit]:
        rid = row["id"]
        case_reason = row.get("case_reason") or ""
        insight_text = row.get("insight_text") or ""
        raw_text = row.get("raw_text") or ""

        # Prefer raw_text if available, else extract from insight_text
        source_text = raw_text.strip() if raw_text.strip() else _extract_raw_text_from_insight(insight_text)
        if not source_text or len(source_text) < 50:
            logger.warning("  [%d] Skipping — source text too short (%d chars)", rid, len(source_text))
            skipped += 1
            continue

        logger.info("  [%d] Resummarizing (source: %d chars, reason: %s)...",
                     rid, len(source_text), case_reason[:30])

        summary_pack = _summarize_via_gateway(source_text, case_reason, timeout_sec=args.timeout)
        if summary_pack:
            summary = str(summary_pack.get("summary") or "")
            is_degraded = bool(summary_pack.get("is_degraded", True))
            route = str(summary_pack.get("route") or "")
            if args.dry_run:
                logger.info(
                    "    [DRY RUN] Summary ready [%d] len=%d degraded=%s route=%s",
                    rid,
                    len(summary),
                    is_degraded,
                    route,
                )
                repaired += 1
            else:
                _update_insight(cur, rid, summary, is_degraded)
                logger.info(
                    "    ✅ Repaired [%d] → %d chars (degraded=%s route=%s)",
                    rid,
                    len(summary),
                    is_degraded,
                    route,
                )
                repaired += 1
        else:
            logger.warning("    ❌ Failed [%d]", rid)
            failed += 1

        # Brief pause to avoid overwhelming the model
        time.sleep(2)

    result = {
        "ok": True,
        "total_broken": len(broken),
        "processed": min(args.limit, len(broken)),
        "repaired": repaired,
        "failed": failed,
        "skipped": skipped,
        "dry_run": args.dry_run,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
