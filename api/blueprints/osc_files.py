"""
OSC Files Blueprint — NAS File Manager (Phase 1)
=================================================
Generic file/folder operations for the web NAS file manager.
Distinct from osc_cases.py's case-bound `/folder-browser` (kept for back-compat).

Routes:
    GET  /api/osc/folders/browse    — list entries under any allowed path (with hidden filter, dir summary)
    GET  /api/osc/folders/tree      — lazy-load tree node children
    POST /api/osc/folders/mkdir     — create new folder
    POST /api/osc/folders/rename    — rename file or folder
    POST /api/osc/folders/move      — move file/folder (incl. .trash recycle bin)
    POST /api/osc/files/upload-multi — multi-file upload (extends osc_cases upload)
    GET  /api/osc/files/preview     — unified preview (delegates to api/osc/preview.py)
    GET  /api/osc/files/info        — file metadata
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import hashlib
import json
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, request, jsonify, send_file, Response
from flask_login import current_user, login_required

from api.osc.utils import (
    _osc_is_safe_local_path,
    _osc_resolve_existing_local_path,
    _osc_local_path_candidates,
    _osc_norm_path,
    _osc_relpath_under,
    _osc_human_size,
)
from api.osc import preview as osc_preview

osc_files_bp = Blueprint("osc_files", __name__)
_log = logging.getLogger(__name__)

# ── helpers ─────────────────────────────────────────────────────────────

# 暫存檔 / 系統雜訊（預設隱藏）
_HIDDEN_PATTERNS = (
    re.compile(r"^\.DS_Store$"),
    re.compile(r"^Thumbs\.db$"),
    re.compile(r"^~\$.*"),                      # MS Office lock
    re.compile(r"^\.synology.*", re.IGNORECASE),
    re.compile(r"^\.DocumentRevisions.*", re.IGNORECASE),
    re.compile(r"^\._.*"),                       # AppleDouble
    re.compile(r"^.*\.tmp$", re.IGNORECASE),
    re.compile(r"^\.Spotlight.*"),
    re.compile(r"^\.Trashes$"),
    re.compile(r"^\.fseventsd$"),
)

# 上傳禁止副檔名
_BLOCKED_UPLOAD_EXTS = {
    ".exe", ".bat", ".cmd", ".sh", ".ps1", ".scr",
    ".msi", ".app", ".pkg", ".dmg", ".com", ".vbs",
}

_SHARE_STORE_PATH = Path(os.environ.get("MAGI_OSC_FILE_SHARE_STORE", "") or (
    Path(__file__).resolve().parents[2] / ".runtime" / "osc_file_shares.json"
))
_SHARE_PUBLIC_BASE_FILE = Path(os.environ.get("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_FILE", "") or (
    Path(__file__).resolve().parents[2] / ".runtime" / "osc_share_public_base_url.txt"
))
_DEFAULT_SHARE_TTL_SEC = int(os.environ.get("MAGI_OSC_FILE_SHARE_TTL_SEC", str(7 * 24 * 3600)) or str(7 * 24 * 3600))
_MAX_SHARE_TTL_SEC = int(os.environ.get("MAGI_OSC_FILE_SHARE_MAX_TTL_SEC", str(30 * 24 * 3600)) or str(30 * 24 * 3600))


def _load_share_store() -> dict:
    try:
        if _SHARE_STORE_PATH.exists():
            data = json.loads(_SHARE_STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        _log.debug("silent-catch load share store", exc_info=True)
    return {"shares": {}}


def _save_share_store(data: dict) -> None:
    _SHARE_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SHARE_STORE_PATH.with_name(
        f"{_SHARE_STORE_PATH.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_SHARE_STORE_PATH)


def _prune_share_store(data: dict) -> dict:
    now = int(time.time())
    shares = data.setdefault("shares", {})
    for token_hash, row in list(shares.items()):
        try:
            if int(row.get("expires_at") or 0) <= now:
                shares.pop(token_hash, None)
        except Exception:
            shares.pop(token_hash, None)
    return data


def _share_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _share_public_base_url() -> str:
    """Independent public base for shared-file links.

    Deliberately do not fall back to MAGI_PUBLIC_BASE_URL: that value is the
    MAGI/Paperclip console URL, and copying it would disclose the console host.
    """
    env_value = str(os.environ.get("MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if env_value:
        return env_value
    try:
        file_value = _SHARE_PUBLIC_BASE_FILE.read_text(encoding="utf-8").strip().rstrip("/")
        if file_value:
            return file_value
    except Exception:
        _log.debug("silent-catch load share public base", exc_info=True)
    return ""


def _share_url_for_token(token: str) -> tuple[str, str]:
    path = "/s/" + token
    base = _share_public_base_url()
    if base:
        return base + path, "independent_share_base"
    if _env_truthy("MAGI_OSC_FILE_SHARE_ALLOW_CONSOLE_BASE"):
        return request.host_url.rstrip("/") + path, "console_base_explicit"
    return "", "share_public_base_required"


def _resolve_safe_file(path_str: str) -> str:
    local = _osc_resolve_existing_local_path(path_str, prefer_dir=False)
    if not local or not _osc_is_safe_local_path(local) or not os.path.isfile(local):
        return ""
    return local


def _copy_with_system_cp(local_file: str, tmp_path: str) -> bool:
    cp_bin = shutil.which("cp") or "/bin/cp"
    try:
        expected_size = os.path.getsize(local_file)
        result = subprocess.run(
            [cp_bin, "-p", local_file, tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(os.environ.get("PAPERCLIP_FILE_CP_TIMEOUT_SEC", "120") or "120"),
        )
        return result.returncode == 0 and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) == expected_size
    except Exception:
        _log.debug("silent-catch share system cp fallback failed", exc_info=True)
        return False


def _stage_file_with_retry(local_file: str, *, max_attempts: int | None = None) -> str:
    """Stage a NAS-backed file locally before sending or sharing it."""
    if max_attempts is None:
        max_attempts = max(4, int(os.environ.get("PAPERCLIP_FILE_STAGE_MAX_ATTEMPTS", "8") or "8"))
    last_exc: Exception | None = None
    expected_size = os.path.getsize(local_file)
    tmp_dir = os.path.join(tempfile.gettempdir(), "paperclip-shares")
    os.makedirs(tmp_dir, exist_ok=True)
    suffix = os.path.splitext(local_file)[1] or ".bin"
    for attempt in range(max_attempts):
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(prefix="osc-share-", suffix=suffix, dir=tmp_dir)
            try:
                with os.fdopen(fd, "wb") as out, open(local_file, "rb") as src:
                    while True:
                        try:
                            chunk = src.read(4 * 1024 * 1024)
                        except TypeError:
                            chunk = src.read()
                        if not chunk:
                            break
                        out.write(chunk)
            except OSError as e:
                try:
                    os.close(fd)
                except OSError:
                    pass
                if e.errno in (11, 35) and _copy_with_system_cp(local_file, tmp_path):
                    return tmp_path
                raise
            if os.path.getsize(tmp_path) != expected_size:
                raise OSError(
                    f"staged copy incomplete: expected {expected_size} bytes, got {os.path.getsize(tmp_path)} bytes"
                )
            return tmp_path
        except OSError as e:
            last_exc = e
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if e.errno in (11, 35) and attempt < max_attempts - 1:
                time.sleep(0.25 * (2 ** attempt))
                continue
            raise
        except Exception as e:
            last_exc = e
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
    if last_exc:
        raise last_exc
    raise OSError("stage_file_failed")


def _read_file_with_retry(local_file: str, *, max_attempts: int = 7) -> bytes:
    """Read via a local staged copy to avoid macOS SMB EDEADLK while sharing."""
    staged = _stage_file_with_retry(local_file, max_attempts=max_attempts)
    try:
        with open(staged, "rb") as handle:
            return handle.read()
    finally:
        try:
            os.remove(staged)
        except OSError:
            pass


def _cleanup_file_once(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _content_disposition(filename: str, *, inline: bool) -> str:
    disposition = "inline" if inline else "attachment"
    suffix = Path(filename or "").suffix
    if not suffix or not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
        suffix = ".bin"
    ascii_raw = filename.encode("ascii", "ignore").decode("ascii").replace("/", "_").replace("\\", "_")
    ascii_raw = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_raw)
    ascii_suffix = Path(ascii_raw).suffix
    if not ascii_suffix or not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", ascii_suffix):
        ascii_suffix = suffix
    raw_stem = ascii_raw[:-len(ascii_suffix)] if ascii_raw.lower().endswith(ascii_suffix.lower()) else Path(ascii_raw).stem
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw_stem).strip(" .-_")
    ascii_name = (stem or "paperclip") + ascii_suffix
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


def _stream_staged_file(staged_file: str, *, download_name: str, mime: str | None, inline: bool):
    try:
        size = os.path.getsize(staged_file)
    except OSError as e:
        _cleanup_file_once(staged_file)
        return jsonify({"ok": False, "error": f"staged_stat_failed: {e}"}), 503

    start = 0
    end = max(0, size - 1)
    status = 200
    range_header = str(request.headers.get("Range") or "").strip()
    if range_header.startswith("bytes=") and size > 0:
        spec = range_header[6:].split(",", 1)[0].strip()
        try:
            left, _, right = spec.partition("-")
            if left == "":
                suffix_len = int(right)
                if suffix_len <= 0:
                    raise ValueError("invalid suffix range")
                start = max(0, size - suffix_len)
            else:
                start = int(left)
                if right:
                    end = min(size - 1, int(right))
            if start < 0 or start >= size or end < start:
                raise ValueError("invalid byte range")
            status = 206
        except Exception:
            _cleanup_file_once(staged_file)
            resp = Response(status=416)
            resp.headers["Content-Range"] = f"bytes */{size}"
            resp.headers["Accept-Ranges"] = "bytes"
            return resp

    length = 0 if size == 0 else end - start + 1
    if request.method == "HEAD":
        _cleanup_file_once(staged_file)
        resp = Response(status=status, mimetype=mime or "application/octet-stream")
    else:
        def _iter_file():
            try:
                with open(staged_file, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
            finally:
                _cleanup_file_once(staged_file)

        resp = Response(_iter_file(), status=status, mimetype=mime or "application/octet-stream")
        resp.call_on_close(lambda: _cleanup_file_once(staged_file))

    resp.headers["Content-Disposition"] = _content_disposition(download_name, inline=inline)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


def _send_local_file(local: str, *, inline: bool):
    mime, _ = mimetypes.guess_type(local)
    name = os.path.basename(local)
    if request.method == "HEAD":
        try:
            st = os.stat(local)
        except OSError as e:
            return jsonify({"ok": False, "error": f"stat_failed: {e}"}), 503
        resp = Response(status=200, mimetype=mime or "application/octet-stream")
        resp.headers["Content-Disposition"] = _content_disposition(name, inline=inline)
        resp.headers["Content-Length"] = str(int(st.st_size))
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "private, max-age=300"
        return resp
    staged_file = ""
    try:
        staged_file = _stage_file_with_retry(local)
    except OSError as e:
        _log.warning("share stage failed: errno=%s file=%s", getattr(e, "errno", None), local)
        return jsonify({"ok": False, "error": f"read_failed: {e}"}), 503
    return _stream_staged_file(staged_file, mime=mime or "application/octet-stream", inline=inline, download_name=name)


def _is_hidden_name(name: str) -> bool:
    return any(p.match(name) for p in _HIDDEN_PATTERNS)


def _resolve_target_dir(path_str: str) -> str:
    """Resolve a possibly-Windows path to a local existing directory under allowed roots.

    若首次解析失敗（路徑指向 NAS 但 share 未掛載），自動觸發 ensure_nas_mounts()
    嘗試掛載一次再 retry。常見情境：結案案件在 lumi share，平時不掛、律師點開
    才需要 mount-on-demand。
    """
    real = _osc_resolve_existing_local_path(path_str, prefer_dir=True)
    if real:
        return real
    # 第二次機會：嘗試 mount NAS 後 retry
    try:
        from api.nas_mount_guard import ensure_nas_mounts
        import logging as _lg
        _lg.getLogger(__name__).info("[file_manager] _resolve_target_dir miss, triggering ensure_nas_mounts() for: %s", path_str)
        ensure_nas_mounts()
    except Exception as e:
        import logging as _lg
        _lg.getLogger(__name__).warning("[file_manager] ensure_nas_mounts failed: %s", e)
    real = _osc_resolve_existing_local_path(path_str, prefer_dir=True)
    return real or ""


def _resolve_with_diagnostic(path_str: str) -> tuple[str, dict]:
    """Resolve + 回傳診斷資訊（candidate 列表 + 各 candidate 是否存在），供前端錯誤訊息用。"""
    real = _resolve_target_dir(path_str)
    diag = {"input_path": path_str, "resolved": real or None}
    if not real:
        try:
            from api.osc.utils import _osc_local_path_candidates
            cands = _osc_local_path_candidates(path_str)
            diag["candidates"] = [
                {"path": c, "exists": os.path.isdir(c)}
                for c in cands
            ]
        except Exception:
            diag["candidates"] = []
    return real, diag


def _safe_join_under(base_real: str, relative_path: str) -> str | None:
    """Join base + rel and verify the result remains under base_real (no traversal)."""
    target = os.path.realpath(os.path.join(base_real, relative_path or ""))
    if target != base_real and not target.startswith(base_real + os.sep):
        return None
    return target


def _summarize_dir(dir_path: str, *, max_scan: int = 200) -> dict:
    """Quick summary: child file count + total size (capped to avoid hammering NAS)."""
    files = 0
    folders = 0
    total = 0
    try:
        for i, name in enumerate(os.listdir(dir_path)):
            if i >= max_scan:
                break
            if _is_hidden_name(name):
                continue
            full = os.path.join(dir_path, name)
            try:
                if os.path.isdir(full):
                    folders += 1
                else:
                    st = os.stat(full)
                    files += 1
                    total += int(st.st_size)
            except OSError:
                continue
    except OSError:
        pass
    return {"file_count": files, "folder_count": folders, "total_size": total}


def _entry_dict(name: str, full_path: str, base_real: str, *, summarize: bool) -> dict | None:
    try:
        is_dir = os.path.isdir(full_path)
        st = os.stat(full_path)
    except OSError:
        return None
    rel = _osc_relpath_under(base_real, full_path)
    ext = "" if is_dir else os.path.splitext(name)[1].lower()
    entry = {
        "name": name,
        "relative_path": rel,
        "type": "dir" if is_dir else "file",
        "ext": ext,
        "size": None if is_dir else int(st.st_size),
        "size_label": "" if is_dir else _osc_human_size(int(st.st_size)),
        "modified_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "mtime_ts": int(st.st_mtime),
        "hidden": _is_hidden_name(name),
    }
    if is_dir and summarize:
        s = _summarize_dir(full_path)
        entry["child_files"] = s["file_count"]
        entry["child_folders"] = s["folder_count"]
        entry["child_total_size"] = s["total_size"]
        entry["child_size_label"] = _osc_human_size(s["total_size"])
    return entry


# ── routes ──────────────────────────────────────────────────────────────


def _root_child_dirs(path_str: str, *, limit: int = 240) -> list[dict]:
    base_real = _resolve_target_dir(path_str)
    if not base_real:
        return []
    children: list[dict] = []
    try:
        for name in sorted(os.listdir(base_real), key=str.lower):
            if _is_hidden_name(name):
                continue
            full = os.path.join(base_real, name)
            if not os.path.isdir(full):
                continue
            has_subdirs = False
            try:
                for sub in os.listdir(full):
                    if _is_hidden_name(sub):
                        continue
                    if os.path.isdir(os.path.join(full, sub)):
                        has_subdirs = True
                        break
            except OSError:
                pass
            children.append({
                "name": name,
                "relative_path": _osc_relpath_under(base_real, full),
                "has_subdirs": has_subdirs,
            })
            if len(children) >= limit:
                break
    except OSError:
        return []
    return children


@osc_files_bp.route("/api/osc/folders/roots", methods=["GET"])
@login_required
def osc_folder_roots_api():
    """Return the two business-facing case folder roots for the file manager."""
    try:
        from api.case_path_mapper import default_case_roots, preferred_case_roots
        roots = preferred_case_roots(include_closed=True) or default_case_roots(include_closed=True)
    except Exception:
        roots = []
    active = roots[0] if roots else ""
    closed = roots[1] if len(roots) > 1 else ""
    items = [
        {
            "id": "active",
            "label": "進行中案件",
            "folder_name": "01_案件",
            "path": active,
            "hint": "依案件種類分類的目前案件資料夾",
        },
        {
            "id": "closed",
            "label": "已結案案件",
            "folder_name": "03_工作資料 / 10_結案",
            "path": closed,
            "hint": "已結案或歸檔案件資料夾",
        },
    ]
    for item in items:
        local = _resolve_target_dir(item["path"]) if item["path"] else ""
        item["local_path"] = local
        item["exists"] = bool(local)
        item["children"] = _root_child_dirs(item["path"]) if item["path"] else []
    return jsonify({"ok": True, "items": items})


_CHUNK_TMP_DIR = Path(os.path.expanduser("~/.cache/paperclip-uploads"))
_CHUNK_SESSION_TTL_SEC = 3600  # 1 hour
_MAX_UPLOAD_BYTES_PER_FILE = 1 * 1024 * 1024 * 1024  # 1 GB cap (chunked enabled)
_MAX_MULTI_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB per multi-upload request


def _check_upload_ext(filename: str) -> tuple[bool, str]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _BLOCKED_UPLOAD_EXTS:
        return False, f"blocked_extension:{ext}"
    return True, ""


# Magic-byte signatures for executables — block even if extension is renamed.
# (commit 13: harden against `disguised_exe.pdf` style renaming)
_EXEC_MAGIC_SIGS = (
    b"MZ",                # Windows PE / DOS
    b"\x7fELF",           # Linux ELF
    b"\xca\xfe\xba\xbe",  # Mach-O fat binary / Java class
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit LE
    b"\xfe\xed\xfa\xce",  # Mach-O 32-bit BE
    b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit BE
    b"#!",                # Shell script shebang
)


def _sniff_executable(path: str) -> str | None:
    """Return a short label if the file's first bytes look executable; else None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    for sig in _EXEC_MAGIC_SIGS:
        if head.startswith(sig):
            return "executable_signature:" + sig.hex()
    return None


