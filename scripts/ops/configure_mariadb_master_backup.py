#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prepare the current MAGI MariaDB as the primary DB for a remote backup replica.

This is the direction used by the private deployment:
    local MAGI MariaDB (master) -> remote MariaDB backup (replica)

The script does not configure the remote host directly. It prepares the local
master, creates/rotates the local replication account, and prints redacted
instructions for the remote replica.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pymysql

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT / ".runtime"
CREDS_PATH = RUNTIME_DIR / "db_replication_credentials.local.json"
STATUS_PATH = RUNTIME_DIR / "db_master_backup_latest.json"
MY_CNF = Path("/opt/homebrew/etc/my.cnf.d/magi.cnf")

DEFAULT_ALLOWED_HOSTS = [
    "100.116.54.16",
    "whale.tail6738b7.ts.net",
    "whale.tail6738b7.ts.net.",
    "100.111.10.126",
    "whale-1.tail6738b7.ts.net",
    "whale-1.tail6738b7.ts.net.",
]


def _random_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits + "_-+=."
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _socket_conn(user: str):
    return pymysql.connect(
        unix_socket="/tmp/mysql.sock",
        user=user,
        database="mysql",
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5,
        read_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _query_status(admin_user: str) -> Dict[str, Any]:
    conn = _socket_conn(admin_user)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT @@hostname AS hostname, @@port AS port, @@server_id AS server_id, "
                "@@log_bin AS log_bin, @@binlog_format AS binlog_format, "
                "@@expire_logs_days AS expire_logs_days, @@version AS version"
            )
            status = cur.fetchone() or {}
            cur.execute("SHOW MASTER STATUS")
            status["master_status"] = cur.fetchone() or {}
            cur.execute("SELECT User,Host FROM mysql.user WHERE User='repl' ORDER BY Host")
            status["repl_users"] = cur.fetchall() or []
        return status
    finally:
        conn.close()


def _load_or_create_credentials(master_host: str, master_dns: str, allowed_hosts: List[str]) -> Dict[str, Any]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if CREDS_PATH.exists():
        data = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
        data.setdefault("allowed_backup_hosts", allowed_hosts)
        data["master_host"] = master_host
        data["master_dns"] = master_dns
        data["master_port"] = 3306
    else:
        data = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "purpose": "Local MAGI master -> remote backup MariaDB replica",
            "master_host": master_host,
            "master_dns": master_dns,
            "master_port": 3306,
            "repl_user": "repl",
            "repl_password": _random_password(),
            "allowed_backup_hosts": allowed_hosts,
        }
    CREDS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(CREDS_PATH, 0o600)
    return data


def _write_master_config(server_id: int) -> Dict[str, Any]:
    original = MY_CNF.read_text(encoding="utf-8", errors="ignore") if MY_CNF.exists() else "[mysqld]\n"
    if "[mysqld]" not in original:
        original = original.rstrip() + "\n\n[mysqld]\n"
    backup = MY_CNF.with_name(MY_CNF.name + "." + datetime.now().strftime("%Y%m%d_%H%M%S") + ".bak")
    backup.write_text(original, encoding="utf-8")
    block = "\n".join(
        [
            "# BEGIN MAGI replica compatibility",
            "# 由 MAGI 管理：讓本機 MariaDB 作為主 DB，供遠端備份庫 replication。",
            "# 這些設定需重新啟動 MariaDB 後才會生效。",
            f"server-id={int(server_id)}",
            "log-bin=magi-bin",
            "max-binlog-size=256M",
            "expire-logs-days=7",
            "binlog-format=ROW",
            "# END MAGI replica compatibility",
            "",
        ]
    )
    begin = "# BEGIN MAGI replica compatibility"
    end = "# END MAGI replica compatibility"
    if begin in original and end in original:
        before, rest = original.split(begin, 1)
        _, after = rest.split(end, 1)
        updated = before.rstrip() + "\n" + block + after.lstrip()
    else:
        updated = original.rstrip() + "\n\n" + block
    MY_CNF.write_text(updated, encoding="utf-8")
    return {"config": str(MY_CNF), "backup": str(backup)}


