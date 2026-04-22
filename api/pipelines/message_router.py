"""
Message routing, intent detection, and conversational dispatch
extracted from Orchestrator.

All functions accept an ``orch`` parameter (the Orchestrator instance)
instead of ``self``, keeping the same logic but as standalone functions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time

from api.help_text import HELP_ALIASES, build_help_text

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def read_openclaw_primary_model() -> str:
    try:
        p = os.path.join(os.path.expanduser("~"), ".openclaw", "openclaw.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            m = (((cfg or {}).get("agents") or {}).get("defaults") or {}).get("model") or {}
            primary = str(m.get("primary") or "").strip()
            if primary:
                return primary
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "read_openclaw_primary_model", exc_info=True)
    return "未設定"


# ── Gibberish report ───────────────────────────────────────────────

_GIBBERISH_REPORT_RE = re.compile(
    r"^(亂碼|這是亂碼|你回的是亂碼|剛才是亂碼|那是亂碼|gibberish|亂碼回報)$",
    re.IGNORECASE,
)
from pathlib import Path as _Path
_GIBBERISH_LOG_PATH = _Path(os.environ.get(
    "MAGI_GIBBERISH_LOG",
    str(_Path(__file__).resolve().parent.parent.parent / "static" / "gibberish_samples.jsonl"),
))


def handle_gibberish_report(orch, user_id, message: str, platform: str = "") -> Optional[str]:
    """使用者回報「亂碼」→ 取上一則 assistant 回覆存入 JSONL，供偵測模組學習。"""
    if not _GIBBERISH_REPORT_RE.search((message or "").strip()):
        return None

    hist = list(orch.user_history.get(user_id, []))
    last_assistant = None
    for entry in reversed(hist):
        if entry.get("role") == "assistant":
            last_assistant = entry.get("content", "")
            break

    if not last_assistant or len(last_assistant) < 5:
        return "⚠️ 找不到上一則回覆，無法記錄。"

    try:
        _GIBBERISH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        record = {
            "ts": _dt.now(_tz.utc).isoformat(),
            "user_id": str(user_id or ""),
            "platform": str(platform or ""),
            "text": last_assistant,
        }
        with _GIBBERISH_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(record, ensure_ascii=False) + "\n")
        # Size-based rotation
        try:
            from api.events.sinks import rotate_jsonl
            rotate_jsonl(_GIBBERISH_LOG_PATH)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"亂碼回報寫入失敗: {e}")
        return "⚠️ 記錄失敗，請稍後重試。"

    try:
        from api.tw_output_guard import _GIBBERISH_KEYWORDS
        text = last_assistant.strip()
        if len(text) >= 6:
            from collections import Counter as _Counter
            trigrams = [text[i:i+3] for i in range(len(text) - 2)]
            trigrams = [t for t in trigrams if not re.match(r"^[\s，。、；：！？「」『』（）\n]", t)]
            common = _Counter(trigrams).most_common(3)
            for gram, count in common:
                if count >= 2 and gram not in _GIBBERISH_KEYWORDS:
                    _GIBBERISH_KEYWORDS.append(gram)
                    logger.info(f"[亂碼學習] 新增關鍵字: {gram}")
    except Exception:
        pass  # best-effort

    return f"✅ 已記錄該亂碼回覆，偵測模組會自動學習。感謝回報！"


def quick_fixed_reply(orch, message: str, role: str = "user") -> Optional[str]:
    """Deterministic quick-replies for frequent operational questions."""
    t = str(message or "").strip().lower()
    if not t:
        return None

    # ── Guard: only match exact short phrases to avoid false positives on natural language ──
    if t in ("下一步", "接下來", "後續怎麼做", "next step"):
        return "下一步建議：1) 先確認 LINE/DC/TG 通道都正常 2) 跑一次自我測試 3) 針對失敗項目自動修復。"

    if t in ("請用繁體中文", "不是繁體中文"):
        return "收到，後續我會固定使用繁體中文（臺灣用語）回覆。"

    # Help/command list: ONLY match exact short phrases.
    # Never intercept conversational messages that happen to contain keywords.
    _HELP_EXACT = HELP_ALIASES
    if t in _HELP_EXACT:
        return build_help_text(role)

    _MODEL_EXACT = {
        "目前模型", "現在模型", "使用什麼模型", "模型是什麼", "模型為何",
        "what model", "你現在使用什麼模型", "現在使用什麼模型",
    }
    if t in _MODEL_EXACT:
        from api.model_config import TEXT_PRIMARY_MODEL
        target_main = (os.environ.get("MAGI_MAIN_MODEL") or TEXT_PRIMARY_MODEL).strip() or TEXT_PRIMARY_MODEL
        omlx_models = []
        try:
            import requests as _req
            try:
                from api.routing.service_registry import get_service_url as _gsurl
                _omlx_base = _gsurl("omlx_inference")
            except Exception:
                _omlx_base = "http://127.0.0.1:8080"
            _r = _req.get(f"{_omlx_base}/v1/models", timeout=3)
            if _r.status_code == 200:
                omlx_models = [m.get("id", "") for m in _r.json().get("data", [])]
        except Exception:
            pass
        active = ", ".join(omlx_models[:4]) if omlx_models else "oMLX 離線"
        return (
            f"推理引擎：oMLX (port 8080)\n"
            f"可用模型：{active}\n"
            f"主要模型：{target_main}\n"
            f"模式：本地推理 oMLX（Ollama 已退役）"
        )

    return None


def brain_runtime_banner() -> str:
    """Runtime banner — returns empty to avoid cluttering every reply."""
    return ""


# ── NL Router ──────────────────────────────────────────────────────

def nl_router_enabled() -> bool:
    v = str(os.environ.get("MAGI_ENABLE_NL_COMMAND_ROUTER", "1")).strip().lower()
    return v in {"1", "true", "yes", "on"}


def should_try_nl_route(orch, message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    if text.startswith("/") or text.startswith("!"):
        return False
    if len(text) > 120000:
        return False
    ambiguous_short_phrases = {
        "翻譯", "全文翻譯", "完整翻譯", "不要摘要",
        "整篇全文", "整篇翻譯", "摘要", "總結",
    }
    if len(text) <= 16 and text.replace(" ", "") in ambiguous_short_phrases:
        return False
    low_compact = text.lower().replace(" ", "")
    for kw_lower in orch._NL_STOCK_PHRASES_LOWER:
        if kw_lower in low_compact:
            return False
    low = text.lower()
    for i, kw in enumerate(orch._NL_ROUTE_KWS):
        if kw in text or orch._NL_ROUTE_KWS_LOWER[i] in low:
            return True
    return False


def run_nl_route(orch, user_id: str, message: str, platform: str, role: str) -> tuple[bool, str]:
    """Route natural language to magi-office-ops commands."""
    if not nl_router_enabled():
        return False, ""
    if not should_try_nl_route(orch, message):
        return False, ""

    from api.runtime_paths import get_orch_dir, get_magi_root_dir, get_skill_python

    code_dir = str(get_orch_dir())
    magi_dir = str(get_magi_root_dir())
    py = str(get_skill_python())
    if not py or not os.path.exists(py):
        py = sys.executable or "python3"

    router_script = os.environ.get(
        "MAGI_NL_ROUTER_SCRIPT",
        os.path.join(os.path.expanduser("~"), ".openclaw", "skills", "magi-office-ops", "intent_router.py"),
    ).strip()
    run_script = os.environ.get(
        "MAGI_NL_RUN_SCRIPT",
        os.path.join(os.path.expanduser("~"), ".openclaw", "skills", "magi-office-ops", "run.sh"),
    ).strip()

    if not (router_script and os.path.exists(router_script) and run_script and os.path.exists(run_script)):
        return False, ""

    try:
        r = subprocess.run(
            [py, router_script],
            input=message,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("MAGI_NL_ROUTE_PARSE_TIMEOUT_SEC", "8") or "8"),
            cwd=code_dir,
        )
    except Exception as e:
        logger.warning(f"NL route parse skipped: {e}")
        return False, ""

    raw = (r.stdout or "").strip()
    if not raw:
        return False, ""
    try:
        route = json.loads(raw)
    except Exception:
        return False, ""

    if not isinstance(route, dict) or not route.get("ok"):
        return False, ""

    intent = str(route.get("intent") or "").strip()
    argv = route.get("argv") if isinstance(route.get("argv"), list) else []
    argv = [str(x) for x in argv if str(x).strip()]
    if not argv:
        return False, ""

    user_safe_intents = {
        "system_status", "skills_check", "brain_status_model",
        "translate_full", "translate_summary", "translate_file",
        "quick_model_info", "quick_language", "quick_next_step",
        "humanizer_apply", "whisper_transcribe",
        "automation_workflow_plan", "proactive_agent_guide", "self_improving_guide",
        "laf_monitor", "laf_closing", "laf_condition_draft", "laf_backfill",
        "file_review_check", "file_review_preview",
        "file_review_check_downloadable", "file_review_downloadable",
        "file_review_download", "file_review_download_case",
        "transcript_sync", "transcript_rename",
        "transcript_download_all", "transcript_download_case",
        "transcript_download_all_fallback",
        "osc_scan_cases", "osc_queue_flush", "pdf_scan",
        "market_prompt", "market_set", "market_add",
        "market_remove", "market_list", "market_briefing",
        "labor_law_calc", "labor_law_overtime",
        "labor_law_annual_leave", "labor_law_severance",
        "laf_go_live", "laf_fee", "laf_inquiry", "laf_withdrawal",
        "judgment_search", "judgment_collect", "judgment_daily_crawl",
        "db_backup", "calendar_sync",
        "autopilot_tick", "autopilot_nightly", "autopilot_self_test",
    }
    brain_notify_intents = {"brain_repair", "brain_calibrate_ngl"}
    if role != "admin" and intent in brain_notify_intents:
        try:
            from skills.ops.red_phone import alert_admin
            alert_admin(
                f"⚠️ 使用者 {user_id} ({platform}) 正在要求執行大腦操作：{intent}\n"
                f"原始訊息：{message[:200]}",
                severity="warning",
            )
        except Exception:
            pass
    elif role != "admin" and intent not in user_safe_intents:
        return True, orch._postprocess_router_reply("⛔ 這個自然語句命令涉及系統流程，僅管理員可執行。", platform)

    env = os.environ.copy()
    env["MAGI_CODE_DIR"] = code_dir
    env["MAGI_ROOT_DIR"] = magi_dir
    env["MAGI_NO_DELETE"] = env.get("MAGI_NO_DELETE", "1") or "1"
    env["MAGI_PREFER_LOCAL_DB"] = env.get("MAGI_PREFER_LOCAL_DB", "0") or "0"

    timeout_sec = int(os.environ.get("MAGI_NL_ROUTE_EXEC_TIMEOUT_SEC", "300") or "300")
    async_timeout_sec = int(os.environ.get("MAGI_NL_ROUTE_ASYNC_TIMEOUT_SEC", "2400") or "2400")
    async_enabled = str(os.environ.get("MAGI_NL_ROUTE_ASYNC", "1")).strip().lower() in {"1", "true", "yes", "on"}
    async_intents = {
        "autopilot_tick", "autopilot_nightly", "autopilot_self_test",
        "laf_monitor", "laf_closing", "laf_condition_draft", "laf_backfill",
        "file_review_check", "file_review_preview",
        "file_review_check_downloadable", "file_review_downloadable",
        "file_review_download", "file_review_download_case",
        "transcript_sync", "transcript_rename",
        "transcript_download_all", "transcript_download_case",
        "transcript_download_all_fallback",
        "osc_scan_cases", "osc_queue_flush", "pdf_scan",
        "brain_repair", "brain_calibrate_ngl",
        "translate_file", "db_backup", "db_backup_restore",
    }

    if async_enabled and intent in async_intents:
        import threading

        def _run_background():
            try:
                proc = subprocess.run(
                    [run_script] + argv,
                    capture_output=True, text=True,
                    timeout=async_timeout_sec, cwd=code_dir, env=env,
                )
                out = (proc.stdout or "").strip()
                err = (proc.stderr or "").strip()
                if proc.returncode != 0:
                    tail = (err or out or "unknown error").strip()[-1200:]
                    msg = f"❌ 命令失敗：`{intent or 'unknown'}`\n{tail}"
                elif out:
                    if len(out) > 1800:
                        msg = f"✅ 已完成：`{intent or 'command'}`\n（輸出較長，以下為尾段）\n{out[-1600:]}"
                    else:
                        msg = out
                else:
                    msg = f"✅ 已完成：`{intent or 'command'}`"
            except subprocess.TimeoutExpired:
                msg = f"⚠️ 自然語句命令逾時（>{async_timeout_sec}s）：`{intent or 'unknown'}`"
            except Exception as e:
                msg = f"❌ 自然語句命令執行失敗：{e}"

            msg = orch._postprocess_router_reply(msg, platform)
            try:
                cb = getattr(orch, "notification_callback", None)
                if cb:
                    cb(str(user_id or ""), msg, str(platform or ""))
            except Exception as notify_err:
                logger.warning(f"NL route async callback failed: {notify_err}")

        threading.Thread(target=_run_background, daemon=True).start()
        return True, orch._postprocess_router_reply(
            f"⏳ 已開始執行：`{intent or 'command'}`。完成後我會主動回報結果。",
            platform,
        )

    try:
        proc = subprocess.run(
            [run_script] + argv,
            capture_output=True, text=True,
            timeout=timeout_sec, cwd=code_dir, env=env,
        )
    except subprocess.TimeoutExpired:
        return True, orch._postprocess_router_reply(f"⚠️ 自然語句命令逾時（>{timeout_sec}s）：`{intent or 'unknown'}`", platform)
    except Exception as e:
        return True, orch._postprocess_router_reply(f"❌ 自然語句命令執行失敗：{e}", platform)

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()

    if proc.returncode != 0:
        tail = (err or out or "unknown error").strip()[-1200:]
        return True, orch._postprocess_router_reply(f"❌ 命令失敗：`{intent or 'unknown'}`\n{tail}", platform)

    if out:
        if len(out) > 1800:
            return True, orch._postprocess_router_reply(
                f"✅ 已執行：`{intent or 'command'}`\n（輸出較長，以下為尾段）\n{out[-1600:]}",
                platform,
            )
        return True, orch._postprocess_router_reply(out, platform)

    return True, orch._postprocess_router_reply(f"✅ 已執行：`{intent or 'command'}`", platform)


# ── Explain Routing ────────────────────────────────────────────────

def explain_routing(orch, message: str, role: str = "user") -> dict:
    """Explain which internal handler would be invoked for a given text message."""
    from api.routing import build_route_decision

    msg = (message or "").strip()
    msg_lower = msg.lower()

    def _res(
        action: str, matched: str, requires_admin: bool = False,
        handler: str = "", *, confidence: float = 1.0,
        reason: str = "", candidates: list[dict] | None = None, intent: str = "",
    ) -> dict:
        return build_route_decision(
            action=action, matched=matched, requires_admin=requires_admin,
            handler=handler, confidence=confidence,
            reason=reason or matched, candidates=candidates, intent=intent,
        )

    if ("codex" in msg_lower or "sidecar" in msg_lower or "分散式" in msg) and any(
        kw in msg_lower for kw in ["開啟", "啟用", "打開", "全開", "on", "enable", "關閉", "停用", "關掉", "off", "disable", "狀態", "status", "模式", "help", "幫助"]
    ):
        return _res(action="codex_distributed_control", matched="codex_sidecar_keywords",
                     requires_admin=True, handler="api/orchestrator.py:_handle_codex_distributed_command")

    if msg_lower in HELP_ALIASES:
        return _res(action="help_menu", matched="universal_help",
                     requires_admin=False, handler="api/orchestrator.py:_handle_command('/help')")

    # Status: require MAGI/system context, not just bare "狀態" which hits case-status questions
    _STATUS_EXACT = {"狀態", "系統狀態", "運作狀態", "節點狀態", "機器狀態", "magi狀態", "magi status",
                     "大腦", "大腦狀態", "brain", "brain status", "status", "目前模型", "現在模型", "使用什麼模型"}
    if msg_lower in _STATUS_EXACT or (
        ("模型" in msg) and len(msg) <= 12 and any(kw in msg_lower for kw in ["目前", "現在", "使用", "模式"])
    ):
        return _res(action="status_report", matched="status_keywords",
                     requires_admin=False, handler="api/orchestrator.py:process_message(status fast-path)")

    _sched_re = re.compile(r'\b(?:schedule|meeting)\b')
    if (msg_lower.strip() in {"今天", "明天"}
        or (len(msg_lower) <= 20 and any(kw in msg_lower for kw in ["行程", "日曆", "會議", "本週", "這週"]))
        or _sched_re.search(msg_lower)):
        return _res(action="schedule_query", matched="schedule_keywords",
                     requires_admin=False, handler="api/orchestrator.py:_get_schedule")

    if any(msg_lower.startswith(k) for k in ["記住", "remember", "save memory", "memorize", "@magi 記住", "@magi learn"]):
        return _res(action="memory_write", matched="memory_write_keywords",
                     requires_admin=True, handler="skills/memory/mem_bridge.py:remember")

    if msg.startswith("翻譯 ") or msg_lower.startswith("translate "):
        return _res(action="translate", matched="translate_prefix",
                     requires_admin=False, handler="skills/bridge/tri_sage_collab.py:translate_text")

    if msg.startswith("製作音樂 ") or msg.startswith("生成音樂 ") or msg_lower.startswith("make music "):
        return _res(action="music_generate", matched="music_prefix",
                     requires_admin=False, handler="skills/bridge/tri_sage_collab.py:generate_music")

    if any(kw in msg_lower for kw in ["analyze code", "讀取程式碼", "code folder", "code資料夾", "連動模式", "改善建議", "read code"]):
        return _res(action="code_analysis_async", matched="code_analysis_keywords",
                     requires_admin=True, handler="skills/bridge/code_analysis.py:analyze_code (background thread)")

    if any(kw in msg_lower for kw in ["系統狀態", "system status", "cpu", "ram", "記憶體", "磁碟", "系統監控", "健康檢查", "service health"]):
        return _res(action="system_monitor", matched="system_monitor_keywords",
                     requires_admin=True, handler="skills/ops/system_monitor.py")

    # Fall back to classifier-based routing.
    try:
        detail: dict | Optional[str]
        classify_detailed = getattr(orch.classifier, "classify_detailed", None)
        if callable(classify_detailed):
            detail = classify_detailed(msg)
        else:
            detail = None

        if isinstance(detail, dict):
            intent = str(detail.get("intent") or "UNKNOWN")
        else:
            legacy_intent = getattr(orch.classifier, "classify", lambda _msg: "UNKNOWN")(msg)
            intent = str(legacy_intent or "UNKNOWN")
            detail = {"intent": intent, "confidence": 0.0, "reason": "legacy_classifier_fallback", "candidates": []}
    except Exception:
        detail = {"intent": "UNKNOWN", "confidence": 0.0, "reason": "classifier_exception", "candidates": []}
        intent = "UNKNOWN"
    return _res(
        action=(
            "command_handler" if intent == "CMD" else
            "query_handler" if intent == "QUERY" else
            "chat_handler" if intent == "CHAT" else
            "danger_handler" if intent == "DANGER" else "unknown"
        ),
        matched="intent_classifier", requires_admin=False,
        handler=(
            "api/orchestrator.py:_handle_command" if intent == "CMD" else
            "api/orchestrator.py:_handle_query" if intent == "QUERY" else
            "api/orchestrator.py:_handle_chat_async" if intent == "CHAT" else
            "api/orchestrator.py:(danger path)" if intent == "DANGER" else ""
        ),
        confidence=float(detail.get("confidence") or 0.0),
        reason=str(detail.get("reason") or "intent_classifier"),
        candidates=list(detail.get("candidates") or []),
        intent=intent,
    )


# ── Topic Fast Path ────────────────────────────────────────────────

def topic_fast_path(orch, topic_key: str, user_id, message: str, role: str, platform: str, attachment=None):
    """
    頻道命令綁定：特定頻道引導使用者執行對應命令。

    若使用者在法扶-開辦頻道發了結案指令，引導到結案頻道。
    若使用者在法扶-開辦頻道發了開辦指令，直接執行（不阻擋）。
    """
    # ── 筆錄-通知頻道 (transcript) 自動補全 ──
    if topic_key == "transcript":
        _tr_aliases = ["同步筆錄", "筆錄同步", "下載筆錄", "筆錄下載"]
        msg_stripped = (message or "").strip()
        # 訊息已帶指令關鍵字 → 直接執行
        if any(msg_stripped.startswith(a) for a in _tr_aliases):
            logger.info("[TopicFastPath] transcript: executing existing command")
            return orch._handle_command(user_id, message, role=role, platform=platform)
        # 看起來像 <法院> <案號> 或年度案號格式 → 自動補同步筆錄前綴
        # 例：HLD 114原訴24 / 花蓮 114原訴24 / 114年度原訴字第000024號
        _CASE_PAT_TR = re.compile(
            r'^[\u4e00-\u9fffA-Za-z]{2,4}\s+\d{2,4}[\u4e00-\u9fff]{1,6}\d+'  # <法院> <案號>
            r'|^\d{2,3}年度'  # 114年度...
        )
        if _CASE_PAT_TR.match(msg_stripped) and len(msg_stripped) <= 60:
            autocompleted = "同步筆錄 " + msg_stripped
            logger.info("[TopicFastPath] transcript autocomplete: '%s' -> '%s'", msg_stripped, autocompleted)
            return orch._handle_command(user_id, autocompleted, role=role, platform=platform)
        # 其他訊息（如一般問題）→ 不攔截
        return None

    # ── 閱卷-繳費頻道 (filereview_payment) 守門 ──
    if topic_key == "filereview_payment":
        msg_stripped = (message or "").strip()
        # 帶明確指令關鍵字或有附件（繳費憑證截圖）→ 放行讓既有管線處理
        _pay_kws = ["已繳費", "繳費完成", "繳費通知", "付款完成", "上傳憑證", "繳費憑證", "繳費截圖"]
        if any(kw in msg_stripped for kw in _pay_kws) or attachment:
            return None
        # 純文字且沒有繳費關鍵字 → 提示頻道用途，不走 chat engine
        if len(msg_stripped) > 1:
            logger.info("[TopicFastPath] filereview_payment: non-payment text, returning hint")
            return "💡 這個頻道顯示**閱卷繳費通知**。如需回報繳費，請上傳繳費憑證截圖，或至閱卷相關頻道執行指令。"
        return None

    # ── 閱卷-下載頻道 (filereview_download) 守門 ──
    if topic_key == "filereview_download":
        msg_stripped = (message or "").strip()
        _dl_kws = ["閱卷查核", "可下載", "下載清單"]
        if any(msg_stripped.startswith(kw) for kw in _dl_kws):
            return orch._handle_command(user_id, message, role=role, platform=platform)
        # 通知頻道，非指令訊息不走 chat engine
        return None

    # ── 閱卷-聲請頻道 (filereview_apply) 自動補全 ──
    if topic_key == "filereview_apply":
        _apply_aliases = ["閱卷聲請", "聲請閱卷", "申請閱卷", "聲請閱覽"]
        msg_stripped = (message or "").strip()
        # 訊息已帶指令關鍵字 → 直接執行
        if any(msg_stripped.startswith(a) for a in _apply_aliases):
            logger.info("[TopicFastPath] filereview_apply: executing existing command")
            return orch._handle_command(user_id, message, role=role, platform=platform)
        # 看起來像 <法院> <案號> [姓名] 格式 → 自動補前綴
        # 例：HLD 115原訴36 [當事人J] / 花蓮 115家救3 [當事人J]
        _CASE_PAT = re.compile(r'^[\u4e00-\u9fffA-Za-z]{2,4}\s+\d{2,4}[\u4e00-\u9fff]{1,6}\d+')
        if _CASE_PAT.match(msg_stripped):
            autocompleted = "閱卷聲請 " + msg_stripped
            logger.info("[TopicFastPath] filereview_apply autocomplete: '%s' -> '%s'", msg_stripped, autocompleted)
            return orch._handle_command(user_id, autocompleted, role=role, platform=platform)
        # 其他訊息（如一般問題、確認碼）→ 不攔截，走一般流程
        return None

    # 頻道→允許的動作映射
    _CHANNEL_ACTION_MAP = {
        "laf_go_live": {"allowed": ("go_live",), "label": "法扶-開辦", "hint": "這個頻道用來執行**開辦回報**", "default_action": "go_live"},
        "laf_closing": {"allowed": ("closing",), "label": "法扶-結案", "hint": "這個頻道用來執行**結案回報**", "default_action": "closing"},
        "laf_fee": {"allowed": ("fee",), "label": "法扶-費用", "hint": "這個頻道用來執行**費用支付回報**", "default_action": "fee"},
        "laf_inquiry": {"allowed": ("inquiry",), "label": "法扶-疑義", "hint": "這個頻道用來執行**疑義回報**", "default_action": "inquiry"},
        "laf_condition": {"allowed": ("condition",), "label": "法扶-二階段", "hint": "這個頻道用來執行**二階段回報**", "default_action": "condition"},
        "laf_progress": {"allowed": ("__progress__",), "label": "法扶-進度回報", "hint": "這個頻道用來查看**未結案件進度回報**通知與確認碼"},
        "laf_dispatch": {"allowed": (), "label": "法扶-派案", "hint": "這個頻道顯示**派案通知**，有新信件時 MAGI 會自動通知"},
        "laf": {"allowed": ("inquiry", "fee", "condition", "withdrawal", "closing", "go_live"), "label": "法扶-一般", "hint": "這個頻道用來執行各項法扶作業"},
        # laf_general 是 discord_channel_router.py 實際使用的 key（原 MAP 只有 "laf" 造成 key 不符）
        "laf_general": {"allowed": ("inquiry", "fee", "condition", "withdrawal", "closing", "go_live"), "label": "法扶-一般", "hint": "這個頻道用來執行各項法扶作業"},
    }

    conf = _CHANNEL_ACTION_MAP.get(topic_key)
    if not conf:
        # 非法扶頻道：走原有邏輯
        handler = getattr(orch, "_TOPIC_HANDLERS", {}).get(topic_key)
        if handler is None:
            return None
        try:
            return handler(orch, user_id, message, role, platform, attachment)
        except Exception as e:
            logger.error(f"❌ Topic fast path '{topic_key}' error: {e}", exc_info=True)
            return None

    # 檢查是否為法扶指令
    try:
        from api.handlers.laf_handler import parse_laf_report_payload
        payload = parse_laf_report_payload(message)
        
        # 情境：使用者在這個頻道發了一則「看起來像人名或案號」但「沒有關鍵字」的訊息
        # 我們自動幫他帶上該頻道的 default_action
        if not payload and conf.get("default_action"):
            # 檢查是否像案號或人名
            cleaned = message.strip()
            is_case_no = bool(re.search(r"\d{6,8}-[A-Za-z]-\d{3}", cleaned)) or bool(re.search(r"\b\d{4}-\d{4}\b", cleaned))
            # 簡易判斷：2-5 個中文字且不含指令關鍵字
            is_potential_name = len(cleaned) >= 2 and len(cleaned) <= 6 and all('\u4e00' <= c <= '\u9fff' for c in cleaned)
            
            if is_case_no or is_potential_name:
                default_act = conf["default_action"]
                # 重新構建一個帶有關鍵字的訊息來觸發
                action_kws = {"go_live": "開辦", "closing": "結案", "fee": "費用支付", "inquiry": "疑義", "condition": "二階段"}
                kw = action_kws.get(default_act, "")
                if kw:
                    logger.info(f"[TopicFastPath] Auto-completing command for {topic_key}: '{message}' -> '{message} {kw}'")
                    # 遞迴呼叫 Orchestrator 的處理流程，但改用合成訊息
                    # 注意：這裡直接返回 None 讓後續流程跑「合成後」的邏輯可能較複雜
                    # 簡單做法：直接修改 message 再繼續
                    message = f"{message} {kw}"
                    payload = parse_laf_report_payload(message)

        if payload:
            action = payload.get("action", "")
            allowed = conf.get("allowed", ())
            if allowed and action not in allowed:
                # 指令不屬於這個頻道 → 引導
                _action_channel = {
                    "go_live": "法扶-開辦", "closing": "法扶-結案",
                    "inquiry": "法扶-疑義", "fee": "法扶-費用",
                    "condition": "法扶-二階段", "withdrawal": "法扶-一般",
                    "__progress__": "法扶-進度回報",
                }
                target = _action_channel.get(action, "法扶-一般")
                return f"📍 這個指令請到 **#{target}** 頻道執行。\n（此頻道是 **{conf['label']}**：{conf['hint']}）"

            # IMPORTANT: Return handle_command result to execute the autocompleted command
            logger.info(f"[TopicFastPath] executing: '{message}'")
            return orch._handle_command(user_id, message, role=role, platform=platform)

        return None
    except Exception:
        pass

    # 非法扶指令（一般對話）→ return None 讓正常流程處理
    return None


# ── Conversational Intent ──────────────────────────────────────────

def try_conversational_intent(orch, message: str, msg_lower: str, user_id, role: str, platform: str):
    """Comprehensive natural-language intent dispatcher."""
    compact = message.replace(" ", "")
    low_compact = compact.lower()

    is_question = bool(re.search(r"[嗎嘛呢阿啊？\?]$", compact))

    has_conv_signal = bool(re.search(
        r"(?:"
        r"可以|能不能|會不會|能否|可否|你會|你能|能幫|幫我|幫忙|"
        r"可不可以|有沒有辦法|是否能|是否可以|有辦法|"
        r"怎麼|如何|要怎麼|該怎麼|怎樣|要怎樣|"
        r"有沒有|是不是|會不會|能不能|"
        r"想要|想用|想看|想問|想知道|想了解|"
        r"我想|我要|我需要|我希望|"
        r"請|麻煩|拜託|勞駕|"
        r"教我|告訴我|跟我說|讓我|給我|"
        r"有什麼|什麼是|這是什麼|那是什麼|"
        r"哪裡|在哪|去哪|"
        r"為什麼|為何|幹嘛|"
        r"看不懂|聽不懂|不會用|不知道怎|找不到|"
        r"不懂|搞不懂|弄不懂|沒辦法|做不到|"
        r"太長|太多|太難|看不完|"
        r"can you|could you|do you|are you able|how to|how do i|"
        r"please|i want|i need|i'd like|show me|tell me|help me"
        r")",
        low_compact, re.IGNORECASE
    ))

    is_soft = bool(re.search(
        r"^(?:我想|我要|幫我|請幫我|麻煩|請你|我需要|我希望|"
        r"可以|能不能|你可以|你能|你會|能幫|"
        r"怎麼|如何|要怎麼|該怎麼|"
        r"教我|告訴我|幫忙|拜託|"
        r"幫|給我|讓我|替我|"
        r"這篇|這段|這個|那個|那篇|那段|"
        r"i want|i need|can you|how|please|help|show)",
        low_compact, re.IGNORECASE
    ))

    if not (is_question or has_conv_signal or is_soft):
        return None

    patterns = [
        # Translation: require clear translation intent, not just bare "翻" which matches "翻開/翻頁"
        (r"(?:翻譯|翻成|翻一下|幫翻|翻成中文|翻成英文|翻成日文|"
         r"translate|translation|翻一篇|"
         r"中翻英|英翻中|日翻中|中翻日|韓翻中|"
         r"看不懂.{0,6}(?:英文|日文|韓文|外文)|"
         r"這篇.{0,4}(?:外文|英文|日文)|"
         r"pdf.{0,6}翻譯|翻譯.{0,6}pdf)", "translate",
         "✅ **我可以幫您翻譯！**\n\n"
         "• 翻譯文字：直接輸入 `翻譯 [文字/網址]`\n"
         "• 翻譯檔案：上傳 PDF/TXT/DOCX 後在留言打 `翻譯`\n"
         "• 支援中英日韓等多語系，透過本地 LLM 引擎處理！", True),

        (r"(?:畫圖|畫畫|畫一|畫個|畫張|畫幅|生成圖|產生圖|做圖|出圖|弄圖|"
         r"generate\s*image|draw|make.*(?:image|picture|art)|create.*(?:image|art)|"
         r"作圖|圖片|插圖|海報|頭像|桌布|logo|illustration|"
         r"設計圖|設計一個|設計一張|做設計|弄設計|"
         r"幫我畫|幫畫|給我一張|弄一張|產一張|"
         r"ai.{0,4}(?:畫|圖|art)|人工智慧.{0,4}畫)", "image",
         "✅ **我可以幫您畫圖！**\n\n"
         "直接輸入描述就好，例如：\n"
         "• `畫一隻可愛的貓咪`\n"
         "• `draw a sunset over mountains`\n"
         "• `幫我畫一張海報`"),

        (r"(?:做音樂|作曲|製作音樂|生成音樂|配樂|編曲|bgm|"
         r"make\s*music|compose|produce.*music|create.*(?:song|music|melody)|"
         r"幫我作曲|弄音樂|弄一首|寫歌|寫一首|"
         r"背景音樂|片頭曲|ringtone|音效)", "music",
         "✅ **我可以幫您製作音樂！**\n\n"
         "請輸入：`製作音樂 [風格描述]`\n"
         "例如：\n"
         "• `製作音樂 溫暖鋼琴、30秒`\n"
         "• `生成音樂 cyberpunk EDM 60s`"),

        # Status: require system/MAGI context prefix to avoid matching "案件狀態" etc.
        (r"(?:系統狀態|系統健康|伺服器狀態|server\s*status|system\s*status|"
         r"cpu使用|ram使用|記憶體使用|磁碟空間|硬碟空間|disk\s*usage|"
         r"機器怎樣|電腦怎樣|機器還好嗎|系統還好嗎|"
         r"系統.*有沒有問題|系統.*有沒有異常|"
         r"系統正常嗎|系統是否正常|系統負載|system\s*load|"
         r"node\s*status|magi.*狀態|casper.*狀態|melchior.*狀態|"
         r"各節點|節點狀態|"
         r"大腦狀態|brain\s*status|運作狀態|看一下系統狀態)", "status", None, True),

        (r"(?:行程|日曆|會議|開會|schedule|calendar|meeting|"
         r"今天有什麼|明天有什麼|這週|本週|下週|"
         r"待辦|to.?do|agenda|接下來|有什麼事|"
         r"今天的安排|明天的安排|今天要幹嘛|"
         r"有沒有會|幾點開會|什麼時候開會|"
         r"my\s*schedule|upcoming|what.*today|what.*tomorrow)", "schedule", None, True),

        (r"(?:記住|記東西|記事|記一下|筆記|memorize|remember|"
         r"幫我記|幫記|存記憶|寫筆記|做筆記|"
         r"把這個記|把這段記|以後記得|記錄一下|"
         r"save.*(?:note|memory)|take.*note|jot.*down|"
         r"我怕忘|怕我忘|不要忘記|別忘了)", "memory",
         "✅ **我可以幫您記住事情！**\n\n"
         "請輸入：`記住 [要記的內容]`\n"
         "例如：\n"
         "• `記住 我的車牌是 ABC-1234`\n"
         "• `記住 下次開會要帶合約`"),

        (r"(?:obsidian|筆記本|vault|知識庫|知識筆記|"
         r"obsidian\s*(?:search|read|ingest|ask|status|設定|搜尋|讀取|問)|"
         r"用.*(?:筆記|notes?).*(?:回答|查|找|搜)|"
         r"查.*(?:筆記|notes?)|搜.*(?:筆記|notes?)|"
         r"(?:筆記|notes?).*(?:搜尋|查詢|查找|search)|"
         r"notebook\s*(?:qa|q&a|query)|"
         r"用obsidian|開obsidian|連obsidian)", "obsidian",
         "✅ **我可以幫您管理 Obsidian 筆記！**\n\n"
         "• 查看狀態：`obsidian status`\n"
         "• 搜尋筆記：`obsidian search <關鍵字>`\n"
         "• 讀取筆記：`obsidian read <筆記路徑>`\n"
         "• 匯入記憶：`obsidian ingest [資料夾]`\n"
         "• 來源匯入：`obsidian ingest_source --source 案件 [--subpath X] [--limit N]`\n"
         "• 筆記問答：`obsidian ask <問題> [--scope source:案件|case:2025-0014]`\n"
         "• 設定 Vault：`obsidian set_vault <路徑>`"),

        (r"(?:分析程式|檢查程式|程式碼|code|analyze\s*code|"
         r"code\s*review|讀code|看code|改code|修code|"
         r"review\s*code|debug|除錯|檢查bug|找bug|"
         r"幫我看程式|看一下程式|程式有問題|code有問題|"
         r"lint|syntax\s*check|程式檢查|原始碼|source\s*code|"
         r"改善.{0,4}程式|優化.{0,4}程式|重構|refactor)", "code_analysis",
         "✅ **我可以幫您分析程式碼！**\n\n"
         "• 全面掃描：`讀取程式碼` 或 `analyze code`\n"
         "• 自動修復：`自動修復code`\n"
         "• 我會深度掃描並產生改善建議報告。"),

        (r"(?:開網頁|開網站|開啟網頁|瀏覽器|browse|open\s*url|"
         r"截圖|screenshot|幫我開|打開.{0,6}網|"
         r"上網|查網頁|查網站|看網站|看網頁|"
         r"訪問.{0,4}網|連到.{0,4}網|去.{0,4}網站|"
         r"navigate|visit\s*(?:url|site|page)|"
         r"幫我查.{0,6}(?:網|site)|capture|screen\s*cap)", "browser",
         "✅ **我可以幫您開啟網頁或截圖！**\n\n"
         "• 開網頁：`打開 https://google.com`\n"
         "• 截圖：`截圖 https://example.com`\n"
         "• 也可以直接貼上網址請我開"),

        (r"(?:找檔案|搜尋檔|列出檔|檔案管理|search\s*file|list\s*file|find\s*file|"
         r"看檔案|查檔案|有沒有這個檔|檔案在哪|"
         r"ls|dir|folder|資料夾|目錄|"
         r"幫我找.{0,6}檔|某個檔案)", "file_manager",
         "✅ **我可以幫您搜尋或列出檔案！**\n\n"
         "• 搜尋：`搜尋檔案 [關鍵字]`\n"
         "• 列出目錄：`列出檔案`"),

        (r"(?:看新聞|訂閱新聞|rss|news|讀新聞|最新消息|"
         r"今日新聞|有什麼新聞|國際新聞|科技新聞|"
         r"幫我看新聞|有沒有新消息|最新資訊|最新動態|"
         r"feed|headline|今天.{0,4}新聞|現在.{0,4}新聞)", "rss",
         "✅ **我可以幫您讀取新聞！**\n\n"
         "• 閱讀最新：`讀新聞` 或 `read news`\n"
         "• 新增訂閱：`訂閱 [RSS 網址]`"),

        (r"(?:github|git\s*hub|搜尋\s*repo|找\s*repo|"
         r"github\s*趨勢|trending|open\s*source|開源|"
         r"找.{0,4}(?:套件|package|library|框架|framework)|"
         r"有沒有.{0,6}(?:repo|專案|project)|"
         r"star|fork|github上)", "github",
         "✅ **我可以幫您搜尋 GitHub！**\n\n"
         "• 趨勢：`github 趨勢`\n"
         "• 搜尋：`github 搜尋 [關鍵字]`"),

        (r"(?:短摘要?|詳細摘要?|簡短摘要?|完整摘要?|長摘要?|精簡摘要?|"
         r"摘要|summarize|summary|整理重點|幫我整理|"
         r"懶人包|太長.{0,4}(?:了|不想看|看不完)|tl;?dr|"
         r"簡單說|簡單講|長話短說|精簡|濃縮|"
         r"幫我看.{0,6}(?:重點|大意)|這篇.{0,4}(?:重點|大意|在講|在說)|"
         r"抓重點|歸納|總結|統整|overview|brief|detailed\s*summary|"
         r"key\s*point|main\s*point|abstract)", "summary",
         "✅ **我可以幫您做摘要！**\n\n"
         "• 網頁摘要：`摘要 [網址]`\n"
         "• 文字摘要：`摘要 [一段文字]`\n"
         "• 也可以上傳檔案請我整理重點\n\n"
         "💡 可指定摘要等級：\n"
         "• `精簡摘要` / `短摘要` → 3-5 點，每點一句話\n"
         "• `摘要` → 5-8 點，每點 1-2 句（預設）\n"
         "• `詳細摘要` / `長摘要` → 12-15 點，每點 2-3 句（含背景與數據）", True),

        (r"(?:存證信函|寫存證|草擬存證|法律信函|legal\s*attest|"
         r"律師函|警告函|催告|催告書|催告函|"
         r"正式信函|法律文件|法律文書|存證|"
         r"寄存證|發存證|怎麼寫.{0,4}存證|"
         r"怎麼.{0,4}(?:寄|發).{0,4}(?:存證|信函))", "legal_attest",
         "✅ **我可以幫您寫存證信函！**\n\n"
         "請直接說：`幫我寫存證信函`\n"
         "我就會一步步引導您填寫寄件人、收件人及內文，最後產生標準 PDF。"),

        (r"(?:委任狀|委託書|委任状|委托书|power\s*of\s*attorney|poa|"
         r"做委任|寫委任|開委任|製作委任|產生委任|草擬委任|"
         r"做委託|寫委託|開委託|製作委託|產生委託|草擬委託|"
         r"怎麼.{0,4}(?:做|寫|開).{0,4}(?:委任|委託))", "poa",
         "✅ **我可以幫您製作委任狀/委託書！**\n\n"
         "請直接說：`幫我做委任狀`\n"
         "我會一步步引導您填寫案件類型、當事人、案號等欄位，最後產生 DOCX 檔案。\n\n"
         "💡 也可以一次提供資訊：\n"
         "• `幫張三做民事委任狀`\n"
         "• `製作刑事辯護人委任狀 114年度訴字第123號`"),

        (r"(?:委任契約|委任合約|engagement\s*agreement|"
         r"做契約|寫契約|製作契約|產生契約|草擬契約|開契約|"
         r"怎麼.{0,4}(?:做|寫|開).{0,4}契約)", "contract",
         "✅ **我可以幫您製作委任契約書！**\n\n"
         "請直接說：`幫我做委任契約書`\n"
         "我會一步步引導您填寫當事人、案由、費用等欄位，最後產生 DOCX 檔案。"),

        (r"(?:收據|收执|收執|receipt|"
         r"做收據|寫收據|開收據|製作收據|產生收據|"
         r"怎麼.{0,4}(?:做|寫|開).{0,4}收據)", "receipt",
         "✅ **我可以幫您開收據！**\n\n"
         "請直接說：`幫我開收據`\n"
         "我會一步步引導您填寫委任人、案由、金額等欄位，最後產生 DOCX 檔案。"),

        (r"(?:有什麼功能|你會什麼|你能做什麼|你有什麼能力|功能列表|"
         r"skill\s*list|what\s*can\s*you\s*do|"
         r"你是誰|你是什麼|自我介紹|介紹.*自己|"
         r"all\s*skills|所有功能|全部功能|功能清單|"
         r"能力清單|能力表|技能表|技能列表|你做得到什麼|"
         r"capabilities|features|what\s*(?:are|is)\s*(?:you|your)|"
         r"你做了什麼|你可以做什麼|有哪些功能|有哪些技能|"
         r"命令列表|指令列表|指令清單)", "skill_list", None, True),

        (r"(?:深度思考|deep\s*think|仔細想|認真想|好好想|"
         r"深度分析|深入分析|詳細分析|深入思考|"
         r"用大腦|用melchior|"
         r"think.*(?:hard|deep|careful)|analyze.*(?:deep|thorough)|"
         r"仔細.{0,4}(?:分析|想|看)|認真.{0,4}(?:分析|想|看)|"
         r"幫我.{0,4}深度|用比較強的)", "deep_think",
         "✅ **我可以用深度思考模式！**\n\n"
         "請輸入：`@MAGI 深度思考 [您的問題]`\n"
         "我會使用深度思考模式為您深度分析。"),

        (r"(?:爬蟲|crawler|爬網|爬取|scrape|抓資料|"
         r"幫我爬|爬一下|爬個|spider|"
         r"抓.{0,4}(?:網頁|網站|資料|data|頁面)|"
         r"定時抓|自動抓|自動爬|排程爬|"
         r"每日.{0,4}(?:爬|抓)|daily\s*crawl)", "crawler",
         "✅ **我可以幫您管理爬蟲！**\n\n"
         "• 新增目標：`新增爬蟲目標 [網址]`\n"
         "• 列出目標：`列出爬蟲目標`\n"
         "• 執行爬取：`爬蟲目標 立即執行`"),

        (r"(?:健康檢查|檢查系統|check\s*health|health\s*check|服務狀態|"
         r"service.*(?:check|health|alive|ok)|"
         r"服務.{0,4}(?:正常|活|掛|死|down)|"
         r"有沒有.{0,4}(?:掛|當|crash)|"
         r"系統.{0,4}(?:掛|當|crash|down)|"
         r"ping|heartbeat|uptime|是否.{0,4}正常)", "sys_monitor", None, True),

        (r"(?:語音|錄音|聽寫|逐字稿|transcript|speech|"
         r"audio|voice|whisper|stt|語音辨識|"
         r"幫我聽|幫我轉文字|轉成文字|"
         r"voice.*text|speech.*text|"
         r"錄音檔|音檔|mp3|wav|m4a)", "audio",
         "✅ **我可以幫您處理語音！**\n\n"
         "直接上傳錄音檔（MP3/WAV/M4A），我就會自動產生逐字稿。\n"
         "• 加上 `翻譯` → 翻譯逐字稿\n"
         "• 加上 `摘要` → 摘要逐字稿"),

        (r"(?:看圖|看照片|分析圖|分析照片|圖片辨識|"
         r"這張圖|這個圖|這張照片|辨識圖|"
         r"image.*(?:analy|recogni)|photo.*(?:analy|recogni)|"
         r"ocr|文字辨識|辨識文字|"
         r"幫我看.{0,4}(?:圖|照片|這張)|"
         r"圖片裡|照片裡|圖上)", "image_analysis",
         "✅ **我可以幫您分析圖片！**\n\n"
         "直接上傳圖片，我就會用 Melchior 視覺模型幫您分析。\n"
         "• 也支援 OCR 文字辨識"),

        (r"(?:加班費|勞基法|勞動基準法|特休假|特別休假|資遣費|"
         r"一例一休|例假日加班|休息日加班|平日延長|延長工時|"
         r"overtime.*pay|severance\s*pay|annual\s*leave.*taiwan|"
         r"算加班|算特休|算資遣|幾天特休|幾個月資遣|"
         r"休息日.{0,4}加班|例假日.{0,4}出勤)", "labor_law",
         "✅ **我可以幫您計算勞基法相關金額！**\n\n"
         "**加班費**：`月薪 50000，休息日加班 3 小時`\n"
         "**特休假**：`到職日 2020-03-01，我有幾天特休`\n"
         "**資遣費**：`月薪 45000，到職 2018-01-01，現在資遣費多少`\n"
         "**試算表代算**：貼上 Google Sheets 公開連結\n\n"
         "假別：平日 / 休息日 / 例假日 / 國定假日", True),

        (r"(?:查判決|找判決|判決搜尋|搜尋判決|收集判決|判決搜集|"
         r"搜尋最高法院判決|最近.{0,4}判決|法院判決|實務見解|法律見解|法院見解|court\s*judgment)", "judgment_search",
         "✅ **我可以幫您查判決！**\n\n"
         "• 直接輸入：`查判決 傷害`\n"
         "• 也可提供案號：`查判決 113年度上訴字第12號`\n"
         "• 實務見解整理：`實務見解 預售屋遲延交屋`", True),

        (r"(?:開庭排程|庭期|最近.{0,4}(?:什麼庭|有庭|開庭)|"
         r"明天.{0,2}開庭|今天.{0,4}庭|下.{0,2}開庭|"
         r"庭前準備|準備.{0,6}開庭資料|準備清單|應備文件|"
         r"案件時程|時程總覽|全部排程|所有案件.{0,3}排程|"
         r"補正期限|繳費期限|補正提醒|繳費提醒|"
         r"什麼時候.{0,3}(?:補正|繳費)|"
         r".{1,8}(?:繳了|交了|繳費了|補正了|已繳|已補正|已交)(?:嗎|呢|沒|了沒|[？?])|"
         r".{1,8}(?:繳了|交了|繳費了|補正了|已繳|已補正|已交|已完成)$|"
         r"關掉.{1,8}(?:提醒|警報|通知)|"
         r"開庭提醒|hearing)", "court_hearing",
         "✅ **我可以幫您查排程！**\n\n"
         "• 查看排程：`最近有什麼庭`（含開庭/補正/繳費）\n"
         "• 庭前準備：`準備 XXX 案的開庭資料`\n"
         "• 標記完成：`張國賢繳了` / `補字第54號交了`", True),

        (r"(?:判決趨勢|趨勢分析|案由分析|判決分析|案由統計|"
         r"判決統計|見解趨勢|裁判趨勢)", "judgment_trend",
         "✅ **我可以分析判決趨勢！**\n\n"
         "• 總覽：`判決趨勢`\n"
         "• 特定案由：`判決趨勢 詐欺`", True),

        (r"(?:追蹤股票|追蹤清單|新增追蹤|增加追蹤|設定追蹤|移除追蹤|"
         r"股市晨報|股市預測|技術分析|macd|rsi|布林通道|watchlist|track\s+stock)", "stock_briefing",
         "✅ **我可以幫您追蹤股票與產生晨報！**\n\n"
         "• 設定：`追蹤股票 台積電 AAPL`\n"
         "• 清單：`追蹤清單`\n"
         "• 晨報：`股市晨報`", True),

        (r"(?:怎麼用|怎麼使用|使用方法|使用教學|新手|"
         r"tutorial|guide|manual|beginner|"
         r"操作說明|操作方式|使用說明|入門)", "help",
         "✅ **歡迎使用 MAGI 系統！**\n\n"
         "輸入 `/help` 或 `指令` 可以看到完整的功能清單。\n"
         "也可以直接用白話問我，例如：\n"
         "• 「幫我翻譯這段英文」\n"
         "• 「我想看今天的行程」\n"
         "• 「幫我畫一張圖」"),
    ]

    for pattern, action, guide, *rest in patterns:
        direct = rest[0] if rest else False
        if not re.search(pattern, low_compact, re.IGNORECASE):
            continue

        if direct and action == "status":
            from api.orchestrator import get_brain_status
            node_status = orch._get_magi_status()
            brain_status = get_brain_status()
            collab_status = orch._get_collaboration_status()
            return f"{node_status}\n\n{brain_status}\n\n{collab_status}"

        if direct and action == "schedule":
            return orch._get_schedule()

        if direct and action == "skill_list":
            return orch._list_skills()

        if direct and action == "sys_monitor":
            try:
                from skills.ops.system_monitor import get_system_status, check_service_health
                if any(kw in msg_lower for kw in ["服務", "service", "健康"]):
                    return check_service_health()
                return get_system_status()
            except Exception as e:
                return f"❌ 系統監控失敗: {e}"

        if direct and action == "translate":
            if orch._looks_like_capability_question(message):
                return guide
            return orch._run_inline_translation_command(user_id, message)

        if direct and action == "summary":
            if orch._looks_like_capability_question(message):
                return guide
            return orch._run_inline_summary_command(message)

        if direct and action == "labor_law":
            if orch._looks_like_capability_question(message):
                return guide
            return orch._run_labor_law_command(message)

        if direct and action == "judgment_search":
            if orch._looks_like_capability_question(message):
                return guide
            return orch._run_judgment_collector_command(message, notify=False)

        if direct and action == "stock_briefing":
            if orch._looks_like_capability_question(message):
                return guide
            return orch._run_stock_briefing_command(message)

        if direct and action == "court_hearing":
            if orch._looks_like_capability_question(message):
                return "✅ **我可以幫您查開庭排程！**\n\n• 查看排程：`最近有什麼庭`\n• 庭前準備：`準備 XXX 案的開庭資料`"
            return orch._run_court_hearing_command(message)

        if direct and action == "judgment_trend":
            if orch._looks_like_capability_question(message):
                return guide
            return orch._run_judgment_trend_command(message)

        if guide:
            return guide

    return None


# ── Route probe / format ───────────────────────────────────────────

def extract_route_probe(message: str) -> tuple[bool, str, str]:
    msg = (message or "").strip()
    if not msg:
        return False, "", ""
    low = msg.lower()

    explicit_prefixes = ["查詢路由", "看路由", "路由判斷", "路由查詢", "路由", "route", "routing"]
    for p in explicit_prefixes:
        if msg.startswith(p) or low.startswith(p + " "):
            rest = msg[len(p):].strip()
            rest = rest.lstrip(" :：\n\t")
            rest = rest.strip("「」\"'")
            if not rest:
                return True, "", "❓ 你想查哪一句會走哪個功能？例如：`查詢路由 翻譯 https://...`"
            return True, rest, ""

    natural_starts = ["幫我看", "麻煩看", "請幫我看", "請你看", "幫我判斷", "麻煩你判斷", "這句話", "這句"]
    natural_markers = ["會怎麼處理", "會走哪個", "會走什麼流程", "會跑哪個流程", "會觸發什麼", "會觸發哪個"]
    if any(msg.startswith(s) for s in natural_starts) and any(m in msg for m in natural_markers):
        marker_hit = next((m for m in natural_markers if m in msg), "")
        marker_end = (msg.find(marker_hit) + len(marker_hit)) if marker_hit else 0
        tail = msg[marker_end:] if marker_end > 0 else msg
        tail = tail.lstrip()

        window = int(os.environ.get("MAGI_ROUTE_EXPLAIN_SEP_WINDOW", "12"))
        sep_full = tail.find("：")
        sep_ascii = tail.find(":")
        if 0 <= sep_ascii <= window:
            left = tail[:sep_ascii].strip().lower()
            if left.endswith("http") or left.endswith("https"):
                sep_ascii = -1
        if 0 <= sep_full <= window:
            rest = tail[sep_full + 1:].strip()
        elif 0 <= sep_ascii <= window:
            rest = tail[sep_ascii + 1:].strip()
        else:
            return True, "", "❓ 麻煩用 `：` 接上要判斷的句子，例如：`幫我看這句會怎麼處理：翻譯 https://...`"
        rest = rest.strip("「」\"'").strip()
        if not rest:
            return True, "", "❓ 麻煩在 `：` 後面貼上要判斷的句子。"
        return True, rest, ""

    return False, "", ""


def format_route_explain(info: dict, role: str = "user") -> str:
    if not isinstance(info, dict) or not info.get("success"):
        return "❌ 無法判斷路由。"

    is_admin = (role == "admin")
    requires_admin = bool(info.get("requires_admin"))
    action = str(info.get("action") or "")
    matched = str(info.get("matched") or "")
    intent = str(info.get("intent") or "")
    handler = str(info.get("handler") or "")

    if not is_admin and requires_admin:
        return (
            "🔎 路由判定（一般使用者）\n"
            f"- 類型: 系統指令（僅管理員可用）\n"
            "說明：此操作屬於系統改動/管理類，已隱藏內部命令碼細節。"
        )

    if not is_admin:
        public_action_map = {
            "translate": "翻譯/摘要（網頁/文字）",
            "music_generate": "製作音樂",
            "schedule_query": "行程查詢",
            "status_report": "狀態查詢",
            "chat_handler": "一般對話",
            "query_handler": "一般查詢",
        }
        public_action = public_action_map.get(action, action or (intent or "unknown"))
        return (
            "🔎 路由判定（一般使用者）\n"
            f"- 會走功能: {public_action}\n"
            f"- 判斷依據: {matched or 'n/a'}"
        )

    lines = [
        "🔎 路由判定（管理員）",
        f"- 功能(action): {action or 'n/a'}",
        f"- 意圖(intent): {intent or info.get('intent','') or 'n/a'}",
        f"- 判斷依據(matched): {matched or 'n/a'}",
        f"- 管理員限定: {requires_admin}",
    ]
    if handler:
        lines.append(f"- 內部處理器(handler): {handler}")
    return "\n".join(lines)
