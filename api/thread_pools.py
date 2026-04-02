"""
MAGI 共享執行緒池
================
統一管理全系統的 ThreadPoolExecutor，取代散落在各模組的獨立池。

池分類：
- io_pool:        檔案 I/O、DB 操作、背景狀態寫入
- inference_pool:  LLM 推理、翻譯、摘要（長時間阻塞）
- channel_pool:    訊息派送（LINE/Discord/Telegram）
"""
import logging

import atexit
import os
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Pool sizing from env (with sane defaults and clamps)
# ---------------------------------------------------------------------------
def _pool_size(env_key: str, default: int, lo: int = 2, hi: int = 16) -> int:
    return max(lo, min(hi, int(os.environ.get(env_key, str(default)) or str(default))))


io_pool = ThreadPoolExecutor(
    max_workers=_pool_size("MAGI_IO_POOL_WORKERS", 4, 2, 8),
    thread_name_prefix="magi-io",
)

inference_pool = ThreadPoolExecutor(
    max_workers=_pool_size("MAGI_INFERENCE_POOL_WORKERS", 6, 2, 12),
    thread_name_prefix="magi-inference",
)

channel_pool = ThreadPoolExecutor(
    max_workers=_pool_size("MAGI_CHANNEL_POOL_WORKERS", 10, 2, 16),
    thread_name_prefix="magi-channel",
)


def shutdown_all(wait: bool = False) -> None:
    """Shut down all shared thread pools (call on process exit)."""
    for pool in (io_pool, inference_pool, channel_pool):
        try:
            pool.shutdown(wait=wait)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 44, exc_info=True)


atexit.register(lambda: shutdown_all(wait=False))
