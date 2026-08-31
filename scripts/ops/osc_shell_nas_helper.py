#!/usr/bin/env python3
"""Shell-context OSC NAS helper service.

Runs a tiny local HTTP server under the interactive user session to execute NAS
heavy I/O in a dedicated process, avoiding Flask worker stalls.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


class OscShellNASThreadingHTTPServer(ThreadingHTTPServer):
    """Local threaded helper whose shutdown never waits on a stuck NAS call."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


# Kept as a module-level seam because deployment/runtime tests replace the
# server class. The threaded implementation is essential: a large /stage copy
# may legitimately run for tens of seconds, but it must never prevent an
# unrelated /listdir or /health request from being served meanwhile.
HTTPServer = OscShellNASThreadingHTTPServer


_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_DIR = Path(
    os.environ.get("MAGI_RUNTIME_DIR", "").strip() or str(_ROOT / ".runtime")
).expanduser().resolve(strict=False)
_HELPER_HOST = os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_HOST", "127.0.0.1").strip() or "127.0.0.1"
_HELPER_PORT = int(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_PORT", "5016") or "5016")
_HELPER_LISTDIR_TIMEOUT = float(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_LISTDIR_TIMEOUT_SEC", "8") or "8")
_HELPER_LISTDIR_CHILD_TIMEOUT = float(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_LISTDIR_CHILD_TIMEOUT_SEC", "6.5") or "6.5")
_HELPER_LISTDIR_STAT_WORKERS = max(
    1,
    min(16, int(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_LISTDIR_STAT_WORKERS", "8") or "8")),
)
_HELPER_LISTDIR_CACHE_FRESH_SECONDS = max(
    1.0,
    float(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_LISTDIR_CACHE_FRESH_SEC", "5") or "5"),
)
_HELPER_LISTDIR_CACHE_STALE_SECONDS = max(
    _HELPER_LISTDIR_CACHE_FRESH_SECONDS,
    float(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_LISTDIR_CACHE_STALE_SEC", "300") or "300"),
)
_HELPER_LISTDIR_CACHE_MAX_ENTRIES = max(
    16,
    min(1024, int(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_LISTDIR_CACHE_MAX", "256") or "256")),
)
_HELPER_STAGE_TIMEOUT = float(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_STAGE_TIMEOUT_SEC", "90") or "90")
_HELPER_SOURCE_STAT_TIMEOUT = float(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_STAT_TIMEOUT_SEC", "2") or "2")
_HELPER_STAGE_TTL_SECONDS = int(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_STAGE_TTL", str(24 * 3600)) or str(24 * 3600))
_HELPER_STAGE_SLOTS = threading.BoundedSemaphore(1)
_LISTDIR_CACHE_LOCK = threading.RLock()
_LISTDIR_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LISTDIR_PATH_LOCKS: dict[str, threading.Lock] = {}
_LISTDIR_REFRESHING: set[str] = set()
_LISTDIR_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="osc-nas-refresh")


def _osc_closed_share_aliases() -> list[str]:
    base = (
        os.environ.get("MAGI_NAS_CLOSED_SHARE_NAME")
        or os.environ.get("MAGI_NAS_ARCHIVE_SHARE")
        or "lumi"
    ).strip().strip("/\\")
    aliases = [base, "lumi", "archive", "bakup"]
    extra = str(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_EXTRA_SHARES", "") or "").strip()
    for item in extra.replace("；", ",").replace(" ", ",").split(","):
        item = item.strip().strip("/\\")
        if item:
            aliases.append(item)
    unique: list[str] = []
    for share in aliases:
        share = str(share or "").strip().strip("/\\")
        if not share or share in unique:
            continue
        unique.append(share)
    return unique


def _user_nas_home() -> str:
    return (
        os.environ.get("MAGI_NAS_HOME_USER")
        or os.environ.get("MAGI_NAS_USER")
        or "home"
    ).strip().strip("/\\")


def _allowed_roots() -> list[Path]:
    home = Path.home()
    user = _user_nas_home()
    shares = _osc_closed_share_aliases()

    roots: list[Path] = []

    # User-level Samba mounts.
    roots.append(home / ".magi_mounts" / "homes" / user)
    for share in shares:
        roots.append(home / ".magi_mounts" / share / share)

    # Shared SMB-like mounts.
    roots.extend([
        Path(p)
        for p in [
            f"/Volumes/{user}",
            f"/Volumes/homes",
            f"/Volumes/homes/{user}",
            f"/Volumes/SynologyDrive",
            f"/Volumes/{user}/{user}",
        ]
    ])
    for share in shares:
        roots.append(Path(f"/Volumes/{share}"))
        roots.append(Path(f"/Volumes/{share}/{share}"))

    # Synology Drive fallbacks and legacy aliases.
    roots.extend([
        home / "Library/CloudStorage/SynologyDrive-homes",
        home / "Library/CloudStorage/SynologyDrive-home",
        home / "SynologyDrive/homes",
        home / "SynologyDrive",
        Path("/Library/CloudStorage/SynologyDrive-homes"),
        Path("/Library/CloudStorage/SynologyDrive"),
        Path("/Volumes/SynologyDrive"),
    ])

    # Runtime local exports.
    roots.extend([_ROOT / "exports", _ROOT / "static" / "exports"])
    configured_exports = os.environ.get("MAGI_EXPORTS_DIR", "").strip()
    if configured_exports:
        roots.append(Path(configured_exports).expanduser())

    extra = os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_ALLOWED_ROOTS", "").strip()
    if extra:
        for item in extra.replace(";", ",").split(","):
            item = item.strip()
            if item:
                roots.append(Path(item).expanduser())

    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        try:
            rp = root.expanduser().resolve().as_posix()
        except Exception:
            rp = str(root.expanduser())
        if rp in seen:
            continue
        seen.add(rp)
        out.append(Path(rp))
    return out


def _is_path_allowed(path: str) -> bool:
    try:
        real_path = Path(os.path.realpath(path)).as_posix()
    except Exception:
        return False
    for root in _ALLOWED_ROOTS:
        root_text = root.as_posix()
        if real_path == root_text or real_path.startswith(root_text.rstrip("/") + "/"):
            return True
    return False


def _hidden_name(name: str) -> bool:
    return bool(re.match(r"^(?:\.DS_Store$|Thumbs\.db$|~\$.*|\.synology.*|\.DocumentRevisions.*|^\._.*|.*\.tmp$|\.Spotlight.*|\.Trashes$|\.fseventsd$)", name, re.IGNORECASE))


class _ListdirHelperError(OSError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)


def _listdir_payload_uncached(path: str) -> dict[str, Any]:
    """Return listdir metadata via a timeout-bound child process.

    macOS SMB can block indefinitely inside opendir/scandir in a launchd child.
    Keep that risk in a disposable subprocess so the helper HTTP process recovers.
    """
    timeout = max(0.5, min(float(_HELPER_LISTDIR_CHILD_TIMEOUT or 6.5), float(_HELPER_LISTDIR_TIMEOUT or 8.0)))
    code = r'''
import json
import os
import re
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

path = sys.argv[1]
stat_workers = max(1, min(16, int(sys.argv[2])))

def hidden_name(name):
    return bool(re.match(r"^(?:\.DS_Store$|Thumbs\.db$|~\$.*|\.synology.*|\.DocumentRevisions.*|^\._.*|.*\.tmp$|\.Spotlight.*|\.Trashes$|\.fseventsd$)", name, re.IGNORECASE))

def emit(payload, code=0):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    raise SystemExit(code)

try:
    names = os.listdir(path)
except FileNotFoundError as exc:
    emit({"ok": False, "error": "not_found", "errno": int(getattr(exc, "errno", 0) or 0)}, 2)
except NotADirectoryError as exc:
    emit({"ok": False, "error": "not_directory", "errno": int(getattr(exc, "errno", 0) or 0)}, 2)
except OSError as exc:
    emit({"ok": False, "error": str(exc), "errno": int(getattr(exc, "errno", 0) or 0)}, 1)

def inspect(name):
    item = {
        "name": name,
        "type": "unknown",
        "is_dir": None,
        "size": None,
        "mtime": None,
        "mtime_ts": None,
        "modified_at": None,
        "hidden": hidden_name(name),
        "errno": 0,
    }
    try:
        st = os.stat(os.path.join(path, name), follow_symlinks=False)
        is_dir = bool(stat.S_ISDIR(st.st_mode))
        mtime = int(st.st_mtime)
        item["is_dir"] = is_dir
        item["type"] = "dir" if is_dir else "file"
        item["mtime"] = mtime
        item["mtime_ts"] = mtime
        item["modified_at"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        if not is_dir:
            item["size"] = int(st.st_size)
    except OSError as exc:
        item["errno"] = int(getattr(exc, "errno", 0) or 0)
        item["error"] = str(exc)
    return item

workers = max(1, min(stat_workers, len(names) or 1))
with ThreadPoolExecutor(max_workers=workers) as pool:
    entries = list(pool.map(inspect, names))

emit({"ok": True, "entries": entries, "count": len(entries)}, 0)
'''
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, path, str(_HELPER_LISTDIR_STAT_WORKERS)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ListdirHelperError(504, f"listdir_timeout: {timeout:.1f}s") from exc

    raw = (result.stdout or "").strip()
    payload: dict[str, Any] | None = None
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except Exception as exc:
            raise _ListdirHelperError(503, f"listdir parse failed: {exc}") from exc

    if result.returncode != 0:
        error = ""
        if payload:
            error = str(payload.get("error") or "")
        if not error:
            error = (result.stderr or "").strip() or f"listdir child exited {result.returncode}"
        status = 404 if error in {"not_found", "not_directory"} else 503
        raise _ListdirHelperError(status, error)

    if not payload or not payload.get("ok") or not isinstance(payload.get("entries"), list):
        raise _ListdirHelperError(503, "listdir child returned malformed payload")
    return payload


def _clone_listdir_payload(
    payload: dict[str, Any],
    *,
    cache_status: str,
    age_seconds: float,
) -> dict[str, Any]:
    cloned = dict(payload)
    cloned["entries"] = [dict(row) for row in payload.get("entries", []) if isinstance(row, dict)]
    cloned["cache_status"] = cache_status
    cloned["cache_age_seconds"] = round(max(0.0, age_seconds), 3)
    return cloned


def _listdir_cache_get(key: str) -> tuple[float, dict[str, Any]] | None:
    with _LISTDIR_CACHE_LOCK:
        cached = _LISTDIR_CACHE.get(key)
        if cached is None:
            return None
        created_at, payload = cached
        return max(0.0, time.monotonic() - created_at), payload


def _listdir_cache_put(key: str, payload: dict[str, Any]) -> None:
    with _LISTDIR_CACHE_LOCK:
        _LISTDIR_CACHE[key] = (time.monotonic(), payload)
        if len(_LISTDIR_CACHE) <= _HELPER_LISTDIR_CACHE_MAX_ENTRIES:
            return
        oldest = min(_LISTDIR_CACHE, key=lambda item: _LISTDIR_CACHE[item][0])
        _LISTDIR_CACHE.pop(oldest, None)
        _LISTDIR_PATH_LOCKS.pop(oldest, None)


def _refresh_listdir_cache(key: str, path: str) -> None:
    try:
        payload = _listdir_payload_uncached(path)
        _listdir_cache_put(key, payload)
    except (OSError, _ListdirHelperError):
        # A stale successful listing is safer for the UI than replacing it
        # with an intermittent SMB failure. The next request will retry.
        pass
    finally:
        with _LISTDIR_CACHE_LOCK:
            _LISTDIR_REFRESHING.discard(key)


def _schedule_listdir_refresh(key: str, path: str) -> None:
    with _LISTDIR_CACHE_LOCK:
        if key in _LISTDIR_REFRESHING:
            return
        _LISTDIR_REFRESHING.add(key)
    try:
        _LISTDIR_REFRESH_EXECUTOR.submit(_refresh_listdir_cache, key, path)
    except RuntimeError:
        with _LISTDIR_CACHE_LOCK:
            _LISTDIR_REFRESHING.discard(key)


def _listdir_payload(path: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Return a bounded NAS listing with stale-while-revalidate protection."""
    key = os.path.realpath(path)
    cached = _listdir_cache_get(key)
    if cached is not None and not force_refresh:
        age, payload = cached
        if age <= _HELPER_LISTDIR_CACHE_FRESH_SECONDS:
            return _clone_listdir_payload(payload, cache_status="hit", age_seconds=age)
        if age <= _HELPER_LISTDIR_CACHE_STALE_SECONDS:
            _schedule_listdir_refresh(key, path)
            return _clone_listdir_payload(payload, cache_status="stale", age_seconds=age)

    with _LISTDIR_CACHE_LOCK:
        path_lock = _LISTDIR_PATH_LOCKS.setdefault(key, threading.Lock())
    with path_lock:
        # Another cold request may have populated the cache while this caller
        # waited for the per-directory single-flight lock.
        cached = _listdir_cache_get(key)
        if cached is not None and not force_refresh:
            age, payload = cached
            if age <= _HELPER_LISTDIR_CACHE_STALE_SECONDS:
                status = "hit" if age <= _HELPER_LISTDIR_CACHE_FRESH_SECONDS else "stale"
                if status == "stale":
                    _schedule_listdir_refresh(key, path)
                return _clone_listdir_payload(payload, cache_status=status, age_seconds=age)

        try:
            payload = _listdir_payload_uncached(path)
        except (OSError, _ListdirHelperError):
            # Explicit refreshes after upload/rename must try the NAS, but an
            # intermittent refresh failure must not blank a previously usable
            # file manager.
            cached = _listdir_cache_get(key)
            if cached is not None:
                age, stale_payload = cached
                if age <= _HELPER_LISTDIR_CACHE_STALE_SECONDS:
                    return _clone_listdir_payload(
                        stale_payload,
                        cache_status="stale_refresh_failed",
                        age_seconds=age,
                    )
            raise
        _listdir_cache_put(key, payload)
        return _clone_listdir_payload(payload, cache_status="miss", age_seconds=0.0)


def _stage_path() -> Path:
    return (
        Path(os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_STAGE_DIR", "")).expanduser().resolve()
        if os.environ.get("MAGI_OSC_SHELL_NAS_HELPER_STAGE_DIR")
        else _RUNTIME_DIR / "osc_shell_stage"
    )


def _cleanup_staged_files() -> None:
    stage_dir = _stage_path()
    stage_dir.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max(3600, _HELPER_STAGE_TTL_SECONDS)
    for item in stage_dir.iterdir():
        if not item.is_file():
            continue
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
        except OSError:
            continue


def _source_file_size_quick(path: str) -> int:
    code = (
        "import json,os,stat,sys\n"
        "st=os.stat(sys.argv[1], follow_symlinks=False)\n"
        "print(json.dumps({'is_file':stat.S_ISREG(st.st_mode),'size':int(st.st_size)}))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)],
            capture_output=True,
            text=True,
            timeout=max(0.1, float(_HELPER_SOURCE_STAT_TIMEOUT)),
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"source_stat_timeout: {_HELPER_SOURCE_STAT_TIMEOUT:.1f}s") from exc
    if result.returncode != 0:
        message = (result.stderr or "source stat failed").strip()
        raise OSError(message.splitlines()[-1] if message else "source stat failed")
    try:
        payload = json.loads((result.stdout or "{}").strip() or "{}")
    except Exception as exc:
        raise OSError(f"source stat parse failed: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("is_file"):
        raise FileNotFoundError("source_not_found")
    return int(payload.get("size") or 0)


def _copy_to_stage(path: str, *, expected_size: int | None = None) -> Path:
    source = str(path)
    cp_bin = shutil.which("cp") or "/bin/cp"
    ditto_bin = shutil.which("ditto")
    stage_dir = _stage_path()
    stage_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_staged_files()

    dst = stage_dir / f"osc-shell-stage-{int(time.time()):x}-{os.getpid()}-{secrets.token_hex(4)}{Path(source).suffix or '.bin'}"
    if expected_size is None:
        expected_size = _source_file_size_quick(source)

    commands: list[list[str]] = [[cp_bin, "-p", "--", source, str(dst)]]
    if ditto_bin:
        commands.append([ditto_bin, "--norsrc", "--noextattr", "--", source, str(dst)])

    last: Exception | None = None
    for command in commands:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=_HELPER_STAGE_TIMEOUT,
            )
            if result.returncode != 0 or not dst.is_file():
                if result.stderr:
                    last = RuntimeError(result.stderr.strip())
                continue
            if dst.stat().st_size != expected_size:
                last = OSError(
                    f"staged size mismatch (expected {expected_size}, got {dst.stat().st_size})"
                )
                continue
            return dst
        except OSError as exc:
            last = exc
            continue
        except Exception as exc:  # pragma: no cover - defensive
            last = exc
    if last:
        raise last
    raise RuntimeError("stage copy failed")


_ALLOWED_ROOTS = _allowed_roots()


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class OscShellNASHandler(BaseHTTPRequestHandler):
    server_version = "osc-shell-nas-helper/1.0"
    error_content_type = "text/plain; charset=utf-8"

    def log_message(self, fmt: str, *args):  # pragma: no cover - noise in daemon logs
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length < 0:
            length = 0
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            text = raw.decode("utf-8")
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            _write_json(self, 200, {"ok": True})
            return
        if parsed.path != "/listdir":
            _write_json(self, 404, {"ok": False, "error": "not_found"})
            return

        params = parse_qs(parsed.query or "")
        path = str((params.get("path") or [""])[0]).strip()
        force_refresh = str((params.get("refresh") or [""])[0]).strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not path:
            _write_json(self, 400, {"ok": False, "error": "path required"})
            return
        if not _is_path_allowed(path):
            _write_json(self, 403, {"ok": False, "error": "path_not_allowed"})
            return

        try:
            payload = _listdir_payload(path, force_refresh=force_refresh)
        except _ListdirHelperError as exc:
            _write_json(self, exc.status, {"ok": False, "error": str(exc)})
            return
        except OSError as exc:
            _write_json(self, 503, {"ok": False, "error": str(exc)})
            return
        _write_json(self, 200, payload)

    def do_POST(self) -> None:
        if self.path != "/stage":
            _write_json(self, 404, {"ok": False, "error": "not_found"})
            return

        data = self._read_json()
        path = str(data.get("path", "")).strip()
        if not path:
            _write_json(self, 400, {"ok": False, "error": "path required"})
            return
        if not _is_path_allowed(path):
            _write_json(self, 403, {"ok": False, "error": "path_not_allowed"})
            return
        try:
            expected_size = _source_file_size_quick(path)
        except FileNotFoundError:
            _write_json(self, 404, {"ok": False, "error": "source_not_found"})
            return
        except (OSError, TimeoutError) as exc:
            _write_json(self, 503, {"ok": False, "error": str(exc)})
            return

        if not _HELPER_STAGE_SLOTS.acquire(blocking=False):
            _write_json(self, 429, {"ok": False, "error": "stage_busy"})
            return
        try:
            staged = _copy_to_stage(path, expected_size=expected_size)
        except OSError as exc:
            _write_json(self, 503, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            _write_json(self, 500, {"ok": False, "error": str(exc)})
            return
        finally:
            _HELPER_STAGE_SLOTS.release()

        _write_json(self, 200, {"ok": True, "staged_path": str(staged), "size": staged.stat().st_size})


def main() -> int:
    server = HTTPServer((_HELPER_HOST, _HELPER_PORT), OscShellNASHandler)
    server.timeout = _HELPER_LISTDIR_TIMEOUT
    pid_file = str(_RUNTIME_DIR / "osc_shell_nas_helper.pid")
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    Path(pid_file).write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            Path(pid_file).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
