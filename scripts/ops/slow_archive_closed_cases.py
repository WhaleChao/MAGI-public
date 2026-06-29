#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Off-peak, resumable closed-case archive mover.

Large case folders can overwhelm a small NAS when moved in one operation.  This
worker copies closed active-case residue to the closed archive with rsync
throttling, verifies the result, then removes the active folder only after the
target is complete.  The DB keeps the canonical Windows archive path (Y:) during
the whole transition.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.blueprints.osc_cases import (  # noqa: E402
    _osc_archive_relative_parent,
    _osc_find_active_case_folder,
)
from api.case_path_mapper import (  # noqa: E402
    local_case_path_candidates,
    translate_local_path_to_canonical,
)
from api.osc.utils import (  # noqa: E402
    _osc_exec,
    _osc_norm_path,
    _osc_replace_path_prefix_references,
)
from api.domains.case_file_operation_lock import acquire_case_file_operation_lock, release_case_file_operation_lock  # noqa: E402

def _default_archive_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    env_root = os.environ.get("MAGI_SLOW_ARCHIVE_ROOT", "").strip()
    if env_root:
        roots.append(Path(env_root).expanduser())
    roots.extend(
        [
            Path.home() / ".magi_mounts" / "lumi" / "lumi" / "03_工作資料" / "10_結案",
            Path("/Volumes/lumi/lumi/03_工作資料/10_結案"),
            Path("/Volumes/lumi-1/lumi/03_工作資料/10_結案"),
            Path("/Volumes/lumi-2/lumi/03_工作資料/10_結案"),
        ]
    )
    return tuple(dict.fromkeys(roots))


DEFAULT_ARCHIVE_ROOTS = _default_archive_roots()
RUNTIME_DIR = ROOT / ".runtime"
LATEST_PATH = RUNTIME_DIR / "slow_archive_closed_cases_latest.json"
HISTORY_PATH = RUNTIME_DIR / "slow_archive_closed_cases.jsonl"


def _offpeak_now() -> bool:
    now = datetime.now().time()
    return now.hour in {1, 2, 3, 4, 5}


def _stat_quick(path: Path, timeout: float = 1.5):
    result: dict[str, Any] = {"stat": None, "error": None}

    def _run() -> None:
        try:
            result["stat"] = path.stat()
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"stat timeout: {path}")
    if result["error"] is not None:
        raise result["error"]
    return result["stat"]


def _is_dir_quick(path: Path, timeout: float = 1.5) -> bool:
    try:
        st = _stat_quick(path, timeout=timeout)
        return bool(st.st_mode & 0o40000)
    except Exception:
        return False


def _is_skip_name(name: str) -> bool:
    return name in {".DS_Store", ".gitkeep", "Thumbs.db"} or name.startswith("._")


def _rsync_append_flag(rsync: str) -> str:
    try:
        proc = subprocess.run([rsync, "--help"], capture_output=True, text=True, timeout=5)
        help_text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    except Exception:
        help_text = ""
    if "--append-verify" in help_text:
        return "--append-verify"
    if "--append" in help_text:
        return "--append"
    return ""


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _server_path_map() -> list[dict[str, str]]:
    raw = os.environ.get("MAGI_NAS_SERVER_PATH_MAP_JSON", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    out: list[dict[str, str]] = []
    for item in parsed if isinstance(parsed, list) else []:
        if not isinstance(item, dict):
            continue
        local_prefix = str(item.get("local_prefix") or "").rstrip("/")
        remote_prefix = str(item.get("remote_prefix") or "").rstrip("/")
        if local_prefix and remote_prefix:
            out.append({"local_prefix": local_prefix, "remote_prefix": remote_prefix})
    return sorted(out, key=lambda x: len(x["local_prefix"]), reverse=True)


def _map_local_to_server_path(path: Path) -> str:
    text = str(path).rstrip("/")
    for item in _server_path_map():
        local_prefix = item["local_prefix"]
        if text == local_prefix or text.startswith(local_prefix + "/"):
            suffix = text[len(local_prefix) :].lstrip("/")
            return item["remote_prefix"] + ("/" + suffix if suffix else "")
    return ""


def _ssh_target() -> str:
    host = os.environ.get("MAGI_NAS_SSH_HOST", "").strip()
    user = os.environ.get("MAGI_NAS_SSH_USER", "").strip()
    if not host:
        return ""
    return f"{user}@{host}" if user else host


def _build_server_side_rsync_command(
    src: Path,
    dst: Path,
    *,
    dry_run: bool,
    bwlimit_mbps: float,
    rsync_timeout_sec: int,
) -> list[str]:
    if not _truthy_env("MAGI_SLOW_ARCHIVE_SERVER_SIDE"):
        return []
    target = _ssh_target()
    remote_src = _map_local_to_server_path(src)
    remote_dst = _map_local_to_server_path(dst)
    if not target or not remote_src or not remote_dst:
        return []
    bw_kbps = max(1, int(max(0.05, bwlimit_mbps) * 1024))
    dry = "--dry-run " if dry_run else ""
    # Resolve append support on the NAS side so old DSM rsync versions still work.
    remote_script = (
        "set -e; "
        "if rsync --help 2>&1 | grep -q -- '--append-verify'; then APP='--append-verify'; "
        "elif rsync --help 2>&1 | grep -q -- '--append'; then APP='--append'; else APP=''; fi; "
        f"mkdir -p {shlex.quote(remote_dst)}; "
        "rsync -a --partial ${APP} --human-readable "
        f"--bwlimit={bw_kbps} --timeout={max(30, int(rsync_timeout_sec))} "
        "--exclude=.DS_Store --exclude='._*' "
        f"{dry}{shlex.quote(remote_src.rstrip('/') + '/')} {shlex.quote(remote_dst.rstrip('/') + '/')}"
    )
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        target,
        remote_script,
    ]


