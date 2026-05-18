#!/usr/bin/env python3
"""Remove empty active-folder shells left by Synology Drive after case archive.

The cleanup is intentionally conservative:
- only cases already marked closed in DB are considered;
- only folders whose basename starts with the same OSC case number are touched;
- folders with any real file are never deleted.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import time
from pathlib import Path

try:
    import mysql.connector
except Exception as exc:  # pragma: no cover
    print(json.dumps({"ok": False, "error": f"mysql import failed: {exc}"}, ensure_ascii=False))
    raise

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from api.case_path_mapper import default_case_roots, local_synology_path_candidates
except Exception:  # pragma: no cover
    default_case_roots = None
    local_synology_path_candidates = None

IGNORED_FILENAMES = {".DS_Store", ".gitkeep", "Thumbs.db", "desktop.ini"}


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _db_config() -> dict:
    return {
        "host": os.environ.get("OSC_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("OSC_DB_PORT", "3306")),
        "user": os.environ.get("OSC_DB_USER", "casper_service"),
        "password": os.environ.get("OSC_DB_PASSWORD", ""),
        "database": os.environ.get("OSC_DB_NAME", "law_firm_data"),
        "use_pure": True,
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
    }


def _active_roots() -> list[str]:
    roots: list[str] = []
    if default_case_roots:
        try:
            roots.extend(default_case_roots(include_closed=False))
        except Exception:
            pass
    home = Path.home()
    nas_user = (
        os.environ.get("MAGI_NAS_HOME_USER")
        or os.environ.get("MAGI_NAS_USER")
        or "home"
    ).strip().strip("/\\") or "home"
    roots.extend(
        [
            str(home / "Library/CloudStorage/SynologyDrive-homes/01_案件"),
            str(home / "SynologyDrive/homes/01_案件"),
            str(home / "SynologyDrive/01_案件"),
            f"/Volumes/homes/{nas_user}/01_案件",
        ]
    )
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        text = str(root or "").rstrip("/")
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _real_file_count(folder: str) -> tuple[int, int]:
    real_files = 0
    dirs = 0
    for current, dirnames, filenames in os.walk(folder):
        dirs += len(dirnames)
        for name in filenames:
            if name in IGNORED_FILENAMES:
                continue
            real_files += 1
            if real_files > 0:
                return real_files, dirs
    return real_files, dirs


def _closed_cases(limit: int) -> list[dict]:
    conn = mysql.connector.connect(**_db_config())
    try:
        cur = conn.cursor(dictionary=True)
        sql = """
            SELECT case_number, client_name, folder_path, status, legal_aid_status
            FROM cases
            WHERE case_number IS NOT NULL AND case_number <> ''
              AND (
                status LIKE '%結案%'
                OR legal_aid_status LIKE '已結案%'
              )
            ORDER BY updated_at DESC, case_number DESC
        """
        if limit > 0:
            sql += " LIMIT %s"
            cur.execute(sql, (limit,))
        else:
            cur.execute(sql)
        rows = list(cur.fetchall() or [])
        cur.close()
        return rows
    finally:
        conn.close()


def _candidate_shells(case_number: str, archived_folder_path: str) -> list[str]:
    archived_existing = ""
    if local_synology_path_candidates and archived_folder_path:
        try:
            archived_existing = next(
                (p for p in local_synology_path_candidates(archived_folder_path) if os.path.isdir(p)),
                "",
            )
        except Exception:
            archived_existing = ""

    candidates: list[str] = []
    for root in _active_roots():
        base = Path(root)
        if not base.exists():
            continue
        patterns = [
            str(base / "*" / f"{case_number}-*"),
            str(base / "*" / "*" / f"{case_number}-*"),
        ]
        for pattern in patterns:
            for path in glob.glob(pattern):
                p = str(path)
                if not os.path.isdir(p):
                    continue
                if archived_existing and os.path.abspath(p) == os.path.abspath(archived_existing):
                    continue
                candidates.append(p)
    out: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def run(*, apply: bool, limit: int, max_seconds: float = 0.0) -> dict:
    _load_env()
    started = time.monotonic()
    removed: list[dict] = []
    conflicts: list[dict] = []
    checked = 0
    timed_out = False
    for row in _closed_cases(limit):
        if max_seconds > 0 and time.monotonic() - started >= max_seconds:
            timed_out = True
            break
        case_number = str(row.get("case_number") or "").strip()
        if not case_number:
            continue
        for folder in _candidate_shells(case_number, str(row.get("folder_path") or "")):
            if max_seconds > 0 and time.monotonic() - started >= max_seconds:
                timed_out = True
                break
            checked += 1
            real_files, dirs = _real_file_count(folder)
            item = {
                "case_number": case_number,
                "client_name": row.get("client_name") or "",
                "folder": folder,
                "real_files": real_files,
                "dirs": dirs,
            }
            if real_files == 0:
                if apply:
                    try:
                        shutil.rmtree(folder)
                        item["removed"] = True
                    except FileNotFoundError:
                        item["removed"] = True
                        item["already_missing"] = True
                else:
                    item["removed"] = False
                removed.append(item)
            else:
                conflicts.append(item)
        if timed_out:
            break
    return {
        "ok": True,
        "apply": apply,
        "timed_out": timed_out,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "checked": checked,
        "removed": len(removed),
        "conflicts": len(conflicts),
        "removed_items": removed[:50],
        "conflict_items": conflicts[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean empty active case shells after archive")
    parser.add_argument("--apply", action="store_true", help="actually remove empty shells")
    parser.add_argument("--limit", type=int, default=0, help="closed-case scan limit; 0 means all")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="stop after this many seconds; 0 means no budget")
    args = parser.parse_args()
    print(json.dumps(run(apply=args.apply, limit=args.limit, max_seconds=args.max_seconds), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
