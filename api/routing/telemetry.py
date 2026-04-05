"""Routing telemetry collector.

Records every routing decision to a JSONL file for offline analysis and
debugging.  The format is compatible with the existing orchestrator trace
(``route_trace``) so that existing dashboards continue to work.

Usage::

    from api.routing.telemetry import RoutingTelemetry

    tel = RoutingTelemetry()
    tel.record(decision)
    stats = tel.summary()
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from api.routing.models import RoutingDecision

_log = logging.getLogger(__name__)

# Default telemetry output path
_DEFAULT_TELEMETRY_DIR = Path(".agent")
_DEFAULT_TELEMETRY_FILE = "routing_telemetry.jsonl"


class RoutingTelemetry:
    """Append-only telemetry logger for routing decisions.

    Parameters:
        telemetry_dir:   Directory to write the JSONL file.  Created
                         automatically if it does not exist.
        filename:        Name of the JSONL file inside *telemetry_dir*.
        enabled:         Set ``False`` to disable writes (dry run).
    """

    def __init__(
        self,
        *,
        telemetry_dir: Path | None = None,
        filename: str = _DEFAULT_TELEMETRY_FILE,
        enabled: bool = True,
    ) -> None:
        self._dir = telemetry_dir or _DEFAULT_TELEMETRY_DIR
        self._path = self._dir / filename
        self._enabled = enabled
        self._lock = threading.Lock()
        self._count = 0
        self._action_counter: Counter[str] = Counter()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, decision: RoutingDecision) -> None:
        """Write a single routing decision as one JSONL line.

        Thread-safe; multiple workers can call this concurrently.
        """
        entry = self._build_entry(decision)
        with self._lock:
            self._count += 1
            self._action_counter[decision.action] += 1
        if not self._enabled:
            return
        self._write_line(entry)

    def record_raw(self, data: dict[str, Any]) -> None:
        """Write an arbitrary dict as one JSONL line.

        Useful for recording legacy ``route_trace`` dicts.
        """
        enriched = {
            "ts": time.time(),
            "source": "raw",
            **data,
        }
        with self._lock:
            self._count += 1
        if not self._enabled:
            return
        self._write_line(enriched)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_all(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Read the last *limit* entries from the JSONL file.

        Returns newest-first.
        """
        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").strip().splitlines()
        except Exception:
            _log.warning("Failed to read telemetry file %s", self._path, exc_info=True)
            return []

        entries: list[dict[str, Any]] = []
        for line in reversed(lines[-limit:]):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Return summary statistics for the current session.

        Includes total count and per-action breakdown.  Reads from the
        in-memory counters (not from disk).
        """
        with self._lock:
            return {
                "total": self._count,
                "by_action": dict(self._action_counter),
            }

    def summary_from_disk(self, *, limit: int = 1000) -> dict[str, Any]:
        """Compute summary statistics from the on-disk JSONL file."""
        entries = self.read_all(limit=limit)
        if not entries:
            return {"total": 0, "by_action": {}}
        counter: Counter[str] = Counter()
        for e in entries:
            counter[e.get("action", "unknown")] += 1
        return {
            "total": len(entries),
            "by_action": dict(counter),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_entry(decision: RoutingDecision) -> dict[str, Any]:
        """Convert a RoutingDecision into a flat JSONL-friendly dict."""
        ctx_id = ""
        user_id = ""
        platform = ""
        if decision.route_context is not None:
            ctx_id = decision.route_context.correlation_id
            user_id = decision.route_context.user_id
            platform = decision.route_context.platform

        return {
            "ts": time.time(),
            "correlation_id": ctx_id,
            "user_id": user_id,
            "platform": platform,
            "action": decision.action,
            "matched": decision.matched,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "intent": decision.intent,
            "handler": decision.handler,
            "candidates": list(decision.candidates),
            "trace": list(decision.trace),
        }

    def _write_line(self, entry: dict[str, Any]) -> None:
        """Append a single JSON line to the telemetry file."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            _log.warning("Failed to write telemetry to %s", self._path, exc_info=True)
