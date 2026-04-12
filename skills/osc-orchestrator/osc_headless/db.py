# -*- coding: utf-8 -*-
"""
OSC headless DB helpers.

Design goals:
- Works even when the main OSC GUI can't run (no tkinter).
- Defaults to Casper local MariaDB (docker on 127.0.0.1:3307).
- Never deletes data; only CREATE TABLE IF NOT EXISTS / INSERT.
"""

from __future__ import annotations
import logging

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import json
import os
import sys
from pathlib import Path

import mysql.connector

_MAGI_ROOT = Path(__file__).resolve().parents[3]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import config_candidates

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 33, exc_info=True)

@dataclass(frozen=True)
class DBConfig:
    host: str = os.environ.get("OSC_DB_HOST", "") or os.environ.get("DB_HOST", "127.0.0.1")
    port: int = int(os.environ.get("OSC_DB_PORT", "") or os.environ.get("DB_PORT", "3306"))
    user: str = os.environ.get("OSC_DB_USER", "") or os.environ.get("DB_USER", "")
    password: str = os.environ.get("OSC_DB_PASSWORD", "") or os.environ.get("DB_PASSWORD", "")
    database: str = os.environ.get("OSC_DB_NAME", "law_firm_data")
    connection_timeout: int = 5


def _failover_host() -> str:
    """透過 db_failover 動態取得目前可用的 DB host。"""
    try:
        from api.db_failover import probe_remote, get_osc_host, _switch_to_local
        if not probe_remote():
            _switch_to_local()
        return get_osc_host()
    except Exception:
        return os.environ.get("OSC_DB_HOST", "127.0.0.1")


def _has_explicit_env(prefix: str = "OSC_DB_") -> bool:
    keys = ["HOST", "PORT", "USER", "PASSWORD", "NAME"]
    for k in keys:
        if (os.environ.get(f"{prefix}{k}", "") or "").strip():
            return True
    return False


def _profile_candidates(prefer_local: bool) -> List[DBConfig]:
    """
    Load DB profile candidates from code config files.
    Order:
    - remote-first (default): Studio_VPN_Remote -> Studio_Local -> Home_Local_Test
    - local-first: Studio_Local -> Home_Local_Test -> Studio_VPN_Remote
    """
    config_paths = [str(p) for p in config_candidates("config.json")]
    cfg_obj = {}
    for p in config_paths:
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    cfg_obj = json.load(f) or {}
                if isinstance(cfg_obj, dict):
                    break
        except Exception:
            continue

    raw_profiles = cfg_obj.get("mariadb_profiles")
    profiles = {}
    if isinstance(raw_profiles, list):
        for it in raw_profiles:
            if not isinstance(it, dict):
                continue
            name = str(it.get("profile_name") or "").strip()
            conf = it.get("config") if isinstance(it.get("config"), dict) else {}
            if not name:
                continue
            profiles[name] = conf

    if prefer_local:
        order = ["Studio_Local", "Home_Local_Test", "Studio_VPN_Remote"]
    else:
        order = ["Studio_VPN_Remote", "Studio_Local", "Home_Local_Test"]

    out: List[DBConfig] = []
    for name in order:
        c = profiles.get(name)
        if not isinstance(c, dict):
            continue
        try:
            out.append(
                DBConfig(
                    host=str(c.get("host") or "127.0.0.1"),
                    port=int(c.get("port") or 3306),
                    user=str(c.get("user") or os.environ.get("OSC_DB_USER", "python_user")),
                    password=str(c.get("password") or os.environ.get("OSC_DB_PASSWORD", "")),
                    database=str(c.get("database") or "law_firm_data"),
                    connection_timeout=int(c.get("connection_timeout") or 5),
                )
            )
        except Exception:
            continue
    return out


