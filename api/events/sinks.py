from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from api.events.models import EventModel


class JsonlSink:
    """Thread-safe JSONL sink for event streams."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, event: EventModel) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = event.to_json()
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def jsonl_sink(path: str | Path) -> Callable[[EventModel], None]:
    """Return a callback suitable for attaching to EventEmitter."""

    sink = JsonlSink(path)
    return sink.write


def append_jsonl(path: str | Path, row: dict) -> None:
    """Small helper for generic JSONL append operations."""

    import json

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

