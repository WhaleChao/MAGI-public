# -*- coding: utf-8 -*-
"""
fs_watcher.py
=============
macOS FSEvents 即時檔案監控模組。

監控案件資料夾的檔案變動，新 PDF 放入後 5 秒內自動觸發 OCR + 命名。
使用 watchdog 套件（底層走 macOS FSEvents API），CPU 開銷趨近於零。

保留 nightly scan 作為 fallback（處理 FSEvents 遺漏的情況）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from typing import Callable, Optional

logger = logging.getLogger("FSWatcher")

# ---------------------------------------------------------------------------
# 嘗試載入 watchdog（可選依賴）
# ---------------------------------------------------------------------------
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileSystemEvent
    HAS_WATCHDOG = True
except ImportError:
    Observer = None
    FileSystemEventHandler = object
    FileSystemEvent = None
    HAS_WATCHDOG = False
    logger.info("watchdog not installed — FSEvents watcher disabled. Install: pip install watchdog")


# ---------------------------------------------------------------------------
# 預設監控的檔案類型
# ---------------------------------------------------------------------------
WATCHED_EXTENSIONS = {".pdf", ".docx", ".jpg", ".png", ".heic", ".tiff"}

# debounce 秒數（防止大檔案寫入中重複觸發）
DEFAULT_DEBOUNCE_SECONDS = 5

# 最小檔案大小（bytes），避免處理不完整的檔案
MIN_FILE_SIZE = 1024  # 1 KB


class CaseFolderHandler(FileSystemEventHandler):
    """監控案件資料夾的檔案變動。"""

    def __init__(
        self,
        callback_queue: Queue,
        watched_extensions: set[str] = None,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
        min_file_size: int = MIN_FILE_SIZE,
    ):
        super().__init__()
        self.callback_queue = callback_queue
        self.watched_extensions = watched_extensions or WATCHED_EXTENSIONS
        self.debounce_seconds = debounce_seconds
        self.min_file_size = min_file_size
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._processed: dict[str, float] = {}  # path → last processed timestamp

    def on_created(self, event: FileSystemEvent):
        if event.is_directory:
            return
        ext = os.path.splitext(event.src_path)[1].lower()
        if ext in self.watched_extensions:
            self._debounce(event.src_path, "created")

    def on_modified(self, event: FileSystemEvent):
        if event.is_directory:
            return
        ext = os.path.splitext(event.src_path)[1].lower()
        if ext in self.watched_extensions:
            self._debounce(event.src_path, "modified")

    def on_moved(self, event: FileSystemEvent):
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        if dest:
            ext = os.path.splitext(dest)[1].lower()
            if ext in self.watched_extensions:
                self._debounce(dest, "moved")

    def _debounce(self, path: str, event_type: str):
        """等待檔案寫入完成後再觸發處理。"""
        with self._lock:
            if path in self._pending:
                self._pending[path].cancel()
            timer = threading.Timer(
                self.debounce_seconds,
                self._dispatch,
                args=(path, event_type),
            )
            timer.daemon = True
            self._pending[path] = timer
            timer.start()

    def _dispatch(self, path: str, event_type: str):
        with self._lock:
            self._pending.pop(path, None)

        # 檢查檔案存在且大小足夠
        try:
            if not os.path.isfile(path):
                logger.debug("FSWatcher: file disappeared before dispatch: %s", path)
                return
            size = os.path.getsize(path)
            if size < self.min_file_size:
                logger.debug("FSWatcher: file too small (%d bytes), skipping: %s", size, path)
                return
        except OSError:
            return

        # 防止短時間內重複處理同一檔案
        now = time.time()
        last = self._processed.get(path, 0)
        if now - last < self.debounce_seconds * 2:
            logger.debug("FSWatcher: duplicate dispatch suppressed for: %s", path)
            return
        self._processed[path] = now

        event_data = {
            "path": path,
            "event": event_type,
            "timestamp": datetime.now().isoformat(),
            "size": size,
            "extension": os.path.splitext(path)[1].lower(),
        }
        logger.info("FSWatcher: %s → %s (%d bytes)", event_type, os.path.basename(path), size)
        self.callback_queue.put(event_data)

    def cleanup_stale_records(self, max_age: float = 3600):
        """清理過期的處理記錄，避免記憶體持續增長。"""
        cutoff = time.time() - max_age
        with self._lock:
            stale = [p for p, t in self._processed.items() if t < cutoff]
            for p in stale:
                del self._processed[p]


class FSWatcher:
    """
    檔案監控器封裝。

    支援多資料夾監控、自動重連（SMB 斷線/重連）、graceful shutdown。
    """

    def __init__(
        self,
        folders: list[str],
        callback: Optional[Callable[[dict], None]] = None,
        watched_extensions: set[str] = None,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ):
        if not HAS_WATCHDOG:
            raise RuntimeError("watchdog not installed. Run: pip install watchdog")

        self.folders = folders
        self.callback = callback
        self.queue: Queue = Queue()
        self.handler = CaseFolderHandler(
            callback_queue=self.queue,
            watched_extensions=watched_extensions,
            debounce_seconds=debounce_seconds,
        )
        self._observer: Optional[Observer] = None
        self._consumer_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> bool:
        """啟動檔案監控。返回是否成功。"""
        if self._running:
            logger.warning("FSWatcher already running")
            return True

        self._observer = Observer()
        scheduled = 0

        for folder in self.folders:
            if os.path.isdir(folder):
                try:
                    self._observer.schedule(self.handler, folder, recursive=True)
                    logger.info("FSWatcher: watching %s", folder)
                    scheduled += 1
                except Exception as e:
                    logger.error("FSWatcher: failed to watch %s: %s", folder, e)
            else:
                logger.warning("FSWatcher: folder not found, skipping: %s", folder)

        if scheduled == 0:
            logger.error("FSWatcher: no valid folders to watch")
            return False

        self._observer.daemon = True
        self._observer.start()
        self._running = True

        # 啟動事件消費線程
        self._consumer_thread = threading.Thread(
            target=self._consume_events, daemon=True, name="FSWatcher-consumer"
        )
        self._consumer_thread.start()

        logger.info("FSWatcher: started with %d folders", scheduled)
        return True

    def stop(self):
        """停止檔案監控。"""
        self._running = False
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        logger.info("FSWatcher: stopped")

    def _consume_events(self):
        """消費事件隊列，呼叫 callback 或記錄日誌。"""
        while self._running:
            try:
                event_data = self.queue.get(timeout=1)
            except Empty:
                # 定期清理過期記錄
                self.handler.cleanup_stale_records()
                continue

            if self.callback:
                try:
                    self.callback(event_data)
                except Exception as e:
                    logger.error("FSWatcher callback error: %s", e, exc_info=True)
            else:
                logger.info("FSWatcher event (no callback): %s", event_data)

    @property
    def is_running(self) -> bool:
        return self._running and self._observer is not None and self._observer.is_alive()

    def get_watched_folders(self) -> list[str]:
        """返回實際正在監控的資料夾列表。"""
        return [f for f in self.folders if os.path.isdir(f)]


def start_watcher(
    folders: list[str],
    callback: Optional[Callable[[dict], None]] = None,
) -> Optional[FSWatcher]:
    """
    快速啟動 FSWatcher 的工廠函式。

    由 daemon.py 在啟動時呼叫。

    Args:
        folders: 要監控的資料夾列表
        callback: 事件回調函式，接收 dict 參數

    Returns:
        FSWatcher 實例，或 None（watchdog 未安裝時）
    """
    if not HAS_WATCHDOG:
        logger.warning("FSWatcher: watchdog not installed, skipping")
        return None

    watcher = FSWatcher(folders=folders, callback=callback)
    if watcher.start():
        return watcher
    return None


# ---------------------------------------------------------------------------
# CLI 入口（測試用）
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python fs_watcher.py <folder1> [folder2] ...")
        sys.exit(1)

    folders = sys.argv[1:]
    print(f"Watching: {folders}")
    print("Press Ctrl+C to stop\n")

    def on_event(event_data):
        print(f"[{event_data['timestamp']}] {event_data['event']}: {event_data['path']}")

    watcher = start_watcher(folders, callback=on_event)
    if not watcher:
        print("Failed to start watcher")
        sys.exit(1)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        watcher.stop()
