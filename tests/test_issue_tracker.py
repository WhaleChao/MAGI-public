# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import json


def _load_issue_tracker(monkeypatch, tmp_path, enable="1", markdown="0", ttl="300"):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("MAGI_USE_RUNTIME_DIR", "1")
    monkeypatch.setenv("MAGI_ISSUE_TRACKER_ENABLE", enable)
    monkeypatch.setenv("MAGI_ISSUE_TRACKER_MARKDOWN", markdown)
    monkeypatch.setenv("MAGI_ISSUE_TRACKER_DEDUP_TTL_SEC", ttl)

    import api.platforms.runtime_dir as runtime_dir
    import skills.management.issue_tracker as issue_tracker

    importlib.reload(runtime_dir)
    return importlib.reload(issue_tracker)


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_disabled_returns_none_and_does_not_write(tmp_path, monkeypatch):
    issue_tracker = _load_issue_tracker(monkeypatch, tmp_path, enable="0")

    result = issue_tracker.log_issue("cmd", "boom")

    assert result is None
    assert not (tmp_path / "issue_agenda.jsonl").exists()


def test_enabled_writes_jsonl_schema(tmp_path, monkeypatch):
    issue_tracker = _load_issue_tracker(monkeypatch, tmp_path)

    result = issue_tracker.log_issue("cmd", "ValueError: boom", context="ctx", source="test")

    assert result is True
    records = _read_jsonl(tmp_path / "issue_agenda.jsonl")
    assert len(records) == 1
    record = records[0]
    assert set(record) == {"ts", "iso", "command", "error", "context", "severity", "source", "dedup_key"}
    assert record["command"] == "cmd"
    assert record["error"] == "ValueError: boom"
    assert record["context"] == "ctx"
    assert record["severity"] == "High"
    assert record["source"] == "test"


def test_regex_scrubs_high_risk_values(tmp_path, monkeypatch):
    issue_tracker = _load_issue_tracker(monkeypatch, tmp_path)
    import skills.engine.pii_scrubber as pii_scrubber

    def fail_db_scrubber():
        raise RuntimeError("db down")

    monkeypatch.setattr(pii_scrubber, "build_scrubber_from_magi_db", fail_db_scrubber)

    result = issue_tracker.log_issue(
        "cmd A123456789 password=foo123",
        "nvapi-abcdefghijklmnopqrstuvwxyz sk-abcdefghijklmnopqrstuvwxyz123456 AIzaabcdefghijklmnopqrstuvwxyz123456",
    )

    assert result is True
    text = (tmp_path / "issue_agenda.jsonl").read_text(encoding="utf-8")
    assert "A123456789" not in text
    assert "foo123" not in text
    assert "nvapi-abcdefghijklmnopqrstuvwxyz" not in text
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in text
    assert "AIzaabcdefghijklmnopqrstuvwxyz123456" not in text
    assert "***ID***" in text
    assert "***NVIDIA_KEY***" in text
    assert "***OPENAI_KEY***" in text
    assert "***GOOGLE_KEY***" in text


def test_db_scrubber_failure_falls_back_to_regex(tmp_path, monkeypatch):
    issue_tracker = _load_issue_tracker(monkeypatch, tmp_path)
    import skills.engine.pii_scrubber as pii_scrubber

    def fail_db_scrubber():
        raise RuntimeError("db down")

    monkeypatch.setattr(pii_scrubber, "build_scrubber_from_magi_db", fail_db_scrubber)

    result = issue_tracker.log_issue("cmd A123456789", "password=foo123")

    assert result is True
    text = (tmp_path / "issue_agenda.jsonl").read_text(encoding="utf-8")
    assert "A123456789" not in text
    assert "foo123" not in text
    assert "***ID***" in text


def test_dedup_suppresses_second_same_command_and_error(tmp_path, monkeypatch):
    issue_tracker = _load_issue_tracker(monkeypatch, tmp_path)

    first = issue_tracker.log_issue("cmd", "boom")
    second = issue_tracker.log_issue("cmd", "boom")

    assert first is True
    assert second is None
    assert len(_read_jsonl(tmp_path / "issue_agenda.jsonl")) == 1


def test_dedup_normalizes_numbers(tmp_path, monkeypatch):
    issue_tracker = _load_issue_tracker(monkeypatch, tmp_path)

    first = issue_tracker.log_issue("case=123", "error 456")
    second = issue_tracker.log_issue("case=999", "error 777")

    assert first is True
    assert second is None
    assert len(_read_jsonl(tmp_path / "issue_agenda.jsonl")) == 1


def test_jsonl_rotation_uses_runtime_dir_atomic_append(tmp_path, monkeypatch):
    issue_tracker = _load_issue_tracker(monkeypatch, tmp_path)
    path = tmp_path / "rotate.jsonl"

    for i in range(6):
        issue_tracker.atomic_append_jsonl(path, {"i": i}, rotate_at=3, keep_tail=2)

    records = _read_jsonl(path)
    assert records == [{"i": 4}, {"i": 5}]


def test_legacy_markdown_flag_writes_runtime_markdown(tmp_path, monkeypatch):
    issue_tracker = _load_issue_tracker(monkeypatch, tmp_path, markdown="1")

    result = issue_tracker.log_issue("cmd", "boom")

    assert result is True
    legacy = tmp_path / "issue_agenda_legacy.md"
    assert legacy.exists()
    assert "cmd" in legacy.read_text(encoding="utf-8")


def test_atomic_append_failure_returns_false(tmp_path, monkeypatch):
    issue_tracker = _load_issue_tracker(monkeypatch, tmp_path)

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(issue_tracker, "atomic_append_jsonl", fail_append)

    result = issue_tracker.log_issue("cmd", "boom")

    assert result is False
