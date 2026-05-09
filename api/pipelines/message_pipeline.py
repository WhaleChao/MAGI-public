"""
message_pipeline.py — extracted from MAGIOrchestrator._process_message_inner

All logic is identical to the original method; the only mechanical changes are:
  * ``self`` parameter renamed to ``orch``
  * ``self.`` references replaced with ``orch.``
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from api.help_text import HELP_ALIASES
from api.model_config import TEXT_PRIMARY_MODEL
from api.runtime_paths import get_legacy_code_root, get_magi_root_dir, legacy_code_enabled
from skills.ops.red_phone import alert_iron_dome_violation

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

# ── Lazy module-level helpers (mirrors orchestrator.py top-level) ──

def fetch_url_content(*a, **kw):
    from skills.research.web_research import fetch_url_content as _fn
    globals()["fetch_url_content"] = _fn
    return _fn(*a, **kw)

def fetch_url_sections(*a, **kw):
    from skills.research.web_research import fetch_url_sections as _fn
    globals()["fetch_url_sections"] = _fn
    return _fn(*a, **kw)

def get_brain_status(*a, **kw):
    import skills.brain_manager.action as _bm
    globals()["get_brain_status"] = _bm.get_brain_status
    return _bm.get_brain_status(*a, **kw)


_FILE_REVIEW_CONFIRM_RE = re.compile(r"(?<![A-Fa-f0-9])([A-Fa-f0-9]{6,12})(?![A-Fa-f0-9])")

# ── docx chat edit 觸發詞（Phase 3）──
_DOCX_EDIT_TRIGGER_RE = re.compile(
    r"@magi\s*(編輯|修改)|編輯這份|修改這份|edit\s+this",
    re.IGNORECASE,
)

# ── docx 附件 MIME types ──
_DOCX_MIME = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "docx",
}


def _handle_docx_chat_edit_if_any(orch, user_id, platform, message, attachment, correlation_id=None):
    """偵測律師上傳 .docx + 訊息含觸發詞 → 路由到 cmd_chat_edit。

    觸發條件（all required）：
    - attachment 不為 None 且 attachment.type / mime 為 docx
    - message 含任一觸發詞：「@MAGI 編輯」「@MAGI 修改」「編輯這份」「修改這份」「edit this」

    Returns: (handled: bool, reply: str)
    """
    if not attachment:
        return False, ""

    # 判斷是否為 docx 附件
    attach_type = str(attachment.get("type") or "").lower()
    attach_mime = str(attachment.get("mime") or "").lower()
    attach_name = str(attachment.get("filename") or attachment.get("name") or "").lower()

    is_docx = (
        attach_type in _DOCX_MIME
        or attach_mime in _DOCX_MIME
        or "docx" in attach_mime
        or attach_name.endswith(".docx")
    )
    if not is_docx:
        return False, ""

    # 判斷 message 是否含觸發詞
    if not _DOCX_EDIT_TRIGGER_RE.search(message or ""):
        return False, ""

    # 取得指令（去除觸發詞後的部分）
    instruction = _DOCX_EDIT_TRIGGER_RE.sub("", message or "").strip()
    if not instruction:
        instruction = message.strip()

    # 取得 docx 檔案路徑
    doc_path = attachment.get("path") or attachment.get("file_path") or ""
    if not doc_path or not os.path.isfile(doc_path):
        return True, "⚠️ 無法讀取上傳的 .docx 檔案路徑，請重新上傳。"

    try:
        import importlib.util as _ilu
        _skill_dir = os.path.join(_MAGI_ROOT, "skills", "docx-editor")
        _spec = _ilu.spec_from_file_location("docx_editor_action", os.path.join(_skill_dir, "action.py"))
        _action_mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_action_mod)

        result = _action_mod.cmd_chat_edit(
            doc_path=doc_path,
            instruction=instruction,
            source=platform or "user",
            author="MAGI",
        )
    except Exception as e:
        logger.error(f"docx_chat_edit_router failed: {e}")
        return True, f"⚠️ 編輯失敗：{e}"

    if not result["ok"] and result["errors"]:
        err_msg = result["errors"][0].get("reason", "未知錯誤")
        return True, f"⚠️ {err_msg}"

    changes = result.get("changes_applied", 0)
    out_path = result.get("output_path", "")
    warnings = result.get("warnings", [])

    parts = [f"✅ 已套用 {changes} 處修改。"]
    if out_path:
        parts.append(f"📄 輸出檔：`{out_path}`")
    if warnings:
        warn_str = "\n".join(f"⚠️ {w}" for w in warnings[:3])
        parts.append(warn_str)
    if changes == 0:
        parts = ["ℹ️ " + (warnings[0] if warnings else "沒有可套用的修改。")]

    return True, "\n".join(parts)
_ARITHMETIC_INTENT_RE = re.compile(
    r"(等於多少|是多少|多少|幫我算|算一下|請.*算|用工具算|不要心算|計算|calculate)",
    re.IGNORECASE,
)
_ARITHMETIC_EXPR_RE = re.compile(
    r"(?<![\w])([0-9][0-9\s+\-＋－*/().,%×xX＊÷／]*[+\-＋－*/%×xX＊÷／][0-9\s+\-＋－*/().,%×xX＊÷／]*[0-9)])"
)


def _normalize_arithmetic_expression(expr: str) -> str:
    """Normalize a user-visible arithmetic expression for the calculate tool."""
    normalized = (expr or "").strip()
    normalized = normalized.replace("＋", "+").replace("－", "-")
    normalized = normalized.replace("×", "*").replace("＊", "*")
    normalized = normalized.replace("÷", "/").replace("／", "/")
    normalized = re.sub(r"(?<=\d)[xX](?=\d)", "*", normalized)
    normalized = normalized.replace(",", "")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized


def _extract_arithmetic_expression(message: str) -> str:
    """Extract a simple arithmetic expression from a natural-language prompt.

    This is intentionally conservative: it only activates when the message has
    arithmetic intent language (or is mostly an expression) and contains a real
    arithmetic operator. This prevents dates and case numbers from being treated
    as math just because they contain hyphens.
    """
    text = (message or "").strip()
    if not text:
        return ""
    candidates = [_normalize_arithmetic_expression(m.group(1)) for m in _ARITHMETIC_EXPR_RE.finditer(text)]
    candidates = [c for c in candidates if c and re.search(r"\d", c) and re.search(r"[+\-*/%]", c)]
    if not candidates:
        return ""
    mostly_expression = bool(re.fullmatch(r"[\d\s+\-＋－*/().,%×xX＊÷／=？?多少等於是多少]+", text))
    if not (_ARITHMETIC_INTENT_RE.search(text) or mostly_expression):
        return ""
    # Pick the longest candidate; prompts often contain a clean expression plus
    # surrounding wording.
    return max(candidates, key=len)


def _try_arithmetic_tool_fast_path(message: str) -> str:
    """Return a deterministic calculator answer when the prompt is arithmetic.

    The live failure in 2026-04-23 showed the chat path could ignore "請用工具算",
    hallucinate the arithmetic result, mix languages, and trigger rule memory.
    This fast path routes simple arithmetic to the registered calculate tool
    before LLM generation or rule-memory capture.
    """
    expr = _extract_arithmetic_expression(message)
    if not expr:
        return ""
    try:
        from skills.engine.tool_registry import TOOLS

        result = str(TOOLS["calculate"]["fn"](expression=expr)).strip()
    except Exception as exc:
        return f"計算工具目前無法使用：{type(exc).__name__}: {exc}"
    if not result:
        return "計算工具沒有回傳結果。"
    return f"{result}\n\n（使用工具：calculate）"


def _find_file_review_confirm_record(message: str) -> tuple[str, dict, str]:
    """Return a file-review confirm token, record, and state found in message."""
    candidates = [m.group(1).upper() for m in _FILE_REVIEW_CONFIRM_RE.finditer(message or "")]
    if not candidates:
        return "", {}, ""

    pending_file = os.path.join(_MAGI_ROOT, "skills", "file-review-orchestrator", ".review_submit_pending.json")
    try:
        with open(pending_file, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except Exception:
        return "", {}, ""
    if not isinstance(pending, dict):
        return "", {}, ""

    import time as _time
    now = _time.time()
    stale_match = ("", {}, "")
    for token in candidates:
        entry = pending.get(token)
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "") != "pending":
            if not stale_match[0]:
                stale_match = (token, entry, "closed")
            continue
        try:
            if now > float(entry.get("expires_at", 0) or 0):
                if not stale_match[0]:
                    stale_match = (token, entry, "expired")
                continue
        except Exception:
            if not stale_match[0]:
                stale_match = (token, entry, "expired")
            continue
        return token, entry, "pending"
    return stale_match


def _find_file_review_confirm_token(message: str) -> str:
    """Return a live file-review confirm token found in message, if any."""
    token, _entry, state = _find_file_review_confirm_record(message)
    return token if state == "pending" else ""


def _handle_file_review_confirmation_if_any(orch, user_id, platform: str, message: str) -> tuple[bool, str]:
    """Dispatch file-review submit confirmation when a user replies with the token."""
    token, entry, state = _find_file_review_confirm_record(message)
    if not token:
        return False, ""
    if state == "expired":
        return True, f"⚠️ 閱卷確認碼 {token} 已逾期，請重新發起閱卷聲請。"
    if state != "pending":
        status = str((entry or {}).get("status") or "已失效")
        return True, f"⚠️ 閱卷確認碼 {token} 目前狀態為 {status}，未送出；請重新發起閱卷聲請。"

    action_script = os.path.join(_MAGI_ROOT, "skills", "file-review-orchestrator", "action.py")
    if not os.path.exists(action_script):
        return True, f"❌ 找不到閱卷確認腳本：{action_script}"

    skill_python = os.environ.get("MAGI_SKILL_PYTHON", "").strip() or sys.executable
    timeout_sec = int(os.environ.get("MAGI_FILE_REVIEW_CONFIRM_TIMEOUT_SEC", "1800") or "1800")
    source = str(platform or "user").lower() or "user"
    task_text = "confirm_apply " + json.dumps(
        {"token": token, "source": source, "notify": True},
        ensure_ascii=False,
    )

    def run_confirm(uid: str, platform_name: str):
        try:
            proc = subprocess.run(
                [skill_python, action_script, "--task", task_text],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            stdout_text = (proc.stdout or "").strip()
            stderr_text = (proc.stderr or "").strip()
            if proc.returncode != 0:
                result_text = f"❌ 閱卷確認送出失敗（code={proc.returncode}）\n{(stderr_text or stdout_text)[:1200]}"
            else:
                data = None
                if stdout_text:
                    try:
                        data = json.loads(stdout_text)
                    except Exception:
                        m = re.search(r"(\{.*\})\s*$", stdout_text, flags=re.S)
                        if m:
                            try:
                                data = json.loads(m.group(1))
                            except Exception:
                                data = None
                if isinstance(data, dict) and data.get("success") and str(data.get("result") or "") == "Applied":
                    case_label = str(data.get("case", "")).strip()
                    msg = str(data.get("message", "")).strip()
                    result_text = "\n".join(x for x in ["📋 閱卷確認送出完成", case_label, msg] if x)
                elif isinstance(data, dict) and data.get("success"):
                    case_label = str(data.get("case", "")).strip()
                    msg = str(data.get("message", "")).strip()
                    result_key = str(data.get("result", "")).strip()
                    result_text = "\n".join(
                        x for x in [
                            "⚠️ 閱卷確認流程未完成送出",
                            case_label,
                            result_key,
                            msg,
                        ] if x
                    )
                elif isinstance(data, dict):
                    case_label = str(data.get("case", "")).strip()
                    err = str(data.get("error") or data.get("message") or "unknown").strip()
                    result_text = "\n".join(x for x in ["❌ 閱卷確認送出失敗", case_label, err] if x)
                else:
                    result_text = f"📋 閱卷確認流程完成。\n{stdout_text[:1200] if stdout_text else '(無輸出)'}"
        except subprocess.TimeoutExpired:
            result_text = f"⏳ 閱卷確認送出逾時（>{timeout_sec} 秒），請稍後查核。"
        except Exception as exc:
            result_text = f"❌ 閱卷確認背景流程異常：{exc}"

        try:
            if getattr(orch, "notification_callback", None):
                orch.notification_callback(uid, result_text, platform_name)
        except Exception as notify_err:
            logger.warning("File-review confirm callback failed: %s", notify_err)

    threading.Thread(
        target=run_confirm,
        args=(str(user_id or ""), str(platform or "")),
        daemon=True,
    ).start()

    return True, f"📤 已收到閱卷確認碼 {token}，正在重新登入送出。"


def process_message_inner(orch, user_id, message, platform="LINE", role="user", attachment=None, correlation_id=None, progress_callback=None, channel_context=None):
    message = orch._sanitize_incoming_message((message or "").strip())

    # @heavy opt-in：允許使用者觸發 NVIDIA NIM 重型兜底（Plan A, 2026-04-19）
    # 2026-04-24：case-insensitive（@HEAVY / @Heavy 都接受）；全形 ＠ 已在 sanitize 統一轉半形
    _heavy_opt_in = False
    _msg_lower_head = message.lstrip().lower()
    if _msg_lower_head.startswith("@heavy ") or _msg_lower_head.startswith("@重型 "):
        _heavy_opt_in = True
        # 保留原大小寫的其餘內容，只剝除前綴
        message = message.lstrip().split(" ", 1)[1].strip() if " " in message.lstrip() else ""
        logger.info("message_pipeline: @heavy opt-in detected, will try NIM fallback if oMLX fails")
    try:
        from flask import g as _flask_g
        _flask_g.heavy_opt_in = _heavy_opt_in
    except Exception:
        pass

    quick_reply = orch._quick_fixed_reply(message, role)
    if quick_reply:
        orch._append_history(user_id, "user", message)
        orch._append_history(user_id, "assistant", quick_reply)
        return quick_reply
    if not message and not attachment:
        return "✍️ 請輸入文字內容，或上傳檔案後告訴我要做的事。"
    orch._append_history(user_id, "user", message)

    # ── 亂碼回報快捷指令 ──
    gibberish_reply = orch._handle_gibberish_report(user_id, message, platform)
    if gibberish_reply:
        orch._append_history(user_id, "assistant", gibberish_reply)
        return gibberish_reply

    # Defense in depth: never trust upstream "role=admin" unless the sender is allowlisted.
    # This prevents accidental privilege escalation (e.g., Discord guild admin, misrouted requests).
    try:
        if role == "admin" and not orch._is_verified_admin_sender(user_id, platform):
            logger.warning(f"⚠️ Admin role downgraded (unverified): {platform}:{user_id}")
            role = "user"
    except Exception:
        if role == "admin":
            role = "user"

    try:
        if attachment:
            orch.remember_recent_attachment(
                user_id=str(user_id or ""),
                platform=str(platform or ""),
                attachment=attachment,
                source_message=message,
            )
        else:
            recent_attachment = orch._maybe_reuse_recent_attachment(
                str(user_id or ""),
                str(platform or ""),
                message,
            )
            if recent_attachment:
                attachment = recent_attachment
                orch._append_route_trace(
                    str(user_id or ""),
                    str(platform or ""),
                    "pre_route",
                    "recent_attachment_reuse",
                    {"attachment_type": str(recent_attachment.get("type") or "")},
                )
    except Exception as recent_err:
        logger.warning(f"Recent attachment context skipped: {recent_err}")

    # ── docx chat edit router (Phase 3) ──
    try:
        _docx_handled, _docx_reply = _handle_docx_chat_edit_if_any(
            orch, user_id, platform, message, attachment, correlation_id
        )
        if _docx_handled:
            orch._append_history(user_id, "assistant", _docx_reply)
            return _docx_reply
    except Exception as _docx_err:
        logger.warning(f"docx_chat_edit_router skipped: {_docx_err}")

    # ════════════════════════════════════════════════════════════════
    # CHANNEL-AWARE ROUTING — topic fast path + general channel logic
    # ════════════════════════════════════════════════════════════════
    _topic_key = ""
    if channel_context:
        _topic_key = (channel_context.get("topic_key", "") if isinstance(channel_context, dict)
                      else getattr(channel_context, "topic_key", ""))
    if _topic_key:
        orch._append_route_trace(
            str(user_id or ""), str(platform or ""),
            "channel_context", _topic_key,
            {"channel_id": str((channel_context or {}).get("channel_id", ""))},
        )

    # ── Topic Fast Path: specialized channels get priority routing ──
    if _topic_key and _topic_key not in ("general", ""):
        _fast_result = orch._topic_fast_path(
            _topic_key, user_id, message, role, platform, attachment,
        )
        if _fast_result is not None:
            orch._append_history(user_id, "assistant", _fast_result)
            orch._append_route_trace(
                str(user_id or ""), str(platform or ""),
                "topic_fast_path", _topic_key, {},
            )
            return _fast_result
        # fast path returned None → fall through to general logic

    # If the user is responding to a "should I remember this rule?" prompt, handle it first.
    try:
        handled, reply = orch._handle_memory_confirmation_if_any(str(user_id or ""), str(platform or ""), message)
        if handled:
            return reply
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4018, exc_info=True)

    try:
        handled, reply = orch._handle_skill_interview_if_any(str(user_id or ""), str(platform or ""), role, message)
        if handled:
            orch._append_history(user_id, "assistant", reply)
            return reply
    except Exception as skill_interview_err:
        logger.debug(f"Skill interview intercept skipped: {skill_interview_err}")

    if not attachment and orch._looks_like_skill_creation_request(message) and not orch._looks_like_capability_question(message):
        reply = orch._start_skill_interview(
            str(user_id or ""),
            str(platform or ""),
            role,
            message,
            trigger_reason="manual",
        )
        orch._append_history(user_id, "assistant", reply)
        return reply

    # --- Legal Attest Generator Intercept ---
    try:
        import json
        import os
        legal_attest_state_file = f"{_MAGI_ROOT}/.agent/legal_attest_state.json"
        in_legal_flow = False
        if os.path.exists(legal_attest_state_file):
            with open(legal_attest_state_file, 'r', encoding='utf-8') as f:
                legal_st = json.load(f)
            if str(user_id) in legal_st:
                in_legal_flow = True

        msg_l = message.lower()
        trigger_start = any(kw in msg_l for kw in ["存證信函", "寫存證信"]) and any(kw in msg_l for kw in ["寫", "產生", "生成", "幫我", "草擬", "製作"])
        # Don't trigger on question-style phrasing (e.g. "你會寫存證信函嗎？")
        # Those should fall through to _try_conversational_intent for a guide.
        if trigger_start and re.search(r"[嗎嘛呢？\?]$", message.strip()):
            trigger_start = False

        if in_legal_flow or trigger_start:
            if in_legal_flow and any(kw in msg_l for kw in ["取消", "算了", "不要寫", "不寫了", "退出"]):
                with open(legal_attest_state_file, 'r', encoding='utf-8') as f:
                    legal_st = json.load(f)
                if str(user_id) in legal_st:
                    del legal_st[str(user_id)]
                    with open(legal_attest_state_file, 'w', encoding='utf-8') as f:
                        json.dump(legal_st, f)
                return "✅ 已為您取消存證信函流程。"

            from skills.legal_attest.action import handle_chat
            cmd = "init" if (trigger_start and not in_legal_flow) else message
            return handle_chat(str(user_id), cmd)
    except Exception as e:
        logger.error(f"Legal attest flow check failed: {e}")

    # --- 書狀製作 Intercept (DOCX→PDF, 正本/副本/繕本) ---
    try:
        msg_lower_dp = (message or "").lower()
        _DOC_PRODUCER_KWS = [
            "轉pdf", "轉換pdf", "轉成pdf",
            "做正本", "做副本", "做繕本",
            "標正本", "標副本", "標繕本",
            "合併pdf", "書狀製作", "製作書狀",
        ]
        if any(kw in msg_lower_dp for kw in _DOC_PRODUCER_KWS):
            from api.pipelines.skill_dispatch import dispatch_doc_producer
            dp_reply = dispatch_doc_producer(orch, user_id, message, platform=platform)
            if dp_reply:
                return dp_reply
    except Exception as e:
        logger.error(f"doc-producer intercept failed: {e}")

    # --- 案件管理 Intercept (Tasks 1: 建案/查案件/案件清單/狀態更新/業務概況) ---
    try:
        _case_mgmt_kws = [
            "建案", "新案件", "案件清單", "列出案件", "查案件", "案件狀態",
            "改為已結案", "改為進行中", "改為暫停", "改為撤回",
            "業務概況", "案件概況", "今天的案件", "案件狀況",
        ]
        if any(kw in message for kw in _case_mgmt_kws):
            from api.pipelines.skill_dispatch import dispatch_case_management
            _cm_result = dispatch_case_management(message, user_id=user_id, platform=platform)
            if _cm_result:
                return _cm_result
    except Exception as e:
        logger.error(f"case-management intercept failed: {e}")
        return {"text": f"⚠️ 案件管理操作失敗，請稍後再試（{type(e).__name__}）"}

    # --- 當事人管理 Intercept (Task 2: 新增/查詢當事人) ---
    try:
        _client_mgmt_kws = ["新增當事人", "建立當事人", "查當事人", "查客戶"]
        _client_data_pattern = message.endswith("的資料") and len(message) <= 15
        if any(kw in message for kw in _client_mgmt_kws) or _client_data_pattern:
            from api.pipelines.skill_dispatch import dispatch_client_management
            _cli_result = dispatch_client_management(message, user_id=user_id, platform=platform)
            if _cli_result:
                return _cli_result
    except Exception as e:
        logger.error(f"client-management intercept failed: {e}")
        return {"text": f"⚠️ 當事人管理操作失敗，請稍後再試（{type(e).__name__}）"}

    # --- 記帳 Intercept (Task 3: 記收入/記支出/帳務查詢) ---
    try:
        _accounting_kws = ["記收入", "記支出", "本月帳務", "帳務查詢", "本月收支", "帳務概況"]
        if any(kw in message for kw in _accounting_kws):
            from api.pipelines.skill_dispatch import dispatch_accounting
            _acc_result = dispatch_accounting(message, user_id=user_id, platform=platform)
            if _acc_result:
                return _acc_result
    except Exception as e:
        logger.error(f"accounting intercept failed: {e}")
        return {"text": f"⚠️ 記帳操作失敗，請稍後再試（{type(e).__name__}）"}

    # --- 報價單 Intercept (Task 4: 開報價單/報價單清單) ---
    try:
        _quotation_kws = ["開報價單", "報價單清單", "查報價單", "報價單列表"]
        if any(kw in message for kw in _quotation_kws):
            from api.pipelines.skill_dispatch import dispatch_quotation
            _quot_result = dispatch_quotation(message, user_id=user_id, platform=platform)
            if _quot_result:
                return _quot_result
    except Exception as e:
        logger.error(f"quotation intercept failed: {e}")
        return {"text": f"⚠️ 報價單操作失敗，請稍後再試（{type(e).__name__}）"}

    # --- 行事曆事件 Intercept (Task 5: 排庭/排開會) ---
    try:
        _calendar_kws = ["排庭", "排開會", "排會議"]
        if any(message.startswith(kw) for kw in _calendar_kws):
            from api.pipelines.skill_dispatch import dispatch_calendar_event
            _cal_result = dispatch_calendar_event(message, user_id=user_id, platform=platform)
            if _cal_result:
                return _cal_result
    except Exception as e:
        logger.error(f"calendar-event intercept failed: {e}")
        return {"text": f"⚠️ 行事曆操作失敗，請稍後再試（{type(e).__name__}）"}

    # --- 書狀 AI 草擬 Intercept (Task 6: 草擬起訴狀/答辯狀) ---
    try:
        _draft_kws = ["草擬起訴狀", "草擬答辯狀", "草擬聲請狀", "草擬陳報狀", "草擬準備狀",
                      "草擬上訴狀", "草擬抗告狀", "幫我草擬", "幫我起草"]
        if any(kw in message for kw in _draft_kws):
            from api.pipelines.skill_dispatch import dispatch_ai_draft
            _draft_result = dispatch_ai_draft(message, user_id=user_id, platform=platform)
            if _draft_result:
                return _draft_result
    except Exception as e:
        logger.error(f"ai-draft intercept failed: {e}")
        return {"text": f"⚠️ 書狀草擬失敗，請稍後再試（{type(e).__name__}）"}

    # --- 文件產生 Intercept (委任狀/委託書/委任契約書/收據) ---
    try:
        import json as _json_poa
        import os as _os_poa
        poa_state_file = f"{_MAGI_ROOT}/.agent/poa_chat_state.json"
        in_poa_flow = False
        if _os_poa.path.exists(poa_state_file):
            with open(poa_state_file, 'r', encoding='utf-8') as f:
                poa_st = _json_poa.load(f)
            if str(user_id) in poa_st:
                in_poa_flow = True

        msg_l_poa = message.lower() if message else ""
        _action_kws = [
            "做", "製作", "產生", "生成", "幫我", "草擬", "建立", "開", "寫",
            "make", "generate", "create",
        ]
        poa_trigger = (
            any(kw in msg_l_poa for kw in ["委任狀", "委託書", "委任状", "委托书"])
            and any(kw in msg_l_poa for kw in _action_kws)
        )
        contract_trigger = (
            any(kw in msg_l_poa for kw in ["委任契約", "契約書", "委任合約"])
            and any(kw in msg_l_poa for kw in _action_kws)
        )
        receipt_trigger = (
            any(kw in msg_l_poa for kw in ["收據", "收执", "收執"])
            and any(kw in msg_l_poa for kw in _action_kws)
        )
        # 優先級消歧：契約 > 委任狀 > 收據
        if poa_trigger and contract_trigger:
            poa_trigger = "契約" not in msg_l_poa
            contract_trigger = not poa_trigger

        # 不攔截詢問式
        if (poa_trigger or contract_trigger or receipt_trigger) and re.search(r"[嗎嘛呢？\?]$", message.strip()):
            poa_trigger = contract_trigger = receipt_trigger = False

        if in_poa_flow or poa_trigger or contract_trigger or receipt_trigger:
            if in_poa_flow and any(kw in msg_l_poa for kw in ["取消", "算了", "不要", "不做了", "退出"]):
                with open(poa_state_file, 'r', encoding='utf-8') as f:
                    poa_st = _json_poa.load(f)
                if str(user_id) in poa_st:
                    del poa_st[str(user_id)]
                    with open(poa_state_file, 'w', encoding='utf-8') as f:
                        _json_poa.dump(poa_st, f)
                return "✅ 已為您取消製作流程。"

            from api.poa_chat_handler import handle_chat as poa_handle_chat
            if (poa_trigger or contract_trigger or receipt_trigger) and not in_poa_flow:
                poa_st = {}
                if _os_poa.path.exists(poa_state_file):
                    with open(poa_state_file, 'r', encoding='utf-8') as f:
                        poa_st = _json_poa.load(f)
                if receipt_trigger:
                    doc_type = "receipt"
                elif contract_trigger:
                    doc_type = "contract"
                else:
                    doc_type = "poa"
                poa_st[str(user_id)] = {
                    "step": "start" if doc_type == "poa" else "ask_client",
                    "doc_type": doc_type,
                    "_raw_message": message,
                }
                with open(poa_state_file, 'w', encoding='utf-8') as f:
                    _json_poa.dump(poa_st, f, ensure_ascii=False)
                return poa_handle_chat(str(user_id), "smart_init")
            else:
                return poa_handle_chat(str(user_id), message)
    except Exception as e:
        logger.error(f"Document gen chat flow check failed: {e}")

    # LAF 兩階段確認碼路由：進度回報（Plan C）+ 開辦（go_live）
    # 律師回覆「正確送出 <確認碼>」時，先試 progress token，再試 go_live token（kind 嚴格分離）。
    try:
        handled, reply = orch._handle_laf_submit_confirmation_if_any(
            str(user_id or ""),
            str(platform or ""),
            str(role or "user"),
            message,
        )
        if handled:
            return reply
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4158, exc_info=True)

    # 閱卷聲請確認碼：使用者可直接回覆 6 位確認碼或「確認碼xxxxxx」送出。
    try:
        handled, reply = _handle_file_review_confirmation_if_any(
            orch,
            str(user_id or ""),
            str(platform or ""),
            message,
        )
        if handled:
            return reply
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "file_review_confirm", exc_info=True)

    # Route explain (safe): allow both user/admin to ask what would be executed.
    ok_route, probe, route_err = orch._extract_route_probe(message)
    if ok_route:
        if route_err:
            return route_err
        info = orch._explain_routing(probe, role=role)
        return orch._format_route_explain(info, role=role)

    # Deterministic arithmetic tool fast path.
    #
    # Keep this before chatlog/rule-memory capture: prompts like
    # "請用工具算，不要心算" contain "不要" but are instructions for this
    # single calculation, not durable user rules.
    arithmetic_reply = _try_arithmetic_tool_fast_path(message)
    if arithmetic_reply:
        orch._append_route_trace(
            str(user_id or ""),
            str(platform or ""),
            "top_level",
            "arithmetic_calculate",
            {"tool": "calculate"},
        )
        orch._append_history(user_id, "assistant", arithmetic_reply)
        return arithmetic_reply

    # Persist chat + user rules for ALL users (but keep system mutation commands admin-only).
    try:
        orch._maybe_capture_chatlog(str(user_id or ""), str(platform or ""), "user", message)
        rule_flag = orch._maybe_capture_user_rules(str(user_id or ""), str(platform or ""), message)
    except Exception:
        rule_flag = None

    log_msg = f"📥 Received from {user_id} ({platform}) [Role:{role}]: {message}"
    if attachment:
        log_msg += f" [Attachment: {attachment['type']}]"
    logger.info(log_msg)

    # 1. Safety Check (Iron Dome)
    if "rm -rf" in message or "drop table" in message.lower():
        try:
            from skills.evolution.skill_genesis import auto_harden_iron_dome_scope

            auto_harden_iron_dome_scope(
                message,
                source=f"{platform}:{user_id}",
                max_new=2,
            )
        except Exception as e:
            logger.warning(f"Iron Dome auto-harden skipped: {e}")
        if role != 'admin':
            logger.warning(f"🛡️ Iron Dome Triggered by {user_id} (Unauthorized)")
            return "⛔ I cannot do that. You do not have permission."
        else:
            logger.warning(f"⚠️ Admin {user_id} is executing a dangerous command.")
            alert_iron_dome_violation("Dangerous Command (Admin)", "Destructive Keywords", message)

    # 2. Multimedia Processing (High Priority)
    # NOTE: keep attachment routing ahead of NL/text intent routing so file tasks
    # (e.g., "請完整翻譯…") are not hijacked into plain-text flows.
    if attachment:
        orch._append_route_trace(
            str(user_id or ""),
            str(platform or ""),
            "top_level",
            "multimedia",
            {
                "attachment_type": str(attachment.get("type") or ""),
                "filename": str(attachment.get("filename") or "")[:120],
            },
        )
        return orch._handle_multimedia(user_id, message, attachment)

    try:
        handled, codex_reply = orch._handle_codex_distributed_command(message, str(role or "user"))
        if handled:
            orch._append_route_trace(
                str(user_id or ""),
                str(platform or ""),
                "top_level",
                "codex_distributed_command",
                {"role": str(role or "user")},
            )
            return codex_reply
    except Exception as e:
        logger.warning(f"Codex sidecar command routing skipped: {e}")

    # 1.5 Natural-language command router (shared across LINE/Discord/Telegram/web callers)
    # This maps colloquial zh-TW phrases to vetted magi-office-ops commands.
    # ⚠️ LAF report commands (開辦回報/報結/疑義 etc.) have a dedicated parser
    #    in _handle_command → parse_laf_report_payload.  Skip NL route for these
    #    to prevent the external intent_router from mis-parsing client names.
    #
    # 2026-03-29 Channel-aware routing: NL Router keyword interception is now
    # DISABLED in general/LINE channels to prevent conversational messages from
    # being hijacked.  Skills are instead reached via slash commands,
    # EmbeddingRouter (≥0.85), or topic fast path in specialized channels.
    # NL Router is ONLY active in specialized topic channels as a secondary route.
    _nl_router_enabled = bool(_topic_key and _topic_key not in ("general", ""))
    
    # ── Phase A: Casual Fast-Path Bypass ──
    # 注意：畫圖請求 / 自然語言提醒 不得走 small-talk fast-path，否則會繞過對應 handler
    # 導致 persona drift 或 LLM 幻覺式「我已設定提醒」回應。
    _draw_exclude_pattern = re.compile(
        r"(?:/draw\b|畫[圖一個張幅]|\bdraw\b|generate image|產生圖片|绘[图画製]|画[圖图一])",
        re.IGNORECASE,
    )
    _is_draw_request = bool(_draw_exclude_pattern.search(message))
    _reminder_exclude_pattern = re.compile(
        r"(?:明天|今天|後天|\d+月\d+日|\d+號).*?(?:[\d零一二兩三四五六七八九十]+)\s*點.*?(?:提醒|記|備忘|開會|會議)"
        r"|(?:提醒我|幫我記|備忘錄|設個提醒).*?(?:明天|今天|後天|\d+月|[\d零一二兩三四五六七八九十]+\s*點|\d+時)",
    )
    _is_reminder_request = bool(_reminder_exclude_pattern.search(message))
    try:
        from skills.bridge.grounded_ai import is_small_talk_intent, _classify_query_tier
        _msg_tier = _classify_query_tier(message)
        if is_small_talk_intent(message, _msg_tier) and not _is_draw_request and not _is_reminder_request:
            orch._append_route_trace(
                str(user_id or ""), str(platform or ""),
                "top_level", "chat_fast_path",
                {"tier": _msg_tier, "router_suppressed": True, "reason": "small_talk_intent"}
            )
            logger.info("⚡ Casual fast path activated: bypassing all routers for small-talk.")
            return orch._handle_chat_async(user_id, message, platform_hint=platform)
    except Exception as _st_err:
        logger.debug(f"Small-talk fast path check skipped: {_st_err}")
        
    _skip_nl_for_laf = False
    if _nl_router_enabled:
        try:
            _skip_nl_for_laf = orch._parse_laf_report_payload(message) is not None
        except Exception as _laf_parse_err:
            logger.error("LAF parser failed, will NOT skip NL router: %s", _laf_parse_err, exc_info=True)
            _skip_nl_for_laf = False
        if _skip_nl_for_laf:
            logger.info("📋 LAF report detected — skipping NL router (dedicated handler)")
    else:
        logger.debug("🔇 NL Router disabled (general/LINE channel, topic=%s)", _topic_key or "none")
    if _nl_router_enabled and not _skip_nl_for_laf:
        try:
            handled, routed_reply = orch._run_nl_route(
                str(user_id or ""),
                message,
                str(platform or ""),
                str(role or "user"),
            )
            if handled:
                orch._append_route_trace(
                    str(user_id or ""),
                    str(platform or ""),
                    "top_level",
                    "nl_router",
                    {"role": str(role or "user")},
                )
                return routed_reply
        except Exception as e:
            logger.warning(f"NL router skipped due to error: {e}")

    # 1.6 Stock watchlist fallback:
    # when first prompt is pending, accept plain symbol/name replies
    # like "台積電 AAPL" even without explicit "追蹤股票：" prefix.
    try:
        handled, quick_set_reply = orch._try_market_watchlist_quick_set(
            message,
            str(platform or ""),
        )
        if handled:
            orch._append_route_trace(
                str(user_id or ""),
                str(platform or ""),
                "top_level",
                "market_quick_set",
                None,
            )
            return quick_set_reply
    except Exception as e:
        logger.warning(f"Market quick-set fallback skipped: {e}")

    # 2.5. Universal Help/Menu Command (High Priority)
    # Check this before LLM classification to ensure menu always accessible
    msg_lower = message.lower()
    # Capture explicit personal facts into long-term memory for all users.
    orch._maybe_capture_profile_fact(user_id, message)
    # Help: exact match only (same whitelist as message_router.py)
    _HELP_EXACT_MP = HELP_ALIASES
    if msg_lower in _HELP_EXACT_MP:
         return orch._handle_command(user_id, "/help", role=role, platform=platform) # Force route to command handler

    # 2.6. Status Command (High Priority) - exact match only, no bare "狀態"
    _STATUS_EXACT_MP = {"系統狀態", "運作狀態", "節點狀態", "機器狀態", "magi狀態",
                        "magi status", "status", "大腦狀態", "目前模型", "現在模型", "使用什麼模型"}
    if msg_lower in _STATUS_EXACT_MP:
        # Combine Node Status (Heartbeat) + Brain Status (Manager)
        node_status = orch._get_magi_status()
        brain_status = get_brain_status()
        collab_status = orch._get_collaboration_status()
        return f"{node_status}\n\n{brain_status}\n\n{collab_status}"

    # 2.6.5. Authoritative realtime data (weather/stock/fx) before generic
    # semantic routing. This prevents weather questions from drifting into
    # calendar/court reminders or LLM-generated estimates.
    try:
        from skills.engine.realtime_data_gateway import handle_realtime_query
        realtime = handle_realtime_query(message)
        if isinstance(realtime, dict):
            reply = realtime.get("reply") or realtime.get("refusal")
            if reply:
                return str(reply)
    except Exception as e:
        logger.warning(f"Realtime data gateway skipped: {e}")

    # 2.7. Schedule/Meeting Query (High Priority) - Check before LLM
    # Use exact set for shortest phrases; longer ones use startswith/contains but with length guard
    _SCHEDULE_EXACT = {"今天行程", "明天行程", "本週行程", "這週行程", "今天會議", "明天會議",
                       "行程表", "會議表", "日曆", "schedule", "my schedule", "meeting"}
    _schedule_triggered = (
        msg_lower.strip() in {"今天", "明天"}
        or msg_lower in _SCHEDULE_EXACT
        or (len(msg_lower) <= 20 and any(kw in msg_lower for kw in ["行程", "會議", "本週", "這週"]))
    )
    if _schedule_triggered:
        return orch._get_schedule()

    # 2.7.0a Council Core Approval Commands (High Priority — must run before
    #         intent_forge / conversational_intent / semantic router to avoid
    #         being intercepted).
    if any(kw in msg_lower for kw in ["核心變更待審", "core approvals", "pending core changes"]):
        try:
            from skills.magi.council_approval import format_pending_summary
            return format_pending_summary(limit=20)
        except Exception as e:
            return f"❌ 讀取核心待審清單失敗: {e}"

    _ccr_match = re.search(r"(ccr-\d{14})", message)
    if any(kw in msg_lower for kw in ["批准核心變更", "approve core"]) or (
        _ccr_match and any(kw in msg_lower for kw in ["批准", "approve", "ok", "通過"])
    ):
        try:
            from skills.magi.council_approval import resolve_core_change
            # Extract ccr- ID from anywhere in the message
            _ccr_id_m = re.search(r"(ccr-\d{14})", message)
            if not _ccr_id_m:
                return "❓ 請提供待審 ID，例如：`批准 ccr-20260213094500`"
            approval_id = _ccr_id_m.group(1)
            # Extract optional note (everything after the ccr- ID)
            note = message[_ccr_id_m.end():].strip()
            result = resolve_core_change(approval_id, "approved", approver=user_id, note=note)
            if result.get("success"):
                item = result.get("item", {})
                exec_info = item.get("execution", {})
                if exec_info.get("success"):
                    files = ", ".join(exec_info.get("patches_applied", []))
                    return (
                        f"✅ 核心變更已核准並自動執行：`{approval_id}`\n"
                        f"修改檔案：{files}\n"
                        f"備份：{exec_info.get('details', {}).get('backup_dir', '?')}"
                    )
                elif exec_info.get("error"):
                    return (
                        f"✅ 核心變更已核准：`{approval_id}`\n"
                        f"⚠️ 自動執行失敗：{exec_info.get('error', '?')[:200]}\n"
                        f"已自動回滾，需要手動處理。"
                    )
                return f"✅ 核心變更已核准：`{approval_id}`"
            return f"❌ 核准失敗：{result.get('error')}"
        except Exception as e:
            return f"❌ 核准流程錯誤：{e}"

    if any(kw in msg_lower for kw in ["拒絕核心變更", "reject core"]) or (
        _ccr_match and any(kw in msg_lower for kw in ["拒絕", "reject", "不要", "駁回"])
    ):
        try:
            from skills.magi.council_approval import resolve_core_change
            _ccr_id_m = re.search(r"(ccr-\d{14})", message)
            if not _ccr_id_m:
                return "❓ 請提供待審 ID，例如：`拒絕 ccr-20260213094500 原因`"
            approval_id = _ccr_id_m.group(1)
            note = message[_ccr_id_m.end():].strip()
            result = resolve_core_change(approval_id, "rejected", approver=user_id, note=note)
            if result.get("success"):
                return f"🛑 核心變更已拒絕：`{approval_id}`"
            return f"❌ 拒絕失敗：{result.get('error')}"
        except Exception as e:
            return f"❌ 拒絕流程錯誤：{e}"

    # 2.7.6 User crawler targets (chat-callable, persisted into nightly run list)
    if any(kw in msg_lower for kw in ["爬蟲目標", "crawl target", "新增爬蟲", "移除爬蟲", "列出爬蟲", "run_daily"]):
        # 爬蟲管理開放給所有使用者 (2026-03-01)
        try:
            skill_script = f"{_MAGI_ROOT}/skills/crawler-targets/action.py"
            if not os.path.exists(skill_script):
                return "❌ 找不到 crawler-targets skill。"

            url_match = re.search(r"(https?://\S+)", message)
            url = (url_match.group(1).strip() if url_match else "").rstrip(").,")

            if any(k in msg_lower for k in ["列出", "list", "查看"]):
                task_value = "list"
            elif any(k in msg_lower for k in ["移除", "刪除", "remove"]):
                if not url:
                    return "⚠️ 請提供要移除的網址，例如：移除爬蟲目標 https://example.com"
                task_value = "remove " + json.dumps({"url": url}, ensure_ascii=False)
            elif any(k in msg_lower for k in ["run_daily", "立即執行", "立刻執行", "現在執行"]):
                task_value = "run_daily {}"
            else:
                if not url:
                    return "⚠️ 請提供要新增的網址，例如：新增爬蟲目標 https://example.com"
                note = ""
                try:
                    tail = message.split(url, 1)[1].strip()
                    if tail:
                        note = tail[:120]
                except Exception:
                    note = ""
                task_value = "add " + json.dumps({"url": url, "note": note}, ensure_ascii=False)

            proc = subprocess.run(
                [sys.executable, skill_script, "--task", task_value],
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("MAGI_CRAWLER_TARGETS_TIMEOUT_SEC", "90") or "90"),
            )
            out = (proc.stdout or "").strip()
            data = {}
            try:
                data = json.loads(out) if out else {}
            except Exception:
                m = re.search(r"(\{[\s\S]*\})\s*$", out or "")
                if m:
                    try:
                        data = json.loads(m.group(1))
                    except Exception:
                        data = {}

            if proc.returncode != 0 or (isinstance(data, dict) and not data.get("success", False)):
                err = ""
                if isinstance(data, dict):
                    err = str(data.get("error") or "").strip()
                if not err:
                    err = (proc.stderr or out or "unknown error").strip()[:240]
                return f"❌ 爬蟲目標操作失敗：{err}"

            if task_value == "list":
                targets = data.get("targets") if isinstance(data, dict) else []
                if not isinstance(targets, list):
                    targets = []
                if not targets:
                    return "📭 目前沒有自訂爬蟲目標。"
                lines = ["🕸️ 自訂爬蟲目標："]
                for idx, t in enumerate(targets[:20], 1):
                    u = str((t or {}).get("url") or "").strip()
                    n = str((t or {}).get("note") or "").strip()
                    lines.append(f"{idx}. {u}" + (f"（{n}）" if n else ""))
                if len(targets) > 20:
                    lines.append(f"...其餘 {len(targets) - 20} 筆")
                return "\n".join(lines)

            if task_value.startswith("add "):
                return f"✅ 已加入每日爬蟲目標：{url}"
            if task_value.startswith("remove "):
                return f"✅ 已移除爬蟲目標：{url}"
            return "✅ 已執行自訂爬蟲目標每日流程。"
        except Exception as e:
            return f"❌ 爬蟲目標指令失敗：{e}"

    # 2.7.6.4 Research brief (extensible multi-namespace literature crawler)
    _rb_kws = ["研究爬蟲", "研究來源", "研究命名空間", "研究摘要", "研究關鍵字",
               "research brief", "research digest"]
    if any(kw in message for kw in _rb_kws):
        try:
            skill_script = f"{_MAGI_ROOT}/skills/research-brief/action.py"
            if not os.path.exists(skill_script):
                return "❌ 找不到 research-brief skill。"
            # Parse sub-command
            msg = message.strip()
            url_match = re.search(r"(https?://\S+)", msg)
            url = (url_match.group(1).strip() if url_match else "").rstrip(").,")
            # Extract namespace (first non-keyword token after command word)
            _RB_WORDS = {"研究爬蟲", "研究來源", "研究命名空間", "研究摘要", "研究關鍵字",
                         "清單", "新增", "移除", "關鍵字", "查詢", "今日摘要",
                         "research", "brief", "digest", "add", "remove", "list"}
            tokens = [t for t in re.split(r"\s+", msg) if t and not t.startswith("http")]
            non_kw = [t for t in tokens if t not in _RB_WORDS]

            cli_args: list[str] = [sys.executable, skill_script]
            task = "list"
            namespace = non_kw[0] if non_kw else ""
            keyword = ""

            if "新增命名空間" in msg:
                task = "add_namespace"
            elif "移除命名空間" in msg:
                task = "remove_namespace"
            elif "今日摘要" in msg or "digest" in msg.lower():
                task = "digest" if namespace else "digest_all"
            elif "查詢" in msg and namespace:
                task = "query"
                # find quoted keyword
                qm = re.search(r'[「""\'](.+?)[」""\']', msg)
                keyword = qm.group(1) if qm else (non_kw[-1] if len(non_kw) >= 2 else "")
            elif "關鍵字" in msg and namespace:
                kw_parts = [t for t in non_kw if t not in {namespace}]
                keyword = kw_parts[-1] if kw_parts else ""
                task = "remove_keyword" if "移除" in msg else "add_keyword"
            elif ("新增" in msg or "add" in msg.lower()) and url and namespace:
                task = "add_source"
            elif ("移除" in msg or "remove" in msg.lower()) and url and namespace:
                task = "remove_source"
            elif ("清單" in msg or "list" in msg.lower()) and namespace:
                task = "list_namespace"
            elif "清單" in msg or "list" in msg.lower():
                task = "list"
            elif namespace:
                task = "list_namespace"

            cli_args += ["--task", task]
            if namespace and task not in ("list",):
                cli_args += ["--namespace", namespace]
            if url:
                cli_args += ["--url", url]
            if keyword:
                cli_args += ["--keyword", keyword]
            if task in ("digest", "digest_all"):
                cli_args += ["--no-notify"]  # chat path returns summary, cron path notifies

            proc = subprocess.run(
                cli_args, capture_output=True, text=True,
                timeout=int(os.environ.get("MAGI_RESEARCH_BRIEF_TIMEOUT_SEC", "120") or "120"),
            )
            out = (proc.stdout or "").strip()
            data: dict = {}
            try:
                data = json.loads(out) if out else {}
            except Exception:
                m = re.search(r"(\{[\s\S]*\})\s*$", out or "")
                if m:
                    try:
                        data = json.loads(m.group(1))
                    except Exception:
                        data = {}

            if proc.returncode != 0 or (isinstance(data, dict) and not data.get("success", False)):
                err = ""
                if isinstance(data, dict):
                    err = str(data.get("error") or "").strip()
                if not err:
                    err = (proc.stderr or out or "unknown error").strip()[:240]
                return f"❌ 研究爬蟲失敗：{err}"

            if task == "list":
                items = data.get("namespaces", [])
                if not items:
                    return "📭 尚無任何研究命名空間。"
                lines = ["📚 **研究命名空間清單**"]
                for it in items:
                    lines.append(
                        f"• `{it['name']}` — 來源 {it['source_count']} · 關鍵字 {it['keyword_count']} · topic={it['topic_key']}"
                    )
                return "\n".join(lines)
            if task == "list_namespace":
                lines = [
                    f"📂 **{data.get('namespace','?')}**（topic={data.get('topic_key','?')}）",
                    f"關鍵字（{len(data.get('keywords', []))}）：" + ", ".join(data.get("keywords", [])[:10]),
                    f"來源（{len(data.get('sources', []))}）："
                ]
                for s in data.get("sources", [])[:15]:
                    note = f"（{s.get('note')}）" if s.get("note") else ""
                    lines.append(f"  - [{s.get('type','?')}/{s.get('lang','?')}] {s.get('url','')}{note}")
                return "\n".join(lines)
            if task in ("digest", "digest_all"):
                results = data.get("results", [])
                lines = ["✅ 摘要已觸發："]
                for r in results:
                    lines.append(f"• {r.get('namespace')}: {r.get('new_entries', 0)} 則新文獻")
                return "\n".join(lines)
            return data.get("message") or "✅ 已執行。"
        except Exception as e:
            return f"❌ 研究爬蟲指令失敗：{e}"

    # 2.7.6.5 勞動基準法計算
    _labor_kws = ["加班費", "勞基法", "勞動基準法", "特休假", "特別休假", "資遣費",
                  "一例一休", "例假日加班", "休息日加班", "平日加班", "overtime計算",
                  "severance pay", "加班計算", "特休天數"]
    if any(kw in message for kw in _labor_kws):
        if orch._looks_like_capability_question(message):
            return (
                "✅ **我可以幫您計算勞基法相關金額！**\n\n"
                "**加班費**：`月薪 50000，休息日加班 3 小時`\n"
                "**特休假**：`到職日 2020-03-01，我有幾天特休`\n"
                "**資遣費**：`月薪 45000，到職 2018-01-01，現在資遣費多少`"
            )
        return orch._run_labor_law_command(message)

    if any(k in msg_lower for k in ["查判決", "找判決", "判決搜尋", "搜尋判決"]):
        return "Public release: legal-research collection and opinion-library integrations are not included."

    # 2.7.79 Payment dismiss early intercept (bypass intent classification)
    # Messages like "張偉銘已繳費" get misclassified as CHAT; force into CMD path.
    _RE_PAYMENT_DISMISS_EARLY = re.compile(
        r"^(.+?)\s*(?:已經繳費了|已經繳費|繳費完畢了|已繳費|繳費完畢|繳費了)\s*$"
    )
    _payment_early_match = _RE_PAYMENT_DISMISS_EARLY.search(message.strip())
    if not _payment_early_match:
        # Also check prefix forms: "已繳費 XXX", "跳過繳費 XXX"
        for _ptrig in ("已繳費", "跳過繳費", "繳費跳過"):
            if message.strip().startswith(_ptrig):
                _payment_early_match = True
                break
    if _payment_early_match:
        return orch._handle_command(user_id, message, role=role, platform=platform)

    # 2.7.8 Memory Commands (High Priority) - Avoid LLM classification
    if any(msg_lower.startswith(k) for k in ["記住", "remember", "save memory", "memorize", "@magi 記住", "@magi learn"]):
        # 記憶寫入開放給所有使用者 (2026-03-01)
        try:
            content = message
            for kw in ["@MAGI 記住", "@MAGI learn", "remember", "記住", "save memory", "memorize", "請記住", "幫我記住"]:
                content = content.replace(kw, "").strip()
            if len(content) < 2:
                return "🧠 請告訴我要記住什麼？例如：`記住我的車牌是 ABC-1234`"
            from skills.memory.mem_bridge import remember
            remember(
                content,
                source=f"user_chat_{user_id}",
                metadata={
                    "verified": True,
                    "confidence": 0.94,
                    "source_type": "user_confirmed",
                    "role": "user",
                },
            )
            return "🧠 已記住。"
        except Exception as e:
            return f"❌ 記憶寫入失敗: {e}"

    if any(msg_lower.startswith(k) for k in ["forget", "刪除記憶", "delete memory"]) or \
       (msg_lower.startswith("忘記") and not any(exc in msg_lower for exc in ["忘記密碼", "忘記帳號", "忘記帶", "忘記了", "忘記怎麼"])):
        try:
            content = message
            for kw in ["forget", "刪除記憶", "忘記", "delete memory", "把這段記憶刪掉", "請把這段記憶刪掉", "這是錯的"]:
                content = content.replace(kw, "").strip()
            if len(content) < 2:
                return "🧠 請告訴我要刪除哪段記憶？例如：`忘記我之前說的地址`"
            # 非管理員：通知管理員等待授權 (2026-03-01)
            if role != "admin":
                try:
                    from skills.ops.red_phone import alert_admin
                    alert_admin(
                        f"🧠 使用者 {user_id} ({platform}) 要求刪除記憶：\n"
                        f"{content[:300]}\n\n"
                        "請管理員回覆「刪除記憶 <內容>」來確認執行。",
                        severity="warning",
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4468, exc_info=True)
                return "🧠 已將刪除記憶的請求通知管理員，請等待授權後才會執行。"
            from skills.memory.mem_bridge import forget
            success, result_msg = forget(content)
            return f"{'🗑️ 已刪除記憶' if success else '⚠️ 刪除失敗'}\n{result_msg}"
        except Exception as e:
            return f"❌ 記憶刪除失敗: {e}"

    # 2.7.9 Obsidian Commands
    if msg_lower.startswith("obsidian ") or msg_lower.startswith("obsidian\n"):
        import subprocess as _sp
        _obs_parts = message.strip().split(None, 2)
        _obs_cmd = _obs_parts[1].lower() if len(_obs_parts) > 1 else "status"
        _obs_arg = _obs_parts[2] if len(_obs_parts) > 2 else ""
        _obs_py = os.path.join(_MAGI_ROOT if '_MAGI_ROOT' not in dir() else os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "obsidian", "action.py")
        _obs_venv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "venv", "bin", "python3")
        try:
            _obs_argv = [_obs_venv, _obs_py]
            if _obs_cmd in ("search", "ask"):
                _obs_argv += ["--task", _obs_cmd, "--query", _obs_arg]
            elif _obs_cmd == "read":
                _obs_argv += ["--task", "read", "--note", _obs_arg]
            elif _obs_cmd in ("set_vault", "set-vault"):
                _obs_argv += ["--task", "set_vault", "--vault-path", _obs_arg]
            elif _obs_cmd in ("ingest_source", "ingest-source"):
                _obs_argv += ["--task", "ingest_source"]
                if _obs_arg:
                    import shlex as _shlex2
                    try:
                        _is_tokens = _shlex2.split(_obs_arg)
                    except ValueError:
                        _is_tokens = _obs_arg.split()
                    _is_i = 0
                    while _is_i < len(_is_tokens):
                        _tok = _is_tokens[_is_i]
                        if _tok == "--source" and _is_i + 1 < len(_is_tokens):
                            _obs_argv += ["--source", _is_tokens[_is_i + 1]]
                            _is_i += 2
                        elif _tok == "--subpath" and _is_i + 1 < len(_is_tokens):
                            _obs_argv += ["--subpath", _is_tokens[_is_i + 1]]
                            _is_i += 2
                        elif _tok == "--limit" and _is_i + 1 < len(_is_tokens):
                            _obs_argv += ["--limit", _is_tokens[_is_i + 1]]
                            _is_i += 2
                        elif _tok == "--force":
                            _obs_argv += ["--force"]
                            _is_i += 1
                        elif not _tok.startswith("--"):
                            _obs_argv += ["--source", _tok]
                            _is_i += 1
                        else:
                            _is_i += 1
            elif _obs_cmd == "ingest":
                _obs_argv += ["--task", "ingest"]
                if _obs_arg:
                    # Parse --tags, --since, --force, --folder flags
                    import shlex as _shlex
                    try:
                        _ingest_tokens = _shlex.split(_obs_arg)
                    except ValueError:
                        _ingest_tokens = _obs_arg.split()
                    _ingest_i = 0
                    _ingest_folder = ""
                    while _ingest_i < len(_ingest_tokens):
                        _tok = _ingest_tokens[_ingest_i]
                        if _tok == "--tags" and _ingest_i + 1 < len(_ingest_tokens):
                            _obs_argv += ["--tags", _ingest_tokens[_ingest_i + 1]]
                            _ingest_i += 2
                        elif _tok == "--since" and _ingest_i + 1 < len(_ingest_tokens):
                            _obs_argv += ["--since", _ingest_tokens[_ingest_i + 1]]
                            _ingest_i += 2
                        elif _tok == "--force":
                            _obs_argv += ["--force"]
                            _ingest_i += 1
                        elif _tok == "--folder" and _ingest_i + 1 < len(_ingest_tokens):
                            _ingest_folder = _ingest_tokens[_ingest_i + 1]
                            _ingest_i += 2
                        elif not _tok.startswith("--") and not _ingest_folder:
                            _ingest_folder = _tok
                            _ingest_i += 1
                        else:
                            _ingest_i += 1
                    if _ingest_folder:
                        _obs_argv += ["--folder", _ingest_folder]
            elif _obs_cmd == "status":
                _obs_argv += ["--task", "status"]
            elif _obs_cmd == "list_vaults":
                _obs_argv += ["--task", "list_vaults"]
            elif _obs_cmd == "help":
                _obs_argv += ["--task", "help"]
            else:
                _obs_argv += ["--task", _obs_cmd]
                if _obs_arg:
                    _obs_argv += ["--query", _obs_arg]
            _obs_r = _sp.run(_obs_argv, capture_output=True, text=True, timeout=120, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            _obs_out = _obs_r.stdout.strip() or _obs_r.stderr.strip() or "No output"
            try:
                _obs_j = json.loads(_obs_out)
                if _obs_j.get("success") is False:
                    return f"⚠️ Obsidian: {_obs_j.get('error', 'unknown error')}"
                return f"📓 **Obsidian**\n```json\n{json.dumps(_obs_j, ensure_ascii=False, indent=2)}\n```"
            except (json.JSONDecodeError, ValueError):
                return f"📓 Obsidian:\n{_obs_out[:2000]}"
        except _sp.TimeoutExpired:
            return "⏱️ Obsidian 操作超時（120秒）"
        except Exception as e:
            return f"❌ Obsidian 錯誤: {e}"

    # 2.7.5. Intent Forge Debug Continuation (High Priority)
    # If CASPER previously asked a blocker question, treat the next message as feedback unless user issues another command.
    # Only trigger debug-clear for specific phrases, NOT bare "取消"/"算了"/"放棄"
    _DEBUG_CLEAR_EXACT = {"清除除錯", "clear feedback", "取消除錯", "取消修復", "取消debug", "放棄修復", "放棄除錯"}
    if msg_lower in _DEBUG_CLEAR_EXACT:
        try:
            from skills.evolution.intent_forge import clear_pending_issue

            clear_pending_issue(str(user_id))
            return "🧹 已清除待補充除錯流程。"
        except Exception as e:
            return f"❌ 清除待補充除錯失敗: {e}"

    if any(kw in msg_lower for kw in ["補充除錯", "debug feedback", "繼續修復", "continue debug"]):
        try:
            from skills.evolution.intent_forge import forge_continue_with_user_feedback

            feedback = (
                message.replace("補充除錯", "")
                .replace("debug feedback", "")
                .replace("繼續修復", "")
                .replace("continue debug", "")
                .strip()
            )
            result = forge_continue_with_user_feedback(str(user_id), feedback)
            return result.get("reply", "ℹ️ 已收到補充，正在續跑。")
        except Exception as e:
            return f"❌ 續跑除錯失敗: {e}"

    try:
        from skills.evolution.intent_forge import get_pending_issue, forge_continue_with_user_feedback

        pending = get_pending_issue(str(user_id))
        if pending and message and not message.startswith("/") and not message.startswith("@MAGI"):
            result = forge_continue_with_user_feedback(str(user_id), message)
            reply = result.get("reply")
            if reply:
                return reply
    except Exception as e:
        logger.warning(f"Pending intent-forge continuation skipped: {e}")

    # ── 2.7.98 Image Generation Early Route (High Priority) ──
    # 畫圖請求必須在 semantic route / LLM 之前攔截，防止 persona drift（「我是大型語言模型」）。
    # 若 generate_image 失敗，明確回 ❌ 錯誤訊息，絕不走到 LLM persona 拒答。
    _draw_early_pattern = re.compile(
        r"(?:/draw\b|畫[圖一個張幅]|\bdraw\b|generate image|產生圖片|绘[图画製]|画[圖图一])",
        re.IGNORECASE,
    )
    if _draw_early_pattern.search(msg_lower) and not msg_lower.startswith("畫面") and not msg_lower.startswith("畫成"):
        _draw_prompt = message
        for _kw in ["/draw", "幫我", "請", "畫圖", "一張", "一個", "draw", "generate image", "產生圖片", "畫", "画"]:
            _draw_prompt = re.sub(re.escape(_kw), "", _draw_prompt, flags=re.IGNORECASE).strip()
        if len(_draw_prompt) < 2:
            _draw_reply = "🎨 請描述您想要的圖片內容。例如：'畫一隻可愛的貓咪'"
        else:
            _draw_reply = orch._generate_image(_draw_prompt, user_id)
            if not _draw_reply or not str(_draw_reply).strip():
                _draw_reply = "❌ **Melchior 回報錯誤**: 畫圖服務暫時無法使用，請稍後再試。"
        orch._append_history(user_id, "assistant", str(_draw_reply))
        return _draw_reply

    # ── 2.7.99 Comprehensive Natural Language Intent Dispatcher ──
    # Catches conversational phrasing for ALL major skills so the user
    # never gets "no specific skill matched" when asking something MAGI can do.
    nl_reply = orch._try_conversational_intent(message, msg_lower, user_id, role, platform)
    if nl_reply is not None:
        orch._append_history(user_id, "assistant", nl_reply)
        return nl_reply

    try:
        handled, semantic_reply = orch._try_safe_semantic_skill_route(
            str(user_id or ""),
            message,
            str(role or "user"),
            str(platform or ""),
        )
        if handled:
            orch._append_history(user_id, "assistant", semantic_reply)
            return semantic_reply
    except Exception as e:
        logger.warning(f"Primary semantic route skipped: {e}")

    # 2.8. Image Generation (High Priority) - Check before LLM
    # Matches: "/draw xxx", "draw a cat", "幫我畫一隻貓", "請畫圖", "生成圖片: sunset"
    draw_pattern = re.compile(r"(?:/draw\b|畫[圖一個張幅]|\bdraw\b|generate image|產生圖片|绘[图画製]|画[圖图一])", re.IGNORECASE)

    if draw_pattern.search(msg_lower):
        # Extract prompt by removing common command words
        prompt = message
        for kw in ["/draw", "幫我", "請", "畫圖", "一張", "一個", "draw", "generate image", "產生圖片", "畫", "画", "a picture of", "an image of"]:
            prompt = re.sub(re.escape(kw), "", prompt, flags=re.IGNORECASE).strip()

        # If prompt became empty but message was long enough, use original message minus strict command
        if len(prompt) < 2:
             return "🎨 請描述您想要的圖片內容。例如：'畫一隻可愛的貓咪'"

        return orch._generate_image(prompt, user_id)

    # 2.8.5. Code Auto-Fix (High Priority)
    if any(kw in msg_lower for kw in ["自動修復code", "修復code資料夾", "autofix code", "auto fix code", "修復程式碼"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以執行 Code Auto-Fix（系統改動指令）。"
        try:
            from skills.management.code_autofix import autofix_codebase
            target = "magi" if "magi" in msg_lower else "code"
            dry_run = any(k in msg_lower for k in ["dry run", "preview", "只分析", "僅檢查"])
            include_tests = any(k in msg_lower for k in ["含測試", "include tests", "含 tests"])
            internalize = any(k in msg_lower for k in ["內化", "internalize", "技能化"])

            result = autofix_codebase(
                target=target,
                max_files=80,
                max_rounds=2,
                dry_run=dry_run,
                include_tests=include_tests,
                task_hint=message,
                internalize_skill=internalize,
                internalize_name="casper-autofix-knowledge",
            )
            if not result.get("success") and result.get("error"):
                return f"❌ 自動修復啟動失敗: {result.get('error')}"

            verify = result.get("verify", {})
            verify_errors = verify.get("errors", [])
            lines = [
                f"🛠️ **Code Auto-Fix 完成** (`{result.get('target', target)}`)",
                f"- 掃描檔案: {result.get('scanned_files', 0)}",
                f"- 發現語法問題: {result.get('syntax_issue_files', 0)}",
                f"- 修復成功: {result.get('fixed_files', 0)}",
                f"- 修復失敗: {result.get('failed_files', 0)}",
                f"- Dry Run: {result.get('dry_run', False)}",
            ]
            if result.get("fixes"):
                first_fix = result["fixes"][0]
                lines.append(f"- 範例修復: `{first_fix.get('file','')}` (rounds={first_fix.get('rounds', 0)})")
            if verify_errors:
                err = verify_errors[0]
                lines.append(f"⚠️ 驗證仍有錯誤: `{err.get('file','')}` -> {err.get('error','')}")
            if result.get("internalized", {}).get("success"):
                lines.append(f"🧬 已內化技能: `{result['internalized'].get('skill_folder')}`")
            return "\n".join(lines)
        except Exception as e:
            return f"❌ 自動修復流程失敗: {e}"

    # 2.8.6. CODE -> SKILL Internalization (High Priority)
    if any(kw in msg_lower for kw in ["內化code", "code技能化", "內化 code", "skillize code", "code internalize"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以執行 CODE 內化（系統改動指令）。"
        try:
            from skills.management.auto_skill import AutoSkill

            autoskill = AutoSkill()
            source_dir = str(get_magi_root_dir())
            if ("legacy" in msg_lower or "archive" in msg_lower) and legacy_code_enabled():
                source_dir = str(get_legacy_code_root())
            force = any(k in msg_lower for k in ["force", "重建", "重新內化"])
            result = autoskill.internalize_codebase_as_skills(
                source_dir=source_dir,
                max_files=60,
                force=force,
                auto_activate=True,
                enable_release=True,
                canary_percent=20,
                promote_min_runs=12,
                promote_max_failure_rate=0.2,
            )
            if not result.get("success"):
                return f"❌ CODE 內化失敗: {result.get('message', result.get('error', 'unknown'))}"
            canary_started = 0
            stable_set = 0
            for item in result.get("items", []):
                rel = item.get("release", {}) or {}
                if isinstance(rel.get("canary"), dict) and rel.get("canary", {}).get("success"):
                    canary_started += 1
                if isinstance(rel.get("stable"), dict) and rel.get("stable", {}).get("success"):
                    stable_set += 1
            return (
                "🧬 CODE 內化完成\n"
                f"- Source: `{result.get('source_dir')}`\n"
                f"- 掃描檔案: {result.get('scanned_files', 0)}\n"
                f"- 新增/更新技能: {result.get('created_skills', 0)}\n"
                f"- 略過: {result.get('skipped_files', 0)}\n"
                f"- 新增知識: {result.get('learned_tips', 0)}\n"
                f"- Canary 啟動: {canary_started}\n"
                f"- Stable 設定: {stable_set}"
            )
        except Exception as e:
            return f"❌ CODE 內化流程失敗: {e}"

    if any(kw in msg_lower for kw in ["導入auto-skill", "import auto-skill", "toolsai auto-skill"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以導入 auto-skill（系統改動指令）。"
        try:
            from skills.management.auto_skill import AutoSkill

            autoskill = AutoSkill()
            result = autoskill.import_toolsai_auto_skill(notify_dc=True)
            if result.get("success"):
                dc = result.get("dc_notify", {}) if isinstance(result.get("dc_notify"), dict) else {}
                return (
                    "📥 Toolsai auto-skill 導入完成\n"
                    f"- 新增知識: {result.get('learned', 0)}\n"
                    f"- 檔案數: {len(result.get('imported_files', []))}\n"
                    f"- DC通知: line={dc.get('line')} discord={dc.get('discord')}"
                )
            return f"❌ 導入失敗: {result.get('message', result.get('error', 'unknown'))}"
        except Exception as e:
            return f"❌ 導入 auto-skill 流程失敗: {e}"

    if any(kw in msg_lower for kw in ["code cycle", "自動巡檢", "工作流程自動化", "流程自動化"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以執行自動巡檢（系統改動指令）。"
        try:
            from scripts.code_skill_cycle import run_cycle

            result = run_cycle()
            if not result.get("success"):
                return "❌ 自動巡檢流程失敗。請查看 `logs` 與 `skill events`。"
            af = result.get("autofix", {})
            ci = result.get("code_internalization", {})
            return (
                "⚙️ 自動巡檢完成\n"
                f"- AutoFix: fixed={af.get('fixed_files',0)} failed={af.get('failed_files',0)}\n"
                f"- Code->Skill: created={ci.get('created_skills',0)} skipped={ci.get('skipped_files',0)}"
            )
        except Exception as e:
            return f"❌ 自動巡檢執行失敗: {e}"

    # 2.8.7. Translation (High Priority)
    # (Conversational translation queries now handled by _try_conversational_intent above)

    if message.startswith("翻譯 ") or message.lower().startswith("translate "):
        try:
            from skills.bridge.tri_sage_collab import translate_text

            text = message.replace("翻譯 ", "", 1).replace("translate ", "", 1).strip()
            if not text:
                return "❓ 請提供要翻譯的文字。"
            result = translate_text(text, target_lang="繁體中文", source_lang="auto", mode="full")
            if result.get("success"):
                translated_text = str(result.get("text") or "").strip()
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
                        source_text=text,
                        translated_text=translated_text,
                        prefix="full_translation",
                        user_id=str(user_id or ""),
                    )
                    if not exported_reply:
                        exported_reply = orch._export_translation_txt(
                            translated_text=translated_text,
                            source=(text[:240] + "…") if len(text) > 240 else text,
                            provider=str(result.get("provider") or "tri-sage"),
                            mode="full_translation",
                            prefix="full_translation",
                            user_id=str(user_id or ""),
                        )
                    if exported_reply:
                        return exported_reply
                return f"🌐 翻譯結果（{result.get('provider','tri-sage')}）:\n{translated_text}"
            return f"❌ 翻譯失敗: {result.get('error')}"
        except Exception as e:
            return f"❌ 翻譯流程失敗: {e}"

    # 2.8.8. Music Generation (High Priority)
    if message.startswith("製作音樂 ") or message.startswith("生成音樂 ") or message.lower().startswith("make music "):
        try:
            from skills.bridge.tri_sage_collab import generate_music

            prompt = (
                message.replace("製作音樂 ", "", 1)
                .replace("生成音樂 ", "", 1)
                .replace("make music ", "", 1)
                .strip()
            )
            if not prompt:
                return "❓ 請提供音樂風格或需求，例如：`製作音樂 溫暖鋼琴、30秒`"
            result = generate_music(prompt, duration_sec=30)
            if result.get("success"):
                return f"🎵 音樂已產生：`{result.get('path','')}`（{result.get('provider','tri-sage')}）"
            return f"❌ 音樂生成失敗: {result.get('error')}"
        except Exception as e:
            return f"❌ 音樂生成流程失敗: {e}"

    # 2.9. Code Analysis (High Priority)
    # Matches: "analyze code", "讀取程式碼", "code folder", "改善建議"
    if any(kw in msg_lower for kw in ["analyze code", "讀取程式碼", "code folder", "code資料夾", "連動模式", "改善建議", "read code"]):
        # Extract basic params
        target = "code"
        if "magi" in msg_lower:
            target = "magi"

        # Async Code Analysis
        from skills.bridge.code_analysis import estimate_effort

        # 1. Estimate Effort
        est = estimate_effort(target)
        if est["success"]:
             wait_msg = f"🧐 **收到請求**\n已識別 {est['file_count']} 個關鍵檔案 (總計 {est['total_files']} 個)。\n**預估分析時間: {est['estimated_minutes']} 分鐘**\n\n正在進行深度分析，請稍候... (背景執行中)"
        else:
             wait_msg = f"🧐 **收到請求**\n正在讀取 `{target}` 資料夾並進行深度分析...\n這個過程可能需要幾分鐘。 (背景執行中)"

        def run_analysis(uid, target_kw, instructions):
            try:
                from skills.bridge.code_analysis import analyze_code
                logger.info(f"🧵 Starting background analysis for {uid}...")
                report = analyze_code(target_kw, instructions)

                if hasattr(orch, 'notification_callback') and orch.notification_callback:
                    header = f"🧐 **程式碼分析報告 (完成)**\n\n"
                    orch.notification_callback(uid, header + report, "Discord")
                else:
                    logger.warning("⚠️ Analysis done but no callback registered to notify user.")

            except Exception as e:
                logger.error(f"❌ Background Analysis Failed: {e}")
                if hasattr(orch, 'notification_callback') and orch.notification_callback:
                    try:
                        orch.notification_callback(uid, "❌ 分析過程中發生錯誤，請再試一次。", "Discord")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4883, exc_info=True)

        # Start background thread
        thread = threading.Thread(target=run_analysis, args=(user_id, target, message))
        thread.daemon = True
        thread.start()

        return wait_msg

    # 2.10. List Skills (High Priority)
    # Matches: any message containing "skill" or "技能" or "功能" combined with listing/query words
    skill_kws = ["skill", "技能", "功能列表"]
    if any(kw in msg_lower for kw in skill_kws) and any(w in msg_lower for w in ["表", "列", "list", "哪些", "什麼", "告訴", "功能", "show", "help"]):
        return orch._list_skills()

    # 2.11. System Monitor (系統監控) — require multi-word context for short keywords
    _sysmon_exact = {"系統狀態", "system status", "系統監控", "健康檢查", "service health"}
    _sysmon_trigger = (
        msg_lower in _sysmon_exact
        or (len(msg_lower) <= 20 and any(kw in msg_lower for kw in ["cpu使用", "ram使用", "記憶體使用", "磁碟空間", "磁碟使用"]))
        or (any(kw in msg_lower for kw in ["系統狀態", "系統監控", "健康檢查", "service health"]))
    )
    if _sysmon_trigger:
        try:
            from skills.ops.system_monitor import get_system_status, check_service_health
            if any(kw in msg_lower for kw in ["服務", "service", "健康"]):
                return check_service_health()
            return get_system_status()
        except Exception as e:
            from skills.management.issue_tracker import log_issue
            log_issue(message, str(e), "System Monitor Skill")
            return f"❌ 系統監控失敗，已加入夜議檢討: {e}"

    # 2.11.5 Process Guardian (程序守護者)
    if any(kw in msg_lower for kw in ["check duplicates", "檢查分身", "kill duplicates", "刪除分身", "process check", "檢查重複"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以清理重複程序（系統改動指令）。"
        try:
            from skills.ops.process_guardian import check_and_clean_duplicates
            # Check Discord Bot by default, maybe check others too if requested?
            # For now focus on the main culprit: discord_bot.py
            report = check_and_clean_duplicates("api/discord_bot.py")
            return report
        except Exception as e:
            return f"❌ Process Guardian Error: {e}"

    # 2.11.7.1 Zombie Patrol (殭屍巡邏)
    if any(kw in msg_lower for kw in ["殭屍巡邏", "zombie patrol", "巡邏殭屍", "殭屍清除", "zombie clean"]):
        try:
            from daemon import reap_orphan_workers, get_reap_report
            dry = "模擬" in message or "dry" in msg_lower
            reap_orphan_workers(force=True, dry_run=dry)
            report = get_reap_report()
            if not report:
                return "✅ 系統乾淨。"
            return report
        except Exception as e:
            return f"❌ 殭屍巡邏失敗: {e}"

    # 2.11.8 Raw URL Reader (網頁閱讀) - High Priority
    # Catch messages that are just a URL or start with a URL
    url_only_match = re.match(r'^(https?://[^\s]+)', message.strip())
    if url_only_match:
        try:
            url = url_only_match.group(1)

            # Check for image extensions - if image, let multimedia handler or Melchior Vision handle it?
            # Actually, orchestrator flows linearly. Multimedia check is at #2 (lines 80).
            # But message text might be a URL to an image.
            if any(url.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                # Let it fall through or handle as image analysis? 
                # For now, let's treat as webpage unless we add specific image URL handling.
                pass

            logger.info(f"🌐 Detected Raw URL: {url} -> Fetching via Web Research")

            # Fetch
            fetch_result = fetch_url_content(url, max_length=6000, exempt_iron_dome=True)

            if fetch_result.get('success'):
                content = fetch_result.get('content', '')
                title = fetch_result.get('title', url)
                prompt = f"User sent this URL: {url}\n\nPlease summarize the content in Traditional Chinese (繁體中文). Focus on the key points.\n\nTitle: {title}\n\nContent:\n{content}"

                # Summarize via InferenceGateway (oMLX → remote → local fallback)
                _gw = orch._inference_gw
                resp = _gw.chat(prompt, task_type="summary", timeout=120, heavy=_heavy_opt_in)
                summary = resp.get("response", "無法產生摘要。")

                if "error" in resp and resp["error"]:
                    summary += f"\n(Error: {resp['error']})"

                return f"🌐 **{title}**\n(來源: {url})\n\n{summary}"
            else:
                return f"❌ 無法讀取網頁: {fetch_result.get('error', '未知錯誤')}"
        except Exception as e:
            logger.error(f"Web Fetch Error: {e}")
            return f"❌ 網頁讀取發生錯誤: {e}"

    # 2.11.9 Webpage Translate/Summarize (網頁翻譯/摘要) - High Priority
    # If user asks to translate/summarize a webpage, prefer HTML section extraction over Playwright visible-text scraping.
    if re.search(r"https?://", msg_lower) and any(kw in msg_lower for kw in ["翻譯", "translate", "摘要", "總結", "整理"]):
        try:
            # Decide mode:
            # - If user explicitly asks for 摘要/總結/整理 => summary mode.
            # - If user just says 翻譯 (or says 不要摘要) => full-translation mode (no summarization).
            wants_translate = any(kw in msg_lower for kw in ["翻譯", "translate"])
            wants_summary = any(kw in msg_lower for kw in ["摘要", "總結", "整理"])
            no_summary = any(kw in msg_lower for kw in ["不要摘要", "不用摘要", "不需要摘要", "不要總結", "不用總結", "不需要總結"])
            disable_txt = any(kw in msg_lower for kw in ["不要txt", "不需要txt", "no txt", "inline", "直接貼上"])

            # For web translation, default to exporting formatted TXT unless explicitly disabled.
            force_txt = wants_translate and (not wants_summary) and (not disable_txt)
            if "full translation without summary" in msg_lower or "完整翻譯不摘要" in msg_lower:
                wants_translate = True
                wants_summary = False
                force_txt = not disable_txt
            elif no_summary:
                wants_summary = False

            url_match = re.search(r"https?://[^\s]+", message)
            if url_match:
                url = url_match.group().strip()
                logger.info(f"🌐 Webpage translate/summarize requested: {url}")

                # For full translation we need more raw content; for summary we can keep it tighter.
                if wants_translate and (not wants_summary):
                    sec = fetch_url_sections(url, max_length=160000, max_sections=12, exempt_iron_dome=True)
                else:
                    sec = fetch_url_sections(url, max_length=60000, max_sections=8, exempt_iron_dome=True)
                if not sec.get("success"):
                    return f"❌ 無法讀取網頁分頁內容: {sec.get('error')}"

                title = (sec.get("title") or "").strip() or "Web Page"
                sections = sec.get("sections") or []
                if not sections:
                    return f"❌ 找不到可用的分頁內容（來源: {url}）"

                # Push a progress note early (LINE will receive via server-registered callback).
                try:
                    if getattr(orch, "notification_callback", None):
                        tab_names = [((s.get("title") or s.get("id") or "分頁").strip()) for s in sections]
                        orch.notification_callback(
                            user_id,
                            "🧾 我已抓到這個網頁的分頁，正在整理翻譯與摘要：\n- " + "\n- ".join(tab_names[:8]),
                            platform,
                        )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5024, exc_info=True)

                def _truncate(txt: str, n: int) -> str:
                    t = (txt or "").strip()
                    if len(t) <= n:
                        return t
                    return t[:n] + "\n...（內容過長已截斷）"

                def _chunk_by_paragraph(txt: str, limit_chars: int = 3800) -> list[str]:
                    """
                    Split by blank lines first, then pack into chunks to keep prompt sizes stable.
                    """
                    s = (txt or "").strip()
                    if not s:
                        return []
                    parts = re.split(r"\n\s*\n", s)
                    chunks = []
                    buf = ""
                    for p in parts:
                        p = (p or "").strip()
                        if not p:
                            continue
                        candidate = (buf + "\n\n" + p).strip() if buf else p
                        if len(candidate) <= limit_chars:
                            buf = candidate
                            continue
                        if buf:
                            chunks.append(buf)
                        # If a single paragraph is huge, hard-split.
                        if len(p) > limit_chars:
                            for i in range(0, len(p), limit_chars):
                                chunks.append(p[i : i + limit_chars])
                            buf = ""
                        else:
                            buf = p
                    if buf:
                        chunks.append(buf)
                    return chunks

                blocks = []
                for s in sections:
                    sid = (s.get("id") or "").strip()
                    stitle = (s.get("title") or "").strip() or (sid or "分頁")
                    serr = (s.get("error") or "").strip()
                    content = (s.get("content") or "").strip()
                    if serr and not content:
                        blocks.append(f"### {stitle}\n⚠️ 讀取失敗或被鐵穹擋下：{serr}")
                        continue
                    if not content:
                        continue
                    # Summary path uses truncated blocks; full-translation uses raw blocks but chunked later.
                    if wants_translate and (not wants_summary):
                        blocks.append(f"### {stitle}\n{content}")
                    else:
                        blocks.append(f"### {stitle}\n{_truncate(content, 6500)}")

                if not blocks:
                    return f"❌ 分頁內容皆為空或被擋下（來源: {url}）"

                model = (os.environ.get("MAGI_MAIN_MODEL") or os.environ.get("MAGI_MAIN_LLM") or TEXT_PRIMARY_MODEL).strip()

                if wants_translate and (not wants_summary):
                    # Full translation mode: preserve structure, do NOT summarize.
                    out_parts = [
                        f"🌐 **{title}**",
                        f"來源: {url}",
                        "",
                        "（完整翻譯，不摘要。若內容太長會改用 TXT 連結傳送。）",
                        "",
                    ]
                    total_tabs = len(sections)
                    done_tabs = 0

                    for s in sections:
                        sid = (s.get("id") or "").strip()
                        stitle = (s.get("title") or "").strip() or (sid or "分頁")
                        serr = (s.get("error") or "").strip()
                        content = (s.get("content") or "").strip()

                        if serr and not content:
                            out_parts.append(f"## {stitle}\n⚠️ 讀取失敗或被鐵穹擋下：{serr}\n")
                            done_tabs += 1
                            continue
                        if not content:
                            done_tabs += 1
                            continue

                        # Progress ping per tab.
                        try:
                            if getattr(orch, "notification_callback", None):
                                orch.notification_callback(
                                    user_id,
                                    f"📄 正在完整翻譯分頁：{stitle}（{done_tabs + 1}/{total_tabs}）",
                                    platform,
                                )
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5120, exc_info=True)

                        chunks = _chunk_by_paragraph(content, limit_chars=3800)

                        def _translate_tab_chunk(idx, ch, total):
                            tprompt = f"""
