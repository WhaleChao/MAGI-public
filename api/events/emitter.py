from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from api.events.models import EventModel
from api.events.sinks import JsonlSink

EventCallback = Callable[[EventModel], Any]


@dataclass(slots=True)
class Subscription:
    emitter: "EventEmitter"
    event_type: str
    callback: EventCallback
    active: bool = field(default=True)

    def unsubscribe(self) -> bool:
        if not self.active:
            return False
        self.active = False
        return self.emitter._unsubscribe(self.event_type, self.callback)


class EventEmitter:
    """Lightweight in-process event emitter with ordered delivery."""

    def __init__(self):
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[EventCallback]] = defaultdict(list)

    @staticmethod
    def _normalize_event_type(event_type: str | type[EventModel]) -> str:
        if isinstance(event_type, str):
            return event_type
        if isinstance(event_type, type) and issubclass(event_type, EventModel):
            return event_type.event_type
        raise TypeError("event_type must be a string or EventModel subclass")

    def subscribe(self, event_type: str | type[EventModel], callback: EventCallback) -> Subscription:
        normalized = self._normalize_event_type(event_type)
        with self._lock:
            self._subscribers[normalized].append(callback)
        return Subscription(self, normalized, callback)

    def _unsubscribe(self, event_type: str, callback: EventCallback) -> bool:
        with self._lock:
            callbacks = self._subscribers.get(event_type)
            if not callbacks:
                return False
            try:
                callbacks.remove(callback)
            except ValueError:
                return False
            if not callbacks:
                self._subscribers.pop(event_type, None)
            return True

    def emit(self, event: EventModel) -> list[Any]:
        with self._lock:
            callbacks: list[EventCallback] = []
            callbacks.extend(self._subscribers.get(event.event_type, ()))
            callbacks.extend(self._subscribers.get("*", ()))
        results: list[Any] = []
        for callback in callbacks:
            results.append(callback(event))
        return results

    def add_jsonl_sink(self, path: str | Path, event_type: str | type[EventModel] = "*") -> Subscription:
        sink = JsonlSink(path)
        return self.subscribe(event_type, sink.write)

    def subscribers_for(self, event_type: str | type[EventModel]) -> list[EventCallback]:
        normalized = self._normalize_event_type(event_type)
        with self._lock:
            return list(self._subscribers.get(normalized, ()))

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()

