#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Configure MAGI local MariaDB as a replica of a remote MariaDB master.

The script is intentionally conservative:
- --check-only is the default and never changes data.
- --apply requires a replication password and an explicit acknowledgement.
- A local mysqldump backup is created before CHANGE MASTER unless --skip-backup
  is explicitly passed.
- Passwords are never printed or written to the status JSON.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pymysql

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT / ".runtime"
STATUS_PATH = RUNTIME_DIR / "db_replica_setup_latest.json"
DEFAULT_BACKUP_DIR = ROOT / "_db_backups" / "replica_cutover"
DEFAULT_MY_CNF = Path("/opt/homebrew/etc/my.cnf.d/magi.cnf")
CONFIG_BEGIN = "# BEGIN MAGI replica compatibility"
CONFIG_END = "# END MAGI replica compatibility"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def _redact(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    return "***"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class DBProfile:
    host: str
    port: int
    user: str
    password: str
    database: str
    connect_timeout: int = 5

    def safe_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["password"] = _redact(data.get("password"))
        return data


@dataclass
class ReplicaTarget:
    host: str
    port: int
    user: str
    password: str
    server_id: int
    use_gtid: bool
    master_log_file: str = ""
    master_log_pos: Optional[int] = None

    def safe_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["password"] = _redact(data.get("password"))
        return data


def _connect(profile: DBProfile, *, dict_cursor: bool = False):
    cursorclass = pymysql.cursors.DictCursor if dict_cursor else pymysql.cursors.Cursor
    return pymysql.connect(
        host=profile.host,
        port=int(profile.port),
        user=profile.user,
        password=profile.password,
        database=profile.database or None,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=max(2, int(profile.connect_timeout or 5)),
        read_timeout=max(5, int(profile.connect_timeout or 5)),
        cursorclass=cursorclass,
    )


def _query_one(profile: DBProfile, sql: str) -> Tuple[Any, ...]:
    conn = _connect(profile)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            return tuple(row or ())
    finally:
        conn.close()


def _local_status(profile: DBProfile) -> Dict[str, Any]:
    row = _query_one(
        profile,
        (
            "SELECT @@hostname, @@port, @@server_id, @@version, "
            "@@log_bin, @@binlog_format, @@read_only"
        ),
    )
    keys = ("hostname", "port", "server_id", "version", "log_bin", "binlog_format", "read_only")
    out = dict(zip(keys, row))
    out["database"] = profile.database
    return out


def _tcp_check(host: str, port: int, timeout: int = 5) -> Dict[str, Any]:
    t0 = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"ok": True, "latency_ms": round((time.time() - t0) * 1000, 1)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _find_binary(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"找不到執行檔：{name}")
    return found


def _backup_database(profile: DBProfile, out_dir: Path) -> Path:
    mysqldump = _find_binary("mysqldump")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{profile.database}_pre_replica_{stamp}.sql.gz"
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    cmd = [
        mysqldump,
        f"--host={profile.host}",
        f"--port={int(profile.port)}",
        f"--user={profile.user}",
        "--default-character-set=utf8mb4",
        "--single-transaction",
        "--quick",
        "--skip-lock-tables",
        "--routines",
        "--events",
        "--triggers",
        profile.database,
    ]
    env = os.environ.copy()
    env["MYSQL_PWD"] = profile.password
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        assert proc.stdout is not None
        with gzip.open(tmp_path, "wb", compresslevel=6) as gz:
            shutil.copyfileobj(proc.stdout, gz, length=1024 * 1024)
        stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
        rc = proc.wait(timeout=300)
        if rc != 0:
            raise RuntimeError(f"mysqldump 失敗 rc={rc}: {stderr[:600]}")
        tmp_path.replace(out_path)
        return out_path
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _managed_config_block(server_id: int, *, relay_log_prefix: str) -> str:
    return "\n".join(
        [
            CONFIG_BEGIN,
            "# 由 MAGI 管理：讓本機 MariaDB 可作為遠端主庫的 replica。",
            "# 這些設定需重新啟動 MariaDB 後才會生效。",
            f"server-id={int(server_id)}",
            f"relay-log={relay_log_prefix}",
            f"relay-log-index={relay_log_prefix}.index",
            "binlog-format=ROW",
            CONFIG_END,
            "",
        ]
    )


def _write_local_config(path: Path, server_id: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else "[mysqld]\n"
    if "[mysqld]" not in original:
        original = original.rstrip() + "\n\n[mysqld]\n"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(path.name + f".bak.{stamp}")
    backup.write_text(original, encoding="utf-8")

    block = _managed_config_block(server_id, relay_log_prefix="magi-relay-bin")
    if CONFIG_BEGIN in original and CONFIG_END in original:
        before, rest = original.split(CONFIG_BEGIN, 1)
        _, after = rest.split(CONFIG_END, 1)
        updated = before.rstrip() + "\n" + block + after.lstrip()
    else:
        updated = original.rstrip() + "\n\n" + block
    path.write_text(updated, encoding="utf-8")
    return backup


def _build_change_master(target: ReplicaTarget) -> Tuple[str, List[Any], str]:
    base = [
        "MASTER_HOST=%s",
        "MASTER_PORT=%s",
        "MASTER_USER=%s",
        "MASTER_PASSWORD=%s",
    ]
    params: List[Any] = [target.host, int(target.port), target.user, target.password]
    if target.use_gtid:
        base.append("MASTER_USE_GTID=slave_pos")
    else:
        if not target.master_log_file or target.master_log_pos is None:
            raise ValueError("未使用 GTID 時，必須提供 --master-log-file 與 --master-log-pos")
        base.extend(["MASTER_LOG_FILE=%s", "MASTER_LOG_POS=%s"])
        params.extend([target.master_log_file, int(target.master_log_pos)])
    sql = "CHANGE MASTER TO " + ", ".join(base)
    display_parts = [
        f"MASTER_HOST='{target.host}'",
        f"MASTER_PORT={int(target.port)}",
        f"MASTER_USER='{target.user}'",
        "MASTER_PASSWORD='***'",
    ]
    if target.use_gtid:
        display_parts.append("MASTER_USE_GTID=slave_pos")
    else:
        display_parts.append(f"MASTER_LOG_FILE='{target.master_log_file}'")
        display_parts.append(f"MASTER_LOG_POS={int(target.master_log_pos or 0)}")
    return sql, params, "CHANGE MASTER TO " + ", ".join(display_parts)


def _apply_replication(admin: DBProfile, target: ReplicaTarget) -> Dict[str, Any]:
    sql, params, display_sql = _build_change_master(target)
    conn = _connect(admin, dict_cursor=True)
    try:
        with conn.cursor() as cur:
            cur.execute("STOP SLAVE")
            cur.execute("RESET SLAVE ALL")
            cur.execute(sql, params)
            cur.execute("START SLAVE")
            cur.execute("SHOW SLAVE STATUS")
            status = cur.fetchone() or {}
        return {"change_master": display_sql, "slave_status": _safe_slave_status(status)}
    finally:
        conn.close()


def _safe_slave_status(status: Dict[str, Any]) -> Dict[str, Any]:
    keep = [
        "Slave_IO_State",
        "Master_Host",
        "Master_User",
        "Master_Port",
        "Connect_Retry",
        "Slave_IO_Running",
        "Slave_SQL_Running",
        "Last_IO_Errno",
        "Last_IO_Error",
        "Last_SQL_Errno",
        "Last_SQL_Error",
        "Seconds_Behind_Master",
        "Using_Gtid",
        "Gtid_IO_Pos",
    ]
    return {k: status.get(k) for k in keep if k in status}


def _restart_mariadb() -> Dict[str, Any]:
    cmds = [
        ["brew", "services", "restart", "mariadb"],
    ]
    last: Dict[str, Any] = {}
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
            last = {
                "cmd": " ".join(cmd),
                "returncode": proc.returncode,
                "stdout": proc.stdout[-1000:],
                "stderr": proc.stderr[-1000:],
            }
            if proc.returncode == 0:
                return last
        except Exception as exc:
            last = {"cmd": " ".join(cmd), "error": f"{type(exc).__name__}: {exc}"}
    return last


def _write_status(payload: Dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_profiles(args: argparse.Namespace) -> Tuple[DBProfile, DBProfile, ReplicaTarget]:
    _load_env_file(ROOT / ".env")
    local = DBProfile(
        host=args.local_host or _env("MAGI_DB_REPLICA_LOCAL_HOST", "MAGI_LOCAL_DB_HOST", "OSC_DB_HOST", "DB_HOST", default="127.0.0.1"),
        port=int(args.local_port or _env("MAGI_DB_REPLICA_LOCAL_PORT", "MAGI_LOCAL_DB_PORT", "OSC_DB_PORT", "DB_PORT", default="3307")),
        user=args.local_user or _env("MAGI_LOCAL_DB_USER", "OSC_DB_USER", "DB_USER", default=""),
        password=args.local_password if args.local_password is not None else _env("MAGI_LOCAL_DB_PASSWORD", "OSC_DB_PASSWORD", "DB_PASSWORD", default=""),
        database=args.database or _env("MAGI_LOCAL_DB_NAME", "OSC_DB_NAME", "DB_NAME", default="law_firm_data"),
        connect_timeout=int(args.timeout),
    )
    admin = DBProfile(
        host=local.host,
        port=local.port,
        user=args.admin_user or _env("MAGI_DB_REPLICA_LOCAL_ADMIN_USER", default=local.user),
        password=args.admin_password if args.admin_password is not None else _env("MAGI_DB_REPLICA_LOCAL_ADMIN_PASSWORD", default=local.password),
        database=local.database,
        connect_timeout=int(args.timeout),
    )
    repl_password = args.repl_password if args.repl_password is not None else _env("MAGI_DB_REPLICA_PASSWORD", "MAGI_REPL_PASSWORD", default="")
    target = ReplicaTarget(
        host=args.remote_host or _env("MAGI_DB_REPLICA_REMOTE_HOST", default="100.97.29.92"),
        port=int(args.remote_port or _env("MAGI_DB_REPLICA_REMOTE_PORT", default="3306")),
        user=args.repl_user or _env("MAGI_DB_REPLICA_USER", default="repl"),
        password=repl_password,
        server_id=int(args.server_id or _env("MAGI_DB_REPLICA_LOCAL_SERVER_ID", default="2")),
        use_gtid=not args.no_gtid,
        master_log_file=args.master_log_file or "",
        master_log_pos=args.master_log_pos,
    )
    return local, admin, target


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="設定 MAGI 本機 MariaDB 成為遠端 MariaDB replica")
    parser.add_argument("--check-only", action="store_true", help="只檢查，不變更；預設行為")
    parser.add_argument("--apply", action="store_true", help="正式套用 STOP/RESET/CHANGE MASTER/START SLAVE")
    parser.add_argument("--yes-i-understand", action="store_true", help="確認已備份且了解會重設本機 replication 狀態")
    parser.add_argument("--skip-backup", action="store_true", help="正式套用前略過本機 mysqldump 備份")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help="本機切換前備份輸出資料夾")
    parser.add_argument("--write-local-config", action="store_true", help="寫入 /opt/homebrew/etc/my.cnf.d/magi.cnf 的 server-id/relay-log 設定")
    parser.add_argument("--restart-mariadb", action="store_true", help="寫入設定後重啟 Homebrew MariaDB")
    parser.add_argument("--config-path", default=str(DEFAULT_MY_CNF), help="MariaDB my.cnf include 檔案")
    parser.add_argument("--local-host")
    parser.add_argument("--local-port", type=int)
    parser.add_argument("--local-user")
    parser.add_argument("--local-password")
    parser.add_argument("--admin-user")
    parser.add_argument("--admin-password")
    parser.add_argument("--database")
    parser.add_argument("--remote-host")
    parser.add_argument("--remote-port", type=int)
    parser.add_argument("--repl-user")
    parser.add_argument("--repl-password")
    parser.add_argument("--server-id", type=int)
    parser.add_argument("--no-gtid", action="store_true", help="不用 GTID；需提供 --master-log-file/--master-log-pos")
    parser.add_argument("--master-log-file")
    parser.add_argument("--master-log-pos", type=int)
    parser.add_argument("--timeout", type=int, default=5)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    local, admin, target = _make_profiles(args)
    payload: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "check-only",
        "local": local.safe_dict(),
        "admin": admin.safe_dict(),
        "remote_master": target.safe_dict(),
        "checks": {},
        "actions": [],
        "warnings": [],
        "errors": [],
    }

    try:
        local_status = _local_status(local)
        payload["checks"]["local_status"] = local_status
        if int(local_status.get("server_id") or 0) != int(target.server_id):
            payload["warnings"].append(
                f"本機 MariaDB 目前 server_id={local_status.get('server_id')}，預期為 {target.server_id}；需寫入設定並重啟後才會生效。"
            )
        if int(local_status.get("log_bin") or 0) == 0:
            payload["warnings"].append("本機 log_bin 未啟用；作為單純 replica 可接受，若要再轉給第三台才需要啟用。")
        if str(local_status.get("binlog_format") or "").upper() != "ROW":
            payload["warnings"].append("本機 binlog_format 不是 ROW；若未啟用 log_bin 只是提示，不阻斷。")
    except Exception as exc:
        payload["errors"].append(f"本機 DB 檢查失敗：{type(exc).__name__}: {exc}")

    payload["checks"]["remote_tcp"] = _tcp_check(target.host, target.port, timeout=int(args.timeout))
    if not payload["checks"]["remote_tcp"].get("ok"):
        payload["errors"].append(f"遠端主庫 {target.host}:{target.port} 無法連線")

    if args.write_local_config:
        try:
            backup = _write_local_config(Path(args.config_path), target.server_id)
            payload["actions"].append({"write_local_config": str(args.config_path), "backup": str(backup)})
        except Exception as exc:
            payload["errors"].append(f"寫入 MariaDB 設定失敗：{type(exc).__name__}: {exc}")

    if args.restart_mariadb:
        if not args.write_local_config:
            payload["warnings"].append("--restart-mariadb 通常應搭配 --write-local-config")
        payload["actions"].append({"restart_mariadb": _restart_mariadb()})
        try:
            payload["checks"]["local_status_after_restart"] = _local_status(local)
        except Exception as exc:
            payload["errors"].append(f"重啟後本機 DB 檢查失敗：{type(exc).__name__}: {exc}")

    if args.apply:
        if not args.yes_i_understand:
            payload["errors"].append("正式套用需加 --yes-i-understand")
        if not target.password:
            payload["errors"].append("缺少 replication 密碼：請用 --repl-password 或 MAGI_DB_REPLICA_PASSWORD 提供")
        if payload["errors"]:
            _write_status(payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
        if not args.skip_backup:
            try:
                backup_path = _backup_database(local, Path(args.backup_dir))
                payload["actions"].append({"pre_replica_backup": str(backup_path)})
            except Exception as exc:
                payload["errors"].append(f"正式套用前備份失敗：{type(exc).__name__}: {exc}")
                _write_status(payload)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 3
        try:
            payload["actions"].append({"replication": _apply_replication(admin, target)})
        except Exception as exc:
            payload["errors"].append(f"套用 replication 失敗：{type(exc).__name__}: {exc}")
            _write_status(payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 4
    else:
        sql_display = ""
        try:
            _, _, sql_display = _build_change_master(target)
        except Exception as exc:
            payload["warnings"].append(f"CHANGE MASTER dry-run 無法完整產生：{type(exc).__name__}: {exc}")
        if sql_display:
            payload["checks"]["change_master_dry_run"] = sql_display

    _write_status(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
