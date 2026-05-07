"""
Specialized command handlers extracted from Orchestrator.

All functions accept an ``orch`` parameter (the Orchestrator instance)
instead of ``self``, keeping the same logic but as standalone functions.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def run_labor_law_command(orch, message: str) -> str:
    try:
        skill_script = f"{_MAGI_ROOT}/skills/labor-law-calculator/action.py"
        if not os.path.exists(skill_script):
            return "❌ 找不到勞基法計算器 skill。"
        task = orch._strip_intent_prefixes(
            message,
            [
                r"^(?:勞基法計算|勞動基準法計算|加班費計算)\s*",
                r"^(?:幫我|請|麻煩|協助我|可以幫我)?\s*",
            ],
        )
        if not task:
            return "❓ 請提供計算條件，例如：`月薪 50000，休息日加班 3 小時`"
        file_paths = re.findall(
            r"(?:/[^\s,，；;]+\.(?:xlsx|xls|pdf)|[A-Za-z]:[^\s,，；;]+\.(?:xlsx|xls|pdf))",
            task, re.IGNORECASE,
        )
        if file_paths:
            task_clean = re.sub(
                r"(?:/[^\s,，；;]+\.(?:xlsx|xls|pdf)|[A-Za-z]:[^\s,，；;]+\.(?:xlsx|xls|pdf))",
                "", task, flags=re.IGNORECASE,
            ).strip()
            cmd = [sys.executable, skill_script, "--task", task_clean, "--file"] + file_paths
            timeout_sec = 120
        else:
            cmd = [sys.executable, skill_script, "--task", task]
            timeout_sec = 30
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_sec,
            cwd=_MAGI_ROOT, env=os.environ.copy(),
        )
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            err = (_proc_err if (_proc_err := (proc.stderr or out or "unknown").strip()) else "unknown")[:300]
            return f"❌ 勞基法計算失敗：{err}"
        return out
    except Exception as e:
        return f"❌ 勞基法計算器錯誤：{e}"


def run_inline_translation_command(orch, user_id, message: str) -> str:
    text = orch._strip_intent_prefixes(
        message,
        [r"^(?:幫我|請|麻煩|協助我|可以幫我)?\s*", r"^(?:翻譯|translate)\s*"],
    )
    if not text:
        return "❓ 請提供要翻譯的文字。"
    if len(text) <= 800 and len(text.splitlines()) <= 4:
        try:
            from skills.bridge.tri_sage_collab import translate_text as _translate_text
            result = _translate_text(text, target_lang="繁體中文", source_lang="auto", mode="full")
        except Exception:
            result = orch._translate_text_complete(text, source_lang="auto", target_lang="繁體中文")
    else:
        result = orch._translate_text_complete(text, source_lang="auto", target_lang="繁體中文")
    if not result.get("success"):
        err = str(result.get("error") or "unknown").strip()
        if err.startswith("translation_off_topic:"):
            return "❌ 翻譯結果偏題，已阻擋送出。請稍後重試。"
        return f"❌ 翻譯失敗: {err}"
    translated_text = str(result.get("text") or "").strip()
    msg_lower = str(message or "").lower()
    disable_txt = any(k in msg_lower for k in ["不要txt", "不需要txt", "no txt", "inline", "直接貼上"])
    explicit_txt = any(k in msg_lower for k in ["txt", "文字檔", "檔案"])
    is_url = bool(re.search(r"https?://", text, flags=re.IGNORECASE))
    try:
        long_threshold = int(os.environ.get("MAGI_TRANSLATE_TXT_MIN_CHARS", "1200") or "1200")
    except Exception:
        long_threshold = 1200
    is_long = len(text) >= max(400, long_threshold)
    want_export = (not disable_txt) and (explicit_txt or is_url or is_long)
    if want_export:
        exported_reply = orch._export_translation_docx(
            source_text=text, translated_text=translated_text,
            title="", subtitle="", prefix="full_translation", user_id=str(user_id or ""),
        )
        if not exported_reply:
            exported_reply = orch._export_translation_txt(
                translated_text=translated_text,
                source=(text[:240] + "…") if len(text) > 240 else text,
                provider=str(result.get("provider") or "tri-sage"),
                mode="full_translation", prefix="full_translation", user_id=str(user_id or ""),
            )
        if exported_reply:
            return exported_reply
    return f"🌐 翻譯結果（{result.get('provider','tri-sage')}）:\n{translated_text}"


def run_translate_file_command(orch, user_id, message: str) -> str:
    """Translate a local file (DOCX/PDF/PPTX/TXT/etc.) end-to-end.

    Invocation: `翻譯檔案 <path>` or `translate file <path>`.
    Optional trailing target language: `翻譯檔案 <path> 目標:英文`.
    Runs full APE-enabled chunk translation via the translator skill and
    returns a DOCX export (bilingual) attached to the reply.
    """
    import shlex
    raw = orch._strip_intent_prefixes(
        message,
        [r"^(?:幫我|請|麻煩|協助我|可以幫我)?\s*",
         r"^(?:翻譯檔案|翻譯文件|translate\s+file)\s*"],
    )
    if not raw:
        return "❓ 請提供檔案路徑，例如：`翻譯檔案 /Users/ai/Desktop/a.docx`"

    # Parse trailing target-language hint: `目標:英文` / `target:en`
    target_lang = "繁體中文"
    m = re.search(r"(?:目標[:：]|target[:：])\s*([A-Za-z\u4e00-\u9fff-]+)", raw)
    if m:
        target_lang = m.group(1).strip()
        raw = raw[:m.start()].strip() + raw[m.end():].strip()

    # Extract file path — support quoted or bare paths
    path = raw.strip().strip("「」\"'` ")
    # If user pasted multiple tokens, try shlex
    if not os.path.exists(path):
        try:
            toks = shlex.split(raw)
            for t in toks:
                if os.path.exists(t):
                    path = t
                    break
        except Exception:
            pass

    if not path or not os.path.exists(path):
        return f"❌ 找不到檔案：`{path or raw}`"
    if not os.path.isfile(path):
        return f"❌ 不是檔案：`{path}`"

    size = os.path.getsize(path)
    if size > 20 * 1024 * 1024:
        return f"❌ 檔案過大（{size/1024/1024:.1f}MB > 20MB），請先切分。"

    try:
        skill_script = f"{_MAGI_ROOT}/skills/translator/action.py"
        if not os.path.exists(skill_script):
            return "❌ 找不到 translator skill。"
        import json as _json
        payload = {
            "input_path": path,
            "target_lang": target_lang,
            "source_lang": "auto",
            "mode": "full",
            "export": "1",
            "export_format": "docx",
            "llm_timeout": int(os.environ.get("MAGI_TRANSLATOR_LLM_TIMEOUT_SEC", "900") or "900"),
            "timeout_sec": int(os.environ.get("MAGI_TRANSLATOR_TIMEOUT_SEC", "1800") or "1800"),
        }
        env = os.environ.copy()
        # Force-enable APE paths for file translation (can be disabled via MAGI_TRANSLATE_FILE_APE=0)
        if str(env.get("MAGI_TRANSLATE_FILE_APE", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}:
            env["MAGI_TRANSLATOR_APE"] = "1"
            env["MAGI_TRANSLATOR_APE_CHUNKS"] = "1"
        cmd = [sys.executable, skill_script, "--task", "translate " + _json.dumps(payload, ensure_ascii=False)]
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=payload["timeout_sec"] + 60,
            cwd=_MAGI_ROOT, env=env,
        )
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 and not out:
            err = (proc.stderr or "unknown").strip()[:400]
            return f"❌ 翻譯檔案失敗：{err}"
        try:
            result = _json.loads(out.splitlines()[-1]) if out else {}
        except Exception:
            # Try from last dict-looking line
            result = {}
            for ln in reversed(out.splitlines()):
                ln = ln.strip()
                if ln.startswith("{") and ln.endswith("}"):
                    try:
                        result = _json.loads(ln)
                        break
                    except Exception:
                        continue
        if not result.get("success"):
            err = str(result.get("error") or proc.stderr or "unknown").strip()[:400]
            return f"❌ 翻譯檔案失敗：{err}"

        docx_path = result.get("docx_path") or result.get("export_path") or ""
        provider = result.get("provider") or "translator"
        lines = [f"📄 翻譯完成（{provider}，目標語言：{target_lang}）"]
        if docx_path and os.path.exists(docx_path):
            lines.append(f"📎 DOCX：`{docx_path}`")
        elif result.get("text"):
            preview = result["text"][:800]
            lines.append(f"\n{preview}{'…' if len(result['text']) > 800 else ''}")
        return "\n".join(lines)
    except subprocess.TimeoutExpired:
        return "❌ 翻譯檔案逾時（超過 30 分鐘）。可嘗試拆分檔案。"
    except Exception as e:
        return f"❌ 翻譯檔案錯誤：{e}"


def run_inline_summary_command(orch, message: str) -> str:
    summary_length = orch._detect_summary_length(message)
    text = orch._strip_intent_prefixes(
        message,
        [
            r"^(?:幫我|請|麻煩|協助我|可以幫我)?\s*",
            r"^(?:短摘要?|詳細摘要?|簡短摘要?|完整摘要?|長摘要?|精簡摘要?)\s*",
            r"^(?:摘要|總結|重點整理|summarize|summarise|summary)\s*",
        ],
    )
    if not text:
        return (
            "❓ 請提供要摘要的內容。\n\n"
            "💡 可指定摘要等級：\n"
            "• `精簡摘要 ...` 或 `短摘要 ...` → 3-5 點，每點一句話\n"
            "• `摘要 ...` → 5-8 點，每點 1-2 句（預設）\n"
            "• `詳細摘要 ...` 或 `長摘要 ...` → 12-15 點，每點 2-3 句（含背景與數據）"
        )
    result = orch._summarize_text_resilient(text, summary_length=summary_length)
    if not result.get("success"):
        return f"❌ 摘要失敗：{str(result.get('error') or 'unknown')}"
    summary_text = str(result.get("text") or result.get("summary") or "").strip()
    if not summary_text:
        return "❌ 摘要失敗：沒有可用結果"
    length_label = {"short": "精簡", "medium": "標準", "long": "詳細"}.get(summary_length, "")
    return f"📝 {length_label}摘要結果（{result.get('provider', 'summary')}）:\n{summary_text}"


def run_court_hearing_command(orch, message: str) -> str:
    py = os.environ.get("MAGI_SKILL_PYTHON", f"{_MAGI_ROOT}/venv/bin/python3").strip()
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"
    skill_script = f"{_MAGI_ROOT}/skills/court-hearing-reminder/action.py"
    if not os.path.exists(skill_script):
        return "❌ 找不到開庭提醒 skill。"
    text = str(message or "").strip()
    from api.pipelines.skill_dispatch import looks_like_capability_question
    if looks_like_capability_question(text):
        return (
            "✅ **我可以幫您查排程！**\n\n"
            "• 查看排程：`最近有什麼庭`（含開庭/補正/繳費）\n"
            "• 庭前準備：`準備 XXX 案的開庭資料`\n"
            "• 準備清單：`XXX案的準備清單`\n"
            "• 案件總覽：`案件時程總覽`\n"
            "• 標記完成：`張國賢繳了` / `補字第54號交了`"
        )

    import re as _re
    done_match = _re.search(
        r"(.+?)(?:的)?(?:繳了|交了|繳費了|補正了|完成了|已繳|已補正|已交|已完成)$", text,
    )
    if not done_match:
        done_match2 = _re.search(r"(?:關掉|取消|關閉)(.+?)(?:的)?(?:提醒|警報|通知)?$", text)
        if done_match2:
            done_match = done_match2

    if done_match:
        task = "done"
        cmd = [py, skill_script, "--task", task, "--text", text]
    elif any(k in text for k in ["pattern", "對造", "歷史案件", "同一對造", "跨案件", "案件分析"]):
        query = text
        for prefix in ["pattern", "跨案件分析", "案件分析", "歷史案件", "查"]:
            query = query.replace(prefix, "")
        query = query.strip()
        task = "patterns"
        cmd = [py, skill_script, "--task", task, "--text", query]
    elif any(k in text for k in ["checklist", "清單", "應備文件", "準備清單"]):
        case_no = text
        for prefix in ["checklist", "準備清單", "應備文件", "開庭清單", "案的", "案"]:
            case_no = case_no.replace(prefix, "")
        case_no = case_no.strip()
        task = "checklist"
        cmd = [py, skill_script, "--task", task, "--text", case_no]
    elif any(k in text for k in ["dashboard", "總覽", "時程總覽", "全部排程", "所有案件"]):
        task = "dashboard"
        cmd = [py, skill_script, "--task", task]
    elif any(k in text for k in ["準備", "庭前", "摘要"]):
        case_no = text
        for prefix in ["準備", "庭前準備", "開庭資料", "案的", "案"]:
            case_no = case_no.replace(prefix, "")
        case_no = case_no.strip()
        task = "prep"
        cmd = [py, skill_script, "--task", task, "--case-number", case_no]
    else:
        task = "list"
        cmd = [py, skill_script, "--task", task]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=_MAGI_ROOT, env=os.environ.copy(),
        )
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            err = ((proc.stderr or out or "unknown").strip())[:300]
            return f"❌ 查詢失敗：{err}"
        return out
    except Exception as e:
        return f"❌ 查詢錯誤：{e}"


def run_embedding_web_search(orch, message: str) -> str:
    """Web search dispatch for EmbeddingRouter."""
    from skills.bridge.web_research import research_topic

    topic = str(message or "").strip()
    # Only strip search-specific prefixes, NOT generic words like 請/幫我/可以/一下
    for kw in ["搜尋", "search", "research", "/search", "查一下", "找一下", "搜一下",
                "google", "幫我搜", "幫我查一下", "執行網路研究", "進行網路研究",
                "網路研究", "網路搜尋", "幫我查詢", "請幫我查詢", "幫我查", "幫我找",
                "@MAGI", "@magi"]:
        topic = re.sub(re.escape(kw), "", topic, flags=re.IGNORECASE).strip()
    topic = re.sub(r"^[:：]\s*", "", topic).strip()
    if len(topic) < 2:
        return "🔍 請告訴我要搜尋什麼主題。例如：'搜尋 AI agent 2024'"
    logger.info(f"🌐 EmbeddingRouter Web Search: {topic}")
    result = research_topic(topic, depth=3)
    if result.get("sources"):
        return summarize_web_results(topic, result)
    return f"🔍 找不到關於「{topic}」的資訊。"


def summarize_web_results(topic: str, result: dict) -> str:
    raw_parts = []
    for i, src in enumerate(result.get("sources", []), 1):
        title = src.get("title", "")[:80]
        url = src.get("url", "")
        preview = src.get("content_preview", "")[:500]
        raw_parts.append(f"[{i}] {title}\nURL: {url}\n{preview}")
    raw_context = "\n\n".join(raw_parts)
    combined = result.get("combined_content", "")[:3000]
    if combined:
        raw_context = combined

    prompt = f"""你是 MAGI 搜尋助理。根據以下網路搜尋結果，用繁體中文撰寫一份簡潔易讀的摘要回覆。

[搜尋主題]
{topic}

[搜尋結果原始資料]
{raw_context}

[回覆規則]
1. 直接回答使用者的問題，用 3-8 句話。
2. 只使用搜尋結果中的資訊，不要編造。
3. 如果搜尋結果包含數字、日期、溫度等具體數據，務必列出。
4. 最後附上 1-3 個最相關的參考來源連結。
5. 格式要簡潔清楚，適合在手機聊天軟體閱讀。
6. 不要使用 HTML 標籤、Markdown 語法（如 **粗體**、`程式碼`、### 標題）。純文字即可。
"""
    try:
        from skills.bridge.grounded_ai import _generate
        summary = _generate(prompt, temperature=0.2, timeout=120, num_ctx=4096)
        if summary and len(summary) > 10:
            return f"🔍 **{topic}**\n\n{summary}"
    except Exception as e:
        logger.warning(f"Web search LLM summarization failed: {e}")

    response = f"🔍 **網路研究報告: {topic}**\n\n"
    for i, src in enumerate(result.get("sources", []), 1):
        title = src.get("title", "")[:50]
        url = src.get("url", "")
        response += f"{i}. {title}\n   {url}\n\n"
    return response
