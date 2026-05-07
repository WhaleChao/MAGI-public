from __future__ import annotations

import datetime as _dt
import json as _json
from pathlib import Path
from typing import Optional

from api.runtime_paths import get_magi_root_dir


_AUTOPILOT_RUNTIME_REL = Path(".runtime") / "autopilot"


def _resolve_root(root: Optional[str] = None) -> Path:
    if root:
        return Path(root).expanduser().resolve()
    return get_magi_root_dir().resolve()


def get_autopilot_runtime_dir(root: Optional[str] = None, ensure: bool = False) -> Path:
    runtime_dir = _resolve_root(root) / _AUTOPILOT_RUNTIME_REL
    if ensure:
        runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def get_kill_reason_path(pid: int, root: Optional[str] = None) -> Path:
    return get_autopilot_runtime_dir(root=root, ensure=True) / f"kill_reason_{int(pid)}.txt"


def get_legacy_kill_reason_path(pid: int, root: Optional[str] = None) -> Path:
    return _resolve_root(root) / f"_autopilot_kill_reason_{int(pid)}"


def get_kill_log_path(root: Optional[str] = None) -> Path:
    return get_autopilot_runtime_dir(root=root, ensure=True) / "kill_log.jsonl"


def write_kill_reason(pid: int, reason: str, root: Optional[str] = None) -> None:
    reason_path = get_kill_reason_path(pid, root=root)
    reason_path.write_text(str(reason or ""), encoding="utf-8")

    log_path = get_kill_log_path(root=root)
    entry = _json.dumps(
        {"ts": _dt.datetime.now().isoformat(), "pid": int(pid), "reason": str(reason or "")},
        ensure_ascii=False,
    )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry + "\n")
    try:
        from api.events.sinks import rotate_jsonl

        rotate_jsonl(str(log_path))
    except Exception:
        pass


def read_kill_reason(pid: int, root: Optional[str] = None, delete: bool = True) -> str:
    candidates = [
        get_kill_reason_path(pid, root=root),
        get_legacy_kill_reason_path(pid, root=root),
    ]
    for path in candidates:
        try:
            if path.exists():
                reason = path.read_text(encoding="utf-8").strip()
                if delete:
                    path.unlink()
                return reason
        except Exception:
            continue
    return ""


def cleanup_stale_kill_reason_files(root: Optional[str] = None, max_age_seconds: int = 3600) -> int:
    now = _dt.datetime.now().timestamp()
    removed = 0
    runtime_dir = get_autopilot_runtime_dir(root=root, ensure=True)
    candidates = list(runtime_dir.glob("kill_reason_*.txt"))
    candidates.extend(_resolve_root(root).glob("_autopilot_kill_reason_*"))
    for path in candidates:
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink()
                removed += 1
        except Exception:
            continue
    return removed