def db_config_from_env(prefix: str = "OSC_DB_") -> DBConfig:
    def _normalize_db_name(name: str) -> str:
        """
        兼容使用者口語/舊設定：
        - law_firm_db (legacy) -> law_firm_data (現行)
        """
        n = (name or "").strip()
        if n.lower() == "law_firm_db":
            return "law_firm_data"
        return n or "law_firm_data"

    def _get(name: str, default: str) -> str:
        v = os.environ.get(f"{prefix}{name}", "").strip()
        return v or default

    # If caller explicitly sets OSC_DB_* env vars, honor them.
    if _has_explicit_env(prefix):
        return DBConfig(
            host=_get("HOST", _failover_host()),
            port=int(_get("PORT", "3306")),
            user=_get("USER", "python_user"),
            password=_get("PASSWORD", ""),
            database=_normalize_db_name(_get("NAME", "law_firm_data")),
            connection_timeout=int(_get("CONNECTION_TIMEOUT", "5")),
        )

    # Otherwise choose profile by policy: remote-first unless MAGI_PREFER_LOCAL_DB=1.
    prefer_local = str(os.environ.get("MAGI_PREFER_LOCAL_DB", "0")).strip().lower() in {"1", "true", "yes", "on"}
    cands = _profile_candidates(prefer_local=prefer_local)
    if cands:
        c0 = cands[0]
        return DBConfig(
            host=c0.host,
            port=c0.port,
            user=c0.user,
            password=c0.password,
            database=_normalize_db_name(c0.database),
            connection_timeout=c0.connection_timeout,
        )

    # Hard fallback — 透過 db_failover 動態取得 host.
    return DBConfig(
        host=_get("HOST", _failover_host()),
        port=int(_get("PORT", "3306")),
        user=_get("USER", "python_user"),
        password=_get("PASSWORD", ""),
        database=_normalize_db_name(_get("NAME", "law_firm_data")),
        connection_timeout=int(_get("CONNECTION_TIMEOUT", "5")),
    )


def connect_mysql(cfg: DBConfig) -> mysql.connector.MySQLConnection:
    def _connect(one: DBConfig) -> mysql.connector.MySQLConnection:
        conn = mysql.connector.connect(
            host=one.host,
            port=one.port,
            user=one.user,
            password=one.password,
            database=one.database,
            autocommit=False,
            charset="utf8mb4",
            collation="utf8mb4_unicode_ci",
            connection_timeout=max(1, int(one.connection_timeout or 5)),
        )
        # Expose selected endpoint for smoke/reporting.
        try:
            setattr(conn, "magi_selected_host", one.host)
            setattr(conn, "magi_selected_port", int(one.port))
            setattr(conn, "magi_selected_db", one.database)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 180, exc_info=True)
        return conn

    tried: set[tuple[str, int, str]] = set()
    last_err: Optional[Exception] = None

    # 1) primary cfg (usually remote-first)
    first_key = (cfg.host, int(cfg.port), cfg.database)
    tried.add(first_key)
    try:
        return _connect(cfg)
    except Exception as e:
        last_err = e

    # 2) profile-based candidates
    prefer_local = str(os.environ.get("MAGI_PREFER_LOCAL_DB", "0")).strip().lower() in {"1", "true", "yes", "on"}
    for c in _profile_candidates(prefer_local=prefer_local):
        key = (c.host, int(c.port), c.database)
        if key in tried:
            continue
        tried.add(key)
        try:
            return _connect(c)
        except Exception as e:
            last_err = e
            continue

    # 3) terminal hard local fallback
    for c in [
        DBConfig(host="127.0.0.1", port=3306, user=cfg.user, password=cfg.password, database=cfg.database, connection_timeout=3),
        DBConfig(host="127.0.0.1", port=3307, user=cfg.user, password=cfg.password, database=cfg.database, connection_timeout=3),
    ]:
        key = (c.host, int(c.port), c.database)
        if key in tried:
            continue
        tried.add(key)
        try:
            return _connect(c)
        except Exception as e:
            last_err = e
            continue

    if last_err:
        raise last_err
    raise RuntimeError("db_connect_failed_no_candidates")


def _is_create_denied_error(exc: Exception) -> bool:
    try:
        errno = int(getattr(exc, "errno", 0) or 0)
    except Exception:
        errno = 0
    msg = str(exc or "").lower()
    if errno in {1044, 1045, 1142}:
        return True
    return ("create command denied" in msg) or ("access denied" in msg and "create" in msg)


