"""Shared clarification gate for ambiguous MAGI work requests.

The gate runs before direct DB/tool routes. It pauses only when two plausible
interpretations would materially change the data set, target, or time range.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import threading


_TTL = timedelta(minutes=15)
_MAX_PENDING = 1000

_COUNT_RE = re.compile(r"(?:數量|筆數|多少|幾件|統計|總數)", re.I)
_CASE_DOMAIN_RE = re.compile(r"(?:案件|法扶|法律扶助)", re.I)
_CASE_ALL_SCOPE_RE = re.compile(r"(?:全部|所有|歷年|總共|含已結案|包括已結案|不分是否結案)", re.I)
_CASE_ACTIVE_SCOPE_RE = re.compile(r"(?:進行中|未結案|尚未最終結案|在辦|辦理中)", re.I)
_CASE_CLOSED_SCOPE_RE = re.compile(r"(?:只看已結案|僅看已結案|已最終結案|結案件數)", re.I)

_VAGUE_REFERENCE_RE = re.compile(
    r"(?:這個|那個|這些|那些|這份|那份|該份|該檔|剛才的|剛剛的|前面的|上面的)",
    re.I,
)
_FILE_ACTION_RE = re.compile(r"(?:預覽|下載|開啟|打開|讀取|摘要|分析|翻譯|歸檔)", re.I)
_FILE_DOMAIN_RE = re.compile(r"(?:檔案|文件|PDF|DOCX|附件|判決|卷宗|卷證)", re.I)
_SCHEDULE_QUERY_RE = re.compile(
    r"(?:查|列出|看看|告訴我|有什麼|有哪些).{0,12}(?:行程|日程|開庭|庭期|會議|期限|待辦)|"
    r"(?:行程|日程|開庭|庭期|會議|期限|待辦).{0,12}(?:查|列出|看看|有什麼|有哪些)",
    re.I,
)
_TIME_SCOPE_RE = re.compile(
    r"(?:今天|今日|明天|明日|後天|本週|這週|下週|本月|這個月|下個月|"
    r"\d{1,2}[/-]\d{1,2}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}月\d{1,2}日)",
    re.I,
)
_ANALYSIS_RE = re.compile(r"(?:分析|比較|評估|摘要|整理重點|判讀)", re.I)
_EXPLICIT_TARGET_RE = re.compile(
    r"(?:20\d{2}-\d{4}|\d{2,3}年度[^\s，。]{1,20}字第?\d+號|[^\s，。]{1,80}\.(?:pdf|docx?|txt|xlsx?))",
    re.I,
)
_NEW_TASK_RE = re.compile(r"^(?:請|幫我|麻煩|我要|我想|查|建立|新增|修改|更新|刪除|下載|分析|摘要|翻譯)", re.I)
_NON_TARGET_ACK_RE = re.compile(r"^(?:好|好的|好啊|可以|可|嗯|嗯嗯|照辦|就這樣|沒問題|同意|確認)$", re.I)


@dataclass(frozen=True)
class ClarificationDecision:
    needed: bool = False
    key: str = ""
    question: str = ""
    reason: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClarificationResolution:
    message: str
    resolved: bool = False
    pending: bool = False


def resolve_recent_case_reference(orch, user_id: str, platform: str, message: str) -> ClarificationResolution:
    """Resolve 「剛才那件」/「同一案件」 from user-authored recent IDs."""
    text = str(message or "").strip()
    if not re.search(r"(?:剛才那件|剛剛那件|同一案件)", text):
        return ClarificationResolution(message=text)
    try:
        from api.session.references import resolve_reference

        references = orch._session_store.list_recent(str(user_id or ""), kind="case")
        resolution = resolve_reference(text, references)
    except Exception:
        resolution = None
    if resolution is not None and not resolution.requires_clarification and resolution.selected:
        ref = resolution.selected.reference
        label = str(ref.label or ref.item_id).strip()
        return ClarificationResolution(
            message=f"{text}（上下文案件：{label}）",
            resolved=True,
        )

    candidates = list(getattr(resolution, "candidates", ()) or []) if resolution is not None else []
    if candidates:
        options = []
        for index, candidate in enumerate(candidates[:3], 1):
            ref = candidate.reference
            options.append(f"{index}. {str(ref.label or ref.item_id).strip()}")
        question = "「這件」可能指下列案件，請選擇：\n" + "\n".join(options)
    else:
        question = "我目前無法安全確定「這件」是哪一案。請提供本所案號、法院案號或當事人。"
    decision = ClarificationDecision(
        needed=True,
        key="recent_case_reference",
        question=question,
        reason="recent_case_reference_ambiguous",
        options=tuple(
            str(candidate.reference.label or candidate.reference.item_id).strip()
            for candidate in candidates[:3]
        ),
    )
    remember_clarification(orch, user_id, platform, text, decision)
    return ClarificationResolution(message=question, pending=True)


def detect_clarification_need(message: str, *, has_attachment: bool = False) -> ClarificationDecision:
    """Return a clarification only when the missing choice changes execution."""
    text = re.sub(r"\s+", "", str(message or ""))
    if not text:
        return ClarificationDecision()

    if _COUNT_RE.search(text) and _CASE_DOMAIN_RE.search(text):
        explicit_scope = bool(
            _CASE_ALL_SCOPE_RE.search(text)
            or _CASE_ACTIVE_SCOPE_RE.search(text)
            or _CASE_CLOSED_SCOPE_RE.search(text)
        )
        if not explicit_scope:
            return ClarificationDecision(
                needed=True,
                key="case_count_scope",
                question=(
                    "這裡的範圍有兩種合理理解。您要查：\n"
                    "1. 全部歷年紀錄（包含已結案）\n"
                    "2. 目前尚未最終結案\n"
                    "也可以回答「兩者都列」。"
                ),
                reason="case_count_scope_changes_result",
            )

    if (
        not has_attachment
        and _VAGUE_REFERENCE_RE.search(text)
        and _FILE_ACTION_RE.search(text)
        and _FILE_DOMAIN_RE.search(text)
        and not _EXPLICIT_TARGET_RE.search(text)
    ):
        return ClarificationDecision(
            needed=True,
            key="file_target",
            question="您指的是哪一個案件或檔案？請提供本所案號、當事人或檔名；若指剛上傳的附件，也可以回答「剛上傳的檔案」。",
            reason="file_target_unresolved",
        )

    if _SCHEDULE_QUERY_RE.search(text) and not _TIME_SCOPE_RE.search(text):
        return ClarificationDecision(
            needed=True,
            key="schedule_range",
            question="您要查哪個時間範圍？例如今天、本週、下週或指定日期。",
            reason="schedule_range_missing",
        )

    if (
        not has_attachment
        and _ANALYSIS_RE.search(text)
        and _VAGUE_REFERENCE_RE.search(text)
        and not _EXPLICIT_TARGET_RE.search(text)
    ):
        return ClarificationDecision(
            needed=True,
            key="analysis_target",
            question="您要分析哪一份內容或哪一個案件？請提供本所案號、檔名、貼上內容，或直接上傳檔案。",
            reason="analysis_target_unresolved",
        )

    # Broader office-domain assessment covers write targets, drafting, legal
    # research and ambiguous clock times.  Keep this after the established
    # high-precision rules above so existing wording and behaviour remain
    # stable for common case-count, file and schedule queries.
    try:
        from api.routing.office_cognition import assess_office_request

        understanding = assess_office_request(message, has_attachment=has_attachment)
        if understanding.needs_clarification:
            return ClarificationDecision(
                needed=True,
                key=understanding.clarification_key,
                question=understanding.clarification_question,
                reason=understanding.clarification_reason,
            )
    except Exception:
        # The clarification gate must remain fail-safe during a rolling
        # upgrade; legacy high-confidence checks above continue to apply.
        pass
    return ClarificationDecision()


def _pending_store(orch) -> tuple[OrderedDict, threading.Lock]:
    store = getattr(orch, "_query_clarification_pending", None)
    lock = getattr(orch, "_query_clarification_lock", None)
    if not isinstance(store, OrderedDict):
        store = OrderedDict()
        setattr(orch, "_query_clarification_pending", store)
    if lock is None:
        lock = threading.Lock()
        setattr(orch, "_query_clarification_lock", lock)
    return store, lock


def _pending_key(user_id: str, platform: str) -> str:
    return f"{str(platform or '').strip().lower()}:{str(user_id or '').strip()}"


def remember_clarification(orch, user_id: str, platform: str, message: str, decision: ClarificationDecision) -> None:
    store, lock = _pending_store(orch)
    now = datetime.now(timezone.utc)
    with lock:
        expired = [key for key, value in store.items() if value["expires_at"] <= now]
        for key in expired:
            store.pop(key, None)
        key = _pending_key(user_id, platform)
        store[key] = {
            "original": str(message or "").strip(),
            "decision_key": decision.key,
            "question": decision.question,
            "options": list(decision.options),
            "expires_at": now + _TTL,
        }
        store.move_to_end(key)
        while len(store) > _MAX_PENDING:
            store.popitem(last=False)


def _case_scope_answer(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    if re.search(r"(?:兩者|都列|都要|全部並區分|1和2|1、2)", compact, re.I):
        return "全部歷年紀錄，並區分尚未最終結案與已最終結案"
    if re.fullmatch(r"(?:1|第一|全部|全部歷年|所有|歷年|含已結案|包括已結案)", compact, re.I):
        return "全部歷年紀錄（包含已結案）"
    if re.fullmatch(r"(?:2|第二|目前|未結案|進行中|在辦|尚未結案|尚未最終結案)", compact, re.I):
        return "目前尚未最終結案"
    return ""


def resolve_pending_clarification(orch, user_id: str, platform: str, message: str) -> ClarificationResolution:
    """Resolve a short follow-up and reconstruct the original work request."""
    store, lock = _pending_store(orch)
    now = datetime.now(timezone.utc)
    key = _pending_key(user_id, platform)
    with lock:
        pending = store.get(key)
        if not pending:
            return ClarificationResolution(message=str(message or ""))
        if pending["expires_at"] <= now:
            store.pop(key, None)
            return ClarificationResolution(message=str(message or ""))

    answer = str(message or "").strip()
    compact = re.sub(r"\s+", "", answer)
    if compact in {"取消", "不用了", "算了", "不要查了"}:
        with lock:
            store.pop(key, None)
        return ClarificationResolution(message="", resolved=True)

    decision_key = str(pending["decision_key"])
    if decision_key == "case_count_scope":
        supplement = _case_scope_answer(answer)
        if not supplement:
            if len(answer) > 30 or _NEW_TASK_RE.search(answer):
                with lock:
                    store.pop(key, None)
                return ClarificationResolution(message=answer)
            return ClarificationResolution(message=str(pending["question"]), pending=True)
    elif decision_key == "schedule_range":
        if not _TIME_SCOPE_RE.search(answer):
            if _NEW_TASK_RE.search(answer):
                with lock:
                    store.pop(key, None)
                return ClarificationResolution(message=answer)
            return ClarificationResolution(message=str(pending["question"]), pending=True)
        supplement = answer
    elif decision_key == "schedule_meridiem":
        if not re.search(r"(?:上午|下午|中午|晚上|凌晨)", answer):
            if _NEW_TASK_RE.search(answer):
                with lock:
                    store.pop(key, None)
                return ClarificationResolution(message=answer)
            return ClarificationResolution(message=str(pending["question"]), pending=True)
        supplement = answer
    elif decision_key == "recent_case_reference":
        options = [str(item or "").strip() for item in (pending.get("options") or []) if str(item or "").strip()]
        if compact in {str(index) for index in range(1, len(options) + 1)}:
            supplement = options[int(compact) - 1]
        elif _EXPLICIT_TARGET_RE.search(answer) or re.fullmatch(r"[\u3400-\u9fff○○·]{2,12}", compact):
            supplement = answer
        else:
            if _NEW_TASK_RE.search(answer):
                with lock:
                    store.pop(key, None)
                return ClarificationResolution(message=answer)
            return ClarificationResolution(message=str(pending["question"]), pending=True)
    else:
        if not answer:
            return ClarificationResolution(message=str(pending["question"]), pending=True)
        explicit_answer_target = bool(
            _EXPLICIT_TARGET_RE.search(answer)
            or re.search(r"(?:剛上傳|附件|本所案號|當事人|檔名)", answer, re.I)
        )
        # Acknowledgements do not resolve a target question.  Treating "好"
        # as an identifier causes the original operation to resume without a
        # safe file/case target.
        if _NON_TARGET_ACK_RE.fullmatch(compact):
            return ClarificationResolution(message=str(pending["question"]), pending=True)
        if not explicit_answer_target and _NEW_TASK_RE.search(answer):
            with lock:
                store.pop(key, None)
            return ClarificationResolution(message=answer)
        supplement = answer

    with lock:
        store.pop(key, None)
    combined = f"{pending['original']}（使用者補充：{supplement}）"
    return ClarificationResolution(message=combined, resolved=True)


def request_clarification_if_needed(
    orch,
    user_id: str,
    platform: str,
    message: str,
    *,
    has_attachment: bool = False,
) -> str:
    decision = detect_clarification_need(message, has_attachment=has_attachment)
    if not decision.needed:
        return ""
    remember_clarification(orch, user_id, platform, message, decision)
    try:
        orch._append_route_trace(
            str(user_id or ""),
            str(platform or ""),
            "clarification_gate",
            decision.key,
            {"reason": decision.reason},
        )
    except Exception:
        pass
    return decision.question
