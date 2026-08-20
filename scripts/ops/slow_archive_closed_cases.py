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
import errno
import glob
import hashlib
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
from pathlib import Path, PurePath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# This worker is also an operations CLI.  Load the release-bound environment
# before importing OSC database helpers so an isolated/manual recovery behaves
# exactly like the scheduled service without shell-sourcing or exposing secrets.
try:
    from dotenv import load_dotenv as _load_dotenv
    from api.runtime_paths import get_env_file as _get_env_file

    _prebound_paths = {
        key: os.environ.get(key, "").strip()
        for key in ("MAGI_ROOT", "MAGI_ROOT_DIR", "MAGI_RUNTIME_DIR")
    }
    _bound_env_file = _get_env_file()
    _load_dotenv(_bound_env_file, override=False)
    # An external secret file may outlive many immutable releases.  Deployment
    # bindings must come from launchd/current execution, never from stale
    # MAGI_ROOT values left inside that shared file.
    if not _prebound_paths["MAGI_ROOT"]:
        os.environ["MAGI_ROOT"] = str(ROOT)
    if not _prebound_paths["MAGI_ROOT_DIR"]:
        os.environ["MAGI_ROOT_DIR"] = str(ROOT)
    if not _prebound_paths["MAGI_RUNTIME_DIR"]:
        shared = _bound_env_file.parent.parent if _bound_env_file.parent.name == "external" else None
        if shared is not None and shared.name == "shared" and (shared / "runtime").is_dir():
            os.environ["MAGI_RUNTIME_DIR"] = str(shared / "runtime")
except Exception:
    pass

from magi_v3.case_lifecycle import (  # noqa: E402
    canonical_case_status,
    requires_closed_storage,
)
from api.case_path_mapper import (  # noqa: E402
    local_case_path_candidates,
    preferred_case_roots,
    translate_local_path_to_canonical,
)
from api.osc.utils import (  # noqa: E402
    _osc_exec,
    _osc_norm_case_category,
    _osc_norm_path,
    _osc_replace_path_prefix_references,
)
from api.osc.folder_utils import build_full_case_path  # noqa: E402
from api.domains.case_file_operation_lock import acquire_case_file_operation_lock, release_case_file_operation_lock  # noqa: E402

def _default_archive_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    env_root = os.environ.get("MAGI_SLOW_ARCHIVE_ROOT", "").strip()
    if env_root:
        roots.append(Path(env_root).expanduser())
    share = (os.environ.get("MAGI_NAS_ARCHIVE_SHARE") or "lumi").strip().strip("/\\")
    aliases = [share, f"{share}-1", f"{share}-2"]
    roots.extend(
        Path("/Volumes") / alias / share / "03_工作資料" / "10_結案"
        for alias in aliases
    )
    roots.append(Path.home() / ".magi_mounts" / share / share / "03_工作資料" / "10_結案")
    return tuple(dict.fromkeys(roots))


DEFAULT_ARCHIVE_ROOTS = _default_archive_roots()
RUNTIME_DIR = Path(os.environ.get("MAGI_RUNTIME_DIR", "").strip() or ROOT / ".runtime").expanduser()
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


def _iter_case_dirs_at_depth(root: str):
    """Yield only standard category/type/case directories without Flask."""
    if not root or not _is_dir_quick(Path(root)):
        return
    try:
        categories = os.scandir(root)
    except OSError:
        return
    with categories:
        for category in categories:
            if category.name.startswith("."):
                continue
            try:
                if not category.is_dir(follow_symlinks=False):
                    continue
                case_types = os.scandir(category.path)
            except OSError:
                continue
            with case_types:
                for case_type in case_types:
                    if case_type.name.startswith("."):
                        continue
                    try:
                        if not case_type.is_dir(follow_symlinks=False):
                            continue
                        case_dirs = os.scandir(case_type.path)
                    except OSError:
                        continue
                    with case_dirs:
                        for case_dir in case_dirs:
                            if case_dir.name.startswith("."):
                                continue
                            try:
                                if case_dir.is_dir(follow_symlinks=False):
                                    yield case_dir.path
                            except OSError:
                                continue


def _case_root_rank(value: str) -> tuple[int, str]:
    text = str(value or "")
    if text.startswith("/Volumes/"):
        return (0, text)
    if "/Library/CloudStorage/" in text:
        return (2, text)
    if "/.magi_mounts/" in text:
        return (3, text)
    return (1, text)


def _unique_paths(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value or "").rstrip("/") for value in values if str(value or "").strip()))