def _table_exists(conn: mysql.connector.MySQLConnection, table_name: str) -> bool:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = DATABASE()
              AND table_name = %s
            LIMIT 1
            """,
            (str(table_name or "").strip(),),
        )
        return bool(cur.fetchone())
    except Exception:
        return False
    finally:
        cur.close()


def ensure_osc_min_schema(conn: mysql.connector.MySQLConnection) -> Dict[str, Any]:
    """
    Ensure the minimal OSC tables needed for headless todo extraction/sync.
    This is safe to run repeatedly.
    """
    ddl = {
        "todo_keywords": """
        CREATE TABLE IF NOT EXISTS `todo_keywords` (
            `id` INT PRIMARY KEY AUTO_INCREMENT,
            `todo_type` TEXT NOT NULL,
            `pattern` TEXT NOT NULL,
            `pattern_type` VARCHAR(50) NOT NULL,
            `days` INT,
            `is_active` TINYINT(1) DEFAULT 1,
            `created_date` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY `unique_keyword` (`todo_type`(64), `pattern`(64), `pattern_type`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
        "case_todos": """
        CREATE TABLE IF NOT EXISTS `case_todos` (
            `id` INT PRIMARY KEY AUTO_INCREMENT,
            `case_number` VARCHAR(255) NOT NULL,
            `client_name` TEXT,
            `todo_type` TEXT NOT NULL,
            `todo_date` DATE,
            `todo_time` TIME,
            `description` TEXT,
            `status` VARCHAR(50) DEFAULT 'pending',
            `source_file` TEXT,
            `google_calendar_id` TEXT,
            `created_date` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            `completed_date` TIMESTAMP NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    }

    status: Dict[str, Any] = {
        "tables": {},
        "create_denied": False,
        "errors": [],
    }

    cur = conn.cursor()
    try:
        for table_name, q in ddl.items():
            try:
                cur.execute(q)
            except Exception as e:
                if _is_create_denied_error(e):
                    status["create_denied"] = True
                    status["errors"].append(f"{table_name}: create_denied")
                else:
                    raise
            status["tables"][table_name] = _table_exists(conn, table_name)
        try:
            conn.commit()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 315, exc_info=True)
    finally:
        cur.close()
    return status


def ensure_cases_schema(conn: mysql.connector.MySQLConnection) -> None:
    """
    Ensure `cases` table has the columns needed by downstream automations
    (transcript downloader, filing, etc.).

    Policy:
    - Never deletes data.
    - Only CREATE/ALTER to add missing columns / indexes.
    """
    cur = conn.cursor()
    try:
        # Base table (legacy init scripts may already create it).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS `cases` (
              `id` int(11) NOT NULL AUTO_INCREMENT,
              `case_number` varchar(50) NOT NULL,
              `client_name` varchar(255) DEFAULT '',
              `case_type` varchar(100) DEFAULT '',
              `case_reason` text DEFAULT NULL,
              `case_category` varchar(50) DEFAULT '',
              `legal_aid_number` varchar(100) DEFAULT '',
              `court_case_number` varchar(255) DEFAULT '',
              `notes` text DEFAULT NULL,
              `folder_path` text DEFAULT NULL,
              `created_date` timestamp NULL DEFAULT current_timestamp(),
              PRIMARY KEY (`id`),
              KEY `idx_case_number` (`case_number`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )

        cur.execute("SHOW COLUMNS FROM `cases`")
        cols = {str(r[0]).strip().lower() for r in (cur.fetchall() or [])}

        def _add(col_sql: str):
            try:
                cur.execute(col_sql)
            except Exception:
                # Best-effort; if it's already there or incompatible, just move on.
                pass

        if "status" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `status` VARCHAR(50) DEFAULT ''")
        if "court_name" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `court_name` VARCHAR(255) DEFAULT ''")
        if "updated_at" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `updated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
        # New split columns (V2): keep legacy columns for compatibility.
        if "laf_case_no" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `laf_case_no` VARCHAR(120) DEFAULT ''")
        if "application_no" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `application_no` VARCHAR(120) DEFAULT ''")
        if "court_case_no" not in cols:
            _add("ALTER TABLE `cases` ADD COLUMN `court_case_no` VARCHAR(255) DEFAULT ''")

        # Backfill new columns from legacy columns (idempotent; no deletes).
        try:
            cur.execute(
                """
                UPDATE `cases`
                   SET `application_no` = COALESCE(NULLIF(`legal_aid_number`, ''), `application_no`)
                 WHERE COALESCE(`application_no`, '') = ''
                   AND COALESCE(`legal_aid_number`, '') <> ''
                """
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 388, exc_info=True)
        try:
            cur.execute(
                """
                UPDATE `cases`
                   SET `laf_case_no` = COALESCE(NULLIF(`application_no`, ''), NULLIF(`legal_aid_number`, ''), `laf_case_no`)
                 WHERE COALESCE(`laf_case_no`, '') = ''
                   AND (COALESCE(`application_no`, '') <> '' OR COALESCE(`legal_aid_number`, '') <> '')
                """
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 399, exc_info=True)
        try:
            cur.execute(
                """
                UPDATE `cases`
                   SET `court_case_no` = COALESCE(NULLIF(`court_case_number`, ''), `court_case_no`)
                 WHERE COALESCE(`court_case_no`, '') = ''
                   AND COALESCE(`court_case_number`, '') <> ''
                """
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 410, exc_info=True)

        # Indexes used by lookups (best-effort).
        try:
            cur.execute("SHOW INDEX FROM `cases`")
            idx = {(str(r[2]).strip() or "").lower() for r in (cur.fetchall() or [])}
        except Exception:
            idx = set()

        if "idx_court_case_number" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_court_case_number` ON `cases` (`court_case_number`(100))")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 423, exc_info=True)
        if "idx_client_name" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_client_name` ON `cases` (`client_name`)")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 428, exc_info=True)
        if "idx_laf_case_no" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_laf_case_no` ON `cases` (`laf_case_no`(100))")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 433, exc_info=True)
        if "idx_application_no" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_application_no` ON `cases` (`application_no`(100))")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 438, exc_info=True)
        if "idx_court_case_no" not in idx:
            try:
                cur.execute("CREATE INDEX `idx_court_case_no` ON `cases` (`court_case_no`(100))")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 443, exc_info=True)

        conn.commit()
    finally:
        cur.close()


def upsert_case(
    conn: mysql.connector.MySQLConnection,
    *,
    case_number: str,
    client_name: str = "",
    case_type: str = "",
    case_category: str = "",
    case_reason: str = "",
    folder_path: str = "",
    court_name: str = "",
    court_case_number: str = "",
    court_case_no: str = "",
    laf_case_no: str = "",
    application_no: str = "",
    legal_aid_number: str = "",
    status: str = "進行中",
) -> Dict[str, int]:
    """
    Upsert a case row by `case_number` (safe for legacy schemas without UNIQUE constraint).
    Returns {"inserted": 0/1, "updated": 0/1}.
    """
    cn = (case_number or "").strip()
    if not cn:
        return {"inserted": 0, "updated": 0}

    court_case_number_v = (court_case_number or court_case_no or "").strip()
    court_case_no_v = (court_case_no or court_case_number or "").strip()
    application_no_v = (application_no or legal_aid_number or laf_case_no or "").strip()
    laf_case_no_v = (laf_case_no or application_no_v or "").strip()
    # Keep legacy legal_aid_number in sync for older modules.
    legal_aid_number_v = (legal_aid_number or application_no_v or laf_case_no_v or "").strip()

    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT `id` FROM `cases` WHERE `case_number`=%s ORDER BY `id` ASC LIMIT 1", (cn,))
        row = cur.fetchone()
        if row and row.get("id"):
            cur.execute(
                """
                UPDATE `cases`
                SET `client_name`=%s,
                    `case_type`=%s,
                    `case_category`=%s,
                    `case_reason`=%s,
                    `folder_path`=%s,
                    `court_name`=%s,
                    `court_case_number`=%s,
                    `court_case_no`=%s,
                    `laf_case_no`=%s,
                    `application_no`=%s,
                    `legal_aid_number`=%s,
                    `status`=%s
                WHERE `id`=%s
                """,
                (
                    (client_name or "").strip(),
                    (case_type or "").strip(),
                    (case_category or "").strip(),
                    (case_reason or "").strip(),
                    (folder_path or "").strip(),
                    (court_name or "").strip(),
                    court_case_number_v,
                    court_case_no_v,
                    laf_case_no_v,
                    application_no_v,
                    legal_aid_number_v,
                    (status or "").strip(),
                    row["id"],
                ),
            )
            conn.commit()
            return {"inserted": 0, "updated": 1}

        # Insert new
        cur.execute(
            """
            INSERT INTO `cases`
              (`case_number`,`client_name`,`case_type`,`case_category`,`case_reason`,`folder_path`,
               `court_name`,`court_case_number`,`court_case_no`,`laf_case_no`,`application_no`,`legal_aid_number`,`status`)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                cn,
                (client_name or "").strip(),
                (case_type or "").strip(),
                (case_category or "").strip(),
                (case_reason or "").strip(),
                (folder_path or "").strip(),
                (court_name or "").strip(),
                court_case_number_v,
                court_case_no_v,
                laf_case_no_v,
                application_no_v,
                legal_aid_number_v,
                (status or "").strip(),
            ),
        )
        conn.commit()
        return {"inserted": 1, "updated": 0}
    finally:
        cur.close()