請把下列英文內容「完整翻譯」成繁體中文（臺灣用語）。

規則：
1. 不要摘要、不省略。
2. 盡量保留原本段落、清單、標點與引用格式。
3. 專有名詞（人名、機構、案件名）保留原文為主（例如 Dickson, United Kingdom, European Court of Human Rights）。
4. 條文請寫 Article 8 或 第8條，不要寫第八章。
5. 請直接輸出翻譯結果，不要加入任何註解或修稿痕跡。

[段落 {idx}/{total}]
{ch}
""".strip()
                            _gw = orch._inference_gw
                            r = _gw.chat(tprompt, task_type="translate", timeout=240, heavy=_heavy_opt_in)
                            t = (r.get("response") or "").strip()
                            if not (r.get("success") and t):
                                err = (r.get("error") or "unknown").strip()
                                return idx, f"（⚠️ 此段翻譯失敗：{err}）\n{_truncate(ch, 1200)}"
                            return idx, t

                        from concurrent.futures import as_completed
                        from api.thread_pools import inference_pool
                        translated_buf = [None] * len(chunks)
                        tab_futs = {inference_pool.submit(_translate_tab_chunk, i+1, ch, len(chunks)): i for i, ch in enumerate(chunks)}
                        for f in as_completed(tab_futs):
                                fi = tab_futs[f]
                                try:
                                    _, txt = f.result()
                                    translated_buf[fi] = txt
                                except Exception as e:
                                    translated_buf[fi] = f"（⚠️ 此段翻譯發生系統錯誤：{e}）"
                        translated_chunks = [t for t in translated_buf if t is not None]

                        out_parts.append(f"## {stitle}\n" + "\n\n".join(translated_chunks) + "\n")
                        done_tabs += 1

                    text = "\n".join(out_parts).strip()

                    # TXT export is handled after cleanup/normalization so saved file is the final output.
                else:
                    # Summary mode (fast): Single-pass summarization/translation for all tabs.
                    combined = "\n\n".join(blocks)
                    prompt = f"""