def _closed_case_roots() -> list[str]:
    active = preferred_case_roots(include_closed=False)
    all_roots = preferred_case_roots(include_closed=True)
    closed = all_roots[len(active):] if len(all_roots) >= len(active) else []
    closed.extend(str(path) for path in DEFAULT_ARCHIVE_ROOTS)
    ordered = sorted(_unique_paths(closed), key=_case_root_rank)
    # When the real archive SMB is mounted, a stale user-level mount is not a
    # valid source of truth.  Keeping it in the resolver would recreate the
    # original ghost-folder bug for any case missing from the real NAS.
    real_smb_available = any(
        root.startswith("/Volumes/") and _is_dir_quick(Path(root))
        for root in ordered
    )
    if real_smb_available:
        ordered = [root for root in ordered if "/.magi_mounts/" not in root]
    return ordered


def _find_case_folder(roots: list[str], case_number: str, folder_name: str = "") -> str:
    exact: list[str] = []
    case_matches: list[str] = []
    for root in sorted(_unique_paths(roots), key=_case_root_rank):
        for case_dir in _iter_case_dirs_at_depth(root) or []:
            name = Path(case_dir).name
            if folder_name and name == folder_name:
                exact.append(case_dir)
            elif case_number and (name == case_number or name.startswith(f"{case_number}-")):
                case_matches.append(case_dir)
    if exact:
        return exact[0]
    unique = _unique_paths(case_matches)
    return unique[0] if len(unique) == 1 else ""


def _find_active_case_folder(case_number: str, folder_name: str = "") -> str:
    return _find_case_folder(
        preferred_case_roots(include_closed=False),
        str(case_number or "").strip(),
        str(folder_name or "").strip(),
    )


def _archive_relative_parent(source_path: str) -> str:
    norm = _osc_norm_path(source_path).replace("\\", "/").strip("/")
    parts = [part for part in norm.split("/") if part]
    try:
        index = len(parts) - 1 - list(reversed(parts)).index("01_案件")
    except ValueError:
        return ""
    relative = parts[index + 1:-1]
    return os.path.join(*relative) if relative else ""


def _expected_closed_case_folder_for_row(row: dict[str, Any]) -> str:
    case_number = str(row.get("case_number") or "").strip()
    if not case_number:
        return ""
    folder_name = ""
    for root in _closed_case_roots():
        expected = build_full_case_path(
            root,
            case_number,
            str(row.get("client_name") or "").strip(),
            case_type=str(row.get("case_type") or "").strip(),
            case_category=_osc_norm_case_category(row.get("case_category") or ""),
            case_stage=str(row.get("case_stage") or "").strip(),
            case_reason=str(row.get("case_reason") or "").strip(),
        )
        folder_name = Path(expected).name
        if _is_dir_quick(Path(expected)):
            return expected
    return _find_case_folder(_closed_case_roots(), case_number, folder_name)


def _legacy_ghost_case_folder_for_row(row: dict[str, Any]) -> tuple[Path | None, Path | None]:
    """Return a unique legacy user-mount copy only as a recovery source.

    ``_closed_case_roots`` intentionally excludes ``~/.magi_mounts`` while a
    real SMB archive is mounted.  That prevents a stale ghost folder from
    becoming LIVE ownership.  Some pre-V3 archives, however, exist only in
    that legacy location.  Keep those copies outside normal resolution and
    expose them solely to the verified recovery transaction below.
    """
    case_number = str(row.get("case_number") or "").strip()
    if not case_number:
        return None, None
    ghost_roots = [
        root for root in DEFAULT_ARCHIVE_ROOTS
        if "/.magi_mounts/" in str(root)
    ]
    folder_name = ""
    for root in ghost_roots:
        expected = build_full_case_path(
            str(root),
            case_number,
            str(row.get("client_name") or "").strip(),
            case_type=str(row.get("case_type") or "").strip(),
            case_category=_osc_norm_case_category(row.get("case_category") or ""),
            case_stage=str(row.get("case_stage") or "").strip(),
            case_reason=str(row.get("case_reason") or "").strip(),
        )
        folder_name = Path(expected).name
        if _is_dir_quick(Path(expected)):
            return Path(expected), Path(root)
    found = _find_case_folder(
        [str(root) for root in ghost_roots],
        case_number,
        folder_name,
    )
    if not found:
        return None, None
    found_path = Path(found)
    for root in ghost_roots:
        try:
            found_path.relative_to(root)
            return found_path, Path(root)
        except ValueError:
            continue
    return None, None


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