def fetch_active_todo_patterns(conn: mysql.connector.MySQLConnection) -> List[Tuple[str, str, str, Optional[int]]]:
    cur = conn.cursor()
    try:
        try:
            cur.execute(
                """
                SELECT `todo_type`, `pattern`, `pattern_type`, `days`
                FROM `todo_keywords`
                WHERE `is_active` = 1
                ORDER BY `todo_type`, `id`
                """
            )
            rows = cur.fetchall()
            return [(r[0], r[1], r[2], r[3]) for r in rows]
        except Exception as e:
            # Restricted DB user may not create/read table in some environments.
            try:
                errno = int(getattr(e, "errno", 0) or 0)
            except Exception:
                errno = 0
            msg = str(e or "").lower()
            if errno in {1142, 1146} or "doesn't exist" in msg or "denied" in msg:
                return []
            raise
    finally:
        cur.close()


def seed_default_todo_keywords(conn: mysql.connector.MySQLConnection) -> int:
    """
    Seed a small default set, using INSERT IGNORE to avoid duplicates.
    Returns number of inserted rows (best-effort).
    """
    defaults = [
        ("補正", r"應於本裁定送達後(\d+)日內補正", "relative", None),
        ("補正", r"請於文到(\d+)日內補正", "relative", None),
        ("補正", r"文到(\d+)日內.*?補正", "relative", None),
        ("補正", r"(\d+)日內補正", "relative", None),
        ("陳述意見", r"文到(\d+)日內陳述意見", "relative", None),
        ("陳述意見", r"(\d+)日內陳述意見", "relative", None),
        ("開庭", r"(\d{1,2})月(\d{1,2})日([上下])午(\d{1,2})時(\d*)分?.*?(開庭|準備程序)", "absolute_time", None),
        ("繳費", r"文到(\d+)日內繳納", "relative", None),
        ("閱卷", r"文到(\d+)日內.*?閱卷", "relative", None),
    ]

    cur = conn.cursor()
    try:
        try:
            cur.executemany(
                """
                INSERT IGNORE INTO `todo_keywords`
                  (`todo_type`, `pattern`, `pattern_type`, `days`, `is_active`)
                VALUES (%s, %s, %s, %s, 1)
                """,
                defaults,
            )
            conn.commit()
            return int(getattr(cur, "rowcount", 0) or 0)
        except Exception as e:
            try:
                errno = int(getattr(e, "errno", 0) or 0)
            except Exception:
                errno = 0
            msg = str(e or "").lower()
            if errno in {1142, 1146} or "denied" in msg or "doesn't exist" in msg:
                return 0
            raise
    finally:
        cur.close()


