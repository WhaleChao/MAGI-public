#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修復降級摘要 — 一次性清理腳本
================================
掃描 DB 中所有降級/Prompt外洩/亂碼的摘要，重新用標準判決摘要流程生成。
WFGY 已退役，不可再用於修復或蒸餾資料生成。

Usage:
    # 先預覽有多少筆需要修復（不實際修改）
    python3 scripts/ops/fix_degraded_summaries.py --dry-run

    # 實際執行修復（每批 5 筆，離峰時段建議一次跑完）
    python3 scripts/ops/fix_degraded_summaries.py --max 50

    # 只修復特定案件
    python3 scripts/ops/fix_degraded_summaries.py --title-like "113%%訴%%591"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_MAGI_ROOT))
os.chdir(_MAGI_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(_MAGI_ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fix_degraded")

# ── DB connection — 複用 judgment-collector 的完整連線邏輯 ──
def _get_db():
    """嘗試透過 judgment-collector 的 _get_db_config()；若 import 失敗則自行 fallback。"""
    import mysql.connector

    # 方法 1: 複用 judgment-collector 的多 profile 自動偵測邏輯
    try:
        sys.path.insert(0, str(_MAGI_ROOT / "skills" / "judgment-collector"))
        from action import _get_db_config
        cfg = _get_db_config()
        if cfg and cfg.get("host") and cfg.get("user"):
            cfg.setdefault("charset", "utf8mb4")
            cfg.setdefault("connection_timeout", 5)
            logger.info("DB config from judgment-collector: %s@%s/%s",
                        cfg.get("user"), cfg.get("host"), cfg.get("database"))
            return mysql.connector.connect(**cfg)
    except Exception as e:
        logger.warning("judgment-collector config failed (%s), using fallback", e)

    # 方法 2: 從 .env 環境變數組合（judgment-collector 用的 DB 是 law_firm_data）
    host = (os.environ.get("JUDGMENT_DB_HOST")
            or os.environ.get("OSC_DB_HOST")
            or os.environ.get("MAGI_REMOTE_DB_HOST")
            or os.environ.get("DB_HOST")
            or "127.0.0.1")
    port = int(os.environ.get("JUDGMENT_DB_PORT")
               or os.environ.get("OSC_DB_PORT")
               or os.environ.get("MAGI_REMOTE_DB_PORT")
               or "3306")
    user = (os.environ.get("JUDGMENT_DB_USER")
            or os.environ.get("OSC_DB_USER")
            or os.environ.get("MAGI_REMOTE_DB_USER")
            or os.environ.get("DB_USER")
            or "python_user")
    password = (os.environ.get("JUDGMENT_DB_PASSWORD")
                or os.environ.get("OSC_DB_PASSWORD")
                or os.environ.get("MAGI_REMOTE_DB_PASSWORD")
                or os.environ.get("DB_PASSWORD")
                or "")
    database = (os.environ.get("JUDGMENT_DB_NAME")
                or os.environ.get("OSC_DB_NAME")
                or os.environ.get("MAGI_REMOTE_DB_NAME")
                or "law_firm_data")
    logger.info("DB fallback: %s@%s/%s", user, host, database)
    return mysql.connector.connect(
        host=host, port=port, user=user, password=password,
        database=database, charset="utf8mb4", connection_timeout=5,
    )


# ── Degraded summary detection (mirrors judgment-collector) ──
_DEGRADED_PATTERNS = [
    "（系統降級回覆）",
    "(系統降級回覆)",
    "（降級摘要）",
    "(降級摘要)",
    "摘要失敗，前 20 行預覽",
    "請稍後再試",
    "模型忙碌",
    "逾時",
    "你是資深法律研究助理",
    "專精司法見解分析",
    "【摘要格式要求】請嚴格按照",
    "EXECUTE WFGY PROTOCOL",
    "7-STEP REASONING CHAIN",
]


def is_degraded(summary: str) -> str:
    """Returns reason string if degraded, empty string if OK."""
    s = (summary or "").strip()
    if not s:
        return "empty"
    for pat in _DEGRADED_PATTERNS:
        if pat in s:
            return f"pattern:{pat[:20]}"
    # Garbled text check
    garbage = sum(1 for c in s if c == '\ufffd' or (ord(c) < 32 and c not in '\n\r\t'))
    if len(s) > 50 and garbage / len(s) > 0.03:
        return f"garbled:{garbage}/{len(s)}"
    return ""


def main():
    parser = argparse.ArgumentParser(description="修復降級摘要")
    parser.add_argument("--dry-run", action="store_true", help="只顯示需要修復的數量，不實際修改")
    parser.add_argument("--max", type=int, default=10, help="最多修復幾筆 (default: 10)")
    parser.add_argument("--timeout", type=int, default=480, help="每筆摘要的超時秒數 (default: 480)")
    parser.add_argument("--title-like", type=str, default="", help="只修復標題匹配的案件 (SQL LIKE)")
    args = parser.parse_args()

    conn = _get_db()
    cur = conn.cursor(dictionary=True)

    # ── Step 1: Find all degraded summaries ──
    where_clauses = ["summary IS NOT NULL", "summary != ''"]
    like_parts = []
    for pat in _DEGRADED_PATTERNS:
        like_parts.append(f"summary LIKE '%%{pat}%%'")
    where_clauses.append(f"({' OR '.join(like_parts)})")

    if args.title_like:
        where_clauses.append(f"title LIKE '{args.title_like}'")

    sql = f"SELECT id, title, case_reason, full_text_path, LEFT(summary, 100) AS summary_preview FROM judgments WHERE {' AND '.join(where_clauses)} ORDER BY id"
    cur.execute(sql)
    rows = cur.fetchall()

    # Also check for empty summaries with full text
    cur.execute("SELECT id, title, case_reason, full_text_path FROM judgments WHERE (summary IS NULL OR summary = '') AND full_text_path IS NOT NULL AND full_text_path != '' ORDER BY id")
    empty_rows = cur.fetchall()

    logger.info("=== 降級摘要掃描結果 ===")
    logger.info(f"降級/Prompt外洩/亂碼：{len(rows)} 筆")
    logger.info(f"有全文但無摘要：{len(empty_rows)} 筆")
    logger.info(f"總計需修復：{len(rows) + len(empty_rows)} 筆")

    if args.dry_run:
        logger.info("\n--- 降級樣本 (前 20 筆) ---")
        for r in rows[:20]:
            reason = is_degraded(r.get("summary_preview", ""))
            logger.info(f"  [{r['id']}] {r['title']} | 原因: {reason}")
        logger.info("\n--- 無摘要樣本 (前 10 筆) ---")
        for r in empty_rows[:10]:
            logger.info(f"  [{r['id']}] {r['title']} | full_text: {r.get('full_text_path','')[:60]}")
        cur.close()
        conn.close()
        return

    # ── Step 2: Re-summarize ──
    from skills.judgment_collector_module import action as jc
    # Fallback: direct import
    try:
        from skills.judgment_collector_module import action as jc
    except ImportError:
        # Add judgment-collector skill path
        sys.path.insert(0, str(_MAGI_ROOT / "skills" / "judgment-collector"))
        import action as jc

    all_targets = []
    for r in rows:
        all_targets.append(r)
    for r in empty_rows:
        r["summary_preview"] = "(空)"
        all_targets.append(r)

    to_fix = all_targets[:args.max]
    logger.info(f"\n=== 開始修復 {len(to_fix)}/{len(all_targets)} 筆 ===")

    fixed = 0
    failed = 0
    for i, row in enumerate(to_fix):
        rid = row["id"]
        title = row["title"]
        case_reason = row.get("case_reason", "")
        ftp = row.get("full_text_path", "")

        logger.info(f"[{i+1}/{len(to_fix)}] {title} ...")

        # Read full text
        full_text = ""
        if ftp and os.path.exists(ftp):
            try:
                with open(ftp, "r", encoding="utf-8", errors="replace") as f:
                    full_text = f.read()
            except Exception:
                pass

        if not full_text:
            logger.warning(f"  ⚠️ 找不到全文檔案: {ftp}")
            failed += 1
            continue

        t0 = time.time()
        try:
            new_summary = jc._summarize_judgment(full_text, case_reason, timeout_sec=args.timeout)
        except Exception as e:
            logger.error(f"  ❌ 摘要失敗: {e}")
            failed += 1
            continue

        elapsed = time.time() - t0

        if not new_summary or jc._is_degraded_summary(new_summary, case_reason):
            logger.warning(f"  ⚠️ 新摘要仍然降級 ({elapsed:.0f}s)")
            failed += 1
            continue

        # Update DB
        try:
            cur2 = conn.cursor()
            cur2.execute(
                "UPDATE judgments SET summary = %s WHERE id = %s",
                (new_summary, rid),
            )
            conn.commit()
            cur2.close()
            fixed += 1
            logger.info(f"  ✅ 已更新 ({elapsed:.0f}s, {len(new_summary)} chars)")
        except Exception as e:
            logger.error(f"  ❌ DB 更新失敗: {e}")
            failed += 1

    logger.info(f"\n=== 修復完成 ===")
    logger.info(f"成功：{fixed}，失敗：{failed}，剩餘：{len(all_targets) - len(to_fix)}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
