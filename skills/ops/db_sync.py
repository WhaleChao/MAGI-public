#!/usr/bin/env python3
"""
Dual-DB Incremental Sync (db_sync.py)
=====================================
Synchronizes records between the Tailscale Database and Localhost fallback.
It reads the connections from `legalbridge_config.json` via LegalBridgeCore and incrementally 
syncs differences in `cases`, `clients`, `case_todos`, `email_drafts`, etc. using updated_at timestamps.
"""
import os
import sys
import logging
import pymysql
import argparse
from pathlib import Path

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path, get_config_path, get_orch_dir

CODE_DIR = str(get_orch_dir())
ensure_orch_on_sys_path()

from legalbridge_core import ConfigManager

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 31, exc_info=True)

try:
    from line_notifier import LAFNotifier
except ImportError:
    LAFNotifier = None

logger = logging.getLogger("db-sync")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

# Important Tables to keep in sync
SYNC_TABLES = [
    "clients",
    "cases",
    "case_todos",
    # "email_drafts",  # Removed: table does not exist on remote Windows DB (law_firm_data)
    "laf_email_records",
    "calendar_events" 
]

def check_db_availability() -> dict:
    import json
    cfg_path = str(get_config_path("legalbridge_config.json"))
    if not os.path.exists(cfg_path):
        cfg_path = str(get_config_path("config.json"))
    
    profiles = []
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            profiles = data.get("mariadb_profiles", [])
            
    if not profiles:
        logger.error("No mariadb_profiles found in config.")
        return {}
        
    conns = {}
    for p in profiles:
        name = p.get("profile_name")
        cfg = p.get("config", {})
        try:
            conn = pymysql.connect(
                host=cfg.get("host", "localhost"),
                port=cfg.get("port", 3306),
                user=cfg.get("user") or os.environ.get("OSC_DB_USER", "root"),
                password=cfg.get("password") or os.environ.get("OSC_DB_PASSWORD", ""),
                database=cfg.get("database", "law_firm_data"),
                charset=cfg.get("charset", "utf8mb4"),
                connect_timeout=3,
                autocommit=True,
                cursorclass=pymysql.cursors.DictCursor
            )
            conns[name] = conn
            logger.info(f"✅ Connected to Profile: {name}")
        except Exception as e:
            logger.warning(f"❌ Failed to connect to {name}: {e}")
            
    return conns

def get_last_sync_time(conn1, conn2) -> str:
    # A lightweight implementation: fetch highest updated_at to compare drifts
    pass

def sync_table(source_name: str, source_conn, target_name: str, target_conn, table: str) -> dict:
    logger.info(f"Syncing [{table}] from {source_name} -> {target_name}...")
    s_cursor = source_conn.cursor()
    t_cursor = target_conn.cursor()

    stats = {
        "table": table,
        "inserted": 0,
        "updated": 0,
        "skipped_older": 0,
        "duplicate_skipped": 0,
        "errors": 0,
        "skipped": False,
    }

    # Get shared schema (columns) and skip safely if table missing on either side.
    try:
        s_cursor.execute(f"DESCRIBE {table}")
        s_desc = s_cursor.fetchall() or []
    except Exception as e:
        logger.warning(f"  -> Skip [{table}] on {source_name}: {e}")
        stats["skipped"] = True
        return stats
    try:
        t_cursor.execute(f"DESCRIBE {table}")
        t_desc = t_cursor.fetchall() or []
    except Exception as e:
        logger.warning(f"  -> Skip [{table}] on {target_name}: {e}")
        stats["skipped"] = True
        return stats

    s_columns = [row["Field"] for row in s_desc if row.get("Field")]
    t_columns = {row["Field"] for row in t_desc if row.get("Field")}
    columns = [c for c in s_columns if c in t_columns]
    if not columns:
        logger.warning(f"  -> Skip [{table}] no shared columns")
        stats["skipped"] = True
        return stats

    pk = "id" if "id" in columns else ""
    if not pk:
        # DESCRIBE returns PRI flag in "Key"
        for row in s_desc:
            if str(row.get("Key") or "").upper() == "PRI" and row.get("Field") in t_columns:
                pk = str(row.get("Field"))
                break
    if not pk:
        logger.warning(f"  -> Skip [{table}] no shared primary key")
        stats["skipped"] = True
        return stats

    try:
        time_col = "updated_at" if "updated_at" in columns else None
        if time_col:
            s_cursor.execute(f"SELECT * FROM {table} WHERE updated_at >= NOW() - INTERVAL 1 DAY")
        else:
            s_cursor.execute(f"SELECT {', '.join(columns)} FROM {table}")
            
        source_rows = s_cursor.fetchall() or []
        if not source_rows:
            return stats
            
        for s_row in source_rows:
            row_id = None
            try:
                row_id = s_row.get(pk)
                if row_id is None:
                    stats["errors"] += 1
                    continue
                t_cursor.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {pk} = %s", (row_id,))
                t_row_list = t_cursor.fetchall() or []
                
                if not t_row_list:
                    # INSERT
                    col_names = ", ".join(columns)
                    placeholders = ", ".join(["%s"] * len(columns))
                    insert_sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
                    t_cursor.execute(insert_sql, tuple(s_row.get(col) for col in columns))
                    stats["inserted"] += 1
                else:
                    # UPDATE with Conflict Resolution (Last Write Wins via updated_at)
                    t_row = t_row_list[0]
                    if time_col and s_row.get(time_col) and t_row.get(time_col):
                        # Ensure we operate on cleanly localized or naive datetimes consistently.
                        # Since both are fetched from DB, simple comparison works if TZ settings match.
                        # If target is newer or equal, DO NOT overwrite it.
                        if t_row.get(time_col) >= s_row.get(time_col):
                            stats["skipped_older"] += 1
                            continue
                            
                    updates = ", ".join([f"{col}=%s" for col in columns if col != pk])
                    if not updates:
                        stats["skipped_older"] += 1
                        continue
                    update_sql = f"UPDATE {table} SET {updates} WHERE {pk}=%s"
                    up_vals = [s_row.get(c) for c in columns if c != pk] + [row_id]
                    t_cursor.execute(update_sql, tuple(up_vals))
                    stats["updated"] += 1
            except Exception as e:
                code = None
                try:
                    code = int((e.args or [None])[0])
                except Exception:
                    code = None
                msg = str(e or "")
                if code == 1062 or "Duplicate entry" in msg:
                    stats["duplicate_skipped"] += 1
                    # Avoid flooding logs for large historical calendars.
                    if stats["duplicate_skipped"] <= 5:
                        logger.info(f"  -> Duplicate skipped row ID {row_id} in {table}")
                    continue
                logger.error(f"  -> Error syncing row ID {row_id} in {table}: {e}")
                stats["errors"] += 1
                
        logger.info(
            f"  -> {table} sync complete: "
            f"Inserts: {stats['inserted']}, Updates: {stats['updated']}, "
            f"Skipped (Target Newer): {stats['skipped_older']}, "
            f"Skipped (Duplicate): {stats['duplicate_skipped']}"
        )
    except Exception as e:
        logger.error(f"  -> Failed to query {table}: {e}")
        stats["errors"] += 1
        
    return stats


