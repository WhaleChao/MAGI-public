"""
Autonomous skill acquisition (forge) flow extracted from Orchestrator.

All functions accept `orch` (the Orchestrator instance) instead of `self`.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError

from api.thread_pools import io_pool

logger = logging.getLogger("Orchestrator")


def auto_acquire_and_execute(orch, user_id, message, platform: str = "LINE") -> str:
    """
    Autonomous capability upgrade with auto-retry:
    acquire skill -> validate/activate -> optionally execute action.py.
    Runs in background thread (non-blocking), retries up to max times.
    """
    uid = str(user_id)
    lock = orch._forge_locks.setdefault(uid, threading.Lock())

    lock_ts_key = f"_forge_lock_ts_{uid}"
    lock_acquired_at = getattr(orch, lock_ts_key, 0)
    if lock_acquired_at and (time.time() - lock_acquired_at) > orch._FORGE_LOCK_TIMEOUT:
        try:
            lock.release()
            logger.warning("Force-released stale forge lock for uid=%s (held %.0fs)", uid, time.time() - lock_acquired_at)
        except RuntimeError:
            pass

    if not lock.acquire(blocking=False):
        return "⏳ 技能生成已在進行中，請稍候上一個完成…"
    setattr(orch, lock_ts_key, time.time())

    def _notify(text: str):
        try:
            cb = getattr(orch, "notification_callback", None)
            if cb:
                cb(str(user_id), text, platform)
            else:
                logger.warning("No notification_callback set, forge notification lost")
        except Exception as e:
            logger.warning(f"Forge notification callback failed: {e}")

    def _rebuild_embed_cache():
        try:
            from skills.bridge.embedding_router import get_router as _get_embed_router
            _er = _get_embed_router()
            if _er.is_ready:
                _er.rebuild_cache()
                logger.info("🔄 Embedding router cache rebuilt after skill genesis")
        except Exception as e:
            logger.debug(f"Embedding router rebuild after genesis: {e}")

    def _run_forge_with_retry():
        import concurrent.futures
        from skills.evolution.intent_forge import forge_execute

        max_retries = orch._FORGE_MAX_RETRIES
        timeouts = orch._FORGE_TIMEOUT_SCHEDULE

        for attempt in range(1, max_retries + 1):
            timeout = timeouts[min(attempt - 1, len(timeouts) - 1)]
            logger.info(f"🧬 Forge attempt {attempt}/{max_retries}, timeout={timeout}s")

            try:
                future = io_pool.submit(
                    forge_execute, str(user_id), message, "", "orchestrator_auto"
                )
                reply = future.result(timeout=timeout)

                msg = reply.get("reply", "ℹ️ 自主演化流程完成。") if isinstance(reply, dict) else str(reply)
                success = reply.get("success", False) if isinstance(reply, dict) else bool(msg)

                if success or attempt == max_retries:
                    _notify(msg)
                    _rebuild_embed_cache()
                    return

                logger.warning(f"Forge attempt {attempt} failed (non-success): {msg[:200]}")
                if attempt < max_retries:
                    _notify(
                        f"⏳ 技能生成第 {attempt} 次未成功，MAGI 正在自動重試"
                        f"（第 {attempt + 1}/{max_retries} 次）…"
                    )

            except concurrent.futures.TimeoutError:
                logger.warning(f"Forge attempt {attempt} timed out after {timeout}s")
                if attempt < max_retries:
                    _notify(
                        f"⏳ 技能生成第 {attempt} 次超時（{timeout}s），MAGI 正在自動接續"
                        f"（第 {attempt + 1}/{max_retries} 次，上限 {timeouts[min(attempt, len(timeouts) - 1)]}s）…"
                    )
                else:
                    _notify(
                        f"❌ 技能生成經過 {max_retries} 次嘗試仍未完成。\n"
                        f"累計等待約 {sum(timeouts[:max_retries]) // 60} 分鐘。\n"
                        "建議：簡化指令再試一次，或手動建立技能。"
                    )
                    return

            except Exception as e:
                logger.error(f"Forge attempt {attempt} error: {e}")
                if attempt < max_retries:
                    _notify(
                        f"⚠️ 技能生成第 {attempt} 次遇到錯誤：{str(e)[:100]}\n"
                        f"MAGI 正在自動重試（第 {attempt + 1}/{max_retries} 次）…"
                    )
                else:
                    _notify(f"❌ 技能生成失敗（{max_retries} 次嘗試）：{str(e)[:200]}")
                    return

    def _run_forge_with_lock():
        try:
            _run_forge_with_retry()
        except Exception as e:
            logger.error("Forge background thread crashed: %s", e)
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass
            try:
                setattr(orch, f"_forge_lock_ts_{uid}", 0)
            except Exception:
                pass

    try:
        threading.Thread(target=_run_forge_with_lock, daemon=True, name="forge-bg").start()
    except Exception as _thread_err:
        lock.release()
        logger.error(f"Failed to start forge thread: {_thread_err}")
        return f"❌ 技能生成啟動失敗：{_thread_err}"
    return "🧬 正在自動生成新技能中，請稍候（約 1-5 分鐘）。完成後我會主動回報，若超時會自動重試。"
