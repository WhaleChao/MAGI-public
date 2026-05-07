"""Golem-inspired operational console APIs for MAGI."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from api.blueprints.web_runtime import _collect_process_monitor


golem_console_bp = Blueprint("golem_console", __name__)

_MAGI_ROOT = Path(__file__).resolve().parents[2]
_STATIC_DIR = _MAGI_ROOT / "static"
_EXPORTS_DIR = _STATIC_DIR / "exports"
_AGENT_DIR = _MAGI_ROOT / ".agent"
_SKILLS_DEFINITIONS = _MAGI_ROOT / "skills" / "definitions.json"
_GUARDIAN_STATE = _STATIC_DIR / "process_guardian_state.json"
_ENV_PATH = _MAGI_ROOT / ".env"
_MANAGED_API_KEYS = {
    "nvidia_nim": {
        "label": "NVIDIA NIM",
        "env_key": "NVIDIA_NIM_API_KEY",
        "enable_key": "NVIDIA_NIM_ENABLE",
        "prefix": "nvapi-",
    },
}


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _is_admin_user() -> bool:
    try:
        checker = getattr(current_user, "is_admin", None)
        if callable(checker):
            return bool(checker())
        return str(getattr(current_user, "role", "") or "").lower() == "admin"
    except Exception:
        return False


def _mask_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 12:
        return "*" * len(text)
    return f"{text[:8]}...{text[-4:]}"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _write_env_values(path: Path, updates: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        original = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        original = []
    backup = path.with_suffix(path.suffix + f".bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    if path.exists():
        shutil.copy2(path, backup)
    else:
        backup.write_text("", encoding="utf-8")

    seen: set[str] = set()
    out: list[str] = []
    for raw in original:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(raw)
    missing = [key for key in updates if key not in seen]
    if missing and out and out[-1].strip():
        out.append("")
    for key in missing:
        out.append(f"{key}={updates[key]}")

    fd, tmp_name = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(out).rstrip() + "\n")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return backup


def _api_key_status() -> dict[str, Any]:
    env_values = _parse_env_file(_ENV_PATH)
    items = []
    for key, meta in _MANAGED_API_KEYS.items():
        env_key = meta["env_key"]
        enable_key = meta.get("enable_key", "")
        value = os.environ.get(env_key) or env_values.get(env_key, "")
        enabled_raw = os.environ.get(enable_key) or env_values.get(enable_key, "") if enable_key else ""
        items.append(
            {
                "id": key,
                "label": meta["label"],
                "env_key": env_key,
                "enable_key": enable_key,
                "configured": bool(value),
                "masked": _mask_secret(value),
                "enabled": str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"},
                "updated_from": str(_ENV_PATH),
            }
        )
    return {"ok": True, "items": items}


def _tail(path: Path, limit: int = 80) -> list[str]:
    try:
        if not path.exists() or not path.is_file():
            return []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-limit:]
        return [line.rstrip("\n") for line in lines if line.strip()]
    except Exception as exc:
        return [f"[read-error] {path.name}: {exc}"]


def _recent_files(directory: Path, *, limit: int = 8, prefix: str = "") -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for item in directory.iterdir():
        if not item.is_file():
            continue
        if prefix and not item.name.startswith(prefix):
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        rows.append(
            {
                "name": item.name,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "url": f"/static/exports/{item.name}",
            }
        )
    return sorted(rows, key=lambda row: row["mtime"], reverse=True)[:limit]


def _skill_rows(limit: int = 18) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = _read_json(_SKILLS_DEFINITIONS, {})
    tools = data.get("tools") if isinstance(data, dict) else []
    if not isinstance(tools, list):
        tools = []
    rows: list[dict[str, Any]] = []
    for tool in tools[:limit]:
        if not isinstance(tool, dict):
            continue
        rows.append(
            {
                "name": tool.get("name") or "(unnamed)",
                "sage": tool.get("sage") or "shared",
                "method": tool.get("method") or "POST",
                "endpoint": tool.get("endpoint") or "",
                "description": tool.get("description") or "",
            }
        )
    return rows, data.get("_meta", {}) if isinstance(data, dict) else {}


def _memory_summary() -> dict[str, Any]:
    index_path = _AGENT_DIR / "doc_vector_index.json"
    faiss_path = _AGENT_DIR / "doc_vector.index"
    index_data = _read_json(index_path, {})
    if isinstance(index_data, list):
        docs = index_data
    elif isinstance(index_data, dict):
        docs = index_data.get("documents") or index_data.get("docs") or []
    else:
        docs = []
    sources = set()
    if isinstance(docs, list):
        for doc in docs:
            if isinstance(doc, dict) and doc.get("source"):
                sources.add(str(doc["source"]))
    return {
        "index_file": str(index_path),
        "doc_count": len(docs) if isinstance(docs, list) else 0,
        "source_count": len(sources),
        "faiss_bytes": faiss_path.stat().st_size if faiss_path.exists() else 0,
        "updated": datetime.fromtimestamp(index_path.stat().st_mtime).isoformat(timespec="seconds")
        if index_path.exists()
        else None,
    }


@golem_console_bp.route("/api/golem/status", methods=["GET"])
@login_required
def golem_status_api():
    process_data = _collect_process_monitor(
        process_monitor_state_path=_GUARDIAN_STATE,
        magi_root=_MAGI_ROOT,
    )
    skills, meta = _skill_rows(limit=8)
    return jsonify(
        {
            "ok": bool(process_data.get("ok")),
            "ts": datetime.now().isoformat(timespec="seconds"),
            "root": str(_MAGI_ROOT),
            "hostname": os.uname().nodename if hasattr(os, "uname") else "",
            "process": process_data,
            "skills": {"items": skills, "meta": meta, "count": meta.get("runtime_filter", {}).get("tools_exposed")},
            "memory": _memory_summary(),
            "exports": _recent_files(_EXPORTS_DIR),
            "market_reports": _recent_files(_EXPORTS_DIR, prefix="market_briefing"),
            "api_keys": _api_key_status()["items"],
        }
    )


@golem_console_bp.route("/api/golem/api-keys", methods=["GET", "POST"])
@login_required
def golem_api_keys_api():
    if request.method == "GET":
        return jsonify(_api_key_status())
    if not _is_admin_user():
        return jsonify({"ok": False, "error": "admin_required"}), 403

    data = request.get_json(silent=True) or {}
    key_id = str(data.get("id") or "nvidia_nim").strip()
    meta = _MANAGED_API_KEYS.get(key_id)
    if not meta:
        return jsonify({"ok": False, "error": "unsupported_key"}), 400

    value = str(data.get("api_key") or "").strip()
    if not value:
        return jsonify({"ok": False, "error": "api_key_required"}), 400
    prefix = str(meta.get("prefix") or "")
    if prefix and not value.startswith(prefix):
        return jsonify({"ok": False, "error": f"invalid_prefix:{prefix}"}), 400

    enable = bool(data.get("enable", True))
    updates = {meta["env_key"]: value}
    if meta.get("enable_key"):
        updates[str(meta["enable_key"])] = "1" if enable else "0"
    backup = _write_env_values(_ENV_PATH, updates)
    for key, val in updates.items():
        os.environ[key] = val
    return jsonify(
        {
            "ok": True,
            "saved": True,
            "backup": str(backup),
            "item": _api_key_status()["items"][0],
            "restart_hint": "目前網頁程序已更新環境變數；背景工作或 daemon 若已載入舊環境，建議重啟 MAGI。",
        }
    )


@golem_console_bp.route("/api/golem/skills", methods=["GET"])
@login_required
def golem_skills_api():
    limit = max(1, min(int(request.args.get("limit", "48") or 48), 120))
    skills, meta = _skill_rows(limit=limit)
    count = meta.get("runtime_filter", {}).get("tools_exposed") if isinstance(meta, dict) else None
    return jsonify({"ok": True, "items": skills, "meta": meta, "count": count or len(skills)})


@golem_console_bp.route("/api/golem/logs", methods=["GET"])
@login_required
def golem_logs_api():
    return jsonify(
        {
            "ok": True,
            "server": _tail(_AGENT_DIR / "server.log", 80),
            "daemon": _tail(_AGENT_DIR / "daemon.log", 60),
            "market": _tail(_MAGI_ROOT / "skills" / "market-briefing" / "market_briefing_notify.log", 40),
        }
    )


@golem_console_bp.route("/api/golem/command", methods=["POST"])
@login_required
def golem_command_api():
    data = request.get_json(silent=True) or {}
    command = str(data.get("command") or "").strip().lower()
    if not command:
        return jsonify({"ok": False, "error": "empty_command"}), 400
    if command in {"status", "sys", "health"}:
        return golem_status_api()
    if command in {"skills", "tools"}:
        return golem_skills_api()
    if command in {"logs", "tail"}:
        return golem_logs_api()
    if command in {"market", "market-report", "briefing"}:
        return jsonify({"ok": True, "market_reports": _recent_files(_EXPORTS_DIR, prefix="market_briefing", limit=12)})
    if command in {"memory", "mem"}:
        return jsonify({"ok": True, "memory": _memory_summary()})
    return jsonify(
        {
            "ok": True,
            "message": "已收到指令。此控制台目前提供 status / skills / logs / market / memory 等安全診斷命令。",
            "command": command,
        }
    )