你是 CASPER。以下是一個網頁（同頁含多個分頁/章節）的英文內容摘錄。請用繁體中文（臺灣用語）輸出「翻譯式摘要」（不是逐字翻譯）。

要求：
0. **必須使用繁體字**（不要出現簡體字）。
1. 先給「整體重點」(8-14 點條列)。
2. 再給「各分頁重點」：每個分頁 4-8 點條列 + 2-4 句白話說明。
3. 最後給「我建議先看哪幾個分頁」(最多 4 個) + 原因。
4. 禁止編造；只能依內容推導。
5. **法規/條約/法院名稱請以內容原文為準**：不要自行改成別的條約或法規；若不確定正式中文名稱，直接保留英文。
6. 任何數字（金額、年份、條文編號、判決結果）若內容沒明講，就不要寫「具體數字」。
7. 不要夾雜英文單字（除非是原文專有名詞，且你不確定正式中文譯名）。
8. 不要引用外部資料或你自己的知識；只用下方內容。
9. 人名/地名/機構名請以原文為主（例如 Dickson, United Kingdom），不要自行翻成其他語言或不常見譯名。
10. 請直接輸出「最終版本」，不要出現任何修稿痕跡或註解，例如「修改成：」、「更正：」、「草稿：」、「思考：」。
11. 條文請寫「Article 8」或「第8條」（不要寫「第八章」）。

