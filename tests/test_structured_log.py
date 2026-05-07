"""Tests for skills.ops.structured_log — JSON formatter and request context."""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from skills.ops.structured_log import (
    JSONFormatter,
    HybridFormatter,
    RequestContextFilter,
    set_request_context,
    clear_request_context,
)


def _make_record(msg="test message", level=logging.INFO, name="TestLogger"):
    logger = logging.getLogger(name)
    record = logger.makeRecord(name, level, "test.py", 42, msg, (), None)
    return record


def test_json_formatter_basic():
    fmt = JSONFormatter()
    record = _make_record("hello world")
    line = fmt.format(record)
    data = json.loads(line)
    assert data["msg"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "TestLogger"
    assert "ts" in data


def test_json_formatter_with_context():
    filt = RequestContextFilter()
    fmt = JSONFormatter()
    set_request_context(request_id="abc123", user_id="U999", platform="LINE")
    try:
        record = _make_record("ctx test")
        filt.filter(record)
        line = fmt.format(record)
        data = json.loads(line)
        assert data["request_id"] == "abc123"
        assert data["user_id"] == "U999"
        assert data["platform"] == "LINE"
    finally:
        clear_request_context()


def test_json_formatter_no_context():
    filt = RequestContextFilter()
    fmt = JSONFormatter()
    clear_request_context()
    record = _make_record("no ctx")
    filt.filter(record)
    line = fmt.format(record)
    data = json.loads(line)
    assert "request_id" not in data  # Empty strings are omitted


def test_json_formatter_warning_includes_file():
    fmt = JSONFormatter()
    record = _make_record("warn msg", level=logging.WARNING)
    line = fmt.format(record)
    data = json.loads(line)
    assert "file" in data
    assert "line" in data


def test_hybrid_formatter_basic():
    fmt = HybridFormatter()
    record = _make_record("hybrid test")
    line = fmt.format(record)
    assert "INFO" in line
    assert "TestLogger" in line
    assert "hybrid test" in line


def test_hybrid_formatter_with_request_id():
    filt = RequestContextFilter()
    fmt = HybridFormatter()
    set_request_context(request_id="xyz789")
    try:
        record = _make_record("tagged")
        filt.filter(record)
        line = fmt.format(record)
        assert "[xyz789]" in line
    finally:
        clear_request_context()


def test_clear_request_context():
    set_request_context(request_id="tmp", user_id="U1", platform="TG")
    clear_request_context()
    filt = RequestContextFilter()
    fmt = JSONFormatter()
    record = _make_record("after clear")
    filt.filter(record)
    line = fmt.format(record)
    data = json.loads(line)
    assert "request_id" not in data
