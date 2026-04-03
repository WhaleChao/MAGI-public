from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.events.sinks import JsonlSink


@dataclass(slots=True)
class HookEventCollector:
    events: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, event) -> dict[str, Any]:
        payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        self.events.append(payload)
        return payload


def jsonl_hook_subscriber(path: str | Path):
    return JsonlSink(path).write
