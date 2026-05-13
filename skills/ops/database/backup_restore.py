#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Daily DB backup/restore utility for MAGI.

Targets:
- remote keeper DB (Studio_VPN_Remote)
- local DB (Studio_Local/Home_Local_Test)

Design:
- backup only uses mysqldump + gzip
- restore requires explicit --yes-i-understand
- never deletes DB rows directly; retention only deletes old backup files
"""

from __future__ import annotations
import logging

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pymysql

_MAGI_ROOT = Path(__file__).resolve().parents[3]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import config_candidates, get_magi_root_dir


def _remote_db_ip_or(fallback: str) -> str:
    try:
        from api.routing.node_registry import get_node_ip
        return get_node_ip("nas") or fallback
    except Exception:
        return fallback

CONFIG_CANDIDATES = [str(p) for p in config_candidates("config.json")]

DEFAULT_BACKUP_DIR = str(get_magi_root_dir() / "_db_backups" / "law_firm_data")
_DOTENV_LOADED = False


def _ensure_dotenv_loaded() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch dotenv load", exc_info=True)


@dataclass
class DBProfile:
    name: str
    host: str
    port: int
    user: str
    password: str
    database: str
    connection_timeout: int = 5


def _now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _q(v: Any, default: str = "") -> str:
    return str(v if v is not None else default).strip()


def _load_profiles() -> Dict[str, DBProfile]:
    _ensure_dotenv_loaded()
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
        name = _q(row.get("profile_name"))
        c = row.get("config") if isinstance(row.get("config"), dict) else {}
        if not name:
            continue
        try:
            out[name] = DBProfile(
                name=name,
                host=_q(c.get("host"), "127.0.0.1"),
                port=int(c.get("port") or 3306),
                user=_q(c.get("user"), "python_user"),
                password=_q(c.get("password"), ""),
                database=_q(c.get("database"), "law_firm_data"),
                connection_timeout=int(c.get("connection_timeout") or 5),
            )
        except Exception:
            continue
    return out


def _choose_remote_profile(profiles: Dict[str, DBProfile]) -> DBProfile:
    _ensure_dotenv_loaded()
    p = profiles.get("Studio_VPN_Remote")
    if p:
        return p
    return DBProfile(
        name="Studio_VPN_Remote",
        host=_q(os.environ.get("MAGI_REMOTE_DB_HOST"), _remote_db_ip_or("")),
        port=int(os.environ.get("MAGI_REMOTE_DB_PORT") or 3306),
        user=_q(os.environ.get("MAGI_REMOTE_DB_USER"), "casper_service"),
        password=_q(os.environ.get("MAGI_REMOTE_DB_PASSWORD"), ""),
        database=_q(os.environ.get("MAGI_REMOTE_DB_NAME"), "law_firm_data"),
        connection_timeout=int(os.environ.get("MAGI_REMOTE_DB_TIMEOUT") or 5),
    )


def _choose_local_profile(profiles: Dict[str, DBProfile]) -> DBProfile:
    if not profiles:
        _ensure_dotenv_loaded()
    for name in ("Studio_Local", "Home_Local_Test"):
        p = profiles.get(name)
        if not p:
            continue
        if _ping_db(p):
            return p
    remote = profiles.get("Studio_VPN_Remote") or _choose_remote_profile(profiles)
    if (
        os.environ.get("MAGI_LOCAL_DB_ALLOW_LOOPBACK_REMOTE_FALLBACK", "1").strip().lower()
        in {"1", "true", "yes", "on"}
        and remote
        and remote.host.strip().lower() in {"127.0.0.1", "localhost", "::1"}
        and _ping_db(remote)
    ):
        return DBProfile(
            name=f"{remote.name}_as_local",
            host=remote.host,
            port=remote.port,
            user=remote.user,
            password=remote.password,
            database=remote.database,
            connection_timeout=remote.connection_timeout,
        )
    return DBProfile(
        name="Studio_Local",
        host=_q(os.environ.get("MAGI_LOCAL_DB_HOST"), "127.0.0.1"),
        port=int(os.environ.get("MAGI_LOCAL_DB_PORT") or 3306),
        user=_q(os.environ.get("MAGI_LOCAL_DB_USER"), "python_user"),
        password=_q(os.environ.get("MAGI_LOCAL_DB_PASSWORD"), ""),
        database=_q(os.environ.get("MAGI_LOCAL_DB_NAME"), "law_firm_data"),
        connection_timeout=int(os.environ.get("MAGI_LOCAL_DB_TIMEOUT") or 5),
    )


def _ping_db(profile: DBProfile) -> bool:
    try:
        conn = pymysql.connect(
            host=profile.host,
            port=int(profile.port),
            user=profile.user,
            password=profile.password,
            database=profile.database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=max(2, int(profile.connection_timeout or 5)),
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
        return True
    except Exception:
        return False


def _find_bin(name: str) -> str:
    b = shutil.which(name)
    if b:
        return b
    raise FileNotFoundError(f"missing binary: {name}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _backup_one(profile: DBProfile, target: str, out_dir: Path) -> Dict[str, Any]:
    mysqldump = _find_bin("mysqldump")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _now()
    out_name = f"{profile.database}_{target}_{ts}.sql.gz"
    out_path = out_dir / out_name
    tmp_path = out_dir / (out_name + ".tmp")

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

    t0 = time.time()
    max_duration = 300  # 5 minutes total deadline for backup
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    stderr_raw = b""
    bytes_written = 0
    try:
        assert proc.stdout is not None
        with gzip.open(tmp_path, "wb", compresslevel=6) as gz:
            while True:
                if time.time() - t0 > max_duration:
                    proc.kill()
                    raise TimeoutError(f"mysqldump exceeded {max_duration}s deadline")
                chunk = proc.stdout.read(1024 * 1024)
                if not chunk:
                    break
                gz.write(chunk)
                bytes_written += len(chunk)
        stderr_raw = (proc.stderr.read() if proc.stderr is not None else b"")
        remaining = max(1, max_duration - int(time.time() - t0))
        rc = proc.wait(timeout=remaining)
    except Exception:
        proc.kill()
        raise

    if rc != 0:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 227, exc_info=True)
        raise RuntimeError(f"mysqldump failed({rc}): {(stderr_raw or b'').decode(errors='ignore')[-500:]}")

    tmp_path.replace(out_path)
    sha = _sha256(out_path)
    elapsed = round(time.time() - t0, 3)

    meta = {
        "ok": True,
        "target": target,
        "profile": profile.name,
        "host": profile.host,
        "port": int(profile.port),
        "database": profile.database,
        "path": str(out_path),
        "sha256": sha,
        "bytes_raw_est": int(bytes_written),
        "bytes_gzip": int(out_path.stat().st_size),
        "elapsed_sec": elapsed,
        "created_at": datetime.now().isoformat(),
    }
    (out_path.with_suffix(out_path.suffix + ".meta.json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return meta


def _cleanup_old(out_dir: Path, keep_days: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"removed": 0, "kept": 0, "keep_days": int(keep_days)}
    if keep_days <= 0:
        return out
    cutoff = time.time() - int(keep_days) * 86400
    for p in sorted(out_dir.glob("*.sql.gz")):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                meta = Path(str(p) + ".meta.json")
                meta.unlink(missing_ok=True)
                out["removed"] += 1
            else:
                out["kept"] += 1
        except Exception:
            continue
    return out


def run_backup(target: str, out_dir: Path, keep_days: int) -> Dict[str, Any]:
    profiles = _load_profiles()
    remote = _choose_remote_profile(profiles)
    local = _choose_local_profile(profiles)

    tasks: List[tuple[str, DBProfile]] = []
    if target in {"remote", "both"}:
        tasks.append(("remote", remote))
    if target in {"local", "both"}:
        tasks.append(("local", local))

    result: Dict[str, Any] = {
        "ok": True,
        "task": "backup",
        "target": target,
        "output_dir": str(out_dir),
        "items": [],
        "errors": [],
    }

    for label, prof in tasks:
        if not _ping_db(prof):
            result["ok"] = False
            result["errors"].append(f"{label}: db unreachable ({prof.host}:{prof.port})")
            continue
        last_err: Optional[str] = None
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                item = _backup_one(prof, label, out_dir)
                item["attempt"] = attempt
                result["items"].append(item)
                last_err = None
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                msg_low = str(e).lower()
                retryable = (
                    "table definition has changed" in msg_low
                    or "deadlock" in msg_low
                    or "lock wait timeout exceeded" in msg_low
                )
                if attempt < max_attempts and retryable:
                    time.sleep(1.5 * attempt)
                    continue
                break
        if last_err:
            result["ok"] = False
            result["errors"].append(f"{label}: {last_err}")

    result["rotation"] = _cleanup_old(out_dir, keep_days)
    return result


def _iter_backups(out_dir: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for p in sorted(out_dir.glob("*.sql.gz"), reverse=True):
        row: Dict[str, Any] = {
            "path": str(p),
            "size": int(p.stat().st_size),
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
            "target": "unknown",
            "database": "",
            "sha256": "",
        }
        name = p.name
        if "_remote_" in name:
            row["target"] = "remote"
        elif "_local_" in name:
            row["target"] = "local"
        meta = Path(str(p) + ".meta.json")
        if meta.exists():
            try:
                md = json.loads(meta.read_text(encoding="utf-8")) or {}
                if isinstance(md, dict):
                    row.update({
                        "target": md.get("target", row["target"]),
                        "database": md.get("database", ""),
                        "sha256": md.get("sha256", ""),
                        "host": md.get("host", ""),
                        "created_at": md.get("created_at", ""),
                    })
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 357, exc_info=True)
        rows.append(row)
    return rows


def run_list(out_dir: Path, limit: int) -> Dict[str, Any]:
    rows = _iter_backups(out_dir)
    return {
        "ok": True,
        "task": "list",
        "output_dir": str(out_dir),
        "count": len(rows),
        "items": rows[: max(1, int(limit))],
    }


def _restore_one(profile: DBProfile, file_path: Path) -> Dict[str, Any]:
    mysql = _find_bin("mysql")
    cmd = [
        mysql,
        f"--host={profile.host}",
        f"--port={int(profile.port)}",
        f"--user={profile.user}",
        "--default-character-set=utf8mb4",
        profile.database,
    ]
    env = os.environ.copy()
    env["MYSQL_PWD"] = profile.password

    t0 = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    try:
        assert proc.stdin is not None
        src = gzip.open(file_path, "rb") if file_path.suffix == ".gz" else file_path.open("rb")
        with src:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                proc.stdin.write(chunk)
        proc.stdin.close()
        stdout, stderr = proc.communicate(timeout=120)
    except Exception:
        proc.kill()
        raise

    if proc.returncode != 0:
        raise RuntimeError(f"mysql restore failed({proc.returncode}): {(stderr or b'').decode(errors='ignore')[-600:]}")

    return {
        "ok": True,
        "host": profile.host,
        "port": int(profile.port),
        "database": profile.database,
        "elapsed_sec": round(time.time() - t0, 3),
        "stdout_tail": (stdout or b"").decode(errors="ignore")[-300:],
    }


def run_restore(
    *,
    file_path: Path,
    restore_target: str,
    out_dir: Path,
    pre_backup: bool,
    keep_days: int,
    confirmed: bool,
) -> Dict[str, Any]:
    if not confirmed:
        return {
            "ok": False,
            "task": "restore",
            "error": "confirm_required",
            "message": "restore 需要 --yes-i-understand",
        }

    if not file_path.exists():
        return {
            "ok": False,
            "task": "restore",
            "error": "backup_not_found",
            "path": str(file_path),
        }

    profiles = _load_profiles()
    profile = _choose_remote_profile(profiles) if restore_target == "remote" else _choose_local_profile(profiles)
    if not _ping_db(profile):
        return {
            "ok": False,
            "task": "restore",
            "error": "db_unreachable",
            "target": restore_target,
            "host": profile.host,
            "port": int(profile.port),
        }

    result: Dict[str, Any] = {
        "ok": True,
        "task": "restore",
        "target": restore_target,
        "input": str(file_path),
        "pre_backup": None,
        "restore": None,
        "rotation": None,
        "errors": [],
    }

    if pre_backup:
        try:
            pb = _backup_one(profile, f"{restore_target}_pre_restore", out_dir)
            result["pre_backup"] = pb
        except Exception as e:
            result["ok"] = False
            result["errors"].append(f"pre_backup_failed: {type(e).__name__}: {e}")
            return result

    try:
        restore_out = _restore_one(profile, file_path)
        result["restore"] = restore_out
    except Exception as e:
        result["ok"] = False
        result["errors"].append(f"restore_failed: {type(e).__name__}: {e}")

    result["rotation"] = _cleanup_old(out_dir, keep_days)
    return result


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="DB backup/restore for MAGI")
    ap.add_argument("--task", default="backup", choices=["backup", "list", "restore"])
    ap.add_argument("--target", default="both", choices=["remote", "local", "both"], help="for backup")
    ap.add_argument("--restore-target", default="remote", choices=["remote", "local"], help="for restore")
    ap.add_argument("--output-dir", default=os.environ.get("MAGI_DB_BACKUP_DIR", DEFAULT_BACKUP_DIR))
    ap.add_argument("--keep-days", type=int, default=int(os.environ.get("MAGI_DB_BACKUP_KEEP_DAYS", "30") or "30"))
    ap.add_argument("--limit", type=int, default=20, help="for list")
    ap.add_argument("--file", default="", help="backup file for restore")
    ap.add_argument("--pre-backup", type=int, default=1, help="run backup before restore")
    ap.add_argument("--yes-i-understand", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.output_dir)

    if args.task == "backup":
        res = run_backup(args.target, out_dir, int(args.keep_days))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.task == "list":
        out_dir.mkdir(parents=True, exist_ok=True)
        res = run_list(out_dir, int(args.limit))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    file_arg = _q(args.file)
    if not file_arg:
        print(json.dumps({"ok": False, "error": "missing --file"}, ensure_ascii=False, indent=2))
        return 2

    file_path = Path(file_arg)
    if not file_path.is_absolute():
        file_path = out_dir / file_arg

    res = run_restore(
        file_path=file_path,
        restore_target=args.restore_target,
        out_dir=out_dir,
        pre_backup=bool(int(args.pre_backup)),
        keep_days=int(args.keep_days),
        confirmed=bool(args.yes_i_understand),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
