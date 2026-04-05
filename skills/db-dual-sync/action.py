#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db-dual-sync / action.py

Wrapper skill for safe dual-db operations:
- status: connectivity and profile check
- sync: bidirectional upsert-only sync
- backup/list: rotating backup helper
"""

from __future__ import annotations
import logging

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_config_path, get_magi_root_dir, get_orch_dir, get_skill_python

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 32, exc_info=True)

_MAGI_ROOT = str(get_magi_root_dir())
CODE_DIR = str(get_orch_dir())
VENV_PY = str(get_skill_python())

SYNC_SCRIPT = os.path.join(_MAGI_ROOT, "skills", "ops", "database", "sync_bidirectional.py")
BACKUP_SCRIPT = os.path.join(_MAGI_ROOT, "skills", "ops", "database", "backup_restore.py")
CFG_CANDIDATES = [
    str(get_config_path("config.json")),
    str(get_config_path("legalbridge_config.json")),
]


def _ok(payload: Dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if bool(payload.get("success")) else 1


def _load_jsonish(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if not t:
        return {}
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"value": t}


def _run(cmd: List[str], timeout_sec: int = 180) -> Dict[str, Any]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(10, timeout_sec))
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        parsed = None
        try:
            parsed = json.loads(out) if out else None
        except Exception:
            parsed = None
        return {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout_tail": out[-800:],
            "stderr_tail": err[-800:],
            "parsed": parsed,
        }
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout.decode("utf-8", errors="ignore") if isinstance(e.stdout, bytes) else (e.stdout or "")) or "").strip()
        err = ((e.stderr.decode("utf-8", errors="ignore") if isinstance(e.stderr, bytes) else (e.stderr or "")) or "").strip()
        return {
            "ok": False,
            "returncode": 124,
            "error": "timeout",
            "stdout_tail": out[-800:],
            "stderr_tail": err[-800:],
            "parsed": None,
        }
    except Exception as ex:
        return {"ok": False, "returncode": 1, "error": f"{type(ex).__name__}: {ex}"}


def _status() -> Dict[str, Any]:
    profiles = []
    source_cfg = ""
    for p in CFG_CANDIDATES:
        if not os.path.exists(p):
            continue
        try:
            d = json.loads(open(p, "r", encoding="utf-8").read() or "{}")
            rows = d.get("mariadb_profiles") if isinstance(d, dict) else []
            if isinstance(rows, list) and rows:
                profiles = rows
                source_cfg = p
                break
        except Exception:
            continue

    if CODE_DIR not in sys.path:
        sys.path.insert(0, CODE_DIR)

    conns = []
    try:
        import pymysql  # type: ignore
    except Exception as e:
        return {"success": False, "error": f"missing_dep:pymysql:{type(e).__name__}"}

    for row in profiles:
        if not isinstance(row, dict):
            continue
        name = str(row.get("profile_name") or "").strip() or "unknown"
        cfg = row.get("config") if isinstance(row.get("config"), dict) else {}
        host = str(cfg.get("host") or "127.0.0.1")
        port = int(cfg.get("port") or 3306)
        user = str(cfg.get("user") or os.environ.get("OSC_DB_USER", "python_user"))
        database = str(cfg.get("database") or "law_firm_data")
        ok = False
        err = ""
        try:
            conn = pymysql.connect(
                host=host,
                port=port,
                user=user,
                password=str(cfg.get("password") or os.environ.get("OSC_DB_PASSWORD", "")),
                database=database,
                charset="utf8mb4",
                connect_timeout=int(cfg.get("connection_timeout") or 4),
                autocommit=True,
                cursorclass=pymysql.cursors.DictCursor,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                _ = cur.fetchone()
            conn.close()
            ok = True
        except Exception as e:
            ok = False
            err = f"{type(e).__name__}: {e}"
        conns.append(
            {
                "profile": name,
                "host": host,
                "port": port,
                "database": database,
                "user": user,
                "ok": ok,
                "error": err[:220],
            }
        )

    try:
        from api.routing.node_registry import get_node_ip as _get_node_ip
        _remote_db_ip = _get_node_ip("nas") or "100.121.61.74"
    except Exception:
        _remote_db_ip = "100.121.61.74"
    remote_ok = any(x.get("ok") and str(x.get("host")) == _remote_db_ip for x in conns)
    local_ok = any(x.get("ok") and str(x.get("host")) in {"127.0.0.1", "localhost"} for x in conns)
    return {
        "success": True,
        "config_path": source_cfg,
        "profiles": conns,
        "remote_ok": remote_ok,
        "local_ok": local_ok,
        "scripts": {
            "sync_script": os.path.exists(SYNC_SCRIPT),
            "backup_script": os.path.exists(BACKUP_SCRIPT),
        },
    }


def _sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.exists(SYNC_SCRIPT):
        return {"success": False, "error": f"sync_script_not_found:{SYNC_SCRIPT}"}
    cmd = [VENV_PY, SYNC_SCRIPT]
    tables = payload.get("tables")
    if isinstance(tables, list):
        tables = ",".join(str(x).strip() for x in tables if str(x).strip())
    if isinstance(tables, str) and tables.strip():
        cmd.extend(["--tables", tables.strip()])
    chunk_size = int(payload.get("chunk_size") or 800)
    update_days = int(payload.get("update_window_days") or 21)
    recent_limit = int(payload.get("recent_limit") or 5000)
    cmd.extend(["--chunk-size", str(max(100, chunk_size))])
    cmd.extend(["--update-window-days", str(max(1, update_days))])
    cmd.extend(["--recent-limit", str(max(200, recent_limit))])
    r = _run(cmd, timeout_sec=int(payload.get("timeout_sec") or 1800))
    return {
        "success": bool(r.get("ok")),
        "command": cmd,
        "result": r.get("parsed") if isinstance(r.get("parsed"), dict) else {},
        "stdout_tail": r.get("stdout_tail", ""),
        "stderr_tail": r.get("stderr_tail", ""),
        "returncode": r.get("returncode"),
    }


def _backup(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.exists(BACKUP_SCRIPT):
        return {"success": False, "error": f"backup_script_not_found:{BACKUP_SCRIPT}"}
    target = str(payload.get("target") or "both").strip().lower()
    if target not in {"remote", "local", "both"}:
        target = "both"
    keep_days = int(payload.get("keep_days") or 30)
    cmd = [VENV_PY, BACKUP_SCRIPT, "--task", "backup", "--target", target, "--keep-days", str(max(1, keep_days))]
    r = _run(cmd, timeout_sec=int(payload.get("timeout_sec") or 2400))
    return {
        "success": bool(r.get("ok")),
        "command": cmd,
        "result": r.get("parsed") if isinstance(r.get("parsed"), dict) else {},
        "stdout_tail": r.get("stdout_tail", ""),
        "stderr_tail": r.get("stderr_tail", ""),
        "returncode": r.get("returncode"),
    }


def _list_backups(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not os.path.exists(BACKUP_SCRIPT):
        return {"success": False, "error": f"backup_script_not_found:{BACKUP_SCRIPT}"}
    limit = int(payload.get("limit") or 20)
    cmd = [VENV_PY, BACKUP_SCRIPT, "--task", "list", "--limit", str(max(1, min(limit, 200)))]
    r = _run(cmd, timeout_sec=120)
    return {
        "success": bool(r.get("ok")),
        "command": cmd,
        "result": r.get("parsed") if isinstance(r.get("parsed"), dict) else {},
        "stdout_tail": r.get("stdout_tail", ""),
        "stderr_tail": r.get("stderr_tail", ""),
        "returncode": r.get("returncode"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="db-dual-sync skill")
    ap.add_argument("--task", required=True, help="help|self_test|status|sync|backup|list_backups")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "list"}:
        return _ok(
            {
                "success": True,
                "commands": [
                    "help",
                    "self_test",
                    "status",
                    "sync",
                    "sync {..json..}",
                    "backup",
                    "backup {..json..}",
                    "list_backups",
                    "list_backups {..json..}",
                ],
            }
        )

    if task == "self_test":
        st = _status()
        return _ok(
            {
                "success": bool(st.get("success")) and bool(st.get("scripts", {}).get("sync_script")) and bool(st.get("scripts", {}).get("backup_script")),
                "status": st,
            }
        )

    if task == "status":
        return _ok(_status())

    if task.startswith("sync"):
        payload = _load_jsonish(task.replace("sync", "", 1).strip())
        return _ok(_sync(payload))

    if task.startswith("backup"):
        payload = _load_jsonish(task.replace("backup", "", 1).strip())
        return _ok(_backup(payload))

    if task.startswith("list_backups"):
        payload = _load_jsonish(task.replace("list_backups", "", 1).strip())
        return _ok(_list_backups(payload))

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())
