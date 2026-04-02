"""
MAGI structured JSON logging.

Provides a JSON formatter and request-context filter for the standard
logging module. No external dependencies (no structlog).

Usage in server.py:
    from skills.ops.structured_log import JSONFormatter, RequestContextFilter

    handler.setFormatter(JSONFormatter())
    logging.getLogger().addFilter(RequestContextFilter())
"""

import json
import logging
import threading
import time
import traceback
from typing import Optional


# ---------------------------------------------------------------------------
# Thread-local request context
# ---------------------------------------------------------------------------
_ctx = threading.local()


def set_request_context(*, request_id: str = "", user_id: str = "", platform: str = ""):
    """Set request-scoped context that will be injected into every log record."""
    _ctx.request_id = request_id
    _ctx.user_id = user_id
    _ctx.platform = platform


def clear_request_context():
    """Clear request context (call at end of request)."""
    _ctx.request_id = ""
    _ctx.user_id = ""
    _ctx.platform = ""


class RequestContextFilter(logging.Filter):
    """Inject request context into every log record."""

    def filter(self, record):
        record.request_id = getattr(_ctx, "request_id", "") or ""
        record.user_id = getattr(_ctx, "user_id", "") or ""
        record.platform = getattr(_ctx, "platform", "") or ""
        return True


# ---------------------------------------------------------------------------
# JSON Formatter
# ---------------------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    """
    Emit each log record as a single JSON line.

    Output fields:
        ts, level, logger, msg, request_id, user_id, platform,
        exc (if exception), file, line, func
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Request context (from filter)
        rid = getattr(record, "request_id", "")
        if rid:
            entry["request_id"] = rid
        uid = getattr(record, "user_id", "")
        if uid:
            entry["user_id"] = uid
        plat = getattr(record, "platform", "")
        if plat:
            entry["platform"] = plat

        # Exception info
        if record.exc_info and record.exc_info[1]:
            entry["exc"] = self.formatException(record.exc_info)

        # Source location (useful for debugging, skip for brevity in production)
        if record.levelno >= logging.WARNING:
            entry["file"] = record.pathname.rsplit("/", 2)[-1] if "/" in record.pathname else record.filename
            entry["line"] = record.lineno

        return json.dumps(entry, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Hybrid Formatter — JSON for file, human-readable for console
# ---------------------------------------------------------------------------

class HybridFormatter(logging.Formatter):
    """
    Console-friendly format that still includes request_id when present.

    Format: 2026-03-11 14:30:22 INFO Server [abc123] message
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        rid = getattr(record, "request_id", "")
        rid_tag = f" [{rid}]" if rid else ""
        msg = record.getMessage()

        base = f"{ts} {record.levelname} {record.name}{rid_tag}: {msg}"

        if record.exc_info and record.exc_info[1]:
            base += "\n" + self.formatException(record.exc_info)

        return base
