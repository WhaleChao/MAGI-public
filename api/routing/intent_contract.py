"""Deterministic intent contract for MAGI conversational routing.

This module is intentionally small and side-effect free.  It decides the
semantic lane for a user message before stale task wizards, embedding routers,
or LLM fallback can consume it.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from api.help_text import HELP_ALIASES
try:
    from api.routing.command_prefixes import split_heavy_prefix, strip_heavy_prefix
except Exception:
    _HEAVY_PREFIX_FALLBACK_RE = re.compile(
        r"^\s*[＠@]\s*(?:heavy|重型)(?=$|[\s:：,，、。!！?？\-–—]|[\u4e00-\u9fff])"
        r"\s*[:：,，、。!！?？\-–—]*\s*",
        re.IGNORECASE,
    )

    def split_heavy_prefix(message: str) -> tuple[bool, str]:  # type: ignore[no-redef]
        text = str(message or "").replace("＠", "@").replace("\u3000", " ").lstrip()
        match = _HEAVY_PREFIX_FALLBACK_RE.match(text)
        return (True, text[match.end():].strip()) if match else (False, text)

    def strip_heavy_prefix(message: str) -> str:  # type: ignore[no-redef]
        return split_heavy_prefix(message)[1]


KIND_EMPTY = "empty"
KIND_HELP_COMMAND = "help_command"
KIND_EXPLICIT_COMMAND = "explicit_command"
KIND_META_CAPABILITY = "meta_capability"
KIND_TOOL_CAPABILITY = "tool_capability"
KIND_BUSY_STATUS = "busy_status"
KIND_REALTIME_ACTION = "realtime_action"
KIND_CASUAL_CHAT = "casual_chat"
KIND_CANCEL_REQUEST = "cancel_request"
KIND_CORRECTION_REQUEST = "correction_request"
KIND_EXPLICIT_TASK = "explicit_task"
KIND_AGENT_TASK = "agent_task"
KIND_STATEFUL_REPLY = "stateful_reply"
KIND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntentDecision:
    kind: str
    confidence: float
    reason: str
    bypass_state: bool = False
    execute_pre_llm: bool = False
    realtime_kind: str = ""
    tool_hint: str = ""


@dataclass(frozen=True)
class NormalizedIntent:
    original: str
    text: str
    heavy_opt_in: bool
    decision: IntentDecision
    route_intent: str
    allow_tool_dispatch: bool
    heavy_route_requested: bool


_TOOL_CAPABILITY_RE = re.compile(
    r"(?:你|magi|系統|這套系統).{0,10}"
    r"(?:會|可以|能不能|能否|可否|能|支援).{0,24}"
    r"(?:查天氣|天氣|氣象|查股票|股票|股價|匯率|查判決|判決|法規|法條|案件|檔案|"
    r"行程|日曆|摘要|翻譯|ocr|搜尋(?:新聞|網路)?|新聞|下載(?:卷宗|檔案)?|卷宗|"
    r"寫(?:書狀|文件)?|書狀|建立(?:行程|案件)?|上傳(?:檔案|附件)?)",
    re.IGNORECASE,
)
_TOOL_ACTION_VERB_RE = re.compile(
    r"(?:查一下|查詢|幫我查|幫忙查|幫.{0,6}查|幫我.{0,6}(?:搜尋|下載|上傳|建立|寫)|"
    r"看一下|看看|告訴我|請.{0,6}查|麻煩.{0,6}查|lookup|search|get)",
    re.IGNORECASE,
)
_BUSY_META_RE = re.compile(
    r"(?:"
    r"(?:你|magi|系統).{0,12}(?:在忙什麼|忙什麼|現在在做什麼|正在做什麼|為什麼忙|為何忙|卡住嗎|是不是忙)|"
    r"(?:模型|工具|系統).{0,8}(?:忙|卡住|離線|逾時)"
    r")",
    re.IGNORECASE,
)
_CASUAL_CHAT_RE = re.compile(
    r"(?:"
    r"一般聊天|閒聊|聊天模式|只是聊天|只想聊天|想跟你聊|跟你聊聊|先聊聊|陪我聊|"
    r"我只是問|不是任務|不要執行|先不要執行|不要當任務|不要填表|"
    r"^(?:你好|哈囉|嗨|早安|午安|晚安|在嗎|謝謝|謝啦|辛苦了)[。！!？?]?$|"
    r"(?:你|magi).{0,10}(?:可以聊天|會聊天|聽得懂|像人一樣聊|陪我聊天)"
    r")",
    re.IGNORECASE,
)
_CANCEL_REQUEST_RE = re.compile(
    r"^(?:"
    r"取消|先取消|不用了|不要了|算了|先不要|不要送出|先不要送出|停止|停下|暫停|退出|中止|放棄|"
    r"stop|cancel|abort|never mind|nevermind"
    r")(?:[。.!！?？\s]*(?:這個|剛剛|剛才|上一個|前一個|流程|任務|操作|送出|指令|申請|動作|全部|all)?)?$",
    re.IGNORECASE,
)
_CANCEL_TARGET_RE = re.compile(
    r"^(?:取消|停止|關掉|關閉|中止|放棄).{1,40}(?:流程|任務|操作|送出|申請|提醒|通知|警報|指令)$",
    re.IGNORECASE,
)
_CORRECTION_PREFIX_RE = re.compile(
    r"^(?:"
    r"更正|修正|改成|改為|改一下|補充更正|我更正|我修正|我剛剛說錯|剛剛說錯|剛才說錯|"
    r"上面說錯|前面說錯|正確是|應該是|correction"
    r")\s*[:：,，、]?\s*\S",
    re.IGNORECASE,
)
_CORRECTION_PAIR_RE = re.compile(
    r"(?:不是|不對|有誤|錯了).{0,30}(?:正確是|應該是|改成|改為|而是)",
    re.IGNORECASE,
)
_GENERAL_CHAT_BOUNDARY_RE = re.compile(
    r"(?:"
    r"^(?:/help|help|指令|功能|能力|你是誰|magi是誰)$|"
    r"(?:請問)?(?:你|magi|這個系統|這套系統|這裡).{0,10}"
    r"(?:可以|能|會|能做|能做到|做得到|可以做).{0,10}"
    r"(?:什麼|甚麼|哪些|何事|事情|事|功能|能力)|"
    r"(?:有什麼|有哪些)(?:功能|能力|技能|指令)|"
    r"(?:功能|能力|技能|指令)(?:列表|清單|一覽)|"
    r"(?:怎麼|如何|要怎麼|該怎麼).{0,10}(?:使用|用)(?:你|magi|這個系統|這套系統)|"
    r"(?:一般聊天|閒聊|聊天模式|只是聊天|先聊聊|我只是問|不是任務|不要執行|先不要執行)|"
    r"(?:你|magi).{0,10}(?:在問什麼|現在在做什麼|在忙什麼|忙什麼|為什麼問|聽得懂|可以聊天|陪我聊)"
    r")",
    re.IGNORECASE,
)
_NEW_TASK_BOUNDARY_RE = re.compile(
    r"(?:"
    r"建案|新案件|案件清單|列出案件|查案件|案件狀態|業務概況|"
    r"今天(?:的)?行程|明天(?:的)?行程|本週(?:的)?行程|這週(?:的)?行程|行事曆|排庭|排開會|排會議|"
    r"開資料夾|開啟資料夾|資料夾樹|預覽|下載|分享|上傳|搜尋檔案|列出檔案|"
    r"委任狀|契約書|收據|存證信函|書狀|起訴狀|答辯狀|"
    r"翻譯|摘要|總結|整理|查判決|找判決|查裁判|查法規|查法條|"
    r"記收入|記支出|帳務查詢|報價單|草擬|起草|畫圖|生成圖|系統狀態|健康檢查"
    r")",
    re.IGNORECASE,
)
_NEW_TASK_PREFIX_RE = re.compile(
    r"^(?:@magi|/|幫我|請幫我|請你|請|麻煩|我要|我想|我需要|可以幫我|幫忙|幫)",
    re.IGNORECASE,
)
_SCHEDULE_QUERY_RE = re.compile(
    r"(?:"
    r"(?:今天|今日|明天|明日|後天|本週|這週|下週|本月|這個月).{0,12}(?:行程|日程|開庭|庭期|會議|期限|待辦)|"
    r"(?:行程|日程|開庭|庭期|會議|期限|待辦).{0,12}(?:今天|今日|明天|明日|後天|本週|這週|下週|本月|這個月)"
    r")",
    re.IGNORECASE,
)
_SYSTEM_OPERATION_QUERY_RE = re.compile(
    r"(?:"
    r"(?:檢查|確認|查看|看一下|查一下).{0,12}(?:MAGI|系統|服務|外網|網路硬碟|NAS).{0,12}(?:健康|狀態|紅燈|黃燈|連線)?|"
    r"(?:MAGI|系統|服務|外網|網路硬碟|NAS).{0,12}(?:健康|狀態|紅燈|黃燈|連線).{0,8}(?:如何|怎樣|正常嗎|檢查|確認)?"
    r")",
    re.IGNORECASE,
)
_NEW_TASK_COMMAND_START_RE = re.compile(
    r"^(?:"
    r"建案|新案件|案件清單|列出案件|查案件|案件狀態|業務概況|"
    r"開資料夾|開啟資料夾|資料夾樹|預覽|下載|分享|上傳|搜尋檔案|列出檔案|"
    r"委任狀|契約書|收據|存證信函|書狀|起訴狀|答辯狀|"
    r"翻譯|摘要|總結|重點整理|查判決|找判決|查裁判|查法規|查法條|"
    r"記收入|記支出|帳務查詢|報價單|排庭|排開會|排會議|草擬|起草|畫圖|生成圖|"
    r"系統狀態|健康檢查"
    r")",
    re.IGNORECASE,
)
_STATEFUL_REPLY_RE = re.compile(
    r"^(?:[\d,]+(?:\.\d+)?|[A-Fa-f0-9]{6,12}|[\u4e00-\u9fff]{2,12}(?:地方法院|高等法院|最高法院|法院|地檢署|地方檢察署))$"
)
_AGENTIC_VERB_RE = re.compile(
    r"(?:"
    r"請|幫我|請幫我|麻煩|我想知道|我想了解|我需要|可以幫|能不能幫|"
    r"查|查詢|搜尋|找|檢索|研究|整理|分析|比較|摘要|翻譯|計算|列出|"
    r"說明|解釋|評估|判斷|推論|規劃|建議|彙整|比對|處理|補抓|抓回|復原"
    r")",
    re.IGNORECASE,
)
_AGENTIC_DOMAIN_RE = re.compile(
    r"(?:"
    r"案件|當事人|案號|法院|判決|裁判|實務見解|實務|法規|法條|法律|民法|刑法|"
    r"行程|日曆|開庭|庭期|調解|檔案|文件|資料|卷證|卷宗|附件|信件|通知|繳費單|"
    r"法扶|閱卷|筆錄|逐字稿|pdf|word|docx|"
    r"天氣|新聞|匯率|股價|股票|市場|金額|利息|日期|期限|合約|契約"
    r")",
    re.IGNORECASE,
)
_AGENTIC_QUESTION_RE = re.compile(
    r"(?:"
    r"為什麼|為何|怎麼|如何|哪裡|哪個|哪些|是否|是不是|能不能|可以嗎|"
    r"有沒有|什麼|甚麼|何時|哪天|幾點|多少|幾件|幾筆|差異|重點|原因|依據|風險|下一步|該怎麼"
    r")",
    re.IGNORECASE,
)
_AGENTIC_WRITE_ACTION_RE = re.compile(
    r"(?:"
    r"送出|提交|刪除|移除|移到|搬移|上傳|同步|結案回報|法扶回報|"
    r"閱卷聲請|確認送出|建立案件|新增案件|產製委任狀|生成委任狀|做委任狀|"
    r"下載|補抓|抓回|修復|修好|排除"
    r")",
    re.IGNORECASE,
)
_EXPLICIT_WORK_ACTION_RE = re.compile(
    r"(?:"
    r"建立|新增|修改|更新|更正|改到|改成|改為|延後|提前|取消|刪除|移除|搬移|移到|移動|上傳|同步|送出|提交|下載|"
    r"產生|生成|製作|草擬|起草|排定|建立提醒|完成|標示|回報|報結|結案|開辦|歸檔|"
    r"補抓|抓回|補登|修復|修好|排除|觸發"
    r")",
    re.IGNORECASE,
)
_EXPLICIT_WORK_DOMAIN_RE = re.compile(
    r"(?:"
    r"案件|案號|當事人|檔案|文件|資料夾|待辦|期限|提醒|日曆|行程|開庭|庭期|法庭|的庭|法扶|閱卷|筆錄|"
    r"書狀|委任狀|報告|附件|卷證|卷宗|裁判|判決|資料|紀錄|帳務|收入|支出|"
    r"信件|通知|繳費單|系統|服務|紅燈|黃燈|PDF|DOCX"
    r")",
    re.IGNORECASE,
)
_AGENTIC_ANALYSIS_INTENT_RE = re.compile(
    r"(?:比較|分析|整理成|整理重點|重點|見解|評估|風險|策略|摘要|彙整|脈絡|趨勢|三點|清單)",
    re.IGNORECASE,
)


def compact_message(message: str) -> str:
    return re.sub(r"\s+", "", strip_heavy_prefix(message).strip().lower())


def looks_like_cancel_request(message: str) -> bool:
    text = strip_heavy_prefix(message).strip()
    if not text or len(text) > 80:
        return False
    compact = compact_message(text)
    return bool(_CANCEL_REQUEST_RE.fullmatch(compact) or _CANCEL_TARGET_RE.search(text))


def looks_like_correction_request(message: str) -> bool:
    text = strip_heavy_prefix(message).strip()
    if not text or len(text) > 500:
        return False
    compact = compact_message(text)
    if compact in {"不是任務", "不是查詢", "不是指令", "不要執行"}:
        return False
    return bool(_CORRECTION_PREFIX_RE.search(text) or _CORRECTION_PAIR_RE.search(compact))


def looks_like_model_capability_query(message: str) -> bool:
    compact = compact_message(message)
    if not compact or len(compact) > 90:
        return False
    talks_to_magi = any(token in compact for token in ("你", "magi", "系統"))
    asks_model = ("模型" in compact or "model" in compact) and any(
        token in compact for token in ("什麼", "甚麼", "哪個", "使用", "現在", "目前")
    )
    asks_capability = any(
        token in compact
        for token in (
            "可以做什麼",
            "能做什麼",
            "會做什麼",
            "能做到什麼",
            "能做到什麼事",
            "能做到哪些",
            "有什麼功能",
            "有哪些功能",
            "有什麼能力",
            "有哪些能力",
            "什麼功能",
            "什麼能力",
            "哪些事情",
            "能做哪些事",
            "做得到什麼",
        )
    )
    return asks_capability or (talks_to_magi and asks_model)


def looks_like_tool_capability_query(message: str) -> bool:
    text = strip_heavy_prefix(message).strip()
    if not text or len(text) > 90:
        return False
    if _TOOL_ACTION_VERB_RE.search(text):
        return False
    return bool(_TOOL_CAPABILITY_RE.search(text))


def looks_like_busy_meta_query(message: str) -> bool:
    text = strip_heavy_prefix(message).strip()
    return bool(text and len(text) <= 120 and _BUSY_META_RE.search(text))


def classify_realtime_kind(message: str) -> str:
    try:
        from skills.engine.realtime_data_gateway import classify_realtime_query

        return str(classify_realtime_query(strip_heavy_prefix(message)) or "")
    except Exception:
        return ""


def looks_like_casual_chat_boundary(message: str) -> bool:
    text = strip_heavy_prefix(message).strip()
    if not text:
        return False
    compact = compact_message(text)
    if len(compact) > 120:
        return False
    return bool(_CASUAL_CHAT_RE.search(compact) or _GENERAL_CHAT_BOUNDARY_RE.search(compact))


def looks_like_new_task_boundary(message: str) -> bool:
    text = strip_heavy_prefix(message).strip()
    if not text:
        return False
    compact = compact_message(text)
    if len(compact) > 220:
        return False
    if re.fullmatch(r"(今天|明天|本週|這週)(?:的)?(?:行程|會議)?", compact):
        return True
    if _SCHEDULE_QUERY_RE.search(compact):
        return True
    if _SYSTEM_OPERATION_QUERY_RE.search(compact):
        return True
    if _NEW_TASK_COMMAND_START_RE.search(compact):
        return True
    return bool(_NEW_TASK_BOUNDARY_RE.search(compact) and _NEW_TASK_PREFIX_RE.search(compact))


def looks_like_agentic_request(message: str) -> bool:
    """Broad natural-language requests that should be handled by the ReAct agent.

    This deliberately excludes short form replies and write-side workflows.
    Dedicated command handlers still run before the agent; this contract is a
    second-layer route for analysis/search/planning prompts that would otherwise
    fall into generic chat templates.
    """
    text = strip_heavy_prefix(message).strip()
    if not text:
        return False
    compact = compact_message(text)
    if len(compact) < 4 or len(compact) > 900:
        return False
    if _STATEFUL_REPLY_RE.fullmatch(compact):
        return False
    if looks_like_model_capability_query(text) or looks_like_tool_capability_query(text):
        return False
    if looks_like_busy_meta_query(text) or looks_like_casual_chat_boundary(text):
        return False
    if _AGENTIC_WRITE_ACTION_RE.search(compact):
        return False

    has_verb = bool(_AGENTIC_VERB_RE.search(compact))
    has_domain = bool(_AGENTIC_DOMAIN_RE.search(compact))
    has_question = bool(_AGENTIC_QUESTION_RE.search(compact))
    if has_verb and (has_domain or has_question):
        return True
    if has_domain and has_question:
        return True
    # Pure analytical requests can be domain-free: e.g. "請比較這兩段差異".
    if has_verb and any(token in compact for token in ("比較", "分析", "整理", "摘要", "翻譯", "計算", "規劃", "建議")):
        return True
    return False


def looks_like_explicit_work_request(message: str) -> bool:
    """Detect concrete workflow mutations without guessing from an LLM.

    Capability questions and casual/meta utterances are filtered by earlier
    contract lanes.  Requiring both an action and a work-domain noun prevents
    ordinary conversation such as「幫我修改一下說法」from being dispatched as
    an operational command.
    """
    text = strip_heavy_prefix(message).strip()
    if not text or len(text) > 900:
        return False
    compact = compact_message(text)
    return bool(
        _EXPLICIT_WORK_ACTION_RE.search(compact)
        and _EXPLICIT_WORK_DOMAIN_RE.search(compact)
    )


def looks_like_operational_lookup_request(message: str) -> bool:
    """Detect read-only schedule/health lookups that must reach tools."""
    text = strip_heavy_prefix(message).strip()
    if not text:
        return False
    compact = compact_message(text)
    if _EXPLICIT_WORK_ACTION_RE.search(compact):
        return False
    return bool(
        _SCHEDULE_QUERY_RE.search(compact)
        or _SYSTEM_OPERATION_QUERY_RE.search(compact)
    )


def classify_intent_contract(message: str) -> IntentDecision:
    text = strip_heavy_prefix(message).strip()
    if not text:
        return IntentDecision(KIND_EMPTY, 1.0, "empty")

    lowered = text.lower()
    if lowered in HELP_ALIASES:
        return IntentDecision(KIND_HELP_COMMAND, 1.0, "explicit_help_alias", bypass_state=True)
    if lowered.startswith(("/", "!")):
        return IntentDecision(KIND_EXPLICIT_COMMAND, 1.0, "explicit_command_prefix", bypass_state=True)
    if looks_like_model_capability_query(text):
        return IntentDecision(KIND_META_CAPABILITY, 0.95, "model_or_capability_question", bypass_state=True, execute_pre_llm=True)
    if looks_like_tool_capability_query(text):
        return IntentDecision(KIND_TOOL_CAPABILITY, 0.93, "tool_capability_question", bypass_state=True, execute_pre_llm=True)
    if looks_like_busy_meta_query(text):
        return IntentDecision(KIND_BUSY_STATUS, 0.94, "busy_or_runtime_meta_question", bypass_state=True, execute_pre_llm=True)
    if looks_like_cancel_request(text):
        return IntentDecision(KIND_CANCEL_REQUEST, 0.96, "explicit_cancel_request", bypass_state=False)
    if looks_like_correction_request(text):
        return IntentDecision(KIND_CORRECTION_REQUEST, 0.90, "explicit_correction_request", bypass_state=False)

    if looks_like_casual_chat_boundary(text):
        return IntentDecision(KIND_CASUAL_CHAT, 0.86, "casual_or_meta_chat_boundary", bypass_state=True, execute_pre_llm=True)
    if looks_like_operational_lookup_request(text):
        return IntentDecision(KIND_AGENT_TASK, 0.93, "operational_tool_lookup", bypass_state=True)
    if looks_like_explicit_work_request(text):
        return IntentDecision(KIND_EXPLICIT_TASK, 0.94, "explicit_workflow_action", bypass_state=True)
    # Resolve concrete office work before real-time facts.  Otherwise a phrase
    # such as「把明天下午三點的庭改到四點」is stolen by the weather lane solely
    # because it contains「明天」.
    realtime_kind = classify_realtime_kind(text)
    if realtime_kind:
        return IntentDecision(
            KIND_REALTIME_ACTION,
            0.92,
            "authoritative_realtime_request",
            bypass_state=True,
            execute_pre_llm=True,
            realtime_kind=realtime_kind,
        )
    if looks_like_agentic_request(text) and _AGENTIC_ANALYSIS_INTENT_RE.search(compact_message(text)):
        return IntentDecision(KIND_AGENT_TASK, 0.84, "analytic_agentic_request", bypass_state=True)
    if looks_like_new_task_boundary(text):
        return IntentDecision(KIND_EXPLICIT_TASK, 0.85, "new_top_level_task", bypass_state=True)
    if _STATEFUL_REPLY_RE.fullmatch(compact_message(text)):
        return IntentDecision(KIND_STATEFUL_REPLY, 0.82, "short_field_or_confirmation_reply")
    if looks_like_agentic_request(text):
        return IntentDecision(KIND_AGENT_TASK, 0.78, "broad_agentic_request", bypass_state=True)
    return IntentDecision(KIND_UNKNOWN, 0.35, "no_deterministic_contract_match")


def route_intent_for_decision(decision: IntentDecision) -> str:
    """Map deterministic semantic lanes to the legacy CHAT/CMD/QUERY router."""
    kind = str(getattr(decision, "kind", "") or "")
    if kind in {KIND_HELP_COMMAND, KIND_EXPLICIT_COMMAND, KIND_EXPLICIT_TASK, KIND_CANCEL_REQUEST}:
        return "CMD"
    if kind in {KIND_REALTIME_ACTION, KIND_AGENT_TASK}:
        return "QUERY"
    return "CHAT"


def normalize_message_intent(message: str) -> NormalizedIntent:
    """Return a stable, side-effect-free routing view for a user message."""
    original = str(message or "")
    heavy_opt_in, text = split_heavy_prefix(original)
    text = str(text or "").strip()
    decision = classify_intent_contract(text)
    route_intent = route_intent_for_decision(decision)
    non_tool_kinds = {
        KIND_EMPTY,
        KIND_META_CAPABILITY,
        KIND_TOOL_CAPABILITY,
        KIND_BUSY_STATUS,
        KIND_CASUAL_CHAT,
        KIND_CANCEL_REQUEST,
        KIND_CORRECTION_REQUEST,
        KIND_STATEFUL_REPLY,
        KIND_UNKNOWN,
    }
    allow_tool_dispatch = route_intent in {"CMD", "QUERY"} and decision.kind not in non_tool_kinds
    heavy_route_requested = bool(heavy_opt_in and allow_tool_dispatch)
    return NormalizedIntent(
        original=original,
        text=text,
        heavy_opt_in=bool(heavy_opt_in),
        decision=decision,
        route_intent=route_intent,
        allow_tool_dispatch=bool(allow_tool_dispatch),
        heavy_route_requested=heavy_route_requested,
    )


def should_bypass_stateful_forms(message: str) -> bool:
    return classify_intent_contract(message).bypass_state


__all__ = [
    "IntentDecision",
    "NormalizedIntent",
    "KIND_EMPTY",
    "KIND_HELP_COMMAND",
    "KIND_EXPLICIT_COMMAND",
    "KIND_META_CAPABILITY",
    "KIND_TOOL_CAPABILITY",
    "KIND_BUSY_STATUS",
    "KIND_REALTIME_ACTION",
    "KIND_CASUAL_CHAT",
    "KIND_CANCEL_REQUEST",
    "KIND_CORRECTION_REQUEST",
    "KIND_EXPLICIT_TASK",
    "KIND_AGENT_TASK",
    "KIND_STATEFUL_REPLY",
    "KIND_UNKNOWN",
    "classify_intent_contract",
    "classify_realtime_kind",
    "compact_message",
    "looks_like_busy_meta_query",
    "looks_like_cancel_request",
    "looks_like_casual_chat_boundary",
    "looks_like_correction_request",
    "looks_like_model_capability_query",
    "looks_like_new_task_boundary",
    "looks_like_explicit_work_request",
    "looks_like_operational_lookup_request",
    "looks_like_agentic_request",
    "looks_like_tool_capability_query",
    "normalize_message_intent",
    "route_intent_for_decision",
    "should_bypass_stateful_forms",
]
