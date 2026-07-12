from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

from flask import has_request_context, request, session
from flask_login import current_user


ROOT = Path(os.environ.get("MAGI_ROOT_DIR") or Path(__file__).resolve().parents[1]).resolve()
AUDIT_PATH = Path(os.environ.get("MAGI_SAAS_AUDIT_PATH") or ROOT / ".runtime" / "saas_audit_events.jsonl")
_LOCK = threading.Lock()
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}


def _sha(value: Any) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _actor() -> dict[str, str]:
    try:
        if current_user and current_user.is_authenticated:
            return {
                "id": str(getattr(current_user, "id", "") or ""),
                "role": str(getattr(current_user, "role", "") or ""),
                "tenant_id": str(getattr(current_user, "tenant_id", "") or session.get("tenant_id") or ""),
            }
    except Exception:
        pass
    return {"id": "anonymous", "role": ""}


def _request_meta() -> dict[str, str]:
    if not has_request_context():
        return {}
    return {
        "method": str(request.method or ""),
        "path": str(request.path or ""),
        "remote_addr_hash": _sha(request.headers.get("CF-Connecting-IP") or request.remote_addr or ""),
        "user_agent_hash": _sha(request.headers.get("User-Agent") or ""),
    }


def _clean_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in _SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        if len(value) > 512:
            return value[:509] + "..."
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _clean_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_value(key, item) for item in value[:50]]
    return str(value)[:512]


def file_ref(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Return an audit-safe file reference without exposing full NAS paths."""
    text = str(path or "")
    p = Path(text)
    return {
        "name": p.name,
        "ext": p.suffix.lower(),
        "path_hash": _sha(text),
    }


def append_audit_event(
    action: str,
    *,
    resource_type: str = "",
    resource_id: str = "",
    status: str = "ok",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "action": str(action or ""),
        "resource_type": str(resource_type or ""),
        "resource_id": str(resource_id or ""),
        "status": str(status or "ok"),
        "actor": _actor(),
        "request": _request_meta(),
        "metadata": _clean_value("metadata", metadata or {}),
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with _LOCK:
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            os.chmod(AUDIT_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return event