def _tree_signature(path: Path) -> dict[str, Any]:
    files = 0
    dirs = 0
    size = 0
    if not path.exists():
        return {"exists": False, "files": 0, "dirs": 0, "size": 0}
    for root, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not _is_skip_name(d)]
        dirs += len(dirnames)
        for filename in filenames:
            if _is_skip_name(filename):
                continue
            fp = Path(root) / filename
            try:
                files += 1
                size += fp.stat().st_size
            except OSError:
                continue
    return {"exists": True, "files": files, "dirs": dirs, "size": size}


def _shallow_path_info(path: Path) -> dict[str, Any]:
    try:
        st = _stat_quick(path, timeout=1.5)
        return {
            "exists": True,
            "is_dir": bool(st.st_mode & 0o40000),
            "size": st.st_size if bool(st.st_mode & 0o100000) else None,
        }
    except OSError:
        return {"exists": False, "is_dir": False, "size": 0}
    except TimeoutError:
        return {"exists": False, "is_dir": False, "size": 0, "timeout": True}


def _archive_root(allow_cloud_target: bool = False) -> Path | None:
    for root in DEFAULT_ARCHIVE_ROOTS:
        if _is_dir_quick(root):
            return root
    if allow_cloud_target:
        cloud = Path.home() / "Library/CloudStorage/SynologyDrive-homes/archive/03_工作資料/10_結案"
        if _is_dir_quick(cloud):
            return cloud
    return None


def _closed_rows(case_number: str = "", limit: int = 1) -> list[dict[str, Any]]:
    where = [
        "(status LIKE '%結案%' OR legal_aid_status LIKE '%結案%')",
        "("
        "folder_path LIKE 'Y:%' OR folder_path LIKE 'Y:\\\\%' "
        "OR folder_path LIKE '%/03_工作資料/10_結案/%' "
        "OR folder_path LIKE '%\\\\03_工作資料\\\\10_結案\\\\%' "
        "OR folder_path LIKE 'Z:%' OR folder_path LIKE 'Z:\\\\%' "
        "OR folder_path LIKE '%/01_案件/%' "
        "OR folder_path LIKE '%\\\\01_案件\\\\%'"
        ")",
    ]
    params: list[Any] = []
    if case_number:
        where.append("case_number=%s")
        params.append(case_number)
    sql = f"""
        SELECT id, case_number, client_name, status, legal_aid_status, folder_path,
               manual_status_lock, manual_status_source, updated_at
          FROM cases
         WHERE {' AND '.join(where)}
         ORDER BY CASE WHEN case_number='2025-0002' THEN 0 ELSE 1 END,
                  updated_at DESC, case_number ASC
         LIMIT %s
    """
    params.append(max(1, int(limit)))
    rows, _ = _osc_exec(sql, tuple(params), fetch="all")
    return [dict(r) for r in (rows or [])]


