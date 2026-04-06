"""
Skill dispatch & polish operations extracted from Orchestrator.

All functions accept an ``orch`` parameter (the Orchestrator instance)
instead of ``self``, keeping the same logic but as standalone functions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def run_transcribe_guidance(message: str) -> str:
    return "🎙️ 請上傳音訊檔，或在訊息中附上可讀取的音訊檔路徑後再要求逐字稿。"


def looks_like_capability_question(message: str) -> bool:
    """Detect pure capability questions like '可以翻譯嗎' but NOT action requests like '可以幫我翻譯這段嗎'."""
    text = str(message or "").strip()
    if not text:
        return False
    # Must end with question particle
    if not re.search(r"[嗎嘛呢？\?]$", text):
        return False
    # Must contain ability-asking keywords
    if not re.search(r"(可以|可不可以|能不能|會不會|如何|怎麼|有沒有辦法|能否|可否)", text, re.IGNORECASE):
        return False
    # If message contains concrete objects/context, it's an ACTION request, not a capability question
    _has_concrete_object = bool(re.search(
        r"(案件|案號|文件|檔案|判決|合約|契約|信件|郵件|這個|這份|這段|那個|那份|那段|"
        r"幫我|給我|替我|告訴我|查一下|看一下|處理|需求|問題|做到|辦到|"
        r"摘要|翻譯|搜尋|備份|下載|上傳|分析|整理|統計|計算)",
        text
    ))
    if _has_concrete_object:
        return False
    has_payload = bool(
        re.search(r"https?://", text, re.IGNORECASE)
        or re.search(r"[A-Za-z]{4,}", text)
        or re.search(r"\d{4,}", text)
        or re.search(r"[。；;，,]", text)
    )
    return len(text) <= 36 or not has_payload


def dispatch_safe_semantic_skill(orch, user_id, message: str, skill: str, role: str, platform: str) -> tuple[bool, str]:
    orch._last_dispatch_message = message
    orch._last_dispatch_user_id = user_id

    if orch._skill_registry and looks_like_capability_question(message):
        guide = orch._skill_registry.get_capability_guide(skill)
        if guide:
            return True, guide

    if orch._skill_registry:
        handled, reply = orch._skill_registry.dispatch(
            skill, message,
            user_id=user_id, role=role,
            platform=platform, orchestrator=orch,
        )
        if handled and reply:
            return True, reply
        if handled:
            return False, ""

    return generic_skill_dispatch(orch, skill, message)


def generic_skill_dispatch(orch, skill: str, message: str) -> tuple[bool, str]:
    """
    Generic skill dispatcher: runs any skill that has an action.py via run_skill_action().
    """
    try:
        from skills.evolution.skill_genesis import run_skill_action
    except ImportError:
        return False, ""

    folder_candidates = []
    try:
        definitions_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "skills", "definitions.json")
        if os.path.exists(definitions_path):
            with open(definitions_path, "r", encoding="utf-8") as f:
                payload = json.load(f) or {}
            for tool in payload.get("tools") or []:
                if not isinstance(tool, dict) or str(tool.get("name") or "").strip() != str(skill or "").strip():
                    continue
                skill_prop = (((tool.get("parameters") or {}).get("properties") or {}).get("skill") or {})
                default_folder = str(skill_prop.get("default") or "").strip()
                if default_folder:
                    folder_candidates.append(default_folder)
                break
    except Exception as def_err:
        logger.debug(f"generic dispatch: definition lookup failed for {skill}: {def_err}")

    folder_candidates.extend([
        skill.replace("_", "-"),
        skill,
        re.sub(r"^run[_-]+", "", skill.replace("_", "-")),
        re.sub(r"^run[_-]+", "", skill),
        f"{skill.replace('_', '-')}-tw",
    ])
    seen_folders = set()
    deduped_folders = []
    for item in folder_candidates:
        folder_name = str(item or "").strip()
        if not folder_name or folder_name in seen_folders:
            continue
        seen_folders.add(folder_name)
        deduped_folders.append(folder_name)

    skill_dirs = [
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "skills", folder_name)
        for folder_name in deduped_folders
    ]
    found_dir = None
    for d in skill_dirs:
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "action.py")):
            found_dir = os.path.basename(os.path.normpath(d))
            break

    if not found_dir:
        logger.debug(f"generic dispatch: no action.py for skill '{skill}'")
        return False, ""

    logger.info(f"🔧 Generic skill dispatch: {skill} → {found_dir}")
    orch._ensure_runtime_foundations()
    started = time.perf_counter()
    action_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "skills", found_dir, "action.py")
    orch._hook_bus.pre_tool(
        f"skill:{skill}",
        input_data={"message_preview": (message or "")[:200]},
        correlation_id=orch._current_correlation_id(),
        metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
    )
    command_decision = orch._permission_enforcer.evaluate_command(f"skill:{skill}")
    if not command_decision.allowed:
        blocked = f"⚠️ 權限策略已阻擋技能執行：{command_decision.reason}"
        orch._hook_bus.post_tool(
            f"skill:{skill}", ok=False, status="denied",
            duration_ms=round((time.perf_counter() - started) * 1000, 3), error=blocked,
            correlation_id=orch._current_correlation_id(),
            metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
        )
        return True, blocked
    path_decision = orch._permission_enforcer.evaluate_path(action_path)
    if not path_decision.allowed:
        blocked = f"⚠️ 權限策略已阻擋技能執行：{path_decision.reason}"
        orch._hook_bus.post_tool(
            f"skill:{skill}", ok=False, status="denied",
            duration_ms=round((time.perf_counter() - started) * 1000, 3), error=blocked,
            correlation_id=orch._current_correlation_id(),
            metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
        )
        return True, blocked
    try:
        result = run_skill_action(
            found_dir, message,
            timeout_sec=60, auto_repair=False, auto_install_deps=True,
        )
        if result.get("success"):
            output = result.get("output", "").strip()
            if not output:
                orch._hook_bus.post_tool(
                    f"skill:{skill}", output_data="✅ 技能執行完成。", ok=True, status="handled",
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    correlation_id=orch._current_correlation_id(),
                    metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
                )
                return True, "✅ 技能執行完成。"
            polished = polish_skill_output(skill, message, output)
            orch._hook_bus.post_tool(
                f"skill:{skill}", output_data=polished, ok=True, status="handled",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                correlation_id=orch._current_correlation_id(),
                metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
            )
            return True, polished
        else:
            err = result.get("error", "unknown")
            logger.warning(f"generic dispatch failed for {skill}: {err}")
            orch._hook_bus.post_tool(
                f"skill:{skill}", ok=False, status="not_handled",
                duration_ms=round((time.perf_counter() - started) * 1000, 3), error=str(err),
                correlation_id=orch._current_correlation_id(),
                metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
            )
            return False, f"⚠️ 技能 {skill} 執行失敗：{err}"
    except Exception as e:
        logger.warning(f"generic dispatch error for {skill}: {e}")
        orch._hook_bus.post_tool(
            f"skill:{skill}", ok=False, status="error",
            duration_ms=round((time.perf_counter() - started) * 1000, 3), error=str(e),
            correlation_id=orch._current_correlation_id(),
            metadata={"dispatch_mode": "generic_subprocess", "skill_name": skill},
        )
        return False, f"⚠️ 技能 {skill} 發生錯誤，請稍後再試。"


def polish_skill_output(skill: str, user_message: str, raw_output: str) -> str:
    if len(raw_output) < 200 and not output_looks_messy(raw_output):
        return raw_output
    truncated = raw_output[:3000]
    prompt = f"""你是 MAGI 助理。以下是技能「{skill}」的原始執行結果。