def insert_case_todos(
    conn: mysql.connector.MySQLConnection,
    *,
    case_number: str,
    client_name: str = "",
    todos: List[Dict],
    source_file: str,
    allow_duplicates: bool = False,
    commit: bool = True,
) -> Dict:
    """
    Insert todos into case_todos with a conservative de-dupe check (no deletes).
    """
    cur = conn.cursor()
    inserted = 0
    skipped = 0
    try:
        for t in todos:
            todo_type = (t.get("type") or "").strip() or "待辦"
            todo_date = (t.get("date") or "").strip() or None
            todo_time = (t.get("time") or "").strip() or None
            desc = (t.get("description") or "").strip() or ""

            if not allow_duplicates:
                cur.execute(
                    """
                    SELECT `id` FROM `case_todos`
                    WHERE `case_number`=%s
                      AND ( (`todo_date`=%s) OR (%s IS NULL AND `todo_date` IS NULL) )
                      AND ( (`todo_time`=%s) OR (%s IS NULL AND `todo_time` IS NULL) )
                      AND `todo_type`=%s
                      AND `source_file`=%s
                      AND `description`=%s
                    LIMIT 1
                    """,
                    (case_number, todo_date, todo_date, todo_time, todo_time, todo_type, source_file, desc),
                )
                if cur.fetchone():
                    skipped += 1
                    continue

            cur.execute(
                """
                INSERT INTO `case_todos`
                  (`case_number`, `client_name`, `todo_type`, `todo_date`, `todo_time`, `description`, `source_file`, `status`)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'pending')
                """,
                (case_number, client_name or "", todo_type, todo_date, todo_time, desc, source_file),
            )
            inserted += 1
        if commit:
            conn.commit()
        return {"inserted": inserted, "skipped": skipped}
    finally:
        cur.close()


