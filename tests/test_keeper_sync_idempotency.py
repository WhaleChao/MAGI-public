# -*- coding: utf-8 -*-
"""Idempotency tests for keeper_sync daemon."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "skills" / "memory" / "keeper_sync.py"


def _load_module():
    name = "keeper_sync_for_test"
    spec = importlib.util.spec_from_file_location(name, str(SCRIPT_PATH))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class _FakeCursor:
    def __init__(self, existing_self: bool):
        self._existing_self = existing_self
        self._fetchone = None
        self.lastrowid = 100
        self.docs_inserted = 0
        self.vectors_inserted = 0

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split()).strip().lower()
        if s.startswith("show columns from documents like 'source'"):
            self._fetchone = ("source", "text")
            return
        if s.startswith("select 1 from documents where id = %s"):
            self._fetchone = (1,) if self._existing_self else None
            return
        if s.startswith("select 1 from documents where source = %s"):
            self._fetchone = None
            return
        if s.startswith("insert into documents (content, source, synced)"):
            self.docs_inserted += 1
            self.lastrowid += 1
            self._fetchone = None
            return
        if s.startswith("insert into vectors"):
            self.vectors_inserted += 1
            self._fetchone = None
            return
        if s.startswith("alter table documents modify column source text"):
            self._fetchone = None
            return
        raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self._fetchone

    def close(self):
        return None


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_sync_to_keeper_skips_reinsert_when_row_already_exists(monkeypatch):
    mod = _load_module()
    pending = [{"id": 7, "content": "same content", "source": "research-brief:語言政策"}]
    marked = []
    fake_cursor = _FakeCursor(existing_self=True)
    fake_conn = _FakeConn(fake_cursor)

    monkeypatch.setattr(mod, "get_pending_sync", lambda limit=50: pending)
    monkeypatch.setattr(mod, "mark_synced", lambda ids: marked.extend(ids) or True)
    monkeypatch.setattr(mod.mysql.connector, "connect", lambda **kwargs: fake_conn)
    monkeypatch.setattr(mod, "get_embedding", lambda text: [0.1] * 3)

    synced_count = mod.sync_to_keeper()
    assert synced_count == 1
    assert marked == [7]
    assert fake_cursor.docs_inserted == 0
    assert fake_cursor.vectors_inserted == 0


def test_sync_to_keeper_inserts_once_when_not_present(monkeypatch):
    mod = _load_module()
    pending = [{"id": 9, "content": "new content", "source": "research-brief:通譯"}]
    marked = []
    fake_cursor = _FakeCursor(existing_self=False)
    fake_conn = _FakeConn(fake_cursor)

    monkeypatch.setattr(mod, "get_pending_sync", lambda limit=50: pending)
    monkeypatch.setattr(mod, "mark_synced", lambda ids: marked.extend(ids) or True)
    monkeypatch.setattr(mod.mysql.connector, "connect", lambda **kwargs: fake_conn)
    monkeypatch.setattr(mod, "get_embedding", lambda text: [0.2] * 3)

    synced_count = mod.sync_to_keeper()
    assert synced_count == 1
    assert marked == [9]
    assert fake_cursor.docs_inserted == 1
    assert fake_cursor.vectors_inserted == 1
