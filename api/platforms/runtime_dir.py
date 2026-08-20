# -*- coding: utf-8 -*-
"""
RuntimeDir — 所有 .runtime/ 路徑集中管理。

Feature flag:
  MAGI_USE_RUNTIME_DIR=0/1   (預設 0)
  MAGI_RUNTIME_DIR=<path>    (override；預設 <MAGI_ROOT>/.runtime)

對外 API（只有這些）：
  - root() -> Path
  - pending(name: str) -> Path        # .runtime/pending/<name>.json
  - metrics(name: str) -> Path        # .runtime/metrics/<name>.jsonl
  - cron_state() -> Path              # .runtime/cron_state.json
  - atomic_write_json(path, data) -> None
  - atomic_append_jsonl(path, record, rotate_at=500, keep_tail=300) -> None
  - legacy_fallback(new_path, legacy_candidates) -> Path
  - reset_for_test() -> None
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional, Sequence

_mkdir_lock = threading.Lock()
_CODE_ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def _append_lock(path: Path):
    """Serialize append+rotation across MAGI processes using a sidecar lock."""

    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            from magi_v3 import fcntl_compat as fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            fcntl = None  # type: ignore[assignment]
        try:
            yield
        finally:
            try:
                if fcntl is not None:  # type: ignore[name-defined]
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _enabled() -> bool:
    return os.environ.get("MAGI_USE_RUNTIME_DIR", "0").strip().lower() in {"1", "true", "on", "yes"}


def _magi_root() -> Path:
    return Path(
        os.environ.get("MAGI_ROOT_DIR")
        or os.environ.get("MAGI_ROOT")
        or str(_CODE_ROOT)
    ).expanduser().resolve()


def root() -> Path:
    """回傳 runtime root；若 flag off 仍回 default path 方便查。"""
    override = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
    p = Path(override).expanduser().resolve() if override else _magi_root() / ".runtime"
    with _mkdir_lock:
        p.mkdir(parents=True, exist_ok=True)
    return p


def pending(name: str) -> Path:
    _validate_name(name)
    d = root() / "pending"
    with _mkdir_lock:
        d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.json"


def metrics(name: str) -> Path:
    _validate_name(name)
    d = root() / "metrics"
    with _mkdir_lock:
        d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.jsonl"


def cron_state() -> Path:
    return root() / "cron_state.json"


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("runtime_dir name must be non-empty str")
    if "/" in name or ".." in name or "\\" in name:
        raise ValueError(f"runtime_dir name contains path separator: {name!r}")


# --- atomic writers -----------------------------------------------------

def atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    with _mkdir_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_append_jsonl(
    path: Path,
    record: Any,
    rotate_at: int = 500,
    keep_tail: int = 300,
) -> None:
    path = Path(path)
    with _mkdir_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _append_lock(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        if rotate_at > 0 and keep_tail > 0:
            tail, overflow = _read_jsonl_tail(path, max_lines=rotate_at + 1)
            if overflow or len(tail) > rotate_at:
                atomic_write_json_lines(path, tail[-keep_tail:])


def _read_jsonl_tail(path: Path, *, max_lines: int) -> tuple[list[str], bool]:
    """Read only the bounded tail needed for rotation, never the whole JSONL."""

    if max_lines <= 0:
        return [], False
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        cursor = handle.tell()
        while cursor > 0 and newline_count <= max_lines:
            size = min(64 * 1024, cursor)
            cursor -= size
            handle.seek(cursor)
            chunk = handle.read(size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    raw = b"".join(reversed(chunks))
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    overflow = cursor > 0 or len(lines) > max_lines
    return lines[-max_lines:], overflow


def atomic_write_json_lines(path: Path, lines: Sequence[str]) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- legacy fallback (read-only) ---------------------------------------

def legacy_fallback(new_path: Path, legacy_candidates: Sequence[Path]) -> Path:
    """
    Dual-READ only：若 new_path 存在，直接回 new_path；
    否則從 legacy_candidates 依序回第一個存在的；都不存在回 new_path（由 caller 決定怎麼處理）。
    **永遠不要雙寫**，寫只寫 new_path。
    """
    new_path = Path(new_path)
    if new_path.exists():
        return new_path
    for c in legacy_candidates:
        cp = Path(c)
        if cp.exists():
            return cp
    return new_path


def reset_for_test() -> None:
    """測試用：不清 env，只是佔位 API 讓測試可 import。"""
    return None
