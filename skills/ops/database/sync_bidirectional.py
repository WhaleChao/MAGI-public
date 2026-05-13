#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bidirectional sync for law_firm_data between:
- Remote Keeper DB (default MAGI_REMOTE_DB_HOST:3306)
- Local fallback DB (from Studio_Local/Home_Local_Test profile)

Policy:
- Upsert only, never delete.
- Designed for offline catch-up: push local inserts/updates back to remote.
"""

from __future__ import annotations
import logging

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymysql

_MAGI_ROOT = Path(__file__).resolve().parents[3]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import config_candidates


def _remote_db_ip_or(fallback: str) -> str:
    try:
        from api.routing.node_registry import get_node_ip
        return get_node_ip("nas") or fallback
    except Exception:
        return fallback

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 36, exc_info=True)

CONFIG_CANDIDATES = [str(p) for p in config_candidates("config.json")]

TIME_COLUMNS = ("updated_at", "modified_at", "update_time", "updated_date", "created_at", "created_date")


@dataclass
class DBProfile:
    name: str
    host: str
    port: int
    user: str
    password: str
    database: str
    connection_timeout: int = 5


def _load_profiles() -> Dict[str, DBProfile]:
    cfg: Dict[str, Any] = {}
    for p in CONFIG_CANDIDATES:
        pp = Path(p)
        if not pp.exists():
            continue
        try:
            cfg = json.loads(pp.read_text(encoding="utf-8")) or {}
            if isinstance(cfg, dict):
                break
        except Exception:
            continue

    out: Dict[str, DBProfile] = {}
    for row in (cfg.get("mariadb_profiles") or []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("profile_name") or "").strip()
        c = row.get("config") if isinstance(row.get("config"), dict) else {}
        if not name:
            continue
        try:
            out[name] = DBProfile(
                name=name,
                host=str(c.get("host") or "127.0.0.1"),
                port=int(c.get("port") or 3306),
                user=str(c.get("user") or os.environ.get("OSC_DB_USER", "python_user")),
                password=str(c.get("password") or os.environ.get("OSC_DB_PASSWORD", "")),
                database=str(c.get("database") or "law_firm_data"),
                connection_timeout=int(c.get("connection_timeout") or 5),
            )
        except Exception:
            continue
    return out


def _connect(profile: DBProfile):
    return pymysql.connect(
        host=profile.host,
        port=int(profile.port),
        user=profile.user,
        password=profile.password,
        database=profile.database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=max(2, int(profile.connection_timeout or 5)),
        cursorclass=pymysql.cursors.Cursor,
    )


def _choose_remote_profile(profiles: Dict[str, DBProfile]) -> DBProfile:
    p = profiles.get("Studio_VPN_Remote")
    if p:
        return p
    return DBProfile(
        name="Studio_VPN_Remote",
        host=os.environ.get("MAGI_REMOTE_DB_HOST") or _remote_db_ip_or(""),
        port=int(os.environ.get("MAGI_REMOTE_DB_PORT", "3306")),
        user=os.environ.get("MAGI_REMOTE_DB_USER", "python_user"),
        password=os.environ.get("MAGI_REMOTE_DB_PASSWORD", ""),
        database=os.environ.get("MAGI_REMOTE_DB_NAME", "law_firm_data"),
        connection_timeout=int(os.environ.get("MAGI_REMOTE_DB_TIMEOUT", "5")),
    )


def _choose_local_profile(profiles: Dict[str, DBProfile]) -> DBProfile:
    for name in ("Studio_Local", "Home_Local_Test"):
        p = profiles.get(name)
        if p:
            try:
                conn = _connect(p)
                conn.close()
                return p
            except Exception:
                continue
    # fallback for docker local
    return DBProfile(
        name="Home_Local_Test",
        host=os.environ.get("MAGI_LOCAL_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MAGI_LOCAL_DB_PORT", "3307")),
        user=os.environ.get("MAGI_LOCAL_DB_USER", "python_user"),
        password=os.environ.get("MAGI_LOCAL_DB_PASSWORD", ""),
        database=os.environ.get("MAGI_LOCAL_DB_NAME", "law_firm_data"),
        connection_timeout=int(os.environ.get("MAGI_LOCAL_DB_TIMEOUT", "5")),
    )


def _qname(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _show_tables(conn) -> List[str]:
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        rows = cur.fetchall() or []
    return [str(r[0]) for r in rows if r and r[0]]


def _table_columns(conn, table: str) -> List[Tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {_qname(table)}")
        rows = cur.fetchall() or []
    out: List[Tuple[str, str]] = []
    for r in rows:
        if not r:
            continue
        col = str(r[0])
        typ = str(r[1] or "")
        out.append((col, typ))
    return out


def _primary_key(conn, table: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW KEYS FROM {_qname(table)} WHERE Key_name='PRIMARY'")
        rows = cur.fetchall() or []
    try:
        rows = sorted(rows, key=lambda r: int(r[3] if len(r) > 3 and r[3] is not None else 9999))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 173, exc_info=True)
    if len(rows) != 1:
        return None
    return str(rows[0][4]) if rows and len(rows[0]) > 4 else None


def _is_numeric_sql_type(t: str) -> bool:
    low = str(t or "").lower()
    return low.startswith(("tinyint", "smallint", "mediumint", "int", "bigint", "decimal", "numeric", "float", "double"))


def _best_time_col(cols: Sequence[Tuple[str, str]]) -> Optional[str]:
    names = {str(c[0]).lower(): str(c[0]) for c in cols}
    for c in TIME_COLUMNS:
        if c in names:
            return names[c]
    return None


def _build_upsert_sql(table: str, cols: Sequence[str], pk: str) -> str:
    csql = ", ".join(_qname(c) for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    non_pk = [c for c in cols if c != pk]
    
    tcol = _best_time_col([(c, "") for c in cols])
    
    if non_pk:
        if tcol:
            # Last Write Wins mechanism
            # Only overwrite if the incoming row's time > the existing row's time.
            up = ", ".join(f"{_qname(c)} = IF(VALUES({_qname(tcol)}) >= {_qname(tcol)}, VALUES({_qname(c)}), {_qname(c)})" for c in non_pk)
        else:
            up = ", ".join(f"{_qname(c)} = VALUES({_qname(c)})" for c in non_pk)
            
        return f"INSERT INTO {_qname(table)} ({csql}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {up}"
    
    return f"INSERT IGNORE INTO {_qname(table)} ({csql}) VALUES ({placeholders})"


def _safe_upsert_many(dst_conn, upsert_sql: str, rows: Sequence[Tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    try:
        with dst_conn.cursor() as dcur:
            dcur.executemany(upsert_sql, rows)
        dst_conn.commit()
        return len(rows)
    except (pymysql.err.IntegrityError, pymysql.err.DataError) as e:
        # Fallback to row-by-row upsert:
        # - skip duplicate-key rows (1062)
        # - skip value/type incompatible rows (1366, 1265, 1264, 1406)
        code = int(e.args[0]) if getattr(e, "args", None) else -1
        if code not in {1062, 1366, 1265, 1264, 1406}:
            raise
        applied = 0
        with dst_conn.cursor() as dcur:
            for row in rows:
                try:
                    dcur.execute(upsert_sql, row)
                    applied += 1
                except (pymysql.err.IntegrityError, pymysql.err.DataError) as e2:
                    code2 = int(e2.args[0]) if getattr(e2, "args", None) else -1
                    if code2 in {1062, 1366, 1265, 1264, 1406}:
                        continue
                    raise
        dst_conn.commit()
        return applied


def _sync_table(
    src_conn,
    dst_conn,
    table: str,
    *,
    chunk_size: int,
    update_window_days: int,
    recent_limit: int,
) -> Dict[str, Any]:
    res: Dict[str, Any] = {
        "table": table,
        "ok": True,
        "pk": None,
        "pk_type": "",
        "time_col": None,
        "copied_by_pk": 0,
        "upserted_recent": 0,
        "skipped": False,
        "reason": "",
    }

    src_cols = _table_columns(src_conn, table)
    dst_cols = _table_columns(dst_conn, table)
    if not src_cols or not dst_cols:
        res["skipped"] = True
        res["reason"] = "no_columns"
        return res

    dst_col_set = {c[0] for c in dst_cols}
    cols = [c for c in src_cols if c[0] in dst_col_set]
    if not cols:
        res["skipped"] = True
        res["reason"] = "no_shared_columns"
        return res
    col_names = [c[0] for c in cols]

    pk_src = _primary_key(src_conn, table)
    pk_dst = _primary_key(dst_conn, table)
    pk = pk_src or pk_dst
    res["pk"] = pk
    tcol = _best_time_col(cols)
    res["time_col"] = tcol

    if not pk or pk not in col_names:
        res["skipped"] = True
        res["reason"] = "no_shared_primary_key"
        return res

    type_map = {c[0]: c[1] for c in src_cols}
    pk_type = type_map.get(pk, "")
    res["pk_type"] = pk_type
    upsert_sql = _build_upsert_sql(table, col_names, pk)

    try:
        # Pass-1: append rows with greater numeric PK (fast path)
        if _is_numeric_sql_type(pk_type):
            with dst_conn.cursor() as dcur:
                dcur.execute(f"SELECT COALESCE(MAX({_qname(pk)}), 0) FROM {_qname(table)}")
                dst_max = dcur.fetchone()[0] or 0

            copied = 0
            while True:
                with src_conn.cursor() as scur:
                    scur.execute(
                        f"SELECT {', '.join(_qname(c) for c in col_names)} FROM {_qname(table)} "
                        f"WHERE {_qname(pk)} > %s ORDER BY {_qname(pk)} ASC LIMIT %s",
                        (dst_max, int(chunk_size)),
                    )
                    batch = scur.fetchall() or []
                if not batch:
                    break
                copied += _safe_upsert_many(dst_conn, upsert_sql, batch)
                dst_max = batch[-1][col_names.index(pk)]
                if len(batch) < int(chunk_size):
                    break
            res["copied_by_pk"] = copied

        # Pass-2: upsert recent changed rows (covers update edits)
        if tcol:
            with src_conn.cursor() as scur:
                scur.execute(
                    f"SELECT {', '.join(_qname(c) for c in col_names)} FROM {_qname(table)} "
                    f"WHERE {_qname(tcol)} >= DATE_SUB(NOW(), INTERVAL %s DAY) "
                    f"ORDER BY {_qname(tcol)} DESC LIMIT %s",
                    (max(1, int(update_window_days)), max(100, int(recent_limit))),
                )
                rows = scur.fetchall() or []
            if rows:
                res["upserted_recent"] = _safe_upsert_many(dst_conn, upsert_sql, rows)

        return res
    except Exception as e:
        try:
            dst_conn.rollback()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 337, exc_info=True)
        res["ok"] = False
        res["reason"] = f"{type(e).__name__}: {e}"
        return res


def sync_bidirectional(
    *,
    tables: Optional[List[str]] = None,
    chunk_size: int = 800,
    update_window_days: int = 21,
    recent_limit: int = 5000,
) -> Dict[str, Any]:
    started = datetime.now().isoformat(timespec="seconds")
    profiles = _load_profiles()
    remote_profile = _choose_remote_profile(profiles)
    local_profile = _choose_local_profile(profiles)

    out: Dict[str, Any] = {
        "ok": False,
        "started_at": started,
        "remote_profile": remote_profile.name,
        "local_profile": local_profile.name,
        "remote_target": f"{remote_profile.host}:{remote_profile.port}/{remote_profile.database}",
        "local_target": f"{local_profile.host}:{local_profile.port}/{local_profile.database}",
        "tables": [],
        "summary": {},
    }

    remote = None
    local = None
    try:
        remote = _connect(remote_profile)
        local = _connect(local_profile)

        rt = set(_show_tables(remote))
        lt = set(_show_tables(local))
        common = sorted(rt & lt)
        if tables:
            wanted = {t.strip() for t in tables if str(t).strip()}
            common = [t for t in common if t in wanted]

        excluded_prefix = ("tmp_",)
        common = [t for t in common if not t.lower().startswith(excluded_prefix)]

        pull_results = []
        push_results = []

        for t in common:
            pull_results.append(
                _sync_table(
                    remote,
                    local,
                    t,
                    chunk_size=chunk_size,
                    update_window_days=update_window_days,
                    recent_limit=recent_limit,
                )
            )
        for t in common:
            push_results.append(
                _sync_table(
                    local,
                    remote,
                    t,
                    chunk_size=chunk_size,
                    update_window_days=update_window_days,
                    recent_limit=recent_limit,
                )
            )

        out["tables"] = [
            {
                "table": t,
                "pull_remote_to_local": pull_results[idx],
                "push_local_to_remote": push_results[idx],
            }
            for idx, t in enumerate(common)
        ]
        out["summary"] = {
            "tables_considered": len(common),
            "pull_ok": sum(1 for x in pull_results if x.get("ok", False)),
            "push_ok": sum(1 for x in push_results if x.get("ok", False)),
            "pull_copied_by_pk": sum(int(x.get("copied_by_pk") or 0) for x in pull_results),
            "push_copied_by_pk": sum(int(x.get("copied_by_pk") or 0) for x in push_results),
            "pull_upserted_recent": sum(int(x.get("upserted_recent") or 0) for x in pull_results),
            "push_upserted_recent": sum(int(x.get("upserted_recent") or 0) for x in push_results),
            "failed_tables": sorted(
                {
                    str(x.get("table"))
                    for x in (pull_results + push_results)
                    if not x.get("ok", False)
                }
            ),
        }
        out["ok"] = len(out["summary"]["failed_tables"]) == 0
        return out
    except Exception as e:
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        for conn in (remote, local):
            try:
                if conn:
                    conn.close()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 444, exc_info=True)
        out["finished_at"] = datetime.now().isoformat(timespec="seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description="Bidirectional sync for law_firm_data (remote <-> local)")
    ap.add_argument("--tables", type=str, default="", help="Comma-separated table names to sync (default: all common tables)")
    ap.add_argument("--chunk-size", type=int, default=800)
    ap.add_argument("--update-window-days", type=int, default=21)
    ap.add_argument("--recent-limit", type=int, default=5000)
    args = ap.parse_args()

    table_list = [x.strip() for x in str(args.tables or "").split(",") if x.strip()]
    result = sync_bidirectional(
        tables=table_list or None,
        chunk_size=max(100, int(args.chunk_size or 800)),
        update_window_days=max(1, int(args.update_window_days or 21)),
        recent_limit=max(200, int(args.recent_limit or 5000)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
