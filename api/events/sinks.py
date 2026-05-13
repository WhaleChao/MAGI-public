from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

from api.events.models import EventModel

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared JSONL rotation helper
# ---------------------------------------------------------------------------
_ROTATE_MAX_BYTES = int(os.environ.get("MAGI_JSONL_ROTATE_MAX_BYTES", 10 * 1024 * 1024))  # 10 MB
_ROTATE_KEEP = int(os.environ.get("MAGI_JSONL_ROTATE_KEEP", 5))


def rotate_jsonl(filepath: Path | str, max_bytes: int = _ROTATE_MAX_BYTES, keep: int = _ROTATE_KEEP) -> None:
    """Size-based rotation for a JSONL file.

    When *filepath* exceeds *max_bytes*, rename it to ``<name>.1.jsonl``,
    shifting existing rotated files up by one.  Files beyond *keep* are
    deleted.  The caller is expected to hold any relevant lock before
    invoking this function.
    """
    fp = Path(filepath)
    try:
        if not fp.exists() or fp.stat().st_size <= max_bytes:
            return
    except OSError:
        return

    stem = fp.stem  # e.g. "routing_telemetry"
    suffix = fp.suffix  # e.g. ".jsonl"
    parent = fp.parent

    # Delete the oldest file that would be pushed beyond *keep*
    oldest = parent / f"{stem}.{keep}{suffix}"
    if oldest.exists():
        try:
            oldest.unlink()
        except OSError:
            pass

    # Shift existing rotated files: .4 -> .5, .3 -> .4, ...
    for i in range(keep - 1, 0, -1):
        src = parent / f"{stem}.{i}{suffix}"
        dst = parent / f"{stem}.{i + 1}{suffix}"
        if src.exists():
            try:
                src.rename(dst)
            except OSError:
                pass

    # Rotate the current file to .1
    try:
        fp.rename(parent / f"{stem}.1{suffix}")
    except OSError as exc:
        _log.warning("JSONL rotation failed for %s: %s", fp, exc)


class JsonlSink:
    """Thread-safe JSONL sink for event streams with size-based rotation."""

    def __init__(self, path: str | Path, *, max_bytes: int = _ROTATE_MAX_BYTES, keep: int = _ROTATE_KEEP):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._max_bytes = max_bytes
        self._keep = keep
        self._write_count = 0

    def write(self, event: EventModel) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = event.to_json()
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._write_count += 1
            # Check rotation every 500 writes to avoid stat() on every call
            if self._write_count % 500 == 0:
                rotate_jsonl(self.path, self._max_bytes, self._keep)


def jsonl_sink(path: str | Path) -> Callable[[EventModel], None]:
    """Return a callback suitable for attaching to EventEmitter."""

    sink = JsonlSink(path)
    return sink.write


def append_jsonl(path: str | Path, row: dict) -> None:
    """Small helper for generic JSONL append operations (with rotation)."""

    import json

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    # Rotate if needed
    try:
        rotate_jsonl(p)
    except Exception:
        pass