def _cleanup_stale_chunk_sessions():
    if not _CHUNK_TMP_DIR.exists():
        return
    now = datetime.now().timestamp()
    for session_dir in _CHUNK_TMP_DIR.iterdir():
        try:
            if not session_dir.is_dir():
                continue
            if now - session_dir.stat().st_mtime > _CHUNK_SESSION_TTL_SEC:
                shutil.rmtree(session_dir, ignore_errors=True)
        except OSError:
            continue


@osc_files_bp.route("/api/osc/files/upload-multi", methods=["POST"])
@login_required
def osc_files_upload_multi_api():
    """
    Multi-file upload (multipart/form-data with files[]).
    Form fields:
      base_path     : NAS root
      relative_path : sub-folder under base (default root)
      overwrite     : "1" to overwrite (default fail on conflict per file)
      files         : multiple file fields
    Returns: per-file results array (some may succeed, some fail).
    """
    base = str(request.form.get("base_path") or "").strip()
    relative = str(request.form.get("relative_path") or "").strip().strip("/")
    overwrite = str(request.form.get("overwrite") or "").strip().lower() in {"1", "true", "yes"}
    if not base:
        return jsonify({"ok": False, "error": "base_path required"}), 400

    base_real = _resolve_target_dir(base)
    if not base_real:
        return jsonify({"ok": False, "error": "base_not_found_or_not_allowed"}), 404
    target = _safe_join_under(base_real, relative)
    if target is None or not _osc_is_safe_local_path(target) or not os.path.isdir(target):
        return jsonify({"ok": False, "error": "target_dir_not_found"}), 404

    uploads = request.files.getlist("files") or request.files.getlist("file")
    if not uploads:
        return jsonify({"ok": False, "error": "files required"}), 400

    results = []
    total_saved = 0
    for up in uploads:
        name = os.path.basename(str(up.filename or "").strip())
        if not name:
            results.append({"ok": False, "error": "empty_filename"})
            continue
        ok, ext_err = _check_upload_ext(name)
        if not ok:
            results.append({"ok": False, "name": name, "error": ext_err})
            continue
        dest = os.path.join(target, name)
        if os.path.exists(dest) and not overwrite:
            results.append({"ok": False, "name": name, "error": "file_exists", "path": dest})
            continue
        try:
            up.save(dest)
            sz = os.path.getsize(dest)
        except OSError as e:
            results.append({"ok": False, "name": name, "error": f"save_failed: {e}"})
            continue
        # Magic-byte sniff: catch executables renamed to allowed extensions.
        sniff = _sniff_executable(dest)
        if sniff:
            try:
                os.remove(dest)
            except OSError:
                pass
            results.append({"ok": False, "name": name, "error": "blocked_content_signature",
                            "detail": sniff})
            continue
        if sz > _MAX_UPLOAD_BYTES_PER_FILE:
            os.remove(dest)
            results.append({"ok": False, "name": name, "error": "file_too_large",
                            "size_mb": round(sz / 1024 / 1024, 1),
                            "limit_mb": _MAX_UPLOAD_BYTES_PER_FILE // 1024 // 1024})
            continue
        total_saved += sz
        if total_saved > _MAX_MULTI_TOTAL_BYTES:
            os.remove(dest)
            results.append({"ok": False, "name": name, "error": "total_too_large",
                            "limit_mb": _MAX_MULTI_TOTAL_BYTES // 1024 // 1024})
            break
        results.append({"ok": True, "name": name, "path": dest, "size": sz,
                        "size_label": _osc_human_size(sz),
                        "relative_path": _osc_relpath_under(base_real, dest)})

    succeeded = sum(1 for r in results if r.get("ok"))
    return jsonify({
        "ok": succeeded > 0,
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
        "target_dir": target,
    })


@osc_files_bp.route("/api/osc/files/upload-chunked", methods=["POST"])
@login_required
def osc_files_upload_chunked_api():
    """
    Chunked upload protocol (for files >10 MB).
    Form fields per request:
      session_id    : client-generated unique id (e.g. uuid)
      chunk_index   : 0-based int
      total_chunks  : int
      filename      : final filename
      base_path     : NAS root (required on every chunk; final chunk also writes file)
      relative_path : sub-folder
      overwrite     : "1"
      chunk         : the binary chunk data
    Behavior:
      - Each chunk written to ~/.cache/paperclip-uploads/<session_id>/<index>.part
      - On chunk_index == total_chunks - 1, concatenate all parts → write to final dest, cleanup session
      - Returns {ok, chunk_index, received, finalized?, path?}
    """
    _cleanup_stale_chunk_sessions()

    session_id = str(request.form.get("session_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", session_id):
        return jsonify({"ok": False, "error": "session_id_invalid"}), 400
    try:
        chunk_index = int(request.form.get("chunk_index") or "0")
        total_chunks = int(request.form.get("total_chunks") or "0")
    except ValueError:
        return jsonify({"ok": False, "error": "chunk_index/total_chunks invalid"}), 400
    if chunk_index < 0 or total_chunks <= 0 or chunk_index >= total_chunks:
        return jsonify({"ok": False, "error": "chunk range invalid"}), 400

    filename = os.path.basename(str(request.form.get("filename") or "").strip())
    if not filename:
        return jsonify({"ok": False, "error": "filename required"}), 400
    ok, ext_err = _check_upload_ext(filename)
    if not ok:
        return jsonify({"ok": False, "error": ext_err}), 400

    chunk_file = request.files.get("chunk")
    if chunk_file is None:
        return jsonify({"ok": False, "error": "chunk required"}), 400

    session_dir = _CHUNK_TMP_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    part_path = session_dir / f"{chunk_index:06d}.part"
    chunk_file.save(str(part_path))

    if chunk_index < total_chunks - 1:
        return jsonify({
            "ok": True,
            "session_id": session_id,
            "chunk_index": chunk_index,
            "received": True,
            "finalized": False,
        })

    # Last chunk → finalize
    base = str(request.form.get("base_path") or "").strip()
    relative = str(request.form.get("relative_path") or "").strip().strip("/")
    overwrite = str(request.form.get("overwrite") or "").strip().lower() in {"1", "true", "yes"}
    base_real = _resolve_target_dir(base) if base else ""
    if not base_real:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": "base_not_found_or_not_allowed"}), 404
    target = _safe_join_under(base_real, relative)
    if target is None or not _osc_is_safe_local_path(target) or not os.path.isdir(target):
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": "target_dir_not_found"}), 404
    dest = os.path.join(target, filename)
    if os.path.exists(dest) and not overwrite:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": "file_exists", "path": dest}), 409

    # Verify all parts present
    missing = []
    for i in range(total_chunks):
        if not (session_dir / f"{i:06d}.part").exists():
            missing.append(i)
    if missing:
        return jsonify({"ok": False, "error": "chunks_missing", "missing": missing}), 400

    try:
        with open(dest, "wb") as out:
            for i in range(total_chunks):
                with open(session_dir / f"{i:06d}.part", "rb") as f:
                    shutil.copyfileobj(f, out, length=4 * 1024 * 1024)
        sz = os.path.getsize(dest)
        if sz > _MAX_UPLOAD_BYTES_PER_FILE:
            os.remove(dest)
            return jsonify({"ok": False, "error": "file_too_large", "size_mb": round(sz / 1024 / 1024, 1)}), 413
        sniff = _sniff_executable(dest)
        if sniff:
            try:
                os.remove(dest)
            except OSError:
                pass
            return jsonify({"ok": False, "error": "blocked_content_signature", "detail": sniff}), 415
    except OSError as e:
        return jsonify({"ok": False, "error": f"finalize_failed: {e}"}), 500
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)

    return jsonify({
        "ok": True,
        "session_id": session_id,
        "finalized": True,
        "path": dest,
        "size": sz,
        "size_label": _osc_human_size(sz),
        "relative_path": _osc_relpath_under(base_real, dest),
    })


_INVALID_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def _validate_filename(name: str) -> tuple[bool, str]:
    n = (name or "").strip()
    if not n:
        return False, "name_empty"
    if n in (".", ".."):
        return False, "name_invalid"
    if _INVALID_NAME_RE.search(n):
        return False, "name_has_invalid_chars"
    if len(n) > 200:
        return False, "name_too_long"
    return True, ""


@osc_files_bp.route("/api/osc/folders/mkdir", methods=["POST"])
@login_required
def osc_folders_mkdir_api():
    payload = request.get_json(silent=True) or {}
    base = str(payload.get("base_path") or "").strip()
    relative = str(payload.get("relative_path") or "").strip().strip("/")
    new_name = str(payload.get("name") or "").strip()
    if not base:
        return jsonify({"ok": False, "error": "base_path required"}), 400
    ok, err = _validate_filename(new_name)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400

    base_real = _resolve_target_dir(base)
    if not base_real:
        return jsonify({"ok": False, "error": "base_not_found_or_not_allowed"}), 404
    parent = _safe_join_under(base_real, relative)
    if parent is None or not _osc_is_safe_local_path(parent) or not os.path.isdir(parent):
        return jsonify({"ok": False, "error": "parent_not_found"}), 404

    target = os.path.join(parent, new_name)
    if os.path.exists(target):
        return jsonify({"ok": False, "error": "already_exists"}), 409
    try:
        os.makedirs(target, exist_ok=False)
    except OSError as e:
        return jsonify({"ok": False, "error": f"mkdir_failed: {e}"}), 500
    return jsonify({
        "ok": True,
        "created_path": target,
        "relative_path": _osc_relpath_under(base_real, target),
    })


@osc_files_bp.route("/api/osc/folders/rename", methods=["POST"])
@login_required
def osc_folders_rename_api():
    payload = request.get_json(silent=True) or {}
    base = str(payload.get("base_path") or "").strip()
    relative = str(payload.get("relative_path") or "").strip().strip("/")
    new_name = str(payload.get("new_name") or "").strip()
    if not base:
        return jsonify({"ok": False, "error": "base_path required"}), 400
    if not relative:
        return jsonify({"ok": False, "error": "relative_path required"}), 400
    ok, err = _validate_filename(new_name)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400

    base_real = _resolve_target_dir(base)
    if not base_real:
        return jsonify({"ok": False, "error": "base_not_found_or_not_allowed"}), 404
    src = _safe_join_under(base_real, relative)
    if src is None or not _osc_is_safe_local_path(src) or not os.path.exists(src):
        return jsonify({"ok": False, "error": "source_not_found"}), 404
    dst = os.path.join(os.path.dirname(src), new_name)
    if os.path.exists(dst):
        return jsonify({"ok": False, "error": "target_exists"}), 409
    try:
        os.rename(src, dst)
    except OSError as e:
        return jsonify({"ok": False, "error": f"rename_failed: {e}"}), 500
    return jsonify({
        "ok": True,
        "new_path": dst,
        "new_relative_path": _osc_relpath_under(base_real, dst),
    })


@osc_files_bp.route("/api/osc/folders/move", methods=["POST"])
@login_required
def osc_folders_move_api():
    """
    Move file or folder to a new location under the same base.
    Special: target_relative_path == ".trash" → moved to <base>/.trash/<name>_<ts>
    (per CLAUDE.md prohibited_actions: never permanent delete, always recycle).
    """
    payload = request.get_json(silent=True) or {}
    base = str(payload.get("base_path") or "").strip()
    src_rel = str(payload.get("source_relative_path") or "").strip().strip("/")
    dst_raw = payload.get("target_relative_path")
    dst_rel = str(dst_raw or "").strip().strip("/")
    to_trash = bool(payload.get("to_trash"))
    if not base:
        return jsonify({"ok": False, "error": "base_path required"}), 400
    if not src_rel:
        return jsonify({"ok": False, "error": "source_relative_path required"}), 400
    if not to_trash and dst_raw is None:
        return jsonify({"ok": False, "error": "target_relative_path or to_trash required"}), 400

    base_real = _resolve_target_dir(base)
    if not base_real:
        return jsonify({"ok": False, "error": "base_not_found_or_not_allowed"}), 404
    src = _safe_join_under(base_real, src_rel)
    if src is None or not _osc_is_safe_local_path(src) or not os.path.exists(src):
        return jsonify({"ok": False, "error": "source_not_found"}), 404

    src_name = os.path.basename(src)

    if to_trash:
        trash_dir = os.path.join(base_real, ".trash")
        os.makedirs(trash_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_name = f"{os.path.splitext(src_name)[0]}_{ts}{os.path.splitext(src_name)[1]}"
        dst = os.path.join(trash_dir, new_name)
    else:
        target_parent = _safe_join_under(base_real, dst_rel)
        if target_parent is None or not _osc_is_safe_local_path(target_parent) or not os.path.isdir(target_parent):
            return jsonify({"ok": False, "error": "target_dir_not_found"}), 404
        dst = os.path.join(target_parent, src_name)
        if os.path.exists(dst):
            return jsonify({"ok": False, "error": "target_exists"}), 409

    try:
        shutil.move(src, dst)
    except OSError as e:
        return jsonify({"ok": False, "error": f"move_failed: {e}"}), 500

    return jsonify({
        "ok": True,
        "new_path": dst,
        "new_relative_path": _osc_relpath_under(base_real, dst),
        "to_trash": to_trash,
    })


@osc_files_bp.route("/api/osc/folders/tree", methods=["GET"])
@login_required
def osc_folders_tree_api():
    """
    Lazy-load tree node children — sub-directories only (files excluded for tree).
    Args:
        base_path : root path
        relative_path : sub-path under base whose children we list (default: root itself)
        show_hidden : "1" to include暫存資料夾
    """
    base = str(request.args.get("base_path") or "").strip()
    relative = str(request.args.get("relative_path") or "").strip().strip("/")
    show_hidden = str(request.args.get("show_hidden") or "").strip().lower() in {"1", "true", "yes"}

    if not base:
        return jsonify({"ok": False, "error": "base_path required"}), 400

    base_real, diag = _resolve_with_diagnostic(base)
    if not base_real:
        return jsonify({
            "ok": False,
            "error": "base_not_found_or_not_allowed",
            "message": "找不到此資料夾，可能 NAS 尚未掛載、案件已歸檔到其他位置、或路徑拼寫有誤",
            "diagnostic": diag,
        }), 404

    target = _safe_join_under(base_real, relative)
    if target is None:
        return jsonify({"ok": False, "error": "path_escape"}), 400
    if not _osc_is_safe_local_path(target):
        return jsonify({"ok": False, "error": "path_not_allowed"}), 403
    if not os.path.isdir(target):
        return jsonify({"ok": False, "error": "folder_not_found"}), 404

    children = []
    try:
        for name in sorted(os.listdir(target), key=str.lower):
            if _is_hidden_name(name) and not show_hidden:
                continue
            full = os.path.join(target, name)
            try:
                if not os.path.isdir(full):
                    continue
            except OSError:
                continue
            # detect grandchild dirs to know if expandable
            has_subdirs = False
            try:
                for sub in os.listdir(full):
                    if _is_hidden_name(sub) and not show_hidden:
                        continue
                    if os.path.isdir(os.path.join(full, sub)):
                        has_subdirs = True
                        break
            except OSError:
                pass
            children.append({
                "name": name,
                "relative_path": _osc_relpath_under(base_real, full),
                "has_subdirs": has_subdirs,
            })
    except OSError as e:
        return jsonify({"ok": False, "error": f"listdir_failed: {e}"}), 500

    return jsonify({
        "ok": True,
        "base_path": base_real,
        "current_relative_path": _osc_relpath_under(base_real, target),
        "children": children,
    })


@osc_files_bp.route("/api/osc/files/preview", methods=["GET"])
@login_required
def osc_files_preview_api():
    """
    Unified preview dispatcher.
    Args:
        path : the file path (will be resolved against allowed roots)
    Behavior:
        - PDF / image / audio / video / text → 302 redirect to /api/osc/files/content?inline=1
          (browser handles natively or Phase 2 modal)
        - Office → convert to PDF → send_file the cached PDF inline
        - HEIC → sips → JPEG → send_file inline
        - CSV / Email / ZIP / Hex → return JSON
    """
    raw = str(request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "path required"}), 400
    local = _osc_resolve_existing_local_path(raw, prefer_dir=False)
    if not local:
        return jsonify({"ok": False, "error": "file_not_found"}), 404
    if not _osc_is_safe_local_path(local):
        return jsonify({"ok": False, "error": "path_not_allowed"}), 403

    kind = osc_preview.categorize(local)

    if kind in ("pdf", "image", "audio", "video", "text"):
        encoded_path = quote(raw, safe="")
        return jsonify({
            "ok": True, "kind": kind,
            "content_url": f"/api/osc/files/content?path={encoded_path}&inline=1",
            "name": os.path.basename(local),
        })

    if kind == "office":
        cached = osc_preview.preview_office_to_pdf(local)
        if not cached:
            return jsonify({"ok": False, "kind": "office", "error": "office_convert_failed",
                            "fallback": "download"}), 500
        return send_file(cached, mimetype="application/pdf", as_attachment=False,
                         download_name=os.path.splitext(os.path.basename(local))[0] + ".pdf")

    if kind == "heic":
        cached = osc_preview.preview_heic_to_jpg(local)
        if not cached:
            return jsonify({"ok": False, "kind": "heic", "error": "heic_convert_failed",
                            "fallback": "download"}), 500
        return send_file(cached, mimetype="image/jpeg", as_attachment=False,
                         download_name=os.path.splitext(os.path.basename(local))[0] + ".jpg")

    if kind == "csv":
        result = osc_preview.preview_csv_to_rows(local)
        result["kind"] = "csv"
        return jsonify(result)

    if kind == "email":
        result = osc_preview.preview_email(local)
        result["kind"] = "email"
        return jsonify(result)

    if kind == "zip":
        result = osc_preview.preview_zip(local)
        result["kind"] = "zip"
        return jsonify(result)

    # other → hex dump
    result = osc_preview.preview_hex_dump(local)
    result["kind"] = "other"
    result["mime"], _ = mimetypes.guess_type(local)
    result["ext"] = os.path.splitext(local)[1].lower()
    return jsonify(result)


@osc_files_bp.route("/api/osc/files/info", methods=["GET"])
@login_required
def osc_files_info_api():
    """File metadata (no content): name, size, mtime, mime, kind."""
    raw = str(request.args.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "path required"}), 400
    local = _osc_resolve_existing_local_path(raw, prefer_dir=False)
    if not local:
        return jsonify({"ok": False, "error": "file_not_found"}), 404
    if not _osc_is_safe_local_path(local):
        return jsonify({"ok": False, "error": "path_not_allowed"}), 403
    try:
        st = os.stat(local)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    mime, _ = mimetypes.guess_type(local)
    return jsonify({
        "ok": True,
        "name": os.path.basename(local),
        "ext": os.path.splitext(local)[1].lower(),
        "size": st.st_size,
        "size_label": _osc_human_size(st.st_size),
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "mime": mime or "application/octet-stream",
        "kind": osc_preview.categorize(local),
        "local_path": local,
    })


@osc_files_bp.route("/api/osc/files/share", methods=["POST"])
@login_required
def osc_files_share_create_api():
    """Create an opaque public token for one file.

    The public URL intentionally contains only a random token, never the NAS path,
    case folder, filename-derived slug, or OSC route name.
    """
    payload = request.get_json(silent=True) or {}
    raw = str(payload.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "path required"}), 400
    local = _resolve_safe_file(raw)
    if not local:
        return jsonify({"ok": False, "error": "file_not_found_or_not_allowed"}), 404
    try:
        ttl = int(payload.get("ttl_sec") or _DEFAULT_SHARE_TTL_SEC)
    except Exception:
        ttl = _DEFAULT_SHARE_TTL_SEC
    ttl = max(300, min(ttl, _MAX_SHARE_TTL_SEC))
    token = secrets.token_urlsafe(32)
    token_hash = _share_token_hash(token)
    now = int(time.time())
    try:
        st = os.stat(local)
    except OSError as e:
        return jsonify({"ok": False, "error": f"stat_failed: {e}"}), 500
    public_url, url_mode = _share_url_for_token(token)
    if not public_url:
        return jsonify({
            "ok": False,
            "error": "share_public_base_required",
            "message": "為避免分享連結洩漏 MAGI/Paperclip 主控台外網網址，請先設定獨立分享入口 MAGI_OSC_FILE_SHARE_PUBLIC_BASE_URL。",
        }), 409
    data = _prune_share_store(_load_share_store())
    data.setdefault("shares", {})[token_hash] = {
        "path": local,
        "name": os.path.basename(local),
        "size": int(st.st_size),
        "created_at": now,
        "expires_at": now + ttl,
        "created_by": str(getattr(current_user, "id", "") or ""),
        "downloads": 0,
    }
    _save_share_store(data)
    return jsonify({
        "ok": True,
        "url": public_url,
        "url_mode": url_mode,
        "expires_at": datetime.fromtimestamp(now + ttl).isoformat(timespec="seconds"),
        "name": os.path.basename(local),
        "size": int(st.st_size),
        "size_label": _osc_human_size(int(st.st_size)),
    })


@osc_files_bp.route("/s/<token>", methods=["GET"])
def osc_files_public_share_api(token):
    """Serve a shared file by opaque token; does not require login."""
    t = str(token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{24,128}", t):
        return jsonify({"ok": False, "error": "not_found"}), 404
    data = _prune_share_store(_load_share_store())
    row = data.get("shares", {}).get(_share_token_hash(t))
    if not isinstance(row, dict):
        _save_share_store(data)
        return jsonify({"ok": False, "error": "not_found"}), 404
    local = _resolve_safe_file(str(row.get("path") or ""))
    if not local:
        data.get("shares", {}).pop(_share_token_hash(t), None)
        _save_share_store(data)
        return jsonify({"ok": False, "error": "not_found"}), 404
    row["downloads"] = int(row.get("downloads") or 0) + 1
    row["last_accessed_at"] = int(time.time())
    _save_share_store(data)
    inline = str(request.args.get("inline") or "").strip().lower() in {"1", "true", "yes"}
    return _send_local_file(local, inline=inline)


@osc_files_bp.route("/api/osc/folders/browse", methods=["GET"])
@login_required
def osc_folders_browse_api():
    """
    List entries under a base path + optional relative path.
    Args:
        base_path  : NAS/Synology path of root (e.g. case folder_path)
        relative_path : sub-path under base
        show_hidden : "1" to include暫存檔 (default hidden)
        summarize_dirs : "1" (default) to compute child file count + size
    """
    base = str(request.args.get("base_path") or request.args.get("path") or "").strip()
    relative = str(request.args.get("relative_path") or "").strip().strip("/")
    show_hidden = str(request.args.get("show_hidden") or "").strip().lower() in {"1", "true", "yes"}
    summarize = str(request.args.get("summarize_dirs") or "1").strip().lower() in {"1", "true", "yes"}

    if not base:
        return jsonify({"ok": False, "error": "base_path required"}), 400

    base_real, diag = _resolve_with_diagnostic(base)
    if not base_real:
        return jsonify({
            "ok": False,
            "error": "base_not_found_or_not_allowed",
            "message": "找不到此資料夾，可能 NAS 尚未掛載、案件已歸檔到其他位置、或路徑拼寫有誤",
            "diagnostic": diag,
        }), 404

    target = _safe_join_under(base_real, relative)
    if target is None:
        return jsonify({"ok": False, "error": "path_escape"}), 400
    if not _osc_is_safe_local_path(target):
        return jsonify({"ok": False, "error": "path_not_allowed"}), 403
    if not os.path.isdir(target):
        return jsonify({"ok": False, "error": "folder_not_found"}), 404

    try:
        names = os.listdir(target)
    except OSError as e:
        return jsonify({"ok": False, "error": f"listdir_failed: {e}"}), 500

    folders, files = [], []
    hidden_count = 0
    for name in names:
        if _is_hidden_name(name):
            hidden_count += 1
            if not show_hidden:
                continue
        full = os.path.join(target, name)
        entry = _entry_dict(name, full, base_real, summarize=summarize)
        if entry is None:
            continue
        if entry["type"] == "dir":
            folders.append(entry)
        else:
            files.append(entry)

    folders.sort(key=lambda e: e["name"].lower())
    files.sort(key=lambda e: e["mtime_ts"], reverse=True)

    parent_relative = ""
    if target != base_real:
        parent_relative = _osc_relpath_under(base_real, os.path.dirname(target))

    return jsonify({
        "ok": True,
        "base_path": base_real,
        "current_path": target,
        "current_relative_path": _osc_relpath_under(base_real, target),
        "parent_relative_path": parent_relative,
        "folders": folders,
        "files": files,
        "hidden_count": hidden_count,
        "show_hidden": show_hidden,
    })