def _source_candidate_rank(path: str) -> tuple[int, str]:
    text = str(path or "").replace("\\", "/")
    if text.startswith("/Volumes/homes/") or text.startswith("/Volumes/homes-"):
        return (0, text)
    if "/.magi_mounts/homes/" in text:
        return (1, text)
    if text.startswith("/Volumes/"):
        return (2, text)
    if "/Library/CloudStorage/SynologyDrive" in text or "/SynologyDrive" in text:
        return (4, text)
    return (3, text)


def _first_existing_source(candidates: list[str]) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in sorted([str(c or "") for c in candidates if c], key=_source_candidate_rank):
        text = raw.rstrip("/")
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    for candidate in ordered:
        if _is_dir_quick(Path(candidate)):
            return str(Path(candidate))
    return ""


def _active_candidates_for_closed_path(folder_path: str) -> list[str]:
    normalized = _osc_norm_path(folder_path or "").replace("\\", "/")
    if "/03_工作資料/10_結案/" not in normalized:
        return []
    rel = normalized.split("/03_工作資料/10_結案/", 1)[1].lstrip("/")
    account = (os.environ.get("MAGI_NAS_HOME_USER") or os.environ.get("MAGI_NAS_USER") or "home").strip().strip("/\\") or "home"
    candidates: list[str] = []
    for mount in ["/Volumes/homes", *sorted(glob.glob("/Volumes/homes-*"))]:
        candidates.append(str(Path(mount) / account / "01_案件" / rel))
    candidates.extend(
        [
            str(Path.home() / ".magi_mounts/homes" / account / "01_案件" / rel),
            str(Path.home() / "Library/CloudStorage/SynologyDrive-homes/01_案件" / rel),
            str(Path.home() / "Library/CloudStorage/SynologyDrive-homes" / account / "01_案件" / rel),
            str(Path.home() / "SynologyDrive/homes/01_案件" / rel),
            str(Path.home() / "SynologyDrive/homes" / account / "01_案件" / rel),
            str(Path.home() / "SynologyDrive/01_案件" / rel),
        ]
    )
    return candidates


def _active_source_for(row: dict[str, Any]) -> str:
    """Resolve the old active folder without broad NAS scans when possible."""
    raw_folder_path = row.get("folder_path") or ""
    folder_path = _osc_norm_path(raw_folder_path).replace("\\", "/")
    if (
        folder_path.startswith("Z:/")
        or folder_path.startswith("/Volumes/homes/")
        or "/01_案件/" in folder_path
        or "\\01_案件\\" in str(raw_folder_path)
    ):
        direct = _first_existing_source(local_case_path_candidates(str(raw_folder_path)))
        if direct:
            return direct
    if "/03_工作資料/10_結案/" in folder_path:
        direct = _first_existing_source(_active_candidates_for_closed_path(folder_path))
        if direct:
            return direct
    return _osc_find_active_case_folder(row.get("case_number") or "")


def _target_for(row: dict[str, Any], archive_root: Path) -> tuple[Path, str]:
    active = _active_source_for(row)
    if not active:
        return Path(), ""
    active_path = Path(active)
    rel_parent = _osc_archive_relative_parent(str(active_path))
    target = archive_root / rel_parent / active_path.name if rel_parent else archive_root / active_path.name
    return target, active


