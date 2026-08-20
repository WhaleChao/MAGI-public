"""Secure staging for configuration updates that require a controlled rebind."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Mapping

from api.runtime_paths import get_runtime_dir


_PENDING_UPDATE_LOCK = threading.RLock()


def pending_env_update_path() -> Path:
    configured = str(os.environ.get("MAGI_PENDING_ENV_UPDATE_FILE") or "").strip()
    return Path(configured).expanduser() if configured else get_runtime_dir() / "pending-config" / "env_updates.json"


def write_pending_env_updates(
    updates: Mapping[str, str],
    *,
    active_env_path: Path,
    requested_by: str,
) -> Path:
    """Atomically stage secrets without changing the active hash-bound env."""

    with _PENDING_UPDATE_LOCK:
        return _write_pending_env_updates_unlocked(
            updates,
            active_env_path=active_env_path,
            requested_by=requested_by,
        )


def _write_pending_env_updates_unlocked(
    updates: Mapping[str, str],
    *,
    active_env_path: Path,
    requested_by: str,
) -> Path:

    path = pending_env_update_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    pending: dict = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            pending = loaded
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pending = {}
    merged = pending.get("updates") if isinstance(pending.get("updates"), dict) else {}
    merged = {str(key): str(value) for key, value in merged.items()}
    merged.update({str(key): str(value) for key, value in updates.items()})
    payload = {
        "schema_version": 1,
        "status": "pending_controlled_rebind",
        "requested_at": datetime.now().astimezone().isoformat(),
        "requested_by": str(requested_by),
        "active_env_file": str(active_env_path),
        "updates": merged,
        "contract": {
            "active_env_mutation_allowed": False,
            "requires_controlled_redeploy_or_rebind": True,
            "apply_in_current_process": False,
        },
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".env-updates.", dir=str(path.parent))
    temporary = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path