def cmd_sync():
    logger.info("Starting Dual-DB Sync Initialization...")
    active_conns = check_db_availability()
    
    if len(active_conns) < 2:
        msg = f"Only {len(active_conns)} DB(s) online. Sync aborted. (Need 2+)"
        logger.warning(msg)
        for c in active_conns.values(): c.close()
        if LAFNotifier:
            LAFNotifier().notify_admin(
                f"⚠️ DB Sync Failed: {msg}",
                topic_key="check",
                source="db_dual_sync",
            )
        return
        
    names = list(active_conns.keys())
    db1_name, db1_conn = names[0], active_conns[names[0]]
    db2_name, db2_conn = names[1], active_conns[names[1]]
    
    logger.info(f"Executing Bidirectional Sync between {db1_name} <-> {db2_name}")
    
    total_stats = []
    
    # Sync DB1 -> DB2
    for t in SYNC_TABLES:
        res = sync_table(db1_name, db1_conn, db2_name, db2_conn, t)
        total_stats.append({**res, "dir": f"{db1_name}->{db2_name}"})
        
    # Sync DB2 -> DB1
    for t in SYNC_TABLES:
        res = sync_table(db2_name, db2_conn, db1_name, db1_conn, t)
        total_stats.append({**res, "dir": f"{db2_name}->{db1_name}"})
        
    logger.info("✅ Sync complete.")
    
    db1_conn.close()
    db2_conn.close()
    
    if LAFNotifier:
        report_lines = ["🔄 **Daily Dual-DB Sync Audit Report**", ""]
        has_updates = False
        
        for st in total_stats:
            if st["inserted"] > 0 or st["updated"] > 0 or st["errors"] > 0:
                has_updates = True
                flag = "❌" if st["errors"] > 0 else "✅"
                report_lines.append(f"{flag} **{st['table']}** (`{st['dir']}`):")
                report_lines.append(f"   Inserts: {st['inserted']}, Updates: {st['updated']}, Skipped (Stale): {st['skipped_older']}, Errors: {st['errors']}")

        if not has_updates:
            report_lines.append("No drift observed today. Both local and Tailscale endpoints are perfectly synchronized.")
            
        # DB Sync 是運維通知，只發 TG，不 mirror 到 DC
        _report_text = "\n".join(report_lines)
        try:
            from skills.ops.red_phone import _get_telegram_config  # type: ignore
            from urllib import request as _urlreq
            _token, _admin_ids = _get_telegram_config()
            if _token and _admin_ids:
                for _cid in _admin_ids:
                    _payload = json.dumps({"chat_id": _cid, "text": _report_text}).encode("utf-8")
                    _req = _urlreq.Request(
                        f"https://api.telegram.org/bot{_token}/sendMessage",
                        data=_payload, method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    _urlreq.urlopen(_req, timeout=10)
        except Exception:
            LAFNotifier().notify_admin(_report_text, topic_key="check", source="db_dual_sync")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MAGI DB Sync")
    parser.add_argument("--task", type=str, required=True, choices=["sync"])
    args = parser.parse_args()
    
    if args.task == "sync":
        cmd_sync()