def _run_rsync(
    src: Path,
    dst: Path,
    *,
    dry_run: bool,
    bwlimit_mbps: float,
    timeout_sec: int,
    rsync_timeout_sec: int,
) -> dict[str, Any]:
    rsync = shutil.which("rsync")
    if not rsync:
        return {"ok": False, "reason": "rsync_missing"}
    dst.mkdir(parents=True, exist_ok=True)
    server_cmd = _build_server_side_rsync_command(
        src,
        dst,
        dry_run=dry_run,
        bwlimit_mbps=bwlimit_mbps,
        rsync_timeout_sec=rsync_timeout_sec,
    )
    if server_cmd:
        started = time.time()
        proc = subprocess.Popen(
            server_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=max(10, timeout_sec))
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                out, err = proc.communicate()
        if proc.returncode == 0 and not timed_out:
            return {
                "ok": True,
                "partial": False,
                "returncode": proc.returncode,
                "duration_sec": round(time.time() - started, 2),
                "mode": "nas_server_side_rsync",
                "cmd": server_cmd[:5] + ["..."],
                "stdout_tail": (out or "")[-2000:],
                "stderr_tail": (err or "")[-2000:],
            }
        if not _truthy_env("MAGI_SLOW_ARCHIVE_SERVER_SIDE_STRICT"):
            server_attempt = {
                "ok": False,
                "partial": bool(timed_out),
                "returncode": proc.returncode,
                "duration_sec": round(time.time() - started, 2),
                "mode": "nas_server_side_rsync",
                "cmd": server_cmd[:5] + ["..."],
                "stdout_tail": (out or "")[-2000:],
                "stderr_tail": (err or "")[-2000:],
            }
        else:
            return {
                "ok": False,
                "partial": bool(timed_out),
                "returncode": proc.returncode,
                "duration_sec": round(time.time() - started, 2),
                "mode": "nas_server_side_rsync",
                "cmd": server_cmd[:5] + ["..."],
                "stdout_tail": (out or "")[-2000:],
                "stderr_tail": (err or "")[-2000:],
            }
    else:
        server_attempt = {}
    bw_kbps = max(1, int(max(0.05, bwlimit_mbps) * 1024))
    cmd = [
        rsync,
        "-a",
        "--partial",
        "--human-readable",
        f"--bwlimit={bw_kbps}",
        f"--timeout={max(30, int(rsync_timeout_sec))}",
        "--exclude=.DS_Store",
        "--exclude=._*",
    ]
    append_flag = _rsync_append_flag(rsync)
    if append_flag:
        cmd.insert(3, append_flag)
    if dry_run:
        cmd.append("--dry-run")
    cmd.extend([str(src).rstrip("/") + "/", str(dst).rstrip("/") + "/"])
    started = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=max(10, timeout_sec))
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
            out, err = proc.communicate()
    return {
        "ok": proc.returncode == 0 and not timed_out,
        "partial": bool(timed_out),
        "returncode": proc.returncode,
        "duration_sec": round(time.time() - started, 2),
        "mode": "mac_smb_rsync",
        "server_side_attempt": server_attempt,
        "cmd": cmd[:3] + ["..."],
        "stdout_tail": (out or "")[-2000:],
        "stderr_tail": (err or "")[-2000:],
    }


def _update_db_after_complete(row: dict[str, Any], source: Path, target: Path) -> dict[str, Any]:
    canonical_target = translate_local_path_to_canonical(str(target)) or str(target)
    canonical_source = translate_local_path_to_canonical(str(source)) or str(source)
    _osc_exec(
        """
        UPDATE cases
           SET folder_path=%s,
               status='已結案',
               manual_status_lock=1,
               manual_status_source=COALESCE(manual_status_source, 'slow_archive_closed_cases'),
               manual_status_at=COALESCE(manual_status_at, NOW()),
               updated_at=NOW()
         WHERE id=%s
        """,
        (_osc_norm_path(canonical_target), row.get("id")),
        fetch="none",
    )
    path_updates = _osc_replace_path_prefix_references(canonical_source, canonical_target, exec_fn=_osc_exec)
    local_updates = _osc_replace_path_prefix_references(str(source), str(target), exec_fn=_osc_exec)
    return {
        "canonical_target": _osc_norm_path(canonical_target),
        "canonical_source": _osc_norm_path(canonical_source),
        "path_references": {
            "updated": int(path_updates.get("updated") or 0) + int(local_updates.get("updated") or 0),
            "attempted": int(path_updates.get("attempted") or 0) + int(local_updates.get("attempted") or 0),
            "errors": (path_updates.get("errors") or []) + (local_updates.get("errors") or []),
        },
    }