[專有名詞固定寫法（請務必遵守）]
- Dickson：一律寫 Dickson（不要自行翻譯成中文名）
- The United Kingdom：可寫「英國」或「United Kingdom」（擇一即可）
- European Court of Human Rights：可寫「歐洲人權法院」

[網頁標題]
{title}

[來源]
{url}

[分頁內容]
{combined}
""".strip()

                    _gw = orch._inference_gw
                    resp = _gw.chat(prompt, task_type="translate", timeout=240, heavy=_heavy_opt_in)
                    text = (resp.get("response") or "").strip()
                    if not (resp.get("success") and text):
                        err = (resp.get("error") or "unknown").strip()
                        return f"❌ 網頁翻譯/摘要失敗：{err}"

                # Force Traditional Chinese (Taiwan) output even if the model slips into Simplified.
                try:
                    from opencc import OpenCC
                    text = OpenCC("s2twp").convert(text)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5212, exc_info=True)

                # If the model leaks Japanese/English, do a quick cleanup pass.
                try:
                    import re as _re

                    def _needs_cleanup(t: str) -> bool:
                        s = (t or "").strip()
                        if not s:
                            return False
                        # Hiragana/Katakana
                        if _re.search(r"[\u3040-\u30ff]", s):
                            return True
                        # Cyrillic / Hangul (model occasionally leaks non-Chinese scripts)
                        if _re.search(r"[\u0400-\u04ff\uac00-\ud7af]", s):
                            return True
                        # Too many latin words (ignore URLs)
                        no_urls = _re.sub(r"https?://\S+", "", s)
                        if len(_re.findall(r"[A-Za-z]{4,}", no_urls)) >= 8:
                            return True
                        return False

                    if _needs_cleanup(text):
                        cleanup_prompt = f"""
