"""
Chat/query processing pipeline extracted from Orchestrator.

Contains: handle_chat_async, handle_query, build_conversation_history,
compress_history, and related helpers.

All functions accept an `orch` parameter (the Orchestrator instance)
instead of `self`.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone

logger = logging.getLogger("Orchestrator")

try:
    from api.session.conversation_history import get_conversation_history
except Exception:
    get_conversation_history = None


# ---------------------------------------------------------------------------
# History / compression helpers
# ---------------------------------------------------------------------------

# Thresholds (mirror Orchestrator class attrs)
_HISTORY_COMPRESS_AT = 30
_HISTORY_COMPRESS_KEEP = 10
_HISTORY_COMPRESS_TIMEOUT = 15
_HISTORY_TOKEN_BUDGET = 2400
_SUMMARY_MAX_TOKENS = 300


def estimate_tokens(text: str) -> int:
    """Estimate token count for mixed CJK / Latin text."""
    if not text:
        return 0
    cjk = 0
    latin_chars = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
                or 0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F
                or 0xFF00 <= cp <= 0xFFEF):
            cjk += 1
        else:
            latin_chars += 1
    return int(cjk * 1.5) + (latin_chars // 4) + 1


def append_history(orch, user_id, role, content):
    orch._ensure_runtime_foundations()
    text = (content or "").strip()
    if not text:
        return
    if len(text) > 800:
        text = text[:800] + "...(truncated)"
    user_hist = orch.user_history[user_id]
    if user_hist:
        last = user_hist[-1]
        if last.get("role") == role and last.get("content") == text:
            return
    ts = datetime.now(timezone.utc).isoformat()
    user_hist.append({"role": role, "content": text, "ts": ts})
    try:
        orch._session_store.append_message(
            str(user_id or ""),
            str(role or ""),
            text,
            source="raw_history",
            metadata={"ts": ts},
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "append_history", exc_info=True)
    try:
        if get_conversation_history and os.environ.get("MAGI_ASSISTANT_MEMORY_LAYER1", "1").strip().lower() not in {"0", "false", "no", "off"}:
            get_conversation_history().append(str(user_id or ""), str(role or ""), text, metadata={"ts": ts})
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "append_history_layer1", exc_info=True)
    if len(user_hist) >= _HISTORY_COMPRESS_AT:
        compress_history(orch, user_id)
    elif len(user_hist) >= _HISTORY_COMPRESS_KEEP + 4:
        total_tokens = sum(
            estimate_tokens(m.get("content", "")) for m in user_hist
        )
        if total_tokens >= _HISTORY_TOKEN_BUDGET:
            compress_history(orch, user_id)


def compress_history(orch, user_id):
    """Summarize oldest messages to prevent context overflow."""
    user_hist = orch.user_history[user_id]
    all_msgs = list(user_hist)

    total_tokens = sum(estimate_tokens(m.get("content", "")) for m in all_msgs)
    if len(all_msgs) < _HISTORY_COMPRESS_KEEP + 2 and total_tokens < _HISTORY_TOKEN_BUDGET:
        return

    to_compress = all_msgs[: -_HISTORY_COMPRESS_KEEP]
    keep_msgs = all_msgs[-_HISTORY_COMPRESS_KEEP:]
    if not to_compress:
        return

    raw_lines = []
    token_count = 0
    for m in to_compress:
        line = f"{m['role']}: {m['content']}"
        line_tokens = estimate_tokens(line)
        if token_count + line_tokens > 1500:
            raw_lines.append(f"...(\u7565 {len(to_compress) - len(raw_lines)} \u5247)")
            break
        raw_lines.append(line)
        token_count += line_tokens
    raw_text = "\n".join(raw_lines)

    with orch._history_summaries_lock:
        prev_summary = orch._history_summaries.get(user_id, "")

    summary_prompt = (
        "\u4f60\u662f\u5c0d\u8a71\u80cc\u666f\u6458\u8981\u5f15\u64ce\u3002\u8acb\u5c07\u4ee5\u4e0b\u5c0d\u8a71\u58d3\u7e2e\u70ba\u7d50\u69cb\u5316\u80cc\u666f\u6458\u8981\uff08\u7e41\u9ad4\u4e2d\u6587\uff0c\u975e\u539f\u6587\u3001\u50c5\u4f9b\u5ef6\u7e8c\u4e0a\u4e0b\u6587\uff09\uff0c"
        "\u4e0d\u5f97\u88dc\u5beb\u539f\u6587\u6c92\u6709\u7684\u7d30\u7bc0\uff0c\u4e5f\u4e0d\u8981\u628a\u63a8\u8ad6\u5beb\u6210\u4e8b\u5be6\u3002"
        "\u683c\u5f0f\u5982\u4e0b\uff1a\n"
        "<summary provenance=\"derived\">\n"
        "\u3010\u4e3b\u984c\u3011\u4e00\u53e5\u8a71\u63cf\u8ff0\u5c0d\u8a71\u4e3b\u984c\n"
        "\u3010\u95dc\u9375\u6c7a\u7b56\u3011\u689d\u5217\u91cd\u8981\u7684\u6307\u4ee4\u3001\u6c7a\u7b56\u6216\u7d50\u8ad6\n"
        "\u3010\u5f85\u8fa6/\u672a\u5b8c\u6210\u3011\u5982\u6709\u672a\u5b8c\u6210\u4e8b\u9805\u8acb\u5217\u51fa\n"
        "</summary>\n\n"
        "\u898f\u5247\uff1a\u5ffd\u7565\u5ba2\u5957\u8a71\u548c\u91cd\u8907\u5167\u5bb9\uff0c\u53ea\u4fdd\u7559\u6709\u610f\u7fa9\u7684\u8cc7\u8a0a\uff1b"
        "\u5982\u679c\u8cc7\u8a0a\u4e0d\u78ba\u5b9a\uff0c\u8acb\u660e\u78ba\u4fdd\u7559\u4e0d\u78ba\u5b9a\u6027\u3002"
        + (("(\u5148\u524d\u5c0d\u8a71\u6458\u8981\uff1a%s)" % prev_summary[:400]) if prev_summary else "")
        + f"\n\n\u5c0d\u8a71\u5167\u5bb9\uff1a\n{raw_text}"
    )

    new_summary = ""
    try:
        from skills.bridge.melchior_bridge import generate_text
        resp = generate_text(summary_prompt)
        if resp:
            new_summary = resp.strip()
            if estimate_tokens(new_summary) > _SUMMARY_MAX_TOKENS:
                chars_budget = _SUMMARY_MAX_TOKENS * 2
                new_summary = new_summary[:chars_budget] + "..."
    except Exception:
        logging.getLogger(__name__).debug(
            "compress_history LLM failed for %s", user_id, exc_info=True
        )

    if not new_summary:
        topics = []
        for m in to_compress:
            content = m.get("content", "").strip()
            if m.get("role") == "user" and content:
                topics.append(content[:60])
        topic_str = "\uff1b".join(topics[:5]) if topics else "\uff08\u591a\u8f2a\u5c0d\u8a71\uff09"
        new_summary = (
            "<summary provenance=\"derived\">\n"
            "\u3010\u6ce8\u610f\u3011\u6b64\u70ba\u975e\u539f\u6587\u80cc\u666f\u6458\u8981\uff0c\u50c5\u4f9b\u5ef6\u7e8c\u4e0a\u4e0b\u6587\uff0c\u4e0d\u53ef\u8996\u70ba\u9010\u5b57\u7d00\u9304\u3002\n"
            f"\u3010\u4e3b\u984c\u3011{topic_str}\n"
            f"\u3010\u8a0a\u606f\u6578\u3011\u5df2\u58d3\u7e2e {len(to_compress)} \u5247\u5c0d\u8a71\n"
            + (("\u3010\u5148\u524d\u6458\u8981\u3011" + prev_summary[:200]) if prev_summary else "") + "\n"
            "</summary>"
        )

    with orch._history_summaries_lock:
        orch._history_summaries[user_id] = new_summary
        if len(orch._history_summaries) > orch._history_summaries_maxsize:
            oldest_keys = list(orch._history_summaries.keys())[:len(orch._history_summaries) // 5]
            for k in oldest_keys:
                orch._history_summaries.pop(k, None)
    try:
        orch._ensure_runtime_foundations()
        orch._session_store.add_summary(
            str(user_id or ""),
            new_summary,
            source="history_compression",
            authoritative=False,
            metadata={"compressed_count": len(to_compress)},
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "compress_history_session", exc_info=True)

    user_hist.clear()
    for m in keep_msgs:
        user_hist.append(m)


def build_conversation_history(orch, user_id, limit=12) -> str:
    """Build conversation history string for LLM, with token budget control."""
    try:
        from api.tw_output_guard import mark_non_authoritative_context as _mark_non_authoritative_context
    except Exception:
        _mark_non_authoritative_context = None

    history = list(orch.user_history.get(user_id, []))
    try:
        if get_conversation_history and os.environ.get("MAGI_ASSISTANT_MEMORY_LAYER1", "1").strip().lower() not in {"0", "false", "no", "off"}:
            layer1 = get_conversation_history().last_n(str(user_id or ""), max(limit, 20))
            if layer1:
                history = layer1
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "build_conversation_history_layer1", exc_info=True)
    with orch._history_summaries_lock:
        summary = orch._history_summaries.get(user_id, "")
    if not history and not summary:
        return ""

    parts = []
    token_used = 0

    if summary:
        marked_summary = summary
        if _mark_non_authoritative_context:
            marked_summary = _mark_non_authoritative_context(
                summary,
                label="\u6b77\u53f2\u6458\u8981",
                source="\u6a21\u578b\u58d3\u7e2e",
            )
        continuation = (
            "\u4ee5\u4e0b\u5167\u5bb9\u662f\u5ef6\u7e8c\u7528\u7684\u80cc\u666f\u6458\u8981\uff0c\u4e0d\u662f\u9010\u5b57\u539f\u6587\uff1b"
            "\u5b83\u7684\u6b0a\u91cd\u4f4e\u65bc\u6700\u8fd1\u7684\u539f\u6587\u8a0a\u606f\uff0c\u82e5\u8207\u539f\u6587\u885d\u7a81\uff0c\u4ee5\u539f\u6587\u70ba\u6e96\u3002\n"
            f"{marked_summary}"
        )
        parts.append(continuation)
        token_used += estimate_tokens(continuation)

    selected = []
    for msg in reversed(history[-limit:]):
        line = f"{msg['role']}: {msg['content']}"
        line_tokens = estimate_tokens(line)
        if token_used + line_tokens > _HISTORY_TOKEN_BUDGET:
            break
        selected.append(line)
        token_used += line_tokens
    selected.reverse()
    parts += selected

    return "\n".join(parts)


def record_assistant_reply(orch, user_id, content):
    """Public hook for server/bot layers to record replies from all return paths."""
    if not content:
        return
    normalized = str(content).replace("|||IMAGE_PATH|||", " [IMAGE] ")
    # Use orch._append_history so that mocks/patches on the instance method work.
    orch._append_history(user_id, "assistant", normalized)
    try:
        capture_assistant = os.environ.get("MAGI_CAPTURE_ASSISTANT_CHATLOG", "0").strip().lower()
        if capture_assistant in {"1", "true", "yes", "on"}:
            orch._maybe_capture_chatlog(str(user_id or ""), "unknown", "assistant", normalized)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "record_assistant_reply", exc_info=True)


# ---------------------------------------------------------------------------
# Query handler
# ---------------------------------------------------------------------------

def handle_query(orch, user_id, message, platform_hint="LINE") -> str:
    """Routes queries to RAG (Keeper) or Search."""
    from skills.bridge.grounded_ai import ask_casper

    try:
        from api.tw_output_guard import mark_unverified_reply as _mark_unverified_reply
    except Exception:
        _mark_unverified_reply = None

    logger.info(f"\U0001f50d Routing Query from {user_id} to Grounded AI...")
    history = build_conversation_history(orch, user_id, limit=8)
    force_research = any(k in message.lower() for k in [
        "\u6700\u65b0", "today", "news", "2026", "\u50f9\u683c",
        "\u5929\u6c23", "\u6c23\u6eab", "weather", "\u4e0a\u7db2", "\u67e5\u4e00\u4e0b", "\u73fe\u5728",
        "\u5e6b\u6211\u67e5", "\u641c\u5c0b",
    ])
    timeout_sec = int(os.environ.get("MAGI_QUERY_TIMEOUT_SEC", "120") or "120")
    async_enabled = str(os.environ.get("MAGI_QUERY_ASYNC", "1")).strip().lower() in {"1", "true", "yes", "on"}
    async_trigger_chars = int(os.environ.get("MAGI_QUERY_ASYNC_TRIGGER_CHARS", "500") or "500")
    async_timeout_sec = int(os.environ.get("MAGI_QUERY_ASYNC_TIMEOUT_SEC", "900") or "900")

    mode_banner = orch._brain_runtime_banner()
    if async_enabled and len(message or "") >= max(400, async_trigger_chars) and getattr(orch, "notification_callback", None):
        uid = str(user_id or "")
        platform_name = str(platform_hint or "LINE")

        def _run_query_background():
            try:
                reply = orch._call_with_timeout(
                    lambda: ask_casper(message, conversation_history=history, force_research=force_research),
                    async_timeout_sec,
                    f"\u26a0\ufe0f \u67e5\u8a62\u903e\u6642\uff08>{async_timeout_sec}s\uff09\uff0c\u76ee\u524d\u6c92\u6709\u53ef\u9a57\u8b49\u7d50\u679c\u3002",
                    "query-async",
                )
                final_text = str(reply or "").strip() or "\u26a0\ufe0f \u67e5\u8a62\u5b8c\u6210\uff0c\u4f46\u6c92\u6709\u53ef\u7528\u8f38\u51fa\u3002"
                if "\u67e5\u8a62\u903e\u6642\uff08>" in final_text:
                    orch._ensure_runtime_foundations()
                    orch._hook_bus.fallback(
                        "query-timeout",
                        stage="query_async",
                        reason=f"\u67e5\u8a62\u903e\u6642\uff08>{async_timeout_sec}s\uff09",
                        detail={"user_id": uid, "platform": platform_name},
                        correlation_id=orch._current_correlation_id(),
                    )
                    safe_reply = "\u76ee\u524d\u6c92\u6709\u53ef\u9a57\u8b49\u7d50\u679c\uff0c\u8acb\u7a0d\u5f8c\u91cd\u8a66\uff0c\u6216\u628a\u554f\u984c\u7e2e\u5c0f\u6210\u66f4\u5177\u9ad4\u7684\u4e00\u500b\u4e8b\u5be6\u9ede\u3002"
                    if _mark_unverified_reply:
                        final_text = _mark_unverified_reply(
                            safe_reply,
                            reason=f"\u67e5\u8a62\u903e\u6642\uff08>{async_timeout_sec}s\uff09",
                        )
                    else:
                        final_text = f"\u26a0\ufe0f \u67e5\u8a62\u903e\u6642\uff08>{async_timeout_sec}s\uff09\n{safe_reply}"
                final_text = f"{mode_banner}\n{final_text}"
            except Exception as e:
                orch._ensure_runtime_foundations()
                orch._hook_bus.fallback(
                    "query-exception",
                    stage="query_async",
                    reason=str(e)[:200],
                    detail={"user_id": uid, "platform": platform_name},
                    correlation_id=orch._current_correlation_id(),
                )
                final_text = f"{mode_banner}\n\u274c \u67e5\u8a62\u5931\u6557\uff1a{e}"
            try:
                orch.notification_callback(uid, final_text, platform_name)
            except Exception as notify_err:
                logger.warning(f"Query async callback failed: {notify_err}")

        orch._bg_task_pool.submit(_run_query_background)
        return f"{mode_banner}\n\u23f3 \u5167\u5bb9\u8f03\u9577\uff0c\u6211\u5df2\u6539\u6210\u80cc\u666f\u67e5\u8a62\u3002\u5b8c\u6210\u5f8c\u6703\u4e3b\u52d5\u56de\u8986\u7d50\u679c\u3002"

    reply = orch._call_with_timeout(
        lambda: ask_casper(message, conversation_history=history, force_research=force_research),
        timeout_sec,
        f"\u26a0\ufe0f \u67e5\u8a62\u903e\u6642\uff08>{timeout_sec}s\uff09\uff0c\u76ee\u524d\u6c92\u6709\u53ef\u9a57\u8b49\u7d50\u679c\u3002",
        "query",
    )
    reply = str(reply or "").strip() or "\u26a0\ufe0f \u67e5\u8a62\u5b8c\u6210\uff0c\u4f46\u76ee\u524d\u6c92\u6709\u53ef\u7528\u8f38\u51fa\u3002"
    if "\u67e5\u8a62\u903e\u6642\uff08>" in reply:
        orch._ensure_runtime_foundations()
        orch._hook_bus.fallback(
            "query-timeout",
            stage="query",
            reason=f"\u67e5\u8a62\u903e\u6642\uff08>{timeout_sec}s\uff09",
            detail={"user_id": str(user_id or ""), "platform": str(platform_hint or "LINE")},
            correlation_id=orch._current_correlation_id(),
        )
        safe_reply = "\u76ee\u524d\u6c92\u6709\u53ef\u9a57\u8b49\u7d50\u679c\uff0c\u8acb\u7a0d\u5f8c\u91cd\u8a66\uff0c\u6216\u628a\u554f\u984c\u7e2e\u5c0f\u6210\u66f4\u5177\u9ad4\u7684\u4e00\u500b\u4e8b\u5be6\u9ede\u3002"
        if _mark_unverified_reply:
            reply = _mark_unverified_reply(
                safe_reply,
                reason=f"\u67e5\u8a62\u903e\u6642\uff08>{timeout_sec}s\uff09",
            )
        else:
            reply = f"\u26a0\ufe0f \u67e5\u8a62\u903e\u6642\uff08>{timeout_sec}s\uff09\n{safe_reply}"
    return f"{mode_banner}\n{reply}"


# ---------------------------------------------------------------------------
# Chat handler
# ---------------------------------------------------------------------------

def handle_chat_async(orch, user_id, message, platform_hint="LINE") -> str:
    """Routes chat to LLM (Casper/oMLX) for generation."""
    logger.info(f"\U0001f4ac Chatting with {user_id}...")

    # 2026-04-24：在 main thread（request thread）讀 flask.g.heavy_opt_in，顯式傳給 chat_casper，
    # 避開 ThreadPoolExecutor 子 thread 讀不到 flask.g 的 P1-2 bug
    _heavy_flag = False
    try:
        from flask import g as _g_hdr
        _heavy_flag = bool(getattr(_g_hdr, "heavy_opt_in", False))
    except Exception:
        _heavy_flag = False

    # Heavy task awareness
    heavy_tasks = orch.get_active_heavy_tasks()
    if heavy_tasks:
        labels = "\u3001".join(t["label"] for t in heavy_tasks[:3])
        elapsed = max(int(time.time() - min(t["start_ts"] for t in heavy_tasks)), 0)
        logger.info(f"\U0001f3cb\ufe0f Chat deferred: oMLX busy with {labels} ({elapsed}s)")
        _uid = str(user_id or "")
        _platform = str(platform_hint or "LINE")
        _msg = message

        def _deferred_chat():
            orch._heavy_task_done_event.clear()
            orch._heavy_task_done_event.wait(timeout=180)
            try:
                history = build_conversation_history(orch, _uid, limit=8)
                from skills.bridge.grounded_ai import chat_casper
                reply = chat_casper(_msg, conversation_history=history, heavy=_heavy_flag)
                reply = str(reply or "").strip() or "\u62b1\u6b49\u8b93\u4f60\u4e45\u7b49\u4e86\uff0c\u4f46\u76ee\u524d\u6c92\u6709\u53ef\u7528\u8f38\u51fa\u3002"
                banner = orch._brain_runtime_banner()
                orch.notification_callback(_uid, f"{banner}\n{reply}", _platform)
            except Exception as e:
                logger.warning(f"Deferred chat failed: {e}")
                orch.notification_callback(_uid, "\u26a0\ufe0f \u5ef6\u9072\u56de\u8986\u5931\u6557\uff0c\u8acb\u518d\u8a66\u4e00\u6b21\u3002", _platform)

        if getattr(orch, "notification_callback", None):
            threading.Thread(target=_deferred_chat, daemon=True).start()
            return f"\u23f3 \u6211\u76ee\u524d\u6b63\u5728\u8655\u7406 **{labels}**\uff08\u5df2\u9032\u884c {elapsed} \u79d2\uff09\uff0c\u5b8c\u6210\u5f8c\u6703\u7acb\u523b\u56de\u8986\u4f60\u7684\u8a0a\u606f\u3002"

    history = build_conversation_history(orch, user_id, limit=8)
    from skills.bridge.grounded_ai import chat_casper
    timeout_sec = int(os.environ.get("MAGI_CHAT_TIMEOUT_SEC", "150") or "150")
    async_enabled = str(os.environ.get("MAGI_CHAT_ASYNC", "1")).strip().lower() in {"1", "true", "yes", "on"}
    async_trigger_chars = int(os.environ.get("MAGI_CHAT_ASYNC_TRIGGER_CHARS", "500") or "500")
    async_timeout_sec = int(os.environ.get("MAGI_CHAT_ASYNC_TIMEOUT_SEC", "900") or "900")

    mode_banner = orch._brain_runtime_banner()
    if async_enabled and len(message or "") >= max(400, async_trigger_chars) and getattr(orch, "notification_callback", None):
        uid = str(user_id or "")
        platform_name = str(platform_hint or "LINE")

        def _run_chat_background():
            try:
                reply = orch._call_with_timeout(
                    lambda: chat_casper(message, conversation_history=history, heavy=_heavy_flag),
                    async_timeout_sec,
                    f"\u26a0\ufe0f \u9577\u8a0a\u606f\u8655\u7406\u903e\u6642\uff08>{async_timeout_sec}s\uff09\u3002",
                    "chat-async",
                )
                final_text = str(reply or "").strip() or "\u26a0\ufe0f \u9577\u8a0a\u606f\u8655\u7406\u5b8c\u6210\uff0c\u4f46\u6c92\u6709\u53ef\u7528\u8f38\u51fa\u3002"
                final_text = f"{mode_banner}\n{final_text}"
            except Exception as e:
                logger.warning(f"Chat async background failed: {e}")
                final_text = f"{mode_banner}\n\u274c \u9577\u8a0a\u606f\u8655\u7406\u5931\u6557\uff0c\u8acb\u518d\u8a66\u4e00\u6b21\u3002"
            try:
                orch.notification_callback(uid, final_text, platform_name)
            except Exception as notify_err:
                logger.warning(f"Chat async callback failed: {notify_err}")

        threading.Thread(target=_run_chat_background, daemon=True).start()
        return f"{mode_banner}\n\u23f3 \u554f\u984c\u5167\u5bb9\u8f03\u9577\uff0c\u6211\u5df2\u6539\u6210\u80cc\u666f\u8655\u7406\u3002\u5b8c\u6210\u5f8c\u6703\u4e3b\u52d5\u56de\u8986\u7d50\u679c\u3002"

    reply = orch._call_with_timeout(
        lambda: chat_casper(message, conversation_history=history, heavy=_heavy_flag),
        timeout_sec,
        f"\u26a0\ufe0f \u6211\u9019\u908a\u56de\u8986\u903e\u6642\uff08>{timeout_sec}s\uff09\uff0c\u8acb\u518d\u8a66\u4e00\u6b21\uff0c\u6216\u6539\u554f\u300c\u72c0\u614b\u300d\u8b93\u6211\u5148\u505a\u5065\u5eb7\u6aa2\u67e5\u3002",
        "chat",
    )
    reply = str(reply or "").strip() or "\u26a0\ufe0f \u56de\u8986\u5b8c\u6210\uff0c\u4f46\u76ee\u524d\u6c92\u6709\u53ef\u7528\u8f38\u51fa\u3002"
    if "\u56de\u8986\u903e\u6642\uff08>" in reply:
        try:
            _gw = orch._inference_gw
            quick = _gw.chat(
                f"\u8acb\u7528\u7e41\u9ad4\u4e2d\u6587\u76f4\u63a5\u56de\u7b54\u4e0b\u5217\u8a0a\u606f\uff0c\u7c21\u6f54\u4f46\u5177\u9ad4\uff1a\n\n{message}",
                task_type="general",
                timeout=max(8, min(14, timeout_sec // 5)),
            )
            qtxt = str((quick or {}).get("response") or "").strip()
            _degraded_markers = ("\u7cfb\u7d71\u964d\u7d1a\u56de\u8986", "\u672c\u6a5f\u6a21\u578b\u903e\u6642", "\u8acb\u7a0d\u5f8c\u91cd\u8a66")
            if quick.get("success") and qtxt and not any(m in qtxt for m in _degraded_markers):
                reply = f"\u26a0\ufe0f \u56de\u8986\u903e\u6642\uff0c\u5148\u63d0\u4f9b\u5feb\u901f\u56de\u8986\uff1a\n{qtxt}"
            else:
                reply = "\u26a0\ufe0f \u76ee\u524d\u6a21\u578b\u5fd9\u7962\u4e2d\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u4e00\u6b21\u3002"
        except Exception as quick_err:
            logger.warning(f"Chat timeout quick fallback failed: {quick_err}")
            reply = "\u26a0\ufe0f \u76ee\u524d\u6a21\u578b\u5fd9\u7962\u4e2d\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\u4e00\u6b21\u3002"
    return f"{mode_banner}\n{reply}"