def _isolated_sha256_file(path: Path) -> str:
    """Hash one file in a killable process so a stale SMB read cannot hold the case lock."""
    tool = Path("/usr/bin/shasum")
    if not tool.is_file():
        raise RuntimeError("sha256_tool_missing")
    try:
        configured = max(
            30,
            int(os.environ.get("MAGI_SLOW_ARCHIVE_VERIFY_FILE_TIMEOUT_SEC") or "180"),
        )
    except (TypeError, ValueError):
        configured = 180
    try:
        size = max(0, int(path.stat().st_size))
    except OSError:
        size = 0
    # Allow genuinely large files enough time even on a slow NAS while keeping
    # small-file hangs bounded.  256 KiB/s is deliberately conservative.
    timeout = max(configured, 30 + int(size / (256 * 1024)))
    try:
        proc = subprocess.run(
            [str(tool), "-a", "256", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"sha256_smb_timeout:{timeout}s:{path}") from exc
    except Exception as exc:
        raise RuntimeError(f"sha256_smb_helper_failed:{type(exc).__name__}:{path}") from exc
    value = str(proc.stdout or "").strip().split(maxsplit=1)[0].lower()
    try:
        valid = len(value) == 64 and int(value, 16) >= 0
    except ValueError:
        valid = False
    if proc.returncode != 0 or not valid:
        raise RuntimeError(f"sha256_smb_helper_invalid:{proc.returncode}:{path}")
    return value


def _sha256_file(path: Path) -> str:
    def _macos_smb_sha256_fallback(error: OSError) -> str:
        if error.errno != errno.EBADF or not str(path).startswith("/Volumes/"):
            raise error
        try:
            return _isolated_sha256_file(path)
        except Exception as exc:
            raise RuntimeError(f"sha256_smb_fallback_failed:{type(exc).__name__}:{path}") from exc

    if str(path).startswith("/Volumes/"):
        return _isolated_sha256_file(path)

    digest = hashlib.sha256()
    try:
        fh = path.open("rb")
        reached_eof = False
        try:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    reached_eof = True
                    break
                digest.update(chunk)
        finally:
            try:
                fh.close()
            except OSError as exc:
                # macOS smbfs may close a reconnected handle server-side and
                # report EBADF locally even though the complete file was read.
                if not (reached_eof and exc.errno == errno.EBADF):
                    raise
    except OSError as exc:
        return _macos_smb_sha256_fallback(exc)
    return digest.hexdigest()


def _verify_source_covered_once(
    source: Path,
    target: Path,
    *,
    timeout_sec: int = 7200,
    hash_files: bool = True,
) -> dict[str, Any]:
    """Prove every source file exists at the same relative target path.

    Aggregate file counts and byte totals are not sufficient: unrelated or
    duplicate target files can make both totals look complete while a source
    document is still absent.  Archival source deletion therefore requires a
    path-by-path check and, by default, a SHA-256 content match.
    """
    started = time.monotonic()
    checked_files = 0
    checked_bytes = 0
    missing: list[str] = []
    mismatched: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    timed_out = False

    if not source.is_dir() or not target.is_dir():
        return {
            "ok": False,
            "method": "relative_path_size_sha256" if hash_files else "relative_path_size",
            "checked_files": 0,
            "checked_bytes": 0,
            "missing_count": 1,
            "mismatch_count": 0,
            "error_count": 0,
            "missing": ["source_or_target_not_directory"],
            "mismatched": [],
            "errors": [],
            "details_truncated": False,
            "timed_out": False,
            "duration_sec": round(time.monotonic() - started, 2),
        }

    def _walk_error(exc: OSError) -> None:
        errors.append({"path": str(getattr(exc, "filename", "") or source), "reason": f"walk_error:{exc}"})

    for root, dirnames, filenames in os.walk(source, onerror=_walk_error):
        for dirname in tuple(dirnames):
            directory = Path(root) / dirname
            try:
                if directory.is_symlink():
                    errors.append({"path": str(directory.relative_to(source)), "reason": "source_symlink_not_certified"})
                    dirnames.remove(dirname)
            except OSError as exc:
                errors.append({"path": str(directory), "reason": f"directory_check:{exc}"})
                dirnames.remove(dirname)
        dirnames[:] = [name for name in dirnames if not _is_skip_name(name)]
        for filename in filenames:
            if _is_skip_name(filename):
                continue
            if timeout_sec > 0 and time.monotonic() - started > timeout_sec:
                timed_out = True
                break
            src = Path(root) / filename
            try:
                rel = src.relative_to(source)
                dst = target / rel
                if src.is_symlink():
                    errors.append({"path": rel.as_posix(), "reason": "source_symlink_not_certified"})
                    continue
                src_stat = src.stat()
                if not dst.is_file() or dst.is_symlink():
                    missing.append(rel.as_posix())
                    continue
                dst_stat = dst.stat()
                if src_stat.st_size != dst_stat.st_size:
                    mismatched.append({
                        "path": rel.as_posix(),
                        "reason": "size_differs",
                        "source_size": int(src_stat.st_size),
                        "target_size": int(dst_stat.st_size),
                    })
                    continue
                if hash_files:
                    src_sha = _sha256_file(src)
                    dst_sha = _sha256_file(dst)
                    if src_sha != dst_sha:
                        mismatched.append({
                            "path": rel.as_posix(),
                            "reason": "sha256_differs",
                            "source_sha256": src_sha,
                            "target_sha256": dst_sha,
                        })
                        continue
                checked_files += 1
                checked_bytes += int(src_stat.st_size)
            except Exception as exc:
                errors.append({"path": str(filename), "reason": f"{type(exc).__name__}: {exc}"})
        if timed_out:
            break

    detail_limit = 100
    ok = not timed_out and not missing and not mismatched and not errors
    return {
        "ok": ok,
        "method": "relative_path_size_sha256" if hash_files else "relative_path_size",
        "checked_files": checked_files,
        "checked_bytes": checked_bytes,
        "missing_count": len(missing),
        "mismatch_count": len(mismatched),
        "error_count": len(errors),
        "missing": missing[:detail_limit],
        "mismatched": mismatched[:detail_limit],
        "errors": errors[:detail_limit],
        "details_truncated": any(len(items) > detail_limit for items in (missing, mismatched, errors)),
        "timed_out": timed_out,
        "duration_sec": round(time.monotonic() - started, 2),
    }


def _is_smb_verification_target(path: Path) -> bool:
    """Return True only for a real mounted archive, never a user ghost."""
    text = str(path)
    return text.startswith("/Volumes/") and "/.magi_mounts/" not in text


def _refresh_smb_target_listing(target: Path, verification: dict[str, Any]) -> None:
    """Best-effort refresh of smbfs directory entries reported as absent.

    macOS smbfs can briefly expose the pre-copy directory snapshot after a
    successful rsync.  Listing the affected parent directories before the
    next proof pass avoids treating that cache lag as permanent data loss.
    This function never creates, removes, or changes a file.
    """
    parents: set[Path] = {target}
    for rel in verification.get("missing") or []:
        candidate = target / str(rel)
        parents.add(candidate.parent)
    for item in verification.get("mismatched") or []:
        candidate = target / str((item or {}).get("path") or "")
        parents.add(candidate.parent)
    for parent in sorted(parents, key=lambda value: len(value.parts)):
        try:
            with os.scandir(parent) as entries:
                for _entry in entries:
                    pass
        except OSError:
            continue


def _verify_source_covered(
    source: Path,
    target: Path,
    *,
    timeout_sec: int = 7200,
    hash_files: bool = True,
) -> dict[str, Any]:
    """Prove full source coverage, tolerating only bounded smbfs cache lag.

    A failed pass never becomes success by assumption: real mounted SMB
    targets receive up to five complete proof passes, and one full pass
    must independently reach zero missing, mismatch, and error counts.
    Non-SMB targets retain the original single-pass fail-closed behaviour.
    """
    try:
        attempts = max(1, min(8, int(os.environ.get("MAGI_SLOW_ARCHIVE_VERIFY_ATTEMPTS") or "5")))
    except Exception:
        attempts = 5
    try:
        settle_seconds = max(0.0, min(30.0, float(os.environ.get("MAGI_SLOW_ARCHIVE_VERIFY_SETTLE_SEC") or "3")))
    except Exception:
        settle_seconds = 3.0
    if not _is_smb_verification_target(target):
        attempts = 1

    history: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        result = _verify_source_covered_once(
            source,
            target,
            timeout_sec=timeout_sec,
            hash_files=hash_files,
        )
        history.append({
            "attempt": attempt,
            "ok": bool(result.get("ok")),
            "missing_count": int(result.get("missing_count") or 0),
            "mismatch_count": int(result.get("mismatch_count") or 0),
            "error_count": int(result.get("error_count") or 0),
            "timed_out": bool(result.get("timed_out")),
            "duration_sec": result.get("duration_sec"),
        })
        if result.get("ok") or attempt >= attempts or result.get("timed_out"):
            break
        _refresh_smb_target_listing(target, result)
        if settle_seconds:
            time.sleep(min(15.0, settle_seconds * attempt))

    result = dict(result)
    result["attempts"] = len(history)
    result["attempt_history"] = history
    result["smb_settle_retry"] = bool(len(history) > 1)
    return result


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


def _schedule_fixture_rows() -> list[dict[str, Any]] | None:
    raw_path = str(os.environ.get("MAGI_SLOW_ARCHIVE_FIXTURE_PATH") or "").strip()
    if not raw_path:
        return None
    fixture_raw = str(os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT") or "").strip()
    if (
        os.environ.get("MAGI_V3_SCHEDULE_ADAPTER") != "real_entrypoint_fixture_v1"
        or os.environ.get("MAGI_V3_SCHEDULE_DRY_RUN") != "1"
        or not fixture_raw
    ):
        raise RuntimeError("slow-archive certification fixture is not safely bound")
    fixture = Path(fixture_raw).expanduser().resolve()
    manifest_path = Path(raw_path).expanduser().resolve()
    if (
        not (fixture / ".magi-v3-schedule-fixture").is_file()
        or not manifest_path.is_file()
        or not manifest_path.is_relative_to(fixture)
    ):
        raise RuntimeError("slow-archive certification fixture escaped its owned root")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("slow-archive certification fixture is unreadable") from exc
    rows = manifest.get("cases")
    if manifest.get("schema") != "magi.v3.slow-archive-fixture/v1" or not isinstance(rows, list):
        raise RuntimeError("slow-archive certification fixture schema is invalid")
    if not rows or len(rows) > 3 or any(not isinstance(item, dict) for item in rows):
        raise RuntimeError("slow-archive certification fixture rows are invalid")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(rows, start=1):
        source = (fixture / str(item.get("source") or "")).resolve()
        parent = PurePath(str(item.get("archive_relative_parent") or ""))
        if (
            not source.is_dir()
            or source.is_symlink()
            or not source.is_relative_to(fixture)
            or parent.is_absolute()
            or ".." in parent.parts
            or not parent.parts
        ):
            raise RuntimeError("slow-archive certification case path is invalid")
        out.append(
            {
                "id": int(item.get("id") or index),
                "case_number": str(item.get("case_number") or ""),
                "client_name": str(item.get("client_name") or ""),
                "status": "已結案",
                "legal_aid_status": str(item.get("legal_aid_status") or "已結案"),
                "folder_path": str(item.get("folder_path") or ""),
                "fixture_source": str(source),
                "fixture_archive_relative_parent": parent.as_posix(),
            }
        )
    return out


def _path_reconcile_priority(row: dict[str, Any], active_case_numbers: set[str]) -> int | None:
    if not requires_closed_storage(row):
        return None
    path = str(row.get("folder_path") or "").replace("\\", "/")
    case_number = str(row.get("case_number") or "").strip()
    if path.startswith("Z:/") or "/01_案件/" in path or case_number in active_case_numbers:
        return 0
    if "/10_結案/" in path or path.startswith("Y:/"):
        rel = path.split("/10_結案/", 1)[1].strip("/") if "/10_結案/" in path else ""
        if rel and len([part for part in rel.split("/") if part]) < 3:
            return 1
        # A canonical-looking DB string is insufficient: if the categorized
        # folder is absent from the authoritative archive, retain it in the
        # recovery queue instead of silently presenting a broken link.
        if not _expected_closed_case_folder_for_row(row):
            return 2
        return None
    # Keep empty/non-standard paths in the queue.  The worker will locate a
    # real source/archive or emit a data-integrity failure; it never creates an
    # empty folder to hide missing case material.
    return 2


def _active_case_number_snapshot() -> set[str]:
    out: set[str] = set()
    for root in preferred_case_roots(include_closed=False):
        for path in _iter_case_dirs_at_depth(str(root)) or []:
            name = Path(path).name
            if len(name) >= 9 and name[4] == "-" and name[:4].isdigit() and name[5:9].isdigit():
                out.add(name[:9])
    return out


def _closed_rows(
    case_number: str = "",
    limit: int = 1,
    *,
    include_reconciled: bool = False,
) -> list[dict[str, Any]]:
    fixture_rows = _schedule_fixture_rows()
    if fixture_rows is not None:
        selected = [row for row in fixture_rows if not case_number or row["case_number"] == case_number]
        return selected[: max(1, int(limit))]
    # Apply the shared lifecycle parser to the bounded case table.  The former
    # SQL LIKE '%結案%' silently omitted 待送出、已報結、已轉入 and 待轉入.
    where = ["case_number IS NOT NULL", "case_number<>''"]
    params: list[Any] = []
    if case_number:
        where.append("case_number=%s")
        params.append(case_number)
    sql = f"""
        SELECT id, case_number, client_name, case_category, case_type, case_stage,
               case_reason, status, legal_aid_status, folder_path,
               manual_status_lock, manual_status_source, updated_at
          FROM cases
         WHERE {' AND '.join(where)}
         ORDER BY updated_at ASC, case_number ASC
         LIMIT %s
    """
    params.append(max(5000, int(limit) * 100))
    rows, _ = _osc_exec(sql, tuple(params), fetch="all")
    active_case_numbers = _active_case_number_snapshot()
    selected: list[tuple[int, str, dict[str, Any]]] = []
    for raw in rows or []:
        row = dict(raw)
        priority = _path_reconcile_priority(row, active_case_numbers)
        if priority is None and include_reconciled and requires_closed_storage(row):
            priority = 3
        if priority is None:
            continue
        selected.append((priority, str(row.get("updated_at") or ""), row))
    selected.sort(key=lambda item: (item[0], item[1], str(item[2].get("case_number") or "")))
    return [item[2] for item in selected[: max(1, int(limit))]]


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
    fixture_source = str(row.get("fixture_source") or "").strip()
    if fixture_source:
        source = Path(fixture_source).resolve()
        fixture_raw = str(os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT") or "").strip()
        fixture = Path(fixture_raw).expanduser().resolve() if fixture_raw else None
        if fixture and source.is_relative_to(fixture) and source.is_dir() and not source.is_symlink():
            return str(source)
        raise RuntimeError("slow-archive certification source is no longer valid")
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
    return _find_active_case_folder(row.get("case_number") or "")


def _target_for(row: dict[str, Any], archive_root: Path) -> tuple[Path, str]:
    active = _active_source_for(row)
    if not active:
        return Path(), ""
    active_path = Path(active)
    fixture_parent = str(row.get("fixture_archive_relative_parent") or "").strip()
    if fixture_parent:
        return archive_root / fixture_parent / active_path.name, active
    rel_parent = _archive_relative_parent(str(active_path))
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


def _update_db_after_complete(
    row: dict[str, Any],
    source: Path | None,
    target: Path,
) -> dict[str, Any]:
    canonical_target = translate_local_path_to_canonical(str(target)) or str(target)
    raw_source = str(source) if source is not None else str(row.get("folder_path") or "")
    canonical_source = translate_local_path_to_canonical(raw_source) or raw_source
    status = canonical_case_status(row)
    _osc_exec(
        """
        UPDATE cases
           SET folder_path=%s,
               status=%s,
               updated_at=NOW()
         WHERE id=%s
        """,
        (_osc_norm_path(canonical_target), status, row.get("id")),
        fetch="none",
    )
    path_updates = _osc_replace_path_prefix_references(canonical_source, canonical_target, exec_fn=_osc_exec)
    local_updates = (
        _osc_replace_path_prefix_references(str(source), str(target), exec_fn=_osc_exec)
        if source is not None
        else {"updated": 0, "attempted": 0, "errors": []}
    )
    return {
        "canonical_target": _osc_norm_path(canonical_target),
        "canonical_source": _osc_norm_path(canonical_source),
        "path_references": {
            "updated": int(path_updates.get("updated") or 0) + int(local_updates.get("updated") or 0),
            "attempted": int(path_updates.get("attempted") or 0) + int(local_updates.get("attempted") or 0),
            "errors": (path_updates.get("errors") or []) + (local_updates.get("errors") or []),
        },
    }


def _recover_missing_closed_case_from_drive(row: dict[str, Any]) -> dict[str, Any]:
    """Restore a missing NAS archive from a unique Drive case folder.

    This is deliberately download-only: it never creates or moves Drive
    folders, never overwrites an existing NAS file, and the caller still
    requires a verified local archive before updating DB/path references.
    """
    case_number = str(row.get("case_number") or "").strip()
    if not case_number:
        return {"ok": False, "reason": "missing_case_number"}
    try:
        from api.osc.drive_case_sync import run_priority_case_sync

        result = run_priority_case_sync(
            case_numbers=[case_number],
            interactive=False,
            file_diff=True,
            execute_downloads=True,
            execute_uploads=False,
            download_limit=0,
            max_download_bytes=0,
            max_case_depth=20,
            max_case_items=10000,
            ensure_drive_case_folders=False,
        )
        verification = run_priority_case_sync(
            case_numbers=[case_number],
            interactive=False,
            file_diff=True,
            execute_downloads=False,
            execute_uploads=False,
            max_case_depth=20,
            max_case_items=10000,
            ensure_drive_case_folders=False,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"drive_recovery_failed:{type(exc).__name__}:{exc}"}
    execution = result.get("execution_result") or {}
    folder = result.get("drive_folder_result") or {}
    file_plan = result.get("file_sync_plan") or {}
    folder_summary = folder.get("summary") or {}
    file_summary = file_plan.get("summary") or {}
    verified_folder = verification.get("drive_folder_result") or {}
    verified_plan = verification.get("file_sync_plan") or {}
    verified_folder_summary = verified_folder.get("summary") or {}
    verified_file_summary = verified_plan.get("summary") or {}
    ok = (
        int(folder_summary.get("resolved") or 0) == 1
        and int(folder_summary.get("failed") or 0) == 0
        and int(file_summary.get("case_errors") or 0) == 0
        and int(verified_folder_summary.get("resolved") or 0) == 1
        and int(verified_folder_summary.get("failed") or 0) == 0
        and int(verified_file_summary.get("drive_missing_in_nas_files") or 0) == 0
        and int(verified_file_summary.get("conflict_files") or 0) == 0
        and int(verified_file_summary.get("content_mismatch_files") or 0) == 0
        and int(verified_file_summary.get("incomplete_case_scans") or 0) == 0
        and int(verified_file_summary.get("case_errors") or 0) == 0
    )
    return {
        "ok": ok,
        "reason": "drive_recovery_complete" if ok else "drive_recovery_incomplete",
        "drive_folder_result": folder,
        "file_sync_summary": file_summary,
        "post_verification_file_sync_summary": verified_file_summary,
        "execution_result": execution,
        "output_paths": result.get("output_paths") or {},
        "verification_output_paths": verification.get("output_paths") or {},
    }


def _recover_missing_closed_case_from_legacy_ghost(
    row: dict[str, Any],
    archive_root: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Copy a legacy ghost archive into the real SMB and prove every byte.

    The ghost is never returned as a canonical archive and is never deleted.
    It may only seed a missing real archive after normal active storage and
    Drive recovery are unavailable.  DB/path references are committed only
    after path-by-path size and SHA-256 coverage succeeds.
    """
    ghost, ghost_root = _legacy_ghost_case_folder_for_row(row)
    if ghost is None or ghost_root is None:
        return {"ok": False, "reason": "legacy_ghost_not_found"}
    if "/.magi_mounts/" not in str(ghost) or not _is_real_archive_recovery_target(archive_root):
        return {"ok": False, "reason": "legacy_ghost_recovery_boundary_rejected"}
    try:
        relative = ghost.relative_to(ghost_root)
    except ValueError:
        return {"ok": False, "reason": "legacy_ghost_relative_path_invalid"}
    target = archive_root / relative
    source_signature = _tree_signature(ghost)
    if not source_signature.get("exists") or int(source_signature.get("files") or 0) <= 0:
        return {
            "ok": False,
            "reason": "legacy_ghost_empty_or_unreadable",
            "source": str(ghost),
            "target": str(target),
            "source_signature": source_signature,
        }
    rsync_result = _run_rsync(
        ghost,
        target,
        dry_run=False,
        bwlimit_mbps=float(getattr(args, "bwlimit_mbps", 3)),
        timeout_sec=int(getattr(args, "max_runtime_sec", 7200)),
        rsync_timeout_sec=int(getattr(args, "rsync_timeout_sec", 600)),
    )
    target_signature = _tree_signature(target)
    shallow_complete = bool(
        target_signature.get("exists")
        and int(source_signature.get("files") or 0) == int(target_signature.get("files") or 0)
        and int(source_signature.get("dirs") or 0) == int(target_signature.get("dirs") or 0)
        and int(source_signature.get("size") or 0) == int(target_signature.get("size") or 0)
    )
    if not rsync_result.get("ok") and not shallow_complete:
        return {
            "ok": False,
            "reason": "legacy_ghost_rsync_incomplete",
            "source": str(ghost),
            "target": str(target),
            "source_signature": source_signature,
            "target_signature": target_signature,
            "rsync": rsync_result,
        }
    try:
        verify_timeout = max(0, int(os.environ.get("MAGI_SLOW_ARCHIVE_VERIFY_TIMEOUT_SEC") or "7200"))
    except Exception:
        verify_timeout = 7200
    verification = _verify_source_covered(
        ghost,
        target,
        timeout_sec=verify_timeout,
        hash_files=True,
    )
    if not verification.get("ok"):
        return {
            "ok": False,
            "reason": "legacy_ghost_coverage_unproven",
            "source": str(ghost),
            "target": str(target),
            "source_signature": source_signature,
            "target_signature": target_signature,
            "rsync": rsync_result,
            "source_coverage_verification": verification,
        }
    return {
        "ok": True,
        "reason": "legacy_ghost_recovery_complete",
        "source": str(ghost),
        "target": str(target),
        "source_signature": source_signature,
        "target_signature": target_signature,
        "rsync": rsync_result,
        "source_coverage_verification": verification,
        "db_update": _update_db_after_complete(row, ghost, target),
        "legacy_source_retained": True,
    }


def _is_real_archive_recovery_target(path: Path) -> bool:
    text = str(path)
    return text.startswith("/Volumes/") and "/.magi_mounts/" not in text


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
        archived = _expected_closed_case_folder_for_row(row)
        recovery: dict[str, Any] = {}
        verify_drive = bool(getattr(args, "verify_drive_case", False))
        if bool(getattr(args, "apply", False)) and (verify_drive or not archived):
            drive_recovery = _recover_missing_closed_case_from_drive(row)
            archived = _expected_closed_case_folder_for_row(row)
            if drive_recovery.get("ok"):
                recovery = drive_recovery
            elif not archived:
                ghost_recovery = _recover_missing_closed_case_from_legacy_ghost(
                    row,
                    archive_root,
                    args,
                )
                if ghost_recovery.get("ok"):
                    recovery = {
                        "ok": True,
                        "reason": "legacy_ghost_recovery_complete",
                        "drive_recovery": drive_recovery,
                        "legacy_ghost_recovery": ghost_recovery,
                    }
                    archived = _expected_closed_case_folder_for_row(row)
                else:
                    recovery = {
                        "ok": False,
                        "reason": "closed_case_recovery_incomplete",
                        "drive_recovery": drive_recovery,
                        "legacy_ghost_recovery": ghost_recovery,
                    }
            else:
                recovery = drive_recovery
            if not recovery.get("ok"):
                item.update({
                    "ok": False,
                    "completed": False,
                    "reason": "closed_case_recovery_verification_incomplete",
                    "target": archived,
                    "drive_recovery": recovery,
                })
                return item
        if archived:
            canonical_target = _osc_norm_path(translate_local_path_to_canonical(archived) or archived)
            canonical_current = _osc_norm_path(str(row.get("folder_path") or ""))
            if canonical_target != canonical_current:
                if bool(getattr(args, "dry_run", False)):
                    item.update({
                        "ok": True,
                        "dry_run": True,
                        "completed": False,
                        "reason": "archive_path_reconcile_planned",
                        "target": archived,
                    })
                    return item
                item.update({
                    "ok": True,
                    "completed": True,
                    "reason": "archive_path_reconciled",
                    "target": archived,
                    "db_update": _update_db_after_complete(row, None, Path(archived)),
                    "drive_recovery": recovery,
                })
                return item
            item.update({
                "ok": True,
                "completed": True,
                "reason": "archive_path_verified",
                "target": archived,
                "drive_recovery": recovery,
            })
            return item
        item.update({
            "ok": False,
            "completed": False,
            "reason": "closed_case_storage_missing",
            "message": "案件已進入結案階段，但找不到進行中來源或可驗證的結案歸檔；已 fail-closed，未建立空資料夾。",
            "drive_recovery": recovery,
        })
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
    try:
        verify_timeout = max(0, int(os.environ.get("MAGI_SLOW_ARCHIVE_VERIFY_TIMEOUT_SEC") or "7200"))
    except Exception:
        verify_timeout = 7200
    hash_files = not (
        os.environ.get("MAGI_SLOW_ARCHIVE_VERIFY_SHA256", "1").strip().lower()
        in {"0", "false", "no", "off"}
    )
    rsync_ok = bool(rsync_result.get("ok"))
    if not rsync_ok:
        # macOS smbfs rsync can return 23 when its temporary-file rename loses
        # a reconnected directory handle, even though the final target already
        # contains every byte.  Never accept counts alone.  They are only the
        # admission gate to the same path-by-path SHA-256 proof used after a
        # clean rsync.  A genuine partial copy still fails closed and resumes.
        shallow_complete = bool(
            src_sig.get("exists")
            and target_sig.get("exists")
            and int(src_sig.get("files") or 0) == int(target_sig.get("files") or 0)
            and int(src_sig.get("dirs") or 0) == int(target_sig.get("dirs") or 0)
            and int(src_sig.get("size") or 0) == int(target_sig.get("size") or 0)
        )
        if not shallow_complete:
            item.update({"ok": True, "partial": True, "reason": "rsync_incomplete_will_resume"})
            return item
    verification = _verify_source_covered(
        source,
        target,
        timeout_sec=verify_timeout,
        hash_files=hash_files,
    )
    item["source_coverage_verification"] = verification
    if not verification.get("ok"):
        item.update({
            "ok": True,
            "partial": True,
            "reason": (
                "rsync_nonzero_coverage_unproven_will_resume"
                if not rsync_ok
                else "source_coverage_unproven_will_resume"
            ),
        })
        return item
    if not rsync_ok:
        item["rsync_nonzero_verified_complete"] = True

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


def _clear_launcher_pid(path: Path | None = None) -> bool:
    pid_path = path or (RUNTIME_DIR / "slow_archive_closed_cases_worker.pid")
    try:
        owner_pid = int(pid_path.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, OSError, ValueError):
        return False
    if owner_pid != os.getpid():
        return False
    try:
        pid_path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


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
    parser.add_argument(
        "--verify-drive-case",
        action="store_true",
        help="For one --case-number, resume download-only Drive recovery and require a zero-missing post-check.",
    )
    parser.add_argument("--json-out", default="")
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    args.dry_run = not bool(args.apply)
    if args.verify_drive_case and (not args.apply or not str(args.case_number or "").strip()):
        parser.error("--verify-drive-case requires --apply and one --case-number")

    report: dict[str, Any] = {
        "ok": True,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "dry_run",
        "offpeak": _offpeak_now(),
        "items": [],
    }
    if os.environ.get("MAGI_SLOW_ARCHIVE_FIXTURE_PATH"):
        report.update(
            {
                "provider_quality_certified": False,
                "provider_role": "bounded_nas_archive_filesystem_fixture",
            }
        )
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
        rows = _closed_rows(
            case_number=args.case_number,
            limit=max(1, args.limit),
            include_reconciled=bool(args.verify_drive_case),
        )
        for row in rows:
            report["items"].append(_process(row, archive_root, args))
        report["completed_at"] = datetime.now().isoformat(timespec="seconds")
        _write_report(report, args.json_out)
        if args.print_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        failed = [item for item in report["items"] if not item.get("ok")]
        report["ok"] = not failed
        if failed:
            _write_report(report, args.json_out)
        return 1 if failed else 0
    finally:
        if args.apply:
            release_case_file_operation_lock()
        _clear_launcher_pid()


if __name__ == "__main__":
    raise SystemExit(main())