請把下列內容「改寫成全篇繁體中文（臺灣用語）」版本，並遵守：
1. 不要出現日文（平假名/片假名/日文漢字用法）或英文單字（除非是原文專有名詞且無合適中文譯名；但也請盡量翻成中文）。
2. 保留原本的章節結構、清單與順序。
3. 不要新增任何新資訊；只做語言與用詞修正。
4. 人名/地名/機構名以原文為主（例如 Dickson, United Kingdom），不要自行翻成別的語言或不常見譯名。

[原內容]
{text}
""".strip()
                        _gw_clean = orch._inference_gw
                        resp2 = _gw_clean.chat(cleanup_prompt, task_type="tc_review", timeout=120)
                        fixed = (resp2.get("response") or "").strip()
                        if resp2.get("success") and fixed:
                            text = fixed
                            try:
                                from opencc import OpenCC
                                text = OpenCC("s2twp").convert(text)
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5254, exc_info=True)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5256, exc_info=True)

                # Hard-sanitize any non-Chinese scripts that can slip through (Cyrillic/Kana/Hangul).
                try:
                    import re as _re
                    text = _re.sub(r"[\u3040-\u30ff\u0400-\u04ff\uac00-\ud7af]", "", text)
                    # Common bad translations / normalization.
                    text = text.replace("應徵者", "申請人")
                    text = _re.sub(r"文章\s*8", "第8條", text)
                    text = _re.sub(r"[ \t]{2,}", " ", text).strip()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5267, exc_info=True)

                # In full-translation mode text already includes header.
                if wants_translate and (not wants_summary):
                    # Export final cleaned translation to TXT by default for web translation.
                    try:
                        export_long = os.environ.get("MAGI_EXPORT_LONG_TEXT", "1").strip().lower() in {"1", "true", "yes", "on"}
                        threshold = int(os.environ.get("MAGI_EXPORT_TEXT_THRESHOLD", "9000") or "9000")
                    except Exception:
                        export_long, threshold = True, 9000
                    if force_txt or (export_long and len(text) >= threshold):
                        # Prefer DOCX bilingual table
                        exported_reply = orch._export_translation_docx(
                            source_text=locals().get("combined", ""),
                            translated_text=text,
                            title=title or "",
                            prefix="web_translate",
                            user_id=str(user_id or ""),
                        )
                        if not exported_reply:
                            exported_reply = orch._export_translation_txt(
                                translated_text=text,
                                source=url,
                                provider=f"melchior:{model}",
                                mode="web_full_translation",
                                prefix="web_translate",
                                user_id=str(user_id or ""),
                            )
                        if exported_reply:
                            return exported_reply
                        if force_txt:
                            return "⚠️ DOCX/TXT 匯出失敗，先提供內文結果（可稍後再輸出）。\n\n" + text
                    return text
                return f"🌐 **{title}**\n來源: {url}\n\n{text}"
        except Exception as e:
            logger.error(f"Webpage translate/summarize error: {e}")
            return f"❌ 網頁翻譯/摘要發生錯誤: {e}"

    # 2.12. Browser Automation (瀏覽器) — only trigger when a URL/domain is present
    _has_url = bool(re.search(r'https?://[^\s]+', message))
    _has_domain = bool(re.search(r'(?:打開|瀏覽|open|browse)\s+([a-zA-Z0-9][\w\-]*\.[a-zA-Z]{2,})', message, re.IGNORECASE))
    if (_has_url or _has_domain) and any(kw in msg_lower for kw in ["打開", "瀏覽", "browse", "open url", "截圖", "screenshot", "網頁"]):
        try:
            from skills.browser.browser_control import browse_url, take_screenshot
            url_match = re.search(r'https?://[^\s]+', message)
            if url_match:
                url = url_match.group()
                if "截圖" in msg_lower or "screenshot" in msg_lower:
                    return take_screenshot(url)
                return browse_url(url)
            domain_match = re.search(r'(?:打開|瀏覽|open|browse)\s+([a-zA-Z0-9][\w\-]*\.[a-zA-Z]{2,}(?:/\S*)?)', message, re.IGNORECASE)
            if domain_match:
                url = f"https://{domain_match.group(1)}"
                return browse_url(url)
        except Exception as e:
            from skills.management.issue_tracker import log_issue
            log_issue(message, str(e), "Browser Skill")
            return f"❌ 瀏覽器操作失敗，已加入夜議檢討: {e}"

    # 2.13. File Manager (檔案管理) — require explicit action verbs, not just "檔案"+"找"
    _filemgr_trigger = (
        any(kw in msg_lower for kw in ["搜尋檔案", "列出檔案", "列出目錄", "搜尋檔", "search file", "list file", "list directory"])
        or (msg_lower.startswith("檔案") and any(w in msg_lower for w in ["列表", "搜尋", "list", "search"]))
    )
    if _filemgr_trigger:
        try:
            from skills.ops.file_manager import list_directory, search_files
            if any(kw in msg_lower for kw in ["搜尋", "search", "找"]):
                # Extract search term
                return search_files(_MAGI_ROOT, message.split("搜尋")[-1].split("search")[-1].strip()[:30])
            return list_directory(_MAGI_ROOT)
        except Exception as e:
            from skills.management.issue_tracker import log_issue
            log_issue(message, str(e), "File Manager Skill")
            return f"❌ 檔案管理失敗，已加入夜議檢討: {e}"

    # 2.14. RSS Reader (RSS 閱讀器)
    # RSS — require "rss" keyword or explicit subscribe+URL pattern
    _rss_trigger = (
        "rss" in msg_lower
        or ("subscribe" in msg_lower and re.search(r'https?://', message))
        or (any(kw in msg_lower for kw in ["訂閱rss", "rss訂閱", "新聞訂閱", "訂閱新聞", "讀新聞", "read news"]))
    )
    if _rss_trigger:
        try:
            from skills.research.rss_reader import RSSReader
            reader = RSSReader()

            result = ""
            # Subscribe logic
            if "訂閱" in message or "subscribe" in msg_lower or "add" in msg_lower:
                if role != "admin":
                    return "⛔ 抱歉，只有管理員可以新增 RSS 訂閱（系統改動指令）。"
                url_match = re.search(r'https?://[^\s]+', message)
                if url_match:
                    result = reader.add_feed(url_match.group())
                else:
                    result = "❌ 請提供 RSS URL，例如: `@MAGI 訂閱 https://news.google.com/rss`"
            else:
                # List/Read logic
                result = reader.read_latest()

            if result.startswith("❌"):
                from skills.management.issue_tracker import log_issue
                log_issue(message, result, "RSS Skill")
                return f"{result}\n(已加入夜議檢討)"
            return result

        except Exception as e:
            from skills.management.issue_tracker import log_issue
            log_issue(message, str(e), "RSS Skill")
            return f"❌ RSS 操作失敗，已加入夜議檢討: {e}"

    # 2.15. GitHub Monitor (GitHub 監控)
    if "github" in msg_lower and any(w in msg_lower for w in ["趨勢", "trend", "search", "搜尋", "找"]):
        try:
            from skills.research.github_monitor import search_repos, get_trending

            result = ""
            if "趨勢" in message or "trend" in msg_lower:
                result = get_trending()
            else:
                # Search
                query = message.split("搜尋")[-1].split("search")[-1].split("github")[-1].strip()
                if not query: query = "AI Agent"
                result = search_repos(query)

            if result.startswith("❌"):
                from skills.management.issue_tracker import log_issue
                log_issue(message, result, "GitHub Monitor Skill")
                return f"{result}\n(已加入夜議檢討)"
            return result

        except Exception as e:
            from skills.management.issue_tracker import log_issue
            log_issue(message, str(e), "GitHub Monitor Skill")
            return f"❌ GitHub 操作失敗，已加入夜議檢討: {e}"

    if "重試摘要佇列自動" in message or "retry_summary_queue_auto" in msg_lower:
        return "Public release: legal-research summary retry is not included."

    # 2.17. Smart Summary (智能摘要) — require explicit intent phrases, not bare "重點"
    _summary_has_url = bool(re.search(r'https?://[^\s]+', message))
    _summary_trigger = (
        _summary_has_url and any(kw in msg_lower for kw in ["摘要", "summarize", "summary", "重點"])
    ) or (
        any(kw in msg_lower for kw in ["摘要", "summarize", "summary"])
        and (msg_lower.startswith("摘要") or msg_lower.startswith("summarize") or msg_lower.startswith("summary") or len(msg_lower) <= 30)
    ) or (
        msg_lower.startswith("重點整理") or msg_lower.startswith("重點摘要")
    )
    if _summary_trigger:
        try:
            from skills.ops.smart_summary import summarize_url, extract_key_points
            url_match = re.search(r'https?://[^\s]+', message)
            if url_match:
                return summarize_url(url_match.group())
            return extract_key_points(message)
        except Exception as e:
            from skills.management.issue_tracker import log_issue
            log_issue(message, str(e), "Smart Summary Skill")
            return f"❌ 摘要失敗，已加入夜議檢討: {e}"

    # 2.18. Cortex Integration (皮質整合)
    if any(kw in msg_lower for kw in ["爬蟲", "crawler", "sync", "同步"]) and any(w in msg_lower for w in ["run", "exec", "執行", "start", "force"]):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以執行爬蟲/同步（系統改動指令）。"
        try:
            if "爬蟲" in message or "crawler" in msg_lower:
                from skills.law_firm.legal_crawler_wrapper import run_crawler
                result = run_crawler()
            elif "同步" in message or "sync" in msg_lower:
                from skills.memory.cortex_sync import CortexSync
                result = CortexSync().run_sync()
            else:
                return "❓ 請指定操作: `執行爬蟲` 或 `執行同步`"

            if result.startswith("❌"):
                from skills.management.issue_tracker import log_issue
                log_issue(message, result, "Cortex Integration")
                return f"{result}\n(已加入夜議檢討)"
            return result

        except Exception as e:
            from skills.management.issue_tracker import log_issue
            log_issue(message, str(e), "Cortex Integration")
            return f"❌ Cortex 操作失敗，已加入夜議檢討: {e}"

    # 2.19. Crawler Architect (爬蟲建築師)
    if (
        "修改爬蟲" in message
        or "modify crawler" in msg_lower
        or ("修改" in message and ("爬蟲" in message or "crawler" in msg_lower))
    ):
        # Only trigger if specifically asking to modify crawler
        if "爬蟲" in message or "crawler" in msg_lower:
            if role != "admin":
                return "⛔ 抱歉，只有管理員可以修改爬蟲（系統改動指令）。"
            try:
                requirement = message.replace("@MAGI", "").replace("修改爬蟲", "").replace("修改", "").strip()
                if not requirement:
                    return "❓ 請說明需求，例如: `@MAGI 修改爬蟲 幫我爬 PTT Stock 版`"

                from skills.law_firm.crawler_architect import CrawlerArchitect
                architect = CrawlerArchitect()
                return architect.execute_modification(requirement)
            except Exception as e:
                return f"❌ 建築師執行失敗: {e}"

    # 2.19. Auto-Skill Learning / Teaching / Internalization
    if (
        message.startswith("@MAGI 教學檔案")
        or message.startswith("@MAGI teach file")
        or message.startswith("教學檔案")
        or message.startswith("teach file")
    ):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以教學/內化檔案（系統改動指令）。"
        try:
            tip_file = (
                message.replace("@MAGI 教學檔案", "")
                .replace("@MAGI teach file", "")
                .replace("教學檔案", "")
                .replace("teach file", "")
                .strip()
            )
            if not tip_file:
                return "❓ 請提供教學檔案路徑，例如：`教學檔案 /path/to/notes.txt`"
            from skills.management.auto_skill import AutoSkill

            autoskill = AutoSkill()
            result = autoskill.learn_from_file(tip_file)
            return result.get("message", "📘 教學檔案已處理。")
        except Exception as e:
            return f"❌ 教學檔案處理失敗: {e}"

    if (
        message.startswith("@MAGI 教學")
        or message.startswith("@MAGI teach")
        or message.startswith("教學 ")
        or message.startswith("teach ")
    ):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以教學（系統改動指令）。"
        try:
            lesson = (
                message.replace("@MAGI 教學", "")
                .replace("@MAGI teach", "")
                .replace("教學 ", "")
                .replace("teach ", "")
                .strip()
            )
            if not lesson:
                return "❓ 請告訴我要學的內容，例如：`教學 遇到 timeout 要先檢查網路與服務健康`"
            from skills.management.auto_skill import AutoSkill

            autoskill = AutoSkill()
            result = autoskill.teach(lesson, context="user-teach", source=f"{platform}:{user_id}")
            return result.get("message", "🧠 教學完成。")
        except Exception as e:
            return f"❌ 教學失敗: {e}"

    # 2.19b. ClaWHub skill search / acquire with Iron Dome review (admin only)
    _clawhub_search_kws = ["搜尋skill", "搜尋 skill", "clawhub search", "skill search", "找skill", "找 skill"]
    _clawhub_install_kws = ["安裝skill", "安裝 skill", "acquire skill", "install skill", "clawhub install"]
    if role == "admin" and any(kw in msg_lower for kw in _clawhub_search_kws):
        try:
            query = re.sub(r"@magi\s+", "", msg_lower)
            for kw in _clawhub_search_kws:
                query = query.replace(kw, "").strip()
            if not query:
                return "❓ 請提供搜尋關鍵字，例如：`@MAGI 搜尋skill pdf converter`"
            from skills.magi.skill_acquire import search_clawhub, format_search_result
            result = search_clawhub(query)
            return format_search_result(result)
        except Exception as e:
            return f"❌ ClaWHub 搜尋失敗: {e}"

    if role == "admin" and any(kw in msg_lower for kw in _clawhub_install_kws):
        try:
            slug = re.sub(r"@magi\s+", "", message.strip())
            for kw in _clawhub_install_kws + ["@MAGI", "@magi"]:
                slug = re.sub(re.escape(kw), "", slug, flags=re.IGNORECASE).strip()
            if not slug:
                return "❓ 請提供 slug，例如：`@MAGI 安裝skill pdf-tools`"
            from skills.magi.skill_acquire import acquire_skill
            result = acquire_skill(slug)
            return result.get("message") or (
                f"技能 '{slug}' 安裝成功。" if result.get("ok")
                else f"❌ 安裝失敗：{result.get('error', '未知錯誤')}\n"
                     + ("\n".join(result.get("violations", []))[:800] if result.get("violations") else "")
            )
        except Exception as e:
            return f"❌ 技能安裝失敗: {e}"

    if (
        message.startswith("@MAGI 內化技能")
        or message.startswith("@MAGI internalize skill")
        or message.startswith("內化技能")
        or message.startswith("internalize skill")
    ):
        try:
            name = (
                message.replace("@MAGI 內化技能", "")
                .replace("@MAGI internalize skill", "")
                .replace("內化技能", "")
                .replace("internalize skill", "")
                .strip()
            )
            from skills.management.auto_skill import AutoSkill

            autoskill = AutoSkill()
            result = autoskill.internalize_as_skill(
                skill_name=name or "casper-learned-skill",
                description="Internalized user-taught CASPER knowledge.",
                auto_activate=True,
            )
            if result.get("success"):
                return (
                    f"{result.get('message')}\n"
                    f"路徑: `{result.get('skill_path')}`"
                )
            return f"❌ 內化技能失敗: {result.get('message')}"
        except Exception as e:
            return f"❌ 內化技能失敗: {e}"

    if message.startswith("@MAGI 記住") or message.startswith("@MAGI learn"):
        if role != "admin":
            return "⛔ 抱歉，只有管理員可以寫入長期經驗（系統改動指令）。"
        try:
            tip = message.replace("@MAGI 記住", "").replace("@MAGI learn", "").strip()
            if not tip:
                return "❓ 請告訴我需要記住什麼經驗。"

            # Fingerprint from tip itself or context? Ideally context.
            # For now, just save it under extracted keywords from the tip.
            from skills.management.auto_skill import AutoSkill
            autoskill = AutoSkill()
            # Simple keyword extraction from the tip content
            keywords = [w for w in re.split(r'\s+', tip) if len(w) > 1]

            result = autoskill.learn(keywords, tip, context="user-manual")
            return result.get("message", "🧠 已記住。")
        except Exception as e:
            return f"❌ 記憶失敗: {e}"

    # 2.20. Council Core Approval Commands (High Priority)
    if any(kw in msg_lower for kw in ["核心變更待審", "core approvals", "pending core changes"]):
        try:
            from skills.magi.council_approval import format_pending_summary

            return format_pending_summary(limit=20)
        except Exception as e:
            return f"❌ 讀取核心待審清單失敗: {e}"

    if any(kw in msg_lower for kw in ["批准核心變更", "approve core"]):
        try:
            from skills.magi.council_approval import resolve_core_change

            text = (
                message.replace("批准核心變更", "")
                .replace("approve core", "")
                .strip()
            )
            if not text:
                return "❓ 請提供待審 ID，例如：`批准核心變更 ccr-20260213094500`"
            parts = text.split(maxsplit=1)
            approval_id = parts[0]
            note = parts[1] if len(parts) > 1 else ""
            result = resolve_core_change(approval_id, "approved", approver=user_id, note=note)
            if result.get("success"):
                return f"✅ 核心變更已核准：`{approval_id}`"
            return f"❌ 核准失敗：{result.get('error')}"
        except Exception as e:
            return f"❌ 核准流程錯誤：{e}"

    if any(kw in msg_lower for kw in ["拒絕核心變更", "reject core"]):
        try:
            from skills.magi.council_approval import resolve_core_change

            text = (
                message.replace("拒絕核心變更", "")
                .replace("reject core", "")
                .strip()
            )
            if not text:
                return "❓ 請提供待審 ID，例如：`拒絕核心變更 ccr-20260213094500 缺少回滾方案`"
            parts = text.split(maxsplit=1)
            approval_id = parts[0]
            note = parts[1] if len(parts) > 1 else ""
            result = resolve_core_change(approval_id, "rejected", approver=user_id, note=note)
            if result.get("success"):
                return f"🛑 核心變更已拒絕：`{approval_id}`"
            return f"❌ 拒絕失敗：{result.get('error')}"
        except Exception as e:
            return f"❌ 拒絕流程錯誤：{e}"

    # 3. [Auto-Skill] Proactive Recall
    try:
        from skills.management.auto_skill import AutoSkill
        autoskill = AutoSkill()
        tips = autoskill.recall(message)
        if tips:
            # 過濾掉佔位符和無實質內容的 tip（防止 LLM 幻覺）
            _PLACEHOLDER_MARKERS = ["此分類記錄", "經驗條目會自動添加", "最佳實踐。"]
            tips = [t for t in tips if not any(m in t for m in _PLACEHOLDER_MARKERS)]
            if tips:
                tips_str = "\n".join(tips)
                logger.info(f"💡 Auto-Skill Recalled: {tips_str[:120]}")
                message += f"\n\n[Auto-Skill 經驗提示]:\n{tips_str}"
    except Exception as e:
        logger.error(f"Auto-Skill Recall Error: {e}")

    # 4. Routing via Hybrid Mode (Deep Thinking)
    if "@MAGI 深度思考" in message or "@MAGI deep" in message or "deep think" in msg_lower:
        from skills.bridge.melchior_bridge import generate_text

        # Remove trigger
        clean_prompt = message.replace("@MAGI 深度思考", "").replace("@MAGI deep", "").strip()
        if not clean_prompt:
            return "❓ 請輸入深度思考的內容。"

        logger.info("🚀 Routing to deep think (%s)...", TEXT_PRIMARY_MODEL)
        response = generate_text(clean_prompt)

        if response:
            reply = f"🧠 [Deep Think]:\n{response}"
            orch._append_history(user_id, "assistant", reply)
            return reply

        fallback = orch._handle_chat_async(user_id, clean_prompt, platform_hint=platform)
        reply = f"⚠️ Melchior 無回應，轉由本地 Casper 回答：\n{fallback}"
        orch._append_history(user_id, "assistant", reply)
        return reply

    # 5. Intent Classification
    # Hard override: LAF report commands should always enter CMD path.
    forced_cmd = False
    if any(k in msg_lower for k in ["法扶回報指令", "法扶指令", "回報指令", "開辦回報", "開辦案件"]):
        forced_cmd = True
    elif orch._parse_laf_report_payload(message):
        forced_cmd = True
    # 自然語言提醒（如「明天下午三點提醒我開會」）的 classifier 會誤判為 CHAT 走 LLM，
    # 導致幻覺式「我已設定提醒」。強制路由到 CMD，讓 command_dispatch 的 _RE_NATURAL_REMINDER
    # 給出誠實「不支援」回覆。
    elif _is_reminder_request:
        forced_cmd = True

    intent = "CMD" if forced_cmd else orch.classifier.classify(message)
    logger.info(f"🧠 Detected Intent: {intent}")
    orch._append_route_trace(
        str(user_id or ""),
        str(platform or ""),
        "classifier",
        str(intent or ""),
        {"role": str(role or "user")},
    )

    # 6. Routing — Embedding Router (primary) → legacy if/elif → SemanticRouter (fallback)
    response = ""

    # 6.0 Embedding-based skill dispatch (fast, runs before legacy handlers)
    # Route ONCE here; reuse result for CHAT override below (avoid duplicate embed call)
    _embed_dispatched = False
    _er_cached_result = None
    try:
        from skills.bridge.embedding_router import get_router as _get_embed_router
        _er = _get_embed_router()
        _er_cached_result = _er.route(message) if _er.is_ready else None
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5744, exc_info=True)

    if intent in ("CMD", "QUERY"):
        try:
            _er_result = _er_cached_result
            # LAF 回報指令已有專屬 handler，不讓 EmbeddingRouter 攔截
            if _er_result and not forced_cmd:
                _er_skill, _er_score, _er_tier = _er_result
                orch._append_route_trace(
                    str(user_id or ""), str(platform or ""),
                    "embedding_router", str(_er_skill),
                    {"score": round(_er_score, 3), "tier": _er_tier, "intent": intent},
                )
                if _er_tier == "DIRECT":
                    _handled, _reply = orch._dispatch_safe_semantic_skill(
                        user_id, message, _er_skill, role, platform
                    )
                    if _handled and _reply:
                        logger.info(f"🧭 EmbeddingRouter DIRECT dispatch: {_er_skill} ({_er_score:.3f})")
                        response = _reply
                        _embed_dispatched = True
                elif _er_tier == "GUIDED" and intent == "QUERY":
                    # For QUERY with a GUIDED match, try the skill before falling to generic query
                    _handled, _reply = orch._dispatch_safe_semantic_skill(
                        user_id, message, _er_skill, role, platform
                    )
                    if _handled and _reply:
                        logger.info(f"🧭 EmbeddingRouter GUIDED dispatch (QUERY): {_er_skill} ({_er_score:.3f})")
                        response = _reply
                        _embed_dispatched = True
            else:
                # Fix #8: trace when embedding router returns no match
                if _er.is_ready:
                    orch._append_route_trace(
                        str(user_id or ""), str(platform or ""),
                        "embedding_router", "no_match",
                        {"intent": intent, "reason": "cooldown_or_api_error" if _er._last_embed_error else "low_score"},
                    )
        except Exception as _er_err:
            logger.debug(f"EmbeddingRouter error: {_er_err}")

    if not _embed_dispatched and intent == "CMD":
        try:
            response = orch._handle_command(user_id, message, role=role, platform=platform)
        except Exception as _cmd_err:
            logger.error(f"❌ _handle_command crashed: {_cmd_err}", exc_info=True)
            response = f"❌ 指令處理失敗：{type(_cmd_err).__name__}: {str(_cmd_err)[:200]}"
        # 6a. Semantic fallback: if CMD handler returned nothing, try semantic router
        if not response:
            try:
                from skills.bridge.semantic_router import route as _semantic_route, suggest_trigger
                sr = _semantic_route(message)
                if sr and sr.get("confidence", 0) >= 0.45:
                    synthetic = suggest_trigger(sr["skill"], message)
                    logger.info(f"SemanticRouter fallback: {sr['skill']} ({sr['confidence']:.2f}) → '{synthetic[:60]}'")
                    orch._append_route_trace(
                        str(user_id or ""),
                        str(platform or ""),
                        "semantic_fallback",
                        str(sr["skill"]),
                        {"confidence": float(sr.get("confidence") or 0.0), "method": str(sr.get("method") or "")},
                    )
                    response = orch._handle_command(user_id, synthetic, role=role, platform=platform)
                    if not response:
                        response = orch._handle_query(user_id, message, platform_hint=platform)
            except Exception as _sr_err:
                logger.debug(f"SemanticRouter error: {_sr_err}")
            # Fix #1: When CMD falls through all routers, try ensemble tools then LLM chat
            if not response:
                # Ensemble tools path (feature flag gated)
                try:
                    from skills.bridge.ensemble_inference import ensemble_chat_with_tools, _ENSEMBLE_TOOLS_ENABLED, format_magi_response
                    if _ENSEMBLE_TOOLS_ENABLED:
                        logger.info("🔧 CMD fallthrough → ensemble_chat_with_tools: '%s'", message[:60])
                        _ecr = ensemble_chat_with_tools(prompt=message, timeout_sec=80)
                        if _ecr and _ecr.result:
                            response = format_magi_response(_ecr)
                            orch._append_route_trace(
                                str(user_id or ""), str(platform or ""),
                                "ensemble_tools", "cmd_fallthrough",
                                {"unanimous": _ecr.unanimous, "tools": _ecr.individual_results.get("tools_used", [])},
                            )
                except Exception as _et_err:
                    logger.debug("ensemble_chat_with_tools error: %s", _et_err)

            if not response:
                logger.warning(f"⚠️ CMD fell through all routers: '{message[:80]}' → defaulting to LLM chat")
                orch._append_route_trace(
                    str(user_id or ""), str(platform or ""),
                    "cmd_fallthrough", "llm_chat",
                    {"message_preview": message[:60]},
                )
                _chat_fallback = orch._handle_chat_async(user_id, message, platform_hint=platform)
                if _chat_fallback:
                    response = f"⚠️ 找不到對應的指令，以對話方式回覆：\n\n{_chat_fallback}"
                else:
                    response = "⚠️ 找不到對應的指令，也無法產生回覆。請嘗試用 /help 查看可用指令。"
    elif not _embed_dispatched and intent == "QUERY":
        # 6b. Before pure QUERY, check if semantic router suggests a concrete skill action
        _sr_fired = False
        try:
            from skills.bridge.semantic_router import route as _semantic_route, suggest_trigger
            sr = _semantic_route(message)
            if sr and sr.get("confidence", 0) >= 0.28 and sr.get("method") in {"semantic", "llm"}:
                synthetic = suggest_trigger(sr["skill"], message)
                logger.info(f"SemanticRouter QUERY override: {sr['skill']} ({sr['confidence']:.2f})")
                orch._append_route_trace(
                    str(user_id or ""),
                    str(platform or ""),
                    "semantic_override",
                    str(sr["skill"]),
                    {"confidence": float(sr.get("confidence") or 0.0), "method": str(sr.get("method") or "")},
                )
                _candidate = orch._handle_command(user_id, synthetic, role=role, platform=platform)
                if _candidate:
                    response = _candidate
                    _sr_fired = True
        except Exception as _sr_err:
            logger.debug(f"SemanticRouter QUERY check error: {_sr_err}")
        if not _sr_fired and orch._should_start_skill_interview_from_gap(message, role, intent="QUERY", er_result=_er_cached_result):
            response = orch._start_skill_interview(
                str(user_id or ""),
                str(platform or ""),
                role,
                message,
                trigger_reason="gap",
            )
        if not response:
            # Ensemble tools path (feature flag gated)
            try:
                from skills.bridge.ensemble_inference import ensemble_chat_with_tools, _ENSEMBLE_TOOLS_ENABLED, format_magi_response
                if _ENSEMBLE_TOOLS_ENABLED:
                    logger.info("🔧 QUERY fallthrough → ensemble_chat_with_tools: '%s'", message[:60])
                    _ecr = ensemble_chat_with_tools(prompt=message, timeout_sec=80)
                    if _ecr and _ecr.result:
                        response = format_magi_response(_ecr)
                        orch._append_route_trace(
                            str(user_id or ""), str(platform or ""),
                            "ensemble_tools", "query_fallthrough",
                            {"unanimous": _ecr.unanimous, "tools": _ecr.individual_results.get("tools_used", [])},
                        )
            except Exception as _et_err:
                logger.debug("ensemble_chat_with_tools error: %s", _et_err)
        if not response:
            response = orch._handle_query(user_id, message, platform_hint=platform)
    elif intent == "CHAT":
        # 6c. Even for CHAT, check if embedding router has a DIRECT match
        # (catches cases where IntentionClassifier misses actionable messages)
        # Reuse _er_cached_result from above — no duplicate embedding call
        # 2026-03-29: In general/LINE channels (no topic), raise threshold to
        # 0.85 to prevent casual mentions from hijacking conversations.
        if not _embed_dispatched:
            try:
                _er_result = _er_cached_result
                if _er_result:
                    _er_skill, _er_score, _er_tier = _er_result
                    from skills.bridge.embedding_router import _DIRECT_THRESH
                    _chat_er_thresh = 0.85 if not _topic_key or _topic_key == "general" else _DIRECT_THRESH
                    if _er_tier == "DIRECT" and _er_score >= _chat_er_thresh:
                        _handled, _reply = orch._dispatch_safe_semantic_skill(
                            user_id, message, _er_skill, role, platform
                        )
                        if _handled and _reply:
                            logger.info(f"🧭 EmbeddingRouter CHAT override: {_er_skill} ({_er_score:.3f})")
                            orch._append_route_trace(
                                str(user_id or ""), str(platform or ""),
                                "embedding_chat_override", str(_er_skill),
                                {"score": round(_er_score, 3)},
                            )
                            response = _reply
                            _embed_dispatched = True
            except Exception as _er_err:
                logger.debug(f"EmbeddingRouter CHAT check error: {_er_err}")
        if not _embed_dispatched and orch._should_start_skill_interview_from_gap(message, role, intent="CHAT", er_result=_er_cached_result):
            response = orch._start_skill_interview(
                str(user_id or ""),
                str(platform or ""),
                role,
                message,
                trigger_reason="gap",
            )
            _embed_dispatched = True
        if not _embed_dispatched:
            response = orch._handle_chat_async(user_id, message, platform_hint=platform)
    elif intent == "DANGER":
        # Second-pass guard against false positives:
        # only trigger Iron Dome "danger" flow when deterministic destructive
        # command patterns are actually present in the message.
        danger_hit = re.search(
            r"(rm\s+-rf|drop\s+table|delete\s+from|truncate\s+table|shutdown\s+-h|reboot\s+now)",
            message or "",
            re.IGNORECASE,
        )
        if not danger_hit:
            logger.warning(
                "⚠️ Intent was DANGER but no deterministic destructive token matched; downgraded to CHAT. user=%s platform=%s",
                user_id,
                platform,
            )
            response = orch._handle_chat_async(user_id, message, platform_hint=platform)
        else:
            try:
                alert_iron_dome_violation("Dangerous Command", f"{platform}:{user_id}", message)
                try:
                    from skills.evolution.skill_genesis import auto_harden_iron_dome_scope

                    auto_harden_iron_dome_scope(message, source=f"{platform}:{user_id}", max_new=2)
                except Exception as e:
                    logger.warning(f"Iron Dome auto-harden (intent danger) skipped: {e}")
            except Exception as e:
                logger.warning(f"Iron Dome alert failed: {e}")
            response = "🛡️ 已偵測高風險指令，已啟動防護並記錄事件。請改用安全且可審核的操作。"
    else:
        response = orch._handle_chat_async(user_id, message, platform_hint=platform)

    # If we flagged an ambiguous rule, append a quick confirmation question.
    if rule_flag == "ASK_CONFIRM":
        response = (response or "").rstrip() + (
            "\n\n我有點不確定你這句話要不要當成「規則」記起來。\n"
            "要記的話回我：`要`；不記回我：`不要`；要改寫回我：`改成：...`"
        )

    orch._append_history(user_id, "assistant", response)
    return response