def _restart_mariadb() -> Dict[str, Any]:
    proc = subprocess.run(["brew", "services", "restart", "mariadb"], text=True, capture_output=True, timeout=90)
    return {"returncode": proc.returncode, "stdout": proc.stdout[-1000:], "stderr": proc.stderr[-1000:]}


def _create_repl_users(admin_user: str, password: str, allowed_hosts: List[str]) -> List[str]:
    conn = _socket_conn(admin_user)
    try:
        applied: List[str] = []
        with conn.cursor() as cur:
            for host in allowed_hosts:
                cur.execute(f"CREATE USER IF NOT EXISTS `repl`@%s IDENTIFIED BY %s", (host, password))
                cur.execute(f"ALTER USER `repl`@%s IDENTIFIED BY %s", (host, password))
                cur.execute(f"GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO `repl`@%s", (host,))
                applied.append(host)
            cur.execute("FLUSH PRIVILEGES")
        return applied
    finally:
        conn.close()


def _remote_instruction(master_host: str, master_port: int, master_file: str, master_pos: Any) -> str:
    return (
        "STOP SLAVE; RESET SLAVE ALL; "
        "CHANGE MASTER TO "
        f"MASTER_HOST='{master_host}', MASTER_PORT={int(master_port)}, "
        "MASTER_USER='repl', MASTER_PASSWORD='***', "
        f"MASTER_LOG_FILE='{master_file}', MASTER_LOG_POS={int(master_pos)}; "
        "START SLAVE;"
    )


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="設定本機 MAGI MariaDB 作為遠端備份庫的 master")
    parser.add_argument("--check-only", action="store_true", help="只檢查，不變更")
    parser.add_argument("--apply", action="store_true", help="寫設定並建立 repl 使用者")
    parser.add_argument("--restart-mariadb", action="store_true", help="套用 my.cnf 後重啟 MariaDB")
    parser.add_argument("--admin-user", default="ai", help="本機 socket 管理帳號")
    parser.add_argument("--server-id", type=int, default=2)
    parser.add_argument("--master-host", default="100.97.29.92")
    parser.add_argument("--master-dns", default="aimac-mini.tail6738b7.ts.net")
    parser.add_argument("--allowed-host", action="append", default=[])
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    allowed_hosts = args.allowed_host or DEFAULT_ALLOWED_HOSTS
    payload: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "check-only",
        "master_host": args.master_host,
        "master_dns": args.master_dns,
        "allowed_backup_hosts": allowed_hosts,
        "actions": [],
        "errors": [],
        "warnings": [],
    }
    try:
        payload["status_before"] = _query_status(args.admin_user)
    except Exception as exc:
        payload["errors"].append(f"本機 master 狀態檢查失敗：{type(exc).__name__}: {exc}")

    if args.apply:
        creds = _load_or_create_credentials(args.master_host, args.master_dns, allowed_hosts)
        try:
            payload["actions"].append({"write_master_config": _write_master_config(args.server_id)})
        except Exception as exc:
            payload["errors"].append(f"寫入 master 設定失敗：{type(exc).__name__}: {exc}")
        if args.restart_mariadb:
            payload["actions"].append({"restart_mariadb": _restart_mariadb()})
        try:
            applied = _create_repl_users(args.admin_user, creds["repl_password"], allowed_hosts)
            payload["actions"].append({"create_repl_users": applied, "credential_file": str(CREDS_PATH)})
        except Exception as exc:
            payload["errors"].append(f"建立 repl 使用者失敗：{type(exc).__name__}: {exc}")

    try:
        payload["status_after"] = _query_status(args.admin_user)
        ms = payload["status_after"].get("master_status") or {}
        if not payload["status_after"].get("log_bin"):
            payload["warnings"].append("log_bin 尚未啟用，遠端 replica 無法追本機；請套用設定並重啟 MariaDB。")
        if ms.get("File") and ms.get("Position"):
            payload["remote_change_master_dry_run"] = _remote_instruction(
                args.master_host, 3306, str(ms["File"]), ms["Position"]
            )
    except Exception as exc:
        payload["errors"].append(f"本機 master 套用後檢查失敗：{type(exc).__name__}: {exc}")

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 1 if payload["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
