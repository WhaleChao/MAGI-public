"""
Memory capture & rule confirmation operations extracted from Orchestrator.

All functions accept an ``orch`` parameter (the Orchestrator instance)
instead of ``self``, keeping the same logic but as standalone functions.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger("Orchestrator")


def is_ambiguous_rule(text: str) -> bool:
    """Heuristic: treat 'rule-like' text as ambiguous if it looks like a question/hypothetical."""
    s = (text or "").strip()
    if not s:
        return False
    low = s.lower()
    if len(s) < 12:
        return True
    if any(x in s for x in ["？", "?", "嗎", "呢", "要不要", "可不可以", "行不行"]):
        return True
    if low.startswith(("如果", "假如", "也許", "可能", "萬一", "看情況")):
        return True
    if any(x in s for x in ["或", "還是", "任一", "其中一個", "二選一"]):
        return True
    if any(x in s for x in ["舉例", "例如", "比如", "ex:", "e.g."]):
        return True
    return False


def handle_memory_confirmation_if_any(orch, user_id: str, platform: str, message: str) -> tuple[bool, str]:
    """Handle user confirmation for pending rule-memory capture."""
    msg = (message or "").strip()
    if not msg:
        return False, ""
    pending = orch._load_memory_pending()
    key = orch._pending_key(user_id, platform)
    entry = pending.get(key) if isinstance(pending, dict) else None
    if not isinstance(entry, dict):
        return False, ""

    now = time.time()
    exp = float(entry.get("expires_at", 0.0) or 0.0)
    if exp and now > exp:
        pending.pop(key, None)
        orch._save_memory_pending(pending)
        return True, "⏱️ 剛剛那筆「是否要記成規則」已過期。你可以再貼一次，我再幫你確認。"

    low = msg.lower()
    accept = low in {"要", "好", "是", "對", "ok", "yes", "y", "記住"} or msg.startswith(("要，", "要:", "要：", "好，", "好:", "好："))
    reject = low in {"不要", "不用", "不必", "取消", "no", "n"} or msg.startswith(("不要", "不用", "取消"))
    edit_prefixes = ("改成：", "改成:", "修正：", "修正:", "更正：", "更正:", "補充：", "補充:")

    if reject:
        pending.pop(key, None)
        orch._save_memory_pending(pending)
        return True, "好，我不會把那句話記成規則。"

    if msg.startswith(edit_prefixes):
        new_text = msg.split(":", 1)[-1].strip() if ":" in msg else msg.split("：", 1)[-1].strip()
        new_text = (new_text or "").strip()
        if not new_text:
            return True, "❓ 你想改成什麼版本？請用 `改成：...` 把完整句子貼上。"
        entry["content"] = orch._redact_secrets(new_text)[:800]
        entry["updated_at"] = now
        pending[key] = entry
        orch._save_memory_pending(pending)
        return True, f"收到，我先改成這句：\n「{entry['content']}」\n\n要把它記成規則嗎？回我：`要` / `不要`"

    if accept:
        try:
            from skills.memory.mem_bridge import remember
            from skills.evolution.skill_genesis import validate_skill_safety

            content = orch._redact_secrets(str(entry.get("content") or ""))[:800]
            ok, _violations = validate_skill_safety(content)
            if not ok:
                pending.pop(key, None)
                orch._save_memory_pending(pending)
                return True, "🛡️ 這句話觸發鐵穹限制，我不會記成規則。"
            src = f"user_rule|platform={platform}|user={user_id}|ts={datetime.now(timezone.utc).isoformat()}"
            remember(content, source=src)
        except Exception as e:
            return True, f"❌ 記憶寫入失敗：{e}"
        pending.pop(key, None)
        orch._save_memory_pending(pending)
        return True, "✅ 好，我已把這句話記成你的規則。"

    return False, ""


def maybe_capture_user_rules(orch, user_id: str, platform: str, message: str):
    """Persist user-provided rules/preferences into long-term memory (for ALL users)."""
    if os.environ.get("MAGI_CAPTURE_USER_RULES", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    msg = (message or "").strip()
    if not msg:
        return
    low = msg.lower()
    rule_markers = [
        "規則", "以後", "請你", "務必", "一定", "不要", "不允許", "禁止", "永遠", "一律",
        "rule", "always", "never", "must", "do not", "don't",
    ]
    if not any(m in msg or m in low for m in rule_markers):
        return
    now = time.time()
    key = (str(user_id or ""), str(platform or ""),)
    with orch._rule_last_write_lock:
        last = float(orch._rule_last_write.get(key, 0.0) or 0.0)
        if now - last < float(os.environ.get("MAGI_RULE_MEMORY_MIN_INTERVAL_SEC", "45")):
            return
        orch._rule_last_write[key] = now
    try:
        from skills.memory.mem_bridge import remember
        from skills.evolution.skill_genesis import validate_skill_safety

        content = orch._redact_secrets(msg)[:800]
        ok, _violations = validate_skill_safety(content)
        if not ok:
            return
        if is_ambiguous_rule(content):
            pending = orch._load_memory_pending()
            pkey = orch._pending_key(user_id, platform)
            pending[pkey] = {
                "kind": "user_rule",
                "content": content,
                "created_at": time.time(),
                "updated_at": time.time(),
                "expires_at": time.time() + float(os.environ.get("MAGI_MEMORY_CONFIRM_TTL_SEC", "600")),
            }
            orch._save_memory_pending(pending)
            return "ASK_CONFIRM"
        src = f"user_rule|platform={platform}|user={user_id}|ts={datetime.now(timezone.utc).isoformat()}"
        remember(
            content,
            source=src,
            metadata={
                "verified": True,
                "confidence": 0.98,
                "source_type": "user_rule",
                "role": "user",
            },
        )
    except Exception as e:
        logger.warning(f"Rule memory capture skipped: {e}")
    return


def maybe_capture_chatlog(orch, user_id: str, platform: str, role: str, content: str):
    """Persist chat turns for ALL users."""
    orch._ensure_runtime_foundations()
    if os.environ.get("MAGI_CAPTURE_CHATLOG", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    role_name = str(role or "").strip().lower()
    if role_name and role_name != "user":
        capture_assistant = os.environ.get("MAGI_CAPTURE_ASSISTANT_CHATLOG", "0").strip().lower()
        if capture_assistant not in {"1", "true", "yes", "on"}:
            return
    text = (content or "").strip()
    if not text:
        return
    now = time.time()
    key = (str(user_id or ""), str(platform or ""), role_name)
    with orch._chatlog_last_write_lock:
        last = float(orch._chatlog_last_write.get(key, 0.0) or 0.0)
        if now - last < float(os.environ.get("MAGI_CHATLOG_MIN_INTERVAL_SEC", "25")):
            return
        orch._chatlog_last_write[key] = now
        if len(orch._chatlog_last_write) > orch._chatlog_last_write_maxsize:
            sorted_keys = sorted(orch._chatlog_last_write, key=orch._chatlog_last_write.get)
            for k in sorted_keys[:len(sorted_keys) // 5]:
                orch._chatlog_last_write.pop(k, None)
    try:
        from skills.memory.mem_bridge import remember
        from skills.evolution.skill_genesis import validate_skill_safety

        safe = orch._redact_secrets(text)
        safe = safe[:1200]
        ok, _violations = validate_skill_safety(safe)
        if not ok:
            orch._hook_bus.memory_write(
                "chatlog", content=safe, accepted=False,
                user_id=str(user_id or ""), platform=str(platform or ""),
                source_signature="chatlog|rejected",
                correlation_id=orch._current_correlation_id(),
                metadata={"reason": "validate_skill_safety_failed", "role": role_name},
            )
            return
        src = f"chatlog|platform={platform}|user={user_id}|role={role}|ts={datetime.now(timezone.utc).isoformat()}"
        remember(
            safe, source=src,
            metadata={
                "verified": role_name == "user",
                "confidence": 0.82 if role_name == "user" else 0.18,
                "source_type": "chatlog",
                "role": role_name,
                "derived_from": "" if role_name == "user" else "assistant_reply",
            },
        )
        orch._hook_bus.memory_write(
            "chatlog", content=safe, accepted=True,
            user_id=str(user_id or ""), platform=str(platform or ""),
            source_signature=src, memory_key="chatlog",
            correlation_id=orch._current_correlation_id(),
            metadata={"role": role_name},
        )
    except Exception as e:
        try:
            orch._hook_bus.memory_write(
                "chatlog", content=(content or "").strip()[:1200], accepted=False,
                user_id=str(user_id or ""), platform=str(platform or ""),
                source_signature="chatlog|error",
                correlation_id=orch._current_correlation_id(),
                metadata={"error": str(e)[:200], "role": role_name},
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "maybe_capture_chatlog", exc_info=True)
        logger.warning(f"Chatlog memory capture skipped: {e}")
