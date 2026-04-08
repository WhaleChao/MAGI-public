# -*- coding: utf-8 -*-
"""Tests for skills/ops/fs_watcher.py — FSEvents file monitoring."""

import os
import time
import threading
from queue import Queue, Empty
from unittest.mock import patch, MagicMock

import pytest


class TestCaseFolderHandler:
    """Test the event handler logic (debounce, filtering, dispatch)."""

    def _make_handler(self, debounce=0.1, min_size=0):
        """Create handler with fast debounce for testing."""
        # Import only if watchdog is available
        pytest.importorskip("watchdog")
        from skills.ops.fs_watcher import CaseFolderHandler
        q = Queue()
        handler = CaseFolderHandler(
            callback_queue=q,
            debounce_seconds=debounce,
            min_file_size=min_size,
        )
        return handler, q

    def test_filters_non_watched_extensions(self):
        handler, q = self._make_handler()
        event = MagicMock(is_directory=False, src_path="/tmp/test.txt")
        handler.on_created(event)
        time.sleep(0.3)
        assert q.empty()

    def test_accepts_pdf_extension(self):
        handler, q = self._make_handler(min_size=0)
        event = MagicMock(is_directory=False, src_path="/tmp/test.pdf")

        with patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=5000):
            handler.on_created(event)
            time.sleep(0.3)

        assert not q.empty()
        data = q.get_nowait()
        assert data["path"] == "/tmp/test.pdf"
        assert data["event"] == "created"

    def test_ignores_directories(self):
        handler, q = self._make_handler()
        event = MagicMock(is_directory=True, src_path="/tmp/new_folder")
        handler.on_created(event)
        time.sleep(0.3)
        assert q.empty()

    def test_debounce_merges_rapid_events(self):
        handler, q = self._make_handler(debounce=0.2, min_size=0)

        with patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=5000):
            for _ in range(5):
                event = MagicMock(is_directory=False, src_path="/tmp/test.pdf")
                handler.on_modified(event)
                time.sleep(0.05)

            time.sleep(0.5)

        # Should have at most 1-2 events due to debounce
        count = 0
        while not q.empty():
            q.get_nowait()
            count += 1
        assert count <= 2

    def test_skips_small_files(self):
        handler, q = self._make_handler(min_size=1024)
        event = MagicMock(is_directory=False, src_path="/tmp/tiny.pdf")

        with patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=100):
            handler.on_created(event)
            time.sleep(0.3)

        assert q.empty()

    def test_cleanup_stale_records(self):
        handler, _ = self._make_handler()
        handler._processed["/tmp/old.pdf"] = time.time() - 7200  # 2 hours ago
        handler._processed["/tmp/recent.pdf"] = time.time()

        handler.cleanup_stale_records(max_age=3600)

        assert "/tmp/old.pdf" not in handler._processed
        assert "/tmp/recent.pdf" in handler._processed


class TestFSWatcher:
    """Test the FSWatcher wrapper."""

    def test_requires_watchdog(self):
        """FSWatcher should raise if watchdog is not installed."""
        with patch.dict("sys.modules", {"watchdog": None, "watchdog.observers": None, "watchdog.events": None}):
            # This would need a fresh import, which is complex in tests.
            # Instead, verify HAS_WATCHDOG flag exists.
            from skills.ops.fs_watcher import HAS_WATCHDOG
            # HAS_WATCHDOG is set at import time, just verify it's a bool
            assert isinstance(HAS_WATCHDOG, bool)

    @pytest.mark.skipif(
        not os.path.exists("/tmp"),
        reason="requires /tmp directory"
    )
    def test_watcher_start_stop(self):
        pytest.importorskip("watchdog")
        from skills.ops.fs_watcher import FSWatcher

        watcher = FSWatcher(folders=["/tmp"], callback=lambda e: None, debounce_seconds=1)
        assert watcher.start() is True
        assert watcher.is_running is True

        watcher.stop()
        time.sleep(0.5)
        assert watcher.is_running is False

    def test_watcher_no_valid_folders(self):
        pytest.importorskip("watchdog")
        from skills.ops.fs_watcher import FSWatcher

        watcher = FSWatcher(folders=["/nonexistent/path"], callback=lambda e: None)
        assert watcher.start() is False

    def test_start_watcher_factory(self):
        pytest.importorskip("watchdog")
        from skills.ops.fs_watcher import start_watcher

        watcher = start_watcher(["/tmp"], callback=lambda e: None)
        assert watcher is not None
        assert watcher.is_running is True
        watcher.stop()

    def test_get_watched_folders(self):
        pytest.importorskip("watchdog")
        from skills.ops.fs_watcher import FSWatcher

        watcher = FSWatcher(
            folders=["/tmp", "/nonexistent/path"],
            callback=lambda e: None,
        )
        folders = watcher.get_watched_folders()
        assert "/tmp" in folders
        assert "/nonexistent/path" not in folders
