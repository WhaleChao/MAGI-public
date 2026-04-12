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
    # If message contains concrete objects/context, it's an ACTION request, not a capability question.
    # Only match true object nouns and demonstratives that point to actual content.
    # Action verbs (摘要, 翻譯, etc.) and request phrases (幫我, 給我, etc.) are
    # intentionally excluded — "可以幫我摘要嗎" with no content is a capability question.
    _has_concrete_object = bool(re.search(
        r"(案件|案號|文件|檔案|判決|合約|契約|信件|郵件|"
        r"這個|這份|這段|那個|那份|那段|"
        r"查一下|看一下|處理|需求|問題)",
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
            return handled, reply

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
    if re.search(r"\n{3,}", text):
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


# ── doc-producer dispatch ──

def dispatch_doc_producer(orch, user_id, message, platform="LINE"):
    # type: (object, object, str, str) -> Optional[str]
    """Parse a doc-producer intent from the user message and run the skill."""
    import subprocess as _sp
    import sys as _sys

    text = (message or "").strip()
    text_lower = text.lower()

    # Determine task and payload
    payload = {}  # type: dict

    # Detect copy_type
    copy_type = None  # type: Optional[str]
    for ct in ("正本", "副本", "繕本"):
        if ct in text:
            copy_type = ct
            break

    # Detect options
    add_poa = any(kw in text for kw in ("附委任狀", "委任狀"))
    add_sent = any(kw in text for kw in ("繕本已送對造", "已送對造", "送對造"))

    # Extract file path (look for absolute path or quoted path)
    import re as _re
    path_match = _re.search(r'["\']?(/[^\s"\']+\.\w+)["\']?', text)
    file_path = path_match.group(1) if path_match else ""

    # Route to appropriate task
    if any(kw in text_lower for kw in ("合併pdf", "合併 pdf")):
        # Merge: extract multiple paths
        paths = _re.findall(r'(/[^\s"\']+\.pdf)', text)
        if len(paths) < 2:
            return "請提供至少兩個 PDF 檔案路徑來合併。"
        output_path = paths[-1].replace(".pdf", "_合併.pdf")
        task = "merge"
        payload = {"inputs": paths, "output": output_path}

    elif any(kw in text_lower for kw in ("轉pdf", "轉換pdf", "轉成pdf")):
        if not file_path:
            return "請提供要轉換的 DOCX 檔案路徑。例如：轉PDF /path/to/file.docx"
        task = "convert"
        payload = {"input": file_path}

    elif any(kw in text_lower for kw in ("做正本", "做副本", "做繕本", "標正本", "標副本", "標繕本")):
        if not file_path:
            return "請提供 PDF 檔案路徑。例如：做正本 /path/to/file.pdf"
        task = "mark"
        payload = {
            "input": file_path,
            "copy_type": copy_type or "正本",
            "add_poa": add_poa,
            "add_sent_to_opponent": add_sent,
        }

    elif any(kw in text_lower for kw in ("書狀製作", "製作書狀")):
        if not file_path:
            return "請提供書狀檔案路徑（DOCX 或 PDF）。例如：書狀製作 /path/to/file.docx 正本"
        task = "produce"
        payload = {
            "input": file_path,
            "copy_type": copy_type or "正本",
            "add_poa": add_poa,
            "add_sent_to_opponent": add_sent,
        }

    else:
        return None

    # Run the skill
    skill_script = os.path.join(_MAGI_ROOT, "skills", "doc-producer", "action.py")
    if not os.path.isfile(skill_script):
        return "doc-producer skill 腳本不存在。"

    from api.runtime_paths import get_skill_python
    skill_python = str(get_skill_python())
    task_arg = "%s %s" % (task, json.dumps(payload, ensure_ascii=False))

    try:
        proc = _sp.run(
            [skill_python, skill_script, "--task", task_arg],
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = (proc.stdout or "").strip()
        if not output:
            return "doc-producer 執行完成但無輸出。stderr: %s" % (proc.stderr or "")[:300]

        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return "doc-producer 輸出解析失敗: %s" % output[:500]

        if result.get("success"):
            # Format success response
            out_file = result.get("output", "")
            outputs = result.get("outputs", {})
            if outputs:
                parts = []
                if outputs.get("pdf"):
                    parts.append("PDF: %s" % outputs["pdf"])
                if outputs.get("marked"):
                    parts.append("標記: %s" % outputs["marked"])
                if outputs.get("merged"):
                    parts.append("合併: %s" % outputs["merged"])
                return "書狀製作完成：\n" + "\n".join(parts)
            elif out_file:
                page_info = ""
                if result.get("page_count"):
                    page_info = "（共 %d 頁）" % result["page_count"]
                return "完成%s：%s" % (page_info, out_file)
            else:
                return "書狀製作完成。"
        else:
            return "書狀製作失敗：%s" % result.get("error", "未知錯誤")

    except _sp.TimeoutExpired:
        return "書狀製作逾時（180 秒）。"
    except Exception as e:
        logger.error("doc-producer dispatch error: %s", e)
        return "書狀製作發生錯誤：%s" % str(e)


# ── Case Management dispatch (Task 1) ──────────────────────────────────────

def dispatch_case_management(message, user_id="", platform=""):
    # type: (str, str, str) -> Optional[str]
    """口語化案件管理：建案 / 查詢 / 狀態更新 / Dashboard。"""
    from typing import Optional as _Opt
    import re as _re
    import uuid as _uuid
    from datetime import datetime as _dt

    text = (message or "").strip()
    text_lower = text.lower()

    try:
        from api.osc.utils import _osc_exec, _osc_resolve_case_id
    except Exception as e:
        logger.warning("dispatch_case_management: cannot import _osc_exec: %s", e)
        return None

    # ── Dashboard ──
    if any(kw in text for kw in ("業務概況", "案件概況", "今天的案件", "案件狀況")):
        try:
            rows, _ = _osc_exec(
                "SELECT status, COUNT(*) as cnt FROM cases GROUP BY status",
                (), fetch="all",
            )
            if not rows:
                return "目前資料庫無案件記錄。"
            parts = ["📊 案件概況："]
            for row in (rows or []):
                s = row.get("status") or "未知"
                c = row.get("cnt") or 0
                parts.append(f"  {s}：{c} 件")
            return "\n".join(parts)
        except Exception as e:
            logger.warning("dispatch_case_management dashboard error: %s", e)
            return None

    # ── List / search cases ──
    if any(kw in text for kw in ("案件清單", "列出案件", "列出進行中", "所有案件")):
        status_filter = None
        if "進行中" in text:
            status_filter = "進行中"
        elif "已結案" in text:
            status_filter = "已結案"
        try:
            if status_filter:
                rows, _ = _osc_exec(
                    "SELECT case_number, client_name, status, case_category FROM cases WHERE status=%s ORDER BY updated_at DESC LIMIT 30",
                    (status_filter,), fetch="all",
                )
            else:
                rows, _ = _osc_exec(
                    "SELECT case_number, client_name, status, case_category FROM cases ORDER BY updated_at DESC LIMIT 30",
                    (), fetch="all",
                )
            if not rows:
                return "目前無符合條件的案件。"
            parts = ["📂 案件清單（最近 %d 件）：" % len(rows)]
            for row in (rows or []):
                cn = row.get("case_number") or row.get("client_name") or "?"
                cli = row.get("client_name") or ""
                st = row.get("status") or ""
                parts.append(f"  • {cn} {cli} [{st}]")
            return "\n".join(parts)
        except Exception as e:
            logger.warning("dispatch_case_management list error: %s", e)
            return None

    if text.startswith("查案件") or text.startswith("案件查詢"):
        q = text.replace("查案件", "").replace("案件查詢", "").strip()
        if not q:
            return "請提供查詢關鍵字，例如：查案件 王大明"
        try:
            like = "%%%s%%" % q
            rows, _ = _osc_exec(
                """
                SELECT case_number, client_name, status, case_category FROM cases
                WHERE case_number LIKE %s OR client_name LIKE %s OR court_case_no LIKE %s
                ORDER BY updated_at DESC LIMIT 10
                """,
                (like, like, like), fetch="all",
            )
            if not rows:
                return "找不到符合「%s」的案件。" % q
            parts = ["🔍 查詢結果（%d 件）：" % len(rows)]
            for row in (rows or []):
                cn = row.get("case_number") or "?"
                cli = row.get("client_name") or ""
                st = row.get("status") or ""
                parts.append(f"  • {cn} {cli} [{st}]")
            return "\n".join(parts)
        except Exception as e:
            logger.warning("dispatch_case_management search error: %s", e)
            return None

    # ── Update case status ──
    status_update_m = _re.search(r"(.+?)\s*(?:改為|更新為|狀態改為|狀態更新為)\s*(已結案|進行中|暫停|撤回)", text)
    if status_update_m:
        keyword = status_update_m.group(1).strip()
        new_status = status_update_m.group(2).strip()
        try:
            like = "%%%s%%" % keyword
            row, _ = _osc_exec(
                "SELECT id, case_number, client_name FROM cases WHERE case_number LIKE %s OR client_name LIKE %s ORDER BY updated_at DESC LIMIT 1",
                (like, like), fetch="one",
            )
            if not row:
                return "找不到符合「%s」的案件，無法更新狀態。" % keyword
            case_id = row.get("id")
            cn = row.get("case_number") or row.get("client_name") or ""
            _osc_exec(
                "UPDATE cases SET status=%s, updated_at=%s WHERE id=%s",
                (new_status, _dt.now().strftime("%Y-%m-%d %H:%M:%S"), case_id),
                fetch="none",
            )
            return "✅ 案件「%s」狀態已更新為「%s」。" % (cn, new_status)
        except Exception as e:
            logger.warning("dispatch_case_management update error: %s", e)
            return None

    # ── Create case ──
    if text.startswith("建案") or text.startswith("新案件"):
        rest = _re.sub(r"^(?:建案|新案件)\s*", "", text).strip()
        if not rest:
            return "請提供案件資訊，例如：建案 114原訴24 王大明 民事 侵權行為"
        parts = rest.split()
        case_number = parts[0] if len(parts) > 0 else ""
        client_name = parts[1] if len(parts) > 1 else ""
        case_type = parts[2] if len(parts) > 2 else ""
        case_reason = " ".join(parts[3:]) if len(parts) > 3 else ""
        if not client_name:
            return "請提供當事人姓名，例如：建案 114原訴24 王大明 民事 侵權行為"

        # Determine case_category
        case_category = ""
        if any(kw in rest for kw in ("法扶", "法律扶助", "消費者債務", "消債", "更生", "清算")):
            case_category = "法律扶助案件"
        elif any(kw in rest for kw in ("刑事", "刑訴")):
            case_category = "刑事案件"
        elif any(kw in rest for kw in ("民事", "民訴", "侵權", "債務", "損害賠償")):
            case_category = "民事案件"
        elif any(kw in rest for kw in ("行政", "訴願")):
            case_category = "行政案件"

        row_id = "chat-%s" % _uuid.uuid4().hex[:10]
        try:
            _osc_exec(
                "INSERT INTO cases (id, case_number, client_name, case_category, case_type, case_reason, status) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (row_id, case_number or None, client_name, case_category or None, case_type or None, case_reason or None, "進行中"),
                fetch="none",
            )
            msg = "✅ 案件已建立：\n  案號：%s\n  當事人：%s\n  類型：%s\n  案由：%s" % (
                case_number or "(未填)", client_name, case_category or case_type or "(未填)", case_reason or "(未填)"
            )
            return msg
        except Exception as e:
            logger.warning("dispatch_case_management create error: %s", e)
            return "建案失敗：%s" % str(e)

    return None


# ── Client Management dispatch (Task 2) ────────────────────────────────────

def dispatch_client_management(message, user_id="", platform=""):
    # type: (str, str, str) -> Optional[str]
    """口語化當事人管理：新增 / 查詢。"""
    import re as _re
    import uuid as _uuid

    text = (message or "").strip()

    try:
        from api.osc.utils import _osc_exec
    except Exception as e:
        logger.warning("dispatch_client_management: cannot import _osc_exec: %s", e)
        return None

    # ── Query client ──
    if _re.search(r"^(?:查當事人|查客戶)\s+", text):
        q = _re.sub(r"^(?:查當事人|查客戶)\s+", "", text).strip()
        if not q:
            return "請提供姓名，例如：查當事人 張三"
        like = "%%%s%%" % q
        try:
            rows, _ = _osc_exec(
                "SELECT name, phone, email, address, status FROM clients WHERE name LIKE %s OR phone LIKE %s ORDER BY updated_at DESC LIMIT 5",
                (like, like), fetch="all",
            )
            if not rows:
                return "找不到符合「%s」的當事人。" % q
            parts = ["👤 當事人查詢結果："]
            for row in (rows or []):
                name = row.get("name") or "?"
                phone = row.get("phone") or ""
                addr = row.get("address") or ""
                st = row.get("status") or ""
                parts.append("  • %s %s %s [%s]" % (name, phone, addr, st))
            return "\n".join(parts)
        except Exception as e:
            logger.warning("dispatch_client_management search error: %s", e)
            return None

    # Handle "XX的資料" pattern
    data_m = _re.match(r"^(.+?)的資料$", text)
    if data_m:
        q = data_m.group(1).strip()
        like = "%%%s%%" % q
        try:
            row, _ = _osc_exec(
                "SELECT name, phone, email, address, notes, status FROM clients WHERE name LIKE %s ORDER BY updated_at DESC LIMIT 1",
                (like,), fetch="one",
            )
            if not row:
                return "找不到「%s」的當事人資料。" % q
            parts = ["👤 %s 的資料：" % q]
            if row.get("phone"):
                parts.append("  電話：%s" % row["phone"])
            if row.get("email"):
                parts.append("  Email：%s" % row["email"])
            if row.get("address"):
                parts.append("  地址：%s" % row["address"])
            if row.get("notes"):
                parts.append("  備註：%s" % row["notes"])
            return "\n".join(parts)
        except Exception as e:
            logger.warning("dispatch_client_management data error: %s", e)
            return None

    # ── Add client ──
    if text.startswith("新增當事人") or text.startswith("建立當事人"):
        rest = _re.sub(r"^(?:新增當事人|建立當事人)\s*", "", text).strip()
        if not rest:
            return "請提供當事人資訊，例如：新增當事人 張三 0912345678 台北市"
        parts = rest.split()
        name = parts[0] if parts else ""
        phone = parts[1] if len(parts) > 1 and _re.match(r"^0\d{8,9}$", parts[1]) else ""
        address = " ".join(parts[2:]) if len(parts) > 2 else ""
        if not name:
            return "請提供當事人姓名。"

        row_id = "cli-%s" % _uuid.uuid4().hex[:10]
        try:
            _osc_exec(
                "INSERT INTO clients (id, name, phone, address, status) VALUES (%s,%s,%s,%s,%s)",
                (row_id, name, phone or None, address or None, "進行中"),
                fetch="none",
            )
            return "✅ 當事人已建立：%s%s%s" % (name, (" | " + phone) if phone else "", (" | " + address) if address else "")
        except Exception as e:
            logger.warning("dispatch_client_management create error: %s", e)
            return "新增當事人失敗：%s" % str(e)

    return None


# ── Accounting dispatch (Task 3) ────────────────────────────────────────────

def dispatch_accounting(message, user_id="", platform=""):
    # type: (str, str, str) -> Optional[str]
    """口語化記帳：記收入 / 記支出 / 帳務查詢。"""
    import re as _re
    from datetime import datetime as _dt, date as _date

    text = (message or "").strip()

    try:
        from api.osc.utils import _osc_exec, _osc_resolve_case_id
    except Exception as e:
        logger.warning("dispatch_accounting: cannot import _osc_exec: %s", e)
        return None

    # ── Monthly query ──
    if any(kw in text for kw in ("本月帳務", "帳務查詢", "本月收支", "帳務概況")):
        today = _date.today()
        start = "%04d-%02d-01" % (today.year, today.month)
        try:
            rows, _ = _osc_exec(
                "SELECT type, SUM(amount) as total FROM case_transactions WHERE date >= %s GROUP BY type",
                (start,), fetch="all",
            )
            if not rows:
                return "本月尚無帳務記錄。"
            parts = ["💰 本月帳務（%d/%d）：" % (today.year, today.month)]
            total_income = 0.0
            total_expense = 0.0
            for row in (rows or []):
                t = row.get("type") or "其他"
                amt = float(row.get("total") or 0)
                parts.append("  %s：%.0f 元" % (t, amt))
                if t == "收入":
                    total_income = amt
                elif t == "支出":
                    total_expense = amt
            parts.append("  淨額：%.0f 元" % (total_income - total_expense))
            return "\n".join(parts)
        except Exception as e:
            logger.warning("dispatch_accounting monthly error: %s", e)
            return None

    # ── Record income / expense ──
    tx_type = None
    if text.startswith("記收入"):
        tx_type = "收入"
        rest = text[3:].strip()
    elif text.startswith("記支出"):
        tx_type = "支出"
        rest = text[3:].strip()
    else:
        return None

    # Parse: <amount> <category> [<case_or_client>]
    parts = rest.split()
    if not parts:
        return "請提供金額，例如：記收入 5000 諮詢費 王大明"
    try:
        amount = float(parts[0].replace(",", ""))
    except ValueError:
        return "請提供有效金額，例如：記收入 5000 諮詢費 王大明"

    category = parts[1] if len(parts) > 1 else ""
    client_or_case = " ".join(parts[2:]) if len(parts) > 2 else ""

    # Resolve case_id if possible
    case_id = None
    if client_or_case:
        try:
            case_id = _osc_resolve_case_id(client_or_case)
        except Exception:
            pass

    # Use first case if still not found
    if not case_id and client_or_case:
        try:
            like = "%%%s%%" % client_or_case
            row, _ = _osc_exec(
                "SELECT id FROM cases WHERE client_name LIKE %s OR case_number LIKE %s ORDER BY updated_at DESC LIMIT 1",
                (like, like), fetch="one",
            )
            if row:
                case_id = row.get("id")
        except Exception:
            pass

    if not case_id:
        return "找不到案件「%s」，請先建案或直接使用案號。" % (client_or_case or "")

    tx_date = _date.today().strftime("%Y-%m-%d")
    desc = "%s %s" % (category, client_or_case) if client_or_case else category
    try:
        _osc_exec(
            "INSERT INTO case_transactions (case_id, date, type, category, description, amount) VALUES (%s,%s,%s,%s,%s,%s)",
            (case_id, tx_date, tx_type, category or None, desc or None, amount),
            fetch="none",
        )
        return "✅ 已記帳：%s %.0f 元（%s）" % (tx_type, amount, desc)
    except Exception as e:
        logger.warning("dispatch_accounting record error: %s", e)
        return "記帳失敗：%s" % str(e)


# ── Quotation dispatch (Task 4) ─────────────────────────────────────────────

def dispatch_quotation(message, user_id="", platform=""):
    # type: (str, str, str) -> Optional[str]
    """口語化報價單：開報價單 / 報價單清單。"""
    import re as _re
    import uuid as _uuid
    from datetime import date as _date

    text = (message or "").strip()

    try:
        from api.osc.utils import _osc_exec, _osc_resolve_case_id
    except Exception as e:
        logger.warning("dispatch_quotation: cannot import _osc_exec: %s", e)
        return None

    # ── List quotations ──
    if any(kw in text for kw in ("報價單清單", "查報價單", "報價單列表")):
        try:
            rows, _ = _osc_exec(
                "SELECT id, client_name, service_type, total_amount, status, created_date FROM quotations ORDER BY created_date DESC LIMIT 10",
                (), fetch="all",
            )
            if not rows:
                return "目前無報價單記錄。"
            parts = ["📋 報價單清單（最近 %d 筆）：" % len(rows)]
            for row in (rows or []):
                qid = row.get("id") or "?"
                cli = row.get("client_name") or "?"
                svc = row.get("service_type") or ""
                amt = row.get("total_amount") or 0
                st = row.get("status") or ""
                parts.append("  • %s %s %s %.0f 元 [%s]" % (qid, cli, svc, float(amt), st))
            return "\n".join(parts)
        except Exception as e:
            logger.warning("dispatch_quotation list error: %s", e)
            return None

    # ── Create quotation ──
    if text.startswith("開報價單"):
        rest = text[4:].strip()
        parts = rest.split()
        client_name = parts[0] if len(parts) > 0 else ""
        service_type = parts[1] if len(parts) > 1 else ""
        try:
            amount = float(parts[2].replace(",", "")) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            amount = 0
        if not client_name:
            return "請提供當事人姓名，例如：開報價單 王大明 民事訴訟 50000"

        # Try to resolve case_id
        case_id = None
        try:
            like = "%%%s%%" % client_name
            row, _ = _osc_exec(
                "SELECT id FROM cases WHERE client_name LIKE %s ORDER BY updated_at DESC LIMIT 1",
                (like,), fetch="one",
            )
            if row:
                case_id = row.get("id")
        except Exception:
            pass

        row_id = "quot-%s" % _uuid.uuid4().hex[:8]
        today = _date.today().strftime("%Y-%m-%d")
        try:
            _osc_exec(
                "INSERT INTO quotations (id, case_id, client_name, service_type, total_amount, status, created_date) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (row_id, case_id, client_name, service_type or None, amount, "草稿", today),
                fetch="none",
            )
            return "✅ 報價單已建立：%s %s %.0f 元（草稿）" % (client_name, service_type, amount)
        except Exception as e:
            logger.warning("dispatch_quotation create error: %s", e)
            return "建立報價單失敗：%s" % str(e)

    return None


# ── Calendar Event dispatch (Task 5) ────────────────────────────────────────

def dispatch_calendar_event(message, user_id="", platform=""):
    # type: (str, str, str) -> Optional[str]
    """口語化行事曆：排庭 / 排開會 → 建立 Apple Calendar 事件。"""
    import re as _re
    from datetime import datetime as _dt, timedelta as _td

    text = (message or "").strip()

    is_court = text.startswith("排庭")
    is_meeting = text.startswith("排開會") or text.startswith("排會議")
    if not (is_court or is_meeting):
        return None

    rest = _re.sub(r"^(?:排庭|排開會|排會議)\s*", "", text).strip()
    if not rest:
        return "請提供時間，例如：排庭 4/20 上午10:00 114原訴24 台北地院"

    # ── Parse date/time ──
    # Formats: 4/20, 04/20, 2026/4/20, 4月20日, 20日
    date_m = _re.search(r"(\d{1,4})[/月](\d{1,2})(?:[日/](\d{1,2}))?", rest)
    time_m = _re.search(r"(上午|下午|早上|晚上)?\s*(\d{1,2})[：:.](\d{2})", rest)

    if not date_m:
        return "請提供日期，例如：排庭 4/20 上午10:00 114原訴24 台北地院"

    now = _dt.now()
    d1, d2, d3 = date_m.group(1), date_m.group(2), date_m.group(3)
    if d3:
        # Year/Month/Day or Month/Day/Hour
        year, month, day = int(d1), int(d2), int(d3)
        if year < 200:
            year += 1911  # ROC year
    else:
        year = now.year
        month, day = int(d1), int(d2)

    hour, minute = 9, 0
    if time_m:
        ampm = time_m.group(1) or ""
        hour = int(time_m.group(2))
        minute = int(time_m.group(3))
        if ampm in ("下午", "晚上") and hour < 12:
            hour += 12
        elif ampm in ("上午", "早上") and hour == 12:
            hour = 0

    try:
        start_dt = _dt(year, month, day, hour, minute)
    except ValueError:
        return "日期格式不正確，請確認年月日是否合法。"

    # ── Build title ──
    # Remove date/time tokens
    clean = _re.sub(r"\d{1,4}[/月]\d{1,2}(?:[日/]\d{1,2})?", "", rest)
    clean = _re.sub(r"(?:上午|下午|早上|晚上)?\s*\d{1,2}[：:.]\\d{2}", "", clean)
    clean = _re.sub(r"(?:上午|下午|早上|晚上)?\s*\d{1,2}[：:.]\d{2}", "", clean)
    clean = clean.strip()

    if is_court:
        title = "開庭 %s" % clean if clean else "開庭"
        location = ""
        # Try to extract court name (last token)
        tokens = clean.split()
        if tokens:
            location = tokens[-1]
            case_part = " ".join(tokens[:-1]) if len(tokens) > 1 else tokens[0]
            title = "開庭 %s" % case_part
    else:
        title = "開會 %s" % clean if clean else "開會"
        location = ""

    end_dt = start_dt + _td(hours=1)

    # ── Extract case number from message for DB linkage ──
    import re as _re2
    case_m = _re2.search(r"(\d{2,3}(?:年度?)?\w+?字第?\d+號?|\d{4}-\d{4})", rest)
    event_case_number = case_m.group(1) if case_m else "非案件行程"

    # ── Write to case_todos DB (for Google Calendar sync) ──
    todo_inserted = False
    try:
        from api.osc.utils import _osc_exec
        _osc_exec(
            "INSERT INTO case_todos (case_number, client_name, todo_type, todo_date, todo_time, description, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'pending')",
            (
                event_case_number,
                "",
                "開庭" if is_court else "開會",
                start_dt.strftime("%Y-%m-%d"),
                start_dt.strftime("%H:%M:%S"),
                "%s — %s" % (title, location) if location else title,
            ),
            fetch=None,
        )
        todo_inserted = True
    except Exception as _dbe:
        logger.warning("dispatch_calendar_event db insert failed: %s", _dbe)

    # ── 偵測 Google Calendar 憑證是否存在 ──
    import os as _os_gcal
    _magi_root = _os_gcal.path.dirname(_os_gcal.path.dirname(_os_gcal.path.abspath(__file__)))
    _cred_path = (
        _os_gcal.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH")
        or _os_gcal.path.join(_magi_root, "json", "credentials.json")
    )
    cred_ok = _os_gcal.path.exists(_cred_path)

    dt_str = start_dt.strftime("%Y/%m/%d %H:%M")
    if todo_inserted and cred_ok:
        return "✅ 行程已存入系統，今日 08:00 將自動同步至 Google 日曆。\n行程：%s｜時間：%s｜地點：%s" % (
            title, dt_str, location or "未填",
        )
    elif todo_inserted:
        _hint = (
            "\n\n📌 尚未設定 Google 日曆 OAuth2 憑證，行程暫存本地：\n"
            "1. 至 Google Cloud Console 建立 OAuth2 Desktop App\n"
            "2. 下載 credentials JSON 存至 `json/credentials.json`\n"
            "   或設定 MAGI_GOOGLE_CREDENTIALS_PATH 環境變數\n"
            "設定完成後執行「日曆同步」即可推送。"
        )
        return "📅 行程已存入系統（Google 日曆待設定憑證後才會同步）。%s\n行程：%s｜時間：%s｜地點：%s" % (
            _hint, title, dt_str, location or "未填",
        )
    else:
        return "⚠️ 行程建立失敗，DB 寫入錯誤，請確認資料庫連線。"


# ── AI Draft dispatch (Task 6) ──────────────────────────────────────────────

def dispatch_ai_draft(message, user_id="", platform=""):
    # type: (str, str, str) -> Optional[str]
    """口語化書狀 AI 草擬：草擬起訴狀 / 答辯狀 / 聲請狀。"""
    import re as _re
    import subprocess as _sp
    import sys as _sys

    text = (message or "").strip()

    _DRAFT_KEYWORDS = ["草擬", "草稿", "幫我寫", "幫我草擬", "幫我起草"]
    _DOC_TYPES = {
        "起訴狀": "起訴狀",
        "答辯狀": "答辯狀",
        "聲請狀": "聲請狀",
        "陳報狀": "陳報狀",
        "準備狀": "準備狀",
        "上訴狀": "上訴狀",
        "抗告狀": "抗告狀",
    }

    if not any(kw in text for kw in _DRAFT_KEYWORDS):
        return None
    doc_type = None
    for kw, dt in _DOC_TYPES.items():
        if kw in text:
            doc_type = dt
            break
    if not doc_type:
        return None

    # Extract case number
    case_m = _re.search(r"(\d{2,3}(?:年度?)?\w+?字第?\d+號?)", text)
    case_number = case_m.group(1) if case_m else ""

    # Extract reason / title
    reason = ""
    for kw in _DRAFT_KEYWORDS + list(_DOC_TYPES.keys()):
        text = text.replace(kw, " ")
    if case_number:
        text = text.replace(case_number, " ")
    reason = " ".join(text.split()).strip()

    try:
        from api.osc.utils import _osc_exec
    except Exception as e:
        logger.warning("dispatch_ai_draft: cannot import _osc_exec: %s", e)
        return None

    # Look up case from DB
    case_row = None
    if case_number:
        like = "%%%s%%" % case_number
        try:
            case_row, _ = _osc_exec(
                "SELECT id, case_number, client_name, court_case_no, case_reason FROM cases WHERE case_number LIKE %s OR court_case_no LIKE %s ORDER BY updated_at DESC LIMIT 1",
                (like, like), fetch="one",
            )
        except Exception:
            pass

    _case_no = case_number or (case_row.get("case_number") if case_row else "")
    _client = (case_row.get("client_name") if case_row else "") or ""
    _reason = reason or (case_row.get("case_reason") if case_row else "") or ""
    prompt = (
        "你是台灣執業律師的書狀助理。請根據以下資訊草擬一份%s，"
        "格式參照台灣民事訴訟法書狀格式，包含當事人欄、案由、事實及理由各段。\n"
        "案件：%s　當事人：%s　案由：%s\n"
        "請直接輸出書狀內文，不要加說明。"
    ) % (doc_type, _case_no or "（未指定）", _client or "（未指定）", _reason or "（未指定）")

    import urllib.request as _ureq2, json as _jdraft

    def _call_llm(url, model, timeout_sec):
        # type: (str, str, int) -> str
        """呼叫 OpenAI-compatible /v1/chat/completions，回傳 content 字串；失敗拋例外。"""
        _body = _jdraft.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "stream": False,
        }).encode()
        _req = _ureq2.Request(
            url.rstrip("/") + "/v1/chat/completions",
            data=_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ureq2.urlopen(_req, timeout=timeout_sec) as _resp:
            _data = _jdraft.loads(_resp.read().decode())
        _choices = _data.get("choices") or []
        return (_choices[0].get("message", {}).get("content", "") if _choices else "").strip()

    # ── 1. 優先走 oMLX（MAGI_OMLX_CHAT_URL，預設 26B）——先確認模型已載入 ──
    _omlx_url = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:8080")
    _omlx_model = os.environ.get("MAGI_TEXT_PRIMARY_MODEL") or "gemma-4-26b-a4b-it-4bit"
    _omlx_timeout = int(os.environ.get("MAGI_DRAFT_OMLX_TIMEOUT_SEC", "120"))
    draft_text = ""
    _omlx_ready = False
    try:
        _h = _ureq2.urlopen(_omlx_url + "/health", timeout=3)
        _hd = json.loads(_h.read().decode())
        _omlx_ready = int(_hd.get("engine_pool", {}).get("loaded_count", 0)) > 0
    except Exception:
        pass
    if _omlx_ready:
        try:
            draft_text = _call_llm(_omlx_url, _omlx_model, _omlx_timeout)
        except Exception as _omlx_err:
            logger.info("dispatch_ai_draft oMLX fail: %s", _omlx_err)
    else:
        logger.info("dispatch_ai_draft: oMLX not ready (loaded_count=0), skipping to Ollama")

    # ── 2. Fallback: Ollama (gemma4:e4b，port 11434) ──
    if not draft_text:
        _ollama_url = os.environ.get("MAGI_DRAFT_OLLAMA_URL", "http://127.0.0.1:11434")
        _ollama_model = os.environ.get("MAGI_DRAFT_OLLAMA_MODEL", "gemma4:e4b")
        _ollama_timeout = int(os.environ.get("MAGI_DRAFT_OLLAMA_TIMEOUT_SEC", "180"))
        try:
            draft_text = _call_llm(_ollama_url, _ollama_model, _ollama_timeout)
        except Exception as _ol_err:
            logger.warning("dispatch_ai_draft Ollama also failed: %s", _ol_err)

    if draft_text:
        return "📝 %s 草稿（前段預覽）：\n\n%s\n\n（完整版請至系統 Web 介面查看）" % (doc_type, draft_text[:800])

    # ── 3. Last-resort: casper collab/chat ──
    try:
        from api.osc.drafts import _osc_generate_draft_with_casper
        draft_text = _osc_generate_draft_with_casper(prompt)
        if draft_text:
            return "📝 %s 草稿（前段預覽）：\n\n%s\n\n（完整版請至系統 Web 介面查看）" % (doc_type, draft_text[:800])
    except Exception as e:
        logger.warning("dispatch_ai_draft casper fallback error: %s", e)
    return "⚠️ 書狀草擬失敗（本機模型記憶體不足，oMLX 26B 需要 14GB 可用 RAM）。請關閉其他應用程式後重試。"
