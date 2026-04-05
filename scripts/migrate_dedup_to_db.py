#!/usr/bin/env python3
"""
將散落的 JSON 去重狀態遷移到 law_firm_data DB
==============================================
一次性遷移腳本 + 建立統一的 dedup_registry 表。

去重表設計：
- 單一表 `dedup_registry` 統一管理所有去重紀錄
- 用 `category` 區分不同功能領域
- 用 `item_key` 做唯一性判斷
- 保留 `metadata` JSON 欄位做彈性擴充

遷移後，原 JSON 檔案保留但不再是 source of truth。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("MigrateDedup")

# Load .env
_SCRIPT_ROOT = Path(__file__).resolve().parents[1]
_env_file = _SCRIPT_ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(_SCRIPT_ROOT)))


def get_conn():
    import mysql.connector
    return mysql.connector.connect(
        host=os.environ.get("OSC_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("OSC_DB_PORT", 3306)),
        user=os.environ.get("OSC_DB_USER", "casper_service"),
        password=os.environ.get("OSC_DB_PASSWORD", ""),
        database="law_firm_data",
    )


def create_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dedup_registry (
            id          BIGINT AUTO_INCREMENT PRIMARY KEY,
            category    VARCHAR(64)   NOT NULL COMMENT '功能領域: download/payment/email/transcript/hearing/laf...',
            item_key    VARCHAR(512)  NOT NULL COMMENT '去重鍵值: 檔名/案號/email_id/...',
            status      VARCHAR(32)   DEFAULT 'done' COMMENT 'done/skipped/pending/error',
            metadata    JSON          DEFAULT NULL COMMENT '額外資訊 (JSON)',
            notified_at DATETIME      DEFAULT NULL COMMENT '通知時間',
            created_at  DATETIME      DEFAULT CURRENT_TIMESTAMP,
            updated_at  DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY  uq_cat_key (category, item_key),
            INDEX       idx_category (category),
            INDEX       idx_created (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        COMMENT='統一去重紀錄表 — 取代散落的 JSON 去重檔案'
    """)
    conn.commit()
    logger.info("✓ dedup_registry table ready")


def migrate_json_file(conn, filepath: str, category: str, key_extractor=None):
    """遷移單個 JSON 檔案到 DB。"""
    p = Path(filepath)
    if not p.exists():
        logger.info(f"  skip {p.name} (not found)")
        return 0

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"  skip {p.name}: {e}")
        return 0

    cur = conn.cursor()
    count = 0

    if isinstance(data, dict):
        for key, val in data.items():
            ts = None
            meta = None
            if isinstance(val, str) and "T" in val:
                ts = val  # ISO timestamp
            elif isinstance(val, dict):
                ts = val.get("notified_at") or val.get("ts") or val.get("timestamp")
                meta = json.dumps(val, ensure_ascii=False, default=str)
            else:
                meta = json.dumps({"value": val}, ensure_ascii=False, default=str)

            try:
                cur.execute(
                    """INSERT INTO dedup_registry (category, item_key, status, metadata, notified_at)
                       VALUES (%s, %s, 'done', %s, %s)
                       ON DUPLICATE KEY UPDATE updated_at=NOW()""",
                    (category, str(key)[:512], meta, ts),
                )
                count += 1
            except Exception as e:
                logger.debug(f"  insert error: {e}")

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                key = item
                meta = None
                ts = None
            elif isinstance(item, dict):
                key = key_extractor(item) if key_extractor else json.dumps(item, ensure_ascii=False, default=str)
                ts = item.get("notified_at") or item.get("ts") or item.get("timestamp")
                meta = json.dumps(item, ensure_ascii=False, default=str)
            else:
                continue

            try:
                cur.execute(
                    """INSERT INTO dedup_registry (category, item_key, status, metadata, notified_at)
                       VALUES (%s, %s, 'done', %s, %s)
                       ON DUPLICATE KEY UPDATE updated_at=NOW()""",
                    (category, str(key)[:512], meta, ts),
                )
                count += 1
            except Exception as e:
                logger.debug(f"  insert error: {e}")

    conn.commit()
    logger.info(f"  ✓ {p.name} → {count} entries as '{category}'")
    return count


def main():
    conn = get_conn()
    create_table(conn)

    total = 0
    dl = MAGI_ROOT / "閱卷下載"
    ce = MAGI_ROOT / "casper_ecosystem" / "law_firm_orchestrators"

    # 1. 閱卷下載
    total += migrate_json_file(conn, dl / "downloaded_registry.json", "download")
    total += migrate_json_file(conn, dl / "notified_cases.json", "payment_notify")
    total += migrate_json_file(conn, dl / "dismissed_payments.json", "payment_dismissed")
    total += migrate_json_file(conn, dl / "payment_registry.json", "payment_track")
    total += migrate_json_file(conn, dl / "payment_proof_registry.json", "payment_proof")
    total += migrate_json_file(conn, dl / ".recent_activity_notified.json", "recent_activity")
    total += migrate_json_file(conn, dl / "apply_registry.json", "apply")
    total += migrate_json_file(conn, dl / "processed_emails.json", "email_filereview")

    # 2. 法扶
    total += migrate_json_file(conn, MAGI_ROOT / "json" / "processed_laf_emails.json", "email_laf")
    total += migrate_json_file(conn, MAGI_ROOT / "json" / "processed_laf_emails_general.json", "email_laf_general")
    total += migrate_json_file(conn, ce / "json" / "processed_laf_emails.json", "email_laf_orc")
    total += migrate_json_file(conn, ce / ".draft_processed_emails.json", "email_draft")
    total += migrate_json_file(conn, ce / "_laf_condition_manual_done.json", "laf_condition_done")
    total += migrate_json_file(conn, ce / "閱卷下載" / "downloaded_registry.json", "download_laf")
    total += migrate_json_file(conn, ce / "閱卷下載" / "processed_emails.json", "email_laf_download")

    # 3. .agent 狀態
    agent = MAGI_ROOT / ".agent"
    total += migrate_json_file(conn, agent / "transcript_index.json", "transcript")
    total += migrate_json_file(conn, agent / "hearing_remind_state.json", "hearing_remind")

    conn.close()
    logger.info(f"\n=== 遷移完成: {total} 筆紀錄 ===")


if __name__ == "__main__":
    main()