def _process(row: dict[str, Any], archive_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    target, active = _target_for(row, archive_root)
    item: dict[str, Any] = {
        "case_number": row.get("case_number") or "",
        "client_name": row.get("client_name") or "",
        "folder_path": row.get("folder_path") or "",
        "source": active,
        "target": str(target) if target else "",
    }
    if not active:
        item.update({"ok": True, "skipped": True, "reason": "active_residue_not_found"})
        return item
    source = Path(active)
    if not source.is_dir():
        item.update({"ok": False, "reason": "source_not_dir"})
        return item
    if args.dry_run and not args.deep_dry_run:
        item.update({
            "ok": True,
            "dry_run": True,
            "shallow": True,
            "source_signature": _shallow_path_info(source),
            "target_signature": _shallow_path_info(target),
        })
        return item
    src_sig = _tree_signature(source)
    item["source_signature"] = src_sig
    if src_sig.get("size", 0) < max(0, int(float(args.min_size_mb) * 1024 * 1024)):
        item.update({"ok": True, "skipped": True, "reason": "below_min_size"})
        return item
    if args.dry_run:
        target_sig = _tree_signature(target)
        item.update({"ok": True, "dry_run": True, "target_signature": target_sig})
        return item

    rsync_result = _run_rsync(
        source,
        target,
        dry_run=False,
        bwlimit_mbps=float(args.bwlimit_mbps),
        timeout_sec=int(args.max_runtime_sec),
        rsync_timeout_sec=int(args.rsync_timeout_sec),
    )
    item["rsync"] = rsync_result
    target_sig = _tree_signature(target)
    item["target_signature"] = target_sig
    complete = (
        bool(target_sig.get("exists"))
        and int(target_sig.get("files") or 0) >= int(src_sig.get("files") or 0)
        and int(target_sig.get("size") or 0) >= int(src_sig.get("size") or 0)
    )
    if not complete:
        item.update({"ok": True, "partial": True, "reason": "copy_incomplete_will_resume"})
        return item

    db_update = _update_db_after_complete(row, source, target)
    item["db_update"] = db_update
    cleanup_error = ""
    if not args.keep_source:
        try:
            shutil.rmtree(source)
        except Exception as exc:
            cleanup_error = str(exc)
    item.update({
        "ok": True,
        "completed": True,
        "reason": "archived_and_source_removed" if not cleanup_error else "archived_cleanup_failed",
        "source_cleanup_error": cleanup_error,
    })
    return item


def _write_report(report: dict[str, Any], json_out: str = "") -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")
    if json_out:
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Slowly archive large closed OSC case folders during off-peak hours.")
    parser.add_argument("--case-number", default=os.environ.get("MAGI_SLOW_ARCHIVE_CASE_NUMBER", ""), help="Limit to a case number.")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--min-size-mb", type=float, default=float(os.environ.get("MAGI_SLOW_ARCHIVE_MIN_SIZE_MB", "100") or 100))
    parser.add_argument("--bwlimit-mbps", type=float, default=float(os.environ.get("MAGI_SLOW_ARCHIVE_BWLIMIT_MBPS", "3") or 3))
    parser.add_argument("--max-runtime-sec", type=int, default=int(os.environ.get("MAGI_SLOW_ARCHIVE_MAX_RUNTIME_SEC", "5400") or 5400))
    parser.add_argument("--rsync-timeout-sec", type=int, default=int(os.environ.get("MAGI_SLOW_ARCHIVE_RSYNC_TIMEOUT_SEC", "600") or 600))
    parser.add_argument("--allow-now", action="store_true", help="Allow apply outside off-peak.")
    parser.add_argument("--allow-cloud-target", action="store_true", help="Allow Synology Drive archive target when SMB archive is unavailable.")
    parser.add_argument("--keep-source", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--deep-dry-run", action="store_true", help="Dry-run with recursive signatures. Avoid during office hours on NAS.")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    args.dry_run = not bool(args.apply)

    report: dict[str, Any] = {
        "ok": True,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "dry_run",
        "offpeak": _offpeak_now(),
        "items": [],
    }
    if args.apply and (not args.allow_now) and (not _offpeak_now()):
        report.update({"ok": True, "skipped": True, "reason": "outside_offpeak"})
        _write_report(report, args.json_out)
        if args.print_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.apply:
        case_file_lock = acquire_case_file_operation_lock(owner="slow_archive_closed_cases")
        report["case_file_operation_lock"] = case_file_lock
        if not case_file_lock.get("acquired"):
            report.update({
                "ok": True,
                "skipped": True,
                "reason": "case_file_operation_already_running",
                "active_pid": case_file_lock.get("active_pid"),
                "lock_path": case_file_lock.get("lock_path") or "",
            })
            _write_report(report, args.json_out)
            if args.print_json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

    try:
        archive_root = _archive_root(allow_cloud_target=bool(args.allow_cloud_target))
        if archive_root is None:
            report.update({"ok": False, "reason": "archive_root_not_mounted", "items": []})
            _write_report(report, args.json_out)
            if args.print_json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2 if args.apply else 0

        report["archive_root"] = str(archive_root)
        rows = _closed_rows(case_number=args.case_number, limit=max(1, args.limit))
        for row in rows:
            report["items"].append(_process(row, archive_root, args))
        report["completed_at"] = datetime.now().isoformat(timespec="seconds")
        _write_report(report, args.json_out)
        if args.print_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        failed = [item for item in report["items"] if not item.get("ok")]
        return 1 if failed else 0
    finally:
        if args.apply:
            release_case_file_operation_lock()


if __name__ == "__main__":
    raise SystemExit(main())
