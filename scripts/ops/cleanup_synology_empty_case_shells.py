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
import re
import shutil
import subprocess
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
CASE_FOLDER_RE = re.compile(r"^(\d{4}-\d{4})(?:-|$)")


def _include_local_synology_roots() -> bool:
    return os.environ.get("MAGI_CLEAN_EMPTY_CASE_SHELL_INCLUDE_LOCAL", "0").strip().lower() in {"1", "true", "on", "yes"}


def _is_local_synology_root(path: str) -> bool:
    text = str(path or "")
    return "SynologyDrive" in text or "/Library/CloudStorage/" in text


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
    include_local = _include_local_synology_roots()
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
    roots.extend([f"/Volumes/homes/{nas_user}/01_案件"])
    if include_local:
        # Local Synology Drive File Provider views can block on dataless folders
        # and may rehydrate stale empty shells.  Keep them opt-in; cleanup should
        # normally operate on the real NAS/SMB active case root.
        roots.extend(
            [
                str(home / "Library/CloudStorage/SynologyDrive-homes/01_案件"),
                str(home / "SynologyDrive/homes/01_案件"),
                str(home / "SynologyDrive/01_案件"),
            ]
        )
    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        text = str(root or "").rstrip("/")
        if not include_local and _is_local_synology_root(text):
            continue
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


def _case_number_from_folder_name(name: str) -> str:
    match = CASE_FOLDER_RE.match(name or "")
    return match.group(1) if match else ""


def _same_active_shell_paths(folder: str) -> list[str]:
    """Return exact SMB/File Provider views for the same active case shell.

    Synology Drive can re-upload an empty shell from its local File Provider
    view after the SMB copy is removed.  We do not scan local views by default
    because that can hang; instead derive only the exact sibling paths for a
    known empty shell and remove all empty views together.
    """
    text = str(folder or "").rstrip("/")
    home = Path.home()
    nas_user = (
        os.environ.get("MAGI_NAS_HOME_USER")
        or os.environ.get("MAGI_NAS_USER")
        or "home"
    ).strip().strip("/\\") or "home"
    prefixes = [
        f"/Volumes/homes/{nas_user}",
        str(home / "Library/CloudStorage/SynologyDrive-homes"),
        str(home / "SynologyDrive/homes"),
        str(home / "SynologyDrive"),
    ]
    rel = ""
    for prefix in prefixes:
        prefix = prefix.rstrip("/")
        if text == prefix or text.startswith(prefix + "/"):
            rel = text[len(prefix):].lstrip("/")
            break
    if not rel:
        return [text]
    candidates = [os.path.join(prefix, rel) for prefix in prefixes]
    candidates.sort(key=lambda p: 1 if p.startswith("/Volumes/") else 0)
    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    if text and text not in seen:
        out.append(text)
    return out


def _path_exists(path: str) -> bool:
    try:
        return os.path.lexists(path)
    except OSError:
        return False


def _remove_tree(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
        return
    # Use rm with a short timeout for Synology File Provider placeholders;
    # shutil.rmtree can block for a long time on dataless directory shells.
    result = subprocess.run(["/bin/rm", "-rf", path], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout or f"rm exited {result.returncode}").strip())
    if _path_exists(path):
        raise OSError("remove finished but path still exists")


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


def _iter_active_case_shells() -> list[tuple[str, str]]:
    """Scan active roots once and return (case_number, folder_path)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in _active_roots():
        if not os.path.isdir(root):
            continue
        try:
            first_level = [entry for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for first in first_level:
            try:
                second_level = [entry for entry in os.scandir(first.path) if entry.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for second in second_level:
                case_number = _case_number_from_folder_name(second.name)
                if case_number:
                    key = os.path.abspath(second.path)
                    if key not in seen:
                        seen.add(key)
                        out.append((case_number, second.path))
                    continue
                try:
                    third_level = [entry for entry in os.scandir(second.path) if entry.is_dir(follow_symlinks=False)]
                except OSError:
                    continue
                for third in third_level:
                    case_number = _case_number_from_folder_name(third.name)
                    if not case_number:
                        continue
                    key = os.path.abspath(third.path)
                    if key not in seen:
                        seen.add(key)
                        out.append((case_number, third.path))
    return out


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
    errors: list[dict] = []
    checked = 0
    timed_out = False
    rows_by_case: dict[str, dict] = {}
    for row in _closed_cases(limit):
        case_number = str(row.get("case_number") or "").strip()
        if case_number:
            rows_by_case.setdefault(case_number, row)
    for case_number, folder in _iter_active_case_shells():
        if max_seconds > 0 and time.monotonic() - started >= max_seconds:
            timed_out = True
            break
        row = rows_by_case.get(case_number)
        if not row:
            continue
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
            peer_paths = [p for p in _same_active_shell_paths(folder) if _path_exists(p)]
            blocked = False
            for peer in peer_paths:
                peer_real_files, peer_dirs = _real_file_count(peer)
                if peer_real_files > 0:
                    conflicts.append(
                        {
                            **item,
                            "folder": peer,
                            "real_files": peer_real_files,
                            "dirs": peer_dirs,
                            "reason": "same_shell_view_has_real_files",
                        }
                    )
                    blocked = True
            if blocked:
                continue
            item["paths"] = peer_paths or [folder]
            if apply:
                removed_paths: list[str] = []
                for peer in peer_paths or [folder]:
                    try:
                        _remove_tree(peer)
                        removed_paths.append(peer)
                    except FileNotFoundError:
                        removed_paths.append(peer)
                    except Exception as exc:
                        errors.append({"path": peer, "error": str(exc)})
                item["removed"] = bool(removed_paths) and not any(e.get("path") in (peer_paths or [folder]) for e in errors)
                item["removed_paths"] = removed_paths
            else:
                item["removed"] = False
            removed.append(item)
        else:
            conflicts.append(item)
    return {
        "ok": True,
        "apply": apply,
        "include_local_synology_roots": _include_local_synology_roots(),
        "active_roots": _active_roots(),
        "timed_out": timed_out,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "checked": checked,
        "removed": len(removed),
        "conflicts": len(conflicts),
        "errors": len(errors),
        "removed_items": removed[:50],
        "conflict_items": conflicts[:50],
        "error_items": errors[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean empty active case shells after archive")
    parser.add_argument("--apply", action="store_true", help="actually remove empty shells")
    parser.add_argument("--limit", type=int, default=0, help="closed-case scan limit; 0 means all")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="stop after this many seconds; 0 means no budget")
    parser.add_argument("--json-out", default="", help="write the JSON report to this path")
    args = parser.parse_args()
    report = run(apply=args.apply, limit=args.limit, max_seconds=args.max_seconds)
    if args.json_out:
        out_path = Path(args.json_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