請將它整理成簡潔、易讀的繁體中文回覆，適合在手機 LINE 上閱讀。

[使用者原始訊息]
{user_message}

[技能原始輸出]
{truncated}

[整理規則]
1. 保留所有重要資訊（數字、日期、名稱、結果）。
2. 移除 HTML 標籤、亂碼、debug 訊息、重複內容。
3. 用簡短段落或條列式呈現，不超過 10 行。
4. 如果原始輸出已經很乾淨，直接保留原文即可，不要畫蛇添足。
5. 不要加上「以下是整理後的結果」之類的前綴，直接給內容。
6. 不要使用 Markdown 語法（如 **粗體**、`程式碼`、### 標題）。純文字即可。
"""
    try:
        from skills.bridge.grounded_ai import _generate
        polished = _generate(prompt, temperature=0.15, timeout=90, num_ctx=4096)
        if polished and len(polished) > 10:
            return polished
    except Exception as e:
        logger.warning(f"Polish LLM failed for {skill}: {e}")
    return basic_cleanup(raw_output)


def output_looks_messy(text: str) -> bool:
    if re.search(r"<[a-zA-Z][^>]*>", text):
        return True
    if re.search(r"\\n{3,}", text):
        return True
    if text.count("\n") > 15:
        return True
    noise = sum(1 for c in text if ord(c) > 127 and not ('\u4e00' <= c <= '\u9fff')
                 and not ('\u3000' <= c <= '\u303f') and not ('\uff00' <= c <= '\uffef'))
    if len(text) > 50 and noise / len(text) > 0.3:
        return True
    return False


def basic_cleanup(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", "", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{3,}", " ", cleaned)
    return cleaned.strip()[:2000]


def try_safe_semantic_skill_route(orch, user_id: str, message: str, role: str, platform: str) -> tuple[bool, str]:
    text = str(message or "").strip()
    if not text or text.startswith("/") or text.startswith("!") or text.startswith("@MAGI"):
        return False, ""
    if len(text) > 600:
        return False, ""

    safe_skills = {
        "web_search": "command", "translate_document": "command",
        "pdf_summarize": "command", "audio_transcribe": "command",
        "image_generate": "command", "judgment_search": "command",
        "run_judgment_collector": "command", "rss_subscribe": "command",
        "memory_search": "command", "transcript_query": "command",
        "pdf_annotate": "command", "stock_briefing": "command",
        "court_hearing": "command", "judgment_trend": "command",
        "labor_law_calc": "command", "tri_sage_translate": "command",
        "summarize_text": "command", "tri_sage_transcribe": "command",
        # 2026-04-06: Added missing user-facing skills
        "run_contract_review": "command", "run_transcript_indexer": "command",
        "run_market_briefing": "command", "run_statutes_vdb": "command",
    }
    min_conf = {"phrase": 0.30, "semantic": 0.36, "llm": 0.46}

    try:
        from skills.bridge.semantic_router import route as _semantic_route, suggest_trigger
    except Exception:
        return False, ""

    try:
        sr = _semantic_route(text)
    except Exception as e:
        logger.debug(f"safe semantic route skipped: {e}")
        return False, ""
    if not sr:
        return False, ""

    skill = str(sr.get("skill") or "").strip()
    method = str(sr.get("method") or "semantic").strip()
    confidence = float(sr.get("confidence") or 0.0)
    if skill not in safe_skills:
        return False, ""
    if confidence < float(min_conf.get(method, 0.38)):
        return False, ""

    synthetic = suggest_trigger(skill, text)
    route_mode = safe_skills[skill]
    orch._append_route_trace(
        str(user_id or ""), str(platform or ""),
        "semantic_primary", skill,
        {"confidence": confidence, "method": method, "route_mode": route_mode,
         "reason": str(sr.get("reason") or ""), "candidates": list(sr.get("candidates") or [])},
    )
    handled, direct_reply = dispatch_safe_semantic_skill(orch, user_id, text, skill, role, platform)
    if handled:
        orch._append_route_trace(
            str(user_id or ""), str(platform or ""),
            "semantic_primary_dispatch", skill, {"dispatch": "direct"},
        )
        return True, direct_reply or ""
    if route_mode == "query":
        reply = orch._handle_query(user_id, text, platform_hint=platform)
        return (bool(reply), reply or "")

    reply = orch._handle_command(user_id, synthetic, role=role, platform=platform)
    return (bool(reply), reply or "")
