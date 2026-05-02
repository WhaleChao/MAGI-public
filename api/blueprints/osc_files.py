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
import shutil
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, send_file
from flask_login import login_required

from api.osc.utils import (
    _osc_is_safe_local_path,
    _osc_resolve_existing_local_path,
    _osc_local_path_candidates,
    _osc_norm_path,
    _osc_relpath_under,
    _osc_human_size,
)

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


def _is_hidden_name(name: str) -> bool:
    return any(p.match(name) for p in _HIDDEN_PATTERNS)


def _resolve_target_dir(path_str: str) -> str:
    """Resolve a possibly-Windows path to a local existing directory under allowed roots."""
    real = _osc_resolve_existing_local_path(path_str, prefer_dir=True)
    return real or ""


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


_CHUNK_TMP_DIR = Path(os.path.expanduser("~/.cache/paperclip-uploads"))
_CHUNK_SESSION_TTL_SEC = 3600  # 1 hour
_MAX_UPLOAD_BYTES_PER_FILE = 1 * 1024 * 1024 * 1024  # 1 GB cap (chunked enabled)
_MAX_MULTI_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB per multi-upload request


def _check_upload_ext(filename: str) -> tuple[bool, str]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _BLOCKED_UPLOAD_EXTS:
        return False, f"blocked_extension:{ext}"
    return True, ""


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
    dst_rel = str(payload.get("target_relative_path") or "").strip().strip("/")
    to_trash = bool(payload.get("to_trash"))
    if not base:
        return jsonify({"ok": False, "error": "base_path required"}), 400
    if not src_rel:
        return jsonify({"ok": False, "error": "source_relative_path required"}), 400
    if not to_trash and not dst_rel:
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

    base_real = _resolve_target_dir(base)
    if not base_real:
        return jsonify({"ok": False, "error": "base_not_found_or_not_allowed"}), 404

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

    base_real = _resolve_target_dir(base)
    if not base_real:
        return jsonify({"ok": False, "error": "base_not_found_or_not_allowed"}), 404

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
