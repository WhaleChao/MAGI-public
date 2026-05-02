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