def list_unsynced_todos_with_case_info(
    conn: mysql.connector.MySQLConnection,
    *,
    limit: int = 50,
) -> List[Dict]:
    """
    List pending todos that haven't been synced to Google Calendar yet.
    Never deletes; read-only query.
    """
    lim = max(1, min(int(limit), 400))
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT
                ct.id,
                ct.case_number,
                ct.client_name,
                ct.todo_type,
                ct.todo_date,
                ct.todo_time,
                ct.description,
                ct.source_file,
                COALESCE(c.court_name, '') AS court_name,
                COALESCE(NULLIF(c.court_case_no, ''), c.court_case_number, '') AS court_case_number
            FROM case_todos ct
            LEFT JOIN cases c
              ON c.case_number COLLATE utf8mb4_unicode_ci
               = ct.case_number COLLATE utf8mb4_unicode_ci
            WHERE (ct.google_calendar_id IS NULL OR ct.google_calendar_id = '')
              AND ct.todo_date IS NOT NULL
              AND (ct.status IS NULL OR ct.status = '' OR ct.status = 'pending')
            ORDER BY ct.todo_date ASC, ct.id ASC
            LIMIT %s
            """,
            (lim,),
        )
        rows = cur.fetchall() or []
        return [dict(r) for r in rows if isinstance(r, dict)]
    finally:
        cur.close()


def set_todo_google_calendar_id(
    conn: mysql.connector.MySQLConnection,
    *,
    todo_id: int,
    google_calendar_id: str,
) -> Dict[str, int]:
    """
    Best-effort update for google_calendar_id. Never deletes.
    """
    gid = (google_calendar_id or "").strip()
    if not gid:
        return {"updated": 0}
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE case_todos
            SET google_calendar_id=%s
            WHERE id=%s AND (google_calendar_id IS NULL OR google_calendar_id = '')
            """,
            (gid, int(todo_id)),
        )
        conn.commit()
        return {"updated": int(getattr(cur, "rowcount", 0) or 0)}
    finally:
        cur.close()
